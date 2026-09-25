#!/usr/bin/env python3
"""Point an existing user launcher at this checkout without changing system binaries."""
from pathlib import Path
import shlex
import shutil
import sys
import tempfile

root = Path(__file__).resolve().parent.parent
destination = Path(sys.argv[1]).absolute()
launcher = root / "scripts/install-local-codex.sh"
content = f"#!/bin/sh\nexec {shlex.quote(str(launcher))} \"$@\"\n"
if destination.exists():
    current = destination.read_text()
    if "codex-patcher" not in current and str(launcher) not in current:
        sys.exit(f"Refusing to replace an unrelated command: {destination}")
    backup = destination.with_name(destination.name + ".before-0.156.1")
    if not backup.exists():
        shutil.copy2(destination, backup)
with tempfile.NamedTemporaryFile(mode="w", dir=destination.parent, delete=False) as stream:
    temporary = Path(stream.name)
    stream.write(content)
try:
    temporary.chmod(0o755)
    temporary.replace(destination)
finally:
    temporary.unlink(missing_ok=True)
print(f"Activated {destination} → {launcher}")
