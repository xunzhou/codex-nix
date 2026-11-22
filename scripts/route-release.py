#!/usr/bin/env python3
"""Select one release build and import only that attempt's verified artifacts."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import zipfile


class Unavailable(Exception):
    """The service cannot start or observe the build."""


class IntegrityError(Exception):
    """Build or artifact verification failed."""


class Actions:
    def __init__(self, repository, token):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
            raise Unavailable()
        self.root = f'https://api.github.com/repos/{repository}'
        self.headers = {'Authorization': f'Bearer {token}',
                        'Accept': 'application/vnd.github+json',
                        'X-GitHub-Api-Version': '2022-11-28'}

    def request(self, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = Request(self.root + path, data=data, headers=self.headers)
        for attempt in range(4):
            try:
                with urlopen(req, timeout=20) as response:
                    payload = response.read()
                    return json.loads(payload) if payload.strip() else {}
            except HTTPError as error:
                if data is not None or error.code not in (404, 429, 500, 502, 503, 504):
                    raise Unavailable() from None
            except (URLError, OSError, ValueError):
                if data is not None:
                    raise Unavailable() from None
            if attempt < 3:
                time.sleep(2 ** attempt)
        raise Unavailable()

    def runners(self):
        return self.request('/actions/runners?per_page=100')['runners']

    def cancel(self, run_id):
        try:
            self.request(f'/actions/runs/{run_id}/cancel', {})
        except Unavailable:
            print('::warning::Could not confirm build cancellation. Its outputs are excluded.')

    def download(self, artifact_id, destination):
        # curl drops authorization when redirected to GitHub's signed storage URL.
        result = subprocess.run([
            'curl', '--silent', '--fail', '--location', '--proto', '=https',
            '--retry', '2', '--max-time', '600',
            '-H', 'Authorization: ' + self.headers['Authorization'],
            '-o', str(destination), f'{self.root}/actions/artifacts/{artifact_id}/zip'],
            stderr=subprocess.DEVNULL)
        if result.returncode:
            raise Unavailable()


def available(runner):
    labels = {label['name'].lower() for label in runner['labels']}
    return (runner['status'] == 'online' and not runner['busy']
            and {'self-hosted', 'linux', 'x64', 'release-build'} <= labels)


def monitor(api, run_id, clock=time.monotonic, sleep=time.sleep):
    start = clock()
    offline_since = None
    while True:
        jobs = api.request(f'/actions/runs/{run_id}/attempts/1/jobs')['jobs']
        job = next((item for item in jobs if item['name'] == 'build'), None)
        if not job or not job.get('runner_id') or job['status'] == 'queued':
            if not job and api.request(f'/actions/runs/{run_id}')['status'] == 'completed':
                raise IntegrityError('Build workflow completed without its expected job.')
            if job and job['status'] == 'completed':
                raise IntegrityError('Build ended before assignment.')
            if clock() - start >= 120:
                raise Unavailable()
        else:
            if job['status'] == 'completed' and job['conclusion'] == 'success':
                return
            failed_steps = [s['name'] for s in job['steps'] if s['conclusion'] == 'failure']
            if failed_steps == ['Download candidate']:
                raise Unavailable()
            if failed_steps:
                raise IntegrityError('Candidate validation or build failed.')
            runner = next((r for r in api.runners() if r['id'] == job['runner_id']), None)
            offline = runner is None or runner['status'] == 'offline'
            if job['status'] == 'completed':
                if offline and job['conclusion'] == 'failure':
                    raise Unavailable()
                raise IntegrityError('Build ended without a successful result.')
            if offline:
                if offline_since is None:
                    offline_since = clock()
                if clock() - offline_since >= 90:
                    raise Unavailable()
            else:
                offline_since = None
        sleep(10)


def select(api, inputs):
    if not any(available(runner) for runner in api.runners()):
        return 0
    reply = api.request('/actions/workflows/release-build.yml/dispatches',
                        {'ref': 'main', 'return_run_details': True, 'inputs': inputs})
    run_id = reply.get('workflow_run_id')
    if type(run_id) is not int or run_id < 1:
        raise Unavailable()
    try:
        monitor(api, run_id)
    except BaseException:
        api.cancel(run_id)
        raise
    return run_id


def unpack(path, kind, directory, version):
    tag = rf'codex-codex-v{re.escape(version)}-[a-f0-9]{{16}}-[a-z0-9]{{16}}'
    patterns = ([tag + suffix for suffix in ('-x86_64-linux\\.nar\\.zst', '-manifest\\.json', '-SHA256SUMS')]
                if kind == 'nix' else [rf'codex-native-{re.escape(version)}-[a-f0-9]{{64}}-linux-x86_64\.tar\.gz'])
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if (len(names) != len(patterns)
                or any(sum(bool(re.fullmatch(p, name)) for name in names) != 1 for p in patterns)):
            raise IntegrityError('Unexpected release file inventory.')
        directory.mkdir()
        for entry in entries:
            if stat.S_IFMT(entry.external_attr >> 16) not in (0, stat.S_IFREG):
                raise IntegrityError('Release artifact contains a special file.')
            with archive.open(entry) as source, (directory / entry.filename).open('xb') as dest:
                shutil.copyfileobj(source, dest)


def import_assets(api, run_id, sha, version, root):
    items = api.request(f'/actions/runs/{run_id}/artifacts')['artifacts']
    expected = {f'{kind}-{sha}': kind for kind in ('nix', 'native')}
    if len(items) != 2 or {a['name'] for a in items} != expected.keys():
        raise IntegrityError('Candidate artifact set is incomplete.')
    with tempfile.TemporaryDirectory(dir=root) as scratch:
        staging = Path(scratch)
        for item in items:
            if (item['expired'] or item['workflow_run']['id'] != run_id
                    or type(item['id']) is not int or item['id'] < 1):
                raise IntegrityError('Invalid candidate artifact provenance.')
            archive = staging / 'artifact.zip'
            api.download(item['id'], archive)
            with archive.open('rb') as data:
                digest = hashlib.file_digest(data, 'sha256').hexdigest()
            if item['digest'] != f'sha256:{digest}':
                raise IntegrityError('Candidate artifact digest mismatch.')
            kind = expected[item['name']]
            unpack(archive, kind, staging / kind, version)
            archive.unlink()
        for kind in ('nix', 'native'):
            (staging / kind).rename(root / f'{kind}-assets')


def main(mode):
    env = os.environ
    if mode == 'select':
        run_id = 0
        if env.get('BUILD_REPOSITORY') and env.get('BUILD_DISPATCH_TOKEN'):
            try:
                api = Actions(env['BUILD_REPOSITORY'], env['BUILD_DISPATCH_TOKEN'])
                inputs = {key: env[key.upper()] for key in
                          ('source_run_id', 'source_base', 'source_sha', 'bundle_sha256', 'version')}
                run_id = select(api, inputs)
            except Unavailable:
                print('::notice::Build service unavailable. Using the hosted build.')
        with open(env['GITHUB_OUTPUT'], 'a') as output:
            print(f'run_id={run_id}', file=output)
    elif mode == 'import':
        api = Actions(env['BUILD_REPOSITORY'], env['BUILD_DISPATCH_TOKEN'])
        import_assets(api, int(env['BUILD_RUN_ID']), env['SOURCE_SHA'], env['VERSION'],
                      Path(env['RUNNER_TEMP']))
    else:
        raise ValueError('Unknown operation.')


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        main(sys.argv[1])
    except (Unavailable, IntegrityError) as exc:
        sys.exit(str(exc) or 'Build service unavailable during artifact transfer.')
