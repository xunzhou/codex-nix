#!/usr/bin/env python3
"""Prepare and verify a source/dependency pin update without guessing patch conflicts."""
import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import tomllib


def command(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def update(root, version):
    manifest = root / 'build.json'
    original = manifest.read_bytes()
    data = json.loads(original)
    current = data['default_version']
    recipe = copy.deepcopy(data['releases'].get(version, data['releases'][current]))
    profile = data['profiles'][recipe['profile']]
    with tempfile.TemporaryDirectory(prefix='codex-update-') as directory:
        scratch = Path(directory)
        archive = scratch / 'source.tar.gz'
        url = f'https://github.com/openai/codex/archive/refs/tags/rust-v{version}.tar.gz'
        command('curl', '-fL', '--retry', '3', '-o', str(archive), url)
        source_parent = scratch / 'source'
        source_parent.mkdir()
        with tarfile.open(archive) as tar:
            tar.extractall(source_parent, filter='data')
        roots = list(source_parent.iterdir())
        if len(roots) != 1 or not roots[0].is_dir():
            raise ValueError('expected one source directory')
        source = roots[0]
        workspace = tomllib.loads((source / 'codex-rs/Cargo.toml').read_text())
        if workspace['workspace']['package']['version'] != version:
            raise ValueError('upstream source version mismatch')
        packages = tomllib.loads((source / 'codex-rs/Cargo.lock').read_text())['package']
        for name, expected in profile.get('crate_versions', {}).items():
            if {p['version'] for p in packages if p['name'] == name} != {expected}:
                raise ValueError(f'{name} changed; review the shared native dependency profile first')
        nar_hash = command('nix', '--extra-experimental-features', 'nix-command', 'hash', 'path', str(source), capture_output=True).stdout.strip()
        # A reviewed port takes precedence over the previous version's patch stack.
        port = root / 'patches' / version
        if port.is_dir():
            names = [Path(p).name for p in recipe['patches']]
            if not all((port / name).is_file() for name in names):
                raise ValueError('incomplete reviewed patch port')
            recipe['patches'] = [f'{version}/{name}' for name in names]
        try:
            for patch in recipe['patches']:
                path = (root / 'patches' / patch).resolve()
                if not path.is_relative_to((root / 'patches').resolve()):
                    raise ValueError('patch path is outside patches directory')
                command('git', '-C', str(source), 'apply', '--check', str(path))
                command('git', '-C', str(source), 'apply', str(path))
        except subprocess.CalledProcessError:
            # Restore pristine new source before rebasing the entire ordered stack.
            import shutil
            shutil.rmtree(source)
            with tarfile.open(archive) as tar:
                tar.extractall(source_parent, filter='data')
            module_spec = importlib.util.spec_from_file_location('rebase_patches', root / 'scripts/rebase-patches.py')
            module = importlib.util.module_from_spec(module_spec)
            module_spec.loader.exec_module(module)
            recipe['patches'] = module.rebase(root, source, data['releases'][current], recipe['patches'], version)
        with archive.open('rb') as stream:
            sha256 = hashlib.file_digest(stream, 'sha256').hexdigest()
        recipe['source'] = {'url': url, 'sha256': sha256}
        recipe['source']['nar_hash'] = nar_hash
        recipe['cargo_hash'] = 'sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA='
        data['releases'] = {version: recipe}
        data['default_version'] = version
        try:
            manifest.write_text(json.dumps(data, indent=2) + '\n')
            result = subprocess.run(['nix', 'build', '--no-link', '.#codex.cargoDeps'], cwd=root, capture_output=True, text=True)
            hashes = re.findall(r'got:\s+(sha256-[A-Za-z0-9+/]+=)', result.stdout + result.stderr)
            if result.returncode == 0 or len(set(hashes)) != 1:
                raise ValueError('could not resolve Cargo vendor hash:\n' + result.stderr)
            recipe['cargo_hash'] = hashes[0]
            manifest.write_text(json.dumps(data, indent=2) + '\n')
            command('nix', 'build', '--no-link', '.#codex.cargoDeps', cwd=root)
        except BaseException:
            manifest.write_bytes(original)
            raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('version')
    args = parser.parse_args()
    if not re.fullmatch(r'\d+\.\d+\.\d+', args.version):
        parser.error('expected stable semantic version')
    update(Path(__file__).resolve().parent.parent, args.version)
