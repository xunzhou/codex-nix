"""Prepare a disposable Cargo workspace and remove its placeholder build outputs."""

import argparse
import json
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("stub")
    clean = commands.add_parser("clean")
    clean.add_argument("--target", required=True)
    clean.add_argument("--profile", required=True)
    args = parser.parse_args()

    metadata = json.loads(subprocess.check_output([
        "cargo", "metadata", "--offline", "--format-version", "1",
    ]))
    local = [package for package in metadata["packages"] if package["source"] is None]

    if args.command == "stub":
        # Keep Cargo's real graph, features, target kinds, and build dependencies.
        # Only workspace/path crate code is disposable in this build.
        for package in local:
            for target in package["targets"]:
                body = "fn main() {}\n" if set(target["kind"]) & {
                    "bin", "custom-build", "example", "bench", "test",
                } else "\n"
                Path(target["src_path"]).write_text(body)
    else:
        # Clean host proc macros/build scripts as well as cross-compiled code.
        # Cargo understands artifact names, including renamed libs/binaries.
        names = sorted({package["name"] for package in local})
        packages = [arg for name in names for arg in ("-p", name)]
        for target_args in ([], ["--target", args.target]):
            subprocess.run([
                "cargo", "clean", "--offline", "--profile", args.profile,
                *target_args, *packages,
            ], check=True)


if __name__ == "__main__":
    main()
