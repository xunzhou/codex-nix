#!/usr/bin/env python3
"""Check rendered Nix nextest flags against a tiny host fixture before Codex builds.

Run with the pinned Cargo, rustc, GCC, and cargo-nextest on PATH, as in CI.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent
SELECTORS = ('esc', 'palette_refresh', 'hook_status_reflows', 'running_hooks_fit',
             'compaction_status', 'usage_notice')


def main():
    data = json.loads(subprocess.check_output([
        'nix', '--extra-experimental-features', 'nix-command flakes',
        'derivation', 'show', str(ROOT) + '#codex'], text=True))
    attributes = next(iter(data['derivations'].values()))['env']
    # This deliberately matches stdenv's concatTo behavior for string flags.
    flags = attributes['cargoTestFlags'].split()
    with tempfile.TemporaryDirectory(prefix='codex-nextest-flags-') as directory:
        fixture = Path(directory)
        (fixture / 'Cargo.toml').write_text(
            '[package]\nname="codex-tui"\nversion="0.0.0"\nedition="2024"\n'
            '[lib]\npath="lib.rs"\n')
        (fixture / 'lib.rs').write_text(''.join(
            f'#[test] fn {name}() {{}}\n' for name in SELECTORS)
            + '#[test] fn unrelated_should_not_run() { panic!("filter was lost"); }\n')
        environment = dict(os.environ, CARGO=shutil.which('cargo'),
                           CARGO_HOME=str(fixture / 'cargo-home'),
                           CARGO_TARGET_DIR=str(fixture / 'target'),
                           CARGO_TERM_COLOR='never', XDG_CACHE_HOME=str(fixture / 'cache'),
                           XDG_STATE_HOME=str(fixture / 'state'))
        for key in ('RUSTFLAGS', 'CARGO_ENCODED_RUSTFLAGS', 'RUSTC',
                    'RUSTC_WRAPPER', 'RUSTC_WORKSPACE_WRAPPER'):
            environment.pop(key, None)
        version = subprocess.check_output(['rustc', '-vV'], text=True, env=environment)
        host = next(line.removeprefix('host: ') for line in version.splitlines()
                    if line.startswith('host: '))
        # Match cargoNextestHook, using the host target only for this tiny test.
        command = ['cargo', 'nextest', 'run', '--target', host, '--offline',
                   '--show-progress=none', '-j', '2', '--cargo-profile',
                   attributes['cargoCheckType'], '--user-config-file', 'none',
                   *flags, '--']
        result = subprocess.run(command, cwd=fixture, env=environment,
                                capture_output=True, text=True)
        output = result.stdout + result.stderr
        if result.returncode or not re.search(r'6 tests run:\s+6 passed', output):
            raise SystemExit('nextest flag preflight failed:\n' + output)
        print('nextest flag preflight: all 6 selected tests passed; unrelated test excluded')


if __name__ == '__main__':
    main()
