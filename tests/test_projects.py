"""Packaged starters and create/run CLI behavior without physics engines."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import mgpd
from mgpd.cli import main
from mgpd.projects import create_project, list_config_templates, load_project_config, prepare_project


class ProjectTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_all_templates_include_editable_self_contained_cards(self):
        templates = list_config_templates()
        self.assertEqual({item['name'] for item in templates}, {'parton', 'pythia', 'delphes'})
        for item in templates:
            with self.subTest(template=item['name']):
                project = create_project(self.root / item['name'], item['name'], mode='condor')
                self.assertEqual(set(project['files']), set(item['files']))
                config = load_project_config(project['directory'])
                self.assertIn('param', config['cards'])
                for card in config['cards'].values():
                    self.assertTrue(Path(card).is_relative_to(Path(project['directory'])))
                    self.assertTrue(Path(card).is_file())
                self.assertEqual(config['run']['nevents'], 100)
                self.assertEqual(config['options']['seed'], 'RANDOM')
        self.assertNotIn('pythia', load_project_config(self.root / 'parton')['cards'])
        self.assertNotIn('delphes', load_project_config(self.root / 'pythia')['cards'])
        self.assertIn('delphes', load_project_config(self.root / 'delphes')['cards'])

    def test_backend_defaults_and_overrides(self):
        for mode, image in [('local-docker', 'franaln/mg-pythia-delphes:3.5.6'),
                            ('local-apptainer', '/opt/images/mg-pythia-delphes-3.5.6.sif'),
                            ('condor', 'mg-pythia-delphes-3.5.6')]:
            project = create_project(self.root / mode, mode=mode, name='physics', nevents=50, njobs=2, seed=101)
            self.assertEqual(project['config']['run']['image'], image)
            self.assertEqual(project['config']['run']['name'], 'physics')
        with patch('mgpd.config.socket.gethostname', return_value='jupiter'):
            project = create_project(self.root / 'local')
            self.assertEqual(project['config']['run']['image'], '/opt/images/mg-pythia-delphes-3.5.6.sif')
        project = create_project(self.root / 'custom', image='custom.sif')
        self.assertEqual(project['config']['run']['image'], 'custom.sif')

    def test_create_never_overwrites_and_invalid_requests_leave_no_project(self):
        folder = self.root / 'existing'; folder.mkdir()
        marker = folder / 'keep'; marker.write_text('keep')
        with self.assertRaises(FileExistsError):
            create_project(folder)
        self.assertEqual(marker.read_text(), 'keep')
        link = self.root / 'alias'; link.symlink_to(folder)
        with self.assertRaises(FileExistsError):
            create_project(link)
        for kwargs in ({'template': 'absent'}, {'nevents': 0}, {'njobs': 30081}, {'seed': -1}, {'mode': 'bad'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                create_project(self.root / 'invalid', **kwargs)
            self.assertFalse((self.root / 'invalid').exists())
        with self.assertRaises(FileNotFoundError):
            create_project(self.root / 'missing/parent')
        (folder / 'manifest.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'prepared'):
            create_project(folder / 'nested')

    def test_simultaneous_creation_and_independent_copies(self):
        def create(_):
            try:
                create_project(self.root / 'one')
                return True
            except FileExistsError:
                return False
        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(sum(pool.map(create, range(2))), 1)
        create_project(self.root / 'two')
        first = self.root / 'one/cards/param_card.dat'
        original = (self.root / 'two/cards/param_card.dat').read_bytes()
        first.write_text('edited')
        self.assertEqual((self.root / 'two/cards/param_card.dat').read_bytes(), original)
        create_project(self.root / 'three')
        self.assertEqual((self.root / 'three/cards/param_card.dat').read_bytes(), original)

    def test_prepare_templates_from_another_cwd_and_saved_execution_guards(self):
        for template in ('parton', 'pythia', 'delphes'):
            project = self.root / template
            create_project(project, template, mode='condor', njobs=2, seed=101)
            before = (project / 'config.yml').read_bytes()
            with redirect_stdout(io.StringIO()):
                prepared = prepare_project(project)
            self.assertEqual(prepared.manifest['mode'], 'condor')
            self.assertEqual([row['seed'] for row in prepared.manifest['jobs']], [101, 102])
            self.assertNotIn('execution_attempt', prepared.manifest)
            self.assertEqual((project / 'config.yml').read_bytes(), before)
            mgpd.load_prepared_production(project / 'production')

    def test_create_and_run_cli_subprocess_without_source_cwd(self):
        project = self.root / 'cli'
        commands = [(['create', str(project), '--template', 'delphes', '--mode', 'condor', '--seed', '101'], None),
                    (['validate-config', str(project)], None),
                    (['prepare', str(project)], project / 'production'),
                    (['prepare', str(project / 'config.yml'), '-o', str(self.root / 'explicit')], self.root / 'explicit')]
        for arguments, output in commands:
            result = subprocess.run([sys.executable, '-m', 'mgpd', *arguments], cwd=self.root,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            if output:
                production = mgpd.load_prepared_production(output)
                self.assertEqual(production.manifest['mode'], 'condor')
                self.assertNotIn('execution_attempt', production.manifest)
        for arguments in (['--help'], ['create', '--help'], ['run', '--help'], ['merge', '--help'], ['create', '--list-templates']):
            result = subprocess.run([sys.executable, '-m', 'mgpd', *arguments], cwd=self.root,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_run_executes_through_existing_core_and_rejects_mixed_sources(self):
        project = self.root / 'run'
        create_project(project, mode='local-apptainer', image='test.sif')
        with patch('mgpd.cli.execute_production') as execute, redirect_stdout(io.StringIO()):
            self.assertEqual(main(['run', str(project)]), 0)
            execute.assert_called_once()
            self.assertEqual(execute.call_args.args[0].manifest['mode'], 'local-apptainer')
        with self.assertRaises(SystemExit):
            main(['run', str(project), '--execute-prepared', str(project / 'production')])
        with redirect_stdout(io.StringIO()), patch('mgpd.cli.production_status', return_value={
                'state': 'prepared', 'recorded_state': 'prepared', 'jobs': [], 'query_errors': []}):
            self.assertEqual(main(['status', str(project / 'production')]), 0)

    def test_ambiguous_default_config_names_are_rejected(self):
        project = self.root / 'ambiguous'
        create_project(project)
        (project / 'config.yaml').write_bytes((project / 'config.yml').read_bytes())
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            load_project_config(project)
