#!/usr/bin/env bash
set -euo pipefail
umask 077

fail() {
  printf 'codex bundle installer: %s\n' "$*" >&2
  exit 1
}

require_command() {
  local name="$1"
  local path

  path=$(command -v "$name") || fail "missing dependency: $name"
  [[ "$path" == /* ]] || fail "dependency is not an absolute path: $name"
  printf '%s\n' "$path"
}

require_store_path() {
  local name="$1"
  local path="$2"

  codex_bundle_store_hash "$name" "$path" >/dev/null || exit 1
}

validate_checksum_file() {
  local checksums_path="$1"
  local archive_name="$2"
  local manifest_name="$3"
  local line filename
  local archive_entries=0
  local manifest_entries=0

  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[0-9a-f]{64}\ \ [^[:space:]/]+$ ]] || return 1
    filename="${line:66}"
    case "$filename" in
      "$archive_name") archive_entries=$((archive_entries + 1)) ;;
      "$manifest_name") manifest_entries=$((manifest_entries + 1)) ;;
      *) return 1 ;;
    esac
  done <"$checksums_path"

  ((archive_entries == 1 && manifest_entries == 1))
}

github_api() {
  local accept="$1"
  shift
  local -a headers=(
    -H "Accept: $accept"
    -H 'X-GitHub-Api-Version: 2022-11-28'
  )

  if [[ -n "${CODEX_GITHUB_TOKEN:-}" ]]; then
    headers+=( -H "Authorization: Bearer $CODEX_GITHUB_TOKEN" )
  fi
  "$curl_bin" --fail-with-body --silent --show-error --location \
    "${headers[@]}" "$@"
}

file_sha256() {
  local path="$1"
  local checksum digest

  checksum=$("$sha256sum_bin" "$path") || return 1
  codex_bundle_require_single_line 'file checksum' "$checksum" || return 1
  digest="${checksum%%[[:space:]]*}"
  [[ "$digest" =~ ^[0-9a-f]{64}$ ]] || return 1
  printf '%s\n' "$digest"
}

verify_installed_output() {
  local actual_version

  [[ -x "$CODEX_BUNDLE_OUTPUT_PATH/bin/codex" ]] || fail 'Codex executable is missing'
  actual_version=$("$CODEX_BUNDLE_OUTPUT_PATH/bin/codex" --version) ||
    fail 'Codex executable version command failed'
  [[ "$actual_version" == "codex-cli $CODEX_BUNDLE_VERSION" ]] ||
    fail 'Codex executable version mismatch'
  grep -aFqm1 -- "$CODEX_BUNDLE_MARKER" "$CODEX_BUNDLE_OUTPUT_PATH/bin/codex" ||
    fail 'Codex palette marker is missing'
  [[ -x "$CODEX_BUNDLE_OUTPUT_PATH/bin/codex-code-mode-host" ]] ||
    fail 'codex-code-mode-host executable is missing'
}

main() {
  local flake_ref=''
  local flake_ref_seen=0
  while (($#)); do
    case "$1" in
      --flake-ref)
        (($# >= 2)) || fail '--flake-ref requires a value'
        ((flake_ref_seen == 0)) || fail '--flake-ref may only be provided once'
        flake_ref="$2"
        flake_ref_seen=1
        shift 2
        ;;
      *) fail "unknown argument: $1" ;;
    esac
  done
  [[ -n "$flake_ref" && "$flake_ref" != *$'\n'* && "$flake_ref" != *$'\r'* ]] ||
    fail '--flake-ref must be a nonempty single line'

  local identity_library="$flake_ref/scripts/lib/bundle.sh"
  [[ -f "$identity_library" ]] || fail 'flake reference does not contain the bundle identity library'
  # The library path is supplied by the pinned flake source.
  # shellcheck disable=SC1090,SC1091
  source "$identity_library"

  local curl_bin git_bin jq_bin nix_bin nix_store_bin sha256sum_bin sudo_bin zstd_bin
  curl_bin=$(require_command curl)
  git_bin=$(require_command git)
  jq_bin=$(require_command jq)
  nix_bin=$(require_command nix)
  nix_store_bin=$(require_command nix-store)
  sha256sum_bin=$(require_command sha256sum)
  sudo_bin=$(require_command sudo)
  zstd_bin=$(require_command zstd)
  [[ "$nix_store_bin" == /*/bin/nix-store ]] ||
    fail 'nix-store must resolve to an absolute path ending in /bin/nix-store'
  : "$git_bin"

  codex_bundle_initialize "$flake_ref"

  local host_system marker
  # Defined by the sourced bundle identity library.
  # shellcheck disable=SC2154
  host_system=$("$nix_bin" "${codex_bundle_nix_global_args[@]}" \
    eval --impure --raw --expr builtins.currentSystem) ||
    fail 'could not determine installer host system'
  codex_bundle_require_single_line 'installer host system' "$host_system" ||
    fail 'installer host system is invalid'
  [[ "$host_system" == "$CODEX_BUNDLE_SYSTEM" ]] ||
    fail "installer host system must be $CODEX_BUNDLE_SYSTEM"
  marker=$("$nix_bin" "${codex_bundle_nix_global_args[@]}" \
    eval --raw "$flake_ref#codex.marker") || fail 'could not evaluate Codex palette marker'
  codex_bundle_require_single_line 'Codex palette marker' "$marker" ||
    fail 'Codex palette marker is invalid'
  CODEX_BUNDLE_MARKER="$marker"
  export CODEX_BUNDLE_MARKER

  if [[ -n "${CODEX_GITHUB_TOKEN:-}" &&
    ( "$CODEX_GITHUB_TOKEN" == *$'\n'* || "$CODEX_GITHUB_TOKEN" == *$'\r'* ) ]]; then
    fail 'CODEX_GITHUB_TOKEN must be a single line'
  fi

  local temporary_directory
  temporary_directory=$(mktemp -d)
  cleanup() {
    rm -rf "$temporary_directory"
  }
  trap cleanup EXIT

  local release_path="$temporary_directory/release.json"
  local release_url="https://api.github.com/repos/$CODEX_BUNDLE_REPO/releases/tags/$CODEX_BUNDLE_TAG"
  github_api 'application/vnd.github+json' -o "$release_path" "$release_url" ||
    fail 'could not resolve the exact bundle release'

  local inventory
  # Shell expansion is intentionally disabled inside the jq program.
  # shellcheck disable=SC2016
  inventory=$("$jq_bin" -er \
    --arg tag "$CODEX_BUNDLE_TAG" \
    --arg archive "$CODEX_BUNDLE_ARCHIVE" \
    --arg manifest "$CODEX_BUNDLE_MANIFEST" \
    --arg checksums "$CODEX_BUNDLE_CHECKSUMS" '
      . as $release
      | [$archive, $manifest, $checksums] as $expected
      | if (($release | type) == "object"
          and .tag_name == $tag
          and ((.assets | type) == "array")
          and (.assets | length) == 3
          and ([.assets[].name] | sort) == ($expected | sort)
          and all(.assets[];
            ((.id | type) == "number") and (.id > 0) and ((.id | floor) == .id)
            and (.state == "uploaded")
            and ((.digest | type) == "string")
            and (.digest | test("^sha256:[0-9a-f]{64}$"))
            and ((.url | type) == "string")
            and (.url | test("^https://api[.]github[.]com/.+/releases/assets/[1-9][0-9]*$")))
          )
        then
          .assets[] | [.name, .url, (.digest | sub("^sha256:"; ""))] | @tsv
        else
          error("release assets do not match the exact bundle inventory")
        end
    ' "$release_path") || fail 'release assets do not match the exact bundle inventory'

  local archive_path="$temporary_directory/$CODEX_BUNDLE_ARCHIVE"
  local manifest_path="$temporary_directory/$CODEX_BUNDLE_MANIFEST"
  local checksums_path="$temporary_directory/$CODEX_BUNDLE_CHECKSUMS"
  local asset_name asset_url expected_digest asset_path actual_digest
  while IFS=$'\t' read -r asset_name asset_url expected_digest; do
    case "$asset_name" in
      "$CODEX_BUNDLE_ARCHIVE") asset_path="$archive_path" ;;
      "$CODEX_BUNDLE_MANIFEST") asset_path="$manifest_path" ;;
      "$CODEX_BUNDLE_CHECKSUMS") asset_path="$checksums_path" ;;
      *) fail 'release assets do not match the exact bundle inventory' ;;
    esac
    github_api 'application/octet-stream' -o "$asset_path" "$asset_url" ||
      fail "could not download release asset: $asset_name"
    actual_digest=$(file_sha256 "$asset_path") || fail "could not digest release asset: $asset_name"
    [[ "$actual_digest" == "$expected_digest" ]] || fail 'downloaded release asset digest mismatch'
  done <<<"$inventory"
  [[ -f "$archive_path" && -f "$manifest_path" && -f "$checksums_path" ]] ||
    fail 'release bundle assets are incomplete'

  validate_checksum_file "$checksums_path" "$CODEX_BUNDLE_ARCHIVE" "$CODEX_BUNDLE_MANIFEST" ||
    fail 'checksum file has invalid entries'
  if ! (
    cd "$temporary_directory"
    "$sha256sum_bin" --check --strict "$CODEX_BUNDLE_CHECKSUMS"
  ); then
    fail 'checksum verification failed'
  fi

  "$jq_bin" -e '
    (.schema == 1)
    and ((.tag | type) == "string")
    and ((.system | type) == "string")
    and ((.codex_version | type) == "string")
    and ((.patch_sha256 | type) == "string")
    and ((.derivation_path | type) == "string")
    and ((.output_path | type) == "string")
    and ((.closure_paths | type) == "number")
    and ((.archive | type) == "string")
    and ((.archive_sha256 | type) == "string")
    and (.archive_sha256 | test("^[0-9a-f]{64}$"))
  ' "$manifest_path" >/dev/null || fail 'manifest schema mismatch'

  local manifest_schema manifest_tag manifest_system manifest_version
  local manifest_patch manifest_derivation manifest_output manifest_closure_paths
  local manifest_archive manifest_archive_digest
  manifest_schema=$("$jq_bin" -er '.schema' "$manifest_path")
  manifest_tag=$("$jq_bin" -er '.tag' "$manifest_path")
  manifest_system=$("$jq_bin" -er '.system' "$manifest_path")
  manifest_version=$("$jq_bin" -er '.codex_version' "$manifest_path")
  manifest_patch=$("$jq_bin" -er '.patch_sha256' "$manifest_path")
  manifest_derivation=$("$jq_bin" -er '.derivation_path' "$manifest_path")
  manifest_output=$("$jq_bin" -er '.output_path' "$manifest_path")
  manifest_closure_paths=$("$jq_bin" -er '.closure_paths' "$manifest_path")
  manifest_archive=$("$jq_bin" -er '.archive' "$manifest_path")
  manifest_archive_digest=$("$jq_bin" -er '.archive_sha256' "$manifest_path")

  require_store_path 'manifest derivation path' "$manifest_derivation"
  require_store_path 'manifest output path' "$manifest_output"
  codex_bundle_require_single_line 'manifest tag' "$manifest_tag" || exit 1
  codex_bundle_require_single_line 'manifest system' "$manifest_system" || exit 1
  codex_bundle_require_single_line 'manifest Codex version' "$manifest_version" || exit 1
  codex_bundle_require_single_line 'manifest patch digest' "$manifest_patch" || exit 1
  codex_bundle_require_single_line 'manifest archive' "$manifest_archive" || exit 1
  codex_bundle_require_single_line 'manifest archive digest' "$manifest_archive_digest" || exit 1

  [[ "$manifest_schema" == 1 ]] || fail 'manifest schema mismatch'
  [[ "$manifest_tag" == "$CODEX_BUNDLE_TAG" ]] || fail 'manifest tag mismatch'
  [[ "$manifest_system" == "$CODEX_BUNDLE_SYSTEM" ]] || fail 'manifest system mismatch'
  [[ "$manifest_version" == "$CODEX_BUNDLE_VERSION" ]] || fail 'manifest Codex version mismatch'
  [[ "$manifest_patch" == "$CODEX_BUNDLE_PATCH_SHA256" ]] || fail 'manifest patch digest mismatch'
  [[ "$manifest_derivation" == "$CODEX_BUNDLE_DERIVATION_PATH" ]] ||
    fail 'manifest derivation path mismatch'
  [[ "$manifest_output" == "$CODEX_BUNDLE_OUTPUT_PATH" ]] || fail 'manifest output path mismatch'
  [[ "$manifest_closure_paths" =~ ^[1-9][0-9]*$ ]] || fail 'manifest closure path count is invalid'
  [[ "$manifest_archive" == "$CODEX_BUNDLE_ARCHIVE" ]] || fail 'manifest archive name mismatch'
  actual_digest=$(file_sha256 "$archive_path") || fail 'could not digest bundle archive'
  [[ "$manifest_archive_digest" == "$actual_digest" ]] || fail 'manifest archive digest mismatch'

  local imported_paths
  imported_paths=$("$zstd_bin" -q -dc "$archive_path" |
    "$sudo_bin" "$nix_store_bin" --import) || fail 'bundle import failed'
  printf '%s\n' "$imported_paths" | grep -Fx "$CODEX_BUNDLE_OUTPUT_PATH" >/dev/null ||
    fail 'expected output path was not imported'
  "$nix_store_bin" --verify-path "$CODEX_BUNDLE_OUTPUT_PATH" || fail 'store verification failed'
  verify_installed_output

  printf 'imported\ntag: %s\noutput: %s\n' "$CODEX_BUNDLE_TAG" "$CODEX_BUNDLE_OUTPUT_PATH"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
