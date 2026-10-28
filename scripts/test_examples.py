#!/usr/bin/env python3
"""Exercise documented examples; real submission requires --execute and --example."""
import argparse
from collections import Counter
import gzip
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import xml.etree.ElementTree as ET

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


def parse_job_ids(stdout):
    """Parse condor_submit -terse's job-ID ranges, preserving submission order."""
    result = []
    for line in stdout.splitlines():
        match = re.fullmatch(r'\s*(\d+)\.(\d+)(?:\s*-\s*(\d+)\.(\d+))?\s*', line)
        if not match:
            if line.strip():
                raise ValueError(f'Unrecognized submit output; inspect submit.log: {line}')
            continue
        cluster, first = int(match[1]), int(match[2])
        last = int(match[4]) if match[4] is not None else first
        if match[3] is not None and int(match[3]) != cluster or last < first:
            raise ValueError('Invalid job-ID range')
        result.extend(f'{cluster}.{proc}' for proc in range(first, last + 1))
    if not result or len(set(result)) != len(result):
        raise ValueError('Missing or duplicate submitted job IDs; inspect submit.log')
    return result


def parse_lhe_init(block):
    """Read HEPRUP records, excluding optional nested XML metadata (LHEF 3)."""
    try:
        root = ET.fromstring(block)
    except ET.ParseError as exc:
        raise ValueError(f'Malformed LHE init block: {exc}') from exc
    # Only direct text belongs to HEPRUP. Generator/weight descriptions inside
    # child elements are metadata, even when their text looks numeric.
    text = '\n'.join([root.text or ''] + [child.tail or '' for child in root])
    rows = [line.split('#', 1)[0].strip() for line in text.splitlines()]
    rows = [line.split() for line in rows if line]
    if not rows or len(rows[0]) != 10:
        raise ValueError('Malformed LHE init beam record: expected 10 fields')
    try:
        nprocess = int(rows[0][9])
    except ValueError as exc:
        raise ValueError('Malformed LHE init NPRUP: expected an integer') from exc
    if nprocess < 1 or len(rows) != nprocess + 1:
        raise ValueError(f'Incomplete or inconsistent LHE cross-section block: '
                         f'NPRUP={nprocess}, found {len(rows) - 1} process records '
                         '(optional XML metadata excluded)')
    cross_sections = []
    for index, row in enumerate(rows[1:], 1):
        if len(row) != 4:
            raise ValueError(f'Malformed LHE process record {index}: expected 4 fields')
        try:
            values = [float(value.replace('D', 'E').replace('d', 'e')) for value in row[:3]]
            int(row[3])
        except ValueError as exc:
            raise ValueError(f'Malformed numeric LHE process record {index}') from exc
        if not all(math.isfinite(value) for value in values):
            raise ValueError(f'Nonfinite values in LHE process record {index}')
        cross_sections.append(values[0])
    return sum(cross_sections)


def check_lhe(stream, job):
    events, seed, init, in_init = 0, None, [], False
    init_closed = False
    with gzip.GzipFile(fileobj=stream) as source:
        for raw in source:
            line = raw.decode('utf-8', errors='replace').strip()
            events += bool(re.fullmatch(r'<event(?:\s[^>]*)?>', line))
            match = re.match(r'(\d+)\s*=\s*iseed\b', line)
            if match:
                seed = int(match[1])
            if re.match(r'<init(?:\s[^>]*)?>', line):
                if init:
                    raise ValueError('Duplicate LHE init block')
                in_init = True
            if in_init:
                init.append(line)
                if '</init>' in line:
                    in_init = False
                    init_closed = True
    if events != job['nevents']:
        raise ValueError(f"Expected {job['nevents']} LHE events, found {events}")
    if seed != job['seed']:
        raise ValueError(f"LHE seed {seed} differs from planned seed {job['seed']}")
    if not init:
        raise ValueError('LHE init block is missing')
    if not init_closed:
        raise ValueError('LHE init block is missing its closing tag')
    cross_section = parse_lhe_init('\n'.join(init))
    if not math.isfinite(cross_section) or cross_section <= 0:
        raise ValueError('Nonpositive/nonfinite cross section')
    return {'lhe_events': events, 'seed': seed, 'cross_section_pb': cross_section}


def inspect_outputs(files, job):
    """files maps basenames to functions opening binary streams; never unpack tar paths."""
    checks = {}
    suffixes = {'lhe': '_unweighted_events.lhe.gz', 'root': '_delphes_events.root',
                'hepmc': '_pythia8_events.hepmc.gz', 'lhco': '_delphes_events.lhco',
                'log': '_logs.tar'}
    for kind in job['outputs']:
        if kind not in suffixes:
            raise ValueError(f'Output validation not implemented for {kind}')
        matches = [name for name in files if name.endswith(suffixes[kind])]
        if len(matches) != 1:
            raise ValueError(f'Expected one {kind} output, found {matches}')
        with files[matches[0]]() as source:
            if kind == 'lhe':
                checks.update(check_lhe(source, job))
            elif kind == 'root':
                import uproot
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / 'events.root'
                    with path.open('wb') as target:
                        shutil.copyfileobj(source, target)
                    with uproot.open(path) as root:
                        entries = root['Delphes'].num_entries
                        if entries <= 0:
                            raise ValueError('Delphes tree is empty')
                        checks['delphes_events'] = entries
            elif kind == 'hepmc':
                with gzip.GzipFile(fileobj=source) as hepmc:
                    if not hepmc.read(1024):
                        raise ValueError('Empty HepMC output')
            elif kind == 'log':
                with tarfile.open(fileobj=source, mode='r:') as logs:
                    checks['log_files'] = len(logs.getmembers())
            elif not source.read(1):
                raise ValueError(f'Empty {kind} output')
    return checks


def check_condor_job(output, job, scheduler_id):
    cluster, process = scheduler_id.split('.')
    stem = f"{job['sample']}_{cluster}_{process}"
    log = output / f'job_{stem}.log'
    if not re.search(r'Normal termination \(return value 0\)', log.read_text()):
        raise ValueError(f'Job {scheduler_id} did not terminate normally with exit code zero')
    archive_path = output / f'output_{stem}.tar.gz'
    with tarfile.open(archive_path) as archive:
        files = {}
        for member in archive.getmembers():
            if member.isfile():
                name = Path(member.name).name
                if name in files:
                    raise ValueError(f'Duplicate output basename: {name}')
                files[name] = lambda member=member: archive.extractfile(member)
        return inspect_outputs(files, job)


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
        for job in manifest['jobs']:
            run_dir = (production / job['run_file']).parent
            events = run_dir / 'RUN/Events'
            events = events / ('run_01_decayed_1' if (events / 'run_01_decayed_1').is_dir() else 'run_01')
            files = {'local_' + path.name: lambda path=path: path.open('rb')
                     for path in events.iterdir() if path.is_file()}
            # Local mode keeps ordinary logs rather than the Condor logs tarball.
            selected = dict(job, outputs=[kind for kind in job['outputs'] if kind != 'log'])
            result.setdefault('checks', []).append(inspect_outputs(files, selected))
        result['state'] = 'passed'
        return
    submitted = subprocess.run([sys.executable, str(RUNNER), '--execute-prepared', str(production)],
                               cwd=directory, capture_output=True, text=True, timeout=60)
    (work / 'submit.log').write_text(submitted.stdout + '\n' + submitted.stderr)
    submitted.check_returncode()
    ids = parse_job_ids(submitted.stdout)
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
        result.setdefault('checks', []).append(check_condor_job(production, job, scheduler_id))
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
    parser.add_argument('--timeout', type=int, default=1800, help='Condor wait per example / local job timeout, seconds')
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
