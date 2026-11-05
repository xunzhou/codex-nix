#!/usr/bin/env bash
# Give release/LTO test compilation headroom on ephemeral hosted runners.
set -euo pipefail
[[ ${GITHUB_ACTIONS:-} == true && ${RUNNER_ENVIRONMENT:-} == github-hosted ]] || {
  echo 'build swap: refusing to modify a non-hosted runner' >&2
  exit 1
}
: "${RUNNER_TEMP:?RUNNER_TEMP is required}"
swapfile="$RUNNER_TEMP/codex-build.swap"
if swapon --show=NAME --noheadings | grep -Fxq -- "$swapfile"; then
  free -h
  exit 0
fi
[[ ! -e "$swapfile" && ! -L "$swapfile" ]] || {
  echo 'build swap: refusing to replace an existing file' >&2
  exit 1
}
sudo fallocate -l 16G "$swapfile"
sudo chmod 600 "$swapfile"
sudo mkswap "$swapfile"
sudo swapon "$swapfile"
free -h
