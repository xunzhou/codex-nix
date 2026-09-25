#!/usr/bin/env bash
set -euo pipefail
root="$(mktemp -d)"
trap 'rm -rf -- "$root"' EXIT
installer="${INSTALLER_UNDER_TEST:-$(cd -- "$(dirname -- "$0")/.." && pwd)/scripts/install-local-codex.sh}"
export CODEX_BUILD_MANIFEST="$root/build.json"
printf '%s\n' '{"schema_version":1,"default_version":"0.155.1","releases":{"0.155.1":{"patches":["example.patch"]}}}' >"$CODEX_BUILD_MANIFEST"
[[ "$("$installer" --reviewed-version)" == 0.155.1 ]]
mkdir "$root/bin"
cat >"$root/bin/sudo" <<'SH'
#!/bin/sh
printf '%s\n' "$*" >"$CALL_LOG"
exit 43
SH
chmod +x "$root/bin/sudo"
for option in --restore-reviewed --install-reviewed; do
  status=0
  PATH="$root/bin:$PATH" CALL_LOG="$root/calls" "$installer" "$option" >"$root/output" 2>&1 || status=$?
  [[ "$status" == 43 ]]
  grep -Fxq 'npm install -g @openai/codex@0.155.1' "$root/calls"
done
printf '%s\n' '{"schema_version":1,"default_version":"0.156.1","releases":{}}' >"$CODEX_BUILD_MANIFEST"
if "$installer" --reviewed-version >"$root/output" 2>&1; then
  printf 'FAIL: unsupported manifest accepted\n' >&2
  exit 1
fi
printf 'PASS: reviewed version selection, restore pin, and invalid manifest rejection\n'
