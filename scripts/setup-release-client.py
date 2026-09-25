#!/usr/bin/env python3
"""Install the standalone release downloader as bin/install-patched-codex."""
import argparse
from pathlib import Path
import shlex
import tempfile


def atomic_write(path, content, mode):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        pending = Path(stream.name)
        stream.write(content)
    try:
        pending.chmod(mode)
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def install(directory):
    directory.mkdir(parents=True, exist_ok=True)
    command = directory / 'install-patched-codex'
    if command.exists():
        previous = command.read_text()
        if not any(name in previous for name in ('install-local-codex.sh', 'codex-patcher/scripts/install-source-codex.py', '.codex-release-client.py')):
            raise ValueError('refusing to replace an unrelated install-patched-codex command')
        backup = directory / 'install-patched-codex.before-release-client'
        if not backup.exists():
            atomic_write(backup, command.read_bytes(), command.stat().st_mode & 0o777)
    client = directory / '.codex-release-client.py'
    source = Path(__file__).resolve().with_name('install-release.py')
    atomic_write(client, source.read_bytes(), 0o755)
    wrapper = '#!/bin/sh\nexec python3 ' + shlex.quote(str(client)) + ' "$@"\n'
    atomic_write(command, wrapper.encode(), 0o755)
    updater = directory / 'update-agents'
    if updater.is_file():
        current = updater.read_text()
        if '--reviewed-version' in current and '@openai/codex@$codex_reviewed_version' in current:
            updated = current.replace('Building/installing Codex source patches', 'Downloading/installing patched Codex')
            updated = updated.replace('Codex source patches installed', 'Patched Codex release installed')
            updated = updated.replace('Codex source patches failed', 'Patched Codex installation failed')
            if updated != current:
                backup = directory / 'update-agents.before-release-client'
                if not backup.exists():
                    atomic_write(backup, updater.read_bytes(), updater.stat().st_mode & 0o777)
                atomic_write(updater, updated.encode(), updater.stat().st_mode & 0o777)
    print(f'Installed {command}. Run it with --install-reviewed to install the latest published patched release.')

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bin-dir', type=Path, default=Path.home() / 'bin')
    install(parser.parse_args().bin_dir.resolve())
