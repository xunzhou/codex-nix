# Codex with a few extras

Codex CLI with small improvements for everyday use:

- Refresh terminal colors without restarting.
- Press Escape twice to interrupt a turn.
- Use `/add-dir <path>` to grant another directory write access for the current session.
- Optionally include the last assistant response when editing your reply with Ctrl+G.

## Install

**npm:** from this checkout, run the installer. Requires Python 3.12+, curl, and npm.

```sh
python3 scripts/install-release.py --install-reviewed
```

**Nix:** import the prebuilt package, then install it.

```sh
nix run github:xunzhou/codex-nix#install
nix profile install github:xunzhou/codex-nix
```

For a manual download, get the `codex-native-…-linux-x86_64.tar.gz` archive from
[Releases](https://github.com/xunzhou/codex-nix/releases).

## Refresh terminal colors

After changing your terminal theme, refresh the most recent Codex process:

```sh
kill -USR1 "$(pgrep -n codex)"
```
