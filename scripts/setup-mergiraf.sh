#!/usr/bin/env bash
# Pinned static binary for GitHub's Linux x86_64 prepare runner.
set -euo pipefail
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
curl -fL --retry 3 -o "$scratch/mergiraf.tar.gz" \
  https://codeberg.org/mergiraf/mergiraf/releases/download/v0.20.0/mergiraf_x86_64-unknown-linux-musl.tar.gz
printf '%s  %s\n' fc77469be936b02ab9021b984a3aebc9250a21ae3d264f2ef2198eb8b31b2a7b \
  "$scratch/mergiraf.tar.gz" | sha256sum -c -
mkdir -p "$RUNNER_TEMP/mergiraf"
tar -xzf "$scratch/mergiraf.tar.gz" -C "$RUNNER_TEMP/mergiraf" mergiraf
test "$("$RUNNER_TEMP/mergiraf/mergiraf" --version)" = 'mergiraf 0.20.0'
printf 'CODEX_MERGIRAF=%s\n' "$RUNNER_TEMP/mergiraf/mergiraf" >> "$GITHUB_ENV"
