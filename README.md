# codex-nix

This flake builds the Codex CLI from pinned upstream source and applies a small
patch that refreshes the terminal palette while Codex is running. It currently
supports `x86_64-linux`.

## Usage

Run the patched CLI directly:

```bash
nix run github:xunzhou/codex-nix -- --version
```

Install it in a Nix profile:

```bash
nix profile install github:xunzhou/codex-nix
```

To import the verified release closure matching the flake source, run:

```bash
nix run github:xunzhou/codex-nix#install
```

The installer validates the release inventory, checksums, manifest identity,
imported Nix store output, CLI version, palette marker, and companion binary.
Release closures are verified against the exact source-derived output before
they are accepted.

After changing terminal colors, ask a running Codex process to refresh them:

```bash
kill -USR1 "$(pgrep -n codex)"
```

GitHub Actions dependencies are pinned to full commits. Their version comments
are updated intentionally through review rather than following floating refs.
