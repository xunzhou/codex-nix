# Patched Codex

Codex with two small changes:

- **Live terminal colors:** refresh the palette without restarting Codex.
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

## Automatic updates

[GitHub Actions](https://github.com/xunzhou/codex-nix/actions/workflows/update.yml)
checks upstream once a day, builds the Nix closure and static native binaries
from the same patched source, tests them, and publishes one release with both.

Simple patch drift is rebased automatically. Conflicts or failing tests stop the
update and leave the last working release available. Check the failed run's
summary and artifacts, commit a reviewed patch port, then rerun the workflow.
