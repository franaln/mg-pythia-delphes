"""Real YAML reviews, semantic validation and atomic saves without engines."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import hashlib
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import mgpd

try:
    import ruamel.yaml
except ImportError:
    HAS_ROUNDTRIP = False
else:
    HAS_ROUNDTRIP = True


@unittest.skipUnless(HAS_ROUNDTRIP, 'Install the configuration extra')
class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / 'run.dat').write_text('')
        self.source = self.root / 'config.yml'
        self.text = ('# physics configuration\nrun:\n  name: "sample" # keep quotes\n'
                     '  mode: condor\n  nevents: 10 # per job\n  outputs: [lhe]\n'
                     'cards:\n  run: run.dat\noptions:\n  seed: 101\nprocess: |\n  generate p p > e+ e-\n')
        self.source.write_text(self.text)

    def plan(self, **options):
        return mgpd.plan_config_change(self.source, **options)

    def apply(self, proposal):
        return mgpd.apply_config_change(proposal, proposal['change_sha256'])

    def test_review_has_no_side_effect_and_edit_preserves_comments_styles(self):
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}
        proposal = self.plan(changes={'run.nevents': 20, 'run.njobs': 3})
        self.assertTrue(proposal['validation']['valid'])
        self.assertIn('-  nevents: 10 # per job', proposal['diff'])
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.root.iterdir()})
        result = self.apply(proposal)
        saved = self.source.read_text()
        self.assertIn('# physics configuration', saved)
        self.assertIn('name: "sample" # keep quotes', saved)
        self.assertIn('nevents: 20 # per job', saved)
        self.assertIn('process: |\n  generate p p > e+ e-', saved)
        self.assertEqual(result['sha256'], hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertTrue(mgpd.inspect_config(self.source)['validation']['valid'])
        with self.assertRaisesRegex(ValueError, 'Source changed'):
            self.apply(proposal)

    def test_create_copy_preserves_source_and_base_not_destination(self):
        folder = self.root / 'new'; folder.mkdir()
        output = folder / 'copy.yml'
        proposal = self.plan(output=output, changes={'run.njobs': 2})
        self.assertEqual(proposal['base_directory'], str(self.root))
        self.apply(proposal)
        self.assertEqual(self.source.read_text(), self.text)
        self.assertTrue(mgpd.inspect_config(output, base_directory=self.root)['validation']['valid'])
        self.assertFalse(mgpd.inspect_config(output)['validation']['valid'])
        with self.assertRaises(FileExistsError):
            self.apply(proposal)
        with self.assertRaises(FileExistsError):
            self.plan(output=output)

    def test_copy_without_edits_is_byte_exact(self):
        proposal = self.plan(output=self.root / 'copy.yml')
        self.apply(proposal)
        self.assertEqual((self.root / 'copy.yml').read_bytes(), self.source.read_bytes())
        with self.assertRaisesRegex(ValueError, 'No configuration'):
            self.plan()

    def test_removal_lists_process_and_pointer_keys(self):
        proposal = self.plan(changes={'run.outputs': ['lhe', 'log'], 'process': 'generate p p > mu+ mu-',
                                      '/options/extra': ['set ptj 30'], '/cards/param/mass.1000': 'run.dat'},
                             remove=['options.seed'])
        self.assertTrue(proposal['validation']['valid'])
        self.apply(proposal)
        data = mgpd.inspect_config(self.source)['config']
        self.assertNotIn('seed', data['options'])
        self.assertEqual(data['cards']['param'], {'mass.1000': 'run.dat'})
        self.assertEqual(data['run']['outputs'], ['lhe', 'log'])
        self.assertEqual(data['process'], 'generate p p > mu+ mu-')

    def test_invalid_proposal_visible_but_cannot_be_saved(self):
        for changes in ({'run.nevents': 0}, {'cards.run': 'missing.dat'}):
            proposal = self.plan(changes=changes)
            self.assertFalse(proposal['validation']['valid'])
            with self.assertRaisesRegex(ValueError, 'invalid'):
                self.apply(proposal)
        self.assertEqual(self.source.read_text(), self.text)
        proposal = self.plan(changes={'run.nevents': 20})
        (self.root / 'run.dat').unlink()
        with self.assertRaisesRegex(ValueError, 'invalid'):
            self.apply(proposal)

    def test_stale_source_and_modified_proposals_are_rejected(self):
        proposal = self.plan(changes={'run.nevents': 20})
        tampered = json.loads(json.dumps(proposal)); tampered['content'] += '# changed\n'
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.apply(tampered)
        with self.assertRaisesRegex(ValueError, 'checksum'):
            mgpd.apply_config_change(proposal, 'wrong')
        self.source.write_text(self.text + '# external edit\n')
        with self.assertRaisesRegex(ValueError, 'Source changed'):
            self.apply(proposal)
        self.assertTrue(self.source.read_text().endswith('# external edit\n'))

    def test_paths_syntax_and_aliases_are_rejected(self):
        for options in ({'changes': {'run': {}, 'run.njobs': 2}},
                        {'changes': {'run.njobs': 2}, 'remove': ['run.njobs']},
                        {'remove': ['options.missing']}, {'changes': {'run.outputs.0': 'lhe'}},
                        {'changes': {'run..njobs': 2}}, {'changes': {'/options/~2bad': 1}},
                        {'changes': {'not_a_section.x': 1}}, {'output': self.root / 'bad.py'}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.plan(**options)
        self.source.write_text(self.text + 'other: &a {x: 1}\nagain: *a\n')
        with self.assertRaisesRegex(ValueError, 'aliases'):
            self.plan(changes={'run.njobs': 2})
        self.source.write_text('run: {name: first, name: second}\n')
        self.assertFalse(mgpd.inspect_config(self.source)['validation']['valid'])
        with self.assertRaisesRegex(ValueError, 'Invalid YAML'):
            self.plan(changes={'run.njobs': 2})

    def test_symlinks_and_prepared_directories_are_protected(self):
        linked = self.root / 'link.yml'; linked.symlink_to(self.source)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            mgpd.plan_config_change(linked, changes={'run.njobs': 2})
        frozen = self.root / 'production'; frozen.mkdir()
        (frozen / 'manifest.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'prepared'):
            self.plan(output=frozen / 'new.yml')
        proposal = self.plan(changes={'run.njobs': 2})
        (self.root / 'manifest.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'prepared'):
            self.apply(proposal)
        self.assertEqual(self.source.read_text(), self.text)

    def test_ambiguous_yaml_strings_and_crlf_are_preserved(self):
        self.source.write_bytes(self.text.replace('\n', '\r\n').encode())
        proposal = self.plan(changes={'run.name': 'on', 'options.extra': ['yes', '123', '2026-10-30']})
        self.apply(proposal)
        data = mgpd.inspect_config(self.source)['config']
        self.assertEqual(data['run']['name'], 'on')
        self.assertEqual(data['options']['extra'], ['yes', '123', '2026-10-30'])
        self.assertIn(b'\r\n', self.source.read_bytes())

    def test_concurrent_writers_and_destination_race_do_not_overwrite(self):
        proposal = self.plan(changes={'run.nevents': 20})
        def save():
            try:
                self.apply(proposal)
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(sum(pool.map(lambda _: save(), range(2))), 1)
        output = self.root / 'new.yml'
        proposal = self.plan(output=output)
        output.write_text('external content')
        with self.assertRaises(FileExistsError):
            self.apply(proposal)
        self.assertEqual(output.read_text(), 'external content')

    def test_failed_publication_keeps_original_and_cleans_temporary(self):
        proposal = self.plan(changes={'run.nevents': 20})
        with patch('mgpd.configuration.os.replace', side_effect=OSError('simulated failure')):
            with self.assertRaises(OSError):
                self.apply(proposal)
        self.assertEqual(self.source.read_text(), self.text)
        self.assertEqual([p.name for p in self.root.glob('.config.yml.*')], ['.config.yml.mgpd.lock'])

    def test_failed_directory_sync_reports_published_unknown(self):
        import stat
        from mgpd.configuration import ConfigWriteUnknown
        proposal = self.plan(changes={'run.nevents': 20})
        real_sync = os.fsync
        def sync(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError('directory sync failed')
            real_sync(descriptor)
        with patch('mgpd.configuration.os.fsync', side_effect=sync):
            with self.assertRaises(ConfigWriteUnknown):
                self.apply(proposal)
        self.assertEqual(self.source.read_text(), proposal['content'])

    def test_save_as_does_not_write_in_source_directory(self):
        folder = self.root / 'output'; folder.mkdir()
        before = {p.name for p in self.root.iterdir()}
        self.apply(self.plan(output=folder / 'copy.yml'))
        self.assertEqual({p.name for p in self.root.iterdir()}, before)
        self.assertFalse((self.root / '.config.yml.mgpd.lock').exists())

    def test_parallel_validation_uses_explicit_base_without_chdir(self):
        second = self.root / 'second'; second.mkdir()
        (second / 'run.dat').write_text('')
        copied = second / 'config.yml'; copied.write_text(self.text)
        cwd = os.getcwd()
        with ThreadPoolExecutor(2) as pool:
            reports = list(pool.map(lambda path: mgpd.inspect_config(path), [self.source, copied]))
        self.assertTrue(all(report['validation']['valid'] for report in reports))
        self.assertEqual(os.getcwd(), cwd)

    def test_inspection_reports_invalid_yaml_and_bounds_input_size(self):
        self.source.write_text('run: [\n')
        inspected = mgpd.inspect_config(self.source)
        self.assertFalse(inspected['validation']['valid'])
        self.assertIsNone(inspected['config'])
        self.source.write_text('x' * (128 * 1024 + 1))
        with self.assertRaisesRegex(ValueError, '128 KiB'):
            mgpd.inspect_config(self.source)
