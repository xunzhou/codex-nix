#!/usr/bin/env python3
"""Verify the native port against its checksum-pinned upstream source archive."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import tomllib

root = Path(__file__).resolve().parent.parent
manifest = json.loads((root / "native-build.json").read_text())
version = manifest["default_version"]
release = manifest["releases"][version]
archive = Path(sys.argv[1])
with archive.open("rb") as stream:
    assert hashlib.file_digest(stream, "sha256").hexdigest() == release["source"]["sha256"]
with tempfile.TemporaryDirectory(prefix="codex-native-port-") as temporary:
    scratch = Path(temporary)
    with tarfile.open(archive) as tar:
        tar.extractall(scratch, filter="data")
    source, = scratch.iterdir()
    workspace = tomllib.loads((source / "codex-rs/Cargo.toml").read_text())
    assert workspace["workspace"]["package"]["version"] == version
    locked = tomllib.loads((source / "codex-rs/Cargo.lock").read_text())["package"]
    profile = manifest["profiles"][release["profile"]]
    for name, expected in profile["crate_versions"].items():
        assert {p["version"] for p in locked if p["name"] == name} == {expected}
    for name in release["patches"]:
        patch = root / "patches" / name
        subprocess.run(["git", "-C", str(source), "apply", "--check", str(patch)], check=True)
        subprocess.run(["git", "-C", str(source), "apply", str(patch)], check=True)
print(f"PASS: Codex {version} archive, dependency profile, and ordered patches")
