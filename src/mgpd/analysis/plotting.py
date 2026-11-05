"""Optional Matplotlib renderer; imported only when graphics are requested."""
import math
from pathlib import Path
from matplotlib import rc_context
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from .reporting import histogram_plot_data


def render_plots(report, cutflow, histograms, directory):

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
