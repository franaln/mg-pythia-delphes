"""Command boundaries, removed syntax and exit codes without scheduler access."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from mgpd.cli import main, parse_args
from mgpd.projects import create_project


class CommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / 'project'
        create_project(self.project, mode='local-apptainer', image='test.sif')

    def call(self, arguments):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            code = main(list(map(str, arguments)))
        return code, output.getvalue()

    def test_prepare_execute_and_run_are_separate_stages(self):
        with patch('mgpd.cli.execute_production') as execute:
            self.assertEqual(self.call(['validate-config', self.project])[0], 0)
            self.assertFalse((self.project / 'production').exists())
            self.assertEqual(self.call(['prepare', self.project])[0], 0)
            execute.assert_not_called()
            self.assertEqual(self.call(['execute', self.project / 'production'])[0], 0)
            execute.assert_called_once()
            self.assertEqual(execute.call_args.args[0].manifest['state'], 'prepared')
        with patch('mgpd.cli.execute_production') as execute:
            self.assertEqual(self.call(['run', self.project, '-o', self.root / 'combined'])[0], 0)
            execute.assert_called_once()
            self.assertEqual(Path(execute.call_args.args[0].output_dir), self.root / 'combined')

    def test_old_root_and_combined_flags_are_rejected_before_work(self):
        cases = [['-c', 'config.yml', '-o', 'production'], ['--execute-prepared', 'production'],
                 ['--status', 'production'], ['--validate-results', 'production'],
                 ['--plan-recovery', 'production'], ['--prepare-recovery', 'production', '-o', 'child'],
                 ['run', self.project, '--dry-run'], ['run', self.project, '--validate-only'],
                 ['run', '-c', self.project / 'config.yml'],
                 ['validate-config', self.project, '--force'], ['execute', 'production', '--mode', 'condor']]
        with patch('mgpd.cli.prepare_project') as prepare, patch('mgpd.cli.execute_production') as execute:
            for arguments in cases:
                with self.subTest(arguments=arguments), self.assertRaises(SystemExit) as error:
                    self.call(arguments)
                self.assertEqual(error.exception.code, 2)
            prepare.assert_not_called(); execute.assert_not_called()

    def test_validation_scopes_and_failure_exit_codes(self):
        code, output = self.call(['validate-config', self.project, '--json'])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output)['valid'])
        (self.project / 'cards/run_card.dat').unlink()
        self.assertEqual(self.call(['validate-config', self.project])[0], 1)
        code, output = self.call(['validate-config', self.project, '--json'])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(output)['valid'])
        report = {'state': 'invalid', 'recorded_state': 'failed', 'jobs': [], 'samples': {}, 'metadata_errors': []}
        with patch('mgpd.cli.validate_production', return_value=report) as validate:
            code, output = self.call(['validate-results', 'production', '--json', '--event-count', 'nonempty', '--skip-seed-check'])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(output)['state'], 'invalid')
            self.assertFalse(validate.call_args.kwargs['check_seed'])
            self.assertEqual(validate.call_args.kwargs['event_count'], 'nonempty')

    def test_explicit_input_base_supports_saved_as_configurations(self):
        copied = self.root / 'copied.yml'
        copied.write_bytes((self.project / 'config.yml').read_bytes())
        self.assertEqual(self.call(['validate-config', copied])[0], 1)
        self.assertEqual(self.call(['validate-config', copied, '--base-directory', self.project])[0], 0)
        self.assertEqual(self.call(['prepare', copied, '--base-directory', self.project,
                                   '-o', self.root / 'copied-production'])[0], 0)

    def test_status_query_errors_remain_errors(self):
        report = {'state': 'unknown', 'recorded_state': 'submitted', 'jobs': [], 'query_errors': [{'error': 'unavailable'}]}
        with patch('mgpd.cli.production_status', return_value=report):
            code, output = self.call(['status', 'production', '--json'])
            self.assertEqual(code, 1)
            self.assertTrue(json.loads(output)['query_errors'])

    def test_recovery_plans_or_prepares_but_never_executes(self):
        with patch('mgpd.cli.plan_recovery', return_value={'jobs': [], 'query_errors': []}) as plan, \
                patch('mgpd.cli.prepare_recovery') as prepare, patch('mgpd.cli.execute_production') as execute:
            self.assertEqual(self.call(['recover', 'production', '--plan', '--json'])[0], 0)
            plan.assert_called_once(); prepare.assert_not_called(); execute.assert_not_called()
            prepare.return_value = SimpleNamespace(output_dir='child', manifest={'production_id': 'id', 'state': 'prepared', 'jobs': []})
            code, output = self.call(['recover', 'production', '-o', 'child', '--job-id', 'sample_00000', '--json'])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output)['state'], 'prepared')
            self.assertEqual(prepare.call_args.kwargs['job_ids'], ['sample_00000'])
            execute.assert_not_called()
        for arguments in (['recover', 'production'], ['recover', 'production', '--plan', '-o', 'child'],
                          ['recover', 'production', '--plan', '--job-id', 'sample_00000']):
            with self.assertRaises(SystemExit):
                self.call(arguments)

    def test_root_and_each_command_help(self):
        commands = ('create', 'prepare', 'execute', 'run', 'validate-config', 'validate-results', 'recover', 'merge', 'status')
        code, output = self.call([])
        self.assertEqual(code, 0)
        for command in commands:
            self.assertIn(command, output)
            with self.subTest(command=command), self.assertRaises(SystemExit) as error:
                self.call([command, '--help'])
            self.assertEqual(error.exception.code, 0)
        _, args = parse_args(['prepare', str(self.project), '--mode', 'condor'])
        self.assertEqual(args.run_mode, 'condor')
