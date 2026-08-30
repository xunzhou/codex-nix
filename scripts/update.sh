#!/usr/bin/env bash
set -euo pipefail

# Exit 10 means the requested release was already current after upstream release
# metadata and local package version validation. Every other nonzero status is a
# failed update or validation.
already_current_exit=10
upstream_api='https://api.github.com/repos/openai/codex'
stable_tag_pattern='^rust-v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'
stable_version_pattern='^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$repo_root"

fail() {
  printf 'codex updater: %s\n' "$*" >&2
  exit 1
}

usage() {
  printf 'usage: %s [VERSION]\n' "${0##*/}" >&2
  printf 'exit 10: requested version is already current after validation\n' >&2
  exit 2
}

github_api() {
  local -a headers=(
    -H 'Accept: application/vnd.github+json'
    -H 'X-GitHub-Api-Version: 2026-03-10'
  )

  if [[ -n ${GITHUB_TOKEN:-} ]]; then
    headers+=(-H "Authorization: Bearer $GITHUB_TOKEN")
  fi

  curl --fail-with-body --silent --show-error --location \
    "${headers[@]}" "$1"
}

(($# <= 1)) || usage

if (($# == 1)); then
  version="$1"
  [[ "$version" =~ $stable_version_pattern ]] ||
    fail 'VERSION must be a stable semantic version without the rust-v prefix'
  release_tag="rust-v$version"
  release_metadata=$(github_api "$upstream_api/releases/tags/$release_tag") ||
    fail "could not resolve upstream release $release_tag"
  validated_tag=$(jq -er --arg tag "$release_tag" '
    if type == "object"
      and .tag_name == $tag
      and .draft == false
      and .prerelease == false
    then .tag_name
    else error("release is not the requested published stable tag")
    end
  ' <<<"$release_metadata") || fail "upstream release $release_tag is not stable"
else
  releases_metadata=$(github_api "$upstream_api/releases?per_page=100") ||
    fail 'could not list upstream releases'
  validated_tag=$(jq -er --arg pattern "$stable_tag_pattern" '
    if type != "array" then
      error("release listing is not an array")
    else
      [
        .[]
        | select(
            type == "object"
            and .draft == false
            and .prerelease == false
            and (.tag_name | type) == "string"
            and (.tag_name | test($pattern))
          )
        | {
            tag: .tag_name,
            semver: (.tag_name | ltrimstr("rust-v") | split(".") | map(tonumber))
          }
      ]
      | if length == 0 then
          error("no stable rust-v releases")
        else
          max_by(.semver).tag
        end
    end
  ' <<<"$releases_metadata") || fail 'no published stable rust-v release found'
  release_tag="$validated_tag"
  version="${release_tag#rust-v}"
fi

[[ "$validated_tag" == "$release_tag" && "$release_tag" =~ $stable_tag_pattern ]] ||
  fail 'resolved release tag is invalid'

current_version=$(nix eval --raw .#codex.version) ||
  fail 'could not evaluate the current Codex version'
[[ "$current_version" =~ $stable_version_pattern ]] ||
  fail 'current Codex version is not a stable semantic version'

if [[ "$current_version" == "$version" ]]; then
  printf 'codex updater: %s is already current; skipping build\n' "$release_tag"
  exit "$already_current_exit"
fi

nix-update codex --flake --version="$version" --override-filename=package.nix ||
  fail "could not update package.nix for $release_tag"

git diff --check || fail 'updated files contain whitespace errors'
nix flake check --no-build || fail 'flake evaluation failed'
nix --extra-system-features codex-artifact-publisher build -L .#codex ||
  fail 'Codex build failed'
test "$(./result/bin/codex --version)" = "codex-cli $version" ||
  fail 'built Codex version does not match the selected release'
./result/bin/codex-code-mode-host --help >/dev/null ||
  fail 'codex-code-mode-host smoke test failed'
grep -aFqm1 'terminal palette refresh did not return default colors' ./result/bin/codex ||
  fail 'built Codex does not contain the terminal palette patch marker'

printf 'codex updater: validated %s\n' "$release_tag"
