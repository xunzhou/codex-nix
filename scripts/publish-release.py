#!/usr/bin/env python3
"""Publish verified Nix and native artifacts together; resume drafts without overwrites."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def native_identity(path, expected):
    with tarfile.open(path) as archive:
        members = archive.getmembers()
        names = {'codex', 'codex-code-mode-host', 'sha256.json', 'bundle.json'}
        require(len(members) == len(names) and {m.name for m in members} == names
                and all(m.isfile() for m in members), 'invalid native archive inventory')
        require(json.load(archive.extractfile('bundle.json')) == expected, 'native recipe mismatch')
        inventory = json.load(archive.extractfile('sha256.json'))
        require(set(inventory) == names - {'sha256.json', 'bundle.json'}, 'invalid native checksums')
        for name, checksum in inventory.items():
            require(hashlib.file_digest(archive.extractfile(name), 'sha256').hexdigest() == checksum,
                    'native checksum mismatch')


def nix_identity(directory, manifest_name):
    manifest = json.loads((directory / manifest_name).read_text())
    tag = manifest['tag']
    archive = f'codex-{tag}-x86_64-linux.nar.zst'
    checksums = f'codex-{tag}-SHA256SUMS'
    require(manifest['schema'] == 1 and manifest['archive'] == archive, 'invalid Nix manifest')
    require(manifest['system'] == 'x86_64-linux', 'invalid Nix system')
    require(isinstance(manifest['closure_paths'], int) and manifest['closure_paths'] > 0,
            'invalid closure count')
    inventory = {}
    for line in (directory / checksums).read_text().splitlines():
        match = re.fullmatch(r'([a-f0-9]{64})  ([^/\s]+)', line)
        require(match is not None and match[2] not in inventory, 'invalid Nix checksums')
        inventory[match[2]] = match[1]
    require(set(inventory) == {archive, manifest_name}, 'invalid Nix checksum inventory')
    require(inventory[manifest_name] == sha256(directory / manifest_name), 'Nix manifest checksum mismatch')
    require(inventory[archive] == manifest['archive_sha256'], 'Nix archive checksum mismatch')
    return manifest, inventory


class GitHub:
    def __init__(self, repository, token):
        require(re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository), 'invalid repository')
        self.root = f'https://api.github.com/repos/{repository}'
        self.uploads = f'https://uploads.github.com/repos/{repository}'
        self.token = token

    def request(self, path, data=None, method=None):
        headers = {'Accept': 'application/vnd.github+json', 'Authorization': f'Bearer {self.token}',
                   'X-GitHub-Api-Version': '2022-11-28'}
        payload = None if data is None else json.dumps(data).encode()
        if payload is not None:
            headers['Content-Type'] = 'application/json'
        try:
            with urlopen(Request(self.root + path, data=payload, headers=headers, method=method), timeout=120) as response:
                return json.load(response)
        except HTTPError as error:
            if error.code == 404 and method is None and data is None:
                return None
            raise

    def download(self, asset, destination):
        require(isinstance(asset['id'], int) and asset['id'] > 0, 'invalid asset ID')
        # curl strips Authorization when GitHub redirects to asset storage.
        result = subprocess.run(['curl', '--fail-with-body', '--silent', '--show-error', '--location',
                                 '-H', 'Accept: application/octet-stream',
                                 '-H', f'Authorization: Bearer {self.token}',
                                 '--output', str(destination),
                                 self.root + f'/releases/assets/{asset["id"]}'],
                                capture_output=True, text=True)
        require(result.returncode == 0, 'GitHub asset download failed')
        require(asset['digest'] == 'sha256:' + sha256(destination), 'downloaded asset digest mismatch')

    def upload(self, release_id, path):
        # Stream large closures from disk; never load the archive into Python memory.
        result = subprocess.run(['curl', '--fail-with-body', '--silent', '--show-error',
                                 '-H', f'Authorization: Bearer {self.token}',
                                 '-H', 'Content-Type: application/octet-stream',
                                 '--data-binary', '@' + str(path),
                                 f'{self.uploads}/releases/{release_id}/assets?name={path.name}'],
                                capture_output=True, text=True)
        require(result.returncode == 0, 'GitHub asset upload failed')
        asset = json.loads(result.stdout)
        require(asset['name'] == path.name and asset['state'] == 'uploaded'
                and asset['digest'] == 'sha256:' + sha256(path), 'uploaded asset digest mismatch')


def publish(api, assets, tag, revision, expected_native, expected_output):
    require(re.fullmatch(r'codex-v[0-9]+\.[0-9]+\.[0-9]+-[a-f0-9]{16}-[0-9a-z]{16}', tag), 'invalid release tag')
    require(re.fullmatch(r'[a-f0-9]{40}', revision), 'invalid source revision')
    manifest_name = f'codex-{tag}-manifest.json'
    local, inventory = nix_identity(assets, manifest_name)
    require(local['tag'] == tag and local['output_path'] == expected_output
            and local['codex_version'] == expected_native['version'], 'Nix source identity mismatch')
    require(sha256(assets / local['archive']) == inventory[local['archive']], 'local Nix archive digest mismatch')
    native_name = f'codex-native-{expected_native["key"]}-linux-x86_64.tar.gz'
    native_identity(assets / native_name, expected_native)
    nix_names = {manifest_name, local['archive'], f'codex-{tag}-SHA256SUMS'}
    names = nix_names | {native_name}
    release_tag = f'bundle-{tag}'
    release = api.request(f'/releases/tags/{release_tag}')
    if release is None:
        release = api.request('/releases', {'tag_name': release_tag, 'target_commitish': revision,
                              'name': f'Codex {local["codex_version"]} — Nix and Linux',
                              'body': 'Verified Nix closure and Ubuntu 24.04 x86_64 GNU/Linux binaries. '
                                      'Native binaries require compatible glibc and system libraries. '
                                      'Assets are recipe-specific and never overwritten.',
                              'draft': True, 'make_latest': 'false'})
    require(release['tag_name'] == release_tag, 'release tag mismatch')
    remote = {asset['name']: asset for asset in release['assets']}
    require(len(remote) == len(release['assets']), 'duplicate release asset')
    for name, asset in remote.items():
        require(name in nix_names or re.fullmatch(r'codex-native-[0-9]+\.[0-9]+\.[0-9]+-[a-f0-9]{64}-linux-x86_64\.tar\.gz', name),
                'unexpected release asset')
        require(asset['state'] == 'uploaded' and re.fullmatch(r'sha256:[a-f0-9]{64}', asset['digest']),
                'invalid release asset digest')
    present_nix = nix_names & remote.keys()
    # Never repair an incomplete public release or replace an asset already
    # visible to installers. Legacy tags are intentionally never modified.
    require(release['draft'] or present_nix == nix_names, 'incomplete published Nix release')
    with tempfile.TemporaryDirectory() as temporary:
        scratch = Path(temporary)
        if present_nix == nix_names:
            for name in (manifest_name, f'codex-{tag}-SHA256SUMS'):
                api.download(remote[name], scratch / name)
            existing, existing_inventory = nix_identity(scratch, manifest_name)
            require({k: v for k, v in existing.items() if k != 'archive_sha256'}
                    == {k: v for k, v in local.items() if k != 'archive_sha256'}, 'existing Nix identity mismatch')
            require(remote[local['archive']]['digest'] == 'sha256:' + existing_inventory[local['archive']],
                    'existing Nix archive digest mismatch')
        else:
            # Incomplete drafts can resume only with byte-identical prepared artifacts.
            for name in present_nix:
                require(remote[name]['digest'] == 'sha256:' + sha256(assets / name), 'partial draft differs; use original run artifacts')
        if native_name in remote:
            api.download(remote[native_name], scratch / native_name)
            native_identity(scratch / native_name, expected_native)
    for name in sorted(names - remote.keys()):
        api.upload(release['id'], assets / name)
    final = api.request(f'/releases/{release["id"]}')
    final_assets = {asset['name']: asset for asset in final['assets']}
    require(set(final_assets) == set(remote) | names and len(final_assets) == len(final['assets']),
            'final release inventory mismatch')
    for name, asset in final_assets.items():
        expected_digest = remote[name]['digest'] if name in remote else 'sha256:' + sha256(assets / name)
        require(asset['state'] == 'uploaded' and asset['digest'] == expected_digest, 'final release digest mismatch')
    if final['draft']:
        api.request(f'/releases/{release["id"]}', {'draft': False, 'make_latest': 'false'}, method='PATCH')
    print(f'Published or verified {release_tag}: Nix and native Linux')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--assets', type=Path, required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--output-path', required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    module_spec = importlib.util.spec_from_file_location('native', root / 'scripts/install-source-codex.py')
    native = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(native)
    manifest = json.loads((root / 'build.json').read_text())
    version = manifest['default_version']
    recipe = native.load_recipe(root / 'build.json', version)
    key = native.recipe_key(recipe, [root / 'patches' / p for p in recipe['patches']], version)
    expected = {'key': key, 'version': version}
    api = GitHub(manifest['release_repository'], os.environ['GH_TOKEN'])
    publish(api, args.assets, args.tag, args.revision, expected, args.output_path)


if __name__ == '__main__':
    main()
