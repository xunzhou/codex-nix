#!/usr/bin/env python3
"""Select stable upstream versions and publish independently verified native releases."""
import argparse
import hashlib
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
    args = parser.parse_args()
    publisher = module('publisher', 'publish-release.py')
    native = module('native', 'install-source-codex.py')
    api = publisher.GitHub('xunzhou/codex-nix', os.environ['GH_TOKEN'])
    if args.action == 'prepare':
        upstream = publisher.GitHub('openai/codex', os.environ['GH_TOKEN'])
        releases = upstream.request('/releases?per_page=100')
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
    data = json.loads((ROOT / 'native-build.json').read_text())
    version = data['default_version']
    spec = native.load_recipe(ROOT / 'native-build.json', version)
    key = native.recipe_key(spec, [ROOT / 'patches' / p for p in spec['patches']], version)
    assets = args.assets
    publisher.native_identity(assets / 'codex-linux-x86_64.tar.gz', {'version': version, 'key': key})
    with tarfile.open(assets / 'recipe.tar.gz', 'w:gz') as archive:
        archive.add(ROOT / 'native-build.json', arcname='build.json')
        archive.add(ROOT / 'scripts/install-source-codex.py', arcname='scripts/install-source-codex.py')
        for patch in spec['patches']:
            archive.add(ROOT / 'patches' / patch, arcname='patches/' + patch)
    metadata = {'schema': 1, 'version': version, 'key': key,
                'revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
                'sha256': {name: publisher.sha256(assets / name) for name in ['codex-linux-x86_64.tar.gz', 'recipe.tar.gz']}}
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
    if set(remote) - expected:
        raise ValueError('unexpected release asset')
    if not release['draft']:
        if set(remote) != expected:
            raise ValueError('incomplete published release')
        print('Release already published; immutable assets retained')
        return
    # Never overwrite assets, including when resuming a partially uploaded draft.
    for name in expected:
        if name in remote:
            if remote[name]['digest'] != 'sha256:' + publisher.sha256(assets / name):
                raise ValueError('partial draft differs; resume with original run artifacts')
        else:
            api.upload(release['id'], assets / name)
    final = api.request(f'/releases/{release["id"]}')
    if {a['name'] for a in final['assets']} != expected or any(
        a['state'] != 'uploaded' or a['digest'] != 'sha256:' + publisher.sha256(assets / a['name']) for a in final['assets']):
        raise ValueError('final release verification failed')
    api.request(f'/releases/{release["id"]}', {'draft': False, 'make_latest': 'false'}, method='PATCH')
    print(f'Published https://github.com/xunzhou/codex-nix/releases/tag/{tag}')

if __name__ == '__main__':
    main()
