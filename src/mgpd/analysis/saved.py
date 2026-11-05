"""Load numerical artifacts for rendering without opening simulation files."""
import hashlib
import json
from pathlib import Path

from .config import validate_analysis_config
from .reporting import validate_numerical


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'Duplicate JSON key: {key}')
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError(f'Invalid JSON constant: {value}')


def _json(content):
    try:
        return json.loads(content, object_pairs_hook=_pairs,
                          parse_constant=_invalid_constant)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f'Invalid saved analysis JSON: {exc}') from exc


def load_saved_results(directory):
    directory = Path(directory).expanduser().resolve(strict=True)
    if not directory.is_dir():
        raise ValueError('Saved analysis source must be a directory')
    names = ('analysis-report.json', 'analysis.yml', 'cutflow.json', 'histograms.json')
    payloads, hashes = {}, {}
    for name in names:
        path = directory / name
        if not path.resolve().is_relative_to(directory) or not path.is_file():
            raise ValueError(f'Saved analysis artifact missing or outside its directory: {name}')
        payloads[name] = path.read_bytes()
        hashes[name] = hashlib.sha256(payloads[name]).hexdigest()
    report = _json(payloads['analysis-report.json'])
    if (not isinstance(report, dict) or type(report.get('schema_version')) is not int or report.get('schema_version') != 1 or report.get('state') != 'completed'
            or not isinstance(report.get('plan'), dict)):
        raise ValueError('Saved analysis is not a completed supported result')
    if not isinstance(report.get('warnings', []), list) or any(not isinstance(x, str) for x in report.get('warnings', [])):
        raise ValueError('Invalid saved analysis warnings')
    from . import _checksum
    plan = report['plan']
    if plan.get('plan_sha256') != _checksum(plan) or report.get('plan_sha256') != plan['plan_sha256']:
        raise ValueError('Saved analysis plan checksum mismatch')
    if plan.get('format') not in ('lhe', 'root') or report.get('normalization') != 'raw':
        raise ValueError('Unsupported saved analysis format or normalization')
    metadata = report.get('artifact_metadata')
    if metadata is not None:
        if not isinstance(metadata, list) or any(not isinstance(entry, dict) or not isinstance(entry.get('path'), str) for entry in metadata):
            raise ValueError('Invalid saved artifact metadata')
        index = {entry['path']: entry for entry in metadata}
        if len(index) != len(metadata):
            raise ValueError('Duplicate saved artifact metadata')
        for name in names[1:]:
            entry = index.get(name, {})
            if entry.get('sha256') != hashes[name] or entry.get('bytes') != len(payloads[name]):
                raise ValueError(f'Saved artifact checksum/size mismatch: {name}')
    # Load the exact YAML bytes already hashed, not a second read of a mutable file.
    from .config import _Loader
    import yaml
    try:
        config, _ = validate_analysis_config(yaml.load(payloads['analysis.yml'], Loader=_Loader))
    except yaml.YAMLError as exc:
        raise ValueError(f'Invalid saved analysis YAML: {exc}') from exc
    if config != plan.get('config'):
        raise ValueError('Saved YAML differs from the frozen analysis plan')
    cutflow, histograms = _json(payloads['cutflow.json']), _json(payloads['histograms.json'])
    validate_numerical(config, cutflow, histograms)
    if type(report.get('events_processed')) is not int or report['events_processed'] != cutflow[0]['n']:
        raise ValueError('Saved event count differs from cutflow')
    provenance = {'directory': str(directory), 'artifacts': [{'path': name, 'sha256': hashes[name]} for name in names],
                  'original_artifact_hashes_verified': metadata is not None}
    return report, config, cutflow, histograms, provenance
