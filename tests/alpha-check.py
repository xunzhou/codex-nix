#!/usr/bin/env python3
"""Run the alpha entry point with local archives and denied publication tools."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
TAG = 'rust-v0.174.0-alpha.4'
HOST = 'codex-code-mode-host-x86_64-unknown-linux-musl'


class AlphaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        (self.repo / 'scripts').mkdir(parents=True)
        for name in ('check-alpha.py', 'probe-update.py', 'rebase-patches.py'):
            shutil.copy2(ROOT / 'scripts' / name, self.repo / 'scripts' / name)
        (self.repo / 'patches').mkdir()
        (self.repo / 'patches/change.patch').write_text(
            'diff --git a/value b/value\n--- a/value\n+++ b/value\n@@ -1 +1 @@\n-old\n+new\n')
        self.manifest = json.dumps(dict(default_version='0.172.0', releases={
            '0.172.0': dict(patches=['change.patch'])}))
        (self.repo / 'build.json').write_text(self.manifest)
        self.archive = self.root / 'source.tar.gz'
        self.make_source('0.174.0-alpha.4')
        self.host = self.root / 'host.tar.gz'
        self.host.write_bytes(b'not a verified host')
        self.data = dict(tag=TAG, url=f'https://github.com/openai/codex/releases/download/{TAG}/{HOST}.tar.gz',
                         sha256='0' * 64)
        self.metadata = self.root / 'metadata.json'
        self.metadata.write_text(json.dumps(self.data))
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        curl = self.bin / 'curl'
        curl.write_text(f'''#!/usr/bin/env python3
import os, shutil, sys
from pathlib import Path
assert 'GH_TOKEN' not in os.environ and 'GITHUB_TOKEN' not in os.environ
url = sys.argv[-1]
assert url.startswith('https://github.com/openai/codex/')
with Path({str(self.root / 'downloads')!r}).open('a') as stream: stream.write(url + '\\n')
shutil.copyfile({str(self.archive)!r} if '/archive/' in url else {str(self.host)!r}, sys.argv[sys.argv.index('--output') + 1])
''')
        curl.chmod(0o755)
        gh = self.bin / 'gh'
        gh.write_text('#!/bin/sh\nexit 97\n')
        gh.chmod(0o755)
        self.env = {**os.environ, 'PATH': str(self.bin) + ':' + os.environ['PATH'],
                    'GH_TOKEN': 'fixture-only', 'GITHUB_TOKEN': 'fixture-only'}

    def make_source(self, version):
        with tarfile.open(self.archive, 'w:gz') as archive:
            for name, data in [('source/value', b'old\n'), ('source/codex-rs/Cargo.toml',
                                 f'[workspace.package]\nversion = "{version}"\n'.encode())]:
                item = tarfile.TarInfo(name)
                item.size = len(data)
                archive.addfile(item, io.BytesIO(data))

    def invoke(self, *args, clean=False):
        env = self.env.copy()
        if clean:
            env.pop('GH_TOKEN')
            env.pop('GITHUB_TOKEN')
        return subprocess.run([sys.executable, str(self.repo / 'scripts/check-alpha.py'), *args],
                              cwd=self.repo, env=env, capture_output=True, text=True)

    def test_metadata_entry_selects_numbered_published_alpha_only(self):
        releases = []
        for tag, draft, prerelease in [('rust-v0.174.0-alpha.1', False, True),
                                      (TAG, False, True),
                                      ('rust-v0.174.0-alpha.9', True, True),
                                      ('rust-v0.175.0', False, False)]:
            releases.append(dict(tag_name=tag, draft=draft, prerelease=prerelease,
                assets=[dict(name=HOST + '.tar.gz', digest='sha256:' + 'a' * 64,
                browser_download_url=f'https://github.com/openai/codex/releases/download/{tag}/{HOST}.tar.gz')]))
        gh = self.bin / 'gh'
        gh.write_text('#!/usr/bin/env python3\nimport sys\n'
                      + "assert sys.argv[1:4] == ['api', '--method', 'GET']\n"
                      + 'print(' + repr(json.dumps(releases)) + ')\n')
        output = self.root / 'selected.json'
        result = self.invoke('metadata', '--output', str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(output.read_text())['tag'], TAG)
        result = self.invoke('metadata', '--tag', 'rust-v0.174.0', '--output', str(output))
        self.assertEqual(result.returncode, 1)

    def test_validate_replays_but_rejects_unverified_runtime_without_publication(self):
        result = self.invoke('validate', '--metadata', str(self.metadata))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('alpha replay: passed', result.stdout)
        self.assertIn('alpha runtime: failed', result.stdout)
        self.assertIn('alpha host digest mismatch', result.stdout)
        self.assertEqual(len((self.root / 'downloads').read_text().splitlines()), 2)
        self.assertEqual((self.repo / 'build.json').read_text(), self.manifest)
        self.assertEqual(sorted(path.name for path in (self.repo / 'patches').iterdir()), ['change.patch'])

    def test_runtime_still_checked_after_replay_failure(self):
        self.make_source('0.173.0-alpha.1')
        result = self.invoke('validate', '--metadata', str(self.metadata))
        self.assertEqual(result.returncode, 1)
        self.assertIn('alpha replay: failed', result.stdout)
        self.assertIn('alpha runtime: failed', result.stdout)
        self.assertEqual(len((self.root / 'downloads').read_text().splitlines()), 2)

    def test_real_patch_application_entry_point(self):
        scratch = self.root / 'replay'
        scratch.mkdir()
        result = self.invoke('replay', '--metadata', str(self.metadata), '--scratch', str(scratch), clean=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((scratch / 'source/source/value').read_text(), 'new\n')
        self.assertEqual(json.loads(result.stdout)['source_sha256'], hashlib.sha256(self.archive.read_bytes()).hexdigest())

    def test_invalid_tag_url_or_digest_stops_before_download(self):
        for field, value in [('tag', 'rust-v0.174.0'), ('tag', '../bad'),
                             ('url', 'https://example.invalid/host'), ('sha256', 'bad')]:
            self.metadata.write_text(json.dumps({**self.data, field: value}))
            result = self.invoke('validate', '--metadata', str(self.metadata))
            self.assertEqual(result.returncode, 1)
            self.assertFalse((self.root / 'downloads').exists())

    def test_verified_digest_does_not_allow_a_symlink_host(self):
        with tarfile.open(self.host, 'w:gz') as archive:
            item = tarfile.TarInfo(HOST)
            item.type = tarfile.SYMTYPE
            item.linkname = '/bin/sh'
            archive.addfile(item)
        self.data['sha256'] = hashlib.sha256(self.host.read_bytes()).hexdigest()
        self.metadata.write_text(json.dumps(self.data))
        scratch = self.root / 'runtime'
        scratch.mkdir()
        result = self.invoke('runtime', '--metadata', str(self.metadata), '--scratch', str(scratch), clean=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('unexpected alpha host archive inventory', result.stderr)

    def test_only_fixed_bubblewrap_runtime_command_is_elevated(self):
        with tarfile.open(self.host, 'w:gz') as archive:
            item = tarfile.TarInfo(HOST)
            item.size = 4
            archive.addfile(item, io.BytesIO(b'host'))
        self.data['sha256'] = hashlib.sha256(self.host.read_bytes()).hexdigest()
        spec = importlib.util.spec_from_file_location('alpha', self.repo / 'scripts/check-alpha.py')
        alpha = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(alpha)
        scratch = self.root / 'runtime'
        scratch.mkdir()
        with patch.object(alpha, 'download', side_effect=lambda url, path: shutil.copyfile(self.host, path)), \
                patch.object(alpha.subprocess, 'run') as run, patch.object(Path, 'is_file', return_value=True):
            alpha.runtime(self.data, scratch, sudo_runtime=True)
        command = run.call_args.args[0]
        self.assertEqual(command[:2], [sys.executable, str(self.repo / 'tests/static-runtime.py')])
        self.assertEqual(command[2:6], ['/usr/bin/sudo', '-n', '--', '/usr/bin/bwrap'])
        self.assertEqual(command[-3:], ['/app/codex-code-mode-host', '--listen', 'grpc+stdio://'])
        for flag in ('--unshare-all', '--clearenv', '--ro-bind'):
            self.assertIn(flag, command)
        self.assertEqual(run.call_args.kwargs['timeout'], 45)

    def test_workflow_is_read_only_and_has_no_publication_or_update_route(self):
        workflow = (ROOT / '.github/workflows/alpha.yml').read_text()
        self.assertIn('permissions:\n  contents: read', workflow)
        self.assertIn('persist-credentials: false', workflow)
        self.assertEqual(workflow.count('GH_TOKEN:'), 1)
        validate = workflow.split('name: Replay patches', 1)[1]
        self.assertNotIn('TOKEN', validate)
        self.assertNotIn('sudo ', validate)
        self.assertIn('--sudo-runtime', validate)
        for forbidden in ('contents: write', 'workflow_call:', 'secrets:', 'publish-',
                          'update.sh', 'update-manifest', 'git push', 'upload-artifact',
                          'build-release', 'route-release', 'repository_dispatch'):
            self.assertNotIn(forbidden, workflow)
        code = (ROOT / 'scripts/check-alpha.py').read_text()
        for forbidden in ('publish-release', 'update.sh', 'update-manifest', "'push'", 'upload-artifact'):
            self.assertNotIn(forbidden, code)
        for required in ('--unshare-all', '--clearenv', 'grpc+stdio://', 'tests/static-runtime.py'):
            self.assertIn(required, code)


if __name__ == '__main__':
    unittest.main()
