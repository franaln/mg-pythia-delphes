#!/usr/bin/env python3
"""Exercise documented examples; real submission requires --execute and --example."""
import argparse
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import time

import yaml

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / 'examples'
RUNNER = REPO / 'scripts/run_mg_pythia_delphes.py'


def catalog():
    return json.loads((EXAMPLES / 'catalog.json').read_text())['examples']


def check_prepared(output, entry):
    """Check the documented sample contract, concrete seeds, and packaged inputs."""
    manifest = json.loads((output / 'manifest.json').read_text())
    jobs = manifest['jobs']
    expected = Counter({sample: entry['jobs_per_sample'] for sample in entry['samples']})
    if Counter(job['sample'] for job in jobs) != expected:
        raise ValueError('Prepared samples/job counts differ from the example catalog')
    seeds = [job['seed'] for job in jobs]
    if len(set(seeds)) != len(jobs) or not all(1 <= x <= 30080 for x in seeds):
        raise ValueError('Seeds must be distinct and within the supported range')
    for job in jobs:
        path = output / job['run_file']
        text = path.read_text()
        values = re.findall(r'^set iseed = (\d+)$', text, re.MULTILINE)
        if values != [str(job['seed'])]:
            raise ValueError(f"Seed mismatch for {job['job_id']}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != manifest['prepared_input_sha256'][job['run_file']]:
            raise ValueError(f'Input checksum mismatch: {path}')
    wrappers = list(output.rglob('run_mg_pythia_delphes.sh'))
    for wrapper in wrappers:
        subprocess.run(['bash', '-n', str(wrapper)], check=True, capture_output=True, text=True)
    if manifest['mode'] in ('condor', 'jupiter'):
        submission = next(output.glob('*.sub')).read_text()
        if submission.count('queue 1\n') != len(jobs):
            raise ValueError('Submission job count differs from manifest')
        for sample in entry['samples']:
            with tarfile.open(output / f'run_{sample}.tar.gz') as archive:
                if not any(Path(member.name).name == 'run.mg5' for member in archive.getmembers()):
                    raise ValueError('Sample archive is missing run.mg5')
    return manifest


# Load the shared runner whether this file is executed or imported by tests.
_runner_spec = importlib.util.spec_from_file_location('_example_workflow_runner', RUNNER)
_runner = importlib.util.module_from_spec(_runner_spec)
sys.modules[_runner_spec.name] = _runner
_runner_spec.loader.exec_module(_runner)
parse_lhe_init = _runner.parse_lhe_init
check_lhe = _runner.check_lhe
inspect_outputs = _runner.inspect_outputs
check_condor_job = _runner.check_condor_job


def run_command(command, cwd, log, timeout):
    with log.open('w') as destination:
        subprocess.run(command, cwd=cwd, stdout=destination, stderr=subprocess.STDOUT,
                       check=True, timeout=timeout)


def run_example(entry, args, result):
    directory = EXAMPLES / Path(entry['config']).parent
    config = yaml.safe_load((EXAMPLES / entry['config']).read_text())
    config['run']['nevents'] = args.events
    image = args.image or config['run']['image']
    if args.mode == 'local-docker' and image.startswith('mg-pythia-delphes-'):
        image = 'franaln/mg-pythia-delphes:' + image.removeprefix('mg-pythia-delphes-')
    elif args.mode == 'local-apptainer' and image.startswith('mg-pythia-delphes-'):
        image = f'/opt/images/{image}.sif'
    config['run']['image'] = image
    work = args.output / entry['name']
    work.mkdir()
    config_path = work / 'test_config.yml'
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    production = work / 'production'
    command = [sys.executable, str(RUNNER), '-c', str(config_path), '-o', str(production),
               '--run_mode', args.mode]
    run_command(command + ['--validate-only'], directory, work / 'validate.log', 60)
    run_command(command + ['--dry-run'], directory, work / 'prepare.log', 120)
    manifest = check_prepared(production, entry)
    result.update(state='prepared', jobs=len(manifest['jobs']), production=str(production))
    if not args.execute:
        return
    if len(manifest['jobs']) > args.max_jobs:
        raise ValueError('Job count exceeds --max-jobs; nothing submitted')
    if args.mode != 'condor':
        run_command([sys.executable, str(RUNNER), '--execute-prepared', str(production)],
                    directory, work / 'execute.log', args.timeout)
        result['validation'] = _runner.validate_production(production)
        if result['validation']['state'] != 'valid':
            raise ValueError('Production output validation failed; inspect report.json')
        result['checks'] = [job['metrics'] for job in result['validation']['jobs']]
        result['state'] = 'passed'
        return
    submitted = subprocess.run([sys.executable, str(RUNNER), '--execute-prepared', str(production)],
                               cwd=directory, capture_output=True, text=True, timeout=60)
    (work / 'submit.log').write_text(submitted.stdout + '\n' + submitted.stderr)
    submitted.check_returncode()
    saved_submission = json.loads((production / 'manifest.json').read_text())['submission']
    ids = [job['scheduler_id'] for job in saved_submission['jobs']]
    result['scheduler_ids'] = ids
    # Persist IDs immediately so interrupted tests can still be inspected/removed.
    (work / 'submitted_jobs.json').write_text(json.dumps(ids, indent=2) + '\n')
    if len(ids) != len(manifest['jobs']):
        raise ValueError('Submitted job count differs from manifest')
    deadline = time.monotonic() + args.timeout
    for job, scheduler_id in zip(manifest['jobs'], ids):
        cluster, process = scheduler_id.split('.')
        log = production / f"job_{job['sample']}_{cluster}_{process}.log"
        # Submission events may take a moment to reach the shared filesystem.
        while not log.exists() and time.monotonic() < deadline:
            time.sleep(1)
        remaining = max(1, int(deadline - time.monotonic()))
        run_command(['condor_wait', '-wait', str(remaining), str(log), scheduler_id],
                    production, work / f'wait_{cluster}_{process}.log', remaining + 5)
        archive = production / f"output_{job['sample']}_{cluster}_{process}.tar.gz"
        while not archive.exists() and time.monotonic() < deadline:
            time.sleep(1)
        if _runner.result_execution_state(production, manifest, job, scheduler_id) != 'completed':
            raise ValueError(f'Job {scheduler_id} did not terminate normally with exit code zero')
    result['validation'] = _runner.validate_production(production)
    if result['validation']['state'] != 'valid':
        raise ValueError('Production output validation failed; inspect report.json')
    result['checks'] = [job['metrics'] for job in result['validation']['jobs']]
    result['state'] = 'passed'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--example', action='append', help='Catalog name; repeat to select several')
    parser.add_argument('--mode', choices=['condor', 'local-docker', 'local-apptainer'], default='condor')
    parser.add_argument('--output', type=Path, help='New directory for reports, logs and productions')
    parser.add_argument('--execute', action='store_true', help='Actually run/submit the explicitly selected examples')
    parser.add_argument('--image', help='Override container image (Condor accepts runner-supported image names)')
    parser.add_argument('--events', type=int, default=100, help='Events per job for tests (default: 100)')
    parser.add_argument('--max-jobs', type=int, default=8, help='Maximum jobs per executed example')
    parser.add_argument('--timeout', type=int, default=1800, help='Wait/execution timeout per example, seconds')
    parser.add_argument('--remove-on-failure', action='store_true', help='Remove only recorded test job IDs on failure')
    args = parser.parse_args()
    entries = catalog()
    if args.list:
        for entry in entries:
            print(f"{entry['name']}: {entry['config']}" + (' [SM smoke]' if entry['smoke'] else ''))
        return 0
    if not args.output:
        parser.error('--output is required')
    if args.execute and not args.example:
        parser.error('--execute requires explicit --example selection')
    if min(args.events, args.timeout, args.max_jobs) <= 0:
        parser.error('--events, --timeout and --max-jobs must be positive')
    if args.example:
        unknown = set(args.example) - {entry['name'] for entry in entries}
        if unknown:
            parser.error(f'Unknown examples: {sorted(unknown)}')
        entries = [entry for entry in entries if entry['name'] in args.example]
    if args.execute:
        required = ['condor_submit', 'condor_wait'] if args.mode == 'condor' else [args.mode.removeprefix('local-')]
        if args.remove_on_failure and args.mode == 'condor':
            required.append('condor_rm')
        for executable in required:
            if not shutil.which(executable):
                parser.error(f'{executable} is not available')
        for entry in entries:
            config = yaml.safe_load((EXAMPLES / entry['config']).read_text())
            if 'root' in config['run'].get('outputs', []):
                if importlib.util.find_spec('uproot') is None:
                    parser.error('Live ROOT checks require uproot in the host Python environment')
    args.output = args.output.resolve()
    if args.output.exists():
        parser.error('--output must be a new directory; existing results are never overwritten')
    args.output.mkdir(parents=True)
    report = {'mode': args.mode, 'execute': args.execute, 'results': []}
    try:
        for entry in entries:
            result = {'example': entry['name']}
            report['results'].append(result)
            print(f"Testing {entry['name']} ...", flush=True)
            try:
                run_example(entry, args, result)
            except (Exception, KeyboardInterrupt) as exc:
                result.update(state='failed', error=f'{type(exc).__name__}: {exc}')
                if args.remove_on_failure and result.get('scheduler_ids'):
                    cleanup = subprocess.run(['condor_rm', *result['scheduler_ids']], capture_output=True, text=True, timeout=60)
                    result['cleanup'] = {'returncode': cleanup.returncode, 'stdout': cleanup.stdout, 'stderr': cleanup.stderr}
                if isinstance(exc, KeyboardInterrupt):
                    raise
            finally:
                (args.output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
            print(f"  {result['state']}" + (f": {result['error']}" if 'error' in result else ''), flush=True)
    except KeyboardInterrupt:
        return 130
    return int(any(result['state'] == 'failed' for result in report['results']))


if __name__ == '__main__':
    sys.exit(main())
