#!/usr/bin/env bash
set -euo pipefail
umask 077

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=lib/bundle.sh
source "$repo_root/scripts/lib/bundle.sh"

fail() {
  printf 'codex bundle publisher: %s\n' "$*" >&2
  exit 1
}

usage() {
  printf 'usage: %s [--dry-run] [--flake-ref REF]\n' "${0##*/}" >&2
  exit 2
}

dry_run=0
flake_ref=.
while (($#)); do
  case "$1" in
    --dry-run)
      dry_run=1
      shift
      ;;
    --flake-ref)
      (($# >= 2)) || usage
      flake_ref="$2"
      shift 2
      ;;
    *) usage ;;
  esac
done

codex_bundle_initialize "$flake_ref"

if ((dry_run)); then
  printf 'dry run\ntag: %s\noutput: %s\n' "$CODEX_BUNDLE_TAG" "$CODEX_BUNDLE_OUTPUT_PATH"
  exit 0
fi

: "${GITHUB_REPOSITORY:=xunzhou/codex-nix}"
: "${GITHUB_TOKEN:?GITHUB_TOKEN is required for publication}"
if [[ "$GITHUB_REPOSITORY" != "$CODEX_BUNDLE_REPO" ]]; then
  fail "GITHUB_REPOSITORY must be $CODEX_BUNDLE_REPO"
fi

require_command() {
  local name="$1"
  local path
  path=$(command -v "$name") || fail "missing dependency: $name"
  [[ "$path" == /* ]] || fail "dependency is not an absolute path: $name"
  printf '%s\n' "$path"
}

curl_bin=$(require_command curl)
jq_bin=$(require_command jq)
nix_bin=$(require_command nix)
nix_store_bin=$(require_command nix-store)
sha256sum_bin=$(require_command sha256sum)
zstd_bin=$(require_command zstd)

github_api() {
  "$curl_bin" --fail-with-body --silent --show-error \
    -H 'Accept: application/vnd.github+json' \
    -H "Authorization: Bearer $GITHUB_TOKEN" \
    -H 'X-GitHub-Api-Version: 2022-11-28' "$@"
}

github_asset_download() {
  "$curl_bin" --fail-with-body --silent --show-error \
    -H 'Accept: application/octet-stream' \
    -H "Authorization: Bearer $GITHUB_TOKEN" \
    -H 'X-GitHub-Api-Version: 2022-11-28' "$@"
}

file_sha256() {
  local path="$1"
  local name="${path##*/}"
  local checksum
  local digest

  checksum=$("$sha256sum_bin" "$path") || fail "could not hash $name"
  digest="${checksum%%[[:space:]]*}"
  [[ "$digest" =~ ^[0-9a-f]{64}$ ]] || fail "invalid SHA-256 for $name"
  printf '%s\n' "$digest"
}

validate_existing_checksum_file() {
  local checksums_path="$1"
  local line
  local digest
  local filename
  local archive_entries=0
  local manifest_entries=0

  existing_archive_sha256=''
  existing_manifest_sha256=''
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[0-9a-f]{64}\ \ [^[:space:]/]+$ ]] || return 1
    digest="${line:0:64}"
    filename="${line:66}"
    case "$filename" in
      "$CODEX_BUNDLE_ARCHIVE")
        archive_entries=$((archive_entries + 1))
        existing_archive_sha256="$digest"
        ;;
      "$CODEX_BUNDLE_MANIFEST")
        manifest_entries=$((manifest_entries + 1))
        existing_manifest_sha256="$digest"
        ;;
      *) return 1 ;;
    esac
  done <"$checksums_path"

  ((archive_entries == 1 && manifest_entries == 1))
}

api_root="${GITHUB_API_URL:-https://api.github.com}"
uploads_root="${GITHUB_UPLOADS_URL:-https://uploads.github.com}"
temporary_directory=$(mktemp -d)
created_release_id=''
cleanup() {
  local status=$?
  if ((status != 0)) && [[ -n "$created_release_id" ]]; then
    github_api -X DELETE \
      "$api_root/repos/$GITHUB_REPOSITORY/releases/$created_release_id" \
      >/dev/null 2>&1 || true
  fi
  rm -rf "$temporary_directory"
  exit "$status"
}
trap cleanup EXIT

release_metadata_path="$temporary_directory/release.json"
release_endpoint="$api_root/repos/$GITHUB_REPOSITORY/releases/tags/$CODEX_BUNDLE_TAG"
if release_http_status=$(github_api -o "$release_metadata_path" -w '%{http_code}' \
  "$release_endpoint"); then
  release_query_status=0
else
  release_query_status=$?
fi

if [[ ! "$release_http_status" =~ ^[0-9]{3}$ ]]; then
  fail 'could not determine whether release exists'
fi

case "$release_http_status" in
  2??)
    if ((release_query_status != 0)); then
      fail 'could not determine whether release exists'
    fi
    if ! "$jq_bin" -e \
      'type == "object"
        and ((.tag_name? | type) == "string")
        and ((.assets? | type) == "array")' \
      "$release_metadata_path" >/dev/null; then
      fail 'could not determine whether release exists'
    fi
    release_exists=1
    ;;
  404)
    if ((release_query_status != 22)); then
      fail 'could not determine whether release exists'
    fi
    release_exists=0
    ;;
  *) fail 'could not determine whether release exists' ;;
esac

if ((release_exists)); then
  if ! "$jq_bin" -e --arg tag "$CODEX_BUNDLE_TAG" '.tag_name == $tag' \
    "$release_metadata_path" >/dev/null; then
    fail 'existing release metadata does not match the exact tag'
  fi
  if ! "$jq_bin" -e \
    --arg archive "$CODEX_BUNDLE_ARCHIVE" \
    --arg manifest "$CODEX_BUNDLE_MANIFEST" \
    --arg checksums "$CODEX_BUNDLE_CHECKSUMS" \
    '.assets as $assets
      | ($assets | length) == 3
      and ([$assets[] | select(.name? == $archive)] | length) == 1
      and ([$assets[] | select(.name? == $manifest)] | length) == 1
      and ([$assets[] | select(.name? == $checksums)] | length) == 1' \
    "$release_metadata_path" >/dev/null; then
    fail 'existing release assets do not match the exact bundle inventory'
  fi
  if ! "$jq_bin" -e \
    'all(.assets[];
      ((.digest? | type) == "string")
      and (.digest | test("^sha256:[0-9a-f]{64}$"))
      and ((.id? | type) == "number")
      and (.id > 0)
      and ((.id | floor) == .id))' \
    "$release_metadata_path" >/dev/null; then
    fail 'existing release asset digest is missing or malformed'
  fi

  release_archive_sha256=$("$jq_bin" -er --arg name "$CODEX_BUNDLE_ARCHIVE" \
    '.assets[] | select(.name == $name) | .digest | ltrimstr("sha256:")' \
    "$release_metadata_path") || fail 'could not read existing archive asset digest'
  release_manifest_sha256=$("$jq_bin" -er --arg name "$CODEX_BUNDLE_MANIFEST" \
    '.assets[] | select(.name == $name) | .digest | ltrimstr("sha256:")' \
    "$release_metadata_path") || fail 'could not read existing manifest asset digest'
  release_checksums_sha256=$("$jq_bin" -er --arg name "$CODEX_BUNDLE_CHECKSUMS" \
    '.assets[] | select(.name == $name) | .digest | ltrimstr("sha256:")' \
    "$release_metadata_path") || fail 'could not read existing checksum asset digest'
  manifest_id=$("$jq_bin" -er --arg name "$CODEX_BUNDLE_MANIFEST" \
    '.assets[] | select(.name == $name) | .id' "$release_metadata_path") ||
    fail 'could not read existing manifest asset ID'
  checksums_id=$("$jq_bin" -er --arg name "$CODEX_BUNDLE_CHECKSUMS" \
    '.assets[] | select(.name == $name) | .id' "$release_metadata_path") ||
    fail 'could not read existing checksum asset ID'

  manifest_path="$temporary_directory/$CODEX_BUNDLE_MANIFEST"
  checksums_path="$temporary_directory/$CODEX_BUNDLE_CHECKSUMS"
  github_asset_download -L -o "$manifest_path" \
    "$api_root/repos/$GITHUB_REPOSITORY/releases/assets/$manifest_id" ||
    fail 'could not download existing manifest asset'
  github_asset_download -L -o "$checksums_path" \
    "$api_root/repos/$GITHUB_REPOSITORY/releases/assets/$checksums_id" ||
    fail 'could not download existing checksum asset'
  [[ -f "$manifest_path" && -f "$checksums_path" ]] ||
    fail 'existing release validation assets are incomplete'

  actual_checksums_sha256=$(file_sha256 "$checksums_path")
  [[ "$actual_checksums_sha256" == "$release_checksums_sha256" ]] ||
    fail 'existing release checksum asset digest mismatch'
  if ! validate_existing_checksum_file "$checksums_path"; then
    fail 'existing release checksum file has invalid entries'
  fi

  actual_manifest_sha256=$(file_sha256 "$manifest_path")
  # shellcheck disable=SC2055 # The file must equal both independently supplied digests.
  if [[ "$actual_manifest_sha256" != "$release_manifest_sha256" ||
    "$actual_manifest_sha256" != "$existing_manifest_sha256" ]]; then
    fail 'existing release manifest checksum mismatch'
  fi

  if ! "$jq_bin" -e \
    --arg tag "$CODEX_BUNDLE_TAG" \
    --arg system "$CODEX_BUNDLE_SYSTEM" \
    --arg version "$CODEX_BUNDLE_VERSION" \
    --arg patch "$CODEX_BUNDLE_PATCH_SHA256" \
    --arg derivation "$CODEX_BUNDLE_DERIVATION_PATH" \
    --arg output "$CODEX_BUNDLE_OUTPUT_PATH" \
    --arg archive "$CODEX_BUNDLE_ARCHIVE" \
    '.schema == 1
      and .tag == $tag
      and .system == $system
      and .codex_version == $version
      and .patch_sha256 == $patch
      and .derivation_path == $derivation
      and .output_path == $output
      and ((.closure_paths | type) == "number")
      and (.closure_paths > 0)
      and ((.closure_paths | floor) == .closure_paths)
      and .archive == $archive
      and ((.archive_sha256 | type) == "string")
      and (.archive_sha256 | test("^[0-9a-f]{64}$"))' \
    "$manifest_path" >/dev/null; then
    fail 'existing release manifest does not match the local bundle identity'
  fi
  manifest_archive_sha256=$("$jq_bin" -er '.archive_sha256' "$manifest_path") ||
    fail 'could not read existing manifest archive checksum'
  # shellcheck disable=SC2055 # All three independently supplied digests must agree.
  if [[ "$existing_archive_sha256" != "$release_archive_sha256" ||
    "$existing_archive_sha256" != "$manifest_archive_sha256" ]]; then
    fail 'existing release archive checksum mismatch'
  fi

  printf 'cache hit\ntag: %s\noutput: %s\n' "$CODEX_BUNDLE_TAG" "$CODEX_BUNDLE_OUTPUT_PATH"
  exit 0
fi

# shellcheck disable=SC2154 # Defined by the sourced bundle identity library.
realized=$("$nix_bin" "${codex_bundle_nix_global_args[@]}" \
  --extra-system-features "$CODEX_BUNDLE_FEATURE" \
  build --no-link --print-out-paths "${CODEX_BUNDLE_DERIVATION_PATH}^*")
if [[ "$realized" != "$CODEX_BUNDLE_OUTPUT_PATH" ]]; then
  fail 'realized output path does not match the local bundle identity'
fi

closure_path="$temporary_directory/closure-paths"
if ! "$nix_store_bin" -qR "$CODEX_BUNDLE_OUTPUT_PATH" | LC_ALL=C sort -u >"$closure_path"; then
  fail 'recursive closure query failed'
fi
mapfile -t closure <"$closure_path"
if ((${#closure[@]} == 0)); then
  fail 'recursive closure is empty'
fi

archive_path="$temporary_directory/$CODEX_BUNDLE_ARCHIVE"
manifest_path="$temporary_directory/$CODEX_BUNDLE_MANIFEST"
checksums_path="$temporary_directory/$CODEX_BUNDLE_CHECKSUMS"
"$nix_store_bin" --export "${closure[@]}" | "$zstd_bin" -q -T0 -o "$archive_path"

archive_sha256=$(file_sha256 "$archive_path")
"$jq_bin" -n \
  --arg tag "$CODEX_BUNDLE_TAG" \
  --arg system "$CODEX_BUNDLE_SYSTEM" \
  --arg version "$CODEX_BUNDLE_VERSION" \
  --arg patch "$CODEX_BUNDLE_PATCH_SHA256" \
  --arg derivation "$CODEX_BUNDLE_DERIVATION_PATH" \
  --arg output "$CODEX_BUNDLE_OUTPUT_PATH" \
  --argjson closure_paths "${#closure[@]}" \
  --arg archive "$CODEX_BUNDLE_ARCHIVE" \
  --arg archive_sha256 "$archive_sha256" \
  '{
    schema: 1,
    tag: $tag,
    system: $system,
    codex_version: $version,
    patch_sha256: $patch,
    derivation_path: $derivation,
    output_path: $output,
    closure_paths: $closure_paths,
    archive: $archive,
    archive_sha256: $archive_sha256
  }' >"$manifest_path"

(
  cd "$temporary_directory"
  "$sha256sum_bin" "$CODEX_BUNDLE_ARCHIVE" "$CODEX_BUNDLE_MANIFEST" >"$CODEX_BUNDLE_CHECKSUMS"
  "$sha256sum_bin" --check "$CODEX_BUNDLE_CHECKSUMS"
)

[[ -s "$archive_path" && -s "$manifest_path" && -s "$checksums_path" ]] ||
  fail 'generated bundle assets are incomplete'

create_payload=$("$jq_bin" -n \
  --arg tag "$CODEX_BUNDLE_TAG" \
  --arg name "$CODEX_BUNDLE_TAG" \
  --arg body "Immutable Nix closure bundle for Codex $CODEX_BUNDLE_VERSION" \
  '{tag_name: $tag, name: $name, body: $body}')
created_metadata_path="$temporary_directory/created-release.json"
if create_http_status=$(github_api -X POST --data "$create_payload" \
  -H 'Content-Type: application/json' \
  -o "$created_metadata_path" \
  -w '%{http_code}' \
  "$api_root/repos/$GITHUB_REPOSITORY/releases"
); then
  create_status=0
else
  create_status=$?
fi
if ((create_status != 0)) || [[ "$create_http_status" != 201 ]]; then
  fail 'release creation did not return HTTP 201'
fi
created_release_candidate=$("$jq_bin" -er --arg tag "$CODEX_BUNDLE_TAG" '
  select(type == "object")
  | select(.tag_name? == $tag)
  | select((.assets? | type) == "array")
  | .id?
  | select(type == "number" and . > 0 and floor == .)
' "$created_metadata_path") || fail 'created release metadata is invalid'
if [[ ! "$created_release_candidate" =~ ^[1-9][0-9]*$ ]]; then
  fail 'created release metadata is invalid'
fi
created_release_id="$created_release_candidate"

upload_index=0
manifest_sha256=$(file_sha256 "$manifest_path")
checksums_sha256=$(file_sha256 "$checksums_path")
for asset_path in "$archive_path" "$manifest_path" "$checksums_path"; do
  upload_index=$((upload_index + 1))
  asset_name="${asset_path##*/}"
  upload_response_path="$temporary_directory/upload-$upload_index.json"
  if upload_http_status=$(github_api -X POST \
    -H 'Content-Type: application/octet-stream' \
    --data-binary "@$asset_path" \
    -o "$upload_response_path" \
    -w '%{http_code}' \
    "$uploads_root/repos/$GITHUB_REPOSITORY/releases/$created_release_id/assets?name=$asset_name" \
  ); then
    upload_status=0
  else
    upload_status=$?
  fi
  if ((upload_status != 0)) || [[ "$upload_http_status" != 201 ]]; then
    fail 'upload did not return HTTP 201'
  fi
  case "$asset_path" in
    "$archive_path") asset_sha256="$archive_sha256" ;;
    "$manifest_path") asset_sha256="$manifest_sha256" ;;
    "$checksums_path") asset_sha256="$checksums_sha256" ;;
    *) fail 'internal asset inventory mismatch' ;;
  esac
  if ! "$jq_bin" -e \
    --arg name "$asset_name" \
    --arg digest "sha256:$asset_sha256" \
    'type == "object"
      and .name == $name
      and .state == "uploaded"
      and .digest == $digest' \
    "$upload_response_path" >/dev/null; then
    fail 'uploaded asset metadata is invalid'
  fi
done

final_release_path="$temporary_directory/final-release.json"
if final_release_http_status=$(github_api -o "$final_release_path" -w '%{http_code}' \
  "$release_endpoint"); then
  final_release_status=0
else
  final_release_status=$?
fi
if ((final_release_status != 0)) || [[ "$final_release_http_status" != 200 ]]; then
  fail 'published release assets do not match the exact bundle inventory and digests'
fi
if ! "$jq_bin" -e \
  --arg tag "$CODEX_BUNDLE_TAG" \
  --arg archive "$CODEX_BUNDLE_ARCHIVE" \
  --arg archive_digest "sha256:$archive_sha256" \
  --arg manifest "$CODEX_BUNDLE_MANIFEST" \
  --arg manifest_digest "sha256:$manifest_sha256" \
  --arg checksums "$CODEX_BUNDLE_CHECKSUMS" \
  --arg checksums_digest "sha256:$checksums_sha256" \
  'type == "object"
    and .tag_name == $tag
    and ((.assets? | type) == "array")
    and (.assets as $assets
      | ($assets | length) == 3
      and ([$assets[]
        | select(.name == $archive and .state == "uploaded" and .digest == $archive_digest)]
        | length) == 1
      and ([$assets[]
        | select(.name == $manifest and .state == "uploaded" and .digest == $manifest_digest)]
        | length) == 1
      and ([$assets[]
        | select(.name == $checksums and .state == "uploaded" and .digest == $checksums_digest)]
        | length) == 1)' \
  "$final_release_path" >/dev/null; then
  fail 'published release assets do not match the exact bundle inventory and digests'
fi
created_release_id=''

printf 'published\ntag: %s\noutput: %s\n' "$CODEX_BUNDLE_TAG" "$CODEX_BUNDLE_OUTPUT_PATH"
