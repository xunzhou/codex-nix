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

    def test_public_signed_history_excluded_but_new_signed_commit_blocked(self):
        raw = self.git('cat-file', 'commit', self.base)
        headers, message = raw.split(b'\n\n', 1)
        signed = self.git('hash-object', '-t', 'commit', '-w', '--stdin',
                          data=headers + b'\ngpgsig synthetic\n signature continuation\n\n'
                          + message).decode().strip()
        with self.assertRaisesRegex(gate.Blocked, 'unsupported-commit-headers'):
            self.check(head=signed)
        self.git('reset', '--hard', signed)
        self.baseline = {'refs/heads/main': signed}
        head = self.commit('Update source', b'new source')
        self.assertEqual(self.check(), [head])

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

    def test_missing_baseline_scans_more_history(self):
        self.assertEqual(self.check(baseline={'refs/heads/main': '1' * 40}), [self.base])

    def test_invalid_private_rule_fails_closed(self):
        self.git('config', 'publication.denyPattern', '[')
        Path('body').write_text('Fix parser.')
        with patch.object(gate, 'run') as transport, self.assertRaises(gate.Blocked) as error:
            gate.api_publish('comment', 1, None, 'body', None)
        transport.assert_not_called()
        self.assertEqual(str(error.exception), 'invalid-policy: field-body')

    def test_policy_read_failure_blocks_publication_and_redacts_output(self):
        Path('body').write_text('Fix parser.')
        result = subprocess.CompletedProcess([], 99, PRIVATE.encode(), PRIVATE.encode())
        with patch.object(gate.subprocess, 'run', return_value=result), \
                patch.object(gate, 'run') as transport, self.assertRaises(gate.Blocked) as error:
            gate.api_publish('comment', 1, None, 'body', None)
        transport.assert_not_called()
        self.assertEqual(str(error.exception), 'policy-read: field-body')
        self.assertNotIn(PRIVATE, str(error.exception))

    def test_bundle_git_fixture_models_unconfigured_private_patterns(self):
        source = (ROOT / 'tests/bundle.sh').read_text()
        marker = """cat >"$fake_bin/git" <<'EOF'\n"""
        mock = source.split(marker, 1)[1].split('\nEOF\n', 1)[0]
        directory = self.root / 'bin'
        directory.mkdir()
        executable = directory / 'git'
        executable.write_text(mock + '\n')
        executable.chmod(0o755)
        with patch.dict(os.environ, {'PATH': str(directory) + os.pathsep + os.environ['PATH']}):
            self.assertEqual(gate.scan(b'clean', 'fixture'), 'clean')
            query = subprocess.run(['git', 'config', '--get-all', 'publication.denyPattern'],
                                   capture_output=True)
            self.assertEqual((query.returncode, query.stdout, query.stderr), (1, b'', b''))
            unsupported = subprocess.run(['git', 'config', '--get-all', 'unrelated.setting'],
                                         capture_output=True)
            self.assertEqual(unsupported.returncode, 99)

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

    def test_unfetched_unrelated_remote_ref_is_omitted(self):
        remote = self.root / 'remote.git'
        self.git('init', '--bare', '-q', str(remote))
        self.git('remote', 'add', 'origin', str(remote))
        self.git('push', 'origin', self.base + ':refs/heads/main')
        tree = self.git('rev-parse', 'HEAD^{tree}').decode().strip()
        other = self.git('-C', str(remote), 'commit-tree', tree, data=b'Unrelated\n').decode().strip()
        self.git('-C', str(remote), 'update-ref', 'refs/pull/99/head', other)
        head = self.commit('Fix parser')
        baseline = gate.advertised('origin')
        self.assertIn('refs/pull/99/head', baseline)
        self.assertEqual(self.check(baseline=baseline), [head])

    def test_stale_tracking_ref_cannot_hide_new_content(self):
        remote = self.root / 'remote.git'
        self.git('init', '--bare', '-q', str(remote))
        self.git('remote', 'add', 'origin', str(remote))
        self.git('push', 'origin', self.base + ':refs/heads/main')
        head = self.commit('Add file', PRIVATE.encode())
        self.git('update-ref', 'refs/remotes/origin/main', head)
        with self.assertRaises(gate.Blocked) as error:
            gate.pre_push(str(remote), f'HEAD {head} refs/heads/topic ' + '0' * 40)
        self.assertNotIn(PRIVATE, str(error.exception))

    def test_missing_outgoing_history_fails_before_exclusions(self):
        blob = self.git('rev-parse', self.base + ':file').decode().strip()
        head = self.commit('Fix parser', b'new code')
        Path('.git/objects', blob[:2], blob[2:]).unlink()
        with self.assertRaisesRegex(gate.Blocked, '^command-failed: publication$'):
            self.check(head=head)

    def test_pr_checks_fixed_destination_despite_origin(self):
        self.git('remote', 'add', 'origin', 'https://github.com/fixture/unrelated.git')
        Path('title').write_text('Fix parser')
        Path('body').write_text('Fix parser.')
        with patch.object(gate, 'advertised', return_value={}) as advertised, \
                patch.object(gate, 'run') as send, self.assertRaises(gate.Blocked) as error:
            gate.api_publish('pr', None, 'title', 'body', 'topic', self.base)
        advertised.assert_called_once_with('https://github.com/xunzhou/codex-nix.git')
        send.assert_not_called()
        self.assertEqual(str(error.exception), 'head-mismatch: pr')
        with patch.object(gate, 'advertised', return_value={'refs/heads/topic': self.base}) as advertised, \
                patch.object(gate, 'run', return_value=b'{}') as send, \
                contextlib.redirect_stdout(io.StringIO()):
            gate.api_publish('pr', None, 'title', 'body', 'topic', self.base)
        advertised.assert_called_once_with('https://github.com/xunzhou/codex-nix.git')
        self.assertIn('repos/xunzhou/codex-nix/pulls', send.call_args.args)

    def test_duplicate_json_keys_rejected_and_redacted(self):
        cases = [
            ('{"body":' + json.dumps(PRIVATE) + ',"body":"clean"}').encode(),
            ('{"' + PRIVATE + '":1,"' + PRIVATE + '":2}').encode(),
            b'{"body":{"nested":1,"nested":2}}',
        ]
        for raw in cases:
            with self.subTest(raw_length=len(raw)):
                with self.assertRaises(gate.Blocked) as error:
                    gate.decode_payload(raw)
                self.assertEqual(str(error.exception), 'duplicate-json-key: payload')
                result = subprocess.run(['python3', str(ROOT / 'scripts/publication_gate.py'),
                                         'payload'], input=raw, capture_output=True)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, b'')
                self.assertNotIn(PRIVATE.encode(), result.stderr)
        with self.assertRaisesRegex(gate.Blocked, 'duplicate-json-key'):
            gate.payload_check({'body': 'clean'}, cases[0])

    def test_payload_types_routes_and_error_redaction(self):
        invalid = [({'body': True}, 'comment'), ({'title': None}, 'pr'),
                   ({'head': False}, 'pr'), ({'draft': 'true'}, 'release'),
                   ({'prerelease': 1}, 'release'), ({'name': []}, 'release'),
                   ({'make_latest': PRIVATE}, 'release'), ({'title': 'clean'}, 'release'),
                   ({PRIVATE: 'clean'}, 'comment'), ({'body': 'clean'}, PRIVATE)]
        for data, route in invalid:
            with self.assertRaises(gate.Blocked) as error:
                gate.payload_check(data, route=route)
            self.assertNotIn(PRIVATE, str(error.exception))
        for data in ({'draft': True, 'prerelease': False}, {'make_latest': 'true'},
                     {'make_latest': 'legacy'}, {'make_latest': 'false'}):
            with contextlib.redirect_stdout(io.StringIO()):
                gate.payload_check(data)

    def test_malformed_json_diagnostic_redacted(self):
        for raw in (PRIVATE.encode(), b'\xff', b'{"body":NaN}',
                    '{"body":"clean"}'.encode('utf-16')):
            with self.assertRaises(gate.Blocked) as error:
                gate.decode_payload(raw)
            self.assertNotIn(PRIVATE, str(error.exception))

    def test_unsupported_commit_headers_rejected(self):
        raw = self.git('cat-file', 'commit', self.base)
        headers, message = raw.split(b'\n\n', 1)
        additions = [b'encoding UTF-8', b'gpgsig synthetic\n continuation',
                     b'mergetag synthetic', b'x-private synthetic value',
                     headers.splitlines()[-1]]
        for addition in additions:
            value = self.git('hash-object', '-t', 'commit', '-w', '--stdin',
                             data=headers + b'\n' + addition + b'\n\n' + message).decode().strip()
            with self.assertRaises(gate.Blocked) as error:
                self.check(head=value)
            self.assertEqual(str(error.exception), 'unsupported-commit-headers: ' + value)
            self.assertNotIn('synthetic value', str(error.exception))

    def test_supported_merge_headers(self):
        tree = self.git('rev-parse', self.base + '^{tree}').decode().strip()
        other = self.git('commit-tree', tree, data=b'Other root\n').decode().strip()
        merge = self.git('commit-tree', tree, '-p', self.base, '-p', other,
                         data=b'Merge changes\n').decode().strip()
        self.assertEqual(set(self.check(head=merge)), {other, merge})

    def test_grafts_rejected_with_redacted_diagnostic(self):
        Path('.git/info/grafts').write_text(PRIVATE)
        with self.assertRaises(gate.Blocked) as error:
            self.check()
        self.assertEqual(str(error.exception), 'grafts: repository')

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
