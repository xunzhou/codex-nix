#!/usr/bin/env python3
"""Exercise the source installer against an npm layout and fake Cargo builds."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / 'scripts/install-source-codex.py'


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
        (self.patches / 'series').mkdir(parents=True)
        self.spec = {'patches': ['first.patch', 'second.patch'], 'markers': ['palette-marker']}
        self.write_spec()
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
        self.env = dict(os.environ, CODEX_PACKAGE_ROOT=str(self.package), CODEX_PATCH_ROOT=str(self.patches), CODEX_SOURCE_ARCHIVE=str(self.archive), CODEX_PATCH_CACHE=str(self.root / 'cache'), CODEX_CARGO_COMMAND=str(cargo), CODEX_STRIP_COMMAND='true', CODEX_PRIVILEGE_COMMAND='', BUILD_COUNT=str(self.root / 'count'))

    def write_spec(self):
        (self.patches / 'series/0.154.0.json').write_text(json.dumps(self.spec))

    def invoke(self, success=True, **env):
        result = subprocess.run([sys.executable, str(INSTALLER)], env=dict(self.env, **env), capture_output=True, text=True)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

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
        self.assertTrue((prefix / '.local/share/codex-patcher/patches/series/0.154.0.json').is_file())


if __name__ == '__main__':
    unittest.main()
