#!/usr/bin/env python3
"""Live merge smoke test of a completed single-sample production (no submissions)."""
import argparse
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile

try:
    import mgpd
except ModuleNotFoundError as exc:
    if exc.name != 'mgpd':
        raise
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    import mgpd
from mgpd.results import inspect_outputs, result_files


def lhco_events(path):
    count = 0
    with path.open() as stream:
        for line in stream:
            fields = line.split()
            if len(fields) >= 3 and fields[0] == '0' and fields[1].isdigit():
                count += 1
    if not count:
        raise ValueError(f'No LHCO event headers in {path}')
    return count


def lhe_weights(path):
    """Record finite event-weight statistics without assuming a weight convention."""
    total, absolute, events = 0.0, 0.0, 0
    event_header = False
    with gzip.open(path, 'rt') as stream:
        for line in stream:
            line = line.strip()
            if re.fullmatch(r'<event(?:\s[^>]*)?>', line):
                event_header = True
            elif event_header and line and not line.startswith('#'):
                weight = float(line.split()[2].replace('D', 'E').replace('d', 'e'))
                if not math.isfinite(weight):
                    raise ValueError('Nonfinite LHE event weight')
                total += weight
                absolute += abs(weight)
                events += 1
                event_header = False
    if not events:
        raise ValueError('No LHE event weights')
    return {'events': events, 'sum_weights': total, 'sum_abs_weights': absolute,
            'mean_weight': total / events}


def normalization_check(inputs, merged):
    """Replica smoke check: merging must not sum independent cross sections."""
    sections = [item['cross_section_pb'] for item in inputs]
    expected = sum(item['cross_section_pb'] * item['lhe_events'] for item in inputs) / sum(
        item['lhe_events'] for item in inputs)
    tolerance = max(abs(expected) * 1e-4, 1e-12)
    observed = merged['cross_section_pb']
    if not min(sections) - tolerance <= observed <= max(sections) + tolerance:
        raise ValueError(f'Merged cross section {observed} pb is outside replica range '
                         f'[{min(sections)}, {max(sections)}] pb; possible normalization error')
    return {'input_cross_sections_pb': sections, 'event_weighted_reference_pb': expected,
            'merged_cross_section_pb': observed,
            'check': 'within input replica range (rounding tolerance 1e-4 of reference)',
            'scope': 'Same-sample positive unweighted smoke production; not a general physics validation'}


def run_test(args, report):
    production = args.production.resolve()
    validation = mgpd.validate_production(production)
    report['validation'] = validation
    if validation['state'] != 'valid':
        raise ValueError('Input production is not valid; inspect report validation')
    manifest = mgpd.read_production_manifest(production)
    jobs = manifest['jobs']
    if len(jobs) != 2 or len({job['sample'] for job in jobs}) != 1:
        raise ValueError('This smoke test requires exactly two jobs from one sample')
    required = {'lhe', 'root', 'lhco'}
    if any(not required.issubset(job['outputs']) for job in jobs):
        raise ValueError('Use example11_merge_smoke: LHE, ROOT and LHCO are required')
    ids = {job['job_id']: job.get('scheduler_id') for job in manifest.get('submission', {}).get('jobs', [])}
    inputs, metrics, lhco_counts = [], [], []
    snapshots = args.output / 'inputs'
    snapshots.mkdir()
    # Copy selected files through the shared read-only artifact reader. Original
    # production files and manifest are never modified; works for local/Condor.
    for index, job in enumerate(jobs):
        folder = snapshots / str(index)
        folder.mkdir()
        with result_files(production, manifest, job, ids.get(job['job_id'])) as (files, _, _):
            for kind, suffix in [('lhe', '_unweighted_events.lhe.gz'),
                                 ('root', '_delphes_events.root'), ('lhco', '_delphes_events.lhco')]:
                matches = [name for name in files if name.endswith(suffix)]
                if len(matches) != 1:
                    raise ValueError(f'Expected one {kind} file for {job["job_id"]}')
                with files[matches[0]]() as source, (folder / f'job{index}{suffix}').open('wb') as target:
                    shutil.copyfileobj(source, target)
        openers = {path.name: (lambda path=path: path.open('rb')) for path in folder.iterdir()}
        metric = inspect_outputs(openers, dict(job, outputs=sorted(required)))
        metric['weights'] = lhe_weights(next(folder.glob('*.lhe.gz')))
        lhco_counts.append(lhco_events(next(folder.glob('*.lhco'))))
        if lhco_counts[-1] != metric['delphes_events']:
            raise ValueError('Input LHCO and ROOT event counts differ')
        metrics.append(metric)
        archive = snapshots / f'job{index}.tar.gz'
        with tarfile.open(archive, 'w:gz') as target:
            for path in sorted(folder.iterdir()):
                target.add(path, arcname=path.name)
        inputs.append(archive)
    report['inputs'] = [{'job_id': job['job_id'], 'seed': job['seed'], 'metrics': metric,
                         'snapshot_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                        for job, metric, path in zip(jobs, metrics, inputs)]
    expected_lhe = sum(item['lhe_events'] for item in metrics)
    expected_root = sum(item['delphes_events'] for item in metrics)
    report['expected'] = {'lhe_events': expected_lhe, 'delphes_events': expected_root,
                          'lhco_events': sum(lhco_counts)}

    def merge(paths, output, label):
        command = [sys.executable, '-m', 'mgpd', 'merge', *map(str, paths), '-o', str(output),
                   '--backend', args.backend, '--image', args.image]
        report.setdefault('commands', []).append(command)
        with (args.output / f'{label}.log').open('w') as log:
            subprocess.run(command, check=True, stdout=log, stderr=subprocess.STDOUT, timeout=args.timeout)

    merged = args.output / 'merged'
    merge(inputs, merged, 'merge')
    openers = {path.name: (lambda path=path: path.open('rb')) for path in merged.iterdir()}
    result = inspect_outputs(openers, {'outputs': sorted(required), 'nevents': expected_lhe}, check_seed=False)
    report['merged'] = result
    report['normalization'] = normalization_check(metrics, result)
    result['weights'] = lhe_weights(merged / 'merged_unweighted_events.lhe.gz')
    result['lhco_events'] = lhco_events(merged / 'merged_delphes_events.lhco')
    if result['delphes_events'] != expected_root or result['lhco_events'] != sum(lhco_counts):
        raise ValueError('Merged ROOT/LHCO event totals differ from the input totals')
    # Exercise the separate LHCO-only path through lhco2root -> root2lhco.
    only = []
    for index in range(2):
        archive = snapshots / f'lhco{index}.tar.gz'
        path = next((snapshots / str(index)).glob('*.lhco'))
        with tarfile.open(archive, 'w:gz') as target:
            target.add(path, arcname=path.name)
        only.append(archive)
    merge(only, args.output / 'lhco-only', 'merge-lhco-only')
    count = lhco_events(args.output / 'lhco-only/merged_delphes_events.lhco')
    report['lhco_only_events'] = count
    if count != sum(lhco_counts):
        raise ValueError('LHCO-only conversion changed the event total')
    report['state'] = 'passed'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--production', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='New test directory')
    parser.add_argument('--backend', choices=['local-apptainer', 'local-docker'], default='local-apptainer')
    parser.add_argument('--image', default='/mnt/R5/images/mg-pythia-delphes-3.5.6.sif')
    parser.add_argument('--timeout', type=int, default=600, help='Seconds per merge invocation')
    args = parser.parse_args()
    args.output = args.output.absolute()
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    if args.output.exists():
        parser.error('Output already exists; choose a new test directory')
    # Require an installed package because child commands use python -m mgpd.
    args.output.mkdir(parents=True)
    report = {'state': 'failed', 'package_version': mgpd.__version__,
              'production': str(args.production.resolve()), 'backend': args.backend, 'image': args.image}
    try:
        run_test(args, report)
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
    (args.output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(f'{report["state"]}: {args.output / "report.json"}')
    if 'error' in report:
        print(report['error'], file=sys.stderr)
    return int(report['state'] != 'passed')


if __name__ == '__main__':
    sys.exit(main())
