#!/usr/bin/env bash
# Run from this repo against an extracted source with the full manifest stack applied.
set -euo pipefail
source_root=${1:?Usage: runtime-add-dir.sh /path/to/patched/codex}
cd "$source_root/codex-rs"
export RUST_MIN_STACK=${RUST_MIN_STACK:-16777216}
cargo test --locked -p codex-core -p codex-tui --lib add_dir_
cargo test --locked -p codex-app-server --test all add_dir_grant_
