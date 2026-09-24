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

The launcher uses this checkout's manifest, patches, and `.native-cache`.
Keep the checkout in place. `--reviewed-version` lets an updater pin its npm
installation to the version this patch set supports. The installation step
needs permission to update the global npm package; building and verification
can run entirely inside a writable workspace.
