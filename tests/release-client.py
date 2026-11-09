#!/usr/bin/env python3
import importlib.util
import io
import json
import os
import shutil
from unittest.mock import patch
from pathlib import Path
import tarfile
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.response import addinfourl
from urllib.request import HTTPHandler, build_opener
from email.message import Message

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('client', ROOT / 'scripts/install-release.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

def release(version, published='2026-09-24', key=None, **kwargs):
    key = key or version + '-' + 'a'*64
    assets = [dict(name=f'codex-native-{key}-linux-x86_64.tar.gz', created_at=published),
              dict(name=f'codex-recipe-{key}.tar.gz', created_at=published)]
    return dict(tag_name='bundle-codex-v' + version + '-' + 'b'*16 + '-' + 'c'*16, published_at=published,
                draft=False, prerelease=False, assets=assets, **kwargs)

class ClientTests(unittest.TestCase):
    def test_metadata_authentication_is_explicit_and_scoped(self):
        with patch.object(m, 'urlopen', return_value=io.BytesIO(b'[]')) as anonymous, \
             patch.dict(os.environ, GITHUB_TOKEN='fixture-metadata-token'):
            self.assertEqual(m.request('/releases?per_page=100'), [])
        self.assertIsNone(anonymous.call_args.args[0].get_header('Authorization'))
        with patch.object(m, 'build_opener') as opener:
            opener.return_value.open.return_value = io.BytesIO(b'[]')
            self.assertEqual(m.request('/releases?per_page=100', token='fixture-metadata-token'), [])
            req = opener.return_value.open.call_args.args[0]
            self.assertEqual(req.full_url, m.API + '/releases?per_page=100')
            self.assertEqual(req.get_header('Authorization'), 'Bearer fixture-metadata-token')
            self.assertIsInstance(opener.call_args.args[0], m.NoMetadataRedirect)
        with patch.object(m, 'build_opener') as opener:
            for path in ['/releases/1/assets', '//example.invalid/releases', '/releases#fragment']:
                with self.subTest(path=path), self.assertRaises(ValueError):
                    m.request(path, token='fixture-metadata-token')
            opener.assert_not_called()

    def test_authenticated_metadata_redirects_are_not_followed(self):
        class RedirectTransport(HTTPHandler):
            def http_open(self, req):
                requests.append(req)
                headers = Message()
                headers['Location'] = destination
                response = addinfourl(io.BytesIO(), headers, req.full_url, code)
                response.msg = 'redirect'
                return response
        for code in [301, 302, 303, 307, 308]:
            for destination in ['http://example.invalid/asset', 'http://metadata.invalid/elsewhere']:
                requests = []
                opener = build_opener(RedirectTransport(), m.NoMetadataRedirect())
                with self.subTest(code=code, destination=destination), \
                     patch.object(m, 'API', 'http://metadata.invalid'), \
                     patch.object(m, 'build_opener', return_value=opener):
                    with self.assertRaises(HTTPError):
                        m.request('/releases', token='fixture-metadata-token')
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0].full_url, 'http://metadata.invalid/releases')

    def test_metadata_token_is_not_retained_for_asset_subprocess(self):
        token = 'fixture-metadata-token'
        before = dict(os.environ)
        with patch.object(m, 'build_opener') as opener:
            opener.return_value.open.return_value = io.BytesIO(b'[]')
            m.request('/releases', token=token)
        self.assertEqual(dict(os.environ), before)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'asset.tar.gz'
            content = b'verified asset'
            remote = release('0.10.0')
            name = remote['assets'][0]['name']
            remote['assets'][0].update(state='uploaded', digest='sha256:' + m.hashlib.sha256(content).hexdigest())
            def curl(argv, **kwargs):
                self.assertNotIn(token, repr((argv, kwargs)))
                self.assertNotIn('--header', argv)
                self.assertNotIn('-H', argv)
                self.assertNotIn('env', kwargs)
                Path(argv[argv.index('--output') + 1]).write_bytes(content)
            with patch.object(m.subprocess, 'run', side_effect=curl):
                m.download(remote, name, target)
            self.assertEqual(target.read_bytes(), content)

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
    def test_release_without_native_recipe_pair_is_skipped(self):
        nix_only = release('0.11.0'); nix_only['assets'] = nix_only['assets'][:1]
        self.assertEqual(m.select([nix_only, release('0.10.0')])['tag_name'], release('0.10.0')['tag_name'])
        legacy = dict(release('0.12.0'), tag_name='native-0.12.0-' + 'a'*64)
        self.assertEqual(m.select([legacy, release('0.10.0')])['tag_name'], release('0.10.0')['tag_name'])
    def test_newest_recipe_in_release_wins(self):
        combined = release('0.10.0')
        combined['assets'] += release('0.10.0', '2026-09-25', key='0.10.0-' + 'e'*64)['assets']
        self.assertEqual(m.native_key(combined), '0.10.0-' + 'e'*64)
    def test_install_reuses_prefetched_release_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'package.json').write_text(json.dumps({'version': '0.10.0'}))
            saved = release('0.10.0')
            (root / 'selected.json').write_text(json.dumps(saved))
            with patch.dict(os.environ, CODEX_PACKAGE_ROOT=str(root), CODEX_RELEASE_CACHE=str(root)), \
                 patch.object(m.sys, 'argv', ['install-release.py']), \
                 patch.object(m, 'request', side_effect=OSError('offline')) as request, \
                 patch.object(m, 'prepare', return_value=(root, '0.10.0')) as prepare, \
                 patch.object(m.subprocess, 'run') as run:
                m.main()
            request.assert_not_called()
            prepare.assert_called_once_with(saved, root)
            self.assertEqual(run.call_args.kwargs['env']['CODEX_INSTALL_MODE'], 'download')

    def test_complete_release_verification_and_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recipe = root / 'recipe'
            (recipe / 'scripts').mkdir(parents=True)
            (recipe / 'patches').mkdir()
            shutil.copyfile(ROOT / 'scripts/install-source-codex.py', recipe / 'scripts/install-source-codex.py')
            (recipe / 'patches/test.patch').write_text('test patch')
            data = json.loads((ROOT / 'build.json').read_text())
            version = data['default_version']
            data['releases'][version]['patches'] = ['test.patch']
            (recipe / 'build.json').write_text(json.dumps(data))
            ns = importlib.util.spec_from_file_location('native_fixture', recipe / 'scripts/install-source-codex.py')
            native = importlib.util.module_from_spec(ns); ns.loader.exec_module(native)
            spec = native.load_recipe(recipe / 'build.json', version)
            key = native.recipe_key(spec, [recipe / 'patches/test.patch'], version)
            binary = root / 'binary'; binary.mkdir()
            for name in spec['binaries']:
                (binary / name).write_text('#!/bin/sh\n'
                    + 'if env | grep -Fq fixture-metadata-token; then exit 1; fi\n'
                    + ''.join('# ' + marker + '\n' for marker in spec['markers'])
                    + 'echo codex-cli ' + version + '\n')
                (binary / name).chmod(0o755)
            (binary / 'bundle.json').write_text(json.dumps({'version': version, 'key': key}))
            (binary / 'sha256.json').write_text(json.dumps({name: m.digest(binary / name) for name in spec['binaries']}))
            recipe_name, native_name = f'codex-recipe-{key}.tar.gz', f'codex-native-{key}-linux-x86_64.tar.gz'
            for archive_name, directory, names in [(recipe_name, recipe, ['build.json', 'scripts/install-source-codex.py', 'patches/test.patch']),
                                                  (native_name, binary, [*spec['binaries'], 'bundle.json', 'sha256.json'])]:
                with tarfile.open(root / archive_name, 'w:gz') as tar:
                    for name in names:
                        tar.add(directory / name, arcname=name)
            remote = release(version, key=key)
            def download(remote, name, destination):
                shutil.copyfile(root / name, destination)
            with patch.object(m, 'build_opener') as opener:
                opener.return_value.open.return_value = io.BytesIO(b'[]')
                m.request('/releases', token='fixture-metadata-token')
            with patch.object(m, 'download', side_effect=download):
                _, selected = m.prepare(remote, root / 'cache')
                self.assertEqual(selected, version)
                (recipe / 'patches/test.patch').write_text('tampered patch')
                with tarfile.open(root / recipe_name, 'w:gz') as tar:
                    for name in ['build.json', 'scripts/install-source-codex.py', 'patches/test.patch']:
                        tar.add(recipe / name, arcname=name)
                with self.assertRaisesRegex(ValueError, 'recipe key mismatch'):
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
