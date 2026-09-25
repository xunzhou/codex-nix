#!/usr/bin/env python3
import importlib.util
import io
import json
import shutil
from unittest.mock import patch
from pathlib import Path
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('client', ROOT / 'scripts/install-release.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

def release(version, published='2026-09-24', **kwargs):
    return dict(tag_name='native-' + version + '-' + 'a'*64, published_at=published, draft=False, prerelease=False, **kwargs)

class ClientTests(unittest.TestCase):
    def test_numeric_version_order_not_publication_order(self):
        self.assertEqual(m.select([release('0.9.0', '2026-10-01'), release('0.10.0')])['tag_name'], release('0.10.0')['tag_name'])
    def test_draft_and_prerelease_excluded(self):
        draft = release('0.11.0'); draft['draft'] = True
        pre = release('0.12.0'); pre['prerelease'] = True
        self.assertEqual(m.select([draft, pre, release('0.10.0')])['tag_name'], release('0.10.0')['tag_name'])
    def test_exact_installed_version(self):
        self.assertEqual(m.select([release('0.9.0'), release('0.10.0')], '0.9.0')['tag_name'], release('0.9.0')['tag_name'])
        with self.assertRaises(ValueError):
            m.select([release('0.10.0')], '0.9.0')
    def test_complete_release_verification_and_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recipe = root / 'recipe'
            (recipe / 'scripts').mkdir(parents=True)
            (recipe / 'patches').mkdir()
            shutil.copyfile(ROOT / 'scripts/install-source-codex.py', recipe / 'scripts/install-source-codex.py')
            (recipe / 'patches/test.patch').write_text('test patch')
            data = json.loads((ROOT / 'native-build.json').read_text())
            version = data['default_version']
            data['releases'][version]['patches'] = ['test.patch']
            (recipe / 'build.json').write_text(json.dumps(data))
            ns = importlib.util.spec_from_file_location('native_fixture', recipe / 'scripts/install-source-codex.py')
            native = importlib.util.module_from_spec(ns); ns.loader.exec_module(native)
            spec = native.load_recipe(recipe / 'build.json', version)
            key = native.recipe_key(spec, [recipe / 'patches/test.patch'], version)
            binary = root / 'binary'; binary.mkdir()
            for name in spec['binaries']:
                (binary / name).write_text('#!/bin/sh\n# terminal palette refresh did not return default colors\necho codex-cli ' + version + '\n')
                (binary / name).chmod(0o755)
            (binary / 'bundle.json').write_text(json.dumps({'version': version, 'key': key}))
            (binary / 'sha256.json').write_text(json.dumps({name: m.digest(binary / name) for name in spec['binaries']}))
            for archive_name, directory, names in [('recipe.tar.gz', recipe, ['build.json', 'scripts/install-source-codex.py', 'patches/test.patch']),
                                                  ('codex-linux-x86_64.tar.gz', binary, [*spec['binaries'], 'bundle.json', 'sha256.json'])]:
                with tarfile.open(root / archive_name, 'w:gz') as tar:
                    for name in names:
                        tar.add(directory / name, arcname=name)
            metadata = {'schema': 1, 'version': version, 'key': key, 'sha256': {name: m.digest(root / name) for name in ['recipe.tar.gz', 'codex-linux-x86_64.tar.gz']}}
            (root / 'release.json').write_text(json.dumps(metadata))
            remote = release(version); remote['tag_name'] = 'native-' + key
            def download(remote, name, destination):
                shutil.copyfile(root / name, destination)
            with patch.object(m, 'download', side_effect=download):
                _, selected = m.prepare(remote, root / 'cache')
                self.assertEqual(selected, version)
                (root / 'codex-linux-x86_64.tar.gz').write_bytes(b'corrupted')
                with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                    m.prepare(remote, root / 'cache')

    def test_symlinks_duplicates_and_traversal_rejected(self):
        for name, kind, duplicate in [('file', tarfile.SYMTYPE, False), ('../escape', tarfile.REGTYPE, False), ('file', tarfile.REGTYPE, True)]:
            with self.subTest(name=name, kind=kind, duplicate=duplicate), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                with tarfile.open(root / 'test.tar', 'w') as tar:
                    member = tarfile.TarInfo(name); member.type = kind
                    tar.addfile(member)
                    if duplicate:
                        tar.addfile(member)
                with self.assertRaises(ValueError):
                    m.extract(root / 'test.tar', root / 'out', ['file'])

if __name__ == '__main__':
    unittest.main()
