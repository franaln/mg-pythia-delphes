"""Analysis planning API. Event processing is implemented in the next milestone."""
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
            'execution_available': False, 'config': effective, 'variable_order': order,
            'format': selected, 'sample': sample, 'inputs': files, 'required_fields': fields,
            'chunk_size': chunk_size, 'max_events': max_events, 'preview_requested': max_events is not None,
            'units': {'momentum': 'GeV', 'angle': 'radian'},
            'policies': {'missing_observables': 'mask; never replace by zero',
                         'nonfinite_weights': 'error', 'histogram_edges': '[left,right), final edge inclusive',
                         'flow_bins': 'stored separately', 'normalization': 'raw'},
            'limitations': ['Event processing is not implemented in this milestone.',
                            'Headers/branches validated; event shapes and expression result types are not evaluated.',
                            'External multi-file inputs assert one sample; physical overlap is not inferred.',
                            'Object pairs will use highest scalar-pT-sum opposite-sign pair, then original indices.']}
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


def run_analysis(plan, output):
    """Execution boundary reserved for stage 2; never creates partial results."""
    if not isinstance(plan, dict) or plan.get('plan_sha256') != _checksum(plan):
        raise ValueError('Analysis plan is missing or changed; plan the analysis again')
    check_output(output, plan)
    from .dependencies import require_dependencies
    require_dependencies()
    raise ValueError('Analysis event processing is not implemented yet; use analyze --validate-only to inspect the plan')


__all__ = ['plan_analysis', 'run_analysis']
