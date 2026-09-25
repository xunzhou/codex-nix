#!/usr/bin/env python3
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('setup_release', ROOT / 'scripts/setup-release-client.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class SetupTests(unittest.TestCase):
    def test_known_launcher_is_backed_up_and_setup_is_repeatable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = '#!/bin/sh\nexec /checkout/scripts/install-local-codex.sh "$@"\n'
            (root / 'install-patched-codex').write_text(old)
            m.install(root)
            m.install(root)
            self.assertEqual((root / 'install-patched-codex.before-release-client').read_text(), old)
            self.assertEqual((root / '.codex-release-client.py').read_bytes(), (ROOT / 'scripts/install-release.py').read_bytes())
    def test_unrelated_command_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'install-patched-codex').write_text('unrelated')
            with self.assertRaises(ValueError):
                m.install(root)
            self.assertEqual((root / 'install-patched-codex').read_text(), 'unrelated')

if __name__ == '__main__':
    unittest.main()
