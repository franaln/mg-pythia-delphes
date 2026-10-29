#!/usr/bin/env python3
"""Opt-in live Condor smoke test: saved execution, status, validation and recovery."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class WorkflowTest:
    def __init__(self, args):
        self.args = args
        self.report = (json.loads((args.output / 'report.json').read_text()) if args.resume
                       else {'state': 'running', 'steps': [], 'productions': []})

    def resume_parent(self):
        parent = self.args.output / 'preparation/example7_parton_only/production'
        records = self.report.get('productions', [])
        if (self.report.get('state') != 'failed' or len(records) != 1
                or records[0]['directory'] != str(parent)
                or self.report.get('withheld') or (self.args.output / 'retry').exists()):
            raise ValueError('Resume supports only a failed test with one submitted parent, before withholding/recovery')
        original = json.loads((parent / 'manifest.json').read_text())
        if (original['production_id'] != records[0]['production_id']
                or original['state'] != 'submitted' or original.get('recoveries')
                or original.get('submission', {}).get('jobs') != records[0]['scheduler_jobs']):
            raise ValueError('Parent identity/submission changed; refusing to resume')
        return original

    def save(self):
        (self.args.output / 'report.json').write_text(json.dumps(self.report, indent=2) + '\n')

    def command(self, name, arguments, expected=0, json_output=False):
        result = subprocess.run([sys.executable, '-m', 'mgpd', *map(str, arguments)],
                                capture_output=True, text=True, timeout=120)
        log = self.args.output / f'{len(self.report["steps"]):03d}-{name}.log'
        log.write_text(result.stdout + '\n' + result.stderr)
        self.report['steps'].append({'name': name, 'arguments': list(map(str, arguments)),
                                     'returncode': result.returncode, 'log': str(log)})
        self.save()
        if result.returncode != expected:
            raise ValueError(f'{name}: expected exit {expected}, got {result.returncode}; see {log}')
        return json.loads(result.stdout) if json_output else result

    def wait(self, directory, label):
        deadline = time.monotonic() + self.args.timeout
        while True:
            before = (directory / 'manifest.json').read_bytes()
            status = self.command(label + '-status', ['--status', directory, '--json'], json_output=True)
            if before != (directory / 'manifest.json').read_bytes():
                raise ValueError('Read-only status changed the manifest')
            if status['query_errors'] or not status.get('identity_checks_enabled'):
                raise ValueError('Scheduler observations lack reliable tagged identity')
            if any(job['state'] in ('failed', 'removed', 'held') for job in status['jobs']):
                raise ValueError(f'{label}: scheduler reports failed, removed or held jobs')
            if status['jobs'] and all(job['state'] == 'completed' and job['source'] == 'history'
                                      for job in status['jobs']):
                validation = self.command(label + '-validate', ['--validate-results', directory, '--json'],
                                          json_output=True)
                if validation['state'] != 'valid':
                    raise ValueError(f'{label}: completed outputs are not valid')
                return status, validation
            if time.monotonic() >= deadline:
                raise TimeoutError(f'{label}: timed out waiting for tagged history completion; inspect saved status logs')
            time.sleep(min(3, max(0, deadline - time.monotonic())))

    def execute(self, directory, label):
        self.command(label + '-execute', ['--execute-prepared', directory])
        manifest = json.loads((directory / 'manifest.json').read_text())
        self.report['productions'].append({'directory': str(directory),
                                           'production_id': manifest['production_id'],
                                           'scheduler_jobs': manifest.get('submission', {}).get('jobs', [])})
        self.save()
        before = (directory / 'manifest.json').read_bytes()
        self.command(label + '-duplicate-rejected', ['--execute-prepared', directory], expected=1)
        if (directory / 'manifest.json').read_bytes() != before:
            raise ValueError('Rejected duplicate execution changed the manifest')
        return manifest

    def run(self):
        prep = self.args.output / 'preparation'
        parent = prep / 'example7_parton_only/production'
        if self.args.resume:
            original = self.resume_parent()
            self.report.setdefault('previous_errors', []).append(self.report.pop('error', ''))
            self.report['state'] = 'running'
            self.save()
        else:
            log = self.args.output / 'prepare.log'
            with log.open('w') as stream:
                subprocess.run([sys.executable, str(REPO / 'scripts/test_examples.py'),
                                '--mode', 'condor', '--example', 'example7_parton_only',
                                '--events', str(self.args.events), '--image', self.args.image,
                                '--output', str(prep)], stdout=stream, stderr=subprocess.STDOUT,
                               check=True, timeout=120)
            original = self.execute(parent, 'parent')
        self.wait(parent, 'parent')
        clean_plan = self.command('clean-plan', ['--plan-recovery', parent, '--json'], json_output=True)
        if clean_plan['eligible_job_ids'] or len(original['jobs']) != 2:
            raise ValueError('Expected two valid jobs without recovery candidates')
        first, good = original['jobs']
        mappings = {item['job_id']: item['scheduler_id'] for item in original['submission']['jobs']}

        def archive(job):
            cluster, process = mappings[job['job_id']].split('.')
            return parent / f'output_{job["sample"]}_{cluster}_{process}.tar.gz'

        missing, preserved = archive(first), archive(good)
        good_hash = digest(preserved)
        original_hash = digest(missing)
        backup = self.args.output / 'withheld'
        backup.mkdir()
        withheld = backup / missing.name
        self.report['withheld'] = {'original': str(missing), 'backup': str(withheld), 'restored': False}
        self.save()
        missing.rename(withheld)
        try:
            invalid = self.command('missing-output', ['--validate-results', parent, '--json'],
                                   expected=1, json_output=True)
            if invalid['state'] == 'valid':
                raise ValueError('Missing output was incorrectly accepted')
            plan = self.command('selective-plan', ['--plan-recovery', parent, '--json'], json_output=True)
            if plan['eligible_job_ids'] != [first['job_id']]:
                raise ValueError('Recovery did not select exactly the missing job')
            child = self.args.output / 'retry'
            self.command('prepare-recovery', ['--prepare-recovery', parent, '-o', child,
                                               '--job-id', first['job_id']])
            child_manifest = json.loads((child / 'manifest.json').read_text())
            if child_manifest['jobs'] != [first] or child_manifest['image'] != original['image']:
                raise ValueError('Recovery changed selected job inputs, seed or image')
            if child_manifest.get('submission') or child_manifest['state'] != 'prepared':
                raise ValueError('Recovery preparation unexpectedly executed work')
            reserved = self.command('reserved-plan', ['--plan-recovery', parent, '--json'], json_output=True)
            if reserved['eligible_job_ids']:
                raise ValueError('Parent offers a job already delegated to recovery')
            self.execute(child, 'recovery')
            _, child_validation = self.wait(child, 'recovery')
            if len(child_validation['jobs']) != 1 or digest(preserved) != good_hash:
                raise ValueError('Recovery changed a valid parent output or ran extra jobs')
            self.report['recovered_job_id'] = first['job_id']
            self.report['seed'] = first['seed']
            self.report['preserved_sha256'] = good_hash
        finally:
            if missing.exists():
                raise ValueError(f'Original path was recreated unexpectedly; backup retained at {withheld}')
            withheld.rename(missing)
            self.report['withheld']['restored'] = digest(missing) == original_hash
            self.save()
            if not self.report['withheld']['restored'] or digest(preserved) != good_hash:
                raise ValueError('Parent output checksum changed during the test')
        self.command('restored-parent-valid', ['--validate-results', parent, '--json'], json_output=True)
        self.report['state'] = 'passed'
        self.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true', help='Authorize two original jobs and one recovery job')
    parser.add_argument('--output', type=Path, required=True, help='New isolated test directory')
    parser.add_argument('--resume', action='store_true', help='Continue a failed test before withholding/recovery; never resubmit the parent')
    parser.add_argument('--image', default='mg-pythia-delphes-3.5.6')
    parser.add_argument('--events', type=int, default=100)
    parser.add_argument('--timeout', type=int, default=1800, help='Seconds waiting per production')
    args = parser.parse_args()
    if not args.execute:
        parser.error('This live test requires --execute (up to three Condor jobs)')
    if not 1 <= args.events <= 1000 or args.timeout <= 0:
        parser.error('Use 1..1000 events and a positive timeout')
    for tool in ('condor_submit', 'condor_q', 'condor_history'):
        if not shutil.which(tool):
            parser.error(f'{tool} is unavailable; run in the JUPITER submit environment')
    args.output = args.output.resolve()
    if args.resume:
        if not (args.output / 'report.json').is_file():
            parser.error('--resume requires an existing workflow test report')
    else:
        if args.output.exists():
            parser.error('Choose a new output directory')
        args.output.mkdir(parents=True)
    test = WorkflowTest(args)
    if args.resume:
        try:
            test.resume_parent()
        except (OSError, ValueError, KeyError) as exc:
            parser.error(str(exc))
    else:
        test.save()
    try:
        test.run()
    except (Exception, KeyboardInterrupt) as exc:
        test.report.update(state='failed', error=f'{type(exc).__name__}: {exc}')
        test.save()
    print(f'{test.report["state"]}: {args.output / "report.json"}')
    if test.report.get('error'):
        print(test.report['error'], file=sys.stderr)
    return int(test.report['state'] != 'passed')


if __name__ == '__main__':
    sys.exit(main())
