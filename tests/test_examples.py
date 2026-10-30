"""Exercise the documentation and test driver without physics software/HTCondor."""
import gzip
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
DRIVER = REPO / 'scripts/test_examples.py'
spec = importlib.util.spec_from_file_location('example_driver', DRIVER)
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)


class ExampleTests(unittest.TestCase):
    def test_every_yaml_example_is_registered(self):
        registered = {entry['config'] for entry in driver.catalog()}
        actual = {str(path.relative_to(driver.EXAMPLES)) for pattern in ('*.yml', '*.yaml')
                  for path in driver.EXAMPLES.rglob(pattern)}
        self.assertEqual(registered, actual)

    def test_all_examples_prepare_for_each_backend(self):
        for mode in ('condor', 'local-docker', 'local-apptainer'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / 'results'
                result = subprocess.run([sys.executable, '-B', str(DRIVER), '--mode', mode,
                                         '--output', str(output)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                report = json.loads((output / 'report.json').read_text())
                self.assertEqual(len(report['results']), len(driver.catalog()))
                self.assertTrue(all(item['state'] == 'prepared' for item in report['results']))

    def test_lhe_validation_detects_wrong_seeds_and_event_counts(self):
        data = b'<LesHouchesEvents>\n<header>\n1001 = iseed\n</header>\n<init>\n1 1 6500 6500 0 0 0 0 3 1\n1.25D+00 0.1 1 1\n</init>\n<event>\n</event>\n</LesHouchesEvents>\n'
        job = {'seed': 1001, 'nevents': 1}
        result = driver.check_lhe(io.BytesIO(gzip.compress(data)), job)
        self.assertEqual(result['cross_section_pb'], 1.25)
        for invalid in ({'seed': 42, 'nevents': 1}, {'seed': 1001, 'nevents': 2}):
            with self.assertRaises(ValueError):
                driver.check_lhe(io.BytesIO(gzip.compress(data)), invalid)

    def test_lhe_init_accepts_optional_xml_metadata(self):
        block = """<init version="3.0">
<!-- Standard beam and process records -->
1 1 6500 6500 0 0 0 0 3 2 # NPRUP=2
1.25D+00 0.1 1 1
2.5e+00 0.1 1 2
<generator name="MadGraph5_aMC@NLO" version="3.5.6">
Generation details
</generator>
<weightinfo name="scale">100 200 300 400</weightinfo>
</init>"""
        self.assertEqual(driver.parse_lhe_init(block), 3.75)
        # Exercise the full streaming validator, including attributed init tags.
        data = ('<LesHouchesEvents>\n<header>\n1001 = iseed\n</header>\n'
                + block + '\n<event>\n</event>\n</LesHouchesEvents>\n')
        result = driver.check_lhe(io.BytesIO(gzip.compress(data.encode())), {'seed': 1001, 'nevents': 1})
        self.assertEqual(result['cross_section_pb'], 3.75)

    def test_lhe_init_still_rejects_missing_or_malformed_records(self):
        for record, message in [('', 'NPRUP=1, found 0'),
                                ('1.0 0.1 1', 'expected 4 fields'),
                                ('bad 0.1 1 1', 'Malformed numeric'),
                                ('nan 0.1 1 1', 'Nonfinite'),
                                ('1 0.1 1 1\n2 0.1 1 2', 'NPRUP=1, found 2')]:
            block = '<init>\n1 1 6500 6500 0 0 0 0 3 1\n' + record + '\n<generator>metadata</generator>\n</init>'
            with self.subTest(record=record), self.assertRaisesRegex(ValueError, message):
                driver.parse_lhe_init(block)

    def test_live_mode_requires_explicit_example(self):
        result = subprocess.run([sys.executable, '-B', str(DRIVER), '--mode', 'condor', '--execute', '--output', '/unused'],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('explicit --example', result.stderr)

    def test_aborted_job_is_not_accepted_as_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            (output / 'job_sample_123_0.log').write_text('009 (123.000.000) Job was aborted.\n')
            with self.assertRaisesRegex(ValueError, 'did not terminate normally'):
                driver.check_condor_job(output, {'sample': 'sample'}, '123.0')

    def fake_condor(self, root):
        tools = root / 'bin'
        tools.mkdir()
        submit = tools / 'condor_submit'
        submit.write_text(f'#!{sys.executable}\n' + '''import gzip, io, json, tarfile
from pathlib import Path
jobs = json.loads(Path('manifest.json').read_text())['jobs']
for i, job in enumerate(jobs):
    stem = f"{job['sample']}_123_{i}"
    Path(f'job_{stem}.log').write_text('005 (123.000.000) Job terminated.\\n (1) Normal termination (return value 0)\\n')
    output = f'output_{stem}'
    lhe = f"<LesHouchesEvents>\\n<header>\\n{job['seed']} = iseed\\n</header>\\n<init>\\n1 1 6500 6500 0 0 0 0 3 1\\n1.0 0.1 1 1\\n</init>\\n" + '<event>\\n</event>\\n' * job['nevents'] + '</LesHouchesEvents>\\n'
    empty_logs = io.BytesIO()
    with tarfile.open(fileobj=empty_logs, mode='w'):
        pass
    with tarfile.open(output + '.tar.gz', 'w:gz') as archive:
        for name, contents in [(output + '_unweighted_events.lhe.gz', gzip.compress(lhe.encode())), (output + '_logs.tar', empty_logs.getvalue())]:
            info = tarfile.TarInfo(name)
            info.size = len(contents)
            archive.addfile(info, io.BytesIO(contents))
print(f'123.0 - 123.{len(jobs)-1}')
''')
        (tools / 'condor_wait').write_text(f'#!{sys.executable}\nimport os, sys\nsys.exit(1 if os.environ.get("MG_TEST_FAIL_WAIT") else 0)\n')
        (tools / 'condor_rm').write_text(f'#!{sys.executable}\nimport sys\nprint(" ".join(sys.argv[1:]))\n')
        (tools / 'condor_q').write_text(f'#!{sys.executable}\nprint("[]")\n')
        (tools / 'condor_history').write_text(f'#!{sys.executable}\n' + """import json, os
from pathlib import Path
manifest = json.loads(Path(os.environ['MG_TEST_PRODUCTION'], 'manifest.json').read_text())
print(json.dumps([dict(ClusterId=123, ProcId=i, JobStatus=4, ExitBySignal=False, ExitCode=0,
                      MGProductionId=manifest['production_id'], MGJobId=job['job_id'])
                  for i, job in enumerate(manifest['jobs'])]))
""")
        for executable in tools.iterdir():
            executable.chmod(0o755)
        return dict(os.environ, PATH=str(tools) + os.pathsep + os.environ['PATH'])

    def test_live_driver_with_fake_scheduler_and_real_output_archives(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                env = self.fake_condor(root)
                if fail:
                    env['MG_TEST_FAIL_WAIT'] = '1'
                output = root / 'results'
                result = subprocess.run([sys.executable, '-B', str(DRIVER), '--mode', 'condor', '--execute',
                                         '--example', 'example7_parton_only', '--output', str(output),
                                         '--remove-on-failure', '--timeout', '10'], env=env,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, int(fail), result.stdout + result.stderr)
                report = json.loads((output / 'report.json').read_text())['results'][0]
                self.assertEqual(report['scheduler_ids'], ['123.0', '123.1'])
                manifest = json.loads((output / 'example7_parton_only/production/manifest.json').read_text())
                self.assertEqual(manifest['state'], 'submitted')
                self.assertIn('execution_attempt', manifest)
                if fail:
                    self.assertEqual(report['state'], 'failed')
                    self.assertEqual(report['cleanup']['stdout'].strip(), '123.0 123.1')
                else:
                    self.assertEqual(report['state'], 'passed')
                    self.assertEqual([check['seed'] for check in report['checks']], [1001, 1002])
                    production = output / 'example7_parton_only/production'
                    before = (production / 'manifest.json').read_bytes()
                    env['MG_TEST_PRODUCTION'] = str(production)
                    status = subprocess.run([sys.executable, '-B', '-m', str(driver.RUNNER), '--status',
                                             str(production), '--json'], env=env,
                                            capture_output=True, text=True)
                    self.assertEqual(status.returncode, 0, status.stderr)
                    self.assertEqual(json.loads(status.stdout)['counts'], {'completed': 2})
                    self.assertEqual((production / 'manifest.json').read_bytes(), before)

    def test_recovery_cli_workflow_with_fake_scheduler(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self.fake_condor(root)
            output = root / 'results'
            result = subprocess.run([sys.executable, '-B', str(DRIVER), '--mode', 'condor', '--execute',
                                     '--example', 'example7_parton_only', '--output', str(output),
                                     '--timeout', '10'], env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            parent = output / 'example7_parton_only/production'
            env['MG_TEST_PRODUCTION'] = str(parent)
            manifest = json.loads((parent / 'manifest.json').read_text())
            first, good = manifest['jobs']
            (parent / f"output_{first['sample']}_123_0.tar.gz").unlink()
            good_path = parent / f"output_{good['sample']}_123_1.tar.gz"
            good_bytes = good_path.read_bytes()
            command = [sys.executable, '-B', '-m', str(driver.RUNNER)]
            plan = subprocess.run(command + ['--plan-recovery', str(parent), '--json'],
                                  env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(plan.returncode, 0, plan.stderr)
            self.assertEqual(json.loads(plan.stdout)['eligible_job_ids'], [first['job_id']])
            child = root / 'retry'
            preparation = subprocess.run(command + ['--prepare-recovery', str(parent), '-o', str(child)],
                                         env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(preparation.returncode, 0, preparation.stdout + preparation.stderr)
            child_manifest = json.loads((child / 'manifest.json').read_text())
            self.assertEqual(len(child_manifest['jobs']), 1)
            self.assertEqual(child_manifest['jobs'][0]['seed'], first['seed'])
            self.assertNotIn('submission', child_manifest)
            execution = subprocess.run(command + ['--execute-prepared', str(child)],
                                       env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(execution.returncode, 0, execution.stdout + execution.stderr)
            validation = subprocess.run(command + ['--validate-results', str(child), '--json'],
                                        env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(validation.returncode, 0, validation.stdout + validation.stderr)
            self.assertEqual(json.loads(validation.stdout)['state'], 'valid')
            self.assertEqual(good_path.read_bytes(), good_bytes)



if __name__ == '__main__':
    unittest.main()
