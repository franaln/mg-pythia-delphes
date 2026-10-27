"""Regression checks that do not require physics software or a scheduler."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/run_mg_pythia_delphes.py'
spec = importlib.util.spec_from_file_location('runner', SCRIPT)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class RunnerTests(unittest.TestCase):
    def config(self, directory):
        card = directory / 'run card.dat'
        card.write_text('')
        return {'run': {'name': 'sample', 'outputs': ['lhe']},
                'process': 'generate p p > t t~', 'cards': {'run': str(card)}}

    def test_validation_rejects_missing_cards_and_bad_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            runner.validate_config(config)
            config['run']['njobs'] = 0
            with self.assertRaises(ValueError):
                runner.validate_config(config)
            config['run']['njobs'] = 1
            config['cards']['run'] = str(Path(tmp) / 'absent')
            with self.assertRaises(ValueError):
                runner.validate_config(config)

    def test_validate_only_does_not_touch_output_even_with_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.config(root)
            output = root / 'existing'
            output.mkdir()
            marker = output / 'keep'
            marker.write_text('keep')
            yaml = root / 'config.yml'
            yaml.write_text(runner.yaml.safe_dump(config))
            with patch('sys.argv', [str(SCRIPT), '-c', str(yaml), '-o', str(output),
                                    '--validate-only', '--force']):
                runner.main()
            self.assertEqual(marker.read_text(), 'keep')

    def test_failed_madgraph_is_reported_by_both_wrappers(self):
        for template in (runner.template_run_local_script, runner.template_run_condor_script):
            with self.subTest(condor=template == runner.template_run_condor_script):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    executable = root / 'mg5_aMC'
                    executable.write_text('#!/bin/bash\nexit 42\n')
                    executable.chmod(0o755)
                    (root / 'run.mg5').write_text('')
                    script = root / 'run.sh'
                    script.write_text(template)
                    subprocess.run(['bash', '-n', str(script)], check=True)
                    env = dict(os.environ, MG_DIR='/unused', PATH=str(root) + ':' + os.environ['PATH'])
                    result = subprocess.run(['bash', str(script), 'sample', 'absent.tar.gz', 'lhe', 'output'],
                                            cwd=root, env=env, capture_output=True, text=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('ERROR running MadGraph', result.stderr)

    def test_integer_core_count(self):
        self.assertEqual(runner.get_expert_options({'expert': {'ncores': 2}}), ['set nb_core 2'])


if __name__ == '__main__':
    unittest.main()
