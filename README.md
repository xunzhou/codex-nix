# codex-nix

Codex CLI patches and prebuilt binaries.

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

## Patches

<details>
<summary>Live terminal colors — refresh without restarting</summary>

After changing your terminal theme, refresh the most recent Codex process:

```sh
kill -USR1 "$(pgrep -n codex)"
```

Colors and automatic syntax highlighting follow the new background. An explicit
`tui.theme` stays selected; older terminal scrollback keeps its original colors.

</details>

<details>
<summary>Double Escape — interrupt a running turn</summary>

Press Escape twice within 400 ms to stop a turn. Menus, Vim mode, and Ctrl+C
keep their usual behavior.

</details>

<details>
<summary>/add-dir — grant write access to another directory</summary>

Between turns, enter `/add-dir <path>` and confirm the resolved directory.
Relative paths use the session's working directory. Access applies from the next
turn and lasts for the current runtime; grant it again after a cold resume.
Existing processes retain their original permissions. Managed policy and
protected paths still apply. Requires a local session with workspace write access.

</details>

<details>
<summary>Ctrl+G — include the last assistant response in your editor</summary>

Enable the optional reference in `config.toml`:

```toml
[tui]
external_editor_include_reference = true
```

Ctrl+G uses your existing `VISUAL` or `EDITOR`. Edit between `BEGIN REPLY` and
`END REPLY`, keeping the reference and markers unchanged. Saving and closing
returns your reply to the composer without submitting it. Failed edits keep the
original draft and retain the editor file for recovery. Editor backups or sync
may retain copies of the reference text.

</details>
