"""Plan explicit files; managed merge directory discovery is a later stage."""
import ast
import gzip
from pathlib import Path
import xml.etree.ElementTree as ET

from ..storage import file_sha256
from .dependencies import require_dependencies
from .expressions import FIELDS

ROOT_FIELDS = {
    'Electron': {'pt': 'PT', 'eta': 'Eta', 'phi': 'Phi', 'charge': 'Charge'},
    'Muon': {'pt': 'PT', 'eta': 'Eta', 'phi': 'Phi', 'charge': 'Charge'},
    'Photon': {'pt': 'PT', 'eta': 'Eta', 'phi': 'Phi'},
    'Jet': {'pt': 'PT', 'eta': 'Eta', 'phi': 'Phi', 'mass': 'Mass', 'charge': 'Charge'},
    'MissingET': {'pt': 'MET', 'eta': 'Eta', 'phi': 'Phi'},
    'Particle': {'pt': 'PT', 'eta': 'Eta', 'phi': 'Phi', 'mass': 'Mass', 'pdg_id': 'PID',
                 'status': 'Status', 'charge': 'Charge'},
}


def required_fields(config, format):
    if format == 'lhe':
        if 'branch' in config['input']['weights']:
            raise ValueError('input.weights.branch is a ROOT-only option')
        if any(obj['source'] != 'particles' for obj in config['objects'].values()):
            raise ValueError('LHE objects must use source: particles')
        return {'particles': sorted(FIELDS), 'event': ['nominal_weight'] if config['input']['weights']['mode'] == 'nominal' else []}
    required = {}
    for key, obj in config['objects'].items():
        source = obj['source']
        if source not in ROOT_FIELDS:
            raise ValueError(f'objects.{key}.source: ROOT requires a supported Delphes collection')
        mapping = ROOT_FIELDS[source]
        fields = {'pt', 'eta', 'phi', obj['sort_by']}
        expressions = list(config['variables'].values()) + [cut['expression'] for cut in config['cuts']]
        for expression in expressions:
            for node in ast.walk(ast.parse(expression, mode='eval')):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == key:
                    fields.add(node.attr)
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == 'leading_os_pair' and isinstance(node.args[0], ast.Name)
                        and node.args[0].id == key):
                    fields.add('charge')
        if source == 'Jet':
            fields.add('mass')
        if 'select' in obj:
            fields.update(node.id for node in ast.walk(ast.parse(obj['select'], mode='eval'))
                          if isinstance(node, ast.Name) and node.id in FIELDS)
        if source in ('Electron', 'Muon'):
            fields.add('charge')
        unsupported = fields - set(mapping)
        if unsupported:
            raise ValueError(f'objects.{key}: unsupported fields for {source}: {sorted(unsupported)}')
        required[key] = sorted(f'{source}.{mapping[field]}' for field in fields)
    weight = config['input']['weights']
    required['event'] = [weight.get('branch', 'Event.Weight')] if weight['mode'] == 'nominal' else []
    return required


def _lhe_header(path):
    opener = gzip.open if path.name.lower().endswith('.gz') else open
    try:
        with opener(path, 'rb') as stream:
            root_checked = False
            in_init = False
            for event, node in ET.iterparse(stream, events=('start', 'end')):
                tag = node.tag.rsplit('}', 1)[-1]
                if not root_checked:
                    if event != 'start' or tag != 'LesHouchesEvents':
                        raise ValueError(f'{path}: expected LesHouchesEvents root element')
                    root_checked = True
                if event == 'start' and tag == 'init':
                    in_init = True
                if event == 'start' and tag == 'event':
                    raise ValueError(f'{path}: LHE event occurs before init')
                if event == 'end' and tag == 'init':
                    records = [line.strip() for line in (node.text or '').splitlines()
                               if line.strip() and not line.lstrip().startswith('#')]
                    if not records or len(records[0].split('#')[0].split()) != 10:
                        raise ValueError(f'{path}: invalid LHE init beam record')
                    beam = records[0].split('#')[0].split()
                    from ..results import parse_lhe_init
                    sigma = parse_lhe_init(ET.tostring(node, encoding='unicode'))
                    return {'weight_strategy': int(beam[8]), 'subprocesses': int(beam[9]),
                            'cross_section_pb': sigma, 'checks': 'init header only; events are not parsed'}
                if event == 'end' and not in_init:
                    node.clear()
    except (ET.ParseError, EOFError, UnicodeError) as exc:
        raise ValueError(f'{path}: malformed LHE header') from exc
    raise ValueError(f'{path}: missing LHE init block')


def _root_header(path, config, fields):
    require_dependencies(('uproot',))
    import uproot
    with uproot.open(path) as root:
        tree_name = config['input']['tree']
        if tree_name not in root:
            raise ValueError(f'{path}: missing Delphes tree')
        tree = root[tree_name]
        if not hasattr(tree, 'num_entries'):
            raise ValueError(f'{path}: Delphes is not a tree')
        missing = set(branch for values in fields.values() for branch in values) - set(tree.keys())
        if missing:
            raise ValueError(f'{path}: missing branches: {sorted(missing)}')
        return {'tree': tree_name, 'entries': int(tree.num_entries),
                'checks': 'tree and branch names only; event shapes are checked during reading'}


def inspect_inputs(inputs, config, format=None):
    if isinstance(inputs, (str, Path)):
        inputs = [inputs]
    paths = [Path(value).expanduser().resolve(strict=True) for value in inputs]
    if not paths:
        raise ValueError('Analysis requires at least one input file')
    if len(paths) != len(set(paths)):
        raise ValueError('Duplicate analysis input path (including symlinks)')
    kinds = set()
    for path in paths:
        if not path.is_file():
            raise ValueError(f'{path}: explicit files are required; merge/select production outputs first')
        lower = path.name.lower()
        kind = 'lhe' if lower.endswith(('.lhe', '.lhe.gz')) else 'root' if lower.endswith('.root') else None
        if kind is None:
            raise ValueError(f'{path}: unsupported file extension; expected .lhe, .lhe.gz or .root')
        kinds.add(kind)
    if len(kinds) != 1:
        raise ValueError('Analyze one representation at a time; mixed LHE/ROOT input is not supported')
    inferred = next(iter(kinds))
    for selected in (format, config['input'].get('format')):
        if selected is not None and selected != inferred:
            raise ValueError('Input format conflicts with file extensions or analysis configuration')
    fields = required_fields(config, inferred)
    files = []
    for path in sorted(paths):
        before = path.stat()
        digest = file_sha256(path)
        metadata = _lhe_header(path) if inferred == 'lhe' else _root_header(path, config, fields)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise ValueError(f'Input changed during planning: {path}')
        files.append({'path': str(path), 'bytes': after.st_size, 'sha256': digest, 'metadata': metadata})
    return inferred, fields, files
