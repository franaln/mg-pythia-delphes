"""Custom MadGraph blocks survive creation/copying and reach prepared run files."""
from contextlib import redirect_stdout
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import yaml
from mgpd import create_project, copy_project, load_project_config, prepare_project


class ProcessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.process = 'import model sm\ndefine leptons = e+ e- mu+ mu-\ngenerate p p > ta+ ta-\nadd process p p > mu+ mu-\n'

    def test_custom_block_reaches_prepared_inputs_unchanged(self):
        project = self.root / 'custom'
        create_project(project, template='proc', process=self.process, mode='condor')
        self.assertEqual(load_project_config(project)['process'], self.process)
        self.assertIn('process: |', (project / 'config.yml').read_text())
        with redirect_stdout(io.StringIO()):
            prepared = prepare_project(project)
        run_file = Path(prepared.output_dir) / prepared.manifest['jobs'][0]['run_file']
        self.assertIn(self.process, run_file.read_text())
        self.assertNotIn('generate p p > e+ e-', run_file.read_text())

    def test_copy_preserves_process_or_explicitly_replaces_process_card(self):
        source = self.root / 'source'
        create_project(source, template='proc', process=self.process)
        copy_project(source, self.root / 'same')
        self.assertEqual(load_project_config(self.root / 'same')['process'], self.process)
        (source / 'cards/proc.dat').write_text('generate p p > e+ e-')
        config_path = source / 'config.yml'
        config = yaml.safe_load(config_path.read_text())
        config['cards']['proc'] = 'cards/proc.dat'
        config_path.write_text(yaml.safe_dump(config))
        before = config_path.read_bytes()
        copy_project(source, self.root / 'changed', process='generate p p > ta+ ta-')
        changed = load_project_config(self.root / 'changed')
        self.assertEqual(changed['process'], 'generate p p > ta+ ta-')
        self.assertNotIn('proc', changed['cards'])
        self.assertEqual(config_path.read_bytes(), before)

    def test_cli_create_and_copy_pass_multiline_process(self):
        for name, extra in [('cli', []), ('copy', ['--copy', str(self.root / 'cli')])]:
            result = subprocess.run([sys.executable, '-m', 'mgpd', 'create', str(self.root / name),
                                     *extra, '--process', self.process], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(load_project_config(self.root / name)['process'], self.process)

    def test_empty_nonstring_and_ignored_input_mode_overrides_are_rejected(self):
        source = self.root / 'source'
        create_project(source, template='proc')
        for process in ('', ' \n ', 123):
            for action in (lambda: create_project(self.root / 'bad', template='proc', process=process),
                           lambda: copy_project(source, self.root / 'bad', process=process)):
                with self.subTest(process=process), self.assertRaises(ValueError):
                    action()
                self.assertFalse((self.root / 'bad').exists())
        inputs = source / 'inputs'; inputs.mkdir()
        (inputs / 'run.mg5').write_text('generate p p > e+ e-\n')
        for key, value in [('input_dir', 'inputs'), ('input_dirs', {'one': 'inputs'}),
                           ('input_files', ['inputs/run.mg5'])]:
            (source / 'config.yml').write_text(yaml.safe_dump({
                'run': {'name': 'sample', 'mode': 'condor', 'outputs': ['lhe']}, key: value}))
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'Process overrides'):
                copy_project(source, self.root / 'bad', process=self.process)
            self.assertFalse((self.root / 'bad').exists())
