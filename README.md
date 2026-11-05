# Patched Codex

Codex with three small changes:

- **Live terminal colors:** refresh the palette and adaptive syntax highlighting without restarting Codex.
- **Double Escape to interrupt:** press Escape twice within 400 ms to stop a turn.
  Menus, Vim mode, and Ctrl-C keep their usual behavior.
- **Optional editor reference:** Ctrl+G can include the active conversation’s last
  assistant response above your editable reply.

## Download or install

Find built binaries on the [releases page](https://github.com/xunzhou/codex-nix/releases).
Each release holds both the Nix closure and native Linux x86_64 binaries.
Download `codex-native-<version>-<key>-linux-x86_64.tar.gz`: it includes static
(musl) `codex` and `codex-code-mode-host` that run on any x86_64 Linux.

To update an npm installation, run from this checkout:

```sh
python3 scripts/install-release.py --install-reviewed
```

Requires Python 3.12+, curl, and npm.
The installer verifies the download before installing its matching npm version.
Routine updates download binaries; they do not compile locally.

With Nix, import the prebuilt closure, then install it:

```sh
nix run github:xunzhou/codex-nix#install
nix profile install github:xunzhou/codex-nix
```

After changing terminal colors, refresh the most recent Codex process:

```sh
kill -USR1 "$(pgrep -n codex)"
```

Palette refresh follows the current session's `tui.theme` preference. With no
explicit theme, syntax highlighting switches between Catppuccin Latte and Mocha
with the terminal background. Explicit bundled or custom themes stay selected.
A successful refresh invalidates syntax rendering caches; a failed terminal
probe keeps the last usable palette and syntax theme. Terminal scrollback that
has already left the redrawable viewport retains its original colors.

## Build and export once

The default Nix package builds static musl `codex` and `codex-code-mode-host`
with static OpenSSL and libcap. The updater exports these exact binaries into
its native tarball, alongside the Nix closure and matching recipe archive.
Release tags, asset naming, checksums, and both installation paths keep their
existing format.

For local development, `codex-static` builds the same source and runs the same
patch tests without the publisher-only feature requirement. It uses eight
compiler jobs; the release package keeps its two-job limit.

```sh
nix build .#codex-static --out-link result-static
python3 scripts/export-native.py --package result-static --output-dir dist
bash tests/static-portability.sh result-static
```

The exporter preserves the exact binary bytes and verifies the version, patch
marker, checksums, and absence of ELF interpreter/shared-library dependencies.
The portability check requires `bwrap`, `readelf`, and Python 3.12+. It runs the
exported pair and a V8 calculation in an empty filesystem without `/nix/store`.
Pass an existing exported tarball as its second argument to skip repacking.
Use sufficient temporary disk space for fresh builds; hosted build jobs reclaim
unused SDKs and add 16 GiB of swap for the optimized Rust test link.
A tiny nextest fixture validates the rendered flags and test selection before
the full compilation. Escape tests match `esc` at a name-word prefix, avoiding
unrelated `descendants`, `reschedule`, and `coalesces` integration tests.

## Automatic updates

[GitHub Actions](https://github.com/xunzhou/codex-nix/actions/workflows/update.yml)
checks upstream once a day, compiles the patched static binaries once with Nix,
runs the palette and interrupt regression tests, and publishes the Nix closure
and native tarball in one release. Both fresh-install download checks remain
required after publication.

Simple patch drift is rebased automatically. Conflicts or failing tests stop the
update and leave the last working release available. Check the failed run's
summary and artifacts, commit a reviewed patch port, then rerun the workflow.

The rebaser also recognizes two reviewed conflict recipes: status-hint snapshots
and the `handle_key_event` return-type change combined with the interrupt-tap
reset. Each recipe verifies the expected conflict before resolving it; unexpected
code changes still stop publication. A reviewed port in `patches/<version>/`
takes precedence over automatic rebasing.

Patches are replayed as Git commits so upstream renames and deletions participate
in the merge. Deleted snapshots stay deleted only when the patch's entire edit
is the verified status-hint rewrite; unrelated snapshot edits still require review.

For other Rust conflicts, the prepare job installs checksum-pinned
[Mergiraf 0.20.0](https://mergiraf.org/usage.html) and runs its contract tests.
The rebaser invokes it only after Git and the reviewed conflict recipes cannot
resolve a Rust file. It uses the three index revisions and accepts only a
successful merge with no remaining conflict markers. Unsupported files,
conflicting edits, tool failures, and timeouts still stop the update. Local
rebases opt in by setting `CODEX_MERGIRAF` to the binary path. Binary builds and
patch behavior tests remain required before publication.

[Git rerere](https://git-scm.com/docs/git-rerere) can reuse recorded resolutions,
but this updater creates a fresh temporary repository on every run. Using rerere
would require a persistent, reviewed resolution cache; enabling it alone would
not help the current workflow.

## Ctrl+G with assistant reference

The 0.166.0 recipe also includes an opt-in native editor extension. In `config.toml`:

```toml
[tui]
external_editor_include_reference = true
```

Or start the patched build with
`codex -c tui.external_editor_include_reference=true`. Existing `VISUAL`/`EDITOR`
configuration is reused, including its wait-for-close option (for example, Zed's
`--wait`). This setting does not configure or install an editor.

Ctrl+G opens the current draft below the active conversation's last completed
assistant response, using the same response cache as Codex's copy-response action.
Edit only between the unique `BEGIN REPLY` and `END REPLY` markers. Closing successfully returns only that
reply, with whitespace preserved, to the originating unsent composer. It does
not submit. Existing drafts are included; with no prior response the document
contains just the draft. An empty reply clears the draft.

Keep the reference and both markers unchanged. If framing is missing, duplicated,
or changed (including line-ending conversion), Codex keeps the original composer,
reports an error, and retains the complete editor file at the reported path for
manual recovery. Copy only your reply from it. Failed or cancelled editor
processes also retain their file; a deleted file cannot be recovered. Quitting
without saving leaves the draft unchanged when the editor exits successfully.

The setting defaults to false, preserving the usual composer-only editor flow.
Successful temporary files are removed through the native flow; failed files
need manual removal. Editor history, backups, swap files, and sync services may
keep copies of assistant reference text. This feature does not delete those
copies or promise automatic deletion.

See [source and validation details](tests/external-editor-reference-validation.md).
