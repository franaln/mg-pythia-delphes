"""Selective recovery using real prepared inputs and mocked scheduler evidence."""
import gzip
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = 'mgpd'
from mgpd import compat as runner
from mgpd import recovery as recovery_module


def lhe(seed):
    return gzip.compress((f'<LesHouchesEvents>\n<header>\n{seed} = iseed\n</header>\n'
                          '<init>\n2212 2212 6500 6500 0 0 0 0 3 1\n10 0.1 1 1\n</init>\n'
                          '<event>\n</event>\n<event>\n</event>\n</LesHouchesEvents>\n').encode())


class RecoveryTests(unittest.TestCase):
    def fixture(self, root, mode='local-apptainer', count=2, good_first=True):
        card = root / 'run_card.dat'
        card.write_text('')
        config = {'run': {'name': 'sample', 'mode': mode, 'image': 'mg-pythia-delphes-latest',
                          'njobs': count, 'nevents': 2, 'outputs': ['lhe']},
                  'process': 'generate p p > e+ e-', 'cards': {'run': str(card)}, 'options': {'seed': 101}}
        production = runner.prepare_production(config, root / 'parent')
        manifest = production.manifest
        manifest['execution_attempt'] = {'started_at': 'previous attempt'}
        manifest['state'] = 'submitted' if mode == 'condor' else 'failed'
        if mode == 'condor':
            manifest['submission'] = {'jobs': [{'job_id': job['job_id'], 'scheduler_id': f'123.{i}'}
                                                for i, job in enumerate(manifest['jobs'])]}
        else:
            manifest['failed_job_id'] = manifest['jobs'][-1]['job_id']
        runner.write_manifest(production.output_dir, manifest)
        if good_first:
            if mode == 'condor':
                output = Path(production.output_dir) / 'output_sample_123_0.tar.gz'
                data = lhe(101)
                with tarfile.open(output, 'w:gz') as archive:
                    info = tarfile.TarInfo('output_sample_123_0_unweighted_events.lhe.gz')
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
                Path(production.output_dir, 'job_sample_123_0.log').write_text('Normal termination (return value 0)\n')
            else:
                path = Path(production.output_dir, manifest['jobs'][0]['run_file']).parent / 'RUN/Events/run_01'
                path.mkdir(parents=True)
                (path / 'unweighted_events.lhe.gz').write_bytes(lhe(101))
        return production

    def scheduler(self, states, errors=None):
        return {'jobs': [{'job_id': f'sample_{i:05d}', 'state': state, 'source': 'history'} for i, state in enumerate(states)],
                'query_errors': errors or [], 'identity_checks_enabled': True}

    def test_plan_preserves_good_jobs_selects_failed_and_blocks_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent = self.fixture(root, 'condor', count=3)
            before = Path(parent.output_dir, 'manifest.json').read_bytes()
            with patch.object(recovery_module, 'production_status', return_value=self.scheduler(['completed', 'failed', 'running'])):
                plan = runner.plan_recovery(parent.output_dir)
            self.assertEqual([job['state'] for job in plan['jobs']], ['preserved', 'eligible', 'blocked'])
            self.assertEqual(plan['eligible_job_ids'], ['sample_00001'])
            self.assertEqual(Path(parent.output_dir, 'manifest.json').read_bytes(), before)

    def test_only_terminal_condor_states_allow_recovery(self):
        for state in ('pending', 'running', 'held', 'transferring', 'suspended', 'unknown', 'completed', 'failed', 'removed'):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                parent = self.fixture(root, 'condor', count=1, good_first=False)
                with patch.object(recovery_module, 'production_status', return_value=self.scheduler([state])):
                    plan = runner.plan_recovery(parent.output_dir)
                self.assertEqual(bool(plan['eligible_job_ids']), state in ('completed', 'failed', 'removed'))
                if state in ('completed', 'failed', 'removed'):
                    queued = self.scheduler([state])
                    queued['jobs'][0]['source'] = 'queue'
                    with patch.object(recovery_module, 'production_status', return_value=queued):
                        self.assertFalse(runner.plan_recovery(parent.output_dir)['eligible_job_ids'])

    def test_queries_and_ambiguous_submissions_block_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent = self.fixture(root, 'condor', count=1, good_first=False)
            with patch.object(recovery_module, 'production_status', return_value=self.scheduler(['failed'], [{'error': 'query failed'}])):
                self.assertFalse(runner.plan_recovery(parent.output_dir)['eligible_job_ids'])
            parent.manifest['state'] = 'submission_unknown'
            runner.write_manifest(parent.output_dir, parent.manifest)
            with patch.object(recovery_module, 'production_status') as query:
                self.assertFalse(runner.plan_recovery(parent.output_dir)['eligible_job_ids'])
                query.assert_not_called()

    def test_condor_child_copies_exact_inputs_seeds_and_only_selected_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent = self.fixture(root, 'condor', count=3)
            good_output = Path(parent.output_dir, 'output_sample_123_0.tar.gz').read_bytes()
            (root / 'run_card.dat').unlink()  # Preparation does not need original source cards.
            with patch.object(recovery_module, 'production_status', return_value=self.scheduler(['completed', 'failed', 'running'])):
                child = runner.prepare_recovery(parent.output_dir, root / 'retry')
            self.assertEqual([job['job_id'] for job in child.manifest['jobs']], ['sample_00001'])
            self.assertEqual(child.manifest['jobs'][0]['seed'], 102)
            for relative in ('run_sample.tar.gz', 'sample_00001.mg5', 'run_mg_pythia_delphes.sh'):
                self.assertEqual(Path(child.output_dir, relative).read_bytes(), Path(parent.output_dir, relative).read_bytes())
            submission = Path(child.output_dir, child.submit_file).read_text()
            self.assertEqual(submission.count('queue 1'), 1)
            self.assertIn(child.manifest['production_id'], submission)
            self.assertNotIn(parent.manifest['production_id'], submission)
            self.assertFalse(Path(child.output_dir, 'sample_00000.mg5').exists())
            self.assertFalse(list(Path(child.output_dir).glob('output_*.tar.gz')))
            self.assertEqual(Path(parent.output_dir, 'output_sample_123_0.tar.gz').read_bytes(), good_output)

    def test_local_child_runs_once_and_cannot_be_claimed_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent = self.fixture(root)
            child = runner.prepare_recovery(parent.output_dir, root / 'retry')
            self.assertEqual([job['seed'] for job in child.manifest['jobs']], [102])
            self.assertFalse(runner.plan_recovery(parent.output_dir)['eligible_job_ids'])
            with self.assertRaises(ValueError):
                runner.prepare_recovery(parent.output_dir, root / 'duplicate', job_ids=['sample_00001'])
            self.assertFalse((root / 'duplicate').exists())
            with patch.object(subprocess, 'run') as launch:
                runner.execute_production(child)
                self.assertEqual(launch.call_count, 1)
                self.assertEqual(launch.call_args.args[0][0], 'apptainer')
                with self.assertRaises(ValueError):
                    runner.execute_production(child)
            parent_state = runner.read_production_manifest(parent.output_dir)
            self.assertEqual(parent_state['state'], 'failed')
            self.assertEqual(parent_state['recoveries'][0]['production_id'], child.manifest['production_id'])

    def test_child_rechecks_active_parent_before_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent = self.fixture(root, 'condor', count=1, good_first=False)
            with patch.object(recovery_module, 'production_status', return_value=self.scheduler(['failed'])):
                child = runner.prepare_recovery(parent.output_dir, root / 'retry')
            with patch.object(recovery_module, 'production_status', return_value=self.scheduler(['running'])), \
                 patch.object(subprocess, 'run') as launch:
                with self.assertRaisesRegex(ValueError, 'no longer eligible'):
                    runner.execute_production(child)
                launch.assert_not_called()
            self.assertNotIn('execution_attempt', runner.read_production_manifest(child.output_dir))

    def test_copied_child_and_force_overwrite_cannot_bypass_ownership(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent = self.fixture(root)
            child = runner.prepare_recovery(parent.output_dir, root / 'retry')
            shutil.copytree(child.output_dir, root / 'copied')
            with self.assertRaisesRegex(ValueError, 'ownership/path'):
                runner.execute_production(runner.load_prepared_production(root / 'copied'))
            with self.assertRaisesRegex(ValueError, 'recovery ownership'):
                runner.prepare_production(child.manifest['config'], child.output_dir, force=True)

    def test_failed_copy_releases_unexecutable_reservation_but_keeps_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent = self.fixture(root)
            with patch.object(shutil, 'copy2', side_effect=OSError('copy failed')):
                with self.assertRaises(OSError):
                    runner.prepare_recovery(parent.output_dir, root / 'bad_retry')
            saved = runner.read_production_manifest(parent.output_dir)
            self.assertEqual(saved['recoveries'][0]['state'], 'preparation_failed')
            self.assertFalse(saved['recoveries'][0]['blocks_jobs'])
            self.assertFalse(Path(root, 'bad_retry/manifest.json').exists())
            self.assertEqual(runner.plan_recovery(parent.output_dir)['eligible_job_ids'], ['sample_00001'])
            def corrupt_copy(source, target):
                Path(target).write_bytes(b'corrupt copy')
            with patch.object(shutil, 'copy2', side_effect=corrupt_copy):
                with self.assertRaisesRegex(ValueError, 'corrupted while copying'):
                    runner.prepare_recovery(parent.output_dir, root / 'corrupt_retry')
            self.assertFalse(Path(root, 'corrupt_retry/manifest.json').exists())
            runner.prepare_recovery(parent.output_dir, root / 'retry')

    def test_failed_child_can_have_its_own_recovery_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent = self.fixture(root)
            child = runner.prepare_recovery(parent.output_dir, root / 'retry')
            with patch.object(subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'apptainer')):
                with self.assertRaises(subprocess.CalledProcessError):
                    runner.execute_production(child)
            grandchild = runner.prepare_recovery(child.output_dir, root / 'retry_again')
            self.assertEqual(grandchild.manifest['recovery']['parent_id'], child.manifest['production_id'])
            self.assertEqual(grandchild.manifest['jobs'][0]['seed'], 102)
            with patch.object(subprocess, 'run') as launch:
                runner.execute_production(grandchild)
                launch.assert_called_once()

    def test_running_local_and_unavailable_validation_are_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent = self.fixture(root, count=1, good_first=False)
            parent.manifest['state'] = 'running'
            runner.write_manifest(parent.output_dir, parent.manifest)
            self.assertFalse(runner.plan_recovery(parent.output_dir)['eligible_job_ids'])
            parent.manifest['state'] = 'failed'
            parent.manifest['jobs'][0]['outputs'] = ['root']
            parent.manifest['plan_sha256'] = runner.plan_sha256(parent.manifest)
            runner.write_manifest(parent.output_dir, parent.manifest)
            path = Path(parent.output_dir, parent.manifest['jobs'][0]['run_file']).parent / 'RUN/Events/run_01'
            path.mkdir(parents=True)
            (path / 'sample_delphes_events.root').write_bytes(b'placeholder')
            with patch.dict(sys.modules, {'uproot': None}):
                plan = runner.plan_recovery(parent.output_dir)
            self.assertFalse(plan['eligible_job_ids'])
            self.assertIn('dependency', plan['jobs'][0]['reason'])
            parent.manifest['jobs'][0]['outputs'] = ['all']
            parent.manifest['plan_sha256'] = runner.plan_sha256(parent.manifest)
            runner.write_manifest(parent.output_dir, parent.manifest)
            plan = runner.plan_recovery(parent.output_dir)
            self.assertFalse(plan['eligible_job_ids'])
            self.assertIn('unsupported', plan['jobs'][0]['reason'])

    def test_recovery_parent_lock_and_cli_plan_do_not_submit(self):
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent = self.fixture(root)
            with runner.execution_lock(parent.output_dir):
                with self.assertRaisesRegex(ValueError, 'Another process'):
                    runner.prepare_recovery(parent.output_dir, root / 'retry')
            output = io.StringIO()
            with redirect_stdout(output), patch.object(subprocess, 'run') as launch:
                self.assertEqual(runner.main(['recover', parent.output_dir, '--plan', '--json']), 0)
                launch.assert_not_called()
            self.assertEqual(json.loads(output.getvalue())['eligible_job_ids'], ['sample_00001'])

    def test_untagged_legacy_ids_and_internal_policy_overrides_are_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent = self.fixture(root, 'condor', count=1, good_first=False)
            status = self.scheduler(['failed'])
            status['identity_checks_enabled'] = False
            with patch.object(recovery_module, 'production_status', return_value=status):
                plan = runner.plan_recovery(parent.output_dir)
            self.assertFalse(plan['eligible_job_ids'])
            self.assertIn('identity tags', plan['jobs'][0]['reason'])
            with self.assertRaisesRegex(ValueError, 'Unsupported recovery validation policy'):
                runner.prepare_recovery(parent.output_dir, root / 'retry', ignore_recovery_id='bypass')
            self.assertFalse((root / 'retry').exists())



if __name__ == '__main__':
    unittest.main()
