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
SELECTORS = ('esc', 'escape_closes', 'backtrack_esc', 'vim_escape_recovery',
             'palette_refresh', 'hook_status_reflows', 'running_hooks_fit',
             'compaction_status', 'usage_notice', 'add_dir_confirmation', 'external_editor_reference')


def main():
    data = json.loads(subprocess.check_output([
        'nix', '--extra-experimental-features', 'nix-command flakes',
        'derivation', 'show', str(ROOT) + '#codex'], text=True))
    attributes = next(iter(data['derivations'].values()))['env']
    if attributes['cargoBuildType'] != 'release' or attributes['cargoCheckType'] != 'ci-test':
        raise SystemExit('expected release binaries and ci-test regression tests')
    # This deliberately matches stdenv's concatTo behavior for string flags.
    flags = attributes['cargoTestFlags'].split()
    with tempfile.TemporaryDirectory(prefix='codex-nextest-flags-') as directory:
        fixture = Path(directory)
        (fixture / 'Cargo.toml').write_text(
            '[workspace]\nmembers=["tui", "core", "config"]\nresolver="2"\n'
            '[profile.ci-test]\ninherits="test"\nopt-level=0\n')
        for name in ('tui', 'core', 'config'):
            package = fixture / name
            package.mkdir()
            (package / 'Cargo.toml').write_text(
                f'[package]\nname="codex-{name}"\nversion="0.0.0"\nedition="2024"\n'
                '[lib]\npath="lib.rs"\n')
        (fixture / 'tui/lib.rs').write_text(''.join(
            f'#[test] fn {name}() {{}}\n' for name in SELECTORS)
            + 'mod nested { #[test] fn escape_closes() {} }\n'
            + ''.join(f'#[test] fn {name}() {{ panic!("unrelated test selected"); }}\n'
                      for name in ('unrelated_should_not_run',
                                   'consumer_roots_include_paginated_archived_descendants_and_keep_missing_usage',
                                   'reschedule_timer', 'coalesces_events')))
        (fixture / 'config/lib.rs').write_text(
            '#[test] fn external_editor_reference_config() { assert!(cfg!(debug_assertions)); }\n'
            '#[test] fn unrelated_config() { panic!("unrelated config selected"); }\n')
        (fixture / 'core/lib.rs').write_text(
            '#[test] fn add_dir_policy() {}\n'
            '#[test] fn esc_core_unrelated() { panic!("core Escape test selected"); }\n'
            '#[test] fn usage_notice_core_unrelated() { panic!("core usage test selected"); }\n')
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
        expected = len(SELECTORS) + 3  # Nested Escape, core policy, editor config.
        if result.returncode or not re.search(rf'{expected} tests run:\s+{expected} passed', output):
            raise SystemExit('nextest flag preflight failed:\n' + output)
        print(f'nextest flag preflight: all {expected} selected tests passed; unrelated tests excluded')


if __name__ == '__main__':
    main()
