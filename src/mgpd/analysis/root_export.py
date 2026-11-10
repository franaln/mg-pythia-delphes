"""TH1D export of saved bins; optional imports load at the export boundary."""
import json
import math

import numpy as np
import uproot
from uproot.writing.identify import to_TAxis, to_TH1x

from .._version import __version__


def _histogram(histogram, normalized=False):
    edges = np.asarray(histogram['edges'], dtype=np.float64)
    if normalized:
        data = histogram['normalized']
        values, variances = data['values'], data['variances']
        under, over = data['underflow'], data['overflow']
        flows = [under['value'], over['value']]
        flow_variances = [under['variance'], over['variance']]
        unit = data['unit']
    else:
        values, variances = histogram['sumw'], histogram['sumw2']
        under, over = histogram['underflow'], histogram['overflow']
        flows = [under['sumw'], over['sumw']]
        flow_variances = [under['sumw2'], over['sumw2']]
        unit = 'raw sumw'
    contents = np.asarray([flows[0], *values, flows[1]], dtype=np.float64)
    errors = np.asarray([flow_variances[0], *variances, flow_variances[1]], dtype=np.float64)
    centers = [float(a)/2+float(b)/2 for a, b in zip(edges[:-1], edges[1:])]
    # Event coordinates are not stored in numerical results. These moments are
    # explicitly binned statistics, not reconstructed unbinned measurements.
    try:
        statistics = [math.fsum(values), math.fsum(variances),
                      math.fsum(w*x for w,x in zip(values, centers)),
                      math.fsum(w*x*x for w,x in zip(values, centers))]
    except OverflowError as exc:
        raise ValueError('ROOT bin-center statistics exceed finite float64 range') from exc
    if not all(math.isfinite(x) for x in statistics):
        raise ValueError('ROOT bin-center statistics exceed finite float64 range')
    entries = sum(histogram['counts']) + histogram['underflow']['n'] + histogram['overflow']['n']
    return to_TH1x(fName=None, fTitle=histogram['name'], data=contents,
                   fEntries=float(entries), fTsumw=statistics[0], fTsumw2=statistics[1],
                   fTsumwx=statistics[2], fTsumwx2=statistics[3], fSumw2=errors,
                   fXaxis=to_TAxis('xaxis', histogram.get('label', histogram['variable']),
                                   len(edges)-1, float(edges[0]), float(edges[-1]), fXbins=edges),
                   fYaxis=to_TAxis('yaxis', unit+' / bin', 1, 0., 1.), fStatOverflows=0)


def write_root_histograms(report, histograms, path, numerical_sha256):
    """Write raw and optional normalized TH1D objects, including explicit Sumw2."""
    metadata = {'schema_version': 1, 'mgpd_version': __version__, 'plan_sha256': report['plan_sha256'],
                'histograms_json_sha256': numerical_sha256,
                'normalization': report.get('normalization_metadata', {'mode': 'raw'}),
                'statistics': 'bin-center moments; exclude flows; original unbinned moments unavailable',
                'entries': 'number of finite fills including flows, excluding missing values',
                'edges': 'stored analysis bins; final edge inclusive during analysis; ROOT refill semantics differ',
                'errors': 'stored per-fill sumw2; object fills exclude additional within-event correlations',
                'histograms': {name: {key: h[key] for key in ('variable', 'stage', 'fill', 'selected_events',
                                  'missing_values', 'counts')} for name,h in histograms.items()}}
    with uproot.create(path) as root:
        for name, histogram in histograms.items():
            root['raw/'+name] = _histogram(histogram)
            if 'normalized' in histogram:
                root['normalized/'+name] = _histogram(histogram, normalized=True)
        root['metadata'] = json.dumps(metadata, sort_keys=True, allow_nan=False)
