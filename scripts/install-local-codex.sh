#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export CODEX_BUILD_MANIFEST="${CODEX_BUILD_MANIFEST:-$root/native-build.json}"
export CODEX_PATCH_ROOT="${CODEX_PATCH_ROOT:-$root/patches}"
export CODEX_PATCH_CACHE="${CODEX_PATCH_CACHE:-$root/.native-cache}"
case "${1:-}" in
  --reviewed-version|--restore-reviewed|--install-reviewed)
    reviewed_version=$(python3 - "${CODEX_BUILD_MANIFEST:-${CODEX_PATCH_ROOT:-$HOME/.local/share/codex-patcher/patches}/../build.json}" <<'PY'
import json, re, sys
with open(sys.argv[1]) as stream:
    manifest = json.load(stream)
version = manifest.get('default_version', '')
if (manifest.get('schema_version') != 1
        or not re.fullmatch(r'\d+\.\d+\.\d+', version)
        or version not in manifest.get('releases', {})
        or not manifest['releases'][version].get('patches')):
    sys.exit('Codex patcher: no valid reviewed default version in manifest')
print(version)
PY
    )
    if [ "$1" = --reviewed-version ]; then
      printf '%s\n' "$reviewed_version"
      exit 0
    fi
    printf 'Installing Codex %s to match the reviewed patch manifest\n' "$reviewed_version"
    sudo npm install -g "@openai/codex@$reviewed_version"
    shift
    ;;
esac
exec python3 "$root/scripts/install-source-codex.py" "$@"
