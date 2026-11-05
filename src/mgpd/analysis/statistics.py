"""Stable weighted counters, subset efficiencies and one-dimensional bins."""
from bisect import bisect_right
import math


class Sum:
    def __init__(self):
        self.total = 0.0
        self.correction = 0.0

    def add(self, value):
        combined = self.total + value
        if abs(self.total) >= abs(value):
            self.correction += (self.total - combined) + value
        else:
            self.correction += (value - combined) + self.total
        self.total = combined
        if not math.isfinite(self.total) or not math.isfinite(self.correction):
            raise ValueError('weighted accumulation overflow')

    def value(self):
        value = self.total + self.correction
        if not math.isfinite(value):
            raise ValueError('weighted accumulation overflow')
        return value


class Counter:
    def __init__(self):
        self.n = self.positive = self.negative = self.zero = 0
        self.sumw, self.sumw2, self.sumabsw = Sum(), Sum(), Sum()

    def add(self, weight):
        if weight and weight * weight == 0:
            raise ValueError('event weight squared underflows floating-point storage')
        if not math.isfinite(weight) or not math.isfinite(weight * weight):
            raise ValueError('nonfinite event weight or weight squared')
        self.n += 1
        self.positive += weight > 0
        self.negative += weight < 0
        self.zero += weight == 0
        self.sumw.add(weight)
        self.sumw2.add(weight * weight)
        self.sumabsw.add(abs(weight))

    def report(self):
        return {'n': self.n, 'sumw': self.sumw.value(), 'sumw2': self.sumw2.value(),
                'sumabsw': self.sumabsw.value(), 'positive_weights': self.positive,
                'negative_weights': self.negative, 'zero_weights': self.zero}


def efficiency(passed, total, unit_weights=False):
    p, t = passed.report(), total.report()
    count = {'value': None, 'interval': None, 'reason': 'empty denominator', 'method': 'Wilson z=1'}
    if t['n']:
        e, n = p['n'] / t['n'], t['n']
        middle = (e + 1/(2*n)) / (1 + 1/n)
        half = math.sqrt(e * (1-e)/n + 1/(4*n*n)) / (1 + 1/n)
        count.update(value=e, interval=[max(0.0, middle-half), min(1.0, middle+half)], reason=None)
    weighted = {'value': None, 'uncertainty': None, 'reason': None,
                'method': 'independent-event subset delta method',
                'kind': 'signed_weighted_ratio' if t['negative_weights'] else 'weighted_efficiency'}
    denominator = t['sumw']
    if not denominator:
        weighted['reason'] = 'zero weighted denominator'
    elif abs(denominator) <= 1e-12 * t['sumabsw']:
        weighted['reason'] = 'unstable cancellation in weighted denominator'
    else:
        e = p['sumw'] / denominator
        # Subtract compensated sums before rounding to one float. Small rejected
        # contributions must not disappear next to a large accepted weight.
        fail_variance = max(0.0, math.fsum([total.sumw2.total, total.sumw2.correction,
                                          -passed.sumw2.total, -passed.sumw2.correction]))
        fail_weight = math.fsum([total.sumw.total, total.sumw.correction,
                                -passed.sumw.total, -passed.sumw.correction])
        try:
            variance = ((fail_weight/denominator) * (math.sqrt(p['sumw2']) / abs(denominator)))**2 + (e * (math.sqrt(fail_variance) / abs(denominator)))**2
            uncertainty = math.sqrt(variance)
            if not math.isfinite(e) or not math.isfinite(uncertainty):
                raise OverflowError
            weighted.update(value=e, uncertainty=uncertainty)
        except OverflowError:
            weighted['reason'] = 'weighted ratio uncertainty overflow'
    # Count intervals describe counts. They are not intervals for weighted ratios.
    count['unit_weight_sample'] = unit_weights
    return {'counts': count, 'weighted': weighted}


class Histogram:
    def __init__(self, definition):
        self.definition = definition
        bins = definition['bins']
        if isinstance(bins, list):
            self.edges = list(bins)
        else:
            n, low, high = bins['count'], bins['min'], bins['max']
            self.edges = [(1-i/n)*low + (i/n)*high for i in range(n+1)]
        if not all(math.isfinite(x) for x in self.edges) or any(a >= b for a, b in zip(self.edges, self.edges[1:])):
            raise ValueError('histogram edges are not representable as increasing finite numbers')
        self.bins = [Counter() for _ in self.edges[1:]]
        self.underflow, self.overflow = Counter(), Counter()
        self.missing = self.selected_events = 0

    def fill(self, value, weight):
        if value is None or isinstance(value, float) and not math.isfinite(value):
            self.missing += 1
            return
        if type(value) not in (int, float):
            raise ValueError('histogram values must be numeric; use fill: objects for collections')
        if value < self.edges[0]:
            self.underflow.add(weight)
        elif value > self.edges[-1]:
            self.overflow.add(weight)
        else:
            index = min(bisect_right(self.edges, value)-1, len(self.bins)-1)
            self.bins[index].add(weight)

    def report(self):
        result = dict(self.definition)
        result.update(edges=self.edges, counts=[b.n for b in self.bins],
                      sumw=[b.sumw.value() for b in self.bins], sumw2=[b.sumw2.value() for b in self.bins],
                      underflow=self.underflow.report(), overflow=self.overflow.report(),
                      missing_values=self.missing, selected_events=self.selected_events,
                      statistical_uncertainties=[math.sqrt(b.sumw2.value()) for b in self.bins],
                      normalization='raw', entry_kind=self.definition['fill'])
        return result
