#!/usr/bin/env python3
"""Decide whether to start the stable updater before installing build tools."""
import argparse
import base64
from datetime import datetime, timezone
import os
import json
from pathlib import Path
import re
import subprocess
import sys
import time

REPOSITORY = 'xunzhou/codex-nix'
VERSION = r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)'
STABLE = re.compile('rust-v(' + VERSION + ')')
PUBLISHED = re.compile('bundle-codex-v(' + VERSION + r')-[a-f0-9]{16}-[0-9a-z]{16}')
ROOT = Path(__file__).resolve().parent.parent


def command(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise ValueError('release probe command failed: ' + args[0])
    return result.stdout


def api(path):
    for attempt in range(3):
        result = subprocess.run(['gh', 'api', '--method', 'GET', path],
                                capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            return json.loads(result.stdout)
        # Retry only known transient GET failures. Never retry a denial or write.
        if attempt == 2 or not re.search(r'HTTP (?:500|502|503|504)\b', result.stderr):
            raise ValueError('GitHub release metadata request failed')
        time.sleep(attempt + 1)


def releases(repository):
    # Traverse complete pages; never decide from a failed or truncated listing.
    result = []
    for page in range(1, 101):
        batch = api(f'repos/{repository}/releases?per_page=100&page={page}')
        if not isinstance(batch, list) or len(batch) > 100:
            raise ValueError('invalid release listing')
        result.extend(batch)
        if len(batch) < 100:
            return result
    raise ValueError('release listing exceeds page limit')


def published(release):
    return (isinstance(release, dict) and release.get('draft') is False
            and release.get('prerelease') is False)


def attempts(version, base):
    prefix = f'refs/tags/codex-update-v{version}-{base}-attempt-'
    refs = command('git', 'ls-remote', '--refs', 'origin', prefix + '*').splitlines()
    entries = []
    for line in refs:
        value, ref = line.split()
        suffix = ref.removeprefix(prefix)
        if not re.fullmatch(r'[1-9][0-9]*', suffix) or not re.fullmatch(r'[a-f0-9]{40}', value):
            raise ValueError('invalid attempt marker')
        entries.append((int(suffix), value, ref))
    entries.sort()
    if [n for n, _, _ in entries] != list(range(1, len(entries) + 1)):
        raise ValueError('noncontiguous attempt markers')
    return prefix, entries


def previous_run(entry, version, base):
    number, value, ref = entry
    command('git', 'fetch', '--no-tags', 'origin', ref)
    raw = command('git', 'cat-file', 'commit', value)
    headers, message = raw.split('\n\n', 1)
    metadata = json.loads(message)
    if (metadata.get('version') != version or metadata.get('base') != base
            or metadata.get('attempt') != number or type(metadata.get('run')) is not int
            or metadata['run'] <= 0 or f'parent {base}' not in headers.splitlines()
            or command('git', 'rev-parse', value + '^{tree}').strip()
                != command('git', 'rev-parse', base + '^{tree}').strip()):
        raise ValueError('invalid attempt binding')
    run = api(f'repos/{REPOSITORY}/actions/runs/{metadata["run"]}')
    if (run.get('id') != metadata['run'] or run.get('repository', {}).get('full_name') != REPOSITORY
            or run.get('path') != '.github/workflows/update.yml' or run.get('head_branch') != 'main'
            or run.get('event') not in ('schedule', 'workflow_dispatch')):
        raise ValueError('attempt run binding mismatch')
    return run


def push_claim(version, base, number, ref):
    if (os.environ.get('GITHUB_REPOSITORY') != REPOSITORY
            or os.environ.get('GITHUB_REF') != 'refs/heads/main'
            or not re.fullmatch(r'[1-9][0-9]*', os.environ.get('GITHUB_RUN_ID', ''))):
        raise ValueError('claim requires this repository main Actions run')
    if command('git', 'ls-remote', '--refs', 'origin', 'refs/heads/main').split() != [base, 'refs/heads/main']:
        raise ValueError('main moved before claim')
    command(sys.executable, 'scripts/publication_gate.py', 'verify')
    # A tiny, gated commit records the owning run without changing any source.
    # Immutable numbered tags provide an atomic slot, including concurrent claims.
    identity = 'github-actions[bot]'
    env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GIT_AUTHOR_NAME': identity, 'GIT_COMMITTER_NAME': identity,
           'GIT_AUTHOR_EMAIL': '41898282+github-actions[bot]@users.noreply.github.com',
           'GIT_COMMITTER_EMAIL': '41898282+github-actions[bot]@users.noreply.github.com'}
    body = json.dumps(dict(version=version, base=base, attempt=number,
                           run=int(os.environ['GITHUB_RUN_ID'])), sort_keys=True)
    result = subprocess.run(['git', 'commit-tree', base + '^{tree}', '-p', base, '-m', body],
                            env=env, capture_output=True, text=True, timeout=120, check=True)
    value = result.stdout.strip()
    # Authentication exists only in this process tree, never checkout config.
    if os.environ.get('GH_TOKEN'):
        auth = base64.b64encode(('x-access-token:' + os.environ['GH_TOKEN']).encode()).decode()
        env.update(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='http.https://github.com/.extraheader',
                   GIT_CONFIG_VALUE_0='AUTHORIZATION: basic ' + auth)
    result = subprocess.run(['git', 'push', '--porcelain', 'origin', value + ':' + ref],
                            env=env, capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise ValueError('claim rejected or uncertain; do not retry this write')
    return any(line.startswith('*\t') and line.split('\t')[1].endswith(':' + ref)
               for line in result.stdout.splitlines())


def probe(event, version='', retry=False, claim=False):
    if event != 'workflow_dispatch' and (version or retry):
        raise ValueError('version and retry require a manual dispatch')
    if retry and not version:
        raise ValueError('retry requires an explicit stable version')
    if version and not re.fullmatch(VERSION, version):
        raise ValueError('version must be a stable semantic version')
    base = command('git', 'rev-parse', 'HEAD').strip()
    def decision(build, reason):
        return dict(build=build, version=version, base=base, reason=reason)
    if event == 'push':
        version = json.loads((ROOT / 'build.json').read_text())['default_version']
        if not re.fullmatch(VERSION, version):
            raise ValueError('invalid manifest version')
        return decision(True, 'packaging-push')
    if version:
        release = api(f'repos/openai/codex/releases/tags/rust-v{version}')
        if not published(release) or release.get('tag_name') != 'rust-v' + version:
            raise ValueError('requested upstream release is not published stable')
    else:
        release = api('repos/openai/codex/releases/latest')
        match = STABLE.fullmatch(release.get('tag_name', '')) if isinstance(release, dict) else None
        if not published(release) or not match:
            raise ValueError('upstream Latest is not a published stable release')
        version = match[1]
    downstream = releases(REPOSITORY)
    if any(published(release) and (match := PUBLISHED.fullmatch(release.get('tag_name', '')))
           and match[1] == version for release in downstream):
        return decision(False, 'already-published')
    prefix, entries = attempts(version, base)
    if entries:
        run = previous_run(entries[-1], version, base)
        if run.get('status') != 'completed':
            return decision(False, 'attempt-still-active')
        if not retry:
            if len(entries) >= 2:
                return decision(False, 'automatic-attempts-exhausted')
            if run.get('conclusion') not in ('failure', 'cancelled', 'timed_out', 'startup_failure', 'success'):
                return decision(False, 'attempt-needs-review')
            updated = datetime.fromisoformat(run['updated_at'].replace('Z', '+00:00'))
            if (datetime.now(timezone.utc) - updated).total_seconds() < 1800:
                return decision(False, 'recovery-cooldown')
    number = len(entries) + 1
    if claim and not push_claim(version, base, number, prefix + str(number)):
        return decision(False, 'already-claimed')
    return decision(True, 'explicit-retry' if retry else 'new-stable' if not entries else 'bounded-recovery')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--event', choices=('push', 'schedule', 'workflow_dispatch'), required=True)
    parser.add_argument('--version', default='')
    parser.add_argument('--retry', action='store_true')
    parser.add_argument('--claim', action='store_true', help='record an attempt using the installed Git hook')
    parser.add_argument('--output', type=Path, help='append validated GitHub job outputs')
    args = parser.parse_args()
    try:
        result = probe(args.event, args.version, args.retry, args.claim)
        print(json.dumps(result, sort_keys=True))
        if args.output:
            with args.output.open('a') as output:
                output.write(f'build={str(result["build"]).lower()}\nversion={result["version"]}\nbase={result["base"]}\n')
    except (ValueError, OSError, subprocess.SubprocessError):
        print('release probe failed; no build authorized', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
