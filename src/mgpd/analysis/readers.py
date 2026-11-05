"""Bounded LHE reading and explicit four-vector/particle records."""
from dataclasses import dataclass
import gzip
import math
from pathlib import Path
import xml.etree.ElementTree as ET


@dataclass(frozen=True)
class Momentum:
    px: float
    py: float
    pz: float
    energy: float
    pdg_id: int = None
    status: int = None
    charge: float = None
    index: int = -1
    mothers: tuple = ()       # LHE mother indices are retained verbatim (one-based).

    @property
    def pt(self):
        return math.hypot(self.px, self.py)

    @property
    def phi(self):
        return math.atan2(self.py, self.px) if self.pt else None

    @property
    def eta(self):
        return math.asinh(self.pz / self.pt) if self.pt else None

    @property
    def mass(self):
        # Scale before squaring to avoid intermediate overflow.
        scale = max(abs(self.energy), abs(self.px), abs(self.py), abs(self.pz))
        if not scale:
            return 0.0
        squared = (self.energy / scale)**2 - sum((p / scale)**2 for p in (self.px, self.py, self.pz))
        return scale * math.sqrt(max(0.0, squared)) if squared >= -1e-12 else None


def _charge(pdg_id):
    # Unknown/BSM charges remain unknown. Never infer them from the PDG sign.
    charges = {1: -1/3, 2: 2/3, 3: -1/3, 4: 2/3, 5: -1/3, 6: 2/3,
               11: -1, 12: 0, 13: -1, 14: 0, 15: -1, 16: 0,
               21: 0, 22: 0, 23: 0, 24: 1, 25: 0,
               111: 0, 211: 1, 130: 0, 310: 0, 311: 0, 321: 1, 2112: 0, 2212: 1}
    charge = charges.get(abs(pdg_id))
    return None if charge is None else charge * (1 if pdg_id > 0 else -1)


def _float(value):
    number = float(value.replace('D', 'E').replace('d', 'e'))
    if not math.isfinite(number):
        raise ValueError('nonfinite numeric field')
    return number


def _event(node, path, number):
    text = (node.text or '') + ''.join(child.tail or '' for child in node)
    rows = [line.split('#', 1)[0].split() for line in text.splitlines()]
    rows = [row for row in rows if row]
    try:
        if not rows or len(rows[0]) != 6:
            raise ValueError('expected six event-header fields')
        nup, process = int(rows[0][0]), int(rows[0][1])
        weight = _float(rows[0][2])
        for value in rows[0][3:]:
            _float(value)
        if nup < 0 or len(rows) != nup + 1:
            raise ValueError('particle count differs from NUP')
        particles = []
        for index, row in enumerate(rows[1:]):
            if len(row) != 13:
                raise ValueError('expected thirteen particle fields')
            integers = [int(value) for value in row[:6]]
            values = [_float(value) for value in row[6:]]
            particles.append(Momentum(*values[:4], pdg_id=integers[0], status=integers[1],
                                      charge=_charge(integers[0]), index=index,
                                      mothers=tuple(integers[2:4])))
        return {'particles': particles, 'weight': weight, 'process_id': process,
                'source': str(path), 'event_index': number}
    except (ValueError, OverflowError) as exc:
        raise ValueError(f'{path}: event {number}: {exc}') from exc


def iter_lhe_batches(path, chunk_size, max_events=None):
    """Yield batches; remove completed XML subtrees to keep memory bounded.

    Only nominal XWGTUP is used. Optional reweight/scale XML is ignored, while
    malformed XML or incomplete event records fail instead of skipping events.
    """
    path = Path(path)
    opener = gzip.open if path.name.lower().endswith('.gz') else open
    batch, stack, count = [], [], 0
    try:
        with opener(path, 'rb') as stream:
            for action, node in ET.iterparse(stream, events=('start', 'end')):
                tag = node.tag.rsplit('}', 1)[-1]
                if action == 'start':
                    if tag == 'eventgroup':
                        raise ValueError(f'{path}: correlated LHE eventgroups are not supported')
                    if not stack and tag != 'LesHouchesEvents':
                        raise ValueError('expected LesHouchesEvents root element')
                    stack.append(node)
                    continue
                is_event = tag == 'event'
                if is_event:
                    if len(stack) < 2 or stack[-2].tag.rsplit('}', 1)[-1] not in ('LesHouchesEvents', 'eventgroup'):
                        raise ValueError('unsupported nested event element')
                    count += 1
                    batch.append(_event(node, path, count))
                stack.pop()
                if is_event or not any(parent.tag.rsplit('}', 1)[-1] == 'event' for parent in stack):
                    if stack:
                        stack[-1].remove(node)
                    node.clear()
                if len(batch) == chunk_size or max_events is not None and count == max_events:
                    yield batch
                    batch = []
                    if max_events is not None and count == max_events:
                        return
        if batch:
            yield batch
    except (ET.ParseError, EOFError) as exc:
        raise ValueError(f'{path}: malformed/truncated LHE XML near event {count + 1}') from exc
