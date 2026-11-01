#!/usr/bin/env python3
"""Test manifest updates and cross-backend recipe consistency."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('update_manifest', ROOT / 'scripts/update-manifest.py')
UPDATER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(UPDATER)


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'patches').mkdir()
        self.data = {'default_version':'0.1.0','profiles':{'v8':{'crate_versions':{'v8':'1.0.0'}}},
                     'releases':{'0.1.0':{'profile':'v8','patches':['example.patch']}}}
        self.manifest = self.root / 'build.json'
        self.manifest.write_text(json.dumps(self.data))
        (self.root / 'patches/example.patch').write_text('fixture')
        self.source = self.root / 'fixture/release/codex-rs'
        self.source.mkdir(parents=True)
        (self.source / 'Cargo.toml').write_text('[workspace.package]\nversion="0.2.0"\n')
        (self.source / 'Cargo.lock').write_text('[[package]]\nname="v8"\nversion="1.0.0"\n')

    def command(self, *args, **kwargs):
        if args[0] == 'curl':
            with tarfile.open(args[args.index('-o')+1], 'w:gz') as archive:
                archive.add(self.source.parent, arcname='release')
        return subprocess.CompletedProcess(args, 0, stdout='sha256-source\n')

    def test_updates_shared_source_pins_and_replaces_previous_release(self):
        mismatch = subprocess.CompletedProcess([], 1, stdout='', stderr='got: sha256-'+'A'*43+'=')
        with mock.patch.object(UPDATER, 'command', side_effect=self.command) as commands, mock.patch.object(UPDATER.subprocess, 'run', return_value=mismatch):
            UPDATER.update(self.root, '0.2.0')
        result = json.loads(self.manifest.read_text())
        self.assertEqual(result['default_version'], '0.2.0')
        self.assertEqual(list(result['releases']), ['0.2.0'])
        self.assertEqual(result['releases']['0.2.0']['source']['nar_hash'], 'sha256-source')
        self.assertEqual(len(result['releases']['0.2.0']['source']['sha256']), 64)
        self.assertTrue(any(call.args[:3] == ('nix','build','--no-link') for call in commands.call_args_list))

    def test_dependency_change_requires_review_without_editing_manifest(self):
        before = self.manifest.read_bytes()
        (self.source / 'Cargo.lock').write_text('[[package]]\nname="v8"\nversion="2.0.0"\n')
        with mock.patch.object(UPDATER, 'command', side_effect=self.command):
            with self.assertRaisesRegex(ValueError, 'review the shared'):
                UPDATER.update(self.root, '0.2.0')
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_failed_vendor_resolution_restores_original_manifest(self):
        before = self.manifest.read_bytes()
        with mock.patch.object(UPDATER, 'command', side_effect=self.command), mock.patch.object(UPDATER.subprocess, 'run', return_value=subprocess.CompletedProcess([],1,stdout='',stderr='network unavailable')):
            with self.assertRaisesRegex(ValueError, 'could not resolve'):
                UPDATER.update(self.root, '0.2.0')
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_new_patch_stack_is_visible_before_vendor_evaluation(self):
        # Exercise both a reviewed untracked port and patches emitted by rebase.
        real_run = subprocess.run
        for rebased in (False, True):
            with self.subTest(rebased=rebased), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'patches').mkdir()
                names = ['live-palette-refresh.patch', 'double-esc-interrupt.patch']
                data = json.loads(json.dumps(self.data))
                data['releases']['0.1.0']['patches'] = names
                (root / 'build.json').write_text(json.dumps(data))
                for name in names:
                    (root / 'patches' / name).write_text('old patch')
                def git(*args):
                    return real_run(['git', '-C', str(root), *args], check=True,
                                    capture_output=True, text=True).stdout
                git('init', '-q')
                git('add', '.')
                git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                    'commit', '-qm', 'fixture')
                before = git('rev-parse', 'HEAD')
                (root / 'patches/unrelated.patch').write_text('unrelated')
                selected = [f'0.2.0/{name}' for name in names]
                def emit(*args):
                    (root / 'patches/0.2.0').mkdir()
                    for name in selected:
                        (root / 'patches' / name).write_text('new patch')
                    return selected
                if not rebased:
                    emit()
                def command(*args, **kwargs):
                    if args[:2] == ('git', 'add'):
                        return real_run(args, check=True, text=True, **kwargs)
                    if rebased and args[0] == 'git' and 'apply' in args:
                        raise subprocess.CalledProcessError(1, args)
                    if args[:2] == ('nix', 'build'):
                        verify_index()
                    return self.command(*args, **kwargs)
                def verify_index():
                    tracked = git('ls-files').splitlines()
                    for name in selected:
                        self.assertIn(f'patches/{name}', tracked)
                    self.assertNotIn('patches/unrelated.patch', tracked)
                    self.assertEqual(git('diff', '--cached', '--name-only'), '')
                    self.assertEqual(git('rev-parse', 'HEAD'), before)
                def vendor(*args, **kwargs):
                    verify_index()
                    return subprocess.CompletedProcess([], 1, stdout='',
                        stderr='got: sha256-' + 'A' * 43 + '=')
                module = mock.Mock()
                module.rebase.side_effect = emit
                with mock.patch.object(UPDATER, 'command', side_effect=command), \
                     mock.patch.object(UPDATER.subprocess, 'run', side_effect=vendor), \
                     mock.patch.object(UPDATER.importlib.util, 'spec_from_file_location'), \
                     mock.patch.object(UPDATER.importlib.util, 'module_from_spec', return_value=module):
                    UPDATER.update(root, '0.2.0')
                result = json.loads((root / 'build.json').read_text())
                self.assertEqual(result['releases']['0.2.0']['patches'], selected)


if __name__ == '__main__':
    unittest.main()
