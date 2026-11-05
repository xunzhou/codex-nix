# Runtime `/add-dir` validation — Codex 0.166.0

Worktree: `.worktrees/14-runtime-add-dir-latest`, branch
`agent/14-runtime-add-dir-latest`. This follow-up fixes the first implementation
in commit `2fb738e`.

## Failure and fix

The manual trial correctly found a bug: a fresh write to `../extra/probe` still
failed with `Read-only file system` after the directory-granted acknowledgement.
The next recorded turn omitted the extra root. Local TUI turns send their cached
workspace roots to the app-server; the command updated the server but left those
client roots unchanged, so the next submission removed the grant.

After an explicit successful server acknowledgement, the TUI now adopts the root
in its app configuration, chat-widget permission roots, primary-session cache,
and buffered thread-session cache. Subsequent turns and thread switches retain
the grant. Duplicate acknowledgements remain idempotent. A failed grant does not
update those caches. The permission/policy enforcement and persistence machinery
remain unchanged.

## Automated validation

Focused tests run through `tests/runtime-add-dir.sh` against the patched upstream
0.166.0 source. The additional regressions cover:

- The actual confirmation event, server acknowledgement, next-turn permission
  roots, cached session roots, duplicate grant, and a rejected protected root.
- Real sandboxed `exec_command` writes with client-owned next-turn settings:
  a write outside the workspace fails before the grant, succeeds afterward,
  while `.git` and an unrelated directory remain unwritable. These tests exclude
  automatic `/tmp` write access and use approval policy `never`.
- Refreshed model environment after the write grant.
- Cold-resume expiry after a turn that supplies updated client roots; warm
  reconnect retains access, and `config.toml` remains byte-identical.
- Existing managed read-only policy rejection, protected metadata restrictions,
  invalid/noncanonical paths, confirmation, cancellation, and duplicates.

The OS probe runs on Unix. It was executed on Linux in this workspace. It uses
fresh command processes; it does not relax or disable the sandbox.

Final checks passed:

- Focused suite: 7 core, 3 TUI, and 3 app-server tests (13 total).
- Actual rendered Nix nextest selection, executed with the host dev profile:
  143 passed, including the new TUI acknowledgement regression.
- Source-installer Python suite: 14 passed.
- Full manifest patch application and package-policy checks passed.
- `nix flake check --no-build` passed.
- Targeted Rust formatting and `git diff --check` passed.
- Rebuilt native debug CLI successfully; `codex --version` reports 0.166.0.

Build/test logs are under this worktree's ignored `.scratch/fix-*.log`. These
results include actual Linux sandbox enforcement; the nextest run is a host dev
build, not the static release build.

## Manual retry using the rebuilt candidate

The existing CLI process still contains the earlier code. Exit it, then launch
this worktree's rebuilt candidate and select the previous manual-test conversation
from the resume picker:

```sh
cd /data/proj/codex-nix/.worktrees/14-runtime-add-dir-latest
.scratch/target/debug/codex --no-daemon \
  -C "$PWD/.scratch/manual/main" \
  --sandbox workspace-write --ask-for-approval never resume
```

This relaunch loads the bugfix and preserves conversation history. It also
expires the old runtime's grant, by design. In the resumed session:

1. Ask for a fresh `exec_command` running
   `printf 'probe\n' > ../extra/probe`. Expect denial before granting access.
2. Enter `/add-dir ../extra`, verify the displayed canonical path, and select
   **Grant write access to this directory**.
3. In the next turn, ask for a new `exec_command` running the same write. Expect
   exit code 0; verify `../extra/probe` contains `probe`.
4. Check that a write in `../extra/.git` is still denied. Create that directory
   outside the agent sandbox first if necessary. An ungranted sibling directory
   must also remain unwritable.

Do not reuse an existing shell, REPL, or background process for step 3: processes
already running at grant time retain their original sandbox permissions.

Grants remain runtime-scoped: no global configuration changes, warm reconnect
retains them, cold resume or runtime replacement expires them. Remote workspaces
and attachment-owned execution environments remain unsupported. The production
release/Nix build and a human interactive retry are separate from these local
checks; no installation, merge, or release is performed by this fix.
