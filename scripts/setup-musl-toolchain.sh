#!/usr/bin/env bash
# Configure a GitHub runner for the static musl release build.
#
# Reuses upstream's own musl toolchain scripts (static OpenSSL, libcap, zig
# cc wrappers) from the checksum-pinned source archive in build.json, so the
# release links exactly like upstream's npm binaries. Requires zig on PATH and
# writes the toolchain environment to $GITHUB_ENV.
set -euo pipefail

: "${GITHUB_ENV:?GITHUB_ENV is required}"
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
manifest="${CODEX_BUILD_MANIFEST:-$repo_root/build.json}"

read -r target url sha256 < <(python3 - "$manifest" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
release = data['releases'][data['default_version']]
print(data['build']['release_target'], release['source']['url'], release['source']['sha256'])
PY
)
[[ "$target" == *-unknown-linux-musl ]] || { echo "release target $target is not musl" >&2; exit 1; }

work=$(mktemp -d "${RUNNER_TEMP:-/tmp}/codex-musl-setup.XXXXXX")
curl -fL --retry 3 -o "$work/source.tar.gz" "$url"
echo "$sha256  $work/source.tar.gz" | sha256sum -c -
tar -xzf "$work/source.tar.gz" -C "$work" --wildcards --strip-components=1 \
  '*/.github/scripts/install-musl-build-tools.sh' '*/.github/scripts/install-musl-openssl.sh'

TARGET="$target" bash "$work/.github/scripts/install-musl-build-tools.sh"
{
  echo "CARGO_BUILD_TARGET=$target"
  # Upstream disables aws-lc jitter entropy on musl builders.
  echo "AWS_LC_SYS_NO_JITTER_ENTROPY=1"
  target_var="${target//-/_}"
  echo "AWS_LC_SYS_NO_JITTER_ENTROPY_${target_var}=1"
} >>"$GITHUB_ENV"
