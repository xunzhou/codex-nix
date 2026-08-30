#!/usr/bin/env bash
set -euo pipefail

fail() {
  printf 'runner disk cleanup: %s\n' "$*" >&2
  exit 1
}

usage() {
  printf 'usage: %s [--root TEST_ROOT]\n' "${0##*/}" >&2
  exit 2
}

cleanup_root=/
if (($#)); then
  [[ $# -eq 2 && $1 == --root ]] || usage
  [[ ${CODEX_RUNNER_DISK_TEST:-0} == 1 ]] ||
    fail '--root is reserved for the sandbox test suite'
  cleanup_root=$(realpath -e -- "$2") || fail 'test root does not exist'
  temporary_root=$(realpath -e -- "${TMPDIR:-/tmp}") ||
    fail 'temporary root does not exist'
  [[ $cleanup_root == "$temporary_root"/* ]] ||
    fail 'test root must be below the temporary directory'
else
  [[ ${GITHUB_ACTIONS:-} == true ]] ||
    fail 'refusing to clean outside GitHub Actions'
  [[ ${RUNNER_ENVIRONMENT:-} == github-hosted ]] ||
    fail 'refusing to clean a non-hosted runner'
  ((EUID == 0)) || fail 'cleanup must run as root'
fi

relative_targets=(
  usr/local/lib/android
  usr/share/dotnet
  opt/ghc
  usr/local/.ghcup
  usr/local/share/boost
  opt/hostedtoolcache
)

targets=()
for relative_path in "${relative_targets[@]}"; do
  target="${cleanup_root%/}/$relative_path"
  [[ -e $target || -L $target ]] || continue
  [[ ! -L $target ]] || fail "refusing symlink target: $target"
  [[ -d $target ]] || fail "target is not a directory: $target"
  resolved_target=$(realpath -e -- "$target") ||
    fail "could not resolve target: $target"
  [[ $resolved_target == "$target" ]] ||
    fail "target escapes its exact path: $target"
  targets+=("$target")
done

printf 'Disk space before cleanup:\n'
df -h -- "$cleanup_root"

for target in "${targets[@]}"; do
  printf 'Removing unused runner directory: %s\n' "$target"
  rm -rf --one-file-system -- "$target"
  [[ ! -e $target && ! -L $target ]] || fail "could not remove target: $target"
done

printf 'Disk space after cleanup:\n'
df -h -- "$cleanup_root"
