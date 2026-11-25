#!/usr/bin/env python3
"""Exercise the stable CLI against a local origin and read-only API fixtures."""
import json
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
BOT = 'github-actions[bot]'
EMAIL = '41898282+github-actions[bot]@users.noreply.github.com'


def release(tag, prerelease=False, draft=False):
    return dict(tag_name=tag, prerelease=prerelease, draft=draft)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.origin = self.root / 'origin.git'
        self.repo.mkdir()
        for path in ('scripts/probe-update.py', 'scripts/publication_gate.py', '.githooks/pre-push', 'build.json'):
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / path, target)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        gh = self.bin / 'gh'
        gh.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
assert sys.argv[1:4] == ['api', '--method', 'GET'], 'unexpected API write'
fixtures = json.loads(Path(os.environ['PROBE_FIXTURE']).read_text())
path = sys.argv[4]
with Path(os.environ['PROBE_CALLS']).open('a') as stream:
    stream.write(path + '\\n')
if path not in fixtures:
    sys.exit(1)
value = fixtures[path]
if isinstance(value, dict) and '__http_error' in value:
    print('HTTP ' + str(value['__http_error']), file=sys.stderr)
    fixtures[path] = value['then']
    Path(os.environ['PROBE_FIXTURE']).write_text(json.dumps(fixtures))
    sys.exit(1)
print(json.dumps(value))
''')
        gh.chmod(0o755)
        self.env = {**os.environ, 'PATH': str(self.bin) + ':' + os.environ['PATH'],
                    'GIT_AUTHOR_NAME': BOT, 'GIT_COMMITTER_NAME': BOT,
                    'GIT_AUTHOR_EMAIL': EMAIL, 'GIT_COMMITTER_EMAIL': EMAIL,
                    'PROBE_FIXTURE': str(self.root / 'fixture.json'),
                    'PROBE_CALLS': str(self.root / 'calls'), 'GITHUB_RUN_ID': '101',
                    'GITHUB_REPOSITORY': 'xunzhou/codex-nix', 'GITHUB_REF': 'refs/heads/main'}
        self.git('init', '-q')
        self.git('add', '.')
        self.git('commit', '-qm', 'Fixture base')
        subprocess.run(['git', 'init', '--bare', '-q', str(self.origin)], check=True)
        self.git('remote', 'add', 'origin', str(self.origin))
        self.git('push', '-q', 'origin', 'HEAD:refs/heads/main')
        subprocess.run([sys.executable, 'scripts/publication_gate.py', 'install'], cwd=self.repo,
                       env=self.env, capture_output=True, check=True)
        self.fixtures = {'repos/openai/codex/releases?per_page=100': [release('rust-v0.173.0')],
                         'repos/xunzhou/codex-nix/releases?per_page=100&page=1': []}
        self.fixtures['repos/openai/codex/releases/tags/rust-v0.173.0'] = release('rust-v0.173.0')
        self.run_status()
        self.save()

    def run_status(self, status='in_progress', conclusion=None, minutes=60, run=101):
        self.fixtures[f'repos/xunzhou/codex-nix/actions/runs/{run}'] = dict(id=run,
            repository={'full_name': 'xunzhou/codex-nix'}, path='.github/workflows/update.yml',
            head_branch='main', event='schedule', status=status, conclusion=conclusion,
            updated_at=(datetime.now(timezone.utc)-timedelta(minutes=minutes)).isoformat())
        self.save()

    def save(self):
        (self.root / 'fixture.json').write_text(json.dumps(self.fixtures))

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.repo, env=self.env, stderr=subprocess.PIPE).decode()

    def run_probe(self, *args, success=True):
        result = subprocess.run([sys.executable, 'scripts/probe-update.py', *args],
                                cwd=self.repo, env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0 if success else 1, result.stderr)
        return json.loads(result.stdout) if success else result

    def test_duplicate_delivery_claims_once_with_real_hook_and_origin(self):
        for expected in (True, False, False):
            result = self.run_probe('--event', 'schedule', '--claim')
            self.assertEqual(result['build'], expected)
        refs = self.git('ls-remote', '--refs', 'origin').splitlines()
        self.assertEqual(len(refs), 2)
        self.assertTrue(any('-attempt-1' in line for line in refs))
        self.assertEqual(len(set(line.split()[0] for line in refs)), 2)
        tag = next(line.split()[0] for line in refs if '-attempt-1' in line)
        self.assertEqual(self.git('rev-parse', tag + '^{tree}'), self.git('rev-parse', 'HEAD^{tree}'))

    def test_concurrent_deliveries_cannot_start_two_builds(self):
        processes = [subprocess.Popen([sys.executable, 'scripts/probe-update.py',
                     '--event', 'schedule', '--claim'], cwd=self.repo, env=self.env,
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(3)]
        builds = 0
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertIn(process.returncode, (0, 1), stderr)
            if process.returncode == 0:
                builds += json.loads(stdout)['build']
        self.assertEqual(builds, 1)

    def test_uncertain_write_fails_closed_then_reconciles_without_retry(self):
        real_git = shutil.which('git')
        wrapper = self.bin / 'git'
        wrapper.write_text(f"#!/bin/sh\n{real_git} \"$@\"\nresult=$?\nif [ \"$1\" = push ]; then exit 1; fi\nexit \"$result\"\n")
        wrapper.chmod(0o755)
        self.run_probe('--event', 'schedule', '--claim', success=False)
        self.assertEqual(self.run_probe('--event', 'schedule', '--claim')['reason'], 'attempt-still-active')
        self.assertEqual(len(self.git('ls-remote', '--refs', 'origin').splitlines()), 2)
        wrapper.unlink()
        self.run_status('completed', 'failure')
        self.env['GITHUB_RUN_ID'] = '102'
        self.assertTrue(self.run_probe('--event', 'schedule', '--claim')['build'])

    def test_already_published_never_claims_or_builds_even_on_retry(self):
        self.fixtures['repos/xunzhou/codex-nix/releases?per_page=100&page=1'] = [
            release('bundle-codex-v0.173.0-' + 'a' * 16 + '-' + 'b' * 16)]
        self.save()
        self.assertEqual(self.run_probe('--event', 'schedule', '--claim')['reason'], 'already-published')
        self.assertFalse(self.run_probe('--event', 'workflow_dispatch', '--version', '0.173.0', '--retry', '--claim')['build'])
        self.assertEqual(len(self.git('ls-remote', '--refs', 'origin').splitlines()), 1)

    def test_packaging_push_builds_without_api_or_attempt_marker(self):
        self.fixtures = {}
        self.save()
        result = self.run_probe('--event', 'push', '--claim')
        self.assertTrue(result['build'])
        self.assertEqual(result['reason'], 'packaging-push')
        self.assertFalse((self.root / 'calls').exists())
        self.assertEqual(len(self.git('ls-remote', '--refs', 'origin').splitlines()), 1)

    def test_read_only_preflight_and_explicit_failed_attempt_retry(self):
        self.assertTrue(self.run_probe('--event', 'schedule')['build'])
        self.assertEqual(len(self.git('ls-remote', '--refs', 'origin').splitlines()), 1)
        self.run_probe('--event', 'schedule', '--claim')
        self.run_status('completed', 'failure')
        self.env['GITHUB_RUN_ID'] = '102'
        self.assertTrue(self.run_probe('--event', 'workflow_dispatch', '--version', '0.173.0', '--retry', '--claim')['build'])
        self.run_probe('--event', 'workflow_dispatch', '--retry', success=False)
        self.run_probe('--event', 'schedule', '--retry', success=False)

    def test_cancelled_run_requires_explicit_retry(self):
        self.run_probe('--event', 'schedule', '--claim')
        self.run_status('completed', 'cancelled')
        self.env['GITHUB_RUN_ID'] = '102'
        self.assertEqual(self.run_probe('--event', 'schedule', '--claim')['reason'], 'cancelled-requires-retry')
        self.assertEqual(len(self.git('ls-remote', '--refs', 'origin').splitlines()), 2)
        retry = self.run_probe('--event', 'workflow_dispatch', '--version', '0.173.0', '--retry', '--claim')
        self.assertEqual(retry['reason'], 'explicit-retry')

    def test_success_without_a_release_requires_review(self):
        self.run_probe('--event', 'schedule', '--claim')
        self.run_status('completed', 'success')
        self.assertEqual(self.run_probe('--event', 'schedule', '--claim')['reason'], 'attempt-needs-review')

    def test_failure_recovers_once_after_cooldown_then_requires_retry(self):
        self.run_probe('--event', 'schedule', '--claim')
        self.run_status('completed', 'failure', minutes=5)
        self.assertEqual(self.run_probe('--event', 'schedule')['reason'], 'recovery-cooldown')
        self.run_status('completed', 'failure')
        self.env['GITHUB_RUN_ID'] = '102'
        self.assertEqual(self.run_probe('--event', 'schedule', '--claim')['reason'], 'bounded-recovery')
        self.run_status('completed', 'failure', run=102)
        self.assertEqual(self.run_probe('--event', 'schedule')['reason'], 'automatic-attempts-exhausted')
        self.env['GITHUB_RUN_ID'] = '103'
        self.assertTrue(self.run_probe('--event', 'workflow_dispatch', '--version', '0.173.0', '--retry', '--claim')['build'])

    def test_repaired_main_gets_new_budget_and_exact_base(self):
        self.run_probe('--event', 'schedule', '--claim')
        self.run_status('completed', 'failure')
        self.git('commit', '--allow-empty', '-qm', 'Repair fixture')
        self.git('push', 'origin', 'HEAD:refs/heads/main')
        result = self.run_probe('--event', 'schedule', '--claim')
        self.assertEqual(result['reason'], 'new-stable')
        self.assertEqual(result['base'], self.git('rev-parse', 'HEAD').strip())

    def test_running_unknown_or_missing_run_cannot_be_retried(self):
        self.run_probe('--event', 'schedule', '--claim')
        for status in ('queued', 'in_progress', 'waiting', 'unknown'):
            self.run_status(status)
            self.assertFalse(self.run_probe('--event', 'workflow_dispatch', '--version', '0.173.0', '--retry')['build'])
        self.run_status('completed', 'action_required')
        self.assertEqual(self.run_probe('--event', 'schedule')['reason'], 'attempt-needs-review')
        del self.fixtures['repos/xunzhou/codex-nix/actions/runs/101']
        self.save()
        self.run_probe('--event', 'schedule', success=False)

    def test_outputs_bind_the_prepare_checkout_to_observed_base(self):
        output = self.root / 'outputs'
        result = self.run_probe('--event', 'schedule', '--output', str(output))
        self.assertEqual(output.read_text(), f'build=true\nversion=0.173.0\nbase={result["base"]}\n')
        self.assertEqual(result['base'], self.git('rev-parse', 'HEAD').strip())

    def test_another_workflows_status_cannot_authorize_recovery(self):
        self.run_probe('--event', 'schedule', '--claim')
        self.run_status('completed', 'failure')
        self.fixtures['repos/xunzhou/codex-nix/actions/runs/101']['path'] = '.github/workflows/check.yml'
        self.save()
        self.run_probe('--event', 'schedule', '--claim', success=False)
        self.assertEqual(len(self.git('ls-remote', '--refs', 'origin').splitlines()), 2)

    def test_wrong_repository_or_branch_cannot_claim(self):
        for key, value in [('GITHUB_REF', 'refs/heads/topic'), ('GITHUB_REPOSITORY', 'example/fork')]:
            original = self.env[key]
            self.env[key] = value
            self.run_probe('--event', 'schedule', '--claim', success=False)
            self.env[key] = original

    def test_explicit_older_stable_still_supported_but_alpha_rejected(self):
        self.fixtures['repos/openai/codex/releases/tags/rust-v0.171.0'] = release('rust-v0.171.0')
        self.save()
        self.assertEqual(self.run_probe('--event', 'workflow_dispatch', '--version', '0.171.0')['version'], '0.171.0')
        for version in ('0.174.0-alpha.1', '01.2.3', '../bad'):
            self.run_probe('--event', 'workflow_dispatch', '--version', version, success=False)

    def test_transient_metadata_get_retries_but_denial_does_not(self):
        endpoint = 'repos/openai/codex/releases/tags/rust-v0.173.0'
        self.fixtures[endpoint] = {'__http_error': 503, 'then': release('rust-v0.173.0')}
        self.save()
        self.assertTrue(self.run_probe('--event', 'workflow_dispatch', '--version', '0.173.0')['build'])
        self.assertEqual((self.root / 'calls').read_text().count(endpoint), 2)
        (self.root / 'calls').unlink()
        self.fixtures[endpoint] = {'__http_error': 403, 'then': release('rust-v0.173.0')}
        self.save()
        self.run_probe('--event', 'workflow_dispatch', '--version', '0.173.0', success=False)
        self.assertEqual((self.root / 'calls').read_text().count(endpoint), 1)

    def test_metadata_failure_or_prerelease_fails_closed(self):
        for data in ({}, {'repos/openai/codex/releases?per_page=100': [release('rust-v0.173.0', prerelease=True)]}):
            self.fixtures = data
            self.save()
            self.run_probe('--event', 'schedule', '--claim', success=False)
            self.assertEqual(len(self.git('ls-remote', '--refs', 'origin').splitlines()), 1)

    def test_denied_marker_push_does_not_authorize_build_or_retry(self):
        hook = self.origin / 'hooks/pre-receive'
        hook.write_text('#!/bin/sh\nexit 1\n')
        hook.chmod(0o755)
        self.run_probe('--event', 'schedule', '--claim', success=False)
        self.assertEqual(len(self.git('ls-remote', '--refs', 'origin').splitlines()), 1)

    def test_unpublished_checkout_cannot_be_used_as_attempt_marker(self):
        self.git('commit', '--allow-empty', '-qm', 'Unpublished fixture')
        self.run_probe('--event', 'schedule', '--claim', success=False)
        self.assertEqual(len(self.git('ls-remote', '--refs', 'origin').splitlines()), 1)

    def test_uninstalled_gate_blocks_marker(self):
        self.git('config', '--unset', 'core.hooksPath')
        self.run_probe('--event', 'schedule', '--claim', success=False)
        self.assertEqual(len(self.git('ls-remote', '--refs', 'origin').splitlines()), 1)

    def test_highest_numeric_stable_ignores_latest_and_nonstable(self):
        self.fixtures['repos/openai/codex/releases/latest'] = release('rust-v0.172.0')
        self.fixtures['repos/openai/codex/releases?per_page=100'] = [
            release('rust-v0.173.0'), release('rust-v0.99.0'),
            release('rust-v0.174.0-alpha.1', prerelease=True),
            release('rust-v0.175.0', draft=True), release('rust-v0.176.0', prerelease=True),
            None, {}, release('rust-v01.200.0')]
        self.save()
        for event in ('schedule', 'workflow_dispatch'):
            self.assertEqual(self.run_probe('--event', event)['version'], '0.173.0')
        self.assertNotIn('/latest', (self.root / 'calls').read_text())

    def test_small_pages_cross_boundary_and_stop_at_first_hundred(self):
        endpoint = 'repos/openai/codex/releases?per_page=100'
        self.fixtures[endpoint] = {'__http_error': 504, 'then': []}
        for page in range(1, 21):
            self.fixtures[f'repos/openai/codex/releases?per_page=5&page={page}'] = [
                release('rust-v0.171.0')] * 5
        self.fixtures['repos/openai/codex/releases?per_page=5&page=2'][0] = release('rust-v0.173.0')
        self.fixtures['repos/openai/codex/releases?per_page=5&page=21'] = [release('rust-v0.200.0')]
        self.save()
        self.assertEqual(self.run_probe('--event', 'schedule')['version'], '0.173.0')
        calls = (self.root / 'calls').read_text().splitlines()
        self.assertEqual(len(calls), 22)  # one large failure, twenty pages, downstream
        self.assertNotIn('repos/openai/codex/releases?per_page=5&page=21', calls)

    def test_incomplete_fallback_does_not_claim_from_earlier_page(self):
        self.fixtures['repos/openai/codex/releases?per_page=100'] = {'__http_error': 504, 'then': []}
        self.fixtures['repos/openai/codex/releases?per_page=5&page=1'] = [release('rust-v0.173.0')] * 5
        self.save()
        self.run_probe('--event', 'schedule', '--claim', success=False)
        self.assertEqual(len(self.git('ls-remote', '--refs', 'origin').splitlines()), 1)
        self.assertFalse(any('codex-nix/releases' in p for p in (self.root / 'calls').read_text().splitlines()))

    def test_bad_metadata_and_denial_never_fall_back(self):
        endpoint = 'repos/openai/codex/releases?per_page=100'
        for value in ({}, None, 'wrong', [release('rust-v0.173.0')] * 101,
                      {'__http_error': 403, 'then': []}):
            self.fixtures[endpoint] = value
            self.save()
            (self.root / 'calls').unlink(missing_ok=True)
            self.run_probe('--event', 'schedule', '--claim', success=False)
            self.assertEqual((self.root / 'calls').read_text().splitlines(), [endpoint])
            self.assertEqual(len(self.git('ls-remote', '--refs', 'origin').splitlines()), 1)

    def test_older_listing_cannot_downgrade_pin(self):
        self.fixtures['repos/openai/codex/releases?per_page=100'] = [release('rust-v0.171.0')]
        self.save()
        self.assertEqual(self.run_probe('--event', 'schedule', '--claim')['reason'], 'older-than-pin')
        self.assertEqual(len((self.root / 'calls').read_text().splitlines()), 1)
        self.assertEqual(len(self.git('ls-remote', '--refs', 'origin').splitlines()), 1)

    def test_same_published_version_has_only_two_gets_and_false_job_output(self):
        self.fixtures['repos/openai/codex/releases?per_page=100'] = [release('rust-v0.172.0')]
        self.fixtures['repos/xunzhou/codex-nix/releases?per_page=100&page=1'] = [
            release('bundle-codex-v0.172.0-' + 'a' * 16 + '-' + 'b' * 16)]
        self.save()
        output = self.root / 'noop-output'
        self.assertFalse(self.run_probe('--event', 'schedule', '--claim', '--output', str(output))['build'])
        self.assertIn('build=false\n', output.read_text())
        self.assertEqual(len((self.root / 'calls').read_text().splitlines()), 2)
        self.assertEqual(len(self.git('ls-remote', '--refs', 'origin').splitlines()), 1)

    def test_large_timeout_uses_small_page_without_retrying_large_request(self):
        import importlib.util
        from unittest.mock import patch
        spec = importlib.util.spec_from_file_location('probe_timeout', ROOT / 'scripts/probe-update.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        small = [release('rust-v0.173.0')]
        with patch.object(module.subprocess, 'run', side_effect=[
                subprocess.TimeoutExpired('gh', 120),
                subprocess.CompletedProcess('gh', 0, json.dumps(small), '')]) as run:
            self.assertEqual(module.upstream_releases(), small)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[1].args[0][-1],
                         'repos/openai/codex/releases?per_page=5&page=1')

    def test_wrong_shape_small_page_fails_closed(self):
        self.fixtures['repos/openai/codex/releases?per_page=100'] = {'__http_error': 504, 'then': []}
        self.fixtures['repos/openai/codex/releases?per_page=5&page=1'] = {}
        self.save()
        self.run_probe('--event', 'schedule', '--claim', success=False)
        self.assertEqual(len((self.root / 'calls').read_text().splitlines()), 2)
        self.assertEqual(len(self.git('ls-remote', '--refs', 'origin').splitlines()), 1)

    def test_draft_release_is_not_publication(self):
        self.fixtures['repos/xunzhou/codex-nix/releases?per_page=100&page=1'] = [
            release('bundle-codex-v0.173.0-' + 'a' * 16 + '-' + 'b' * 16, draft=True)]
        self.save()
        self.assertTrue(self.run_probe('--event', 'schedule')['build'])


if __name__ == '__main__':
    unittest.main()
