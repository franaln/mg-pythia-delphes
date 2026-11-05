"""Portable numerical reports and headless, linear-scale plots from saved data."""
import csv
import math
import os
from pathlib import Path
import tempfile
import json

from ..storage import file_sha256

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
    except (KeyError, TypeError, OverflowError) as exc:
        raise ValueError(f'Invalid serialized analysis results: {exc}') from exc


def write_cutflow_csv(cutflow, path):
    columns = ['stage', *COUNTERS, 'missing_cut_values']
    suffixes = ('count_value', 'count_interval_low', 'count_interval_high', 'count_reason',
                'count_method', 'weighted_value', 'weighted_uncertainty', 'weighted_kind',
                'weighted_reason', 'weighted_method')
    columns += [f'{prefix}_{key}' for prefix in ('relative', 'cumulative') for key in suffixes]
    with Path(path).open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for row in cutflow:
            output = {key: row.get(key) for key in ['stage', *COUNTERS, 'missing_cut_values']}
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
    return {'edges': edges, 'centers': [a/2+b/2 for a, b in zip(edges, edges[1:])],
            'values': list(histogram['sumw']), 'errors': [math.sqrt(x) for x in histogram['sumw2']]}


def render_plots(report, cutflow, histograms, directory):
    from matplotlib import rc_context
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    directory = Path(directory)
    directory.mkdir()
    sample = report['plan'].get('sample') or 'Sample'
    prefix = 'PREVIEW | ' if report.get('preview_requested') else ''
    title = f"{prefix}{sample} | {report['plan']['format'].upper()}"
    with rc_context({'text.usetex': False, 'font.family': 'DejaVu Sans'}):
        for name, histogram in histograms.items():
            figure = Figure(figsize=(7, 5), layout='constrained')
            FigureCanvasAgg(figure)
            try:
                axis = figure.subplots()
                data = histogram_plot_data(histogram)
                axis.stairs(data['values'], data['edges'], color='C0')
                axis.errorbar(data['centers'], data['values'], yerr=data['errors'], fmt='o', markersize=3,
                              color='C0', capsize=2, label='Raw sumw; errors sqrt(sumw2)')
                axis.axhline(0, linewidth=.7, color='0.5')
                axis.set_xlabel(histogram.get('label') or histogram['variable'])
                axis.set_ylabel('Sum of object weights / bin (raw)' if histogram['entry_kind'] == 'objects'
                                else 'Sum of event weights / bin (raw)')
                axis.set_title(f"{title}\nStage: {histogram['stage']}")
                text = (f"Selected events: {histogram['selected_events']} | Missing values: {histogram['missing_values']}\n"
                        f"Flow sumw: under={histogram['underflow']['sumw']:.5g}, over={histogram['overflow']['sumw']:.5g}")
                axis.text(.02, .98, text, transform=axis.transAxes, ha='left', va='top', fontsize=8,
                          bbox={'facecolor': 'white', 'alpha': .8, 'edgecolor': 'none'})
                axis.margins(y=.22)
                axis.legend(loc='lower right', fontsize=8)
                for extension in ('png', 'pdf'):
                    figure.savefig(directory / f'hist_{name}.{extension}', dpi=150)
            finally:
                figure.clear()
        labels = [row['stage'] for row in cutflow]
        for weighted in (False, True):
            figure = Figure(figsize=(8, max(3, .45*len(labels)+1.5)), layout='constrained')
            FigureCanvasAgg(figure)
            try:
                axis = figure.subplots()
                values = [row['sumw' if weighted else 'n'] for row in cutflow]
                errors = [math.sqrt(row['sumw2']) for row in cutflow] if weighted else None
                axis.barh(range(len(labels)), values, xerr=errors, color='C0', alpha=.75, capsize=2)
                axis.set_yticks(range(len(labels)), labels)
                axis.invert_yaxis()
                axis.axvline(0, linewidth=.7, color='0.5')
                axis.set_xlabel('Sum of event weights (raw); errors sqrt(sumw2)' if weighted else 'Events')
                axis.set_title(title + '\nCumulative cutflow')
                for extension in ('png', 'pdf'):
                    figure.savefig(directory / f"cutflow_{'weights' if weighted else 'counts'}.{extension}", dpi=150)
            finally:
                figure.clear()


def publish_results(report, config, cutflow, histograms, destination):
    """Render/stage all artifacts and exclusively publish one complete directory."""
    import time
    import yaml
    from importlib.metadata import version
    from .._version import __version__
    validate_numerical(config, cutflow, histograms)
    with tempfile.TemporaryDirectory(prefix='.mgpd-analysis-', dir=destination.parent) as temporary:
        staged = Path(temporary) / 'result'
        staged.mkdir()
        (staged / 'analysis.yml').write_text(yaml.safe_dump(config, sort_keys=False), encoding='utf-8')
        for name, data in [('cutflow.json', cutflow), ('histograms.json', histograms)]:
            (staged / name).write_text(json.dumps(data, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        write_cutflow_csv(cutflow, staged / 'cutflow.csv')
        started = time.perf_counter()
        try:
            render_plots(report, cutflow, histograms, staged / 'plots')
        except (RuntimeError, ValueError, TypeError) as exc:
            raise ValueError(f'Could not render analysis plots: {exc}') from exc
        report['rendering'] = {'mgpd_version': __version__, 'matplotlib_version': version('matplotlib'),
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
