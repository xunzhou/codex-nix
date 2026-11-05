# Native external-editor reference: implementation and validation

The recipe on this branch pins Codex **0.166.0**, the latest stable upstream
release checked on 2026-11-05. The installed binary is not modified. Its upstream
annotated tag `rust-v0.166.0` resolves to commit
`6ff1e1ca3b30fc8b914720681fcf5fe62e95f856`. The realized source's NAR hash was
verified against `build.json`:
`sha256-4zxnVT9PxIj0+ULQ4LSA46GmvfyNTueZ+BsQ2SZwk1I=`.
The existing palette and double-Escape patches apply before
`patches/0.166.0/external-editor-reference.patch`. Dependencies are unchanged.

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
it changes no application permissions. Nix adds the
`external_editor_reference` filter for both crates during `doCheck`, alongside
the existing palette, Escape, status, compaction, and usage regressions. The native
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

The 0.166.0 source port passes **33 focused tests** (32 TUI and one configuration),
including the real App editor launch/return path, exact unsent reply, no submit
operation, and malformed-buffer recovery. The source implementation applied
directly except for relocating its test module declaration to the current App
test module list. Framing, process handling, and recovery tests are byte-for-byte
the validated implementation.

Additional port checks passed:

- 12 rendered-nextest fixture tests across the TUI and configuration crates;
  unrelated fixture tests remain excluded.
- 71 repository Python tests; three optional Mergiraf executable tests skipped
  because the pinned resolver is not configured locally. The release-client
  fixture now carries every recipe marker, including the new editor marker.
- Updater and bundle suites, repository package policy (including strict ordered
  source patch application), and `nix flake check --no-build`.

Both `codex-cli` and `codex-code-mode-host` built in the native debug profile with
Rust 1.95.0 and the recipe's checksum-verified V8 152.2.0 assets. The binaries
passed `--version` (0.166.0) and `--help` smoke checks respectively. Both recipe
markers are present in the CLI, and all removed editor headings/instructions are
absent from it.

All **145 selected patch-regression cases passed**: 144 in the original tool
sandbox, plus one existing upstream test,
`external_writer_escape_preserves_snapshot_and_explicit_quits`, that requires a
loopback socket. The initial combined run encountered `Operation not permitted`
at its socket bind. That case passed independently with loopback available inside
bwrap: the root filesystem stayed read-only, with only worktree scratch and
`/tmp` writable. It does not launch an editor. No filesystem permission was
broadened, and the shipped nextest selection still includes this case. All Ctrl+G
editor tests ran in the original sandbox.

Nix validation above is evaluation and flag-preflight evidence, not a completed
Nix static release build. The actual source builds/tests were native Cargo builds.
No Zed GUI trial was performed for the 0.166.0 port.

The existing static release pipeline and all prior regression selectors are
preserved. Both Nix and native nextest selection add `codex-config` and the
`external_editor_reference` filter. The rendered-flag preflight now includes both
crates, proving the TUI framing and configuration opt-in selectors run.

## Limits

The patch is reviewed for pinned 0.166.0. It is on an isolated branch based on the
committed 0.166.0 static-release candidate; the pending release PR is unchanged.
No installed/default binary, owner configuration, or owner session is modified.
Synthetic editor tests are source integration tests. The owner trial covered the
0.163.0 implementation; it does not establish GUI behavior for this port.

Successful temporary files use native cleanup. Retained failure files require
manual removal. Editor history, backups, swap files, or sync may retain reference
copies, and this extension cannot remove those. User-entered text inside the
reply area is returned as the reply; the boundary protection prevents automatic
import of the reference section.
