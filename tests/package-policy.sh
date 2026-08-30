#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)

test -f "$repo_root/package.nix"
test -f "$repo_root/patches/live-palette-refresh.patch"
grep -Fq 'rustPlatform.buildRustPackage' "$repo_root/package.nix"
grep -Fq '"codex-cli"' "$repo_root/package.nix"
grep -Fq '"codex-code-mode-host"' "$repo_root/package.nix"
grep -Fq 'requiredSystemFeatures = [ "codex-artifact-publisher" ];' "$repo_root/package.nix"
grep -Fq 'systems = [ "x86_64-linux" ];' "$repo_root/flake.nix"

git -C "$repo_root" rev-parse --is-inside-work-tree >/dev/null

# Split literal signatures so this tracked policy file can scan itself.
privacy_patterns=(
  '/ho''me/[[:alnum:]_.-]+(/|[^[:alnum:]_.-]|$)'
  '/Us''ers/[[:alnum:]_.-]+(/|[^[:alnum:]_.-]|$)'
  '/ro''ot(/|[^[:alnum:]_.-]|$)'
  '[A-Za-z]:\\Us''ers\\[[:alnum:]_.-]+(\\|[^[:alnum:]_.-]|$)'
  '-----BEGIN [A-Z0-9 ]*PRIVATE'' KEY-----'
  '(ghp|github_pat|glpat|xox[baprs])_[[:alnum:]_-]{16,}'
  'AKIA[0-9A-Z]{16}'
  "(password|passwd|secret|api[_-]?(key|token)|access[_-]?token|auth[_-]?token)[[:space:]]*[:=][[:space:]]*[\"']?[^\"'[:space:]]+"
  '(git\+)?ss''h://'
  'fi''le://'
  '[[:alnum:]_.-]+''@[[:alnum:]_.-]+:'
  'https?://[^/@[:space:]]+:[^/@[:space:]]+@'
  'https?://(localhost|127\.[0-9]{1,3}(\.[0-9]{1,3}){2}|\[::1\]|10\.[0-9.]+|192\.168\.[0-9.]+|172\.(1[6-9]|2[0-9]|3[01])\.[0-9.]+)([:/]|$)'
  'https?://[^/[:space:]]+\.(internal|local)([:/]|$)'
)

git_grep_args=(-naE)
for pattern in "${privacy_patterns[@]}"; do
  git_grep_args+=(-e "$pattern")
done

if git -C "$repo_root" grep "${git_grep_args[@]}" -- >&2; then
  exit 1
else
  status=$?
  if [[ $status -ne 1 ]]; then
    exit "$status"
  fi
fi
