#!/usr/bin/env python3
"""Check native publication and automatic recovery from partial uploads."""
import copy
import hashlib
import importlib.util
import io
import json
import os
from unittest.mock import patch
from pathlib import Path
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result

m = load('native_release', 'scripts/native-release.py')
fixtures = load('release_fixtures', 'tests/release.py')

class NativeReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.assets = Path(self.tmp.name)
        self.api = fixtures.FakeGitHub()
        data = json.loads((ROOT / 'native-build.json').read_text())
        self.version = data['default_version']
        recipe = fixtures.native.load_recipe(ROOT / 'native-build.json', self.version)
        self.key = fixtures.native.recipe_key(recipe, [ROOT / 'patches' / p for p in recipe['patches']], self.version)
        self.write_bundle()

    def write_bundle(self, content=b'cli'):
        files = {'codex': content, 'codex-code-mode-host': b'host'}
        files['sha256.json'] = json.dumps({n: hashlib.sha256(b).hexdigest() for n, b in files.items()}).encode()
        files['bundle.json'] = json.dumps({'key': self.key, 'version': self.version}).encode()
        with tarfile.open(self.assets / 'codex-linux-x86_64.tar.gz', 'w:gz') as archive:
            for name, content in files.items():
                info = tarfile.TarInfo(name); info.size = len(content)
                archive.addfile(info, io.BytesIO(content))

    def publish(self):
        m.publish(self.api, self.assets, fixtures.publisher, fixtures.native)

    def test_recovery_requires_successful_native_build_on_main(self):
        for branch, conclusion, accepted in [('main', 'success', True), ('feature', 'success', False), ('main', 'failure', False)]:
            with self.subTest(branch=branch, conclusion=conclusion):
                run = {'head_branch': branch, 'path': '.github/workflows/native.yml'}
                jobs = {'jobs': [{'steps': [{'name': 'Build, test patches, and verify binary pair', 'conclusion': conclusion}]}]}
                api = fixtures.FakeGitHub()
                api.request = lambda path: jobs if '/jobs?' in path else run
                output = self.assets / 'output'
                with patch.object(m.sys if hasattr(m, 'sys') else __import__('sys'), 'argv', ['native-release.py', 'prepare', '--recover-run', '123']), \
                     patch.dict(os.environ, GH_TOKEN='test', GITHUB_OUTPUT=str(output)), \
                     patch.object(m, 'module', side_effect=lambda name, filename: fixtures.publisher if name == 'publisher' else fixtures.native), \
                     patch.object(fixtures.publisher, 'GitHub', return_value=api):
                    if accepted:
                        m.main()
                        self.assertIn('recover=true', output.read_text())
                    else:
                        with self.assertRaisesRegex(ValueError, 'tested native build'):
                            m.main()

    def test_publish_and_verify_without_overwrite(self):
        self.publish()
        self.assertFalse(self.api.release['draft'])
        original = copy.deepcopy(self.api.files)
        self.write_bundle(b'rebuilt cli')
        self.publish()
        self.assertEqual(self.api.files, original)

    def test_partial_upload_resumes_with_original_binary(self):
        upload = self.api.upload
        def fail_second(release_id, path):
            if self.api.files:
                raise RuntimeError('interrupted')
            upload(release_id, path)
        self.api.upload = fail_second
        with self.assertRaises(RuntimeError):
            self.publish()
        self.assertTrue(self.api.release['draft'])
        original = self.api.files['codex-linux-x86_64.tar.gz']
        self.write_bundle(b'later rebuild')
        self.api.upload = upload
        self.publish()
        self.assertFalse(self.api.release['draft'])
        self.assertEqual(self.api.files['codex-linux-x86_64.tar.gz'], original)

    def test_corrupt_remote_bytes_stop_publication(self):
        self.publish()
        self.api.release['draft'] = True
        self.api.files['recipe.tar.gz'] = b'corrupted'
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            self.publish()
        self.assertTrue(self.api.release['draft'])

if __name__ == '__main__':
    unittest.main()
