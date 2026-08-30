#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)

fail() {
  printf 'package policy: %s\n' "$*" >&2
  exit 1
}

check_workflow="$repo_root/.github/workflows/check.yml"
smoke_workflow="$repo_root/.github/workflows/install-smoke.yml"
update_workflow="$repo_root/.github/workflows/update.yml"
readme="$repo_root/README.md"

test -f "$repo_root/package.nix"
test -f "$repo_root/patches/live-palette-refresh.patch"
grep -Fq 'rustPlatform.buildRustPackage' "$repo_root/package.nix"
grep -Fq '"codex-cli"' "$repo_root/package.nix"
grep -Fq '"codex-code-mode-host"' "$repo_root/package.nix"
grep -Fq 'requiredSystemFeatures = [ "codex-artifact-publisher" ];' "$repo_root/package.nix"
grep -Fq 'systems = [ "x86_64-linux" ];' "$repo_root/flake.nix"

for required_file in "$check_workflow" "$smoke_workflow" "$update_workflow" "$readme"; do
  [[ -f "$required_file" ]] || fail "required file is absent: ${required_file#"$repo_root/"}"
done

grep -Fq 'bash tests/package-policy.sh' "$check_workflow"
grep -Fq 'bash tests/update.sh' "$check_workflow"
grep -Fq 'bash tests/bundle.sh' "$check_workflow"
grep -Fq 'nix flake check --no-build' "$check_workflow"
grep -Fq 'nix --extra-system-features codex-artifact-publisher build -L .#codex' "$check_workflow"
grep -Fq './result/bin/codex --version' "$check_workflow"
grep -Fq './result/bin/codex-code-mode-host --help' "$check_workflow"

grep -Eq '^[[:space:]]+workflow_call:' "$smoke_workflow"
grep -Eq '^[[:space:]]+workflow_dispatch:' "$smoke_workflow"
grep -Fq 'CODEX_GITHUB_TOKEN: ${{ github.token }}' "$smoke_workflow"
grep -Fq 'nix eval --raw .#codex.outPath' "$smoke_workflow"
grep -Fq 'nix run .#install' "$smoke_workflow"
if grep -Fq 'codex-artifact-publisher' "$smoke_workflow" ||
  grep -Eq 'nix[[:space:]]+build[^#\n]*[.]#codex' "$smoke_workflow"; then
  fail 'fresh install smoke can realize Codex locally'
fi

grep -Fq 'uses: ./.github/workflows/install-smoke.yml' "$update_workflow"

grep -Fq 'nix run github:xunzhou/codex-nix -- --version' "$readme"
grep -Fq 'nix profile install github:xunzhou/codex-nix' "$readme"
grep -Fq 'nix run github:xunzhou/codex-nix#install' "$readme"
grep -Fq 'kill -USR1 "$(pgrep -n codex)"' "$readme"

supported_systems='["x86_64-linux"]'
for output in packages apps checks; do
  actual_systems=$(nix --extra-experimental-features 'nix-command flakes' \
    eval --json ".#$output" --apply builtins.attrNames) ||
    fail "could not evaluate $output system outputs"
  [[ "$actual_systems" == "$supported_systems" ]] ||
    fail "$output exposes unsupported systems: $actual_systems"
done

git -C "$repo_root" rev-parse --is-inside-work-tree >/dev/null

license_pattern='(^|/)LICENSE($|[.])'
if git -C "$repo_root" ls-files | grep -Eiq -- "$license_pattern"; then
  fail 'a license file is tracked'
fi

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
  '(^|[^[:alnum:]_-])(node|host|server)[_-]?[0-9]+([^[:alnum:]_-]|$)'
  '([Pp]rivate[[:space:]]+(release[[:space:]]+)?(closure|repository|repo|staging|visibility))'
)

git_grep_args=(-naE)
for pattern in "${privacy_patterns[@]}"; do
  git_grep_args+=(-e "$pattern")
done

scan_privacy_ref() {
  local ref="${1:-}"
  local status
  local -a ref_arg=()
  [[ -z "$ref" ]] || ref_arg=("$ref")

  if git -C "$repo_root" grep "${git_grep_args[@]}" "${ref_arg[@]}" -- >&2; then
    return 1
  else
    status=$?
    [[ $status -eq 1 ]] || return "$status"
  fi
}

check_action_refs() {
  local ref="${1:-}"
  local label="${ref:-tracked tree}"
  local matches status line action_ref
  local -a ref_arg=()
  [[ -z "$ref" ]] || ref_arg=("$ref")

  set +e
  matches=$(git -C "$repo_root" grep -a -nE \
    '^[[:space:]]*(-[[:space:]]+)?uses:[[:space:]]*[^[:space:]]+' \
    "${ref_arg[@]}" -- '.github/workflows/*.yml' '.github/workflows/*.yaml')
  status=$?
  set -e
  [[ $status -eq 0 || $status -eq 1 ]] || return "$status"
  [[ $status -eq 0 ]] || return 0

  while IFS= read -r line; do
    [[ "$line" =~ uses:[[:space:]]+([^[:space:]#]+) ]] ||
      fail "could not parse action reference in $label"
    action_ref="${BASH_REMATCH[1]}"
    case "$action_ref" in
      ./* | docker://*) continue ;;
    esac
    [[ "$action_ref" =~ ^[^@]+@[0-9a-f]{40}$ ]] ||
      fail "external action is not pinned to a full commit in $label: $action_ref"
    [[ "$line" =~ \#[[:space:]]+v[0-9]+([.][0-9]+)*([.-][[:alnum:].-]+)?[[:space:]]*$ ]] ||
      fail "pinned action lacks a version comment in $label: $action_ref"
  done <<<"$matches"
}

scan_privacy_ref
check_action_refs

while IFS= read -r commit; do
  scan_privacy_ref "$commit" || fail "privacy signature exists in reachable commit $commit"
  check_action_refs "$commit"
  if git -C "$repo_root" ls-tree -r --name-only "$commit" |
    grep -Eiq -- "$license_pattern"; then
    fail "a license file exists in reachable commit $commit"
  fi
done < <(git -C "$repo_root" rev-list --all)
