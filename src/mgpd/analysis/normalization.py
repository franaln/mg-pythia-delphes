"""Explicit external normalization, retaining raw statistics unchanged."""
import math


def normalization_plan(config, format, files):
    policy = config['input']['normalization']
    mode = policy['mode']
    if mode == 'raw':
        return {'mode': 'raw', 'factor': 1., 'unit': 'raw'}
    weights = config['input']['weights']
    if format == 'lhe':
        strategies = {entry['metadata']['weight_strategy'] for entry in files}
        supported = {3} if weights['mode'] == 'unit' else {3, -3}
        if len(strategies) != 1 or not strategies <= supported:
            raise ValueError('Physical LHE normalization supports IDWTUP=3 (or -3 with nominal weights) only; other strategies remain raw')
    factor = policy['cross_section_pb'] / policy['sumw_generated']
    if mode == 'luminosity':
        factor *= policy['luminosity_pb_inverse']
    if not math.isfinite(factor) or not factor or not math.isfinite(factor * factor) or not factor * factor:
        raise ValueError('Normalization factor or squared factor overflows/underflows')
    return {'mode': mode, 'factor': factor, 'unit': 'pb' if mode == 'cross_section' else 'events',
            'cross_section_pb': policy['cross_section_pb'], 'sumw_generated': policy['sumw_generated'],
            'luminosity_pb_inverse': policy.get('luminosity_pb_inverse'),
            'weight_convention': policy['weight_convention'], 'provenance': 'explicit analysis configuration',
            'denominator': 'external full generated sample before analysis cuts, not inferred from analyzed events',
            'uncertainties': 'fixed external sigma/luminosity/denominator; scaled independent-event sumw2 only'}


def scaled(sumw, sumw2, policy):
    factor = policy['factor']
    value, variance = sumw * factor, sumw2 * factor * factor
    if (not math.isfinite(value) or not math.isfinite(variance)
            or sumw and not value or sumw2 and not variance):
        raise ValueError('Normalized value/variance overflows or underflows')
    return {'value': value, 'variance': variance, 'uncertainty': math.sqrt(variance), 'unit': policy['unit']}


def apply_normalization(cutflow, histograms, policy):
    if policy['mode'] == 'raw':
        return
    for row in cutflow:
        row['normalized'] = scaled(row['sumw'], row['sumw2'], policy)
    for histogram in histograms.values():
        bins = [scaled(value, variance, policy) for value, variance in zip(histogram['sumw'], histogram['sumw2'])]
        histogram['normalized'] = {'mode': policy['mode'], 'unit': policy['unit'],
                                   'values': [item['value'] for item in bins],
                                   'variances': [item['variance'] for item in bins],
                                   'uncertainties': [item['uncertainty'] for item in bins],
                                   **{key: scaled(histogram[key]['sumw'], histogram[key]['sumw2'], policy)
                                      for key in ('underflow', 'overflow')}}


def validate_normalized(config, cutflow, histograms):
    """Recompute normalized fields from raw sums before publishing or rendering."""
    policy = normalization_plan(config, 'root', [])  # File strategies were checked in the frozen plan.
    for row in cutflow:
        expected = None if policy['mode'] == 'raw' else scaled(row['sumw'], row['sumw2'], policy)
        if row.get('normalized') != expected:
            raise ValueError('Normalized cutflow differs from raw statistics/configuration')
    for histogram in histograms.values():
        if policy['mode'] == 'raw':
            if 'normalized' in histogram:
                raise ValueError('Unexpected normalized histogram for raw analysis')
        else:
            shadow = dict(histogram)
            apply_normalization([], {'histogram': shadow}, policy)
            if histogram.get('normalized') != shadow['normalized']:
                raise ValueError('Normalized histogram differs from raw statistics/configuration')
