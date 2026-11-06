# Publication checks

Install in each checkout using the approved `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`,
`GIT_COMMITTER_NAME`, and `GIT_COMMITTER_EMAIL` environment values:

```sh
python3 scripts/publication_gate.py install
python3 scripts/publication_gate.py verify
python3 tests/publication-gate.py
```

Installation requires GitHub noreply addresses and records exact name/address pairs
in repository-local Git configuration. The existing Actions bot is also allowed.
It refuses to replace unrelated hooks. Linked worktrees share local configuration;
install only when all linked worktrees contain the hook. No global settings change.
Installation on another computer must be performed and verified there.

## Git and source artifacts

The pre-push hook checks the exact object IDs supplied by Git for every outgoing
ref. Only history currently advertised by the destination is excluded. Fetch all
its branches and tags first; missing baseline objects block publication. Local
remote-tracking refs alone are not trusted. Remote deletions after advertisement
do not make previously public content private again.

All new commits, both identities, complete messages and trailers, tree paths, and
every newly reachable blob version are checked. Patch headers are blob content.
Intermediate versions remain checked even if a later commit removes them. Existing
public blobs are excluded; inherited filenames and tree modes are still checked.
New branches and multiple refs use the same destination baseline. Deletions check
the ref name and transmit no objects. Lightweight commit tags are supported;
annotated tags, non-commit targets, other ref namespaces, submodules, shallow
repositories, replacement objects, and grafts are blocked. Symlink targets are
scanned as text. Invalid UTF-8, control characters, binary data and blobs over
8 MiB are blocked, including opaque archives and Git LFS pointers containing
blocked content. LFS object uploads themselves are not covered.

Each successful check emits a digest binding the ref updates, advertised baseline,
and checked commit IDs. Git sends those supplied immutable IDs even if a local
branch moves later. The scheduled updater checks before creating its public source
artifact, verifies the bundle's HEAD against the checked commit, and records the
bundle digest. The publish job checks again at push time. Build artifacts are not
privacy-scanned: existing archive inventory and checksum checks remain in place.

## Public text

Use files containing the exact intended title and body. The wrapper supports draft
PR creation, issue/PR comments, and release name/body edits:

```sh
python3 scripts/publication_gate.py publish pr --head topic \
  --expected-head COMMIT_ID --title /tmp/title --body /tmp/body
python3 scripts/publication_gate.py publish comment --number 123 --body /tmp/body
python3 scripts/publication_gate.py publish release --number 456 \
  --title /tmp/title --body /tmp/body
```

These commands target this repository only. `gh` must already be authenticated.
PR creation verifies the advertised head against the supplied commit ID and emits
a combined head/text receipt. GitHub resolves branch heads server-side; concurrent
remote writers can still move that branch before or after PR creation.

The wrapper reads each file once, validates the decoded strings, serializes once,
and sends those same in-memory bytes. Receipts contain hashes, never text. Existing
release publishers validate the payload immediately before transmission without
changing release tags, draft promotion, Latest selection, or permissions.

Titles are limited to 120 UTF-8 bytes; bodies and commit messages to 2,000 bytes.
Checks reject non-noreply email addresses, selected credential formats, transcript
markers and selected internal-process phrases. Additional private regular
expressions can be stored in `publication.denyPattern` in local Git configuration;
configure them through a private channel, never in repository files or command
logs. Invalid rules block publication. Errors identify categories and object IDs
or numbered locations, without reproducing matched text or unsafe filenames.

Keep public summaries to the technical problem, change, validation, and material
limitations. Length and pattern checks cannot determine whether every sentence is
necessary or detect all personal details, credentials, encoded content or paraphrased
instructions. Review the exact public text as well as the automated result.

## Enforcement boundary

Hooks and wrappers are editable by anyone controlling the checkout. Direct API,
connector and UI writes, `git push --no-verify`, changed hook configuration, custom
transports, separate clones, workflow edits and build logs can bypass them. A digest
receipt is an audit record, not a signed authorization. It does not stop a process
with write access from replacing an artifact between checking and uploading it.
Post-push CI cannot prevent the initial disclosure. Existing public history is not
cleaned or certified by this gate.

No independent publisher is configured. Stronger enforcement requires a separately
controlled publisher with the only write credential, immutable gate policy, and
content-addressed inputs. Coding environments would need their direct Git/API write
access removed, and alternate publication routes restricted. That changes access
and security policy and requires separate approval. Repository checks alone cannot
provide that boundary.
