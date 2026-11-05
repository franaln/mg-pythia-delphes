"""Resolve one managed merge without scanning job/recovery output trees."""
import hashlib
import json
from pathlib import Path

from ..storage import file_sha256


def file_format(path):
    name = str(path).lower()
    return next((kind for kind, suffixes in {'lhe': ('.lhe', '.lhe.gz'), 'root': ('.root',),
                                             'lhco': ('.lhco', '.lhco.gz')}.items()
                 if name.endswith(suffixes)), None)


def _load(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f'Duplicate merge metadata key: {key}')
            result[key] = value
        return result
    data = path.read_bytes()
    try:
        def invalid(value):
            raise ValueError(f'Nonfinite merge metadata constant: {value}')
        report = json.loads(data, object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, UnicodeError) as exc:
        raise ValueError(f'Invalid managed dataset metadata: {path}: {exc}') from exc
    return report, {'path': str(path.resolve()), 'sha256': hashlib.sha256(data).hexdigest()}


def _inside(root, name, *, must_exist=True):
    if not isinstance(name, str) or Path(name).is_absolute() or '..' in Path(name).parts:
        raise ValueError('Unsafe managed dataset path')
    path = (root / name).resolve(strict=must_exist)
    if not path.is_relative_to(root):
        raise ValueError('Managed dataset path escapes its directory')
    return path


def resolve_dataset(inputs, *, format=None, sample=None):
    """Return explicit paths, resolved label, and frozen optional merge provenance."""
    paths = [Path(value).expanduser().resolve(strict=True) for value in
             ([inputs] if isinstance(inputs, (str, Path)) else inputs)]
    if not any(path.is_dir() for path in paths):
        return paths, sample, None
    if len(paths) != 1 or not paths[0].is_dir():
        raise ValueError('Use one managed dataset directory or explicit files; do not mix them')
    directory = paths[0]
    metadata = []
    selected_subdir = None
    if (directory / 'merge-report.json').is_file():
        report_path = directory / 'merge-report.json'
    elif (directory / 'manifest.json').is_file():
        report_path = directory / 'merged' / 'merge-report.json'
        if not report_path.is_file():
            raise ValueError('Production has no managed merge; run mgpd merge first, then analyze its output directory')
        manifest, entry = _load(directory / 'manifest.json')
        metadata.append(entry)
    elif (directory.parent / 'merge-report.json').is_file():
        report_path = directory.parent / 'merge-report.json'
        selected_subdir = directory
    else:
        raise ValueError('Expected a managed merge-report.json; use explicit files or merge/select production outputs first')
    if report_path.resolve(strict=True).parent != report_path.parent.resolve():
        raise ValueError('Managed merge report escapes its directory')
    report_path = report_path.resolve(strict=True)
    root = report_path.parent
    report, entry = _load(report_path)
    metadata.append(entry)
    try:
        if type(report['schema_version']) is not int or report['schema_version'] != 1 or type(report['partial']) is not bool:
            raise ValueError('Unsupported managed merge schema')
        if report['partial'] or report['omitted_jobs']:
            raise ValueError('Partial/omitted managed merges cannot be analyzed; complete the production first')
        if metadata[0]['path'].endswith('/manifest.json'):
            if (manifest['production_id'] != report['production_id']
                    or manifest['plan_sha256'] != report['plan_sha256']):
                raise ValueError('Default merge does not belong to this production plan')
        samples = report['samples']
        if not isinstance(samples, dict) or not samples:
            raise ValueError('Managed merge has no samples')
        ids = set()
        for name, value in samples.items():
            if value['state'] != 'merged' or not value['jobs']:
                raise ValueError('Managed merge contains an omitted/empty sample')
            for job in value['jobs']:
                identity = job['job_id']
                if not isinstance(identity, str) or not identity or identity in ids:
                    raise ValueError('Repeated or invalid logical job identity in merge provenance')
                ids.add(identity)
                if not isinstance(job['production_id'], str) or not job['production_id']:
                    raise ValueError('Missing attempt identity in merge provenance')
        if selected_subdir is not None:
            matches = [name for name, value in samples.items() if _inside(root, value['directory']) == selected_subdir]
            if len(matches) != 1 or sample is not None and sample != matches[0]:
                raise ValueError('Sample directory conflicts with managed sample selection')
            sample = matches[0]
        if sample is None:
            if len(samples) != 1:
                raise ValueError(f'Multiple managed samples; choose --sample from {sorted(samples)}')
            sample = next(iter(samples))
        if sample not in samples:
            raise ValueError(f'Unknown managed sample {sample}; available: {sorted(samples)}')
        selected = samples[sample]
        sample_dir = _inside(root, selected['directory'])
        if not sample_dir.is_dir():
            raise ValueError('Managed sample directory is not a directory')
        outputs = {}
        names = set()
        for item in report['outputs']:
            name = item['path']
            if name in names:
                raise ValueError('Duplicate managed output inventory path')
            names.add(name)
            path = _inside(root, name, must_exist=False)
            kind = file_format(path)
            # Never select the --keep-all per-job copies below all/.
            if path.parent == sample_dir and kind:
                outputs.setdefault(kind, []).append((path, item))
        if set(selected['formats']) - {'hepmc'} != set(outputs):
            raise ValueError('Managed output inventory differs from declared sample formats')
        if format is None:
            if len(outputs) != 1:
                raise ValueError(f'Multiple managed representations; choose --format from {sorted(outputs)}')
            format = next(iter(outputs))
        if format not in outputs or len(outputs[format]) != 1:
            raise ValueError('Managed sample requires exactly one merged file for the selected format')
        path, inventory = outputs[format][0]
        if (not path.is_file() or path.stat().st_size != inventory['bytes']
                or file_sha256(path) != inventory['sha256']):
            raise ValueError('Managed output checksum/size differs from merge report')
        count_key = {'lhe': 'lhe_events', 'root': 'delphes_events'}.get(format)
        expected = selected['metrics'][count_key] if count_key else selected['metrics'].get('lhe_events', selected['metrics'].get('delphes_events'))
        if expected is not None and (type(expected) is not int or expected < 0):
            raise ValueError('Invalid managed expected event count')
        return [path], sample, {'kind': 'managed_merge', 'directory': str(root),
                               'production_id': report['production_id'], 'production_plan_sha256': report['plan_sha256'],
                               'selection_sha256': report['selection_sha256'], 'sample': sample,
                               'format': format, 'partial': False, 'expected_events': expected,
                               'jobs': selected['jobs'], 'metadata': metadata,
                               'selected_output': {'path': str(path), 'sha256': inventory['sha256']}}
    except (KeyError, TypeError) as exc:
        raise ValueError(f'Incomplete managed merge metadata: {exc}') from exc
