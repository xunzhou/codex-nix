#!/usr/bin/env python3
"""Publication boundary regressions using synthetic repositories and transports."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('gate', ROOT / 'scripts/publication_gate.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
NAME = 'Fixture'
EMAIL = 'fixture' + '@users.noreply.github.com'
IDENTITY = f'{NAME} <{EMAIL}>'
PRIVATE = 'fixture' + '@' + 'example.invalid'


class GateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.old = Path.cwd()
        self.addCleanup(os.chdir, self.old)
        self.root = Path(self.temp.name)
        os.chdir(self.root)
        self.env = patch.dict(os.environ, {f'GIT_{role}_{key}': value
            for role in ('AUTHOR', 'COMMITTER') for key, value in [('NAME', NAME), ('EMAIL', EMAIL)]})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', NAME)
        self.git('config', 'user.email', EMAIL)
        self.git('config', 'publication.identity', IDENTITY)
        self.commit('Initial', b'already public\n')
        self.base = self.git('rev-parse', 'HEAD').strip().decode()
        self.baseline = {'refs/heads/main': self.base}

    def git(self, *args, data=None):
        return subprocess.run(['git', *args], input=data, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout

    def commit(self, message, content=None, name='file'):
        if content is not None:
            Path(name).write_bytes(content)
            self.git('add', '--', name)
        self.git('commit', '-q', '--allow-empty', '-m', message)
        return self.git('rev-parse', 'HEAD').strip().decode()

    def check(self, head=None, baseline=None):
        return gate.check_updates([(head or self.git('rev-parse', 'HEAD').strip().decode(),
                                    'refs/heads/topic')],
                                  self.baseline if baseline is None else baseline, {IDENTITY})

    def test_clean_new_branch(self):
        head = self.commit('Fix parser', b'new code\n')
        self.assertEqual(self.check(), [head])

    def test_already_public_history_excluded(self):
        head = self.commit('Historical', PRIVATE.encode())
        self.baseline['refs/heads/main'] = head
        self.commit('Fix parser', b'clean\n')
        self.assertEqual(len(self.check()), 1)

    def test_intermediate_deleted_blob_rejected(self):
        self.commit('Add file', PRIVATE.encode())
        self.git('rm', 'file')
        self.commit('Remove file')
        with self.assertRaisesRegex(gate.Blocked, 'email: blob-'):
            self.check()

    def test_intermediate_message_rejected(self):
        self.commit(PRIVATE)
        self.commit('Fix parser')
        with self.assertRaisesRegex(gate.Blocked, 'email: commit-'):
            self.check()

    def test_both_identities_checked(self):
        for role in ('AUTHOR', 'COMMITTER'):
            with self.subTest(role=role):
                self.git('reset', '--hard', self.base)
                with patch.dict(os.environ, {f'GIT_{role}_EMAIL': 'other' + '@users.noreply.github.com'}):
                    self.commit('Fix parser')
                with self.assertRaisesRegex(gate.Blocked, 'identity:'):
                    self.check()

    def test_trailer_checked(self):
        self.commit('Fix parser\n\nReviewed-by: Fixture <' + PRIVATE + '>')
        with self.assertRaises(gate.Blocked):
            self.check()

    def test_private_filename_diagnostic_redacted(self):
        self.commit('Add file', b'clean', PRIVATE)
        with self.assertRaises(gate.Blocked) as error:
            self.check()
        self.assertNotIn(PRIVATE, str(error.exception))
        self.assertIn('path-', str(error.exception))

    def test_binary_and_non_utf8_rejected(self):
        for content in (b'x\x00y', b'\xff'):
            self.git('reset', '--hard', self.base)
            self.commit('Add file', content)
            with self.assertRaises(gate.Blocked):
                self.check()

    def test_patch_headers_checked(self):
        self.commit('Update patch', ('From: Fixture <' + PRIVATE + '>\n').encode())
        with self.assertRaises(gate.Blocked):
            self.check()

    def test_lightweight_tag_allowed_annotated_tag_blocked(self):
        head = self.commit('Fix parser')
        self.assertEqual(gate.check_updates([(head, 'refs/tags/v1')], self.baseline, {IDENTITY}), [head])
        self.git('tag', '-a', 'v2', '-m', 'Release')
        tag = self.git('rev-parse', 'v2').strip().decode()
        with self.assertRaisesRegex(gate.Blocked, 'unsupported-tag'):
            gate.check_updates([(tag, 'refs/tags/v2')], self.baseline, {IDENTITY})

    def test_multi_ref_and_deletion(self):
        safe = self.commit('Fix parser')
        bad = self.commit('Add file', PRIVATE.encode())
        with self.assertRaises(gate.Blocked):
            gate.check_updates([(safe, 'refs/heads/a'), (bad, 'refs/heads/b')], self.baseline, {IDENTITY})
        self.assertEqual(gate.check_updates([('0' * 40, 'refs/heads/a')], self.baseline, {IDENTITY}), [])

    def test_missing_baseline_fails_closed(self):
        with self.assertRaises(gate.Blocked):
            self.check(baseline={'refs/heads/main': '1' * 40})

    def test_invalid_private_rule_fails_closed(self):
        self.git('config', 'publication.denyPattern', '[')
        with self.assertRaisesRegex(gate.Blocked, 'invalid-policy'):
            gate.scan(b'clean', 'fixture')

    def test_private_rule_redacted(self):
        self.git('config', 'publication.denyPattern', 'synthetic forbidden phrase')
        with self.assertRaisesRegex(gate.Blocked, '^private-pattern: fixture$'):
            gate.scan(b'synthetic forbidden phrase', 'fixture')

    def test_text_minimal_and_instructions(self):
        for body in (PRIVATE, 'revision 1 ' + 'of at most 2', 'User' + ': do something', 'x' * 2001):
            with self.subTest(length=len(body)), self.assertRaises(gate.Blocked):
                gate.payload_check({'body': body})
        with self.assertRaises(gate.Blocked):
            gate.payload_check({'unknown': 'clean'})

    def test_exact_payload_sent_and_receipted(self):
        Path('body').write_bytes(b'Fix parser.\n')
        output = io.StringIO()
        with patch.object(gate, 'run', return_value=b'{}') as transport, contextlib.redirect_stdout(output):
            gate.api_publish('comment', 1, None, 'body', None)
        actual = transport.call_args.kwargs['data']
        self.assertEqual(json.loads(actual), {'body': 'Fix parser.\n'})
        receipts = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertTrue(all(r['sha256'] == gate.digest(actual) for r in receipts))
        self.assertNotIn('Fix parser', output.getvalue())

    def test_rejected_payload_never_sent(self):
        Path('body').write_text(PRIVATE)
        with patch.object(gate, 'run') as transport, self.assertRaises(gate.Blocked):
            gate.api_publish('comment', 1, None, 'body', None)
        transport.assert_not_called()

    def test_real_hook_blocks_push_and_installs(self):
        (self.root / 'scripts').mkdir()
        (self.root / '.githooks').mkdir()
        shutil.copy(ROOT / 'scripts/publication_gate.py', 'scripts/publication_gate.py')
        shutil.copy(ROOT / '.githooks/pre-push', '.githooks/pre-push')
        with contextlib.redirect_stdout(io.StringIO()):
            gate.install()
        remote = self.root / 'remote.git'
        self.git('init', '--bare', '-q', str(remote))
        self.git('remote', 'add', 'origin', str(remote))
        self.git('push', 'origin', self.base + ':refs/heads/main')
        self.commit('Add file', PRIVATE.encode())
        result = subprocess.run(['git', 'push', 'origin', 'HEAD:refs/heads/topic'], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(PRIVATE.encode(), result.stderr)
        self.assertEqual(self.git('ls-remote', 'origin', 'refs/heads/topic'), b'')

    def test_empty_destination_scans_history(self):
        self.commit('Historical', PRIVATE.encode())
        self.commit('Fix parser', b'clean')
        with self.assertRaises(gate.Blocked):
            self.check(baseline={})

    def test_shallow_repository_rejected(self):
        Path('.git/shallow').write_text(self.base + '\n')
        with self.assertRaisesRegex(gate.Blocked, 'shallow'):
            self.check()

    def test_noncommit_tag_and_private_ref_rejected(self):
        blob = self.git('hash-object', '-w', '--stdin', data=b'clean').strip().decode()
        with self.assertRaisesRegex(gate.Blocked, 'unsupported-tag'):
            gate.check_updates([(blob, 'refs/tags/blob')], self.baseline, {IDENTITY})
        with self.assertRaises(gate.Blocked):
            gate.check_updates([('0' * 40, 'refs/heads/' + PRIVATE)], self.baseline, {IDENTITY})

    def test_raw_json_receipt_matches_shell_payload(self):
        raw = b'{ "body": "Fix parser." }'
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(gate.payload_check(json.loads(raw), raw), raw)
        self.assertEqual(json.loads(out.getvalue())['sha256'], gate.digest(raw))

    def test_pr_remote_head_mismatch_blocks_send(self):
        Path('body').write_text('Fix parser.')
        Path('title').write_text('Fix parser')
        with patch.object(gate, 'advertised', return_value={}), patch.object(gate, 'run') as send:
            with self.assertRaisesRegex(gate.Blocked, 'head-mismatch'):
                gate.api_publish('pr', None, 'title', 'body', 'topic', self.base)
        send.assert_not_called()

    def test_pr_title_bytes_preserved(self):
        Path('body').write_text('Fix parser.\n')
        Path('title').write_text('Fix parser\n')
        with patch.object(gate, 'advertised', return_value={'refs/heads/topic': self.base}), \
                patch.object(gate, 'run', return_value=b'{}') as send, \
                contextlib.redirect_stdout(io.StringIO()):
            gate.api_publish('pr', None, 'title', 'body', 'topic', self.base)
        self.assertEqual(json.loads(send.call_args.kwargs['data'])['title'], 'Fix parser\n')

    def test_source_bundle_receipt_binds_commit_and_file(self):
        self.git('remote', 'add', 'origin', str(self.root / 'remote.git'))
        self.git('init', '--bare', '-q', str(self.root / 'remote.git'))
        output = self.root / 'source.bundle'
        result = subprocess.run(['python3', str(ROOT / 'scripts/publication_gate.py'),
                                 'bundle', '--output', str(output)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        item = json.loads(result.stdout.splitlines()[-1])
        self.assertEqual(item['commit'], self.base)
        self.assertEqual(item['sha256'], gate.digest(output.read_bytes()))

    def test_bundle_head_race_blocks(self):
        with patch.object(gate, 'pre_push'), \
                patch.object(gate, 'git', side_effect=[self.base.encode(), b'', b'1' * 40 + b' HEAD\n']), \
                patch('sys.argv', ['gate', 'bundle', '--output', 'bundle']), \
                contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(gate.main(), 1)
        self.assertIn('bundle-head-race', error.getvalue())

    def test_release_transport_blocks_before_request(self):
        release_spec = importlib.util.spec_from_file_location('release', ROOT / 'scripts/publish-release.py')
        release = importlib.util.module_from_spec(release_spec)
        release_spec.loader.exec_module(release)
        with patch.object(release, 'urlopen') as transport:
            with self.assertRaises(ValueError):
                release.GitHub('fixture/repository', 'synthetic').request('/releases', {'body': PRIVATE})
        transport.assert_not_called()

    def test_replacement_objects_rejected(self):
        head = self.commit('Fix parser')
        self.git('replace', head, self.base)
        with self.assertRaisesRegex(gate.Blocked, 'replacement-objects'):
            self.check()

    def test_submodule_rejected(self):
        self.git('update-index', '--add', '--cacheinfo', '160000,' + self.base + ',module')
        self.commit('Add module')
        with self.assertRaisesRegex(gate.Blocked, 'unsupported-tree-entry'):
            self.check()


if __name__ == '__main__':
    unittest.main()
