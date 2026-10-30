"""Validated, sample-wise dataset assembly across registered recovery attempts."""
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import hashlib
from pathlib import Path
import re
import shutil
import tarfile
import tempfile

from .config import resolve_run_mode
from .execution import load_prepared_production
from .storage import execution_lock, file_sha256
from .results import validate_production, result_files, inspect_outputs
from .merging import merge_outputs, _decompress, DOCKER_IMAGE, APPTAINER_IMAGE

SUFFIXES = {'lhe': '_unweighted_events.lhe.gz', 'root': '_delphes_events.root',
            'lhco': '_delphes_events.lhco', 'hepmc': '_pythia8_events.hepmc.gz', 'log': '_logs.tar'}
MERGEABLE = {'lhe', 'root', 'lhco'}


def _verify_child(parent, child, registration):
    before, after = parent.manifest, child.manifest
    link = after.get('recovery', {})
    selected = registration['job_ids']
    if (after['production_id'] != registration['production_id']
            or Path(registration['directory']).resolve() != Path(child.output_dir)
            or link.get('parent_id') != before['production_id']
            or link.get('parent_plan_sha256') != before['plan_sha256']
            or Path(link.get('parent_directory', '')).resolve() != Path(parent.output_dir)
            or len(set(selected)) != len(selected)
            or set(link.get('job_ids', [])) != set(selected)
            or len(after['jobs']) != len(selected)
            or {job['job_id'] for job in after['jobs']} != set(selected)
            or after['image'] != before['image'] or after['mode'] != before['mode']):
        raise ValueError('Recovery identity/path/ownership does not match its parent')
    original = {job['job_id']: job for job in before['jobs']}
    for job in after['jobs']:
        if job != original.get(job['job_id']):
            raise ValueError('Recovery changed a logical job or its seed')
    # Child submit files are new; all other frozen execution inputs are clones.
    for name, digest in after['execution_input_sha256'].items():
        if name == after['execution']['submit_file']:
            continue
        if before['execution_input_sha256'].get(name) != digest:
            raise ValueError('Recovery input differs from its parent')


def _family(directory, stack):
    family, seen_paths, seen_ids = [], set(), set()

    def visit(path, parent=None, registration=None):
        path = Path(path).resolve()
        if path in seen_paths:
            raise ValueError('Repeated/cyclic production path in recovery family')
        seen_paths.add(path)
        stack.enter_context(execution_lock(path))
        production = load_prepared_production(path)
        identity = production.manifest['production_id']
        if identity in seen_ids:
            raise ValueError('Repeated production identity in recovery family')
        seen_ids.add(identity)
        if parent:
            _verify_child(parent, production, registration)
        family.append(production)
        owned = set()
        for entry in production.manifest.get('recoveries', []):
            if not entry.get('blocks_jobs', True):
                continue
            ids = set(entry['job_ids'])
            if owned & ids:
                raise ValueError('Overlapping recovery ownership')
            owned.update(ids)
            visit(entry['directory'], production, entry)
    visit(directory)
    if family[0].manifest.get('recovery'):
        raise ValueError('Merge from the original production directory, not a recovery child')
    return family


def merge_production(directory, output=None, *, samples=None, allow_partial=False,
                     backend=None, image=None, extract_lhe=False, keep_all=False,
                     preview=False, selection_sha256=None, **policy):
    """Select the first validated stopped attempt per job, then merge per sample.

    Traverses only registered, verified descendants in parent-first order. It
    never queries/submits scheduler jobs or changes production manifests.
    """
    policy = {'event_count': 'exact', 'require_positive_cross_section': True, 'check_seed': True, **policy}
    root = Path(directory).resolve()
    destination = Path(output).absolute() if output is not None else root / 'merged'
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f'Output already exists: {destination}')
    if str(destination).endswith('.tar.gz'):
        raise ValueError('Production merging requires an output directory; use explicit archives for tar output')
    if not destination.parent.is_dir():
        raise ValueError('Output parent directory must exist')
    backend = resolve_run_mode(backend or 'local')
    if backend not in ('local-apptainer', 'local-docker'):
        raise ValueError('Merge backend must be local-apptainer or local-docker')
    with ExitStack() as stack:
        family = _family(root, stack)
        manifest = family[0].manifest
        if image is None:
            compatible = (backend == 'local-apptainer' and manifest['image'].endswith('.sif')
                          or backend == 'local-docker' and manifest['mode'] == 'local-docker')
            image = manifest['image'] if compatible else (APPTAINER_IMAGE if backend == 'local-apptainer' else DOCKER_IMAGE)
        jobs = manifest['jobs']
        if len({job['job_id'] for job in jobs}) != len(jobs):
            raise ValueError('Duplicate logical job IDs')
        names = {job['sample'] for job in jobs}
        selected_samples = set(samples) if samples else names
        if not selected_samples or selected_samples - names:
            raise ValueError(f'Unknown or empty sample selection; available: {sorted(names)}')
        if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name) for name in selected_samples):
            raise ValueError('Unsafe sample name')
        targets = [job for job in jobs if job['sample'] in selected_samples]
        attempts = {job['job_id']: [] for job in targets}
        for production in family:
            validation = validate_production(production.output_dir, **policy)
            if validation['metadata_errors']:
                raise ValueError(f'Unreliable production metadata: {production.output_dir}')
            definitions = {job['job_id']: job for job in production.manifest['jobs']}
            for row in validation['jobs']:
                if row['job_id'] not in attempts:
                    continue
                stopped = (row['execution_state'] == 'completed' if production.manifest['mode'] in ('condor', 'jupiter')
                           else production.manifest['state'] in ('execution_finished', 'failed'))
                attempts[row['job_id']].append((production, definitions[row['job_id']], row,
                                                row['state'] == 'valid' and stopped))
        chosen, omitted = [], []
        for job in targets:
            candidates = attempts[job['job_id']]
            valid = next((entry for entry in candidates if entry[3]), None)
            if valid:
                chosen.append(valid)
            else:
                omitted.append({'job_id': job['job_id'], 'sample': job['sample'],
                                'attempts': [{'directory': entry[0].output_dir,
                                              'state': entry[2]['state'],
                                              'execution_state': entry[2]['execution_state'],
                                              'error': entry[2].get('error'),
                                              'outputs': entry[2]['outputs']} for entry in candidates]})
        if omitted and not allow_partial:
            raise ValueError('Missing/invalid or unfinished jobs: ' + ', '.join(row['job_id'] for row in omitted)
                             + '; inspect mgpd validate-results or use --allow-partial explicitly')
        if not chosen:
            raise ValueError('No validated stopped jobs are available for merging')
        report = {'schema_version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
                  'production_id': manifest['production_id'], 'production_directory': str(root),
                  'plan_sha256': manifest['plan_sha256'], 'backend': backend, 'image': image,
                  'policy': policy, 'partial': bool(omitted), 'omitted_jobs': omitted,
                  'selection_rule': 'original valid stopped attempt, then first valid registered descendant (parent-first)',
                  'family': [{'production_id': p.manifest['production_id'], 'directory': p.output_dir,
                              'plan_sha256': p.manifest['plan_sha256']} for p in family], 'samples': {}}
        for sample in selected_samples:
            entries = [entry for entry in chosen if entry[1]['sample'] == sample]
            if entries:
                formats = set(entries[0][1]['outputs']) & MERGEABLE
                if not formats or any(set(entry[1]['outputs']) & MERGEABLE != formats for entry in entries):
                    raise ValueError(f'No consistent mergeable LHE/ROOT/LHCO selection for sample {sample}')
        selection = []
        for production, job, row, _ in chosen:
            files_digest = {}
            with result_files(production.output_dir, production.manifest, job,
                              row['scheduler_id']) as (files, _, _):
                for name, opener in sorted(files.items()):
                    digest = hashlib.sha256()
                    with opener() as source:
                        for block in iter(lambda: source.read(1024 * 1024), b''):
                            digest.update(block)
                    files_digest[name] = digest.hexdigest()
            selection.append({'job_id': job['job_id'], 'sample': job['sample'],
                              'seed': job['seed'], 'production_id': production.manifest['production_id'],
                              'directory': production.output_dir, 'files': files_digest})
        review = {'production_id': manifest['production_id'], 'plan_sha256': manifest['plan_sha256'],
                  'output': str(destination), 'backend': backend, 'image': image, 'policy': policy,
                  'allow_partial': allow_partial, 'extract_lhe': extract_lhe, 'keep_all': keep_all,
                  'samples': sorted(selected_samples), 'selection': selection,
                  'omitted_job_ids': [job['job_id'] for job in omitted], 'family': report['family']}
        digest = hashlib.sha256(json.dumps(review, sort_keys=True).encode()).hexdigest()
        if selection_sha256 is not None and selection_sha256 != digest:
            raise ValueError('Merge selection changed since review; plan the merge again')
        report['selection_sha256'] = digest
        if preview:
            return {**review, 'selection_sha256': digest, 'partial': bool(omitted)}
        reviewed_files = {(entry['production_id'], entry['job_id']): entry['files'] for entry in selection}
        with tempfile.TemporaryDirectory(prefix='.mgpd-dataset-', dir=destination.parent) as temporary:
            work = Path(temporary)
            staged = work / 'result'
            staged.mkdir()
            for sample in sorted(selected_samples):
                entries = [entry for entry in chosen if entry[1]['sample'] == sample]
                if not entries:
                    report['samples'][sample] = {'state': 'omitted', 'jobs': []}
                    continue
                formats = set(entries[0][1]['outputs']) & MERGEABLE
                if not formats or any(set(entry[1]['outputs']) & MERGEABLE != formats for entry in entries):
                    raise ValueError(f'No consistent mergeable LHE/ROOT/LHCO selection for sample {sample}')
                archives, provenance = [], []
                for index, (production, job, row, _) in enumerate(entries):
                    folder = work / 'snapshots' / sample / str(index)
                    folder.mkdir(parents=True)
                    files_report = []
                    with result_files(production.output_dir, production.manifest, job,
                                      row['scheduler_id']) as (files, location, local_logs):
                        copied_kinds = []
                        for kind in job['outputs']:
                            if kind == 'log' and local_logs is not None:
                                continue
                            suffix = SUFFIXES[kind]
                            matches = [name for name in files if name.endswith(suffix)]
                            if len(matches) != 1:
                                raise ValueError(f'Expected exactly one {kind} output for {job["job_id"]}')
                            path = folder / f'job{index}{suffix}'
                            with files[matches[0]]() as source, path.open('wb') as target:
                                shutil.copyfileobj(source, target)
                            copied_kinds.append(kind)
                            if file_sha256(path) != reviewed_files[(production.manifest['production_id'], job['job_id'])][matches[0]]:
                                raise ValueError('Output changed since merge review')
                            files_report.append({'kind': kind, 'source_name': matches[0],
                                                 'sha256': file_sha256(path), 'bytes': path.stat().st_size})
                        # Revalidate the copied snapshot, not just mutable source files.
                        openers = {path.name: (lambda path=path: path.open('rb')) for path in folder.iterdir()}
                        metrics = inspect_outputs(openers, dict(job, outputs=copied_kinds), **policy)
                        if any(row['metrics'].get(key) != value for key, value in metrics.items()):
                            raise ValueError(f'Output metrics changed while copying {job["job_id"]}')
                    archive = folder.with_suffix('.tar.gz')
                    with tarfile.open(archive, 'w:gz') as target:
                        for path in sorted(folder.iterdir()):
                            target.add(path, arcname=path.name)
                    archives.append(archive)
                    provenance.append({'job_id': job['job_id'], 'seed': job['seed'],
                                       'production_id': production.manifest['production_id'],
                                       'directory': production.output_dir, 'scheduler_id': row['scheduler_id'],
                                       'source_location': location, 'metrics': metrics, 'files': files_report})
                merged = work / f'merged-{sample}'
                merge_outputs(archives, merged, backend=backend, image=image, keep_all=keep_all)
                expected_events = sum(item['metrics'].get('lhe_events', 0) for item in provenance)
                openers = {path.name: (lambda path=path: path.open('rb')) for path in merged.iterdir() if path.is_file()}
                merged_metrics = inspect_outputs(openers, {'outputs': sorted(formats), 'nevents': expected_events},
                                                 check_seed=False, event_count='exact',
                                                 require_positive_cross_section=policy.get('require_positive_cross_section', True))
                if 'root' in formats and merged_metrics['delphes_events'] != sum(item['metrics']['delphes_events'] for item in provenance):
                    raise ValueError(f'Merged ROOT event count differs from input totals for {sample}')
                if extract_lhe:
                    for path in merged.rglob('*.lhe.gz'):
                        _decompress(path)
                output_subdir = staged if len(selected_samples) == 1 else staged / sample
                if output_subdir != staged:
                    output_subdir.mkdir()
                for path in merged.iterdir():
                    shutil.move(str(path), str(output_subdir / path.name))
                report['samples'][sample] = {'state': 'merged', 'jobs': provenance,
                                            'formats': sorted(formats), 'metrics': merged_metrics,
                                            'not_merged': sorted(set().union(*(set(entry[1]['outputs']) for entry in entries)) - formats),
                                            'directory': '.' if output_subdir == staged else sample}
            report['outputs'] = [{'path': str(path.relative_to(staged)), 'sha256': file_sha256(path),
                                  'bytes': path.stat().st_size} for path in sorted(staged.rglob('*')) if path.is_file()]
            (staged / 'merge-report.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
            destination.mkdir()  # Reserve without replacing concurrent callers' output.
            try:
                for path in staged.iterdir():
                    shutil.move(str(path), str(destination / path.name))
            except BaseException:
                shutil.rmtree(destination)
                raise
        return str(destination)


def plan_production_merge(directory, output=None, **options):
    """Validate and fingerprint the exact merge selection without invoking engines.

    Acquires the same production locks as merging; no outputs are published.
    """
    return merge_production(directory, output, preview=True, **options)
