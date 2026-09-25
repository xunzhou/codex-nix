# Patched Codex

Codex with two small changes:

- **Live terminal colors:** refresh the palette without restarting Codex.
- **Double Escape to interrupt:** press Escape twice within 400 ms to stop a turn.
  Menus, Vim mode, and Ctrl-C keep their usual behavior.

## Download or install

Find built binaries on the [releases page](https://github.com/xunzhou/codex-nix/releases).
For Linux x86_64, choose a **native** release and download `codex-linux-x86_64.tar.gz`.
It includes `codex` and `codex-code-mode-host`, built on Ubuntu 24.04.

To update an npm installation, run from this checkout:

```sh
python3 scripts/install-release.py --install-reviewed
```

Requires Python 3.12+, curl, npm, and compatible Linux system libraries.
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

[GitHub Actions](https://github.com/xunzhou/codex-nix/actions/workflows/native.yml)
checks upstream every four hours, builds the patched binaries, tests them, and
publishes a release. Native releases run independently of Nix builds.

Simple patch drift is rebased automatically. Conflicts or failing tests stop the
update and leave the last working release available. Check the failed run's
summary and artifacts, commit a reviewed patch port, then rerun the workflow.
