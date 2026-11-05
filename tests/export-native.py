#!/usr/bin/env python3
"""Verify portable export rejects dynamic linking and preserves binary bytes."""

import importlib.util
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
module = importlib.util.spec_from_file_location("exporter", ROOT / "scripts/export-native.py")
exporter = importlib.util.module_from_spec(module)
module.loader.exec_module(exporter)
TARGET = "x86_64-unknown-linux-musl"


class ExportTests(unittest.TestCase):
    def test_rejects_wrong_target(self):
        with patch.object(exporter.native, "elf_target", return_value="x86_64-unknown-linux-gnu"):
            with self.assertRaisesRegex(ValueError, "release target"):
                exporter.check_static(Path("codex"), TARGET)

    def test_rejects_dynamic_musl_and_needed_libraries(self):
        for outputs in [("INTERP /lib/ld-musl-x86_64.so.1", ""),
                        ("", "0x00000001 (NEEDED) Shared library: [libssl.so]")]:
            with self.subTest(outputs=outputs), \
                 patch.object(exporter.native, "elf_target", return_value=TARGET), \
                 patch.object(exporter.subprocess, "check_output", side_effect=outputs):
                with self.assertRaisesRegex(ValueError, "dynamically linked"):
                    exporter.check_static(Path("codex"), TARGET)

    def test_accepts_static_pie(self):
        with patch.object(exporter.native, "elf_target", return_value=TARGET), \
             patch.object(exporter.subprocess, "check_output", side_effect=["LOAD DYNAMIC", "(FLAGS_1) PIE"]):
            exporter.check_static(Path("codex"), TARGET)

    def test_export_preserves_pair_and_existing_bundle_protocol(self):
        version = json.loads((ROOT / "build.json").read_text())["default_version"]
        spec = exporter.native.load_recipe(ROOT / "build.json", version)
        expected_key = exporter.native.recipe_key(
            spec, [ROOT / "patches" / name for name in spec["patches"]], version)
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "package"
            (package / "bin").mkdir(parents=True)
            contents = {name: (name + " exact bytes").encode() for name in spec["binaries"]}
            for name, data in contents.items():
                (package / "bin" / name).write_bytes(data)
            with patch.object(exporter, "check_static"), \
                 patch.object(exporter.native, "valid_bundle", return_value=True), \
                 patch.object(exporter.native, "smoke_bundle"), \
                 patch.object(exporter.subprocess, "check_output", return_value=f"codex-cli {version}\n"):
                asset = exporter.export(package, Path(temporary) / "output", version)
            self.assertEqual(asset.name, f"codex-native-{expected_key}-linux-x86_64.tar.gz")
            with tarfile.open(asset) as archive:
                self.assertEqual(set(archive.getnames()), {*contents, "sha256.json", "bundle.json"})
                for name, data in contents.items():
                    self.assertEqual(archive.extractfile(name).read(), data)
                    self.assertEqual(archive.getmember(name).mode, 0o755)
                self.assertEqual(json.load(archive.extractfile("bundle.json")),
                                 {"key": expected_key, "version": version})
                self.assertEqual(json.load(archive.extractfile("sha256.json")),
                                 {name: exporter.native.digest(package / "bin" / name) for name in contents})


if __name__ == "__main__":
    unittest.main()
