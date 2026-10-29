"""Exercise the live-workflow harness with a fake scheduler, never real Condor."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import test_examples as examples_tests

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/test_condor_workflow.py'


class CondorWorkflowDriverTests(unittest.TestCase):
    def fake_scheduler(self, root, fail_child=False):
        env = examples_tests.ExampleTests().fake_condor(root)
        history = root / 'bin/condor_history'
        history.write_text(f'#!{sys.executable}\n' + f'''import json, sys
from pathlib import Path
records = []
for path in Path({str(root)!r}).rglob('manifest.json'):
    manifest = json.loads(path.read_text())
    if manifest['production_id'] not in ' '.join(sys.argv):
        continue
    for i, job in enumerate(manifest['jobs']):
        records.append(dict(ClusterId=123, ProcId=i, JobStatus=4, ExitBySignal=False, ExitCode=0,
                            MGProductionId=manifest['production_id'], MGJobId=job['job_id']))
print(json.dumps(records))
''')
        submit = root / 'bin/condor_submit'
        code = submit.read_text()
        line = "jobs = json.loads(Path('manifest.json').read_text())['jobs']"
        injected = line + f'''\nwith Path({str(root / 'submits.jsonl')!r}).open('a') as log:
    log.write(str(len(jobs)) + '\\n')
'''
        if fail_child:
            injected += "if len(jobs) == 1:\n    raise SystemExit(42)\n"
        submit.write_text(code.replace(line, injected))
        return env

    def test_explicit_execute_required_before_any_submission(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'results'
            result = subprocess.run([sys.executable, str(SCRIPT), '--output', str(output)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('requires --execute', result.stderr)
            self.assertFalse(output.exists())

    def test_scheduler_query_failure_does_not_prepare_or_submit_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = self.fake_scheduler(root)
            (root / 'bin/condor_q').write_text('#!/bin/bash\nexit 1\n')
            output = root / 'results'
            result = subprocess.run([sys.executable, str(SCRIPT), '--execute', '--output', str(output)],
                                    env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 1)
            report = json.loads((output / 'report.json').read_text())
            self.assertEqual(report['state'], 'failed')
            self.assertNotIn('withheld', report)
            self.assertFalse((output / 'retry').exists())
            self.assertEqual((root / 'submits.jsonl').read_text().splitlines(), ['2'])
            # Retry the observation with the real-world empty successful queue
            # response. Only the child may be submitted during resume.
            (root / 'bin/condor_q').write_text('#!/bin/bash\nexit 0\n')
            resumed = subprocess.run([sys.executable, str(SCRIPT), '--execute', '--resume',
                                      '--output', str(output)], env=env,
                                     capture_output=True, text=True, timeout=60)
            self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
            resumed_report = json.loads((output / 'report.json').read_text())
            self.assertEqual(resumed_report['state'], 'passed')
            self.assertTrue(resumed_report['withheld']['restored'])
            self.assertTrue(resumed_report['previous_errors'])
            self.assertEqual((root / 'submits.jsonl').read_text().splitlines(), ['2', '1'])

    def test_full_workflow_and_failure_restore_withheld_archive(self):
        for fail_child in (False, True):
            with self.subTest(fail_child=fail_child), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                env = self.fake_scheduler(root, fail_child)
                output = root / 'results'
                result = subprocess.run([sys.executable, str(SCRIPT), '--execute',
                                         '--events', '2', '--timeout', '10', '--output', str(output)],
                                        env=env, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, int(fail_child), result.stdout + result.stderr)
                report = json.loads((output / 'report.json').read_text())
                self.assertEqual(report['withheld']['restored'], True)
                self.assertTrue(Path(report['withheld']['original']).is_file())
                self.assertFalse(Path(report['withheld']['backup']).exists())
                self.assertEqual((root / 'submits.jsonl').read_text().splitlines(), ['2', '1'])
                if not fail_child:
                    self.assertEqual(report['state'], 'passed')
                    self.assertEqual(len(report['productions']), 2)
                    self.assertEqual(len(report['productions'][1]['scheduler_jobs']), 1)
                before = (output / 'report.json').read_bytes()
                again = subprocess.run([sys.executable, str(SCRIPT), '--execute', '--output', str(output)],
                                       env=env, capture_output=True, text=True)
                self.assertEqual(again.returncode, 2)
                self.assertEqual((output / 'report.json').read_bytes(), before)
                if not fail_child:
                    refused = subprocess.run([sys.executable, str(SCRIPT), '--execute', '--resume',
                                              '--output', str(output)], env=env, capture_output=True, text=True)
                    self.assertEqual(refused.returncode, 2)
                    self.assertEqual((output / 'report.json').read_bytes(), before)
                    self.assertEqual((root / 'submits.jsonl').read_text().splitlines(), ['2', '1'])
