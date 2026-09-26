#!/usr/bin/env python3
"""Exercise publication, retry, and recipe discovery without builds or GitHub writes."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


publisher = module('publisher', 'scripts/publish-release.py')
native = module('native', 'scripts/install-source-codex.py')
TAG = 'codex-v0.154.0-' + 'a' * 16 + '-' + 'b' * 16
OUTPUT = '/nix/store/' + 'c' * 32 + '-codex'
EXPECTED = {'key': '0.154.0-' + 'd' * 64, 'version': '0.154.0'}


class FakeGitHub:
    def __init__(self):
        self.release = None
        self.files = {}
        self.calls = []
        self.fail_upload = False

    def request(self, path, data=None, method=None):
        self.calls.append((path, data, method))
        if data and path == '/releases':
            self.release = dict(data, id=1, assets=[])
        elif method == 'PATCH':
            self.release.update(data)
        return copy.deepcopy(self.release)

    def upload(self, release_id, path):
        if self.fail_upload:
            raise RuntimeError('upload interrupted')
        self.files[path.name] = path.read_bytes()
        self.release['assets'].append({'name': path.name, 'id': len(self.files),
                                      'state': 'uploaded', 'digest': 'sha256:' + publisher.sha256(path)})

    def download(self, asset, destination):
        destination.write_bytes(self.files[asset['name']])
        publisher.require('sha256:' + publisher.sha256(destination) == asset['digest'], 'downloaded asset digest mismatch')


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.assets = Path(self.temp.name)
        self.api = FakeGitHub()
        self.manifest_name = f'codex-{TAG}-manifest.json'
        self.archive_name = f'codex-{TAG}-x86_64-linux.nar.zst'
        (self.assets / self.archive_name).write_bytes(b'nix closure')
        self.manifest = dict(schema=1, tag=TAG, system='x86_64-linux', codex_version='0.154.0',
                             output_path=OUTPUT, derivation_path=OUTPUT + '.drv', patch_sha256='a' * 64,
                             closure_paths=2, archive=self.archive_name,
                             archive_sha256=publisher.sha256(self.assets / self.archive_name))
        self.write_manifest()
        self.native_name = f'codex-native-{EXPECTED["key"]}-linux-x86_64.tar.gz'
        self.write_native(EXPECTED)
        self.recipe_name = f'codex-recipe-{EXPECTED["key"]}.tar.gz'
        recipe_temp = tempfile.TemporaryDirectory()
        self.addCleanup(recipe_temp.cleanup)
        recipe = Path(recipe_temp.name)
        (recipe / 'build.json').write_text('{}')
        (recipe / 'fix.patch').write_text('patch')
        self.recipe_files = {'build.json': recipe / 'build.json', 'patches/fix.patch': recipe / 'fix.patch'}

    def write_manifest(self):
        (self.assets / self.manifest_name).write_text(json.dumps(self.manifest))
        (self.assets / f'codex-{TAG}-SHA256SUMS').write_text(''.join(
            f'{publisher.sha256(self.assets / name)}  {name}\n' for name in (self.archive_name, self.manifest_name)))

    def write_native(self, expected):
        import hashlib
        files = {'codex': b'cli', 'codex-code-mode-host': b'host'}
        files['sha256.json'] = json.dumps({name: hashlib.sha256(data).hexdigest() for name, data in files.items()}).encode()
        files['bundle.json'] = json.dumps(expected).encode()
        with tarfile.open(self.assets / self.native_name, 'w:gz') as archive:
            for name, data in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))

    def publish(self):
        publisher.publish(self.api, self.assets, TAG, 'e' * 40, EXPECTED, OUTPUT, self.recipe_files)

    def test_draft_published_only_with_both_backends(self):
        (self.assets / 'source.zip').write_bytes(b'not a release asset')
        self.publish()
        create = next(data for path, data, _ in self.api.calls if path == '/releases')
        self.assertTrue(create['draft'])
        self.assertEqual(create['tag_name'], 'bundle-' + TAG)
        self.assertFalse(self.api.release['draft'])
        self.assertEqual(len(self.api.release['assets']), 5)
        self.assertIn(self.recipe_name, self.api.files)
        self.assertEqual(self.api.calls[-1][2], 'PATCH')
        self.assertEqual(self.api.calls[-1][1], {'draft': False, 'make_latest': 'true'})

    def test_rerun_verifies_without_overwriting(self):
        self.publish()
        original = copy.deepcopy(self.api.release)
        self.publish()
        self.assertEqual(original, self.api.release)

    def test_failed_upload_leaves_resumable_draft(self):
        self.api.fail_upload = True
        with self.assertRaises(RuntimeError):
            self.publish()
        self.assertTrue(self.api.release['draft'])
        self.api.fail_upload = False
        self.publish()
        self.assertFalse(self.api.release['draft'])

    def test_partial_draft_can_resume_exact_artifacts(self):
        self.api.release = dict(id=1, tag_name='bundle-' + TAG, draft=True, assets=[])
        self.api.upload(1, self.assets / self.archive_name)
        self.publish()
        self.assertFalse(self.api.release['draft'])

    def test_partial_draft_rejects_changed_artifacts(self):
        self.api.release = dict(id=1, tag_name='bundle-' + TAG, draft=True, assets=[])
        self.api.upload(1, self.assets / self.archive_name)
        (self.assets / self.archive_name).write_bytes(b'other closure')
        self.manifest['archive_sha256'] = publisher.sha256(self.assets / self.archive_name)
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, 'partial draft differs'):
            self.publish()

    def test_complete_release_accepts_equivalent_nix_export(self):
        self.publish()
        (self.assets / self.archive_name).write_bytes(b're-exported closure')
        self.manifest['archive_sha256'] = publisher.sha256(self.assets / self.archive_name)
        self.write_manifest()
        self.publish()
        self.assertEqual(self.api.files[self.archive_name], b'nix closure')

    def test_wrong_native_recipe_never_creates_release(self):
        self.write_native(dict(EXPECTED, key='wrong'))
        with self.assertRaisesRegex(ValueError, 'native recipe mismatch'):
            self.publish()
        self.assertIsNone(self.api.release)

    def test_corrupt_local_nix_never_creates_release(self):
        (self.assets / self.archive_name).write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'local Nix archive digest'):
            self.publish()
        self.assertIsNone(self.api.release)

    def test_corrupt_existing_digest_is_rejected(self):
        self.publish()
        self.api.files[self.manifest_name] = b'corrupt'
        with self.assertRaisesRegex(ValueError, 'downloaded asset digest mismatch'):
            self.publish()

    def test_incomplete_published_release_is_rejected(self):
        self.publish()
        self.api.release['assets'] = [a for a in self.api.release['assets'] if a['name'] != self.manifest_name]
        with self.assertRaisesRegex(ValueError, 'incomplete published'):
            self.publish()

    def test_wrong_nix_output_is_rejected(self):
        self.manifest['output_path'] = 'wrong'
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, 'Nix source identity mismatch'):
            self.publish()

    def test_recipe_archive_is_deterministic(self):
        first, second = self.assets / 'first.tar.gz', self.assets / 'second.tar.gz'
        publisher.write_recipe(first, self.recipe_files)
        publisher.write_recipe(second, self.recipe_files)
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_changed_remote_recipe_is_rejected(self):
        self.publish()
        self.recipe_files['patches/fix.patch'].write_text('other patch')
        with self.assertRaisesRegex(ValueError, 'recipe content mismatch'):
            self.publish()

    def test_unexpected_remote_asset_is_rejected(self):
        self.publish()
        self.api.release['assets'].append(dict(name='unexpected', state='uploaded', digest='sha256:' + 'f' * 64))
        with self.assertRaisesRegex(ValueError, 'unexpected release asset'):
            self.publish()


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.scratch = Path(self.temp.name)
        self.name = f'codex-native-{EXPECTED["key"]}-linux-x86_64.tar.gz'

    def lookup(self, pages):
        calls = []
        def download(url, path):
            calls.append(url)
            path.write_text(json.dumps(pages[len(calls) - 1]))
            return True
        with patch.object(native, 'http_download', side_effect=download):
            result = native.unified_bundle_url({'release_repository': 'example/test'}, EXPECTED['key'], self.scratch)
        return result, calls

    def test_exact_recipe_found_after_first_page(self):
        result, calls = self.lookup([[{'tag_name': 'old'}] * 100,
                                    [{'tag_name': 'bundle-' + TAG, 'assets': [{'name': self.name}]}]])
        self.assertEqual(result, f'https://github.com/example/test/releases/download/bundle-{TAG}/{self.name}')
        self.assertTrue(calls[-1].endswith('page=2'))

    def test_absent_recipe_and_drafts_are_skipped(self):
        result, _ = self.lookup([[{'draft': True, 'tag_name': 'bundle-' + TAG, 'assets': [{'name': self.name}]}]])
        self.assertIsNone(result)

    def test_network_failure_does_not_become_missing_release(self):
        with patch.object(native, 'http_download', side_effect=ValueError('HTTP 503')):
            with self.assertRaisesRegex(ValueError, 'HTTP 503'):
                native.unified_bundle_url({'release_repository': 'example/test'}, EXPECTED['key'], self.scratch)


if __name__ == '__main__':
    unittest.main()
