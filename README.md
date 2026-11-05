# Patched Codex

Codex with two small changes:

- **Live terminal colors:** refresh the palette and adaptive syntax highlighting without restarting Codex.
- **Double Escape to interrupt:** press Escape twice within 400 ms to stop a turn.
  Menus, Vim mode, and Ctrl-C keep their usual behavior.

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

## Static Nix experiment

The experimental `codex-static` target builds the binary pair with Nix's musl
toolchain, static OpenSSL, and static libcap. It uses eight compiler jobs.
Export that output directly into the existing native release format:

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

Verified locally with Codex 0.163.0: both binaries ran without Nix, and V8
returned `42`. The clean compile took 42 minutes 49 seconds with eight jobs.
This run needed dependency sources moved out of an almost-full RAM-backed
`/tmp` after compilation; allow sufficient temporary storage for fresh builds.

The default Nix package and scheduled release builds still use the existing
pipeline. This target tests whether one static compilation can supply both
release formats before changing publication.

## Automatic updates

[GitHub Actions](https://github.com/xunzhou/codex-nix/actions/workflows/update.yml)
checks upstream once a day, builds the Nix closure and static native binaries
from the same patched source, tests them, and publishes one release with both.

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
