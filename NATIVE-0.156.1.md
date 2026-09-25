# Native Codex 0.156.1 port

`native-build.json` pins the official 0.156.1 source archive and the existing
V8 150.4.0 inputs. The ordered patches live under `patches/0.156.1/`.
The older Nix recipe in `build.json` is unchanged; this native port does not
claim a verified Nix build or Cargo vendor hash.

The palette patch preserves the upstream terminal size monitor while pausing
input for a SIGUSR1 color probe. The double-Escape patch preserves startup
recovery routing and renders the two-key hint through upstream's new span API.
Escape must be pressed twice within 400 ms to interrupt; remapped keys retain
their existing behavior.

Verify the archive and patch application:

```sh
python3 tests/native-port.py /path/to/codex-0.156.1.tar.gz
python3 tests/source-patcher.py
bash tests/local-installer.sh
```

Build the native bundle with the existing checksum and smoke verification:

```sh
scripts/install-local-codex.sh --build-only --version 0.156.1
```

Install the matching npm package and then the patched binary pair:

```sh
scripts/install-local-codex.sh --install-reviewed
```

To point an existing `~/bin/install-patched-codex` at this checkout, preserving
a backup of the old launcher:

```sh
python3 scripts/activate-local-launcher.py ~/bin/install-patched-codex
```

The launcher uses this checkout's manifest, patches, and `.native-cache`.
Keep the checkout in place. `--reviewed-version` lets an updater pin its npm
installation to the version this patch set supports. The installation step
needs permission to update the global npm package; building and verification
can run entirely inside a writable workspace.

## Verification on 2026-09-24

- Both native binaries built in release mode with Rust 1.95.0.
- The official source archive checksum and ordered patch application passed.
- 141 focused TUI tests passed, covering Escape timing, repeat/release events,
  pending steers, goals, owned transcript input, palette refresh, and affected
  status layouts. The port includes 51 reviewed snapshot updates.
- The broader TUI run recorded 5,353 passed, 50 failed, and 4 skipped before
  the last hook-layout snapshot update. That snapshot was corrected and its
  focused test passed. The remaining failures involve untouched release-version
  and key-hint snapshot expectations, plus a test expecting the debug-only
  `/rollout` command in a release build. An unpatched baseline was not rebuilt;
  this is not a claim that the full upstream suite is green.
- Twelve source-installer tests, three manifest tests, and launcher checks passed.
- The real npm 0.156.1 package was installed into a temporary prefix. Both native
  binaries were replaced, their checksums verified, and a second installation
  correctly reported that the complete patch set already matched.
- The patched npm entry point reports `codex-cli 0.156.1`; both binaries pass
  their smoke commands and have no unresolved shared libraries.
- Rust formatting checks passed. The repository-wide `just fmt` was attempted,
  but unrelated Bazel/Python formatters could not complete in this environment.

The user launcher now points at this checkout and reports reviewed version
0.156.1. System-wide npm files were not modified by this porting session.
Run `~/bin/install-patched-codex --install-reviewed` to install the matching
system package and reuse the verified local bundle.
