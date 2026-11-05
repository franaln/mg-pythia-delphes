"""Delphes split-branch adapter; optional dependencies load only on use."""
import math

from .inputs import ROOT_FIELDS
from .readers import Momentum


def iter_root_batches(path, config, required, chunk_size, max_events=None):
    import awkward as ak
    import uproot

    branches = sorted({branch for values in required.values() for branch in values})
    weight_config = config['input']['weights']
    unit = weight_config['mode'] == 'unit'
    weight_branch = weight_config.get('branch', 'Event.Weight')
    number = 0
    with uproot.open(path) as root:
        tree = root[config['input']['tree']]
        stop = min(tree.num_entries, max_events) if max_events is not None else tree.num_entries
        for start in range(0, stop, chunk_size):
            size = min(chunk_size, stop - start)
            arrays = tree.arrays(expressions=branches, entry_start=start, entry_stop=start + size,
                                 library='ak', how=dict)
            columns = {key: ak.to_list(value) for key, value in arrays.items()}
            if any(len(value) != size for value in columns.values()):
                raise ValueError(f'{path}: inconsistent event branch lengths')
            batch = []
            for entry in range(size):
                number += 1
                try:
                    weight = 1.0 if unit else columns[weight_branch][entry]
                    if isinstance(weight, list):
                        if len(weight) != 1:
                            raise ValueError('nominal weight branch must contain exactly one value per event')
                        weight = weight[0]
                    if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not math.isfinite(weight):
                        raise ValueError('event weight must be a finite number')
                    event = {'weight': float(weight), 'source': str(path), 'event_index': number}
                    for key, obj in config['objects'].items():
                        source = obj['source']
                        mapping = ROOT_FIELDS[source]
                        values = {field: columns[f'{source}.{branch}'][entry]
                                  for field, branch in mapping.items()
                                  if f'{source}.{branch}' in required[key]}
                        if any(not isinstance(value, list) for value in values.values()):
                            raise ValueError(f'{source}: expected jagged object branches')
                        lengths = {len(value) for value in values.values()}
                        if len(lengths) != 1:
                            raise ValueError(f'{source}: inconsistent object branch lengths')
                        particles = []
                        for index in range(next(iter(lengths))):
                            fields = {field: value[index] for field, value in values.items()}
                            if any(isinstance(value, bool) or not isinstance(value, (int, float))
                                   or not math.isfinite(value) for value in fields.values()):
                                raise ValueError(f'{source}: nonfinite or nonnumeric object field')
                            pt, eta, phi = (fields[name] for name in ('pt', 'eta', 'phi'))
                            mass = fields.get('mass', {'Electron': 0.00051099895, 'Muon': 0.1056583755}.get(source, 0.0))
                            if pt < 0 or mass < 0:
                                raise ValueError(f'{source}: negative pT or mass')
                            px, py, pz = pt * math.cos(phi), pt * math.sin(phi), pt * math.sinh(eta)
                            energy = math.hypot(px, py, pz, mass)
                            if not all(math.isfinite(value) for value in (px, py, pz, energy)):
                                raise ValueError(f'{source}: nonfinite four-vector')
                            particles.append(Momentum(px, py, pz, energy, pdg_id=fields.get('pdg_id'),
                                                      status=fields.get('status'), charge=fields.get('charge'), index=index))
                        event[key] = particles
                    batch.append(event)
                except (ValueError, OverflowError, TypeError) as exc:
                    raise ValueError(f'{path}: event {number}: {exc}') from exc
            yield batch
