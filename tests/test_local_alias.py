"""Site-aware alias resolution and saved-plan portability, without containers."""
from contextlib import redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

import mgpd
from mgpd.config import resolve_run_mode

ROOT = Path(__file__).resolve().parents[1]


class LocalAliasTests(unittest.TestCase):
    def test_site_hostname_and_configured_alias_matrix(self):
        cases = [('jupiter', {}, 'local-apptainer'),
                 ('JUPITER.iflp.unlp.edu.ar', {}, 'local-apptainer'),
                 ('jupiter-copy', {}, 'local-docker'), ('laptop', {}, 'local-docker'),
                 ('worker1', {'MGPD_SITE': 'jupiter'}, 'local-apptainer'),
                 ('jupiter', {'MGPD_SITE': 'default'}, 'local-docker'),
                 ('leda.site', {'MGPD_JUPITER_HOSTS': 'leda.site, node1'}, 'local-apptainer')]
        for host, settings, expected in cases:
            with self.subTest(host=host, settings=settings), patch.dict(os.environ, settings, clear=True), \
                    patch('mgpd.config.socket.gethostname', return_value=host):
                self.assertEqual(resolve_run_mode('local'), expected)

    def test_concrete_modes_and_condor_alias_ignore_site_settings(self):
        with patch.dict(os.environ, {'MGPD_SITE': 'invalid'}, clear=True):
            for mode in ('local-docker', 'local-apptainer', 'condor', 'jupiter'):
                self.assertEqual(resolve_run_mode(mode), mode)
            with self.assertRaisesRegex(ValueError, 'MGPD_SITE'):
                resolve_run_mode('local')

    def test_programmatic_preparation_freezes_backend_and_saved_execution_does_not_resolve_again(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            card = root / 'run.dat'; card.write_text('')
            config = {'run': {'name': 'alias', 'mode': 'local', 'image': '/images/test.sif',
                              'nevents': 2, 'outputs': ['lhe']},
                      'cards': {'run': str(card)}, 'process': 'generate p p > e+ e-'}
            before = copy.deepcopy(config)
            with patch.dict(os.environ, {'MGPD_SITE': 'jupiter'}), redirect_stdout(io.StringIO()):
                production = mgpd.prepare_production(config, root / 'production')
            self.assertEqual(config, before)
            self.assertEqual(production.manifest['mode'], 'local-apptainer')
            self.assertEqual(production.manifest['config']['run']['mode'], 'local')
            source = root / 'local.yml'
            source.write_text(yaml.safe_dump(config))
            result = subprocess.run([sys.executable, '-m', 'mgpd', '-c', str(source),
                                     '-o', str(root / 'yaml-production'), '--dry-run'],
                                    env=dict(os.environ, MGPD_SITE='jupiter'), capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads((root / 'yaml-production/manifest.json').read_text())['mode'],
                             'local-apptainer')
            with patch.dict(os.environ, {'MGPD_SITE': 'default'}), \
                    patch('mgpd.config.resolve_run_mode', side_effect=AssertionError('Must not re-resolve')), \
                    patch('mgpd.execution.subprocess.run') as run, redirect_stdout(io.StringIO()):
                loaded = mgpd.load_prepared_production(production.output_dir)
                mgpd.execute_production(loaded)
            self.assertEqual(run.call_args.args[0][0], 'apptainer')

    def test_cli_and_example_driver_resolve_alias_before_backend_handling(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for site, expected in [('jupiter', 'local-apptainer'), ('default', 'local-docker')]:
                env = dict(os.environ, MGPD_SITE=site)
                output = root / site
                result = subprocess.run([sys.executable, str(ROOT / 'scripts/test_examples.py'),
                                         '--mode', 'local', '--example', 'example7_parton_only',
                                         '--output', str(output)], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                report = json.loads((output / 'report.json').read_text())
                self.assertEqual(report['requested_mode'], 'local')
                self.assertEqual(report['mode'], expected)
                manifest = json.loads((output / 'example7_parton_only/production/manifest.json').read_text())
                self.assertEqual(manifest['mode'], expected)
                command = [sys.executable, '-m', 'mgpd', '-c', str(output / 'example7_parton_only/test_config.yml'),
                           '-o', str(root / f'cli-{site}'), '--run_mode', 'local', '--dry-run']
                # Generated YAML retains example-relative card paths.
                result = subprocess.run(command, cwd=ROOT / 'examples/example7_parton_only',
                                        env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(json.loads((root / f'cli-{site}/manifest.json').read_text())['mode'], expected)
