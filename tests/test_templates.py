"""Packaged file templates preserve shell and scheduler substitutions."""
from pathlib import Path
import subprocess
import tempfile
import unittest

import mgpd
from mgpd.templates import load_template, create_file_from_template, create_file_from_script_template


class TemplateTests(unittest.TestCase):
    def test_declared_public_exports_exist(self):
        for module in (mgpd,):
            for name in module.__all__:
                with self.subTest(module=module.__name__, name=name):
                    self.assertTrue(hasattr(module, name), name)

    def test_madgraph_template_substitutes_all_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'run.mg5'
            values = {'process': 'generate p p > e+ e-', 'expert_options': 'set nb_core 1',
                      'run_madspin': '', 'run_pythia': 'shower=OFF', 'run_delphes': '',
                      'cards': 'cards/run_card.dat', 'options': 'set nevents 100'}
            create_file_from_template('run.mg5', path, values)
            text = path.read_text()
            self.assertIn(values['process'], text)
            self.assertIn('set nevents 100', text)
            self.assertNotIn('${', text)
            self.assertEqual(text.strip().splitlines()[-1], 'done')

    def test_condor_template_preserves_scheduler_macros(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'job.sub'
            values = {key: 'example' for key in ('container_image', 'input_file', 'arguments',
                                               'notifications', 'requirements', 'jobs')}
            create_file_from_template('job.sub', path, values)
            text = path.read_text()
            self.assertIn('job_name = $(Cluster)_$(Process)', text)
            self.assertIn('transfer_input_files = $(input_file),$(seed_file)', text)
            self.assertNotIn('${container_image}', text)

    def test_shell_templates_preserve_expansions_and_parse(self):
        with tempfile.TemporaryDirectory() as temporary:
            for name in ('run_mgpd_local.sh', 'run_mgpd_condor.sh'):
                with self.subTest(name=name):
                    path = Path(temporary) / name
                    replacements = {'# MGPD_LOCAL_OUTPUTS': "outputs='lhe root lhco'"} if 'local' in name else {}
                    create_file_from_script_template(name, path, replacements)
                    expected = load_template(name)
                    for old, new in replacements.items():
                        expected = expected.replace(old, new)
                    self.assertEqual(path.read_text(), expected)
                    self.assertIn('${run_name}', expected)
                    result = subprocess.run(['bash', '-n', str(path)], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
