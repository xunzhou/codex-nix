#!/usr/bin/env python3
"""Download a published patched release before changing the npm installation."""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
from urllib.request import urlopen, Request

REPOSITORY = 'xunzhou/codex-nix'
API = f'https://api.github.com/repos/{REPOSITORY}'
TAG = re.compile(r'bundle-codex-v(\d+\.\d+\.\d+)-[a-f0-9]{16}-[0-9a-z]{16}')
NATIVE = re.compile(r'codex-native-(\d+\.\d+\.\d+-[a-f0-9]{64})-linux-x86_64\.tar\.gz')

def request(path):
    with urlopen(Request(API + path, headers={'Accept': 'application/vnd.github+json'}), timeout=60) as response:
        return json.load(response)


def native_key(release):
    """Newest recipe key with both a native archive and its recipe in this release."""
    names = {a['name']: a for a in release.get('assets', [])}
    version = TAG.fullmatch(release['tag_name'])[1]
    keys = [(names[name].get('created_at', ''), match[1]) for name in names
            if (match := NATIVE.fullmatch(name)) and match[1].startswith(version + '-')
            and f'codex-recipe-{match[1]}.tar.gz' in names]
    return max(keys)[1] if keys else None


def select(releases, version=None):
    candidates = [r for r in releases if not r.get('draft') and not r.get('prerelease')
                  and TAG.fullmatch(r['tag_name']) and (version is None or TAG.fullmatch(r['tag_name'])[1] == version)
                  and native_key(r)]
    if not candidates:
        raise ValueError('no published patched release is available for the requested version')
    return max(candidates, key=lambda r: (tuple(map(int, TAG.fullmatch(r['tag_name'])[1].split('.'))), r['published_at']))


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def download(release, name, destination):
    matches = [a for a in release['assets'] if a['name'] == name]
    if len(matches) != 1 or matches[0]['state'] != 'uploaded':
        raise ValueError('missing or duplicate release asset: ' + name)
    expected = matches[0].get('digest', '')
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', expected):
        raise ValueError('missing GitHub asset digest')
    if destination.is_file() and 'sha256:' + digest(destination) == expected:
        return
    url = f'https://github.com/{REPOSITORY}/releases/download/{release["tag_name"]}/{name}'
    pending = destination.with_suffix('.pending')
    subprocess.run(['curl', '-fL', '--retry', '3', '--output', str(pending), url], check=True, stdout=sys.stderr)
    if 'sha256:' + digest(pending) != expected:
        raise ValueError('GitHub asset checksum mismatch')
    pending.replace(destination)


def extract(archive, destination, expected):
    with tarfile.open(archive) as tar:
        members = tar.getmembers()
        if len(members) != len(expected) or {m.name for m in members} != set(expected) or any(not m.isfile() for m in members):
            raise ValueError('unexpected release archive inventory')
        tar.extractall(destination, filter='data')


def prepare(release, cache):
    tag = release['tag_name']
    if not TAG.fullmatch(tag):
        raise ValueError('invalid release tag')
    key = native_key(release)
    if key is None:
        raise ValueError('release has no native archive with its recipe')
    version = TAG.fullmatch(tag)[1]
    directory = cache / key
    directory.mkdir(parents=True, exist_ok=True)
    recipe_name, native_name = f'codex-recipe-{key}.tar.gz', f'codex-native-{key}-linux-x86_64.tar.gz'
    for name in [recipe_name, native_name]:
        download(release, name, directory / name)
    with tarfile.open(directory / recipe_name) as archive:
        data = json.load(archive.extractfile('build.json'))
    patches = data['releases'][version]['patches']
    if any(Path(p).is_absolute() or '..' in Path(p).parts for p in patches):
        raise ValueError('invalid patch path')
    extract(directory / recipe_name, directory, ['build.json', 'scripts/install-source-codex.py', *['patches/' + p for p in patches]])
    module_spec = importlib.util.spec_from_file_location('released_installer', directory / 'scripts/install-source-codex.py')
    native = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(native)
    spec = native.load_recipe(directory / 'build.json', version)
    if native.recipe_key(spec, [directory / 'patches' / p for p in spec['patches']], version) != key:
        raise ValueError('release recipe key mismatch')
    bundle = directory / key
    bundle.mkdir(exist_ok=True)
    extract(directory / native_name, bundle, [*spec['binaries'], 'bundle.json', 'sha256.json'])
    if json.loads((bundle / 'bundle.json').read_text()) != {'key': key, 'version': version} or not native.valid_bundle(bundle, version, spec['markers'], spec['binaries']):
        raise ValueError('native bundle verification failed')
    native.smoke_bundle(bundle, spec)
    return directory, version


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reviewed-version', action='store_true')
    parser.add_argument('--install-reviewed', '--restore-reviewed', action='store_true')
    args = parser.parse_args()
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise ValueError('published native releases support Linux x86_64')
    version = None
    if not (args.reviewed_version or args.install_reviewed):
        entry = Path(os.environ.get('CODEX_MANAGED_ENTRYPOINT', '/usr/local/bin/codex'))
        package = Path(os.environ.get('CODEX_PACKAGE_ROOT', entry.resolve().parent.parent))
        version = json.loads((package / 'package.json').read_text())['version']
    cache = Path(os.environ.get('CODEX_RELEASE_CACHE', Path.home() / '.cache/codex-releases'))
    cache.mkdir(parents=True, exist_ok=True)
    with (cache / 'installer.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        selection = cache / 'selected.json'
        release = None
        if version is not None and selection.is_file():
            saved = json.loads(selection.read_text())
            match = TAG.fullmatch(saved.get('tag_name', ''))
            if match and match[1] == version:
                release = saved
        if release is None:
            releases = []
            page = 1
            while True:
                batch = request(f'/releases?per_page=100&page={page}')
                releases.extend(batch)
                if len(batch) < 100:
                    break
                page += 1
            release = select(releases, version)
        directory, version = prepare(release, cache)
        # Persist the verified selection before npm can replace the working pair.
        # The subsequent patch step can finish using cached, reverified bytes even
        # when the network disappears between the two update-agents invocations.
        pending = cache / 'selected.pending'
        pending.write_text(json.dumps(release))
        pending.replace(selection)
        if args.reviewed_version:
            print(version)
            return
        if args.install_reviewed:
            subprocess.run([*shlex.split(os.environ.get('CODEX_PRIVILEGE_COMMAND', 'sudo')), 'npm', 'install', '-g', '@openai/codex@' + version], cwd=Path.home(), check=True)
        env = dict(os.environ, CODEX_BUILD_MANIFEST=str(directory / 'build.json'), CODEX_PATCH_ROOT=str(directory / 'patches'),
                   CODEX_PATCH_CACHE=str(directory), CODEX_INSTALL_MODE='download')
        subprocess.run([sys.executable, str(directory / 'scripts/install-source-codex.py')], env=env, check=True)

if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        sys.exit(f'Codex release installer: {error}')
