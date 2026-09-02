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
cleanup_script="$repo_root/scripts/reclaim-runner-disk.sh"

test -f "$repo_root/package.nix"
test -f "$repo_root/patches/live-palette-refresh.patch"
grep -Fq 'rustPlatform.buildRustPackage' "$repo_root/package.nix"
grep -Fq '"codex-cli"' "$repo_root/package.nix"
grep -Fq '"codex-code-mode-host"' "$repo_root/package.nix"
grep -Fq 'requiredSystemFeatures = [ "codex-artifact-publisher" ];' "$repo_root/package.nix"
grep -Fq 'systems = [ "x86_64-linux" ];' "$repo_root/flake.nix"

for required_file in \
  "$check_workflow" \
  "$smoke_workflow" \
  "$update_workflow" \
  "$readme" \
  "$cleanup_script"; do
  [[ -f "$required_file" ]] || fail "required file is absent: ${required_file#"$repo_root/"}"
done

cleanup_test_directory=$(mktemp -d)
trap 'rm -rf "$cleanup_test_directory"' EXIT
cleanup_root="$cleanup_test_directory/runner"
cleanup_targets=(
  usr/local/lib/android
  usr/share/dotnet
  opt/ghc
  usr/local/.ghcup
  usr/local/share/boost
  opt/hostedtoolcache
)
mkdir -p "$cleanup_root/keep"
touch "$cleanup_root/keep/sentinel"
for relative_path in "${cleanup_targets[@]}"; do
  mkdir -p "$cleanup_root/$relative_path"
  touch "$cleanup_root/$relative_path/unused-sdk"
done

cleanup_output=$(CODEX_RUNNER_DISK_TEST=1 \
  bash "$cleanup_script" --root "$cleanup_root") ||
  fail 'runner disk cleanup failed against a sandbox root'
grep -Fq 'Disk space before cleanup:' <<<"$cleanup_output"
grep -Fq 'Disk space after cleanup:' <<<"$cleanup_output"
for relative_path in "${cleanup_targets[@]}"; do
  [[ ! -e "$cleanup_root/$relative_path" ]] ||
    fail "runner disk cleanup retained $relative_path"
done
[[ -f "$cleanup_root/keep/sentinel" ]] ||
  fail 'runner disk cleanup removed an unlisted path'

unsafe_root="$cleanup_test_directory/unsafe-runner"
outside_root="$cleanup_test_directory/outside"
mkdir -p "$unsafe_root/usr/local/lib" "$outside_root"
touch "$outside_root/sentinel"
ln -s "$outside_root" "$unsafe_root/usr/local/lib/android"
if CODEX_RUNNER_DISK_TEST=1 \
  bash "$cleanup_script" --root "$unsafe_root" >/dev/null 2>&1; then
  fail 'runner disk cleanup followed a symlink target'
fi
[[ -f "$outside_root/sentinel" ]] ||
  fail 'runner disk cleanup deleted through a symlink'

# Exercise the shipped patch against the declared source. Tests can override the
# source path to reproduce compatibility against a candidate upstream release.
patch_source=${CODEX_PATCH_SOURCE:-}
if [[ -z "$patch_source" ]]; then
  patch_source=$(nix --extra-experimental-features 'nix-command flakes' \
    build --no-link --print-out-paths "$repo_root#codex.src") ||
    fail 'could not realize the declared Codex source'
fi
[[ -d "$patch_source" ]] || fail "Codex patch source is not a directory: $patch_source"

patch_test_root="$cleanup_test_directory/patch"
cp -R --no-preserve=mode "$patch_source" "$patch_test_root"
if ! patch --batch --forward -d "$patch_test_root" -p1 \
  <"$repo_root/patches/live-palette-refresh.patch" >/dev/null; then
  fail 'live palette patch does not apply to the Codex source'
fi

patched_palette="$patch_test_root/codex-rs/tui/src/terminal_palette.rs"
patched_events="$patch_test_root/codex-rs/tui/src/tui/event_stream.rs"
grep -Fq 'static TEST_DEFAULT_COLORS:' "$patched_palette" ||
  fail 'live palette patch discarded the upstream test palette override'
grep -Fq 'fn with_test_default_colors<T>(' "$patched_palette" ||
  fail 'live palette patch discarded the upstream scoped test helper'
grep -Fq 'fn refreshed_default_colors(' "$patched_palette" ||
  fail 'live palette patch lacks refresh fallback semantics'
grep -Fq 'fn poll_palette_refresh_signal(' "$patched_events" ||
  fail 'live palette patch lacks the SIGUSR1 event path'

grep -Fq 'bash tests/package-policy.sh' "$check_workflow"
grep -Fq 'bash tests/update.sh' "$check_workflow"
grep -Fq 'bash tests/bundle.sh' "$check_workflow"
grep -Fq 'bash scripts/reclaim-runner-disk.sh' "$check_workflow"
grep -Fq 'bash scripts/reclaim-runner-disk.sh' "$update_workflow"
# Treat only a direct `with.fetch-depth` child of every pinned checkout step as valid.
# Identical text nested in a `run: |` block has deeper indentation and must not count.
if ! awk '
  function leading_spaces(line) {
    match(line, /[^ ]/)
    return RSTART ? RSTART - 1 : length(line)
  }

  function finish_checkout() {
    if (in_checkout && !has_full_depth) {
      invalid_checkout = 1
    }
    in_checkout = 0
    in_with = 0
    has_full_depth = 0
  }

  {
    indent = leading_spaces($0)
    text = substr($0, indent + 1)

    if (in_checkout && text !~ /^($|#)/ && indent < checkout_indent) {
      finish_checkout()
    }

    if (text ~ /^uses:[[:space:]]+actions\/checkout@[0-9a-f]{40}([[:space:]]+#.*)?$/) {
      finish_checkout()
      saw_checkout = 1
      in_checkout = 1
      checkout_indent = indent
      next
    }

    if (!in_checkout || text ~ /^($|#)/) {
      next
    }

    if (indent == checkout_indent && text ~ /^with:[[:space:]]*$/) {
      in_with = 1
      with_indent = indent
      next
    }

    if (indent == checkout_indent) {
      in_with = 0
      next
    }

    if (in_with && indent == with_indent + 2 &&
        text ~ /^fetch-depth:[[:space:]]+0([[:space:]]|#|$)/) {
      has_full_depth = 1
    }
  }

  END {
    finish_checkout()
    exit !(saw_checkout && !invalid_checkout)
  }
' "$check_workflow"; then
  fail 'pinned checkout step does not fetch complete history'
fi
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
grep -Eq '^[[:space:]]+schedule:[[:space:]]*$' "$update_workflow" ||
  fail 'Codex update workflow is not scheduled'
grep -Fq "cron: '17 8 * * *'" "$update_workflow" ||
  fail 'Codex update workflow does not run daily'
grep -Fq "run-name: Update Codex (\${{ inputs.request_id || 'scheduled' }})" "$update_workflow" ||
  fail 'scheduled Codex updates lack a correlation label'

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

codex_derivation=$(nix --extra-experimental-features 'nix-command flakes' \
  derivation show .#codex) || fail 'could not inspect the Codex derivation'
codex_pre_build=$(jq -r \
  '.derivations | to_entries[0].value.env.preBuild // ""' \
  <<<"$codex_derivation")
effective_build_cores=$(
  export NIX_BUILD_CORES=8
  eval "$codex_pre_build"
  printf '%s' "$NIX_BUILD_CORES"
)
[[ "$effective_build_cores" == 2 ]] ||
  fail "Codex build permits $effective_build_cores concurrent Cargo jobs"

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
  '(gh[a-z]|github_pat|glpat|xox[baprs])_[[:alnum:]_-]{16,}'
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
