#!/usr/bin/env python3
"""Install the generic patcher and replace the palette-only update-agents step."""

import argparse
from pathlib import Path
import shlex
import shutil
import tempfile


OLD = '''step "Building/installing Codex live-palette patch"
"$HOME/.config/catppuccin/install-patched-codex"
ok "Codex live-palette patch installed"'''
NEW = '''step "Building/installing Codex source patches"
"$HOME/bin/install-patched-codex"
ok "Codex source patches installed"'''


def atomic_write(path, content, mode):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(content.encode())
    try:
        temporary.chmod(mode)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, default=Path.home(), help="home directory containing bin/update-agents")
    args = parser.parse_args()
    prefix = args.prefix.resolve()
    updater = prefix / "bin/update-agents"
    current = updater.read_text()
    if current.count(OLD) == 1:
        updated = current.replace(OLD, NEW)
    elif current.count(NEW) == 1 and OLD not in current:
        updated = current
    else:
        raise SystemExit("update-agents does not contain the expected Codex step; refusing to rewrite it")
    root = Path(__file__).resolve().parent.parent
    destination = prefix / ".local/share/codex-patcher"
    command = prefix / "bin/install-patched-codex"
    if command.exists() and "codex-patcher/scripts/install-source-codex.py" not in command.read_text():
        raise SystemExit(f"refusing to replace an unrelated command: {command}")
    (destination / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copytree(root / "patches", destination / "patches", dirs_exist_ok=True)
    installer = destination / "scripts/install-source-codex.py"
    atomic_write(destination / "build.json", (root / "build.json").read_text(), 0o644)
    atomic_write(installer, (root / "scripts/install-source-codex.py").read_text(), 0o755)
    atomic_write(command, f"#!/bin/sh\nexec python3 {shlex.quote(str(installer))} \"$@\"\n", 0o755)
    if updated != current:
        backup = updater.with_name("update-agents.before-source-patcher")
        if not backup.exists():
            shutil.copy2(updater, backup)
        atomic_write(updater, updated, updater.stat().st_mode & 0o777)
    print(f"Installed {command}; updated only the Codex patch step in {updater}")
    print("Run install-patched-codex now, or let update-agents invoke it after npm updates.")


if __name__ == "__main__":
    main()
