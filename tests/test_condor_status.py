"""Scheduler tracking tests with recorded ClassAds; no real Condor is contacted."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/run_mg_pythia_delphes.py'
spec = importlib.util.spec_from_file_location('status_runner', SCRIPT)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class CondorStatusTests(unittest.TestCase):
    def manifest(self, root, count=2, tagged=True):
        value = {'schema_version': 2, 'production_id': 'a' * 32, 'mode': 'condor', 'state': 'submitted',
                 'jobs': [{'job_id': f'sample_{i}', 'sample': 'sample'} for i in range(count)],
                 'submission': {'identity_tagged': tagged, 'jobs': [
                     {'job_id': f'sample_{i}', 'sample': 'sample', 'scheduler_id': f'123.{i}'}
                     for i in range(count)]}}
        runner.write_manifest(root, value)
        return value

    def ad(self, index, status, **extra):
        return dict(ClusterId=123, ProcId=index, JobStatus=status,
                    MGProductionId='a' * 32, MGJobId=f'sample_{index}', **extra)

    def response(self, records):
        return subprocess.CompletedProcess([], 0, stdout=json.dumps(records), stderr='')

    def test_terse_ranges_count_and_duplicate_checks(self):
        self.assertEqual(runner.parse_condor_job_ids('12.0 - 12.2\n13.0\n', 4),
                         ['12.0', '12.1', '12.2', '13.0'])
        for stdout in ('', 'warning text', '12.1 - 13.2', '12.2 - 12.0', '12.0\n12.0', '12.0 - 12.100000000'):
            with self.subTest(stdout=stdout), self.assertRaises(ValueError):
                runner.parse_condor_job_ids(stdout, 2)
        with self.assertRaisesRegex(ValueError, 'Expected 2'):
            runner.parse_condor_job_ids('12.0', 2)

    def test_queue_then_history_only_for_missing_ids_and_no_manifest_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.manifest(root)
            before = (root / 'manifest.json').read_bytes()
            responses = [self.response([self.ad(0, 2)]),
                         self.response([self.ad(1, 4, ExitBySignal=False, ExitCode=0)])]
            with patch.object(subprocess, 'run', side_effect=responses) as query:
                result = runner.production_status(root)
            self.assertEqual([job['state'] for job in result['jobs']], ['running', 'completed'])
            self.assertEqual([job['source'] for job in result['jobs']], ['queue', 'history'])
            self.assertEqual(result['counts'], {'running': 1, 'completed': 1})
            queue_args, history_args = [call.args[0] for call in query.call_args_list]
            self.assertIn('MGProductionId == "' + 'a' * 32 + '"', queue_args[-1])
            self.assertNotIn('ProcId == 0', history_args[history_args.index('-constraint') + 1])
            self.assertIn('ProcId == 1', history_args[history_args.index('-constraint') + 1])
            self.assertEqual((root / 'manifest.json').read_bytes(), before)

    def test_empty_successful_queue_uses_history_without_query_error(self):
        for stdout in ('', ' \n\t'):
            with self.subTest(stdout=stdout), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.manifest(root)
                responses = [subprocess.CompletedProcess([], 0, stdout=stdout, stderr=''),
                             self.response([self.ad(i, 4, ExitBySignal=False, ExitCode=0) for i in range(2)])]
                with patch.object(subprocess, 'run', side_effect=responses):
                    status = runner.production_status(root)
                self.assertEqual(status['counts'], {'completed': 2})
                self.assertEqual(status['query_errors'], [])
                self.assertTrue(all(job['source'] == 'history' for job in status['jobs']))

    def test_empty_queue_and_history_remain_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.manifest(root)
            with patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout='', stderr='')):
                status = runner.production_status(root)
            self.assertEqual(status['counts'], {'unknown': 2})
            self.assertEqual(status['query_errors'], [])

    def test_empty_response_with_stderr_is_still_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.manifest(root)
            with patch.object(subprocess, 'run', side_effect=[
                    subprocess.CompletedProcess([], 0, stdout='', stderr='scheduler warning'), self.response([])]):
                status = runner.production_status(root)
            self.assertTrue(status['query_errors'])
            self.assertEqual(status['counts'], {'unknown': 2})

    def test_scheduler_states_require_real_exit_information(self):
        cases = [(self.ad(0, 1), 'pending'), (self.ad(0, 2), 'running'),
                 (self.ad(0, 3), 'removed'), (self.ad(0, 5), 'held'),
                 (self.ad(0, 6), 'transferring'), (self.ad(0, 7), 'suspended'),
                 (self.ad(0, 4), 'unknown'),
                 (self.ad(0, 4, ExitCode=0), 'unknown'),
                 (self.ad(0, 4, ExitCode=0, ExitBySignal=False), 'completed'),
                 (self.ad(0, 4, ExitCode=42, ExitBySignal=False), 'failed'),
                 (self.ad(0, 4, ExitSignal=9, ExitBySignal=True), 'failed'),
                 (self.ad(0, True), 'unknown')]
        for ad, expected in cases:
            with self.subTest(ad=ad):
                self.assertEqual(runner.condor_ad_status(ad), expected)

    def test_missing_history_and_identity_mismatches_are_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.manifest(root)
            wrong_job = self.ad(0, 4, ExitBySignal=False, ExitCode=0)
            wrong_job['MGJobId'] = 'some_other_job'
            wrong_production = self.ad(1, 4, ExitBySignal=False, ExitCode=0)
            wrong_production['MGProductionId'] = 'b' * 32
            with patch.object(subprocess, 'run', side_effect=[self.response([wrong_job, wrong_production]), self.response([])]):
                result = runner.production_status(root)
            self.assertEqual(result['state'], 'unknown')
            self.assertEqual(result['counts'], {'unknown': 2})

    def test_query_errors_do_not_become_success(self):
        errors = [FileNotFoundError('condor_q'), subprocess.TimeoutExpired('condor_q', 30),
                  subprocess.CalledProcessError(1, 'condor_q', stderr='scheduler unavailable'),
                  subprocess.CompletedProcess([], 0, stdout='not JSON', stderr='')]
        for error in errors:
            with self.subTest(error=error), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.manifest(root)
                with patch.object(subprocess, 'run', side_effect=[error, self.response([])]):
                    result = runner.production_status(root)
                self.assertEqual(result['counts'], {'unknown': 2})
                self.assertTrue(result['query_errors'])

    def test_held_reason_is_reported_without_history_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.manifest(root, count=1)
            with patch.object(subprocess, 'run', return_value=self.response([
                    self.ad(0, 5, HoldReason='Input transfer failed', HoldReasonCode=13)])) as query:
                result = runner.production_status(root)
            query.assert_called_once()
            self.assertEqual(result['state'], 'held')
            self.assertEqual(result['jobs'][0]['HoldReason'], 'Input transfer failed')

    def test_prepared_and_local_states_do_not_query_scheduler(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = self.manifest(root)
            value['state'] = 'prepared'
            value.pop('submission')
            runner.write_manifest(root, value)
            with patch.object(subprocess, 'run') as query:
                result = runner.production_status(root)
                self.assertEqual(result['state'], 'prepared')
                value.update(mode='local-apptainer', state='execution_finished')
                runner.write_manifest(root, value)
                self.assertEqual(runner.production_status(root)['state'], 'execution_finished')
                query.assert_not_called()

    def test_old_raw_submission_output_can_be_read_without_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = self.manifest(root, tagged=False)
            value['submission'] = {'stdout': '123.0 - 123.1\n'}
            runner.write_manifest(root, value)
            before = (root / 'manifest.json').read_bytes()
            with patch.object(subprocess, 'run', return_value=self.response([
                    self.ad(0, 1), self.ad(1, 2)])) as query:
                result = runner.production_status(root)
            self.assertEqual(result['counts'], {'pending': 1, 'running': 1})
            self.assertFalse(result['identity_checks_enabled'])
            self.assertNotIn('MGProductionId ==', query.call_args.args[0][-1])
            self.assertEqual((root / 'manifest.json').read_bytes(), before)

    def test_status_cli_json_is_machine_readable(self):
        from contextlib import redirect_stdout
        import io
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.manifest(root, count=1)
            output = io.StringIO()
            with patch.object(subprocess, 'run', return_value=self.response([
                    self.ad(0, 4, ExitBySignal=False, ExitCode=0)])), redirect_stdout(output):
                code = runner.main(['--status', str(root), '--json'])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())['state'], 'completed')


if __name__ == '__main__':
    unittest.main()
