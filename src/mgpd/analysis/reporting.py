"""Portable numerical reports and headless, linear-scale plots from saved data."""
import csv
import math
import os
from pathlib import Path
import tempfile
import json
import time
import yaml

from ..storage import file_sha256
from importlib.metadata import version
from .._version import __version__
from .normalization import validate_normalized

COUNTERS = ('n', 'sumw', 'sumw2', 'sumabsw', 'positive_weights', 'negative_weights', 'zero_weights')


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _counter(row):
    if not isinstance(row, dict) or any(key not in row for key in COUNTERS):
        raise ValueError('incomplete weighted counter')
    for key in ('n', 'positive_weights', 'negative_weights', 'zero_weights'):
        if type(row[key]) is not int or row[key] < 0:
            raise ValueError('counter counts must be nonnegative integers')
    if sum(row[key] for key in ('positive_weights', 'negative_weights', 'zero_weights')) != row['n']:
        raise ValueError('counter sign counts do not sum to N')
    if not all(_number(row[key]) for key in ('sumw', 'sumw2', 'sumabsw')) or row['sumw2'] < 0 or row['sumabsw'] < 0:
        raise ValueError('invalid weighted counter sums')


def validate_numerical(config, cutflow, histograms):
    """Check serialized shapes and semantics before rendering external results."""
    try:
        expected = ['initial'] + [cut['name'] for cut in config['cuts']]
        if not isinstance(cutflow, list) or [row['stage'] for row in cutflow] != expected:
            raise ValueError('cutflow stages differ from the analysis configuration')
        stages = {}
        previous = None
        for row in cutflow:
            _counter(row)
            if previous is not None and row['n'] > previous:
                raise ValueError('cutflow counts are not cumulative')
            previous = row['n']
            stages[row['stage']] = row['n']
            for prefix in ('relative', 'cumulative'):
                for kind in ('counts', 'weighted'):
                    ratio = row[prefix][kind]
                    if ratio['value'] is not None and not _number(ratio['value']):
                        raise ValueError('invalid efficiency value')
                    if not isinstance(ratio['method'], str) or ratio.get('reason') is not None and not isinstance(ratio['reason'], str):
                        raise ValueError('invalid efficiency metadata')
                    if kind == 'counts':
                        interval = ratio['interval']
                        if interval is not None and (not isinstance(interval, list) or len(interval) != 2
                                                     or not all(_number(x) and 0 <= x <= 1 for x in interval)
                                                     or interval[0] > interval[1]):
                            raise ValueError('invalid count efficiency interval')
                        if ratio['value'] is not None and not 0 <= ratio['value'] <= 1:
                            raise ValueError('invalid count efficiency')
                    else:
                        error = ratio['uncertainty']
                        if error is not None and (not _number(error) or error < 0):
                            raise ValueError('invalid weighted ratio uncertainty')
                        if ratio['kind'] not in ('weighted_efficiency', 'signed_weighted_ratio'):
                            raise ValueError('invalid weighted ratio kind')
        if not isinstance(histograms, dict) or set(histograms) != {h['name'] for h in config['histograms']}:
            raise ValueError('histograms differ from the analysis configuration')
        for definition in config['histograms']:
            histogram = histograms[definition['name']]
            if any(histogram.get(key) != value for key, value in definition.items()):
                raise ValueError('histogram definition differs from configuration')
            edges = histogram['edges']
            if (not isinstance(edges, list) or len(edges) < 2 or not all(_number(x) for x in edges)
                    or any(a >= b for a, b in zip(edges, edges[1:]))):
                raise ValueError('histogram edges must be finite and increasing')
            bins = definition['bins']
            if isinstance(bins, list):
                if edges != bins:
                    raise ValueError('histogram edges differ from configured edges')
            elif len(edges) != bins['count'] + 1 or edges[0] != bins['min'] or edges[-1] != bins['max']:
                raise ValueError('histogram bin count/range differs from configuration')
            for key in ('counts', 'sumw', 'sumw2'):
                values = histogram[key]
                if not isinstance(values, list) or len(values) != len(edges)-1 or not all(_number(x) for x in values):
                    raise ValueError('invalid histogram array length or values')
            if any(type(x) is not int or x < 0 for x in histogram['counts']) or any(x < 0 for x in histogram['sumw2']):
                raise ValueError('histogram counts/variances must be nonnegative')
            errors = histogram.get('statistical_uncertainties')
            if errors is not None and (not isinstance(errors, list) or len(errors) != len(edges)-1
                                       or any(not _number(error) or error < 0 or not math.isclose(error, math.sqrt(variance), rel_tol=1e-12)
                                              for error, variance in zip(errors, histogram['sumw2']))):
                raise ValueError('histogram uncertainties differ from stored variances')
            for key in ('underflow', 'overflow'):
                _counter(histogram[key])
            if histogram['normalization'] != 'raw' or histogram['entry_kind'] != definition['fill']:
                raise ValueError('unsupported histogram normalization/entry kind')
            if type(histogram['selected_events']) is not int or histogram['selected_events'] != stages[definition['stage']]:
                raise ValueError('histogram stage event count differs from cutflow')
            if type(histogram['missing_values']) is not int or histogram['missing_values'] < 0:
                raise ValueError('invalid missing-value count')
            if definition['fill'] == 'events':
                count = sum(histogram['counts']) + histogram['underflow']['n'] + histogram['overflow']['n'] + histogram['missing_values']
                if count != histogram['selected_events']:
                    raise ValueError('histogram entries do not account for all selected events')
        validate_normalized(config, cutflow, histograms)
    except (KeyError, TypeError, OverflowError) as exc:
        raise ValueError(f'Invalid serialized analysis results: {exc}') from exc


def write_cutflow_csv(cutflow, path):
    normalized = any('normalized' in row for row in cutflow)
    columns = ['stage', *COUNTERS, 'missing_cut_values']
    if normalized:
        columns += ['normalized_value', 'normalized_variance', 'normalized_uncertainty', 'normalized_unit']
    suffixes = ('count_value', 'count_interval_low', 'count_interval_high', 'count_reason',
                'count_method', 'weighted_value', 'weighted_uncertainty', 'weighted_kind',
                'weighted_reason', 'weighted_method')
    columns += [f'{prefix}_{key}' for prefix in ('relative', 'cumulative') for key in suffixes]
    with Path(path).open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for row in cutflow:
            output = {key: row.get(key) for key in ['stage', *COUNTERS, 'missing_cut_values']}
            if normalized:
                output.update({f'normalized_{key}': value for key, value in row['normalized'].items()})
            for prefix in ('relative', 'cumulative'):
                counts, weighted = row[prefix]['counts'], row[prefix]['weighted']
                interval = counts['interval'] or [None, None]
                fields = {'count_value': counts['value'], 'count_interval_low': interval[0], 'count_interval_high': interval[1],
                          'count_reason': counts.get('reason'), 'count_method': counts['method'],
                          'weighted_value': weighted['value'], 'weighted_uncertainty': weighted['uncertainty'],
                          'weighted_kind': weighted['kind'], 'weighted_reason': weighted.get('reason'),
                          'weighted_method': weighted['method']}
                output.update({f'{prefix}_{key}': value for key, value in fields.items()})
            writer.writerow(output)


def histogram_plot_data(histogram):
    """Pure numerical transformation; never reread events or clip negative bins."""
    edges = list(histogram['edges'])
    normalized = histogram.get('normalized')
    return {'edges': edges, 'centers': [a/2+b/2 for a, b in zip(edges, edges[1:])],
            'values': list(normalized['values'] if normalized else histogram['sumw']),
            'errors': list(normalized['uncertainties']) if normalized else [math.sqrt(x) for x in histogram['sumw2']]}


def render_plots(report, cutflow, histograms, directory):
    """Load the optional graphical renderer only at the plotting boundary."""

    from .plotting import render_plots as render
    return render(report, cutflow, histograms, directory)


def publish_results(report, config, cutflow, histograms, destination, *, plots=False, export_root=False):
    """Render/stage all artifacts and exclusively publish one complete directory."""

    validate_numerical(config, cutflow, histograms)
    with tempfile.TemporaryDirectory(prefix='.mgpd-analysis-', dir=destination.parent) as temporary:
        staged = Path(temporary) / 'result'
        staged.mkdir()
        (staged / 'analysis.yml').write_text(yaml.safe_dump(config, sort_keys=False), encoding='utf-8')
        for name, data in [('cutflow.json', cutflow), ('histograms.json', histograms)]:
            (staged / name).write_text(json.dumps(data, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        write_cutflow_csv(cutflow, staged / 'cutflow.csv')
        report['root_export'] = {'enabled': False}
        if export_root:
            from .root_export import write_root_histograms
            write_root_histograms(report, histograms, staged / 'histograms.root', file_sha256(staged / 'histograms.json'))
            report['root_export'] = {'enabled': True, 'mgpd_version': __version__, 'uproot_version': version('uproot'),
                                     'format': 'TH1D', 'raw_directory': 'raw', 'normalized_directory': 'normalized',
                                     'statistics': 'bin-center moments; exclude flows'}
        report['rendering'] = {'enabled': False, 'formats': []}
        if plots:
            started = time.perf_counter()
            try:
                render_plots(report, cutflow, histograms, staged / 'plots')
            except (RuntimeError, ValueError, TypeError) as exc:
                raise ValueError(f'Could not render analysis plots: {exc}') from exc
            report['rendering'] = {'enabled': True, 'mgpd_version': __version__, 'matplotlib_version': version('matplotlib'),
                                   'backend': 'Agg', 'scale': 'linear', 'formats': ['png', 'pdf'],
                                   'seconds': time.perf_counter()-started, 'flow_bins': 'annotated, not folded',
                                   'error_model': 'sqrt(sumw2) per fill; within-event object correlations are not included'}
        files = sorted(path for path in staged.rglob('*') if path.is_file())
        report['artifact_metadata'] = [{'path': str(path.relative_to(staged)), 'bytes': path.stat().st_size,
                                       'sha256': file_sha256(path)} for path in files]
        report['artifacts'] = [entry['path'] for entry in report['artifact_metadata']] + ['analysis-report.json']
        (staged / 'analysis-report.json').write_text(json.dumps(report, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        destination.mkdir()
        try:
            os.replace(staged, destination)
        except BaseException:
            try:
                destination.rmdir()
            except OSError:
                pass
            raise
    return report
