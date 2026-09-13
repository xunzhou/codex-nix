#!/usr/bin/env python3
"""Exercise the source installer against an npm layout and fake Cargo builds."""
import json
import hashlib
import importlib.util
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / 'scripts/install-source-codex.py'
MODULE_SPEC = importlib.util.spec_from_file_location('source_patcher', INSTALLER)
PATCHER = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(PATCHER)


class BuildAssetsTests(unittest.TestCase):
    def test_pinned_assets_are_verified_cached_and_exported(self):
        contents = b'test prebuilt input'
        checksum = hashlib.sha256(contents).hexdigest()
        spec = {'build_assets': {'test-target': {
            name: {'url': 'https://example.invalid/' + name, 'sha256': checksum}
            for name in ('RUSTY_V8_ARCHIVE', 'RUSTY_V8_SRC_BINDING_PATH')
        }}}
        def download(*args):
            Path(args[5]).write_bytes(contents)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(PATCHER, 'run', side_effect=download) as fetch:
            cache = Path(directory)
            env = {'CARGO_BUILD_TARGET': 'test-target'}
            PATCHER.prepare_build_assets(spec, cache, env)
            for name in spec['build_assets']['test-target']:
                self.assertEqual(Path(env[name]).read_bytes(), contents)
            self.assertEqual(fetch.call_count, 2)
            PATCHER.prepare_build_assets(spec, cache, {'CARGO_BUILD_TARGET': 'test-target'})
            self.assertEqual(fetch.call_count, 2)
            Path(env['RUSTY_V8_ARCHIVE']).write_bytes(b'corrupt')
            PATCHER.prepare_build_assets(spec, cache, {'CARGO_BUILD_TARGET': 'test-target'})
            self.assertEqual(fetch.call_count, 3)

    def test_bad_asset_is_rejected_and_explicit_overrides_are_preserved(self):
        spec = {'build_assets': {'test-target': {'RUSTY_V8_ARCHIVE': {
            'url': 'https://example.invalid/v8', 'sha256': '0' * 64
        }}}}
        def download(*args):
            Path(args[5]).write_bytes(b'wrong checksum')
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(PATCHER, 'run', side_effect=download) as fetch:
            env = {'CARGO_BUILD_TARGET': 'test-target'}
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                PATCHER.prepare_build_assets(spec, Path(directory), env)
            self.assertNotIn('RUSTY_V8_ARCHIVE', env)
            overrides = {'RUSTY_V8_ARCHIVE': '/custom/archive', 'RUSTY_V8_SRC_BINDING_PATH': '/custom/bindings'}
            PATCHER.prepare_build_assets(spec, Path(directory), overrides)
            self.assertEqual(fetch.call_count, 1)


class SourcePatcherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.package = self.root / 'npm'
        self.vendor = self.package / 'node_modules/platform/vendor/target/bin'
        self.vendor.mkdir(parents=True)
        (self.package / 'package.json').write_text('{"version":"0.154.0"}')
        for name in ('codex', 'codex-code-mode-host'):
            (self.vendor / name).write_text('stock ' + name)
        self.patches = self.root / 'patches'
        self.patches.mkdir(parents=True)
        self.spec = {'patches': ['first.patch', 'second.patch'], 'markers': ['palette-marker']}
        for name, before, after in [('first', 'before', 'middle'), ('second', 'middle', 'after')]:
            (self.patches / f'{name}.patch').write_text(f'--- a/codex-rs/probe\n+++ b/codex-rs/probe\n@@ -1 +1 @@\n-{before}\n+{after}\n')
        source = self.root / 'source/release/codex-rs'
        source.mkdir(parents=True)
        (source / 'probe').write_text('before\n')
        (source / 'Cargo.toml').write_text('[workspace.package]\nversion="0.154.0"\n[package]\nname="codex-cli"\nversion.workspace=true\n')
        (source / 'Cargo.lock').write_text('[[package]]\nname = "codex-cli"\nversion = "0.0.0"\n')
        self.archive = self.root / 'source.tar.gz'
        with tarfile.open(self.archive, 'w:gz') as archive:
            archive.add(source.parent, arcname='release')
        cargo = self.root / 'cargo'
        cargo.write_text('''#!/usr/bin/env python3
import os,sys
from pathlib import Path
assert Path('probe').read_text() == 'after\\n'
assert 'version = "0.154.0"' in Path('Cargo.lock').read_text()
if sys.argv[1]=='metadata': sys.exit(0)
if os.environ.get('FAIL_BUILD'): sys.exit(42)
assert 'codex-code-mode-host' in sys.argv
count=Path(os.environ['BUILD_COUNT']);count.write_text(str(int(count.read_text())+1) if count.exists() else '1')
p=Path(os.environ['CARGO_TARGET_DIR'])/'release';p.mkdir(parents=True,exist_ok=True)
for name in ('codex','codex-code-mode-host'):
 f=p/name;f.write_text('#!/bin/sh\\n# palette-marker\\n# '+name+'\\necho "codex-cli 0.154.0"\\n');f.chmod(0o755)
''')
        cargo.chmod(0o755)
        self.write_spec()
        self.env = dict(os.environ, CODEX_INSTALL_MODE='source', CODEX_PACKAGE_ROOT=str(self.package), CODEX_PATCH_ROOT=str(self.patches), CODEX_SOURCE_ARCHIVE=str(self.archive), CODEX_PATCH_CACHE=str(self.root / 'cache'), CODEX_CARGO_COMMAND=str(cargo), CODEX_STRIP_COMMAND='true', CODEX_PRIVILEGE_COMMAND='', BUILD_COUNT=str(self.root / 'count'))

    def write_spec(self):
        release = dict(self.spec, profile='test', source={'url':'https://example.invalid/source.tar.gz','sha256':hashlib.sha256(self.archive.read_bytes()).hexdigest()})
        manifest = {'schema_version':1,'default_version':'0.154.0','release_repository':'example/test','profiles':{'test':{}},
                    'build':{'binaries':{'codex-code-mode-host':{'package':'codex-code-mode-host','smoke_args':['--help']},'codex':{'package':'codex-cli','smoke_args':['--version']}}},
                    'releases':{'0.154.0':release}}
        (self.root / 'build.json').write_text(json.dumps(manifest))

    def invoke(self, success=True, args=(), **env):
        result = subprocess.run([sys.executable, str(INSTALLER), *args], env=dict(self.env, **env), capture_output=True, text=True)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def test_ci_bundle_download_installs_without_cargo(self):
        self.check_bundle_download(unified=False)

    def test_combined_release_download_installs_without_cargo(self):
        self.check_bundle_download(unified=True)

    def check_bundle_download(self, unified):
        archive = self.root / 'published.tar.gz'
        self.invoke(args=('--build-only', '--version', '0.154.0', '--output', str(archive)))
        self.assertEqual((self.vendor / 'codex').read_text(), 'stock codex')
        for cached in (self.root / 'cache').glob('0.154.0-*'):
            shutil.rmtree(cached)
        commands = self.root / 'commands'
        commands.mkdir()
        curl = commands / 'curl'
        curl.write_text('''#!/usr/bin/env python3
import json,os,shutil,sys,tarfile
url=sys.argv[-1]
output=sys.argv[sys.argv.index('--output')+1]
if os.environ['UNIFIED'] == '1':
    if '/releases/download/native-' in url:
        print('404',end='');sys.exit(0)
    with tarfile.open(os.environ['PUBLISHED_BUNDLE']) as bundle:
        key=json.load(bundle.extractfile('bundle.json'))['key']
    name='codex-native-'+key+'-linux-x86_64.tar.gz'
    tag='bundle-codex-v0.154.0-'+('a'*16)+'-'+('b'*16)
    if '/releases?per_page=100&page=1' in url:
        with open(output,'w') as stream:
            json.dump([{'tag_name':tag,'assets':[{'name':name}]}],stream)
        print('200',end='');sys.exit(0)
    assert url.endswith('/releases/download/'+tag+'/'+name)
else:
    assert '/releases/download/native-0.154.0-' in url
shutil.copyfile(os.environ['PUBLISHED_BUNDLE'],output)
print('200',end='')
''')
        curl.chmod(0o755)
        self.invoke(CODEX_INSTALL_MODE='download', PUBLISHED_BUNDLE=str(archive), UNIFIED='1' if unified else '0',
                    PATH=str(commands)+os.pathsep+os.environ['PATH'], FAIL_BUILD='1')
        self.assertEqual((self.root / 'count').read_text(), '1')
        self.assertIn('palette-marker', (self.vendor / 'codex').read_text())

    def test_unexpected_archive_bytes_fail_before_cargo(self):
        self.archive.write_bytes(b'not the pinned source')
        result = self.invoke(success=False)
        self.assertIn('source archive checksum mismatch', result.stderr)
        self.assertFalse((self.root / 'count').exists())

    def test_download_rejects_recipe_mismatch(self):
        archive = self.root / 'published.tar.gz'
        self.invoke(args=('--build-only', '--version', '0.154.0', '--output', str(archive)))
        spec = PATCHER.load_recipe(self.root / 'build.json', '0.154.0')
        with mock.patch.object(PATCHER, 'http_download', side_effect=lambda url, out: (shutil.copyfile(archive, out), True)[1]):
            with self.assertRaisesRegex(ValueError, 'does not match'):
                PATCHER.download_bundle(spec, 'wrong-recipe', self.root / 'cache', self.root / 'destination', '0.154.0', ['codex', 'codex-code-mode-host'])
        self.assertFalse((self.root / 'destination').exists())

    def test_cache_restores_both_binaries_and_patch_change_rebuilds(self):
        self.invoke()
        originals = {name: (self.vendor / name).read_bytes() for name in ('codex', 'codex-code-mode-host')}
        self.invoke()
        for name in originals:
            (self.vendor / name).write_text('npm overwrote this')
        self.invoke()
        self.assertEqual((self.root / 'count').read_text(), '1')
        self.assertEqual({name: (self.vendor / name).read_bytes() for name in originals}, originals)
        patch = self.patches / 'second.patch'
        patch.write_text('# revised patch identity\n' + patch.read_text())
        self.invoke()
        self.assertEqual((self.root / 'count').read_text(), '2')

    def test_unsupported_version_and_build_failure_preserve_installation(self):
        self.invoke(success=False, FAIL_BUILD='1')
        self.assertEqual((self.vendor / 'codex').read_text(), 'stock codex')
        (self.package / 'package.json').write_text('{"version":"0.155.0"}')
        result = self.invoke(success=False)
        self.assertIn('no reviewed patch set', result.stderr)
        self.assertFalse((self.root / 'count').exists())

    def test_corrupt_cached_companion_forces_rebuild(self):
        self.invoke()
        cached = next((self.root / 'cache').glob('0.154.0-*/codex-code-mode-host'))
        cached.write_text('corrupted')
        self.invoke()
        self.assertEqual((self.root / 'count').read_text(), '2')

    def test_reversed_patch_order_is_rejected_before_build(self):
        self.spec['patches'].reverse()
        self.write_spec()
        self.invoke(success=False)
        self.assertFalse((self.root / 'count').exists())
        self.assertEqual((self.vendor / 'codex').read_text(), 'stock codex')

    def test_install_failure_rolls_back_companion(self):
        privilege = self.root / 'privilege'
        privilege.write_text('''#!/usr/bin/env python3
import os,subprocess,sys
from pathlib import Path
args=sys.argv[1:]
if args[0]=='mv' and args[-1].endswith('/codex') and not args[-2].endswith('.bak'):
 sys.exit(51)
sys.exit(subprocess.call(args))
''')
        privilege.chmod(0o755)
        self.invoke(success=False, CODEX_PRIVILEGE_COMMAND=str(privilege))
        for name in ('codex','codex-code-mode-host'):
            self.assertEqual((self.vendor / name).read_text(), 'stock '+name)

    def test_setup_replaces_only_codex_step_and_is_idempotent(self):
        prefix = self.root / 'home'
        (prefix / 'bin').mkdir(parents=True)
        updater = prefix / 'bin/update-agents'
        before = '#!/bin/bash\necho before\nstep "Building/installing Codex live-palette patch"\n"$HOME/.config/catppuccin/install-patched-codex"\nok "Codex live-palette patch installed"\necho after\n'
        updater.write_text(before)
        updater.chmod(0o755)
        command = [sys.executable, str(ROOT / 'scripts/setup-source-patcher.py'), '--prefix', str(prefix)]
        subprocess.run(command, check=True, capture_output=True)
        after = updater.read_text()
        self.assertIn('"$HOME/bin/install-patched-codex"', after)
        self.assertTrue(after.endswith('echo after\n'))
        self.assertEqual(updater.with_name('update-agents.before-source-patcher').read_text(), before)
        subprocess.run(command, check=True, capture_output=True)
        self.assertEqual(updater.read_text(), after)
        self.assertTrue((prefix / '.local/share/codex-patcher/build.json').is_file())


if __name__ == '__main__':
    unittest.main()
