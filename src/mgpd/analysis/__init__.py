"""Analysis planning and bounded LHE/Delphes numerical execution API."""
from pathlib import Path
import time
import copy
from datetime import datetime, timezone
from importlib.metadata import version

from .._version import __version__
from .config import load_analysis_config
from .inputs import inspect_inputs
from .integrity import checksum as _checksum
from . import engine
from .dependencies import require_dependencies, RUNTIME_MODULES
from .reporting import publish_results
from ..templates import _resource
from .saved import load_saved_results
from .reporting import publish_results

DEFAULT_CHUNK_SIZE = 10000


def plan_analysis(inputs, config, *, format=None, sample=None, max_events=None, chunk_size=DEFAULT_CHUNK_SIZE):
    """Validate explicit-file analysis inputs; return a serializable plan, with no writes.

    Multiple files are an explicit assertion of one physical sample. Validation
    checks headers/branches, not every event or the runtime expression shapes.
    """
    if type(chunk_size) is not int or not 1 <= chunk_size <= 1000000:
        raise ValueError('chunk_size: expected an integer from 1 to 1000000')
    if max_events is not None and (type(max_events) is not int or max_events < 1):
        raise ValueError('max_events: expected a positive integer')
    if format not in (None, 'lhe', 'root', 'lhco'):
        raise ValueError('format: expected lhe, root or lhco')
    if sample is not None and (not isinstance(sample, str) or not sample.strip()):
        raise ValueError('sample: expected a nonempty label')
    effective, order = load_analysis_config(config)
    selected, fields, files = inspect_inputs(inputs, effective, format)
    effective['input']['format'] = selected
    plan = {'schema_version': 1, 'mgpd_version': __version__, 'state': 'planned',
            'execution_available': True, 'engine': f'{selected}-scalar-v1', 'config': effective, 'variable_order': order,
            'format': selected, 'sample': sample, 'inputs': files, 'required_fields': fields,
            'chunk_size': chunk_size, 'max_events': max_events, 'preview_requested': max_events is not None,
            'units': {'momentum': 'GeV', 'angle': 'radian'},
            'policies': {'missing_observables': 'mask; never replace by zero',
                         'nonfinite_weights': 'error', 'histogram_edges': '[left,right), final edge inclusive',
                         'flow_bins': 'stored separately', 'normalization': 'raw',
                         'weighted_denominator_cancellation_tolerance': 1e-12},
            'limitations': ['Physical normalization is not implemented. ROOT uses split Delphes branches; electron/muon masses are fixed to PDG values, photons/MissingET are massless.',
                            'Headers/branches validated; event shapes and expression result types are not evaluated.',
                            'External multi-file inputs assert one sample; physical overlap is not inferred.',
                            'Object pairs use highest scalar-pT-sum opposite-sign pair, then original indices. Independent-event errors; correlated eventgroups are rejected.']}
    if selected == 'lhco':
        plan['limitations'].append('LHCO supports standard 11-column records and explicit unit weights only; lepton charge is sign(ntrk), zero is missing. No PID/status or jet charge. MET is transverse and massless. Native converter validation pending.')
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


def run_analysis(plan, output, *, plots=False):
    """Compute raw event statistics and publish a new complete numerical result."""

    if not isinstance(plan, dict) or plan.get('plan_sha256') != _checksum(plan):
        raise ValueError('Analysis plan is missing or changed; plan the analysis again')

    if type(plots) is not bool:
        raise ValueError('plots must be a boolean')
    destination = check_output(output, plan)

    if not plan.get('execution_available') or plan.get('engine') != f"{plan.get('format')}-scalar-v1":
        raise ValueError('This plan cannot execute with the current engine; plan the analysis again')
    require_dependencies()

    _verify_inputs(plan)

    started = time.perf_counter()
    result = engine.calculate_analysis(plan)
    compute_seconds = time.perf_counter()-started

    _verify_inputs(plan)

    warnings = []
    if result['limit_reached']:
        warnings.append('Event limit reached; input exhaustion was not verified and no absolute normalization is available.')
    if any(row['negative_weights'] for row in result['cutflow']):
        warnings.append('Signed weights: weighted ratios are not probabilities or binomial efficiencies.')
    if not result['events_processed']:
        warnings.append('No input events; efficiencies with empty denominators are undefined.')

    report = {
        'schema_version': 1,
        'state': 'completed',
        'mgpd_version': __version__,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'output': str(destination),
        'plan_sha256': plan['plan_sha256'],
        'plan': plan,
        'dependency_versions': {module: version(module) for module in RUNTIME_MODULES},
        'normalization': 'raw',
        'units': plan['units'],
        'warnings': warnings,
        'statistics_model': 'independent events; subset covariance included',
        **{key: value for key, value in result.items() if key not in ('cutflow', 'histograms')},
        'compute_seconds': compute_seconds
    }

    return publish_results(report, plan['config'], result['cutflow'], result['histograms'], destination, plots=plots)


def analysis_example(name='lhe-dileptons'):
    """Return a packaged editable starter, with no writes or optional imports."""

    if name not in ('lhe-dileptons', 'delphes-dileptons', 'lhco-dileptons'):
        raise ValueError('Unknown analysis example; choose lhe-dileptons, delphes-dileptons or lhco-dileptons')
    return _resource(f'analysis/{name}.yml').read_text(encoding='utf-8')


def render_analysis(source, output):
    """Regenerate CSV and plots from saved numbers, never opening original events."""


    source_path = Path(source).expanduser().resolve(strict=True)
    report, config, cutflow, histograms, provenance = load_saved_results(source_path)
    destination = check_output(output, {'inputs': [{'path': str(source_path / name)} for name in
                                                  ('analysis-report.json', 'analysis.yml', 'cutflow.json', 'histograms.json')]})
    if destination.is_relative_to(source_path):
        raise ValueError('Rendering output must be outside the saved analysis directory')

    require_dependencies(('matplotlib',))

    report = copy.deepcopy(report)
    report['output'] = str(destination)
    report['rendered_at'] = datetime.now(timezone.utc).isoformat()
    report['rendered_from'] = provenance

    if not provenance['original_artifact_hashes_verified']:
        report.setdefault('warnings', []).append('Source result has no artifact hashes; integrity before rendering cannot be established.')

    return publish_results(report, config, cutflow, histograms, destination, plots=True)


__all__ = ['plan_analysis', 'run_analysis', 'render_analysis', 'analysis_example']
