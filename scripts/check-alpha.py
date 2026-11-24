#!/usr/bin/env python3
"""Replay patches and exercise a verified upstream alpha host without publishing."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parent.parent
ALPHA = re.compile(r'rust-v((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)-alpha\.(?:0|[1-9][0-9]*))')
HOST = 'codex-code-mode-host-x86_64-unknown-linux-musl'


class AlphaFailure(ValueError):
    pass


def module(path):
    spec = importlib.util.spec_from_file_location(path.stem.replace('-', '_'), path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def checked(metadata):
    tag = metadata['tag']
    if not isinstance(tag, str) or not ALPHA.fullmatch(tag):
        raise AlphaFailure('expected a numbered alpha tag')
    expected = f'https://github.com/openai/codex/releases/download/{tag}/{HOST}.tar.gz'
    if metadata['url'] != expected or not re.fullmatch(r'[0-9a-f]{64}', metadata['sha256']):
        raise AlphaFailure('invalid alpha asset binding')
    return metadata


def recent_releases(probe):
    # Match the updater's recent-100 window. Small pages avoid timeouts caused
    # by upstream releases with many binary assets; incomplete reads fail closed.
    result = []
    for page in range(1, 21):
        batch = probe.api(f'repos/openai/codex/releases?per_page=5&page={page}')
        if not isinstance(batch, list) or len(batch) > 5:
            raise AlphaFailure('invalid upstream release page')
        result.extend(batch)
        if len(batch) < 5:
            break
    return result


def metadata(tag):
    probe = module(ROOT / 'scripts/probe-update.py')
    if tag and not ALPHA.fullmatch(tag):
        raise AlphaFailure('only numbered alpha releases are accepted')
    releases = ([probe.api('repos/openai/codex/releases/tags/' + tag)] if tag
                else recent_releases(probe))
    candidates = [r for r in releases if isinstance(r, dict) and r.get('draft') is False
                  and r.get('prerelease') is True and ALPHA.fullmatch(r.get('tag_name', ''))
                  and (not tag or r['tag_name'] == tag)]
    if not candidates:
        raise AlphaFailure('no published alpha')
    release = max(candidates, key=lambda r: tuple(map(int, re.findall(r'\d+', r['tag_name']))))
    assets = [a for a in release.get('assets', []) if a.get('name') == HOST + '.tar.gz']
    if len(assets) != 1 or not re.fullmatch(r'sha256:[0-9a-f]{64}', assets[0].get('digest', '')):
        raise AlphaFailure('alpha host requires a GitHub SHA256 asset digest')
    return checked(dict(tag=release['tag_name'], url=assets[0]['browser_download_url'],
                        sha256=assets[0]['digest'][7:]))


def download(url, target):
    subprocess.run(['curl', '--fail', '--location', '--silent', '--show-error',
                    '--max-time', '300', '--max-filesize', '268435456',
                    '--output', str(target), url], check=True, timeout=310)


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def source(archive, destination):
    destination.mkdir()
    with tarfile.open(archive) as tar:
        if sum(member.size for member in tar.getmembers()) > 2 * 1024**3:
            raise AlphaFailure('source archive exceeds limit')
        tar.extractall(destination, filter='data')
    roots = list(destination.iterdir())
    if len(roots) != 1 or not roots[0].is_dir():
        raise AlphaFailure('invalid alpha source layout')
    return roots[0]


def replay(data, scratch):
    archive = scratch / 'source.tar.gz'
    download(f'https://github.com/openai/codex/archive/refs/tags/{data["tag"]}.tar.gz', archive)
    tree = source(archive, scratch / 'source')
    version = ALPHA.fullmatch(data['tag'])[1]
    manifest = tomllib.loads((tree / 'codex-rs/Cargo.toml').read_text())
    if manifest['workspace']['package']['version'] != version:
        raise AlphaFailure('alpha source version mismatch')
    build = json.loads((ROOT / 'build.json').read_text())
    recipe = build['releases'][build['default_version']]
    patches = [str(ROOT / 'patches' / name) for name in recipe['patches']]
    # Test the complete stack in order. Rebase only using the existing reviewed
    # port procedure and verified previous source, in a disposable patch root.
    for patch in patches:
        result = subprocess.run(['git', '-C', str(tree), 'apply', patch], capture_output=True)
        if result.returncode:
            shutil.rmtree(scratch / 'source')
            tree = source(archive, scratch / 'source')
            patch_root = scratch / 'port'
            shutil.copytree(ROOT / 'patches', patch_root / 'patches')
            failure = scratch / 'failure'
            os.environ['CODEX_FAILURE_DIR'] = str(failure)
            try:
                module(ROOT / 'scripts/rebase-patches.py').rebase(
                    patch_root, tree, recipe, recipe['patches'], version)
            except subprocess.CalledProcessError:
                packets = list(failure.glob('*/failure.json'))
                index = json.loads(packets[0].read_text()).get('patch_index') if len(packets) == 1 else None
                if type(index) is int and 0 <= index < len(patches):
                    raise AlphaFailure(f'patch replay failed at index {index}') from None
                raise AlphaFailure('patch replay failed') from None
            break
    print(json.dumps(dict(patches=len(patches), source_sha256=digest(archive))))


def runtime(data, scratch, sudo_runtime=False):
    archive = scratch / 'host.tar.gz'
    download(data['url'], archive)
    if digest(archive) != data['sha256']:
        raise AlphaFailure('alpha host digest mismatch')
    app = scratch / 'app'
    app.mkdir()
    binary = app / 'codex-code-mode-host'
    with tarfile.open(archive) as tar:
        members = tar.getmembers()
        if len(members) != 1 or members[0].name not in (HOST, './' + HOST) or not members[0].isfile():
            raise AlphaFailure('unexpected alpha host archive inventory')
        if members[0].size > 512 * 1024**2:
            raise AlphaFailure('alpha host exceeds limit')
        with tar.extractfile(members[0]) as stream, binary.open('wb') as output:
            shutil.copyfileobj(stream, output)
    binary.chmod(0o755)
    isolated = ['bwrap', '--unshare-all', '--die-with-parent', '--clearenv',
                '--ro-bind', str(app), '/app', '--proc', '/proc', '--dev', '/dev',
                '--tmpfs', '/tmp', '--setenv', 'HOME', '/tmp', '--setenv', 'PATH', '/app',
                '--chdir', '/tmp', '/app/codex-code-mode-host', '--listen', 'grpc+stdio://']
    if sudo_runtime:
        if not Path('/usr/bin/sudo').is_file() or not Path('/usr/bin/bwrap').is_file():
            raise AlphaFailure('sudo runtime helper unavailable')
        isolated[0] = '/usr/bin/bwrap'
    elevated = ['/usr/bin/sudo', '-n', '--'] if sudo_runtime else []
    subprocess.run([sys.executable, str(ROOT / 'tests/static-runtime.py'), *elevated, *isolated],
                   check=True, timeout=45)


def validate(data, sudo_runtime=False):
    # Neither replay nor downloaded code inherits credentials or Git overrides.
    # The host additionally runs with no network, host home, or shared libraries.
    results = {}
    with tempfile.TemporaryDirectory(prefix='codex-alpha-') as directory:
        scratch = Path(directory)
        metadata_path = scratch / 'metadata.json'
        metadata_path.write_text(json.dumps(data))
        for stage in ('replay', 'runtime'):
            stage_root = scratch / stage
            stage_root.mkdir()
            home = stage_root / 'home'
            home.mkdir()
            env = {'PATH': os.environ['PATH'], 'HOME': str(home), 'LANG': 'C.UTF-8',
                   'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull}
            # Transport configuration is needed on managed workers. These values
            # remain outside the host's --clearenv sandbox, like the HTTP client.
            for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
                        'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy',
                        'SSL_CERT_FILE', 'SSL_CERT_DIR', 'CURL_CA_BUNDLE'):
                if key in os.environ:
                    env[key] = os.environ[key]
            if os.environ.get('CODEX_MERGIRAF'):
                env['CODEX_MERGIRAF'] = os.environ['CODEX_MERGIRAF']
            try:
                command = [sys.executable, str(Path(__file__).resolve()), stage,
                           '--metadata', str(metadata_path), '--scratch', str(stage_root)]
                if stage == 'runtime' and sudo_runtime:
                    command.append('--sudo-runtime')
                completed = subprocess.run(command,
                    env=env, capture_output=True, timeout=1200 if stage == 'replay' else 360)
                results[stage] = 'passed' if completed.returncode == 0 else 'failed'
                lines = completed.stderr.decode('utf-8', errors='replace').splitlines()
                if lines and lines[-1].startswith('alpha validation failed: '):
                    print(lines[-1], flush=True)
            except subprocess.TimeoutExpired:
                results[stage] = 'timeout'
            # No upstream content, paths, conflict text, or binary output in CI logs.
            print(f'alpha {stage}: {results[stage]}', flush=True)
    print(json.dumps(dict(tag=data['tag'], host_sha256=data['sha256'], **results), sort_keys=True))
    return 0 if all(value == 'passed' for value in results.values()) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    select = sub.add_parser('metadata')
    select.add_argument('--tag', default='')
    select.add_argument('--output', required=True, type=Path)
    for stage in ('validate', 'replay', 'runtime'):
        item = sub.add_parser(stage)
        item.add_argument('--metadata', required=True, type=Path)
        if stage in ('validate', 'runtime'):
            item.add_argument('--sudo-runtime', action='store_true')
        if stage != 'validate':
            item.add_argument('--scratch', required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == 'metadata':
            args.output.write_text(json.dumps(metadata(args.tag), sort_keys=True) + '\n')
        else:
            data = checked(json.loads(args.metadata.read_text()))
            if args.command == 'validate':
                return validate(data, args.sudo_runtime)
            if args.command == 'runtime':
                runtime(data, args.scratch, args.sudo_runtime)
            else:
                replay(data, args.scratch)
    except (ValueError, KeyError, OSError, subprocess.SubprocessError, tarfile.TarError) as error:
        reason = str(error) if isinstance(error, AlphaFailure) else type(error).__name__
        print('alpha validation failed: ' + reason, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
