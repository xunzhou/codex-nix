#!/usr/bin/env python3
"""Rebase a patch stack using verified old source; never guess conflict resolutions."""
import hashlib
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile


def git(root, *args):
    return subprocess.check_output(['git', '-c', 'user.name=Codex updater', '-c',
        'user.email=codex-updater@localhost', '-C', str(root), *args])


def rebase(root, source, previous, patches, version):
    with tempfile.TemporaryDirectory(prefix='codex-rebase-') as directory:
        scratch = Path(directory)
        archive = scratch / 'old.tar.gz'
        subprocess.run(['curl', '-fL', '--retry', '3', '-o', str(archive), previous['source']['url']], check=True)
        with archive.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != previous['source']['sha256']:
                raise ValueError('previous source checksum mismatch')
        unpacked = scratch / 'old'
        unpacked.mkdir()
        with tarfile.open(archive) as tar:
            tar.extractall(unpacked, filter='data')
        roots = list(unpacked.iterdir())
        if len(roots) != 1 or not roots[0].is_dir():
            raise ValueError('invalid previous source archive')
        tree = roots[0]
        git(tree, 'init', '-q')
        git(tree, 'add', '-f', '.')
        git(tree, 'commit', '-qm', 'verified previous source')
        full = []
        for name in patches:
            git(tree, 'apply', '--index', str(root / 'patches' / name))
            full.append(git(tree, 'diff', '--cached', '--binary', '--full-index'))
            git(tree, 'commit', '-qm', name)
        # Preserve the old blobs for Git's three-way merge, replacing only the worktree.
        for item in tree.iterdir():
            if item.name != '.git':
                shutil.rmtree(item) if item.is_dir() and not item.is_symlink() else item.unlink()
        shutil.copytree(source, tree, dirs_exist_ok=True, symlinks=True)
        git(tree, 'add', '-A', '-f')
        git(tree, 'commit', '-qm', 'new upstream source')
        rebased = []
        for index, patch in enumerate(full):
            path = scratch / f'{index}.patch'
            path.write_bytes(patch)
            # Nonzero (including any conflict) aborts before publishing any patch.
            git(tree, 'apply', '--3way', '--index', str(path))
            diff = git(tree, 'diff', '--cached', '--binary', '--full-index')
            if not diff:
                raise ValueError('patch became empty; review whether upstream superseded it')
            rebased.append(diff)
            git(tree, 'commit', '-qm', patches[index])
        names = [f'{version}/{Path(name).name}' for name in patches]
        for name, patch in zip(names, rebased):
            destination = root / 'patches' / name
            if destination.exists():
                raise ValueError('refusing to overwrite a reviewed patch')
        for name, patch in zip(names, rebased):
            destination = root / 'patches' / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(patch)
        for item in source.iterdir():
            shutil.rmtree(item) if item.is_dir() and not item.is_symlink() else item.unlink()
        shutil.copytree(tree, source, dirs_exist_ok=True, symlinks=True, ignore=shutil.ignore_patterns('.git'))
        return names
