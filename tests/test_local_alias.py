"""Hostname-only local selection and saved-plan portability, without containers."""
from contextlib import redirect_stdout
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import mgpd
from mgpd.cli import main as cli_main
from mgpd.config import resolve_run_mode
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('local_alias_example_driver', ROOT / 'scripts/test_examples.py')
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)


class LocalAliasTests(unittest.TestCase):
    def test_hostname_matrix_and_explicit_modes(self):
        for host, expected in [('jupiter', 'local-apptainer'),
                               ('JUPITER.iflp.unlp.edu.ar.', 'local-apptainer'),
                               ('jupiter-copy', 'local-docker'), ('node1', 'local-docker'),
                               ('laptop', 'local-docker')]:
            with self.subTest(host=host), patch('mgpd.config.socket.gethostname', return_value=host):
                self.assertEqual(resolve_run_mode('local'), expected)
                for mode in ('local-docker', 'local-apptainer', 'condor', 'jupiter'):
                    self.assertEqual(resolve_run_mode(mode), mode)

    def test_omitted_and_explicit_local_modes_freeze_backend_without_mutating_config(self):
        for host, expected in [('jupiter', 'local-apptainer'), ('laptop', 'local-docker')]:
            for mode in (None, 'local'):
                with self.subTest(host=host, mode=mode), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    card = root / 'run.dat'; card.write_text('')
                    config = {'run': {'name': 'alias', 'image': '/images/test.sif',
                                      'nevents': 2, 'outputs': ['lhe']},
                              'cards': {'run': str(card)}, 'process': 'generate p p > e+ e-'}
                    if mode:
                        config['run']['mode'] = mode
                    before = copy.deepcopy(config)
                    with patch('mgpd.config.socket.gethostname', return_value=host), redirect_stdout(io.StringIO()):
                        production = mgpd.prepare_production(config, root / 'production')
                    self.assertEqual(config, before)
                    self.assertEqual(production.manifest['mode'], expected)
                    source = root / 'local.yml'
                    source.write_text(yaml.safe_dump(config))
                    with patch('mgpd.config.socket.gethostname', return_value=host), redirect_stdout(io.StringIO()):
                        cli_main(['-c', str(source), '-o', str(root / 'yaml-production'), '--dry-run'])
                    self.assertEqual(json.loads((root / 'yaml-production/manifest.json').read_text())['mode'], expected)
                    with patch('mgpd.config.resolve_run_mode', side_effect=AssertionError('Must not re-resolve')), \
                            patch('mgpd.execution.subprocess.run') as run, redirect_stdout(io.StringIO()):
                        mgpd.execute_production(mgpd.load_prepared_production(production.output_dir))
                    self.assertEqual(run.call_args.args[0][0], 'apptainer' if host == 'jupiter' else 'docker')

    def test_example_driver_default_and_alias_resolve_before_backend_handling(self):
        for host, expected in [('jupiter', 'local-apptainer'), ('laptop', 'local-docker')]:
            for flag in ([], ['--mode', 'local']):
                with self.subTest(host=host, flag=flag), tempfile.TemporaryDirectory() as temporary:
                    output = Path(temporary) / 'results'
                    argv = ['test_examples.py', *flag, '--example', 'example7_parton_only', '--output', str(output)]
                    with patch('sys.argv', argv), patch('mgpd.config.socket.gethostname', return_value=host), \
                            redirect_stdout(io.StringIO()):
                        self.assertEqual(driver.main(), 0)
                    report = json.loads((output / 'report.json').read_text())
                    self.assertEqual(report['requested_mode'], 'local')
                    self.assertEqual(report['mode'], expected)
                    manifest = json.loads((output / 'example7_parton_only/production/manifest.json').read_text())
                    self.assertEqual(manifest['mode'], expected)

    def test_cli_mode_overrides_yaml_and_old_option_is_not_accepted(self):
        from mgpd.cli import parse_args
        _, args = parse_args(['-c', 'config.yml', '--mode', 'local', '--validate-only'])
        self.assertEqual(args.run_mode, 'local')
        with redirect_stdout(io.StringIO()), patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit):
            parse_args(['-c', 'config.yml', '--run_mode', 'local', '--validate-only'])
