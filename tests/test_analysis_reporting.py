"""Portable analysis reports, real headless rendering and saved-data integrity."""
from contextlib import redirect_stderr, redirect_stdout
import copy
import csv
from importlib.util import find_spec
import io
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml
from mgpd.analysis import analysis_example, plan_analysis, render_analysis, run_analysis
from mgpd.analysis.config import load_analysis_config
from mgpd.analysis.reporting import histogram_plot_data, validate_numerical, write_cutflow_csv
from mgpd.analysis.saved import load_saved_results
from mgpd.cli import main
from test_analysis_engine import analysis_config, document, EVENTS

HAS_ANALYSIS = all(find_spec(name) for name in ('numpy', 'awkward', 'vector', 'uproot', 'matplotlib'))


class AnalysisExampleTests(unittest.TestCase):
    def test_example_is_packaged_valid_and_printed_without_optional_imports(self):
        text = analysis_example()
        config, order = load_analysis_config(yaml.safe_load(text))
        self.assertEqual(config['input']['format'], 'lhe')
        self.assertIn('m_ee', order)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['analyze', '--example', 'dileptons-lhe']), 0)
        self.assertEqual(output.getvalue(), text)
        code = "import sys; from mgpd.cli import main; main(['analyze','--example','dileptons-lhe']); assert not {'numpy','uproot','matplotlib'} & set(sys.modules)"
        subprocess.run([sys.executable, '-c', code], check=True, capture_output=True, text=True)

    def test_cli_modes_are_mutually_exclusive(self):
        cases = [['--example', 'dileptons-lhe', '--config', 'analysis.yml'],
                 ['--example', 'dileptons-lhe', '-o', 'results'],
                 ['--from-results', 'saved'], ['--from-results', 'saved', '-o', 'new', 'events.lhe'],
                 ['--from-results', 'saved', '-o', 'new', '--validate-only'],
                 ['events.lhe'], ['--config', 'analysis.yml'],
                 ['--from-results', 'saved', '-o', 'new', '--chunk-size', '10000'],
                 ['--example', 'dileptons-lhe', '--plots'],
                 ['--example', 'dileptons-lhe', '--export-root'],
                 ['events.lhe', '--config', 'analysis.yml', '--validate-only', '--export-root'],
                 ['events.lhe', '--config', 'analysis.yml', '--validate-only', '--plots']]
        for args in cases:
            with self.subTest(args=args), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(['analyze', *args])


@unittest.skipUnless(HAS_ANALYSIS, 'Install the analysis/dev extra for rendering tests')
class AnalysisReportingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.base = Path(cls.temporary.name).resolve()
        events = cls.base / 'events.lhe'; events.write_text(document(EVENTS))
        cls.plan = plan_analysis(events, analysis_config(), sample='Dileptons')
        cls.saved = cls.base / 'original'
        cls.report = run_analysis(cls.plan, cls.saved, plots=True)
        events.unlink()  # All rendering tests deliberately run without original events.

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / 'saved'
        shutil.copytree(self.saved, self.source)

    def test_csv_preserves_values_intervals_reasons_and_signed_ratios(self):
        with (self.source / 'cutflow.csv').open(newline='') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual([int(row['n']) for row in rows], [5, 4, 3, 1])
        self.assertEqual([float(row['sumw']) for row in rows], [4.5, 4.5, 1.5, 2])
        self.assertAlmostEqual(float(rows[-1]['relative_weighted_value']), 4/3)
        self.assertAlmostEqual(float(rows[-1]['relative_weighted_uncertainty']), math.sqrt(32/27))
        self.assertEqual(rows[-1]['relative_weighted_kind'], 'signed_weighted_ratio')
        self.assertIn('Wilson', rows[-1]['cumulative_count_method'])
        self.assertTrue(rows[-1]['cumulative_count_interval_low'])
        cutflow = json.loads((self.source / 'cutflow.json').read_text())
        cutflow[0]['cumulative']['weighted'].update(value=None, uncertainty=None, reason='zero weighted denominator')
        path = self.root / 'null.csv'; write_cutflow_csv(cutflow, path)
        with path.open(newline='') as stream:
            row = next(csv.DictReader(stream))
        self.assertEqual(row['cumulative_weighted_value'], '')
        self.assertEqual(row['cumulative_weighted_reason'], 'zero weighted denominator')

    def test_plot_numbers_are_exactly_saved_bins_and_variances(self):
        histogram = json.loads((self.source / 'histograms.json').read_text())['mass_plot']
        data = histogram_plot_data(histogram)
        self.assertEqual(data['edges'], [60, 80, 100])
        self.assertEqual(data['centers'], [70, 90])
        self.assertEqual(data['values'], [.5, 1])
        self.assertEqual(data['errors'], [.5, math.sqrt(5)])
        histogram['sumw'] = [-3, 0]
        self.assertEqual(histogram_plot_data(histogram)['values'], [-3, 0])

    def test_real_png_pdf_files_and_artifact_hash_inventory(self):
        from mgpd.storage import file_sha256
        plots = sorted((self.source / 'plots').iterdir())
        self.assertEqual(len(plots), 6)
        for path in plots:
            with self.subTest(path=path):
                signature = b'\x89PNG\r\n\x1a\n' if path.suffix == '.png' else b'%PDF-'
                self.assertTrue(path.read_bytes().startswith(signature))
                self.assertGreater(path.stat().st_size, 1000)
        report = json.loads((self.source / 'analysis-report.json').read_text())
        self.assertEqual(report['rendering']['scale'], 'linear')
        self.assertEqual(report['rendering']['backend'], 'Agg')
        for entry in report['artifact_metadata']:
            path = self.source / entry['path']
            self.assertEqual(path.stat().st_size, entry['bytes'])
            self.assertEqual(file_sha256(path), entry['sha256'])
        self.assertNotIn('analysis-report.json', {item['path'] for item in report['artifact_metadata']})

    def test_saved_render_never_reads_events_and_preserves_numerical_data(self):
        before = {path: path.read_bytes() for path in self.source.rglob('*') if path.is_file()}
        shutil.rmtree(self.source / 'plots')  # Derived plots are optional for regeneration.
        destination = self.root / 'rendered'
        with patch('mgpd.analysis._verify_inputs', side_effect=AssertionError('No event access')), \
                patch('mgpd.analysis.engine.calculate_analysis', side_effect=AssertionError('No computation')):
            report = render_analysis(self.source, destination)
        self.assertEqual(report['events_processed'], 5)
        self.assertTrue(report['rendered_from']['original_artifact_hashes_verified'])
        self.assertEqual((destination / 'cutflow.json').read_bytes(), before[self.source / 'cutflow.json'])
        self.assertEqual((destination / 'histograms.json').read_bytes(), before[self.source / 'histograms.json'])
        for path, content in before.items():
            if path.parent.name != 'plots':
                self.assertEqual(path.read_bytes(), content)
        self.assertTrue((destination / 'plots/hist_mass_plot.pdf').is_file())

    def test_saved_numerical_tampering_fails_before_output(self):
        path = self.source / 'histograms.json'
        data = json.loads(path.read_text()); data['mass_plot']['sumw'][0] = 999
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'checksum'):
            render_analysis(self.source, self.root / 'failed')
        self.assertFalse((self.root / 'failed').exists())

    def test_saved_schema_validation_rejects_bad_data_even_without_hashes(self):
        report_path = self.source / 'analysis-report.json'
        report = json.loads(report_path.read_text()); report.pop('artifact_metadata')
        report_path.write_text(json.dumps(report))
        path = self.source / 'histograms.json'
        data = json.loads(path.read_text()); data['mass_plot']['sumw2'][0] = -1
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'nonnegative'):
            render_analysis(self.source, self.root / 'failed')
        self.assertFalse((self.root / 'failed').exists())

    def test_earlier_numerical_only_results_are_explicitly_unverified(self):
        path = self.source / 'analysis-report.json'
        report = json.loads(path.read_text()); report.pop('artifact_metadata')
        path.write_text(json.dumps(report))
        result = render_analysis(self.source, self.root / 'rendered')
        self.assertFalse(result['rendered_from']['original_artifact_hashes_verified'])
        self.assertTrue(any('no artifact hashes' in warning for warning in result['warnings']))

    def test_nonfinite_duplicate_json_incomplete_and_escaping_inputs_fail(self):
        histogram_path = self.source / 'histograms.json'
        original = histogram_path.read_bytes()
        for payload in (b'{"x":NaN}', b'{"x":1,"x":2}'):
            histogram_path.write_bytes(payload)
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                load_saved_results(self.source)
        histogram_path.write_bytes(original)
        path = self.source / 'analysis-report.json'
        report = json.loads(path.read_text()); report['state'] = 'running'
        path.write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError, 'completed'):
            load_saved_results(self.source)
        path.write_bytes((self.saved / 'analysis-report.json').read_bytes())
        outside = self.root / 'outside.json'; outside.write_bytes(original)
        histogram_path.unlink(); histogram_path.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'outside'):
            load_saved_results(self.source)

    def test_invalid_histogram_dimensions_and_path_names_are_rejected(self):
        _, config, cuts, histograms, _ = load_saved_results(self.source)
        bad = copy.deepcopy(histograms); bad['mass_plot']['sumw'] = [1]
        with self.assertRaisesRegex(ValueError, 'length'):
            validate_numerical(config, cuts, bad)
        bad = copy.deepcopy(histograms); bad['mass_plot']['edges'] = [60, 60, 100]
        with self.assertRaisesRegex(ValueError, 'increasing'):
            validate_numerical(config, cuts, bad)
        bad = copy.deepcopy(histograms); bad['mass_plot']['statistical_uncertainties'][0] = 999
        with self.assertRaisesRegex(ValueError, 'uncertainties'):
            validate_numerical(config, cuts, bad)
        bad_config = copy.deepcopy(config); bad_config['histograms'][0]['name'] = '../../escape'
        with self.assertRaises(ValueError):
            load_analysis_config(bad_config)

    def test_saved_yaml_scalar_cannot_be_treated_as_an_external_path(self):
        path = self.source / 'analysis-report.json'
        report = json.loads(path.read_text()); report.pop('artifact_metadata')
        path.write_text(json.dumps(report))
        (self.source / 'analysis.yml').write_text('/do-not-open/events.lhe')
        with patch('mgpd.analysis.config.load_analysis_config', side_effect=AssertionError('No path loading')):
            with self.assertRaisesRegex(ValueError, 'mapping'):
                load_saved_results(self.source)

    def test_existing_and_nested_render_destinations_are_preserved(self):
        destination = self.root / 'existing'; destination.mkdir()
        (destination / 'keep').write_text('preserve')
        with self.assertRaises(FileExistsError):
            render_analysis(self.source, destination)
        self.assertEqual((destination / 'keep').read_text(), 'preserve')
        with self.assertRaisesRegex(ValueError, 'outside'):
            render_analysis(self.source, self.source / 'nested')
        self.assertFalse((self.source / 'nested').exists())

    def test_plot_failure_publishes_nothing_and_restores_matplotlib_state(self):
        import matplotlib
        previous = matplotlib.rcParams['text.usetex']
        with patch('matplotlib.figure.Figure.savefig', side_effect=RuntimeError('plot failed')):
            with self.assertRaisesRegex(ValueError, 'plot failed'):
                render_analysis(self.source, self.root / 'failed')
        self.assertEqual(matplotlib.rcParams['text.usetex'], previous)
        self.assertFalse((self.root / 'failed').exists())
        self.assertFalse(list(self.root.glob('.mgpd-analysis-*')))

    def test_saved_results_cli_and_packaged_starter_execution(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(['analyze', '--from-results', str(self.source), '-o', str(self.root / 'rendered')])
        self.assertEqual(code, 0)
        self.assertIn('no events reread', output.getvalue())
        events = self.root / 'events.lhe'; events.write_text(document(EVENTS))
        config_path = self.root / 'analysis.yml'; config_path.write_text(analysis_example())
        with redirect_stdout(io.StringIO()):
            code = main(['analyze', str(events), '--config', str(config_path), '-o', str(self.root / 'example'), '--plots'])
        self.assertEqual(code, 0)
        self.assertTrue((self.root / 'example/plots/hist_pt_ee_selected.png').is_file())

    def test_default_execution_skips_plotting_but_retains_csv_and_can_render_later(self):
        events = self.root / 'events.lhe'; events.write_text(document(EVENTS))
        output = self.root / 'numerical'
        with patch('mgpd.analysis.reporting.render_plots', side_effect=AssertionError('No plotting by default')):
            report = run_analysis(plan_analysis(events, analysis_config()), output)
        self.assertFalse(report['rendering']['enabled'])
        self.assertFalse((output / 'plots').exists())
        self.assertTrue((output / 'cutflow.csv').is_file())
        events.unlink()
        result = render_analysis(output, self.root / 'rendered')
        self.assertTrue(result['rendering']['enabled'])
        self.assertTrue((self.root / 'rendered/plots/hist_mass_plot.png').is_file())

    def test_default_cli_does_not_load_matplotlib(self):
        events = self.root / 'events.lhe'; events.write_text(document(EVENTS))
        config_path = self.root / 'analysis.yml'; config_path.write_text(analysis_example())
        command = ['analyze', str(events), '--config', str(config_path), '-o', str(self.root / 'cli-numerical')]
        code = ('import sys; from mgpd.cli import main; '
                'assert main(' + repr(command) + ') == 0; '
                'assert "matplotlib" not in sys.modules')
        subprocess.run([sys.executable, '-c', code], check=True, capture_output=True, text=True)
        self.assertFalse((self.root / 'cli-numerical/plots').exists())

    def test_empty_and_preview_results_have_renderable_artifacts(self):
        for count, maximum in ((0, None), (2, 1)):
            events = self.root / f'events{count}.lhe'; events.write_text(document(EVENTS[:count]))
            result = run_analysis(plan_analysis(events, analysis_config(), max_events=maximum), self.root / f'output{count}', plots=True)
            self.assertTrue((Path(result['output']) / 'plots/hist_mass_plot.png').is_file())
            self.assertEqual(result['preview_requested'], maximum is not None)
