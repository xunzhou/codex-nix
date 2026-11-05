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
python3 "$repo_root/tests/static-runtime.py" "${isolated[@]}" /app/codex-code-mode-host --listen stdio
printf 'Exported binary pair passed without /nix/store or shared libraries.\n'
