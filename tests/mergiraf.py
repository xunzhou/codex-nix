#!/usr/bin/env python3
"""Contract tests against the actual pinned Mergiraf binary, not a mock resolver."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('rebase', ROOT / 'scripts/rebase-patches.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


@unittest.skipUnless(os.environ.get('CODEX_MERGIRAF'), 'set CODEX_MERGIRAF to test the real binary')
class MergirafTests(unittest.TestCase):
    def merge(self, base, upstream, patched):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(m, 'git', side_effect=[s.encode() for s in (base, upstream, patched)]):
                return m.merge_rust_conflict(Path(directory), 'interaction.rs')

    def test_return_type_and_entry_reset(self):
        base = '''impl ChatWidget {
    pub(crate) fn handle_key_event(&mut self, key_event: KeyEvent) {
        self.handle_question_key(key_event);
    }
}
'''
        upstream = base.replace('key_event: KeyEvent) {', 'key_event: KeyEvent) -> KeyEventAction {').replace(
            '        self.handle_question_key(key_event);',
            '        self.handle_question_key(key_event);\n        KeyEventAction::None')
        reset = '        self.bottom_pane.reset_interrupt_tap_on_other_key(key_event);\n'
        patched = base.replace('        self.handle_question_key', reset + '        self.handle_question_key')
        expected = upstream.replace('        self.handle_question_key', reset + '        self.handle_question_key')
        self.assertEqual(self.merge(base, upstream, patched), expected)

    def test_conflicting_behavior_remains_unresolved(self):
        base = 'fn delay() -> u64 {\n    400\n}\n'
        self.assertIsNone(self.merge(base, base.replace('400', '500'), base.replace('400', '600')))

    def test_independent_imports(self):
        base = 'use std::time::Instant;\n\nfn main() {}\n'
        upstream = 'use std::time::Duration;\n' + base
        patched = 'use std::path::Path;\n' + base
        result = self.merge(base, upstream, patched)
        self.assertIsNotNone(result)
        for line in ('use std::time::Instant;', 'use std::time::Duration;', 'use std::path::Path;'):
            self.assertEqual(result.count(line), 1)


if __name__ == '__main__':
    unittest.main()
