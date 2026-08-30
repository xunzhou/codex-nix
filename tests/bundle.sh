#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
library="$repo_root/scripts/lib/bundle.sh"
publisher="$repo_root/scripts/publish-bundle.sh"
installer="$repo_root/scripts/install-bundle.sh"
requested_suite="${1:-all}"
case "$requested_suite" in
  all | installer | publisher) ;;
  *)
    printf 'usage: %s [installer|publisher]\n' "$0" >&2
    exit 2
    ;;
esac
real_path="$PATH"
real_nix=$(command -v nix)
real_jq=$(command -v jq)
real_sha256sum=$(command -v sha256sum)
fake_bash=$(command -v bash)
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT

fail() {
  printf 'bundle test failed: %s\n' "$*" >&2
  exit 1
}

assert_equals() {
  local expected="$1" actual="$2" message="$3"
  [[ "$actual" == "$expected" ]] ||
    fail "$message (expected $expected, got $actual)"
}

assert_contains() {
  local expected="$1" actual="$2" message="$3"
  [[ "$actual" == *"$expected"* ]] ||
    fail "$message (expected output containing $expected, got: $actual)"
}

assert_fails() {
  local expected="$1"
  shift
  local output
  if output=$("$@" 2>&1); then
    fail "expected failure: $expected"
  fi
  [[ "$output" == *"$expected"* ]] ||
    fail "expected failure containing $expected, got: $output"
}

fake_bin="$scratch/bin"
mkdir -p "$fake_bin"

apply_shebang() {
  local file="$1"
  sed -i "1s|.*|#!$fake_bash|" "$file"
  chmod +x "$file"
}

cat >"$fake_bin/git" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
[[ "$1" == -C && "$2" == "$FAKE_REPO_ROOT" && "$3" == status &&
  "$4" == --porcelain && "$5" == --untracked-files=no ]] || exit 99
printf '%b' "${FAKE_GIT_STATUS:-}"
EOF

cat >"$fake_bin/nix" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
command_line="$*"
if [[ -n "${INSTALLER_LOG:-}" ]]; then
  printf 'nix %s\n' "$command_line" >>"$INSTALLER_LOG"
fi
if [[ "$1" != --extra-experimental-features || "$2" != 'nix-command flakes' ]]; then
  printf 'fake nix: missing flakes activation: %s\n' "$command_line" >&2
  exit 97
fi
shift 2
flake_ref="${FAKE_EXPECT_FLAKE_REF:-.}"
case "$*" in
  "eval --raw $flake_ref#codex.system") printf '%b' "${FAKE_SYSTEM:-x86_64-linux}" ;;
  "eval --raw $flake_ref#codex.version") printf '%b' "${FAKE_VERSION:-0.150.1}" ;;
  "eval --raw $flake_ref#codex.marker")
    printf '%b' "${FAKE_MARKER:-terminal palette refresh did not return default colors}"
    ;;
  "eval --raw $flake_ref#codex.patchFile")
    printf '%b' "${FAKE_PATCH_FILE:-/nix/store/0123456789abcdfghijklmnpqrsvwxyz-live-palette-refresh.patch}"
    ;;
  "path-info --derivation $flake_ref#codex")
    printf '%b' "${FAKE_DERIVATION_PATH:-/nix/store/0abcdfghijklmnpq0123456789abcdrv-codex.drv}"
    ;;
  'derivation show '*)
    if [[ -n "${FAKE_DERIVATION_SHOW+x}" ]]; then
      printf '%s' "$FAKE_DERIVATION_SHOW"
    else
      printf '{"%s":{"outputs":{"out":{"path":"%s"}}}}' \
        "${FAKE_DERIVATION_PATH:-/nix/store/0abcdfghijklmnpq0123456789abcdrv-codex.drv}" \
        "${FAKE_OUTPUT_PATH:-/nix/store/0123456789abcdfghijklmnpqrsvwxyz-v0.150.1-codex}"
    fi
    ;;
  'eval --impure --raw --expr builtins.currentSystem')
    printf '%b' "${FAKE_HOST_SYSTEM:-x86_64-linux}"
    ;;
  '--extra-system-features codex-artifact-publisher build --no-link --print-out-paths /nix/store/0abcdfghijklmnpq0123456789abcdrv-codex.drv^*')
    printf 'nix-build %s\n' "$command_line" >>"$PUBLISHER_LOG"
    printf '%s\n' "${FAKE_REALIZED_PATH:-/nix/store/0123456789abcdfghijklmnpqrsvwxyz-v0.150.1-codex}"
    ;;
  *)
    printf 'unexpected nix command: %s\n' "$command_line" >&2
    exit 98
    ;;
esac
EOF

cat >"$fake_bin/sha256sum" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${FAKE_USE_REAL_FILE_SHA:-no}" == yes ]]; then
  case "${1:-}" in
    *-manifest.json | *-SHA256SUMS) exec "$REAL_SHA256SUM" "$@" ;;
  esac
fi
if [[ "${1:-}" == --check ]]; then
  if [[ -n "${INSTALLER_LOG:-}" ]]; then
    [[ "${2:-}" == --strict && -n "${3:-}" ]] || exit 96
    printf 'checksums\n' >>"$INSTALLER_LOG"
  fi
  [[ "${FAKE_CHECKSUMS_VALID:-yes}" == yes ]]
  exit
fi
if [[ "${1:-}" == *.nar.zst ]]; then
  printf '%s  %s\n' "${FAKE_ARCHIVE_SHA256:-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef}" "$1"
  exit 0
fi
printf '%s  %s\n' \
  "${FAKE_PATCH_SHA256:-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef}" \
  "${1:-patch}"
EOF

cat >"$fake_bin/jq" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
exec "$REAL_JQ" "$@"
EOF

for command in git nix sha256sum jq; do
  apply_shebang "$fake_bin/$command"
done

export FAKE_REPO_ROOT="$repo_root" REAL_JQ="$real_jq" REAL_SHA256SUM="$real_sha256sum"

if [[ "$requested_suite" == installer && ! -f "$installer" ]]; then
  fail 'installer is absent'
fi

initialize() {
  PATH="$fake_bin:$real_path"
  export PATH
  # shellcheck source=../scripts/lib/bundle.sh
  source "$library"
  codex_bundle_initialize "${1:-.}"
}

run_identity_case() {
  (
    initialize path:fixture
    assert_equals 'xunzhou/codex-nix' "$CODEX_BUNDLE_REPO" 'repository identity'
    assert_equals 'x86_64-linux' "$CODEX_BUNDLE_SYSTEM" 'system identity'
    assert_equals 'codex-artifact-publisher' "$CODEX_BUNDLE_FEATURE" 'feature identity'
    assert_equals '0.150.1' "$CODEX_BUNDLE_VERSION" 'Codex version'
    assert_equals '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef' \
      "$CODEX_BUNDLE_PATCH_SHA256" 'patch SHA-256'
    assert_equals '/nix/store/0abcdfghijklmnpq0123456789abcdrv-codex.drv' \
      "$CODEX_BUNDLE_DERIVATION_PATH" 'derivation path'
    assert_equals '/nix/store/0123456789abcdfghijklmnpqrsvwxyz-v0.150.1-codex' \
      "$CODEX_BUNDLE_OUTPUT_PATH" 'output path'
    assert_equals 'codex-v0.150.1-0123456789abcdef-0abcdfghijklmnpq' \
      "$CODEX_BUNDLE_TAG" 'bundle tag'
    assert_equals 'codex-codex-v0.150.1-0123456789abcdef-0abcdfghijklmnpq-x86_64-linux.nar.zst' \
      "$CODEX_BUNDLE_ARCHIVE" 'bundle archive'
    assert_equals 'codex-codex-v0.150.1-0123456789abcdef-0abcdfghijklmnpq-manifest.json' \
      "$CODEX_BUNDLE_MANIFEST" 'bundle manifest'
    assert_equals 'codex-codex-v0.150.1-0123456789abcdef-0abcdfghijklmnpq-SHA256SUMS' \
      "$CODEX_BUNDLE_CHECKSUMS" 'bundle checksums'
  )
}

FAKE_EXPECT_FLAKE_REF=path:fixture run_identity_case

real_nix_derivation_show='{"derivations":{"0abcdfghijklmnpq0123456789abcdrv-codex.drv":{"outputs":{"out":{"path":"0123456789abcdfghijklmnpqrsvwxyz-v0.150.1-codex"}}}},"version":4}'
if ! identity_output=$(env PATH="$fake_bin:$real_path" FAKE_EXPECT_FLAKE_REF=. \
  FAKE_DERIVATION_SHOW="$real_nix_derivation_show" \
  bash -c 'source "$1"; codex_bundle_initialize .' bash "$library" 2>&1); then
  fail "Nix derivation schema did not initialize: $identity_output"
fi

assert_identity_rejected() {
  local expected="$1"
  shift
  assert_fails "$expected" env PATH="$fake_bin:$real_path" FAKE_EXPECT_FLAKE_REF=. "$@" \
    bash -c 'source "$1"; codex_bundle_initialize .' bash "$library"
}

assert_identity_rejected 'expected exactly one derivation' \
  FAKE_DERIVATION_SHOW='{"derivations":{},"version":4}'
assert_identity_rejected 'expected exactly one output path' \
  FAKE_DERIVATION_SHOW='{"derivations":{"only":{"outputs":{}}},"version":4}'
assert_identity_rejected 'tracked tree is dirty' FAKE_GIT_STATUS=' M package.nix'
assert_identity_rejected 'unsupported system' FAKE_SYSTEM=aarch64-linux
assert_identity_rejected 'must not contain newlines' FAKE_VERSION=$'0.150.1\nforged'
assert_identity_rejected 'must be an absolute /nix/store path' FAKE_PATCH_FILE=/tmp/patch
assert_identity_rejected 'must be an absolute /nix/store path' FAKE_DERIVATION_PATH=/tmp/codex.drv
assert_identity_rejected 'must be an absolute /nix/store path' FAKE_OUTPUT_PATH=/tmp/codex
assert_identity_rejected 'invalid Nix store hash' \
  FAKE_DERIVATION_PATH=/nix/store/eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee-codex.drv
assert_identity_rejected 'must be a direct /nix/store child' \
  FAKE_DERIVATION_PATH=/nix/store/0abcdfghijklmnpq0123456789abcdrv
assert_identity_rejected 'must be a direct /nix/store child' \
  FAKE_OUTPUT_PATH=/nix/store/0123456789abcdfghijklmnpqrsvwxyz-codex/subpath
assert_identity_rejected 'patch checksum must begin with a 64-character lowercase hexadecimal digest' \
  FAKE_PATCH_SHA256=invalid

non_git_source="$scratch/non-git-source"
mkdir -p "$non_git_source/scripts/lib"
cp "$library" "$non_git_source/scripts/lib/bundle.sh"
assert_fails 'source root must be a direct registered Nix store path' \
  env PATH="$fake_bin:$real_path" FAKE_EXPECT_FLAKE_REF=path:fixture \
  FAKE_REPO_ROOT="$repo_root" bash -c \
  'source "$1"; codex_bundle_initialize path:fixture' \
  bash "$non_git_source/scripts/lib/bundle.sh"

registered_store_source=$("$real_nix" --extra-experimental-features 'nix-command flakes' \
  flake archive --json "$repo_root" | "$real_jq" -er .path)
if ! registered_store_output=$(env PATH="$fake_bin:$real_path" \
  FAKE_EXPECT_FLAKE_REF="$registered_store_source" bash -c \
  'source "$1"; codex_bundle_initialize "$2"' \
  bash "$registered_store_source/scripts/lib/bundle.sh" "$registered_store_source" 2>&1); then
  fail "registered immutable store source did not initialize: $registered_store_output"
fi

store_source_link="$scratch/store-source-link"
ln -s "$registered_store_source" "$store_source_link"
assert_fails 'source root must be a direct registered Nix store path' \
  env PATH="$fake_bin:$real_path" FAKE_EXPECT_FLAKE_REF="$registered_store_source" \
  bash -c 'source "$1"; codex_bundle_initialize "$2"' \
  bash "$store_source_link/scripts/lib/bundle.sh" "$registered_store_source"

if ! env PATH="$fake_bin:$real_path" bash -c \
  'source "$1"; declare -F codex_bundle_source_is_immutable >/dev/null' \
  bash "$library"; then
  fail 'immutable source-root classifier is absent'
fi
if env PATH="$fake_bin:$real_path" bash -c \
  'source "$1"; codex_bundle_source_is_immutable "$2" "$2"' \
  bash "$library" /nix/store/eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee-invalid-source; then
  fail 'invalid store-shaped source was accepted as immutable'
fi
if env PATH="$fake_bin:$real_path" bash -c \
  'source "$1"; codex_bundle_source_is_immutable "$2" "$2"' \
  bash "$library" /nix/store/0123456789abcdfghijklmnpqrsvwxyz-unregistered-source; then
  fail 'unregistered store-shaped source was accepted as immutable'
fi

cat >"$fake_bin/nix-store" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
case "$1" in
  -qR)
    printf '%b' "${FAKE_CLOSURE:-/nix/store/zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz-dependency\n/nix/store/0123456789abcdfghijklmnpqrsvwxyz-v0.150.1-codex\n}"
    [[ "${FAKE_CLOSURE_FAIL:-no}" != yes ]] || exit 92
    ;;
  --export)
    printf 'export %s\n' "${*:2}" >>"$PUBLISHER_LOG"
    printf 'fake nar stream\n'
    ;;
  --import)
    cat >/dev/null
    printf 'import\n' >>"$INSTALLER_LOG"
    printf '%b' "${FAKE_IMPORTED_PATHS:-/nix/store/0123456789abcdfghijklmnpqrsvwxyz-v0.150.1-codex\n}"
    ;;
  --verify-path)
    printf 'verify %s\n' "${2:-}" >>"$INSTALLER_LOG"
    [[ "${FAKE_STORE_VERIFIED:-yes}" == yes ]]
    ;;
  *) exit 95 ;;
esac
EOF

cat >"$fake_bin/zstd" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'zstd %s\n' "$*" >>"$PUBLISHER_LOG"
if [[ "${1:-}" == -q && "${2:-}" == -dc && -n "${3:-}" ]]; then
  printf 'fake nar stream\n'
  exit 0
fi
while (($#)); do
  case "$1" in
    -o) cat >/dev/null; printf 'fake archive\n' >"$2"; exit 0 ;;
    *) shift ;;
  esac
done
exit 94
EOF

cat >"$fake_bin/sudo" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'sudo %s\n' "$*" >>"$INSTALLER_LOG"
exec "$@"
EOF

cat >"$fake_bin/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
method=GET
output=''
write_out=''
data=''
url=''
content_type_json=0
accept_json=0
accept_octet=0
authorization_headers=0
state_file="$PUBLISHER_LOG.state"
while (($#)); do
  case "$1" in
    -X|--request) method="$2"; shift 2 ;;
    -o|--output) output="$2"; shift 2 ;;
    -w|--write-out) write_out="$2"; shift 2 ;;
    -d|--data|--data-raw|--data-binary)
      data="$2"
      [[ "$1" == --data-binary ]] && method=POST
      shift 2
      ;;
    -H|--header)
      [[ "$2" != 'Content-Type: application/json' ]] || content_type_json=1
      [[ "$2" != 'Accept: application/vnd.github+json' ]] || accept_json=$((accept_json + 1))
      [[ "$2" != 'Accept: application/octet-stream' ]] || accept_octet=$((accept_octet + 1))
      [[ "$2" != Authorization:\ Bearer\ * ]] || authorization_headers=$((authorization_headers + 1))
      shift 2
      ;;
    --fail-with-body|--silent|--show-error|-L|--location) shift ;;
    http://*|https://*) url="$1"; shift ;;
    *) printf 'unexpected curl argument: %s\n' "$1" >&2; exit 96 ;;
  esac
done
printf 'curl %s %s\n' "$method" "$url" >>"$PUBLISHER_LOG"
if [[ -n "${INSTALLER_LOG:-}" ]]; then
  case "${FAKE_EXPECT_AUTH:-any}:$authorization_headers" in
    absent:0 | present:1 | any:*) ;;
    *) printf 'unexpected authorization header count\n' >&2; exit 87 ;;
  esac
  if ((authorization_headers)); then
    printf 'curl-auth present\n' >>"$INSTALLER_LOG"
  else
    printf 'curl-auth absent\n' >>"$INSTALLER_LOG"
  fi
fi

write_body() {
  if [[ -n "$output" ]]; then printf '%s' "$1" >"$output"; else printf '%s' "$1"; fi
}

case "$url" in
  */releases/tags/*)
    if [[ -n "${INSTALLER_LOG:-}" ]]; then
      write_body "${FAKE_INSTALLER_RELEASE_METADATA:?}"
      exit 0
    fi
    status=404
    body='{"message":"Not Found"}'
    rc=22
    if [[ -f "$state_file" && "${FAKE_RELEASE:-missing}" == missing ]]; then
      mapfile -t uploaded_assets <"$state_file"
      ((${#uploaded_assets[@]} == 3)) || exit 88
      IFS='|' read -r name0 digest0 <<<"${uploaded_assets[0]}"
      IFS='|' read -r name1 digest1 <<<"${uploaded_assets[1]}"
      IFS='|' read -r name2 digest2 <<<"${uploaded_assets[2]}"
      if [[ "${FAKE_FINAL_RELEASE_MODE:-valid}" == bad-digest ]]; then
        digest0=ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff
      fi
      body=$("$REAL_JQ" -n \
        --arg tag "${url##*/}" \
        --arg name0 "$name0" --arg digest0 "$digest0" \
        --arg name1 "$name1" --arg digest1 "$digest1" \
        --arg name2 "$name2" --arg digest2 "$digest2" \
        '{tag_name: $tag, assets: [
          {id: 20, name: $name0, state: "uploaded", digest: ("sha256:" + $digest0)},
          {id: 21, name: $name1, state: "uploaded", digest: ("sha256:" + $digest1)},
          {id: 22, name: $name2, state: "uploaded", digest: ("sha256:" + $digest2)}
        ]}')
      if [[ "${FAKE_FINAL_RELEASE_MODE:-valid}" == extra-asset ]]; then
        body=$(printf '%s' "$body" | "$REAL_JQ" \
          '.assets += [{id: 23, name: "unexpected", state: "uploaded", digest: "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"}]')
      fi
      status=200
      rc=0
    else
      case "${FAKE_RELEASE:-missing}" in
      existing|existing-command-error)
        status=200
        body="${FAKE_RELEASE_METADATA-}"
        [[ -n "$body" ]] || body='{}'
        rc=0
        ;;
      forbidden) status=403; body='{"message":"Forbidden"}'; rc=22 ;;
      server-error) status=503; body='{"message":"Unavailable"}'; rc=22 ;;
      missing-wrong-exit) status=404; body='{"message":"Not Found"}'; rc=7 ;;
      transport) printf 'network unreachable\n' >&2; exit 7 ;;
      malformed-status) status=wat; body='{}'; rc=0 ;;
      *) ;;
      esac
    fi
    write_body "$body"
    [[ -z "$write_out" ]] || printf '%s' "$status"
    [[ "${FAKE_RELEASE:-}" != existing-command-error ]] || exit 7
    exit "$rc"
    ;;
  */releases/assets/10)
    [[ "$accept_octet" == 1 && "$accept_json" == 0 ]] || exit 89
    write_body "${FAKE_ARCHIVE_CONTENT:-fake archive}"
    ;;
  */releases/assets/11)
    [[ "$accept_octet" == 1 && "$accept_json" == 0 ]] || exit 89
    write_body "${FAKE_MANIFEST_CONTENT:?}"
    ;;
  */releases/assets/12)
    [[ "$accept_octet" == 1 && "$accept_json" == 0 ]] || exit 89
    write_body "${FAKE_CHECKSUMS_CONTENT:?}"
    ;;
  */releases)
    [[ "$method" == POST && "$content_type_json" == 1 ]] || exit 93
    body="${FAKE_CREATE_METADATA-}"
    [[ -n "$body" ]] ||
      body='{"id":42,"tag_name":"codex-v0.150.1-0123456789abcdef-0abcdfghijklmnpq","assets":[]}'
    write_body "$body"
    [[ -z "$write_out" ]] || printf '%s' "${FAKE_CREATE_STATUS:-201}"
    : >"$state_file"
    ;;
  */releases/42/assets\?name=*)
    [[ "$method" == POST && "$data" == @* ]] || exit 92
    name="${url##*name=}"
    printf 'upload %s\n' "$name" >>"$PUBLISHER_LOG"
    source_path="${data#@}"
    case "$name" in
      *.nar.zst) digest="${FAKE_ARCHIVE_SHA256:-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef}" ;;
      *)
        digest=$("$REAL_SHA256SUM" "$source_path")
        digest="${digest%%[[:space:]]*}"
        ;;
    esac
    if [[ -n "${UPLOAD_CAPTURE_DIR:-}" ]]; then cp "${data#@}" "$UPLOAD_CAPTURE_DIR/$name"; fi
    if [[ "${FAKE_UPLOAD_FAIL_NAME:-}" == "$name" ]]; then
      printf 'upload failed\n' >&2
      exit 22
    fi
    response_name="$name"
    response_state=uploaded
    response_digest="$digest"
    case "${FAKE_UPLOAD_RESPONSE_MODE:-valid}" in
      wrong-name) response_name=unexpected ;;
      bad-state) response_state=new ;;
      bad-digest) response_digest=ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff ;;
    esac
    body=$("$REAL_JQ" -n \
      --arg name "$response_name" --arg state "$response_state" --arg digest "$response_digest" \
      '{name: $name, state: $state, digest: ("sha256:" + $digest)}')
    write_body "$body"
    [[ -z "$write_out" ]] || printf '%s' "${FAKE_UPLOAD_STATUS:-201}"
    printf '%s|%s\n' "$name" "$digest" >>"$state_file"
    ;;
  */releases/42)
    [[ "$method" == DELETE ]] || exit 91
    printf 'delete 42\n' >>"$PUBLISHER_LOG"
    ;;
  *) printf 'unexpected curl URL: %s\n' "$url" >&2; exit 90 ;;
esac
EOF

for command in nix-store zstd curl sudo; do
  apply_shebang "$fake_bin/$command"
done

run_publisher() {
  local log="$1"
  shift
  local -a env_args=()
  local -a script_args=()
  while (($#)) && [[ "$1" != -- ]]; do
    env_args+=("$1")
    shift
  done
  if (($#)); then
    shift
    script_args=("$@")
  fi
  : >"$log"
  rm -f "$log.state"
  env PATH="$fake_bin:$real_path" \
    FAKE_REPO_ROOT="$repo_root" \
    FAKE_EXPECT_FLAKE_REF=. \
    REAL_JQ="$real_jq" \
    REAL_SHA256SUM="$real_sha256sum" \
    PUBLISHER_LOG="$log" \
    "${env_args[@]}" bash "$publisher" "${script_args[@]}"
}

if [[ "$requested_suite" != installer ]]; then
dry_run_log="$scratch/dry-run.log"
dry_run_output=$(run_publisher "$dry_run_log" -- --dry-run)
assert_contains 'dry run' "$dry_run_output" 'dry-run reports its mode'
[[ ! -s "$dry_run_log" ]] || fail 'dry-run invoked a network or build operation'

flake_dry_run_log="$scratch/flake-dry-run.log"
flake_dry_run_output=$(FAKE_EXPECT_FLAKE_REF=path:fixture \
  run_publisher "$flake_dry_run_log" FAKE_EXPECT_FLAKE_REF=path:fixture \
  -- --dry-run --flake-ref path:fixture)
assert_contains 'codex-v0.150.1-0123456789abcdef-0abcdfghijklmnpq' "$flake_dry_run_output" \
  'flake-ref dry-run retains bundle identity'
[[ ! -s "$flake_dry_run_log" ]] || fail 'flake-ref dry-run invoked a network or build operation'

assert_fails 'GITHUB_TOKEN is required for publication' \
  run_publisher "$scratch/no-token.log"
assert_fails 'GITHUB_REPOSITORY must be xunzhou/codex-nix' \
  run_publisher "$scratch/wrong-repository.log" \
  GITHUB_TOKEN=test-token GITHUB_REPOSITORY=someone/else

cache_tag='codex-v0.150.1-0123456789abcdef-0abcdfghijklmnpq'
cache_archive="codex-${cache_tag}-x86_64-linux.nar.zst"
cache_manifest_name="codex-${cache_tag}-manifest.json"
cache_checksums_name="codex-${cache_tag}-SHA256SUMS"
cache_archive_digest='0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'
cache_manifest=$("$real_jq" -n \
  --arg tag "$cache_tag" --arg system x86_64-linux --arg version 0.150.1 \
  --arg patch 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef \
  --arg derivation /nix/store/0abcdfghijklmnpq0123456789abcdrv-codex.drv \
  --arg output /nix/store/0123456789abcdfghijklmnpqrsvwxyz-v0.150.1-codex \
  --arg archive "$cache_archive" --arg digest "$cache_archive_digest" \
  '{schema: 1, tag: $tag, system: $system, codex_version: $version,
    patch_sha256: $patch, derivation_path: $derivation, output_path: $output,
    closure_paths: 2, archive: $archive, archive_sha256: $digest}')

text_sha256() {
  local sum
  sum=$(printf '%s' "$1" | "$real_sha256sum")
  printf '%s\n' "${sum%%[[:space:]]*}"
}

cache_manifest_digest=$(text_sha256 "$cache_manifest")
cache_checksums=$(printf '%s  %s\n%s  %s' \
  "$cache_archive_digest" "$cache_archive" \
  "$cache_manifest_digest" "$cache_manifest_name")
cache_checksums_digest=$(text_sha256 "$cache_checksums")
cache_release_metadata=$("$real_jq" -n \
  --arg tag "$cache_tag" --arg archive "$cache_archive" \
  --arg manifest "$cache_manifest_name" --arg checksums "$cache_checksums_name" \
  --arg archive_digest "$cache_archive_digest" \
  --arg manifest_digest "$cache_manifest_digest" \
  --arg checksums_digest "$cache_checksums_digest" \
  '{tag_name: $tag, assets: [
    {id: 10, name: $archive, digest: ("sha256:" + $archive_digest), url: "https://api.github.com/repos/xunzhou/codex-nix/releases/assets/10"},
    {id: 11, name: $manifest, digest: ("sha256:" + $manifest_digest), url: "https://api.github.com/repos/xunzhou/codex-nix/releases/assets/11"},
    {id: 12, name: $checksums, digest: ("sha256:" + $checksums_digest), url: "https://api.github.com/repos/xunzhou/codex-nix/releases/assets/12"}
  ]}')

assert_indeterminate_lookup() {
  local fixture="$1" log="$2"
  assert_fails 'could not determine whether release exists' \
    run_publisher "$log" GITHUB_TOKEN=test-token FAKE_RELEASE="$fixture"
  if grep -Eq '^(nix-build|export|upload|delete) ' "$log"; then
    fail "publisher built or mutated after indeterminate $fixture lookup"
  fi
}

assert_indeterminate_lookup forbidden "$scratch/forbidden.log"
assert_indeterminate_lookup server-error "$scratch/server-error.log"
assert_indeterminate_lookup missing-wrong-exit "$scratch/missing-wrong-exit.log"
assert_indeterminate_lookup transport "$scratch/transport.log"
assert_indeterminate_lookup malformed-status "$scratch/malformed-status.log"
assert_indeterminate_lookup existing-command-error "$scratch/nonzero-200.log"

cache_log="$scratch/cache-hit.log"
cache_output=$(run_publisher "$cache_log" \
  GITHUB_TOKEN=test-token FAKE_RELEASE=existing \
  FAKE_RELEASE_METADATA="$cache_release_metadata" \
  FAKE_MANIFEST_CONTENT="$cache_manifest" FAKE_CHECKSUMS_CONTENT="$cache_checksums" \
  FAKE_USE_REAL_FILE_SHA=yes)
assert_contains 'cache hit' "$cache_output" 'matching release is a cache hit'
[[ "$(grep -c '/releases/tags/' "$cache_log")" == 1 ]] ||
  fail 'cache hit did not perform exactly one exact-tag REST lookup'
if grep -Eq '^(nix-build|export|upload|delete) ' "$cache_log"; then
  fail 'cache hit built or mutated a release'
fi

assert_cache_rejected() {
  local expected="$1" log="$2" manifest="$3" checksums="$4" metadata="$5"
  assert_fails "$expected" run_publisher "$log" \
    GITHUB_TOKEN=test-token FAKE_RELEASE=existing FAKE_RELEASE_METADATA="$metadata" \
    FAKE_MANIFEST_CONTENT="$manifest" FAKE_CHECKSUMS_CONTENT="$checksums" \
    FAKE_USE_REAL_FILE_SHA=yes
  if grep -Eq '^(nix-build|export|upload|delete) ' "$log"; then
    fail "publisher mutated after rejecting existing release: $expected"
  fi
}

missing_asset_metadata=$(printf '%s' "$cache_release_metadata" |
  "$real_jq" --arg name "$cache_checksums_name" '.assets |= map(select(.name != $name))')
assert_cache_rejected 'existing release assets do not match the exact bundle inventory' \
  "$scratch/missing-asset.log" "$cache_manifest" "$cache_checksums" "$missing_asset_metadata"

extra_asset_metadata=$(printf '%s' "$cache_release_metadata" | "$real_jq" \
  '.assets += [{id: 13, name: "unexpected", digest: "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"}]')
assert_cache_rejected 'existing release assets do not match the exact bundle inventory' \
  "$scratch/extra-asset.log" "$cache_manifest" "$cache_checksums" "$extra_asset_metadata"

duplicate_asset_metadata=$(printf '%s' "$cache_release_metadata" | "$real_jq" \
  '.assets += [.assets[0]]')
assert_cache_rejected 'existing release assets do not match the exact bundle inventory' \
  "$scratch/duplicate-asset.log" "$cache_manifest" "$cache_checksums" "$duplicate_asset_metadata"

bad_digest_metadata=$(printf '%s' "$cache_release_metadata" |
  "$real_jq" '.assets[0].digest = "sha256:invalid"')
assert_cache_rejected 'existing release asset digest is missing or malformed' \
  "$scratch/bad-digest.log" "$cache_manifest" "$cache_checksums" "$bad_digest_metadata"

tampered_checksums=$(printf '%s  %s\n%s  %s' \
  ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff "$cache_archive" \
  "$cache_manifest_digest" "$cache_manifest_name")
assert_cache_rejected 'existing release checksum asset digest mismatch' \
  "$scratch/tampered-checksums.log" "$cache_manifest" "$tampered_checksums" "$cache_release_metadata"

bad_manifest='{}'
bad_manifest_digest=$(text_sha256 "$bad_manifest")
bad_manifest_checksums=$(printf '%s  %s\n%s  %s' \
  "$cache_archive_digest" "$cache_archive" "$bad_manifest_digest" "$cache_manifest_name")
bad_manifest_metadata=$(printf '%s' "$cache_release_metadata" | "$real_jq" \
  --arg digest "$bad_manifest_digest" --arg checksums "$(text_sha256 "$bad_manifest_checksums")" \
  '(.assets[] | select(.id == 11).digest) = ("sha256:" + $digest)
   | (.assets[] | select(.id == 12).digest) = ("sha256:" + $checksums)')
assert_cache_rejected 'existing release manifest does not match the local bundle identity' \
  "$scratch/bad-manifest.log" "$bad_manifest" "$bad_manifest_checksums" "$bad_manifest_metadata"

assert_creation_metadata_rejected() {
  local label="$1" metadata="$2" log="$3"
  assert_fails 'created release metadata is invalid' run_publisher "$log" \
    GITHUB_TOKEN=test-token FAKE_CREATE_METADATA="$metadata" FAKE_USE_REAL_FILE_SHA=yes
  if grep -Eq '^curl DELETE ' "$log"; then
    fail "publisher armed cleanup from $label creation metadata"
  fi
}

assert_creation_metadata_rejected unrelated-tag \
  '{"id":99,"tag_name":"unrelated","assets":[]}' "$scratch/create-unrelated-tag.log"
assert_creation_metadata_rejected fractional-id \
  "$(printf '{\"id\":42.5,\"tag_name\":\"%s\",\"assets\":[]}' "$cache_tag")" \
  "$scratch/create-fractional-id.log"
assert_creation_metadata_rejected malformed-release \
  "$(printf '{\"id\":99,\"tag_name\":\"%s\",\"assets\":{}}' "$cache_tag")" \
  "$scratch/create-malformed-release.log"
assert_creation_metadata_rejected multiple-releases \
  "$(printf '{\"id\":42,\"tag_name\":\"%s\",\"assets\":[]}\n{\"id\":99,\"tag_name\":\"%s\",\"assets\":[]}' \
    "$cache_tag" "$cache_tag")" "$scratch/create-multiple-releases.log"

create_status_log="$scratch/create-status.log"
assert_fails 'release creation did not return HTTP 201' run_publisher "$create_status_log" \
  GITHUB_TOKEN=test-token FAKE_CREATE_STATUS=200 FAKE_USE_REAL_FILE_SHA=yes
if grep -Eq '^(upload|curl DELETE) ' "$create_status_log"; then
  fail 'publisher uploaded or claimed cleanup ownership after an HTTP 200 creation response'
fi

upload_status_log="$scratch/upload-status.log"
assert_fails 'upload did not return HTTP 201' run_publisher "$upload_status_log" \
  GITHUB_TOKEN=test-token FAKE_UPLOAD_STATUS=200 FAKE_USE_REAL_FILE_SHA=yes
grep -Fx 'delete 42' "$upload_status_log" >/dev/null ||
  fail 'publisher did not retain cleanup ownership after an invalid upload status'

assert_upload_metadata_rejected() {
  local mode="$1" log="$2"
  assert_fails 'uploaded asset metadata is invalid' run_publisher "$log" \
    GITHUB_TOKEN=test-token FAKE_UPLOAD_RESPONSE_MODE="$mode" FAKE_USE_REAL_FILE_SHA=yes
  grep -Fx 'delete 42' "$log" >/dev/null ||
    fail "publisher did not retain cleanup ownership after $mode upload metadata"
}

assert_upload_metadata_rejected wrong-name "$scratch/upload-wrong-name.log"
assert_upload_metadata_rejected bad-state "$scratch/upload-bad-state.log"
assert_upload_metadata_rejected bad-digest "$scratch/upload-bad-digest.log"

assert_final_release_rejected() {
  local mode="$1" log="$2"
  assert_fails 'published release assets do not match the exact bundle inventory and digests' \
    run_publisher "$log" GITHUB_TOKEN=test-token FAKE_FINAL_RELEASE_MODE="$mode" \
    FAKE_USE_REAL_FILE_SHA=yes
  grep -Fx 'delete 42' "$log" >/dev/null ||
    fail "publisher disarmed cleanup before rejecting $mode final release metadata"
}

assert_final_release_rejected bad-digest "$scratch/final-bad-digest.log"
assert_final_release_rejected extra-asset "$scratch/final-extra-asset.log"

capture_dir="$scratch/uploads"
mkdir -p "$capture_dir"
publish_log="$scratch/publish.log"
publish_output=$(run_publisher "$publish_log" \
  GITHUB_TOKEN=test-token UPLOAD_CAPTURE_DIR="$capture_dir" FAKE_USE_REAL_FILE_SHA=yes)
assert_contains 'published' "$publish_output" 'new release is published'
grep -Fx 'nix-build --extra-experimental-features nix-command flakes --extra-system-features codex-artifact-publisher build --no-link --print-out-paths /nix/store/0abcdfghijklmnpq0123456789abcdrv-codex.drv^*' \
  "$publish_log" >/dev/null || fail 'publisher did not use protected realization command'
grep -Fx 'export /nix/store/0123456789abcdfghijklmnpqrsvwxyz-v0.150.1-codex /nix/store/zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz-dependency' \
  "$publish_log" >/dev/null || fail 'publisher did not export sorted recursive closure'
[[ -s "$capture_dir/$cache_archive" ]] || fail 'archive asset was not created and uploaded'
[[ -s "$capture_dir/$cache_manifest_name" ]] || fail 'manifest asset was not uploaded'
[[ -s "$capture_dir/$cache_checksums_name" ]] || fail 'checksum asset was not uploaded'
[[ "$(grep -c '^upload ' "$publish_log")" == 3 ]] || fail 'publisher did not upload exactly three assets'
[[ "$(grep -c '/releases/tags/' "$publish_log")" == 2 ]] ||
  fail 'publisher did not re-fetch the exact tag after uploading all assets'
"$real_jq" -e --arg tag "$cache_tag" --arg output /nix/store/0123456789abcdfghijklmnpqrsvwxyz-v0.150.1-codex \
  '.schema == 1 and .tag == $tag and .output_path == $output and .closure_paths == 2' \
  "$capture_dir/$cache_manifest_name" >/dev/null || fail 'published manifest identity is invalid'

failed_upload_log="$scratch/failed-upload.log"
assert_fails 'upload failed' run_publisher "$failed_upload_log" \
  GITHUB_TOKEN=test-token FAKE_UPLOAD_FAIL_NAME="$cache_manifest_name" FAKE_USE_REAL_FILE_SHA=yes
grep -Fx 'delete 42' "$failed_upload_log" >/dev/null ||
  fail 'publisher did not delete the newly-created interrupted release'

partial_log="$scratch/partial.log"
assert_fails 'recursive closure query failed' run_publisher "$partial_log" \
  GITHUB_TOKEN=test-token FAKE_CLOSURE_FAIL=yes
if grep -Eq '^(export|upload|delete) ' "$partial_log"; then
  fail 'publisher mutated after a partial closure query failure'
fi
fi

if [[ "$requested_suite" != publisher ]]; then
  [[ -f "$installer" ]] || fail 'installer is absent'
  assert_fails '--flake-ref may only be provided once' \
    bash "$installer" --flake-ref . --flake-ref "$scratch/override"

  installer_manifest() {
    "$real_jq" -n \
      --argjson schema "${MANIFEST_SCHEMA:-1}" \
      --arg tag "${MANIFEST_TAG:-codex-v0.150.1-0123456789abcdef-0abcdfghijklmnpq}" \
      --arg system "${MANIFEST_SYSTEM:-x86_64-linux}" \
      --arg version "${MANIFEST_VERSION:-0.150.1}" \
      --arg patch "${MANIFEST_PATCH:-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef}" \
      --arg derivation "${MANIFEST_DERIVATION:-/nix/store/0abcdfghijklmnpq0123456789abcdrv-codex.drv}" \
      --arg output "${MANIFEST_OUTPUT:-/nix/store/0123456789abcdfghijklmnpqrsvwxyz-v0.150.1-codex}" \
      --argjson closure_paths "${MANIFEST_CLOSURE_PATHS:-2}" \
      --arg archive "${MANIFEST_ARCHIVE:-codex-codex-v0.150.1-0123456789abcdef-0abcdfghijklmnpq-x86_64-linux.nar.zst}" \
      --arg digest "${MANIFEST_ARCHIVE_SHA256:-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef}" \
      '{schema: $schema, tag: $tag, system: $system, codex_version: $version,
        patch_sha256: $patch, derivation_path: $derivation, output_path: $output,
        closure_paths: $closure_paths, archive: $archive, archive_sha256: $digest}'
  }

  installer_checksums() {
    local manifest="$1"
    local manifest_digest
    manifest_digest=$(printf '%s' "$manifest" | "$real_sha256sum")
    manifest_digest="${manifest_digest%%[[:space:]]*}"
    printf '%s  %s\n%s  %s\n' \
      0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef \
      codex-codex-v0.150.1-0123456789abcdef-0abcdfghijklmnpq-x86_64-linux.nar.zst \
      "$manifest_digest" \
      codex-codex-v0.150.1-0123456789abcdef-0abcdfghijklmnpq-manifest.json
  }

  installer_release_metadata() {
    local manifest="$1"
    local checksums="$2"
    local manifest_digest checksums_digest
    manifest_digest=$(printf '%s' "$manifest" | "$real_sha256sum")
    manifest_digest="${manifest_digest%%[[:space:]]*}"
    checksums_digest=$(printf '%s' "$checksums" | "$real_sha256sum")
    checksums_digest="${checksums_digest%%[[:space:]]*}"
    "$real_jq" -n \
      --arg archive codex-codex-v0.150.1-0123456789abcdef-0abcdfghijklmnpq-x86_64-linux.nar.zst \
      --arg manifest codex-codex-v0.150.1-0123456789abcdef-0abcdfghijklmnpq-manifest.json \
      --arg checksums codex-codex-v0.150.1-0123456789abcdef-0abcdfghijklmnpq-SHA256SUMS \
      --arg archive_digest 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef \
      --arg manifest_digest "$manifest_digest" --arg checksums_digest "$checksums_digest" \
      '{tag_name: "codex-v0.150.1-0123456789abcdef-0abcdfghijklmnpq", assets: [
        {id: 10, name: $archive, state: "uploaded", digest: ("sha256:" + $archive_digest),
          url: "https://api.github.com/repos/example/codex-nix/releases/assets/10"},
        {id: 11, name: $manifest, state: "uploaded", digest: ("sha256:" + $manifest_digest),
          url: "https://api.github.com/repos/example/codex-nix/releases/assets/11"},
        {id: 12, name: $checksums, state: "uploaded", digest: ("sha256:" + $checksums_digest),
          url: "https://api.github.com/repos/example/codex-nix/releases/assets/12"}
      ]}'
  }

  run_installer() {
    local log="$1" manifest="$2" checksums="$3"
    shift 3
    local metadata
    metadata=$(installer_release_metadata "$manifest" "$checksums")
    : >"$log"
    env PATH="$fake_bin:$real_path" \
      FAKE_REPO_ROOT="$repo_root" FAKE_EXPECT_FLAKE_REF=. \
      REAL_JQ="$real_jq" REAL_SHA256SUM="$real_sha256sum" \
      PUBLISHER_LOG="$log" INSTALLER_LOG="$log" \
      FAKE_USE_REAL_FILE_SHA=yes \
      FAKE_ARCHIVE_SHA256=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef \
      FAKE_MANIFEST_CONTENT="$manifest" FAKE_CHECKSUMS_CONTENT="$checksums" \
      FAKE_INSTALLER_RELEASE_METADATA="$metadata" CODEX_GITHUB_TOKEN= \
      "$@" bash "$installer" --flake-ref .
  }

  assert_installer_rejected() {
    local expected="$1" log="$2" manifest="$3" checksums="$4"
    shift 4
    assert_fails "$expected" run_installer "$log" "$manifest" "$checksums" "$@"
    if grep -Eq '^(sudo|import)($| )' "$log"; then
      fail "installer imported after rejecting: $expected"
    fi
  }

  valid_installer_manifest=$(installer_manifest)
  valid_installer_checksums=$(installer_checksums "$valid_installer_manifest")

  anonymous_log="$scratch/installer-anonymous.log"
  assert_fails 'Codex executable is missing' run_installer "$anonymous_log" \
    "$valid_installer_manifest" "$valid_installer_checksums" FAKE_EXPECT_AUTH=absent
  [[ "$(grep -c '^curl-auth absent$' "$anonymous_log")" == 4 ]] ||
    fail 'anonymous installer did not omit authorization from all REST requests'

  authenticated_log="$scratch/installer-authenticated.log"
  assert_fails 'Codex executable is missing' run_installer "$authenticated_log" \
    "$valid_installer_manifest" "$valid_installer_checksums" \
    FAKE_EXPECT_AUTH=present CODEX_GITHUB_TOKEN=installer-secret
  [[ "$(grep -c '^curl-auth present$' "$authenticated_log")" == 4 ]] ||
    fail 'authenticated installer did not authorize all REST requests'
  if grep -Fq 'installer-secret' "$authenticated_log"; then
    fail 'installer printed CODEX_GITHUB_TOKEN'
  fi

  checksum_line=$(grep -n '^checksums$' "$anonymous_log" | cut -d: -f1)
  import_line=$(grep -n '^import$' "$anonymous_log" | cut -d: -f1)
  [[ "$checksum_line" -lt "$import_line" ]] ||
    fail 'installer imported before checksum validation'
  [[ "$(grep -c '^sudo ' "$anonymous_log")" == 1 ]] ||
    fail 'installer used sudo outside the one import operation'
  grep -Fx "sudo $fake_bin/nix-store --import" "$anonymous_log" >/dev/null ||
    fail 'installer did not narrowly sudo nix-store --import'
  if grep -Eq '(^| )(build|codex-artifact-publisher)($| )' "$anonymous_log"; then
    fail 'installer invoked a build or protected publisher feature'
  fi

  malformed_checksums="${valid_installer_checksums}bad-entry"
  assert_installer_rejected 'checksum file has invalid entries' \
    "$scratch/installer-malformed-checksum.log" "$valid_installer_manifest" "$malformed_checksums"
  assert_installer_rejected 'checksum verification failed' \
    "$scratch/installer-checksum-mismatch.log" "$valid_installer_manifest" \
    "$valid_installer_checksums" FAKE_CHECKSUMS_VALID=no

  assert_manifest_case() {
    local expected="$1" label="$2" manifest="$3"
    local checksums
    checksums=$(installer_checksums "$manifest")
    assert_installer_rejected "$expected" "$scratch/installer-$label.log" "$manifest" "$checksums"
  }

  assert_manifest_case 'manifest schema mismatch' schema "$(MANIFEST_SCHEMA=2 installer_manifest)"
  assert_manifest_case 'manifest tag mismatch' tag "$(MANIFEST_TAG=forged installer_manifest)"
  assert_manifest_case 'manifest system mismatch' system "$(MANIFEST_SYSTEM=aarch64-linux installer_manifest)"
  assert_manifest_case 'manifest Codex version mismatch' version "$(MANIFEST_VERSION=0.0.0 installer_manifest)"
  assert_manifest_case 'manifest patch digest mismatch' patch "$(MANIFEST_PATCH=ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff installer_manifest)"
  assert_manifest_case 'manifest derivation path mismatch' derivation "$(MANIFEST_DERIVATION=/nix/store/0abcdfghijklmnpq0123456789abcdrv-forged.drv installer_manifest)"
  assert_manifest_case 'manifest output path mismatch' output "$(MANIFEST_OUTPUT=/nix/store/0123456789abcdfghijklmnpqrsvwxyz-forged installer_manifest)"
  assert_manifest_case 'manifest closure path count is invalid' closure-paths "$(MANIFEST_CLOSURE_PATHS=0 installer_manifest)"
  assert_manifest_case 'manifest archive name mismatch' archive "$(MANIFEST_ARCHIVE=forged.nar.zst installer_manifest)"
  assert_manifest_case 'manifest archive digest mismatch' archive-digest "$(MANIFEST_ARCHIVE_SHA256=ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff installer_manifest)"
  assert_manifest_case 'manifest derivation path must be an absolute /nix/store path' invalid-derivation-store "$(MANIFEST_DERIVATION=/tmp/codex.drv installer_manifest)"
  assert_manifest_case 'manifest output path must be a direct /nix/store child' invalid-output-store "$(MANIFEST_OUTPUT=/nix/store/0123456789abcdfghijklmnpqrsvwxyz-codex/subpath installer_manifest)"

  valid_metadata=$(installer_release_metadata "$valid_installer_manifest" "$valid_installer_checksums")
  extra_metadata=$(printf '%s' "$valid_metadata" | "$real_jq" \
    '.assets += [{id: 13, name: "unexpected", state: "uploaded", digest: "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef", url: "https://api.github.com/repos/example/codex-nix/releases/assets/13"}]')
  assert_installer_rejected 'release assets do not match the exact bundle inventory' \
    "$scratch/installer-extra-asset.log" "$valid_installer_manifest" \
    "$valid_installer_checksums" FAKE_INSTALLER_RELEASE_METADATA="$extra_metadata"

  missing_output_log="$scratch/installer-missing-output.log"
  assert_fails 'expected output path was not imported' run_installer "$missing_output_log" \
    "$valid_installer_manifest" "$valid_installer_checksums" \
    FAKE_IMPORTED_PATHS=$'/nix/store/zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz-dependency\n'
  if grep -q '^verify ' "$missing_output_log"; then
    fail 'installer verified a path after the expected import output was absent'
  fi

  # shellcheck source=../scripts/install-bundle.sh
  source "$installer"
  smoke_root="$scratch/installed-output"
  mkdir -p "$smoke_root/bin"
  cat >"$smoke_root/bin/codex" <<'EOF'
#!/usr/bin/env bash
# terminal palette refresh did not return default colors
printf '%s\n' 'codex-cli 0.150.1'
EOF
  apply_shebang "$smoke_root/bin/codex"
  cat >"$smoke_root/bin/codex-code-mode-host" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
  apply_shebang "$smoke_root/bin/codex-code-mode-host"
  CODEX_BUNDLE_OUTPUT_PATH="$smoke_root"
  CODEX_BUNDLE_VERSION=0.150.1
  CODEX_BUNDLE_MARKER='terminal palette refresh did not return default colors'
  verify_installed_output

  sed -i 's/codex-cli 0.150.1/codex-cli 0.0.0/' "$smoke_root/bin/codex"
  assert_fails 'Codex executable version mismatch' verify_installed_output
  sed -i 's/codex-cli 0.0.0/codex-cli 0.150.1/' "$smoke_root/bin/codex"
  sed -i '/terminal palette refresh/d' "$smoke_root/bin/codex"
  assert_fails 'Codex palette marker is missing' verify_installed_output
  printf '%s\n' '# terminal palette refresh did not return default colors' >>"$smoke_root/bin/codex"
  chmod -x "$smoke_root/bin/codex-code-mode-host"
  assert_fails 'codex-code-mode-host executable is missing' verify_installed_output

  install_program=$("$real_nix" --extra-experimental-features 'nix-command flakes' \
    eval --raw "$repo_root#apps.x86_64-linux.install.program") ||
    fail 'flake install app is absent'
  [[ "$install_program" == /nix/store/*/bin/install-codex-bundle ]] ||
    fail "flake install app has an invalid program: $install_program"
fi

printf 'bundle tests: PASS\n'
