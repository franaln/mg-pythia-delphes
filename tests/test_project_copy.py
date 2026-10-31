"""Project copies preserve inputs/settings and apply only explicit overrides."""
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
import yaml
from mgpd import create_project, copy_project, load_project_config


class CopyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / 'source'
        create_project(self.source, template='proc', level='detector', mode='condor',
                       name='original', njobs=5, nevents=23, seed=321, image='custom')

    def test_copy_preserves_configuration_and_independent_files(self):
        (self.source / 'notes.txt').write_text('notes')
        production = self.source / 'production'; production.mkdir()
        (production / 'manifest.json').write_text('{}')
        before = {str(p): p.read_bytes() for p in self.source.rglob('*') if p.is_file()}
        result = copy_project(self.source, self.root / 'copy')
        copied = Path(result['directory'])
        original = yaml.safe_load((self.source / 'config.yml').read_text())
        self.assertEqual(yaml.safe_load((copied / 'config.yml').read_text()), original)
        self.assertEqual((copied / 'notes.txt').read_text(), 'notes')
        self.assertFalse((copied / 'production').exists())
        (copied / 'cards/run_card.dat').write_text('edited')
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.source.rglob('*') if p.is_file()})

    def test_explicit_overrides_including_default_values(self):
        result = copy_project(self.source, self.root / 'copy', name='renamed', mode='local-docker',
                              image='new-image', njobs=1, nevents=100, seed=0, level='parton')
        config = load_project_config(result['directory'])
        for key, value in dict(name='renamed', mode='local-docker', image='new-image', njobs=1, nevents=100).items():
            self.assertEqual(config['run'][key], value)
        self.assertEqual(config['options']['seed'], 0)
        self.assertEqual(set(config['cards']), {'run', 'param'})
        self.assertEqual(config['run']['outputs'], ['lhe', 'log'])
        for level in ('particle', 'detector'):
            result = copy_project(self.root / 'copy', self.root / level, level=level)
            config = load_project_config(result['directory'])
            self.assertIn('pythia', config['cards'])
            self.assertEqual('delphes' in config['cards'], level == 'detector')

    def test_external_cards_and_scan_paths_are_copied(self):
        card = self.root / 'external.dat'; card.write_text('external')
        path = self.source / 'config.yml'
        config = yaml.safe_load(path.read_text())
        config['cards']['param'] = {'one': str(card), 'two': str(card)}
        config['cards']['run'] = str(self.source / 'cards/run_card.dat')
        path.write_text(yaml.safe_dump(config))
        result = copy_project(self.source, self.root / 'copy')
        loaded = load_project_config(result['directory'])
        self.assertEqual(Path(loaded['cards']['param']['one']).read_text(), 'external')
        self.assertEqual(loaded['cards']['param']['one'], loaded['cards']['param']['two'])
        for value in (loaded['cards']['run'], loaded['cards']['param']['one']):
            self.assertTrue(Path(value).is_relative_to(Path(result['directory'])))
        card.unlink()
        load_project_config(result['directory'])

    def test_input_directory_configuration_is_rebased(self):
        inputs = self.root / 'inputs'; inputs.mkdir()
        (inputs / 'run.mg5').write_text('generate p p > e+ e-\n')
        (inputs / 'card.dat').write_text('custom')
        (self.source / 'config.yml').write_text(yaml.safe_dump({
            'run': {'name': 'dir', 'mode': 'condor', 'outputs': ['lhe']},
            'input_dirs': {'sample': str(inputs)}}))
        result = copy_project(self.source, self.root / 'copy')
        config = load_project_config(result['directory'])
        self.assertEqual((Path(config['input_dirs']['sample']) / 'card.dat').read_text(), 'custom')
        with self.assertRaises(ValueError):
            copy_project(self.source, self.root / 'invalid', level='parton')

    def test_cli_copy_preserves_omissions_and_applies_explicit_defaults(self):
        result = subprocess.run([sys.executable, '-m', 'mgpd', 'create', str(self.root / 'cli'),
                                 '--copy', str(self.source), '--njobs', '1', '--nevents', '100'],
                                cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = load_project_config(self.root / 'cli')
        self.assertEqual(config['run']['name'], 'original')
        self.assertEqual(config['run']['mode'], 'condor')
        self.assertEqual(config['run']['image'], 'custom')
        self.assertEqual(config['run']['njobs'], 1)
        self.assertEqual(config['run']['nevents'], 100)
        self.assertEqual(config['options']['seed'], 321)

    def test_invalid_requests_never_publish_or_overwrite(self):
        for options in ({'njobs': 0}, {'nevents': 0}, {'seed': -1}, {'level': 'bad'}, {'image': ''}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                copy_project(self.source, self.root / 'bad', **options)
            self.assertFalse((self.root / 'bad').exists())
        with self.assertRaises(FileExistsError):
            copy_project(self.source, self.source)
        with self.assertRaises(ValueError):
            copy_project(self.source, self.source / 'nested')
        (self.source / 'link').symlink_to(self.root / 'source/cards/run_card.dat')
        with self.assertRaises(ValueError):
            copy_project(self.source, self.root / 'linked')
        self.assertFalse((self.root / 'linked').exists())
        (self.source / 'link').unlink()
        (self.source / 'manifest.json').write_text('{}')
        with self.assertRaises(ValueError):
            copy_project(self.source, self.root / 'frozen')
