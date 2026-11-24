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

For a manual download, get the `codex-native-…-linux-x86_64.tar.gz` or `codex-native-…-linux-aarch64.tar.gz` archive from
[Releases](https://github.com/xunzhou/codex-nix/releases).

## Patches

<details>
<summary>Live terminal colors — refresh without restarting</summary>

**Refresh**

Change your terminal theme, then run:

```sh
kill -USR1 "$(pgrep -n codex)"
```

This refreshes the most recent Codex process.

**Notes**

- Colors and automatic syntax highlighting follow the new background.
- An explicit `tui.theme` stays selected.
- Older terminal scrollback keeps its original colors.

</details>

<details>
<summary>Double Escape — interrupt a running turn</summary>

**Shortcut**

Press **Escape twice within 400 ms** to stop a turn.

**Notes**

- Menus and Vim mode keep their usual Escape behavior.
- Ctrl+C works as before.

</details>

<details>
<summary>/add-dir — grant write access to another directory</summary>

**Use it**

1. Between turns, enter `/add-dir <path>`.
2. Check the resolved directory and confirm.
3. Use the directory on the next turn.

**Scope**

- Relative paths start from the session's working directory.
- Grants last for the current runtime; grant again after a cold resume.
- Already-running processes keep their original permissions.

**Requirements**

- A local session with workspace write access.
- Managed policy and protected paths still apply.

</details>

<details>
<summary>Ctrl+G — include the last assistant response in your editor</summary>

**Enable once**

Add to `config.toml`:

```toml
[tui]
external_editor_include_reference = true
```

**Use it**

1. Press **Ctrl+G** to open your configured `VISUAL` or `EDITOR`.
2. Edit only between `BEGIN REPLY` and `END REPLY`.
3. Save and close to return your reply to the composer. It is not submitted.

**Notes**

- Keep the reference and reply markers unchanged.
- Failed edits preserve the original draft and retain the editor file for recovery.
- Editor backups or sync may retain copies of the reference text.

</details>

<details>
<summary>Alt+M and Alt+Shift+M — cycle models</summary>

Between turns, press **Alt+M** to select the next available model for the
current session. Press **Alt+Shift+M** for the previous model. Both shortcuts wrap around. To change the shortcut,
set the binding in your Codex configuration.

```toml
[tui.keymap.chat]
next_model = "alt-m"
previous_model = "alt-shift-m"
```

</details>

<details>
<summary>Alt+P and Alt+Shift+P — cycle permission modes</summary>

Press **Alt+P** to cycle through Read Only, Ask for approval, Approve for me,
and Full Access. **Alt+Shift+P** cycles in reverse. Unavailable modes are
skipped. Selecting Full Access opens the usual confirmation before it applies.

```toml
[tui.keymap.chat]
next_permission_mode = "alt-p"
previous_permission_mode = "alt-shift-p"
```

</details>

## Update checks

Update Codex probes stable releases every 15 minutes; published versions skip all
builds. The serialized queue preserves packaging pushes: while a build runs,
probes wait, then skip published or exhausted work before installing build tools.
Each stable version/main revision gets at most two automatic attempts, with a
30-minute cooldown after a completed failed/cancelled attempt. A repaired main
gets a fresh budget. Active runs are never displaced. Missing run records or
unknown status fail closed; inspect Actions before intervening.

For further recovery, run **Update Codex** on main with an explicit stable
`version`, anonymous `request_id`, and `retry` selected. Retry still cannot bypass
an active attempt or rebuild an already published version. Immutable attempt tags
record the owning Actions run; do not move or delete them. An authorized event
sender may use this same dispatch entry point; no sender is installed here.

**Alpha compatibility** checks recent published alphas every six hours (or a
selected tag manually). It replays patches in temporary directories and runs the
checksum-verified upstream host's isolated V8 smoke. It cannot publish, and does
not replace patched compilation or the stable release checks.
