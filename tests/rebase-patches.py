#!/usr/bin/env python3
import hashlib
import importlib.util
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('rebase', ROOT / 'scripts/rebase-patches.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class RebaseTests(unittest.TestCase):
    def run_case(self, conflict=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / 'old'
            old.mkdir()
            original = ''.join(f'line {i}\n' for i in range(30))
            (old / 'example').write_text(original)
            (old / 'link').symlink_to('example')
            archive = root / 'source.tar.gz'
            with tarfile.open(archive, 'w:gz') as tar:
                tar.add(old, arcname='release')
            previous = {'source': {'url': archive.as_uri(), 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}}
            m.git(old, 'init', '-q')
            m.git(old, 'add', '.')
            m.git(old, 'commit', '-qm', 'base')
            (old / 'example').write_text(original.replace('line 15\n', 'patched\n'))
            (root / 'patches').mkdir()
            (root / 'patches/example.patch').write_bytes(m.git(old, 'diff'))
            source = root / 'new'
            source.mkdir()
            new = original.replace('line 12\n', 'new context\n')
            if conflict:
                new = new.replace('line 15\n', 'conflicting upstream\n')
            (source / 'example').write_text(new)
            (source / 'link').symlink_to('example')
            if conflict:
                with self.assertRaises(subprocess.CalledProcessError):
                    m.rebase(root, source, previous, ['example.patch'], '0.2.0')
                self.assertFalse((root / 'patches/0.2.0').exists())
                self.assertEqual((source / 'example').read_text(), new)
            else:
                names = m.rebase(root, source, previous, ['example.patch'], '0.2.0')
                self.assertEqual(names, ['0.2.0/example.patch'])
                self.assertTrue((source / 'link').is_symlink())
                self.assertEqual((source / 'example').read_text(), new.replace('line 15\n', 'patched\n'))
                (source / 'example').write_text(new)
                m.git(source, 'apply', '--check', str(root / 'patches' / names[0]))
    def test_snapshot_hint_rebase_preserves_new_upstream_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / 'old'; old.mkdir()
            base = 'old layout\n• Working (0s • esc to interrup…\nfooter\n'
            (old / 'example.snap').write_text(base)
            archive = root / 'source.tar.gz'
            with tarfile.open(archive, 'w:gz') as tar:
                tar.add(old, arcname='release')
            previous = {'source': {'url': archive.as_uri(), 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}}
            m.git(old, 'init', '-q'); m.git(old, 'add', '.'); m.git(old, 'commit', '-qm', 'base')
            (old / 'example.snap').write_text(base.replace('esc to interrup…', 'esc esc to inte…'))
            (root / 'patches').mkdir()
            (root / 'patches/example.patch').write_bytes(m.git(old, 'diff'))
            source = root / 'new'; source.mkdir()
            upstream = base.replace('old layout', 'new layout') + '\n• Working (0s • esc to interrupt)\n'
            (source / 'example.snap').write_text(upstream)
            names = m.rebase(root, source, previous, ['example.patch'], '0.2.0')
            expected = upstream.replace('esc to interrup…', 'esc esc to inte…').replace('esc to interrupt)', 'esc esc to interrupt)')
            self.assertEqual((source / 'example.snap').read_text(), expected)
            (source / 'example.snap').write_text(upstream)
            m.git(source, 'apply', '--check', str(root / 'patches' / names[0]))

    def test_hint_rewrite_is_idempotent_and_preserves_other_text(self):
        original = 'user says esc to interrupt\n• Working (0s • esc to interrupt)    \n• Working (0s • e…\n'
        expected = 'user says esc to interrupt\n• Working (0s • esc esc to interrupt)\n• Working (0s • e…\n'
        self.assertEqual(m.double_escape_snapshot(original), expected)
        self.assertEqual(m.double_escape_snapshot(expected), expected)

    def test_unrelated_snapshot_conflict_is_not_resolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'example.snap').write_text('unchanged')
            with patch.object(m, 'git', side_effect=[b'example.snap\0', b'base', b'upstream', b'unrelated edit']):
                self.assertFalse(m.resolve_hint_snapshots(root))
            self.assertEqual((root / 'example.snap').read_text(), 'unchanged')

    def test_clean_rebase(self):
        self.run_case()
    def test_conflicts_publish_nothing(self):
        self.run_case(True)

if __name__ == '__main__':
    unittest.main()
