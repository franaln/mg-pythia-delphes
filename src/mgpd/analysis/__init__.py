"""Analysis planning and bounded LHE numerical execution API."""
import hashlib
import json
from pathlib import Path

from .._version import __version__
from .config import load_analysis_config
from .inputs import inspect_inputs

DEFAULT_CHUNK_SIZE = 10000


def _checksum(plan):
    body = {key: value for key, value in plan.items() if key != 'plan_sha256'}
    return hashlib.sha256(json.dumps(body, sort_keys=True, allow_nan=False).encode()).hexdigest()


def plan_analysis(inputs, config, *, format=None, sample=None, max_events=None, chunk_size=DEFAULT_CHUNK_SIZE):
    """Validate explicit-file analysis inputs; return a serializable plan, with no writes.

    Multiple files are an explicit assertion of one physical sample. Validation
    checks headers/branches, not every event or the runtime expression shapes.
    """
    if type(chunk_size) is not int or not 1 <= chunk_size <= 1000000:
        raise ValueError('chunk_size: expected an integer from 1 to 1000000')
    if max_events is not None and (type(max_events) is not int or max_events < 1):
        raise ValueError('max_events: expected a positive integer')
    if format not in (None, 'lhe', 'root'):
        raise ValueError('format: expected lhe or root')
    if sample is not None and (not isinstance(sample, str) or not sample.strip()):
        raise ValueError('sample: expected a nonempty label')
    effective, order = load_analysis_config(config)
    selected, fields, files = inspect_inputs(inputs, effective, format)
    effective['input']['format'] = selected
    plan = {'schema_version': 1, 'mgpd_version': __version__, 'state': 'planned',
            'execution_available': selected == 'lhe', 'engine': 'lhe-scalar-v1' if selected == 'lhe' else None, 'config': effective, 'variable_order': order,
            'format': selected, 'sample': sample, 'inputs': files, 'required_fields': fields,
            'chunk_size': chunk_size, 'max_events': max_events, 'preview_requested': max_events is not None,
            'units': {'momentum': 'GeV', 'angle': 'radian'},
            'policies': {'missing_observables': 'mask; never replace by zero',
                         'nonfinite_weights': 'error', 'histogram_edges': '[left,right), final edge inclusive',
                         'flow_bins': 'stored separately', 'normalization': 'raw',
                         'weighted_denominator_cancellation_tolerance': 1e-12},
            'limitations': ['LHE numerical execution only; ROOT execution, CSV and plotting are not implemented.',
                            'Headers/branches validated; event shapes and expression result types are not evaluated.',
                            'External multi-file inputs assert one sample; physical overlap is not inferred.',
                            'Object pairs use highest scalar-pT-sum opposite-sign pair, then original indices. Independent-event errors; correlated eventgroups are rejected.']}
    plan['plan_sha256'] = _checksum(plan)
    return plan


def check_output(output, plan):
    if output is None:
        raise ValueError('Analysis execution requires a new output directory')
    raw = Path(output).expanduser()
    if raw.exists() or raw.is_symlink():
        raise FileExistsError('Analysis output already exists; choose a new directory')
    destination = raw.resolve()
    if not destination.parent.is_dir():
        raise FileNotFoundError('Analysis output parent directory must exist')
    if any(Path(item['path']).is_relative_to(destination) for item in plan['inputs']):
        raise ValueError('Analysis output overlaps an input')
    return destination


def _verify_inputs(plan):
    from ..storage import file_sha256
    for entry in plan['inputs']:
        path = Path(entry['path'])
        if str(path.resolve()) != entry['path'] or not path.is_file() or file_sha256(path) != entry['sha256']:
            raise ValueError(f'Analysis input missing or changed: {path}; plan the analysis again')


def run_analysis(plan, output):
    """Compute raw LHE statistics and publish a new complete numerical result."""
    if not isinstance(plan, dict) or plan.get('plan_sha256') != _checksum(plan):
        raise ValueError('Analysis plan is missing or changed; plan the analysis again')
    destination = check_output(output, plan)
    if plan.get('format') != 'lhe':
        raise ValueError('ROOT event processing is not implemented yet; use --validate-only')
    if not plan.get('execution_available') or plan.get('engine') != 'lhe-scalar-v1':
        raise ValueError('This plan cannot execute with the current LHE engine; plan the analysis again')
    from .dependencies import require_dependencies, RUNTIME_MODULES
    require_dependencies()
    _verify_inputs(plan)
    from .engine import calculate_analysis
    import os
    import tempfile
    from datetime import datetime, timezone
    from importlib.metadata import version
    import yaml
    result = calculate_analysis(plan)
    _verify_inputs(plan)
    warnings = []
    if result['limit_reached']:
        warnings.append('Event limit reached; input exhaustion was not verified and no absolute normalization is available.')
    if any(row['negative_weights'] for row in result['cutflow']):
        warnings.append('Signed weights: weighted ratios are not probabilities or binomial efficiencies.')
    if not result['events_processed']:
        warnings.append('No input events; efficiencies with empty denominators are undefined.')
    report = {'schema_version': 1, 'state': 'completed', 'mgpd_version': __version__,
              'created_at': datetime.now(timezone.utc).isoformat(), 'output': str(destination),
              'plan_sha256': plan['plan_sha256'], 'plan': plan,
              'dependency_versions': {module: version(module) for module in RUNTIME_MODULES},
              'normalization': 'raw', 'units': plan['units'], 'warnings': warnings,
              'statistics_model': 'independent events; subset covariance included',
              **{key: value for key, value in result.items() if key not in ('cutflow', 'histograms')},
              'artifacts': ['analysis.yml', 'analysis-report.json', 'cutflow.json', 'histograms.json']}
    with tempfile.TemporaryDirectory(prefix='.mgpd-analysis-', dir=destination.parent) as temporary:
        staged = Path(temporary) / 'result'
        staged.mkdir()
        (staged / 'analysis.yml').write_text(yaml.safe_dump(plan['config'], sort_keys=False), encoding='utf-8')
        for name, data in [('analysis-report.json', report), ('cutflow.json', result['cutflow']),
                           ('histograms.json', result['histograms'])]:
            (staged / name).write_text(json.dumps(data, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        # Exclusive reservation prevents replacing a directory created since planning.
        destination.mkdir()
        try:
            os.replace(staged, destination)
        except BaseException:
            try:
                destination.rmdir()  # Remove only an empty reservation; preserve intervening user files.
            except OSError:
                pass
            raise
    return report


__all__ = ['plan_analysis', 'run_analysis']
