#!/usr/bin/env bash
# Exercise the exported tarball in an empty filesystem, without /nix/store.
set -euo pipefail
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
package=$(realpath "${1:?usage: static-portability.sh NIX_OUTPUT}")
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT

if [[ -n "${2:-}" ]]; then
  asset=$(realpath "$2")
else
  asset=$(python3 "$repo_root/scripts/export-native.py" --package "$package" --output-dir "$scratch/archive")
fi
mkdir "$scratch/unpacked"
tar -xzf "$asset" -C "$scratch/unpacked"
for binary in codex codex-code-mode-host; do
  cmp "$package/bin/$binary" "$scratch/unpacked/$binary"
done

# The root contains only the exported pair, /proc, /dev, and a writable /tmp.
# Clear host variables so certificates and libraries cannot come from Nix.
isolated=(bwrap --unshare-all --die-with-parent --clearenv
  --ro-bind "$scratch/unpacked" /app --proc /proc --dev /dev --tmpfs /tmp
  --setenv HOME /tmp --setenv PATH /app --chdir /tmp)
"${isolated[@]}" /app/codex --version
"${isolated[@]}" /app/codex-code-mode-host --help >/dev/null
# HTTP/2 support belongs to the client outside the sandbox, never the tarball.
# Resolve it from this checkout's locked nixpkgs on PR and external release workers.
# sudo may omit the multi-user Nix profile from PATH.
nix_command=$(command -v nix || true)
nix_command=${nix_command:-/nix/var/nix/profiles/default/bin/nix}
smoke_python=$("$nix_command" --extra-experimental-features 'nix-command flakes' \
  build --impure --no-link --print-out-paths --expr '
  { repo }: let
    flake = builtins.getFlake repo;
    pkgs = import flake.inputs.nixpkgs { system = builtins.currentSystem; };
  in pkgs.python3.withPackages (ps: [ ps.h2 ])
' --argstr repo "$repo_root")
"$smoke_python/bin/python3" "$repo_root/tests/static-runtime-test.py"
"$smoke_python/bin/python3" "$repo_root/tests/static-runtime.py" "${isolated[@]}" /app/codex-code-mode-host --listen grpc+stdio://
printf 'Exported binary pair passed without /nix/store or shared libraries.\n'
