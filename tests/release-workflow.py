#!/usr/bin/env python3
"""Guard one compilation, the combined release inventory, and download checks."""
from pathlib import Path
import os
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = (ROOT / '.github/workflows/update.yml').read_text()
# Job headers have exactly two spaces; nested YAML and embedded scripts do not.
PARTS = re.split(r'^  ([a-z][a-z0-9-]*):\s*$', WORKFLOW, flags=re.MULTILINE)
JOBS = dict(zip(PARTS[1::2], PARTS[2::2]))


def artifact_names(job, action):
    return set(re.findall(
        rf'uses: actions/{action}-artifact@[^\n]+\n(?:\s+if:[^\n]+\n)?\s+with:\n\s+name: ([^\n]+)', job))


class ReleaseWorkflowTests(unittest.TestCase):
    def test_native_metadata_token_is_isolated_from_download_and_execution(self):
        job = JOBS['native-download-smoke']
        metadata, smoke = job.split('      - name: Download, verify, and smoke-test the published release', 1)
        self.assertEqual(metadata.count('GITHUB_TOKEN: ${{ github.token }}'), 1)
        self.assertIn("token=os.environ['GITHUB_TOKEN']", metadata)
        self.assertIn("'release-metadata.json').write_text(json.dumps(releases))", metadata)
        self.assertNotIn('client.prepare(', metadata)
        self.assertNotIn('client.download(', metadata)
        self.assertNotIn('subprocess', metadata)
        self.assertNotIn('env:', smoke)
        self.assertNotIn('TOKEN', smoke)
        self.assertNotIn('client.request(', smoke)
        self.assertIn("'release-metadata.json').read_text()", smoke)
        self.assertIn('client.prepare(client.select(releases, version)', smoke)
        self.assertIn('permissions:\n  contents: read', WORKFLOW)

    def test_recovery_upload_requires_successful_scan_on_failure_path(self):
        prepare = JOBS['prepare']
        check = prepare.split('name: Check failed patch replay diagnostics', 1)[1].split(
            'name: Preserve checked patch replay diagnostics', 1)[0]
        self.assertIn("if: failure() && steps.candidate.outcome == 'failure'", check)
        self.assertIn('set -euo pipefail', check)
        self.assertLess(check.index('scripts/publication_gate.py recovery'),
                        check.index("echo 'checked=true'"))
        upload = prepare.split('name: Preserve checked patch replay diagnostics', 1)[1].split(
            'retention-days: 14', 1)[0]
        self.assertIn("if: failure() && steps.recovery.outcome == 'success' && steps.recovery.outputs.checked == 'true'", upload)
        self.assertIn('path: ${{ runner.temp }}/update-failure.json\n', upload)
        self.assertIn('if-no-files-found: error', upload)
        self.assertNotIn('include-hidden-files', prepare)
        self.assertNotIn('continue-on-error', check + upload)

    def test_fresh_update_checks_before_publication(self):
        prepare = JOBS['prepare']
        self.assertLess(prepare.index('python3 tests/publication-gate.py'),
                        prepare.index('name: Prepare candidate without publishing it'))
        self.assertLess(prepare.index('scripts/publication_gate.py bundle'),
                        prepare.index('uses: actions/upload-artifact@'))
        self.assertNotIn('git bundle create', prepare)
        self.assertIn("if: steps.candidate.outputs.changed == 'true'", prepare)
        self.assertIn('bundle --base \"$base_sha\"', prepare)
        for name in ('nix', 'publish'):
            self.assertIn("if: needs.prepare.outputs.changed == 'true'", JOBS[name])
            self.assertIn('restore-bundle', JOBS[name])
            self.assertIn('--sha256 \"$BUNDLE_SHA256\"', JOBS[name])
            self.assertIn('test \"$EXPECTED_SHA\" = \"$EXPECTED_BASE\"', JOBS[name])
        publish = JOBS['publish']
        self.assertLess(publish.index('git config --local core.hooksPath .githooks'),
                        publish.index('git push --force-with-lease='))
        self.assertIn('git config --local publication.identity', prepare)
        self.assertIn('git config --local publication.identity', publish)

    def test_one_compilation_exports_both_formats(self):
        self.assertNotIn('native', JOBS)
        self.assertEqual(len(re.findall(r'nix .* build .*\.#codex\b', WORKFLOW)), 1)
        self.assertNotIn('--build-only', WORKFLOW)
        self.assertNotIn('setup-musl-toolchain.sh', WORKFLOW)
        build = JOBS['nix']
        self.assertIn('scripts/export-native.py --package result', build)
        self.assertIn('scripts/publish-bundle.sh --prepare-dir', build)
        self.assertEqual(artifact_names(build, 'upload'), {'release-nix', 'release-native'})
        self.assertIn('tests/static-portability.sh result', build)

    def test_publication_still_requires_complete_asset_pair(self):
        publish = JOBS['publish']
        self.assertRegex(publish, r'needs: \[prepare, nix\]')
        self.assertIn("if: github.ref == 'refs/heads/main'", publish)
        self.assertEqual(artifact_names(publish, 'download'),
                         {'release-source', 'release-nix', 'release-native'})
        self.assertIn('scripts/publish-release.py --assets', publish)
        self.assertIn('--tag "$CODEX_BUNDLE_TAG"', publish)
        self.assertIn('--output-path "$CODEX_BUNDLE_OUTPUT_PATH"', publish)

    def test_both_fresh_download_checks_remain(self):
        for name in ('install-smoke', 'native-download-smoke'):
            self.assertRegex(JOBS[name], r'needs: \[prepare, publish\]')
        self.assertIn('uses: ./.github/workflows/install-smoke.yml', JOBS['install-smoke'])
        self.assertIn('client.prepare(client.select(releases, version)', JOBS['native-download-smoke'])

    def test_patch_selection_and_memory_headroom_remain(self):
        package = (ROOT / 'package.nix').read_text()
        self.assertIn('doCheck = true;', package)
        self.assertIn('useNextest = true;', package)
        self.assertIn('test(/(^|::|_)esc/)', package)
        self.assertIn('"-p" "codex-core"', package)
        self.assertIn('package(codex-tui)&(', package)
        self.assertIn('test(add_dir_)', package)
        native = (ROOT / 'scripts/install-source-codex.py').read_text()
        self.assertIn('test(/(^|::|_)esc/)', native)
        self.assertNotIn('test(esc)', package + native)
        for selector in ('palette_refresh', 'hook_status_reflows',
                         'running_hooks_fit', 'compaction_status', 'usage_notice',
                         'external_editor_reference'):
            self.assertIn(f'test({selector})', package)
            self.assertIn(f'test({selector})', native)
        self.assertIn('"-p" "codex-config"', package)
        self.assertIn('export NIX_BUILD_CORES=2', package)
        # The Nix nextest hook supplies -j for test concurrency. Compilation has
        # its own --build-jobs flag; duplicating --test-threads is a CLI error.
        self.assertIn('"--build-jobs" "2"', package)
        self.assertNotIn('"--test-threads', package)
        self.assertIn('pkgs.pkgsStatic.callPackage ./package.nix', (ROOT / 'flake.nix').read_text())
        for name in ('update', 'check'):
            workflow = (ROOT / f'.github/workflows/{name}.yml').read_text()
            self.assertIn('bash scripts/setup-build-swap.sh', workflow)
            self.assertIn('apt-get install -y bubblewrap', workflow)
            self.assertIn('--command python3 tests/nextest-flags.py', workflow)

    def test_swap_setup_refuses_local_machine(self):
        result = subprocess.run(['bash', str(ROOT / 'scripts/setup-build-swap.sh')],
                                env={'PATH': os.environ['PATH']}, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('refusing to modify a non-hosted runner', result.stderr)


if __name__ == '__main__':
    unittest.main()
