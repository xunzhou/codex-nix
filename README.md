# codex-nix

This flake builds the Codex CLI from pinned upstream source and applies a small
patch that refreshes the terminal palette while Codex is running. It currently
supports `x86_64-linux`.

## Usage

Run the patched CLI directly:

```bash
nix run github:xunzhou/codex-nix -- --version
```

Install it in a Nix profile:

```bash
nix profile install github:xunzhou/codex-nix
```

To import the verified release closure matching the flake source, run:

```bash
nix run github:xunzhou/codex-nix#install
```

The installer validates the release inventory, checksums, manifest identity,
imported Nix store output, CLI version, palette marker, and companion binary.
Release closures are verified against the exact source-derived output before
they are accepted.

After changing terminal colors, ask a running Codex process to refresh them:

```bash
kill -USR1 "$(pgrep -n codex)"
```

GitHub Actions dependencies are pinned to full commits. Their version comments
are updated intentionally through review rather than following floating refs.

## Interrupt protection

While a turn is running, press Escape twice within 400 ms to interrupt.
A single Escape still dismisses menus and leaves Vim insert mode normally.
Other keys and turn boundaries reset the pending tap; reported key repeats do
not count as a second press. Ctrl-C and remapped interrupt keys keep their
existing behavior. Terminals that report autorepeat as ordinary presses cannot
distinguish a held Escape from repeated taps.

The source patch targets the pinned Codex version and is applied after the
palette patch. Run `cargo test -p codex-tui --lib` in the patched source tree
to validate input handling and rendered hints when updating Codex.

## Linux npm installation (without Nix)

The generic source patcher supports npm-managed Codex 0.153.4 and 0.154.0.
It selects an explicit patch series for the installed version, applies the
palette and double-Escape patches, and builds both `codex` and
`codex-code-mode-host`. Nix is not required. Python 3.12+, Git, Cargo/Rust,
the upstream Linux build dependencies, curl, grep, and strip are required.

Install the patcher and replace the old palette-only step in `bin/update-agents`:

```sh
python3 scripts/setup-source-patcher.py
```

This installs `bin/install-patched-codex` and its files under
`.local/share/codex-patcher` in your home directory. It saves the original
updater as `bin/update-agents.before-source-patcher`. Setup does not run the
updater or restart services. Then run `install-patched-codex`, or let
`update-agents` call it after the npm update.

Cache identity includes the complete ordered patch set, installer, version,
and platform. Both cached binaries have recorded SHA-256 digests; npm replacing
either binary triggers restoration, and changing a patch forces a build.
Release build intermediates remain in the cache for subsequent builds.
Installation stages both binaries, replaces the CLI last, and rolls back on
failure. An unknown release stops with a missing-patch-series error; add and
verify `patches/series/VERSION.json` before supporting another release.

The main overrides are `CODEX_PACKAGE_ROOT`, `CODEX_MANAGED_ENTRYPOINT`,
`CODEX_PATCH_ROOT`, `CODEX_PATCH_CACHE`, and `CODEX_SOURCE_ARCHIVE`.
`CODEX_PRIVILEGE_COMMAND` defaults to `sudo`; set it to an empty string for a
user-owned npm installation. The x86_64 GNU/Linux patch series also pins the
V8 archive and generated Rust bindings from OpenAI's Codex release. The patcher
verifies their SHA-256 digests and caches them before invoking Cargo, avoiding
the missing default rusty_v8 release assets. Explicit `RUSTY_V8_ARCHIVE` and
`RUSTY_V8_SRC_BINDING_PATH` overrides remain supported. Run the installer tests with:

```sh
python3 tests/source-patcher.py
```
