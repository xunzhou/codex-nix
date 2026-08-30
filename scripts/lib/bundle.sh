#!/usr/bin/env bash

codex_bundle_repo='xunzhou/codex-nix'
codex_bundle_system='x86_64-linux'
codex_bundle_feature='codex-artifact-publisher'
codex_bundle_nix_global_args=(
  --extra-experimental-features
  'nix-command flakes'
)

codex_bundle_fail() {
  printf 'codex bundle: %s\n' "$*" >&2
  return 1
}

codex_bundle_require_single_line() {
  local name="$1"
  local value="$2"

  if [[ -z "$value" || "$value" == *$'\n'* || "$value" == *$'\r'* ]]; then
    codex_bundle_fail "$name must not contain newlines or be empty"
    return 1
  fi
}

codex_bundle_store_hash() {
  local name="$1"
  local path="$2"
  local store_name
  local store_hash

  codex_bundle_require_single_line "$name" "$path" || return 1
  if [[ "$path" != /nix/store/* ]]; then
    codex_bundle_fail "$name must be an absolute /nix/store path"
    return 1
  fi

  store_name="${path#/nix/store/}"
  if [[ "$store_name" != *-* || "$store_name" == */* || -z "${store_name#*-}" ]]; then
    codex_bundle_fail "$name must be a direct /nix/store child"
    return 1
  fi
  store_hash="${store_name%%-*}"
  if [[ ! "$store_hash" =~ ^[0-9a-df-np-sv-z]{32}$ ]]; then
    codex_bundle_fail "invalid Nix store hash in $name"
    return 1
  fi
  printf '%s\n' "$store_hash"
}

codex_bundle_source_is_immutable() {
  local logical_root="$1"
  local physical_root="$2"

  [[ "$logical_root" == "$physical_root" ]] || return 1
  codex_bundle_store_hash 'source root' "$physical_root" >/dev/null 2>&1 || return 1
  nix-store --check-validity "$physical_root" >/dev/null 2>&1
}

codex_bundle_initialize() {
  local flake_ref="${1:-.}"
  local library_path
  local library_dir
  local logical_repo_root
  local repo_root
  local git_status
  local system
  local version
  local patch_file
  local patch_checksum
  local patch_sha256
  local derivation_path
  local output_path
  local drv_hash

  codex_bundle_require_single_line 'flake reference' "$flake_ref" || return 1
  library_path="${BASH_SOURCE[0]}"
  if [[ "$library_path" != /* ]]; then
    library_path="$PWD/$library_path"
  fi
  logical_repo_root=$(dirname "$(dirname "$(dirname "$library_path")")") || return 1
  logical_repo_root=$(cd -L "$logical_repo_root" && pwd -L) || return 1
  library_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P) || return 1
  repo_root=$(cd "$library_dir/../.." && pwd -P) || return 1

  if [[ -e "$repo_root/.git" ]]; then
    git_status=$(git -C "$repo_root" status --porcelain --untracked-files=no) || return 1
    if [[ -n "$git_status" ]]; then
      codex_bundle_fail 'tracked tree is dirty'
      return 1
    fi
  elif ! codex_bundle_source_is_immutable "$logical_repo_root" "$repo_root"; then
    codex_bundle_fail 'source root must be a direct registered Nix store path'
    return 1
  fi

  system=$(nix "${codex_bundle_nix_global_args[@]}" eval --raw "$flake_ref#codex.system") || return 1
  codex_bundle_require_single_line 'Nix system' "$system" || return 1
  if [[ "$system" != "$codex_bundle_system" ]]; then
    codex_bundle_fail "unsupported system: $system"
    return 1
  fi

  version=$(nix "${codex_bundle_nix_global_args[@]}" eval --raw "$flake_ref#codex.version") || return 1
  codex_bundle_require_single_line 'Codex version' "$version" || return 1

  patch_file=$(nix "${codex_bundle_nix_global_args[@]}" eval --raw "$flake_ref#codex.patchFile") || return 1
  codex_bundle_store_hash 'patch file' "$patch_file" >/dev/null || return 1
  patch_checksum=$(sha256sum "$patch_file") || return 1
  codex_bundle_require_single_line 'patch checksum' "$patch_checksum" || return 1
  patch_sha256="${patch_checksum%%[[:space:]]*}"
  if [[ ! "$patch_sha256" =~ ^[0-9a-f]{64}$ ]]; then
    codex_bundle_fail 'patch checksum must begin with a 64-character lowercase hexadecimal digest'
    return 1
  fi

  derivation_path=$(nix "${codex_bundle_nix_global_args[@]}" path-info --derivation "$flake_ref#codex") || return 1
  drv_hash=$(codex_bundle_store_hash 'derivation path' "$derivation_path") || return 1
  if [[ "$derivation_path" != *.drv ]]; then
    codex_bundle_fail 'derivation path must end in .drv'
    return 1
  fi

  output_path=$(nix "${codex_bundle_nix_global_args[@]}" derivation show "$derivation_path" | jq -er '
    def derivation_map:
      if (.derivations? | type) == "object" then
        .derivations
      elif type == "object" then
        .
      else
        error("derivation metadata is not an object")
      end;

    derivation_map
    | if length == 1 then .[] else error("expected exactly one derivation") end
    | [.outputs? | objects | .out? | objects | .path? | strings]
    | if length == 1 then .[0] else error("expected exactly one output path") end
  ') || return 1
  if [[ "$output_path" != /* && "$output_path" != */* ]]; then
    output_path="/nix/store/$output_path"
  fi
  codex_bundle_store_hash 'output path' "$output_path" >/dev/null || return 1

  CODEX_BUNDLE_REPO="$codex_bundle_repo"
  CODEX_BUNDLE_SYSTEM="$codex_bundle_system"
  CODEX_BUNDLE_FEATURE="$codex_bundle_feature"
  CODEX_BUNDLE_VERSION="$version"
  CODEX_BUNDLE_PATCH_SHA256="$patch_sha256"
  CODEX_BUNDLE_DERIVATION_PATH="$derivation_path"
  CODEX_BUNDLE_OUTPUT_PATH="$output_path"
  CODEX_BUNDLE_TAG="codex-v${version}-${patch_sha256:0:16}-${drv_hash:0:16}"
  CODEX_BUNDLE_ARCHIVE="codex-${CODEX_BUNDLE_TAG}-x86_64-linux.nar.zst"
  CODEX_BUNDLE_MANIFEST="codex-${CODEX_BUNDLE_TAG}-manifest.json"
  CODEX_BUNDLE_CHECKSUMS="codex-${CODEX_BUNDLE_TAG}-SHA256SUMS"
  export CODEX_BUNDLE_REPO CODEX_BUNDLE_SYSTEM CODEX_BUNDLE_FEATURE CODEX_BUNDLE_VERSION
  export CODEX_BUNDLE_PATCH_SHA256 CODEX_BUNDLE_DERIVATION_PATH CODEX_BUNDLE_OUTPUT_PATH
  export CODEX_BUNDLE_TAG CODEX_BUNDLE_ARCHIVE CODEX_BUNDLE_MANIFEST CODEX_BUNDLE_CHECKSUMS
}
