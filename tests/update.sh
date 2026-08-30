#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
updater="$repo_root/scripts/update.sh"

fail() {
  printf 'update tests: %s\n' "$*" >&2
  exit 1
}

[[ -f "$updater" ]] || fail 'updater is absent'

test_root=$(mktemp -d)
trap 'rm -rf "$test_root"' EXIT

fake_bin="$test_root/bin"
fixture_repo="$test_root/repo"
mkdir -p "$fake_bin" "$fixture_repo/scripts"
ln -s "$updater" "$fixture_repo/scripts/update.sh"

cat >"$fake_bin/curl" <<'FAKE'
#!/usr/bin/env bash
set -euo pipefail
printf 'curl' >>"$TEST_COMMAND_LOG"
printf ' <%s>' "$@" >>"$TEST_COMMAND_LOG"
printf '\n' >>"$TEST_COMMAND_LOG"
if [[ ${TEST_CURL_FAIL:-0} == 1 ]]; then
  exit 22
fi
printf '%s\n' "$TEST_RELEASE_JSON"
FAKE

cat >"$fake_bin/nix-update" <<'FAKE'
#!/usr/bin/env bash
set -euo pipefail
printf 'nix-update' >>"$TEST_COMMAND_LOG"
printf ' <%s>' "$@" >>"$TEST_COMMAND_LOG"
printf '\n' >>"$TEST_COMMAND_LOG"
if [[ ${TEST_NIX_UPDATE_FAIL:-0} == 1 ]]; then
  exit 23
fi
FAKE

cat >"$fake_bin/nix" <<'FAKE'
#!/usr/bin/env bash
set -euo pipefail
printf 'nix' >>"$TEST_COMMAND_LOG"
printf ' <%s>' "$@" >>"$TEST_COMMAND_LOG"
printf '\n' >>"$TEST_COMMAND_LOG"

case " $* " in
  *' eval --raw .#codex.version '*)
    printf '%s\n' "$TEST_CURRENT_VERSION"
    ;;
  *' flake check --no-build '*)
    if [[ ${TEST_FLAKE_CHECK_FAIL:-0} == 1 ]]; then
      exit 24
    fi
    ;;
  *' --extra-system-features codex-artifact-publisher build -L .#codex '*)
    if [[ -n ${TEST_BUILD_STATUS:-} ]]; then
      exit "$TEST_BUILD_STATUS"
    fi
    if [[ ${TEST_BUILD_FAIL:-0} == 1 ]]; then
      exit 25
    fi
    mkdir -p result/bin
    cat >result/bin/codex <<CODEX
#!/usr/bin/env bash
# terminal palette refresh did not return default colors
printf 'codex-cli %s\\n' '$TEST_BUILD_VERSION'
CODEX
    cat >result/bin/codex-code-mode-host <<'HOST'
#!/usr/bin/env bash
[[ ${1:-} == --help ]]
HOST
    chmod +x result/bin/codex result/bin/codex-code-mode-host
    ;;
  *)
    printf 'unexpected nix invocation: %s\n' "$*" >&2
    exit 26
    ;;
esac
FAKE

cat >"$fake_bin/git" <<'FAKE'
#!/usr/bin/env bash
set -euo pipefail
printf 'git' >>"$TEST_COMMAND_LOG"
printf ' <%s>' "$@" >>"$TEST_COMMAND_LOG"
printf '\n' >>"$TEST_COMMAND_LOG"
if [[ $1 == diff && ${2:-} == --check && ${TEST_DIFF_CHECK_FAIL:-0} == 1 ]]; then
  exit 27
fi
FAKE

chmod +x "$fake_bin/curl" "$fake_bin/nix-update" "$fake_bin/nix" "$fake_bin/git"

stable_release() {
  local version="$1"
  printf '{"tag_name":"rust-v%s","draft":false,"prerelease":false}\n' "$version"
}

run_update() {
  local current_version="$1"
  local build_version="$2"
  local release_json="$3"
  shift 3

  : >"$TEST_COMMAND_LOG"
  rm -rf "$fixture_repo/result"
  (
    cd "$fixture_repo"
    env \
      PATH="$fake_bin:/usr/bin:/bin" \
      TEST_COMMAND_LOG="$TEST_COMMAND_LOG" \
      TEST_CURRENT_VERSION="$current_version" \
      TEST_BUILD_VERSION="$build_version" \
      TEST_RELEASE_JSON="$release_json" \
      "$@" \
      bash scripts/update.sh "${TEST_VERSION_ARGUMENTS[@]}"
  )
}

assert_log_contains() {
  local literal="$1"
  grep -Fq -- "$literal" "$TEST_COMMAND_LOG" ||
    fail "command log did not contain: $literal"
}

assert_log_excludes() {
  local literal="$1"
  if grep -Fq -- "$literal" "$TEST_COMMAND_LOG"; then
    fail "command log unexpectedly contained: $literal"
  fi
}

TEST_COMMAND_LOG="$test_root/commands.log"

# Removing the stable-tag filter would select the alpha/RC entries instead.
latest_releases='[
  {"tag_name":"rust-v0.149.0","draft":false,"prerelease":false},
  {"tag_name":"rust-v0.153.0-alpha.1","draft":false,"prerelease":true},
  {"tag_name":"rust-v0.152.0-rc.1","draft":false,"prerelease":false},
  {"tag_name":"rust-v0.151.0","draft":false,"prerelease":false}
]'
TEST_VERSION_ARGUMENTS=()
run_update 0.150.1 0.151.0 "$latest_releases" env
assert_log_contains 'curl <-'
assert_log_contains '/repos/openai/codex/releases?per_page=100'
assert_log_contains 'nix-update <codex> <--flake> <--version=0.151.0> <--override-filename=package.nix>'

# Skipping exact release validation would allow a different or prerelease tag.
TEST_VERSION_ARGUMENTS=(0.151.0)
run_update 0.150.1 0.151.0 "$(stable_release 0.151.0)" env
assert_log_contains '/repos/openai/codex/releases/tags/rust-v0.151.0'
assert_log_contains 'nix-update <codex> <--flake> <--version=0.151.0> <--override-filename=package.nix>'

for invalid_version in 0.151.0-alpha.1 0.151.0-rc.1 rust-v0.151.0 01.151.0; do
  TEST_VERSION_ARGUMENTS=("$invalid_version")
  if run_update 0.150.1 0.151.0 "$(stable_release 0.151.0)" env; then
    fail "unstable or malformed version was accepted: $invalid_version"
  fi
  assert_log_excludes 'curl'
  assert_log_excludes 'nix-update'
done

TEST_VERSION_ARGUMENTS=(0.151.0)
if run_update 0.150.1 0.151.0 \
  '{"tag_name":"rust-v0.151.0","draft":false,"prerelease":true}' env; then
  fail 'prerelease metadata was accepted for an explicit version'
fi
assert_log_excludes 'nix-update'

# Changing the already-current branch to update or to skip validation breaks this case.
TEST_VERSION_ARGUMENTS=(0.150.1)
set +e
run_update 0.150.1 0.150.1 "$(stable_release 0.150.1)" env
already_current_status=$?
set -e
[[ $already_current_status -eq 10 ]] ||
  fail "already-current exit status was $already_current_status, expected 10"
assert_log_excludes 'nix-update'
assert_log_contains 'git <diff> <--check>'
assert_log_contains 'nix <flake> <check> <--no-build>'
assert_log_contains 'nix <--extra-system-features> <codex-artifact-publisher> <build> <-L> <.#codex>'
assert_log_excludes 'git <commit>'

# A failed updater must stop before validation or any commit boundary.
TEST_VERSION_ARGUMENTS=(0.151.0)
if run_update 0.150.1 0.151.0 "$(stable_release 0.151.0)" \
  env TEST_NIX_UPDATE_FAIL=1; then
  fail 'nix-update failure was ignored'
fi
assert_log_excludes 'git <diff> <--check>'
assert_log_excludes 'git <commit>'

# A failed build must remain uncommitted.
if run_update 0.150.1 0.151.0 "$(stable_release 0.151.0)" \
  env TEST_BUILD_FAIL=1; then
  fail 'build failure was ignored'
fi
assert_log_contains 'git <diff> <--check>'
assert_log_contains 'nix <--extra-system-features> <codex-artifact-publisher> <build> <-L> <.#codex>'
assert_log_excludes 'git <commit>'

# Dependency failures must never collide with the reserved already-current status.
set +e
run_update 0.150.1 0.151.0 "$(stable_release 0.151.0)" \
  env TEST_BUILD_STATUS=10
colliding_failure_status=$?
set -e
[[ $colliding_failure_status -ne 10 ]] ||
  fail 'build failure escaped as the already-current status'
assert_log_excludes 'git <commit>'

printf 'update tests: PASS\n'
