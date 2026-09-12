# Double Escape patch verification

Verified against the pinned Codex 0.153.4 source.

- Both source patches apply in package order with `patch --forward --fuzz=0`.
- Package policy checks pass, including Nix derivation evaluation.
- Patched TUI: 4,050 passed, 28 failed, 2 ignored.
- Unmodified TUI: 4,048 passed, 28 failed, 2 ignored.
- The failing test names are identical: existing snapshot differences. The
  patch introduces no additional failures.

Run the TUI tests from `codex-rs` with:

```sh
RUST_MIN_STACK=16777216 cargo test -p codex-tui --lib --offline -- --test-threads=4
```

The larger stack avoids a stack overflow in the default debug test harness.
The full suite needs local socket and terminal access. Updated snapshots cover
only the wider Escape sequence hint and its resulting wrapping.

Coverage includes first-tap suppression, timeout, intervening input, turn
boundaries, key repeats/releases, goal pause, pending steers, Vim Escape,
popup dismissal, and remapped interruption. No release binary was installed.

## Linux npm source installer and Codex 0.154.0

The 0.154.0 port compiles and passes all 110 tests matching `esc`, including
normal and hidden-status interruption, goals, steers, popup dismissal, and Vim.
Both patches pass `git apply --check` against the official 0.154.0 archive,
which also passes Python's safe archive extraction filter.

Six installer integration tests pass: full patch-set invalidation, binary-pair
cache restoration, corrupt cache recovery, unsupported versions/build failure,
ordered patch application, rollback, and idempotent updater setup are covered.
The setup helper is prepared; no live updater files or npm binaries were changed
from this development workspace.
