#!/usr/bin/env python3
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('route', ROOT / 'scripts/route-release.py')
route = importlib.util.module_from_spec(spec)
spec.loader.exec_module(route)

SHA = 'a' * 40
VERSION = '0.171.0'
TAG = 'codex-codex-v' + VERSION + '-' + 'b' * 16 + '-' + 'c' * 16


def runner(status='online', busy=False, enabled=True):
    labels = ['self-hosted', 'linux', 'x64'] + (['release-build'] if enabled else [])
    return dict(id=5, status=status, busy=busy, labels=[{'name': name} for name in labels])


def check_job(job, status='online'):
    api = Mock()
    api.request.return_value = {'jobs': [dict(name='build', runner_id=5, steps=[], **job)]}
    api.runners.return_value = [runner(status=status, busy=True)]
    elapsed = 0

    def advance(seconds):
        nonlocal elapsed
        elapsed += seconds

    route.monitor(api, 10, clock=lambda: elapsed, sleep=advance)


class RoutingTests(unittest.TestCase):
    def test_availability(self):
        self.assertTrue(route.available(runner()))
        for other in (runner(enabled=False), runner(busy=True), runner(status='offline')):
            api = Mock()
            api.runners.return_value = [other]
            self.assertEqual(route.select(api, {}), 0)
            api.request.assert_not_called()

    def test_queue_race_and_running_disconnect(self):
        with self.assertRaises(route.Unavailable):
            check_job({'status': 'queued'})
        with self.assertRaises(route.Unavailable):
            check_job({'status': 'in_progress'}, 'offline')

    def test_preserves_failures(self):
        for conclusion in ('failure', 'cancelled', 'timed_out'):
            with self.assertRaises(route.IntegrityError):
                check_job({'status': 'completed', 'conclusion': conclusion})
        check_job({'status': 'completed', 'conclusion': 'success'})
        with self.assertRaises(route.Unavailable):
            check_job({'status': 'completed', 'conclusion': 'failure'}, 'offline')

    def test_failed_build_step_survives_offline_status(self):
        api = Mock()
        api.request.return_value = {'jobs': [dict(name='build', runner_id=5, status='completed',
            conclusion='failure', steps=[{'name': 'Compile', 'conclusion': 'failure'}])]}
        with self.assertRaises(route.IntegrityError):
            route.monitor(api, 10)
        api.runners.assert_not_called()

    def test_cancels_exact_attempt_when_monitor_loses_runner_or_parent_stops(self):
        for error in (route.Unavailable, KeyboardInterrupt):
            api = Mock()
            api.runners.return_value = [runner()]
            api.request.return_value = {'workflow_run_id': 12}
            with patch.object(route, 'monitor', side_effect=error), self.assertRaises(error):
                route.select(api, {})
            api.cancel.assert_called_once_with(12)

    def test_import_digest_provenance_and_inventory_before_promoting_either_set(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            items = []
            for kind, names in [('nix', [TAG + suffix for suffix in
                                        ('-x86_64-linux.nar.zst', '-manifest.json', '-SHA256SUMS')]),
                                ('native', ['codex-native-' + VERSION + '-' + 'd' * 64 + '-linux-x86_64.tar.gz'])]:
                artifact_id = len(items) + 1
                archive = root / str(artifact_id)
                with zipfile.ZipFile(archive, 'w') as z:
                    for name in names:
                        z.writestr(name, b'bytes')
                items.append(dict(id=artifact_id, name=kind + '-' + SHA, expired=False,
                                  workflow_run={'id': 12}, digest='sha256:' + hashlib.sha256(archive.read_bytes()).hexdigest()))
            api = Mock()
            api.request.return_value = {'artifacts': items}
            api.download.side_effect = lambda i, path: path.write_bytes((root / str(i)).read_bytes())
            with self.assertRaises(route.IntegrityError):
                route.import_assets(api, 13, SHA, VERSION, root)
            with self.assertRaises(route.IntegrityError):
                route.import_assets(api, 12, 'other', VERSION, root)
            with patch.dict(items[1], digest='incorrect'), self.assertRaises(route.IntegrityError):
                route.import_assets(api, 12, SHA, VERSION, root)
            self.assertFalse((root / 'nix-assets').exists())
            with zipfile.ZipFile(root / 'unsafe', 'w') as z:
                z.writestr('../escape', b'bytes')
            with self.assertRaises(route.IntegrityError):
                route.unpack(root / 'unsafe', 'native', root / 'bad', VERSION)
            route.import_assets(api, 12, SHA, VERSION, root)
            self.assertEqual(len(list((root / 'nix-assets').iterdir())), 3)
            self.assertEqual(len(list((root / 'native-assets').iterdir())), 1)


if __name__ == '__main__':
    unittest.main()
