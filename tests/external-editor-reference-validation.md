# Native external-editor reference: implementation and validation

The recipe on this branch pins Codex **0.163.0**, not the installed 0.166.0
binary. Its upstream annotated tag `rust-v0.163.0` resolves to commit
`83086188ae0614bd882818ff1aa23f3194962012`. The realized source's NAR hash was
verified against `build.json`:
`sha256-Fa9dfiGJy+uqSF4A0EM6AQQhhjQdu+jfhKzqXid9PNA=`.
The existing palette and double-Escape patches apply before
`patches/0.163.0/external-editor-reference.patch`. Dependencies are unchanged.

## Data path and format

`ChatWidget::external_editor_reference` uses the active widget's existing
`TranscriptState::last_agent_markdown`, the same completed-response cache used by
copy-response. Completion and history replay populate it; conversation resets
clear it. There is no filesystem/session lookup or terminal scraping. Separate
widgets keep separate response caches. The opt-in is deserialized through
`codex-config::types::Tui`, resolved into the runtime `Config`, and carried through
`LocalSettings`; the generated configuration schema includes it.

The native `App::launch_external_editor` still resolves `VISUAL`/`EDITOR`, pauses
terminal input, and awaits the editor while holding the mutable originating App.
Conversation-switch and submit events cannot be processed during that await.
Success calls `apply_external_edit`, never a submit operation. The opt-in removes
the legacy trailing-whitespace trim so replies round-trip exactly.

A document with reference context has this shape (the actual id is a fresh UUID,
checked against the reference and draft):

```text
Which color?
Which size?

<!-- BEGIN REPLY <unique id> -->
[existing draft; edit here]
<!-- END REPLY <unique id> -->
```

Extraction requires an exact match for the complete reference prefix and the
closing suffix. Framing contributes its own newlines; those are removed without
trimming reply bytes. A repeated marker id in the reply is rejected. Modified
references, changed/missing markers, duplicated documents, extra text outside the
reply, and line-ending conversion fail closed. Codex does not guess which text
might be a reply. It preserves the original composer and retains the editor file
at its existing path for manual recovery, with an error naming that path.

Editor spawn failures, nonzero exits, signals, and read errors also preserve the
original composer and retain available files when opted in. If the editor deletes
the file, Codex reports it as unavailable. Successful unchanged exits preserve the
draft; saving an empty reply clears it. With no reference, only the draft is
opened. Disabled mode keeps the original editor behavior, including trimming and
failed-file cleanup. No editor-directory or sandbox permission rule is changed.

## Reproducing source checks

Apply the ordered `build.json` patch stack to the pinned upstream source, then
normalize local workspace lockfile versions with the existing source-build
workflow. From `codex-rs`, run:

```sh
RUST_MIN_STACK=16777216 cargo test --locked -p codex-tui -p codex-config --lib external_editor -- --test-threads=2
cargo build --locked -p codex-cli -p codex-code-mode-host
```

The debug test-thread stack setting accommodates the large existing App fixtures;
it changes no application permissions. Nix runs the narrower
`external_editor_reference` filter for both crates during `doCheck`. The native
source installer includes the editor tests in `CODEX_VERIFY_PATCHES=1` verification.
The source installer supplies the recipe's pinned V8 assets when building.

Coverage includes opt-in default/config parsing; two active conversation widgets;
history replay and cache reset; exact whitespace/Unicode reply extraction;
existing and empty drafts; reference and delimiter corruption; duplicate framing;
unchanged editor exits; empty saved replies; signals and editor failures; retained
recovery files; and deleted files. An App-level synthetic shell-editor test drives
questions → native editor → edited originating unsent composer and verifies no
submit operation. Its malformed-buffer pass verifies the original draft and
reported recovery file through the same App return path.

## Results

The owner reported the original feature validated in a manual trial, then requested
removing the reference heading and three instruction/backup-notice lines from the
editor document. Only the assistant response and marked reply area remain. The
backup notice remains in the README.

Validated in the isolated feature worktree:

- **33 focused source tests passed:** 32 in `codex-tui` and one in `codex-config`.
  This includes the real App launch/return path with a synthetic shell editor,
  exact unsent reply, no submit operation, and visible malformed-file recovery.
- Both `codex-cli` and `codex-code-mode-host` built successfully in the native
  debug profile with Rust 1.95.0 and the recipe's checksum-verified V8 150.4.0 assets.
  The binaries passed `--version` (0.163.0) and `--help` smoke checks respectively.
- 61 repository Python tests passed across source-patcher, manifest, release,
  rebase-patches, release-client, and release-setup. Three Mergiraf executable
  tests were skipped because the pinned resolver was not configured locally.
- Updater and bundle shell suites passed. Repository package policy passed,
  including strict application of the ordered patches to the pinned source.
- `nix flake check --no-build` passed. This is Nix evaluation evidence, **not a
  completed Nix release build**. The source builds/tests above are native Cargo
  builds, not the installed binary or a Zed GUI trial.

The initial debug harness hit an existing chord test's small default thread stack;
using the documented stack size resolved it. A deletion fixture initially hit the
machine's trash wrapper; the focused test now uses `unlink` on its own temporary
file. No editor permissions were widened to make either test pass.

## Limits

The patch is reviewed for the pinned 0.163.0 source. Porting to the separate
0.166.0 release branch requires applying/reviewing it against that recipe; the
installed 0.166.0 binary does not gain this feature. Builds and tests here do not
install or switch the default binary, modify owner sessions/configuration, or
exercise an elevated editor. Synthetic editor tests are source integration tests,
not evidence of a Zed GUI trial.

Successful temporary files use native cleanup. Retained failure files require
manual removal. Editor history, backups, swap files, or sync may retain reference
copies, and this extension cannot remove those. User-entered text inside the
reply area is returned as the reply; the boundary protection prevents automatic
import of the reference section.
