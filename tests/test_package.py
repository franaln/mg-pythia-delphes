"""Installed package entry points, legacy compatibility and public API checks."""
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
# Source-checkout test runs are supported; installed runs import the wheel first.
try:
    import mgpd
except ModuleNotFoundError as exc:
    if exc.name != 'mgpd':
        raise
    sys.path.insert(0, str(REPO / 'src'))
    import mgpd


class PackageTests(unittest.TestCase):
    def distribution(self):
        try:
            return importlib.metadata.distribution('mg-pythia-delphes')
        except importlib.metadata.PackageNotFoundError:
            self.skipTest('Install the package to test distribution metadata and console entry points')

    def launcher(self):
        self.distribution()
        launcher = Path(sys.executable).parent / 'mgpd'
        self.assertTrue(launcher.is_file())
        return str(launcher)

    def test_public_api_exports_real_workflow_functions(self):
        from mgpd import config, preparation, execution, condor, results, recovery
        self.assertIs(mgpd.load_config, config.load_config)
        self.assertIs(mgpd.prepare_production, preparation.prepare_production)
        self.assertIs(mgpd.execute_production, execution.execute_production)
        self.assertIs(mgpd.production_status, condor.production_status)
        self.assertIs(mgpd.validate_production, results.validate_production)
        self.assertIs(mgpd.prepare_recovery, recovery.prepare_recovery)

    def test_distribution_metadata_and_optional_dependencies(self):
        distribution = self.distribution()
        self.assertEqual(distribution.version, mgpd.__version__)
        entry = next(item for item in distribution.entry_points if item.name == 'mgpd')
        self.assertEqual(entry.value, 'mgpd.cli:main')
        requirements = distribution.requires or []
        self.assertTrue(any(item.startswith('PyYAML') for item in requirements))
        uproot = [item for item in requirements if item.startswith('uproot')]
        self.assertTrue(uproot)
        self.assertTrue(all('extra ==' in item for item in uproot))

    def test_installed_commands_outside_checkout(self):
        launcher = self.launcher()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            commands = [[launcher, '--version'], [launcher, '--help'],
                        [sys.executable, '-m', 'mgpd', '--version']]
            env = dict(os.environ)
            env.pop('PYTHONPATH', None)
            for command in commands:
                with self.subTest(command=command):
                    result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(mgpd.__version__ if '--version' in command else 'create', result.stdout)

    def test_console_and_module_cli_prepare_identical_inputs(self):
        launcher = self.launcher()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'run_card.dat').write_text('')
            (root / 'config.yml').write_text('''run:
  name: package_test
  mode: local-apptainer
  image: placeholder.sif
  nevents: 2
  outputs: [lhe]
process: generate p p > e+ e-
cards:
  run: run_card.dat
options:
  seed: 101
''')
            for command, output in [([launcher], 'console'),
                                    ([sys.executable, '-m', 'mgpd'], 'module')]:
                result = subprocess.run(command + ['prepare', 'config.yml', '-o', output],
                                        cwd=root, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
            def inputs(directory):
                return {str(path.relative_to(directory)): path.read_bytes()
                        for path in directory.rglob('*') if path.is_file() and path.name != 'manifest.json'}
            self.assertEqual(inputs(root / 'console'), inputs(root / 'module'))
            for name in ('console', 'module'):
                manifest = json.loads((root / name / 'manifest.json').read_text())
                self.assertEqual(manifest['jobs'][0]['seed'], 101)
                self.assertNotIn('execution_attempt', manifest)

    def test_prepackage_schema2_fixture_is_still_loadable(self):
        fixture = REPO / 'tests/fixtures/legacy_schema2'
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'production'
            shutil.copytree(fixture, output)
            production = mgpd.load_prepared_production(output)
            self.assertEqual(production.manifest['jobs'][0]['seed'], 101)
            self.assertEqual(production.manifest['state'], 'prepared')


if __name__ == '__main__':
    unittest.main()
