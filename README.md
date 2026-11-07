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

## Update recovery

Update preparation validates source, patches, and Cargo dependencies in a temporary
Git checkout. Preparation failures leave the caller's manifest, patches, and index
unchanged. Only validated candidates are copied back. Run updates in a dedicated
worktree without concurrent edits.

To retain an unresolved patch replay locally:

```sh
CODEX_FAILURE_DIR=/tmp/codex-update-failures bash scripts/update.sh
```

CI uploads an `update-failure` artifact when patch replay cannot resolve a conflict
(retained for 14 days). Each packet contains the conflicted source tree including
its Git index, source identity, failing patch and commit, and replay instructions.
Inspect `tree` with `git status` and `git diff --cc`; use a copy for destructive
replay commands. This packet covers unresolved patch conflicts, not every build
or network failure.

## Build validation

Shipped binaries use Cargo's optimized `release` profile. Patch regression tests
use upstream's `ci-test` profile to avoid release optimization and thin LTO for
test executables. The test selectors are unchanged; release binaries retain
version/help smoke checks and patch-marker checks during installation.

The profile preflight checks the rendered Nix settings and exercises the nextest
selectors on a small fixture. The full build runs the selected regressions from
the patched source:

```sh
nix build -L .#codex-static
```

On the local Codex 0.166.0 comparison, test compilation fell from 117m 30s with
`release` to 25m 04s with `ci-test` (about 79% less time). Both runs passed the
same 155 regression tests, compared by exact test name. Test execution took
2.4s and 3.8s respectively. These are single-run measurements, not CI timing
guarantees; no reliable peak-memory comparison was collected.
