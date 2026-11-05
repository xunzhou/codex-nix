#!/usr/bin/env python3
"""Export an already-built static Nix package in the native release format."""

import argparse
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parent.parent
module = importlib.util.spec_from_file_location("native", ROOT / "scripts/install-source-codex.py")
native = importlib.util.module_from_spec(module)
module.loader.exec_module(native)


def check_static(binary, target):
    if native.elf_target(binary) != target:
        raise ValueError(f"{binary.name} does not match release target {target}")
    # Musl can also be dynamically linked. Check the actual ELF dependencies.
    headers = subprocess.check_output(["readelf", "-lW", str(binary)], text=True)
    dynamic = subprocess.check_output(["readelf", "-dW", str(binary)], text=True)
    if re.search(r"\bINTERP\b", headers) or "(NEEDED)" in dynamic:
        raise ValueError(f"{binary.name} is dynamically linked")


def export(package, output, version):
    spec = native.load_recipe(ROOT / "build.json", version)
    patches = [ROOT / "patches" / name for name in spec["patches"]]
    key = native.recipe_key(spec, patches, version)
    with tempfile.TemporaryDirectory(prefix="codex-export-") as temporary:
        bundle = Path(temporary)
        for name in spec["binaries"]:
            source = package / "bin" / name
            check_static(source, spec["release_target"])
            # Preserve the Nix output's exact bytes: no stripping or relinking.
            shutil.copyfile(source, bundle / name)
            (bundle / name).chmod(0o755)
        checksums = {name: native.digest(bundle / name) for name in spec["binaries"]}
        (bundle / "sha256.json").write_text(json.dumps(checksums))
        (bundle / "bundle.json").write_text(json.dumps({"key": key, "version": version}))
        if not native.valid_bundle(bundle, version, spec["markers"], spec["binaries"]):
            raise ValueError("exported binary pair failed bundle verification")
        actual = subprocess.check_output([str(bundle / "codex"), "--version"], text=True).strip()
        if actual != f"codex-cli {version}":
            raise ValueError(f"unexpected Codex version: {actual}")
        native.smoke_bundle(bundle, spec)
        output.mkdir(parents=True, exist_ok=True)
        asset = output / f"codex-native-{key}-linux-x86_64.tar.gz"
        with tempfile.TemporaryDirectory(dir=output) as staging:
            pending = Path(staging) / asset.name
            with tarfile.open(pending, "w:gz", compresslevel=6) as archive:
                for name in [*spec["binaries"], "sha256.json", "bundle.json"]:
                    archive.add(bundle / name, arcname=name)
            pending.replace(asset)
        return asset


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True, help="Nix output directory")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--version", default=json.loads((ROOT / "build.json").read_text())["default_version"])
    args = parser.parse_args()
    print(export(args.package.resolve(), args.output_dir, args.version))


if __name__ == "__main__":
    main()
