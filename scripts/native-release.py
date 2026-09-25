#!/usr/bin/env python3
"""Select stable upstream versions and publish independently verified native releases."""
import argparse
import gzip
import io
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]

def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'publish'])
    parser.add_argument('--version', default='')
    parser.add_argument('--assets', type=Path)
    parser.add_argument('--recover-run', default='')
    args = parser.parse_args()
    publisher = module('publisher', 'publish-release.py')
    native = module('native', 'install-source-codex.py')
    api = publisher.GitHub('xunzhou/codex-nix', os.environ['GH_TOKEN'])
    if args.action == 'prepare' and args.recover_run:
        if not re.fullmatch(r'[1-9][0-9]*', args.recover_run):
            raise ValueError('recovery run must be a numeric Actions run ID')
        run = api.request('/actions/runs/' + args.recover_run)
        jobs = api.request('/actions/runs/' + args.recover_run + '/jobs?per_page=100')
        if run['head_branch'] != 'main' or run['path'] != '.github/workflows/native.yml' or not any(
            step['name'] == 'Build, test patches, and verify binary pair' and step['conclusion'] == 'success'
            for job in jobs['jobs'] for step in job['steps']):
            raise ValueError('recovery requires a tested native build from main')
        data = json.loads((ROOT / 'native-build.json').read_text())
        version = data['default_version']
        if args.version and args.version != version:
            raise ValueError('recovery version must match the current native recipe')
        recipe = native.load_recipe(ROOT / 'native-build.json', version)
        key = native.recipe_key(recipe, [ROOT / 'patches' / p for p in recipe['patches']], version)
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write(f'version={version}\nkey={key}\nbuild=true\nrecover=true\n')
        return
    if args.action == 'prepare':
        upstream = publisher.GitHub('openai/codex', os.environ['GH_TOKEN'])
        releases = []
        for page in range(1, 11):
            batch = upstream.request(f'/releases?per_page=10&page={page}')
            releases.extend(batch)
            if len(batch) < 10:
                break
        versions = {r['tag_name'][6:] for r in releases if not r['draft'] and not r['prerelease']
                    and re.fullmatch(r'rust-v\d+\.\d+\.\d+', r['tag_name'])}
        version = args.version or max(versions, key=lambda v: tuple(map(int, v.split('.'))))
        if version not in versions:
            raise ValueError('requested version is not a published stable upstream release')
        data = json.loads((ROOT / 'native-build.json').read_text())
        if version != data['default_version']:
            module('updater', 'update-manifest.py').update(ROOT, version, native=True)
        spec = native.load_recipe(ROOT / 'native-build.json', version)
        key = native.recipe_key(spec, [ROOT / 'patches' / p for p in spec['patches']], version)
        release = api.request('/releases/tags/native-' + key)
        ready = bool(release and not release['draft'])
        if ready:
            expected = {'codex-linux-x86_64.tar.gz', 'recipe.tar.gz', 'release.json'}
            if {a['name'] for a in release['assets']} != expected:
                raise ValueError('published native release has an incomplete asset inventory')
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write(f'version={version}\nkey={key}\nbuild={str(not ready).lower()}\n')
        print(f'Codex {version}: ' + ('release already available' if ready else 'build required'))
        return
    publish(api, args.assets, publisher, native)


def publish(api, assets, publisher, native):
    data = json.loads((ROOT / 'native-build.json').read_text())
    version = data['default_version']
    spec = native.load_recipe(ROOT / 'native-build.json', version)
    key = native.recipe_key(spec, [ROOT / 'patches' / p for p in spec['patches']], version)
    publisher.native_identity(assets / 'codex-linux-x86_64.tar.gz', {'version': version, 'key': key})
    files = {'build.json': ROOT / 'native-build.json',
             'scripts/install-source-codex.py': ROOT / 'scripts/install-source-codex.py',
             **{'patches/' + p: ROOT / 'patches' / p for p in spec['patches']}}
    # Canonical metadata makes the recipe archive identical across clean checkouts.
    with (assets / 'recipe.tar.gz').open('wb') as output:
        with gzip.GzipFile(filename='', fileobj=output, mode='wb', mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w') as archive:
                for name, path in sorted(files.items()):
                    content = path.read_bytes()
                    info = tarfile.TarInfo(name)
                    info.size = len(content)
                    info.mode = 0o644
                    archive.addfile(info, io.BytesIO(content))
    metadata = {'schema': 1, 'version': version, 'key': key,
                'revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
                'sha256': {name: publisher.sha256(assets / name) for name in ['codex-linux-x86_64.tar.gz', 'recipe.tar.gz']}}
    if os.environ.get('CODEX_BUILD_RUN_ID'):
        metadata['build_run_id'] = int(os.environ['CODEX_BUILD_RUN_ID'])
    if os.environ.get('CODEX_BUILD_REVISION'):
        metadata['build_revision'] = os.environ['CODEX_BUILD_REVISION']
    (assets / 'release.json').write_text(json.dumps(metadata, indent=2) + '\n')
    tag = 'native-' + key
    release = api.request('/releases/tags/' + tag)
    if release is None:
        release = api.request('/releases', {'tag_name': tag, 'target_commitish': metadata['revision'],
            'name': f'Patched Codex {version} — native Linux x86_64', 'draft': True, 'make_latest': 'false',
            'body': 'Built and patch-tested by GitHub Actions on Ubuntu 24.04. Includes Codex and code-mode host. '
                    'Requires compatible glibc and system libraries. Download codex-linux-x86_64.tar.gz; '
                    'release.json records checksums and the source revision.'})
    expected = {'codex-linux-x86_64.tar.gz', 'recipe.tar.gz', 'release.json'}
    remote = {a['name']: a for a in release['assets']}
    if set(remote) - expected or len(remote) != len(release['assets']):
        raise ValueError('unexpected or duplicate release asset')
    if not release['draft'] and set(remote) != expected:
        raise ValueError('incomplete published release')
    # Reuse original uploaded bytes after a partial failure. Validate their recipe
    # before accepting them; never overwrite a remote asset with a later rebuild.
    for name, asset in remote.items():
        if asset['state'] != 'uploaded':
            raise ValueError('incomplete remote upload')
        api.download(asset, assets / name)
    publisher.native_identity(assets / 'codex-linux-x86_64.tar.gz', {'version': version, 'key': key})
    with tarfile.open(assets / 'recipe.tar.gz') as archive:
        members = archive.getmembers()
        if len(members) != len(files) or {m.name for m in members} != set(files) or any(not m.isfile() for m in members):
            raise ValueError('remote recipe inventory mismatch')
        for name, path in files.items():
            if archive.extractfile(name).read() != path.read_bytes():
                raise ValueError('remote recipe content mismatch')
    checksums = {name: publisher.sha256(assets / name) for name in ['codex-linux-x86_64.tar.gz', 'recipe.tar.gz']}
    if 'release.json' in remote:
        metadata = json.loads((assets / 'release.json').read_text())
        if metadata.get('schema') != 1 or metadata.get('version') != version or metadata.get('key') != key or metadata.get('sha256') != checksums or not re.fullmatch(r'[a-f0-9]{40}', metadata.get('revision', '')):
            raise ValueError('remote release manifest mismatch')
    else:
        metadata['sha256'] = checksums
        (assets / 'release.json').write_text(json.dumps(metadata, indent=2) + '\n')
    for name in sorted(expected - remote.keys()):
        api.upload(release['id'], assets / name)
    final = api.request(f'/releases/{release["id"]}')
    if {a['name'] for a in final['assets']} != expected or any(
        a['state'] != 'uploaded' or a['digest'] != 'sha256:' + publisher.sha256(assets / a['name']) for a in final['assets']):
        raise ValueError('final release verification failed')
    if final['draft']:
        api.request(f'/releases/{release["id"]}', {'draft': False, 'make_latest': 'false'}, method='PATCH')
    print(f'Published https://github.com/xunzhou/codex-nix/releases/tag/{tag}')


if __name__ == '__main__':
    main()
