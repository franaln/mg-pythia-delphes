"""Streaming standard 11-column LHCO objects; no inferred event weights.

Header: 0 event-number trigger. Object: index type eta phi pt mass ntrk
btag had/em dummy dummy. Lepton charge is the sign of ntrk when nonzero.
"""
import ast
import gzip
import math
from pathlib import Path

from .expressions import FIELDS
from .readers import Momentum

LHCO_TYPES = {0: 'Photon', 1: 'Electron', 2: 'Muon', 3: 'Tau', 4: 'Jet', 6: 'MissingET'}
KINEMATICS = {'pt', 'eta', 'phi', 'mass', 'px', 'py', 'pz', 'energy'}
CHARGED = {'Electron', 'Muon', 'Tau'}


def required_lhco_fields(config):
    weights = config['input']['weights']
    if weights['mode'] != 'unit' or 'branch' in weights:
        raise ValueError('LHCO has no supported nominal weights; choose input.weights.mode: unit explicitly')
    objects, variables = config['objects'], config['variables']
    origin_cache = {}

    def origins(name, seen=()):
        if name in objects:
            return {name}
        if name not in variables or name in seen:
            return set()
        if name not in origin_cache:
            origin_cache[name] = set().union(*(origins(node.id, (*seen, name)) for node in
                                              ast.walk(ast.parse(variables[name], mode='eval'))
                                              if isinstance(node, ast.Name)))
        return origin_cache[name]

    required = {key: set(KINEMATICS) for key in objects}
    for key, obj in objects.items():
        if obj['source'] not in LHCO_TYPES.values():
            raise ValueError(f'objects.{key}: unsupported LHCO collection {obj["source"]}')
        required[key].add(obj['sort_by'])
        if 'select' in obj:
            required[key].update(node.id for node in ast.walk(ast.parse(obj['select'], mode='eval'))
                                 if isinstance(node, ast.Name) and node.id in FIELDS)
    for expression in [*variables.values(), *(cut['expression'] for cut in config['cuts'])]:
        for node in ast.walk(ast.parse(expression, mode='eval')):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                for key in origins(node.value.id):
                    required[key].add(node.attr)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'leading_os_pair':
                for argument in ast.walk(node.args[0]):
                    if isinstance(argument, ast.Name):
                        for key in origins(argument.id):
                            required[key].add('charge')
    for key, fields in required.items():
        available = KINEMATICS | ({'charge'} if objects[key]['source'] in CHARGED else set())
        if fields - available:
            raise ValueError(f'objects.{key}: unsupported LHCO fields: {sorted(fields - available)}')
    return {**{key: sorted(fields) for key, fields in required.items()}, 'event': []}


def iter_lhco_events(path):
    """Retain empty events; EOF ends the last event (there is no event trailer)."""
    path = Path(path)
    opener = gzip.open if path.name.lower().endswith('.gz') else open
    current = None
    number = line_number = 0
    try:
        with opener(path, 'rt', encoding='utf-8') as stream:
            while True:
                line = stream.readline(65537)
                if not line:
                    break
                line_number += 1
                if len(line) > 65536:
                    raise ValueError('LHCO line exceeds 65536 characters')
                row = line.split('#', 1)[0].split()
                if not row:
                    continue
                index = int(row[0])
                if index == 0:
                    if len(row) != 3 or int(row[1]) < 0 or int(row[2]) < 0:
                        raise ValueError('expected LHCO header: 0 event-number trigger')
                    if current is not None:
                        yield current
                    number += 1
                    current = {'weight': 1., 'source': str(path), 'event_index': number,
                               'lhco_event_number': int(row[1]), 'lhco_trigger': int(row[2]),
                               'collections': {name: [] for name in LHCO_TYPES.values()}, 'object_count': 0}
                    continue
                if current is None:
                    raise ValueError('object before LHCO event header')
                if len(row) != 11:
                    raise ValueError('expected eleven LHCO object fields')
                if index != current['object_count'] + 1:
                    raise ValueError('LHCO object indices must be consecutive from one in each event')
                kind = int(row[1])
                if kind not in LHCO_TYPES:
                    raise ValueError(f'unsupported LHCO object type {kind}')
                values = [float(value.replace('D', 'E').replace('d', 'e')) for value in row[2:]]
                if not all(math.isfinite(value) for value in values):
                    raise ValueError('nonfinite LHCO object field')
                eta, phi, pt, mass, ntrk = values[:5]
                if pt < 0 or mass < 0:
                    raise ValueError('negative LHCO pT or mass')
                source = LHCO_TYPES[kind]
                charge = math.copysign(1., ntrk) if source in CHARGED and ntrk else None
                # Missing ET is a transverse massless proxy, never a neutrino reconstruction.
                if source == 'MissingET':
                    eta, mass = 0., 0.
                px, py, pz = pt * math.cos(phi), pt * math.sin(phi), pt * math.sinh(eta)
                energy = math.hypot(px, py, pz, mass)
                if not all(math.isfinite(value) for value in (px, py, pz, energy)):
                    raise ValueError('nonfinite LHCO four-vector')
                current['collections'][source].append(Momentum(px, py, pz, energy, charge=charge, index=index - 1))
                current['object_count'] += 1
        if current is not None:
            yield current
    except (ValueError, OverflowError, EOFError, OSError, UnicodeError) as exc:
        raise ValueError(f'{path}: event {number or 1}: line {line_number}: {exc}') from exc


def iter_lhco_batches(path, config, chunk_size, max_events=None):
    batch = []
    events = iter_lhco_events(path)
    try:
        for number, event in enumerate(events, 1):
            collections = event.pop('collections')
            event.update({key: collections[obj['source']] for key, obj in config['objects'].items()})
            batch.append(event)
            if len(batch) == chunk_size or number == max_events:
                yield batch
                batch = []
            if number == max_events:
                return
        if batch:
            yield batch
    finally:
        events.close()


def inspect_lhco_header(path):
    events = iter_lhco_events(path)
    try:
        first = next(events, None)
        return {'dialect': 'standard-11-column', 'nominal_weights_available': False,
                'checks': 'first event only; remaining records validated during execution',
                'first_event_number': None if first is None else first['lhco_event_number']}
    finally:
        events.close()
