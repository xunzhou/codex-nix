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

## Shared build definition

`build.json` is the build definition for both backends. It records each supported
release's source URL, archive SHA-256, Nix source hash, ordered patches, and Cargo
vendor hash. Shared profiles pin native inputs such as V8 and assert the matching
crate versions. The binary list, Cargo packages, marker checks, and smoke commands
are shared too. `package.nix` reads this definition directly; it contains no
release-specific source, patch, or V8 pins.

The shared default is 0.154.0; the manifest also retains 0.153.4. To evaluate
another declared release, override `releaseVersion` on the Nix package.

`scripts/update.sh VERSION` validates an upstream stable release and invokes
`scripts/update-manifest.py`. It verifies source version, patch application, and
native dependency compatibility, resolves and verifies Nix hashes, and finally
builds the Nix package. An upstream patch conflict or changed V8 dependency stops
the update for review. A failed hash update restores the original manifest.

## Linux npm installation (without Nix)

GitHub Actions builds ordinary x86_64 GNU/Linux bundles containing `codex` and
`codex-code-mode-host`. They are built on Ubuntu 24.04, tested for missing shared
libraries and Nix store references, and published under immutable recipe-specific
`native-...` release tags. They require a compatible glibc Linux system and system
libraries; they are not static binaries. No Nix installation is needed.

Install the downloader/builder and replace the old palette-only updater step:

```sh
python3 scripts/setup-source-patcher.py
```

This installs `bin/install-patched-codex`, the shared manifest, and patches under
`.local/share/codex-patcher` in your home directory. It saves the original updater
as `bin/update-agents.before-source-patcher`. Setup does not run the updater or
restart services. Then run `install-patched-codex`, or let `update-agents` invoke
it after the npm update.

The installer downloads the bundle matching the installed npm version and exact
recipe, validates the archive layout, recipe identity, binary SHA-256 digests,
version, and smoke commands, then stages and installs both binaries with rollback.
It reuses verified cached bundles after npm overwrites the vendor binaries.

If a release asset is absent (HTTP 404), it falls back to a source build. Set
`CODEX_INSTALL_MODE=download` to require a prebuilt bundle, or use `--source` to
build locally. Network errors and invalid bundles fail rather than silently
falling back. The installed manifest must be refreshed with the setup command
when support for new versions or patches is added.

Downloads need Python 3.12+, curl, and grep. Source builds additionally need Git,
Cargo/Rust, strip, and the upstream Linux build dependencies. Source archives and
V8 assets are verified against the manifest before Cargo runs. Build intermediates
remain cached between attempts. `RUSTY_V8_ARCHIVE` and
`RUSTY_V8_SRC_BINDING_PATH` overrides are supported for custom toolchains.

Overrides include `CODEX_PACKAGE_ROOT`, `CODEX_MANAGED_ENTRYPOINT`,
`CODEX_BUILD_MANIFEST`, `CODEX_PATCH_ROOT`, `CODEX_PATCH_CACHE`, and
`CODEX_SOURCE_ARCHIVE`. `CODEX_PRIVILEGE_COMMAND` defaults to `sudo`; use an empty
string for a user-owned npm installation.

## Native build workflow

`.github/workflows/native.yml` runs for relevant changes on main and can be
started manually. It builds all declared releases, or a selected version, with
no npm installation or Nix dependency:

```sh
python3 scripts/install-source-codex.py --build-only --version 0.154.0 --output dist/codex-linux-x86_64.tar.gz
```

The workflow uploads downloadable Actions artifacts and publishes GitHub release
assets only from main. Scheduled manifest updates also call the native workflow,
since pushes made with the Actions token do not trigger ordinary push workflows.
Existing recipe-specific release assets are never overwritten. Publication must
run successfully before a matching prebuilt download is available.

Run the local installer and manifest tests with:

```sh
python3 tests/source-patcher.py
python3 tests/manifest.py
bash tests/update.sh
```
