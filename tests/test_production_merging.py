"""Production/recovery assembly using real plans/files and fake container tools."""
from contextlib import redirect_stdout
import gzip
import io
import json
from pathlib import Path
import shlex
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import mgpd
from mgpd.storage import write_manifest
from mgpd.production_merging import merge_production
from mgpd.merging import main as merge_main


def lhe(seed, events=2):
    return gzip.compress((f'<LesHouchesEvents>\n<header>\n{seed} = iseed\n</header>\n'
                          '<init>\n2212 2212 6500 6500 0 0 0 0 3 1\n10 0.1 1 1\n</init>\n'
                          + '<event>\n</event>\n' * events + '</LesHouchesEvents>\n').encode())


class ProductionMergingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def fixture(self, mode='local-apptainer', multiple=False, outputs=None):
        card = self.root / 'card.dat'; card.write_text('')
        config = {'run': {'name': 'sample', 'mode': mode, 'image': 'mg-pythia-delphes-3.5.6',
                          'njobs': 2, 'nevents': 2, 'outputs': outputs or ['lhe']},
                  'process': 'generate p p > e+ e-', 'cards': {'run': str(card)}, 'options': {'seed': 101}}
        if multiple:
            config['cards']['param'] = {'a': str(card), 'b': str(card)}
        if outputs and 'root' in outputs:
            config['cards']['delphes'] = str(card)
        with redirect_stdout(io.StringIO()):
            production = mgpd.prepare_production(config, self.root / 'parent')
        self.complete(production)
        return production

    def complete(self, production):
        manifest = production.manifest
        condor = manifest['mode'] == 'condor'
        manifest['state'] = 'submitted' if condor else 'execution_finished'
        if condor:
            manifest['submission'] = {'identity_tagged': True, 'jobs': [
                {'job_id': job['job_id'], 'scheduler_id': f'123.{i}'} for i, job in enumerate(manifest['jobs'])]}
        write_manifest(production.output_dir, manifest)
        for i, job in enumerate(manifest['jobs']):
            if condor:
                archive = Path(production.output_dir) / f'output_{job["sample"]}_123_{i}.tar.gz'
                with tarfile.open(archive, 'w:gz') as target:
                    data = lhe(job['seed'])
                    member = tarfile.TarInfo(f'output_{i}_unweighted_events.lhe.gz'); member.size = len(data)
                    target.addfile(member, io.BytesIO(data))
                (Path(production.output_dir) / f'job_{job["sample"]}_123_{i}.log').write_text('Normal termination (return value 0)\n')
            else:
                folder = Path(production.output_dir, job['run_file']).parent / 'RUN/Events/run_01'
                folder.mkdir(parents=True, exist_ok=True)
                (folder / 'unweighted_events.lhe.gz').write_bytes(lhe(job['seed']))
                if 'root' in job['outputs']:
                    self.root_file(folder / 'tag_delphes_events.root', 2)

    def root_file(self, path, events):
        import numpy as np
        import uproot
        with uproot.recreate(path) as output:
            output['Delphes'] = {'event': np.arange(events)}

    def native_output(self, production, index):
        job = production.manifest['jobs'][index]
        return Path(production.output_dir, job['run_file']).parent / 'RUN/Events/run_01/unweighted_events.lhe.gz'

    def fake_container(self, command, check):
        work = Path(command[command.index('--bind') + 1][:-len(':/work')]) if '--bind' in command else Path(command[command.index('-v') + 1][:-len(':/work')])
        args = shlex.split(command[-1].split(' && ')[-1])
        if args[0].endswith('merge.pl'):
            count = sum(gzip.decompress((work / name).read_bytes()).count(b'<event>') for name in args[1:-2])
            (work / args[-2]).write_bytes(lhe(101, count))
        elif args[0] == 'hadd':
            import uproot
            count = 0
            for name in args[2:]:
                with uproot.open(work / name) as source:
                    count += source['Delphes'].num_entries
            self.root_file(work / args[1], count)
        else:
            raise AssertionError(f'Unexpected tool: {args}')
        return subprocess.CompletedProcess(command, 0)

    def merge(self, directory, **kwargs):
        with patch('mgpd.merging.subprocess.run', side_effect=self.fake_container):
            output = merge_production(directory, backend='local-apptainer', **kwargs)
        return Path(output), json.loads((Path(output) / 'merge-report.json').read_text())

    def test_review_is_stable_and_changed_source_blocks_execution(self):
        parent = self.fixture()
        before = Path(parent.output_dir, 'manifest.json').read_bytes()
        with patch('mgpd.production_merging.merge_outputs', side_effect=AssertionError('Planning must not run engines')):
            review = mgpd.plan_production_merge(parent.output_dir, backend='local-apptainer')
            again = mgpd.plan_production_merge(parent.output_dir, backend='local-apptainer')
        self.assertEqual(review['selection_sha256'], again['selection_sha256'])
        self.assertEqual(len(review['selection']), 2)
        self.assertFalse(Path(parent.output_dir, 'merged').exists())
        self.assertEqual(Path(parent.output_dir, 'manifest.json').read_bytes(), before)
        output = self.native_output(parent, 0)
        output.write_bytes(gzip.compress(gzip.decompress(output.read_bytes()).replace(b'10 0.1 1 1', b'11 0.1 1 1')))
        with patch('mgpd.production_merging.merge_outputs', side_effect=AssertionError('Stale review must fail first')):
            with self.assertRaisesRegex(ValueError, 'selection changed'):
                mgpd.merge_production(parent.output_dir, backend='local-apptainer', selection_sha256=review['selection_sha256'])
        self.assertFalse(Path(parent.output_dir, 'merged').exists())

    def test_local_and_condor_directories_merge_once_and_preserve_manifests(self):
        for mode in ('local-apptainer', 'condor'):
            with self.subTest(mode=mode):
                # Each mode has its own fixture directory.
                if (self.root / 'parent').exists():
                    self.root = self.root / 'next'; self.root.mkdir()
                parent = self.fixture(mode)
                before = Path(parent.output_dir, 'manifest.json').read_bytes()
                output, report = self.merge(parent.output_dir)
                self.assertEqual(output, Path(parent.output_dir) / 'merged')
                self.assertFalse(report['partial'])
                self.assertEqual(report['samples']['sample']['metrics']['lhe_events'], 4)
                self.assertEqual(len(report['samples']['sample']['jobs']), 2)
                self.assertTrue(all(item['files'][0]['sha256'] for item in report['samples']['sample']['jobs']))
                self.assertEqual(Path(parent.output_dir, 'manifest.json').read_bytes(), before)
                with self.assertRaises(FileExistsError):
                    self.merge(parent.output_dir)

    def test_multiple_samples_are_separate_and_filter_can_select_one(self):
        parent = self.fixture(multiple=True)
        output, report = self.merge(parent.output_dir)
        self.assertEqual(set(report['samples']), {'sample_a', 'sample_b'})
        for sample in report['samples']:
            self.assertTrue((output / sample / 'merged_unweighted_events.lhe.gz').is_file())
            self.assertEqual(report['samples'][sample]['metrics']['lhe_events'], 4)
        filtered, report = self.merge(parent.output_dir, output=self.root / 'filtered', samples=['sample_b'])
        self.assertTrue((filtered / 'merged_unweighted_events.lhe.gz').is_file())
        self.assertEqual(set(report['samples']), {'sample_b'})
        with self.assertRaisesRegex(ValueError, 'Unknown'):
            self.merge(parent.output_dir, output=self.root / 'bad', samples=['other'])

    def test_missing_output_blocks_default_and_partial_records_omission(self):
        parent = self.fixture()
        self.native_output(parent, 1).unlink()
        with self.assertRaisesRegex(ValueError, 'sample_00001'):
            self.merge(parent.output_dir)
        self.assertFalse((Path(parent.output_dir) / 'merged').exists())
        _, report = self.merge(parent.output_dir, allow_partial=True)
        self.assertTrue(report['partial'])
        self.assertEqual([row['job_id'] for row in report['omitted_jobs']], ['sample_00001'])
        self.assertEqual(report['samples']['sample']['metrics']['lhe_events'], 2)

    def recovery(self, parent, name):
        with redirect_stdout(io.StringIO()):
            return mgpd.prepare_recovery(parent.output_dir, self.root / name)

    def test_recovery_chain_preserves_one_attempt_per_job_and_original_wins_if_restored(self):
        parent = self.fixture()
        original = self.native_output(parent, 1).read_bytes()
        self.native_output(parent, 1).unlink()
        child = self.recovery(parent, 'retry')
        self.complete(child)
        self.native_output(child, 0).unlink()
        grandchild = self.recovery(child, 'retry2')
        self.complete(grandchild)
        _, report = self.merge(parent.output_dir)
        selected = report['samples']['sample']['jobs']
        self.assertEqual([row['production_id'] for row in selected],
                         [parent.manifest['production_id'], grandchild.manifest['production_id']])
        self.assertEqual([row['seed'] for row in selected], [101, 102])
        self.native_output(parent, 1).write_bytes(original)
        _, report = self.merge(parent.output_dir, output=self.root / 'prefer-original')
        self.assertEqual({row['production_id'] for row in report['samples']['sample']['jobs']}, {parent.manifest['production_id']})

    def test_condor_recovery_is_selected_without_querying_scheduler_at_merge_time(self):
        parent = self.fixture('condor')
        (Path(parent.output_dir) / 'output_sample_123_0.tar.gz').unlink()
        evidence = {'query_errors': [], 'identity_checks_enabled': True, 'jobs': [
            {'job_id': job['job_id'], 'state': 'completed', 'source': 'history'}
            for job in parent.manifest['jobs']]}
        with patch('mgpd.recovery.production_status', return_value=evidence):
            child = self.recovery(parent, 'retry')
        self.complete(child)
        _, report = self.merge(parent.output_dir)
        selected = report['samples']['sample']['jobs']
        self.assertEqual([row['production_id'] for row in selected],
                         [child.manifest['production_id'], parent.manifest['production_id']])
        self.assertEqual(report['samples']['sample']['metrics']['lhe_events'], 4)

    def test_bad_registration_is_not_bypassed_by_partial_mode(self):
        parent = self.fixture()
        self.native_output(parent, 1).unlink()
        child = self.recovery(parent, 'retry')
        self.complete(child)
        manifest = json.loads(Path(parent.output_dir, 'manifest.json').read_text())
        manifest['recoveries'][0]['production_id'] = 'b' * 32
        write_manifest(parent.output_dir, manifest)
        with self.assertRaisesRegex(ValueError, 'ownership'):
            self.merge(parent.output_dir, allow_partial=True)
        self.assertFalse((Path(parent.output_dir) / 'merged').exists())

    def test_unfinished_local_outputs_are_not_selected(self):
        parent = self.fixture()
        manifest = parent.manifest
        manifest['state'] = 'running'
        write_manifest(parent.output_dir, manifest)
        with self.assertRaisesRegex(ValueError, 'unfinished'):
            self.merge(parent.output_dir)

    def test_unknown_condor_exit_evidence_blocks_selection(self):
        parent = self.fixture('condor')
        for path in Path(parent.output_dir).glob('job_*.log'):
            path.unlink()
        with self.assertRaisesRegex(ValueError, 'unfinished'):
            self.merge(parent.output_dir)

    def test_changed_recovery_inputs_are_rejected(self):
        parent = self.fixture()
        self.native_output(parent, 1).unlink()
        child = self.recovery(parent, 'retry')
        self.complete(child)
        path = Path(child.output_dir, child.manifest['jobs'][0]['run_file'])
        path.write_text(path.read_text() + '# tampered\n')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.merge(parent.output_dir)

    def test_tool_failure_does_not_publish_any_sample(self):
        parent = self.fixture(multiple=True)
        calls = 0
        def fail_second(command, check):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise subprocess.CalledProcessError(1, command)
            return self.fake_container(command, check)
        with patch('mgpd.merging.subprocess.run', side_effect=fail_second), self.assertRaises(subprocess.CalledProcessError):
            merge_production(parent.output_dir)
        self.assertFalse((Path(parent.output_dir) / 'merged').exists())
        self.assertFalse(list(Path(parent.output_dir).glob('.mgpd-dataset-*')))

    def test_production_cli_without_output_and_archive_expert_interface(self):
        parent = self.fixture()
        with patch('mgpd.merging.subprocess.run', side_effect=self.fake_container), redirect_stdout(io.StringIO()):
            self.assertEqual(merge_main([parent.output_dir, '--sample', 'sample', '--backend', 'local-apptainer']), 0)
        with patch('mgpd.merging.merge_outputs', return_value='custom') as merge, redirect_stdout(io.StringIO()):
            self.assertEqual(merge_main(['job1.tar.gz', 'job2.tar.gz', '-o', 'custom']), 0)
            self.assertEqual(merge.call_args.args, (['job1.tar.gz', 'job2.tar.gz'], 'custom'))

    def test_real_root_event_totals_and_decompressed_lhe(self):
        try:
            import uproot
        except ImportError:
            self.skipTest('Optional uproot is not installed')
        parent = self.fixture(outputs=['lhe', 'root'])
        output, report = self.merge(parent.output_dir, extract_lhe=True, keep_all=True)
        self.assertEqual(report['samples']['sample']['metrics']['delphes_events'], 4)
        self.assertTrue((output / 'merged_unweighted_events.lhe').is_file())
        self.assertFalse(list(output.rglob('*.lhe.gz')))
        with uproot.open(output / 'merged_delphes_events.root') as source:
            self.assertEqual(source['Delphes'].num_entries, 4)
