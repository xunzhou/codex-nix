## Automated native Linux releases

The **Update native Codex** workflow checks stable upstream releases every four
hours and on relevant pushes. Native builds are independent of the Nix closure:
`native-build.json` tracks the native channel and `build.json` tracks Nix.
Both use the same patch port directories and manifest updater. The native channel
starts with the tested 0.156.1 port.

When patches apply, the workflow builds both executables and runs the focused TUI
patch tests before publishing an immutable `native-VERSION-RECIPE_HASH` release.
If context has moved, the updater reconstructs the old patch stack from its
checksum-verified source and attempts Git's three-way merge. It publishes no
conflict resolution guesses. A real conflict, changed V8 dependency, or failing
test stops the release; the previous download remains available. The failed run's
summary and artifacts contain the candidate recipe and patches. Review and commit
a port under `patches/VERSION/`, then rerun. Unchanged successful recipes skip builds;
failed or missing releases retry at the next scheduled check.

Routine installation (Python 3.12+, Linux x86_64, compatible Ubuntu 24.04 libraries):

```sh
python3 scripts/install-release.py --install-reviewed
```

This downloads and verifies the latest **published patched** release before
installing its exact npm version, then installs the verified binary pair. It never
falls back to local compilation. `--reviewed-version` prefetches and verifies the
release, printing just its version for `update-agents`. With no arguments it
patches the installed npm version using its matching published release. Override
`CODEX_RELEASE_CACHE` to select the download directory. Recipe and binary archives
are checked against GitHub asset digests and the release manifest; archive contents,
recipe identity, binary checksums, version, and patch marker are checked too.

The source installer remains available for deliberate local development. A stable
upstream release with semantic patch conflicts still needs a reviewed port; CI
cannot guarantee that arbitrary future code changes preserve patch behavior.

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

`build.json` is the build definition for both backends. It records the current
release's source URL, archive SHA-256, Nix source hash, ordered patches, and Cargo
vendor hash. Shared profiles pin native inputs such as V8 and assert the matching
crate versions. The binary list, Cargo packages, marker checks, and smoke commands
are shared too. `package.nix` reads this definition directly; it contains no
release-specific source, patch, or V8 pins.

The manifest supports the current version only (0.154.0). Patches use stable
filenames and are maintained in place for that version; older versions remain
available in Git history. Successful updates replace the previous release entry.

`scripts/update.sh VERSION` validates an upstream stable release and invokes
`scripts/update-manifest.py`. It verifies source version, patch application, and
native dependency compatibility, resolves and verifies Nix hashes, and finally
builds the Nix package. An upstream patch conflict or changed V8 dependency stops
the update for review. A failed hash update restores the original manifest.

## Linux npm installation (without Nix)

GitHub Actions builds ordinary x86_64 GNU/Linux bundles containing `codex` and
`codex-code-mode-host`. They are built on Ubuntu 24.04, tested for missing shared
libraries and Nix store references, and published beside the Nix closure in a
combined `bundle-codex-v...` release. Native asset names include the exact recipe
hash. They require a compatible glibc Linux system and system
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

## Combined build and release workflow

`.github/workflows/update.yml` handles relevant main-branch pushes, daily upstream
checks, and manual runs. It prepares one candidate commit and saves it as a Git
bundle, then builds Nix and native Linux in parallel from that exact commit.
A scheduled update is pushed only after both builds and their tests succeed;
a concurrent change to main makes the guarded push fail without overwriting it.
Pull requests retain the separate validation workflow.

One publication job collects both Actions artifacts and creates a draft release.
It validates both inventories and checksums before publishing the draft. The
release includes the Nix closure, its manifest and checksums, and
`codex-native-<version>-<recipe-hash>-linux-x86_64.tar.gz`. Existing assets are
verified and never overwritten. Interrupted drafts can be resumed with the same
run's artifacts. A changed native recipe can add a new asset to the same Nix
release, without replacing earlier native assets.

Older `codex-v...` and `native-...` releases remain untouched. The Nix installer
tries the combined tag before falling back to its legacy tag on HTTP 404. The
native installer retains its exact legacy download lookup and then searches the
paginated release list for an asset with the exact recipe hash. API errors and
invalid bundles stop installation; only a missing recipe permits a source build.
Refresh the installed patcher with the setup command to use combined releases.

To build just the native bundle locally:

```sh
python3 scripts/install-source-codex.py --build-only --version 0.154.0 --output dist/codex-linux-x86_64.tar.gz
```

Run the local installer and manifest tests with:

```sh
python3 tests/source-patcher.py
python3 tests/manifest.py
python3 tests/release.py
bash tests/update.sh
```
