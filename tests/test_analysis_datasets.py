"""Managed merge selection and explicit physical normalization contracts."""
from contextlib import redirect_stdout
import copy
import csv
import gzip
from importlib.util import find_spec
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mgpd.analysis import plan_analysis, run_analysis, render_analysis
from mgpd.analysis.config import validate_analysis_config
from mgpd.analysis.reporting import histogram_plot_data
from mgpd.analysis.saved import load_saved_results
from mgpd.storage import file_sha256
from mgpd.cli import main
import yaml
from test_analysis_engine import document, event, analysis_config


def physical(mode='cross_section', denominator=4):
    result = {'mode': mode, 'cross_section_pb': 12., 'sumw_generated': denominator,
              'weight_convention': 'dimensionless'}
    if mode == 'luminosity':
        result['luminosity_pb_inverse'] = 100.
    return result


class DatasetFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.merged = self.root / 'merged'; self.merged.mkdir()
        self.path = self.merged / 'merged_unweighted_events.lhe'
        self.path.write_text(document([event(1, 40), event(1, 20)]).replace('-4 1', '3 1'))
        self.config = analysis_config()
        self.report = {'schema_version': 1, 'partial': False, 'omitted_jobs': [],
                       'production_id': 'root-production', 'plan_sha256': 'production-plan',
                       'selection_sha256': 'reviewed-selection', 'samples': {
                           'signal': {'state': 'merged', 'directory': '.', 'formats': ['lhe'],
                                      'metrics': {'lhe_events': 2}, 'jobs': [
                                          {'job_id': 'job-1', 'production_id': 'original'},
                                          {'job_id': 'job-2', 'production_id': 'recovery'}]}},
                       'outputs': [{'path': self.path.name, 'bytes': self.path.stat().st_size,
                                    'sha256': file_sha256(self.path)}]}
        self.write_report()

    def write_report(self):
        (self.merged / 'merge-report.json').write_text(json.dumps(self.report))

    def plan(self, **kwargs):
        return plan_analysis(self.merged, self.config, **kwargs)


class DatasetPlanningTests(DatasetFixture):
    def test_managed_resolution_records_original_and_recovered_job_once(self):
        plan = self.plan()
        self.assertEqual(plan['sample'], 'signal')
        self.assertEqual([row['path'] for row in plan['inputs']], [str(self.path)])
        self.assertEqual([job['job_id'] for job in plan['dataset']['jobs']], ['job-1', 'job-2'])
        self.assertEqual(plan['dataset']['expected_events'], 2)
        # Other retained job copies are never globbed.
        retained = self.merged / 'all'; retained.mkdir()
        (retained / 'copy.lhe').write_text(self.path.read_text())
        self.assertEqual(len(self.plan()['inputs']), 1)
        self.report['outputs'].append({'path': 'all/removed-copy.lhe', 'bytes': 1, 'sha256': 'unused'})
        self.write_report()
        self.assertEqual(len(self.plan()['inputs']), 1)

    def test_production_default_merge_and_missing_or_stale_merge(self):
        manifest = self.root / 'manifest.json'
        manifest.write_text(json.dumps({'production_id': 'root-production', 'plan_sha256': 'production-plan'}))
        self.assertEqual(plan_analysis(self.root, self.config)['inputs'], self.plan()['inputs'])
        manifest.write_text(json.dumps({'production_id': 'different', 'plan_sha256': 'production-plan'}))
        with self.assertRaisesRegex(ValueError, 'does not belong'):
            plan_analysis(self.root, self.config)
        (self.merged / 'merge-report.json').unlink()
        with self.assertRaisesRegex(ValueError, 'mgpd merge first'):
            plan_analysis(self.root, self.config)

    def test_partial_duplicate_and_checksum_refusal(self):
        for change, message in [('partial', 'Partial'), ('jobs', 'logical job'), ('hash', 'checksum')]:
            with self.subTest(change=change):
                original = copy.deepcopy(self.report)
                if change == 'partial':
                    self.report['partial'] = True
                elif change == 'jobs':
                    self.report['samples']['signal']['jobs'][1]['job_id'] = 'job-1'
                else:
                    self.report['outputs'][0]['sha256'] = '0' * 64
                self.write_report()
                with self.assertRaisesRegex(ValueError, message):
                    self.plan()
                self.report = original
        self.write_report()

    def test_multiple_samples_and_subdirectory_selection(self):
        subdir = self.merged / 'second'; subdir.mkdir()
        path = subdir / 'merged_unweighted_events.lhe'; path.write_bytes(self.path.read_bytes())
        self.report['samples']['background'] = {'state': 'merged', 'directory': 'second', 'formats': ['lhe'],
                                               'metrics': {'lhe_events': 2},
                                               'jobs': [{'job_id': 'job-3', 'production_id': 'original'}]}
        self.report['outputs'].append({'path': 'second/' + path.name, 'bytes': path.stat().st_size,
                                       'sha256': file_sha256(path)})
        self.write_report()
        with self.assertRaisesRegex(ValueError, '--sample'):
            self.plan()
        self.assertEqual(self.plan(sample='background')['inputs'][0]['path'], str(path))
        self.assertEqual(plan_analysis(subdir, self.config)['sample'], 'background')
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            plan_analysis(subdir, self.config, sample='signal')

    def test_format_choice_and_unsafe_paths(self):
        lhco = self.merged / 'merged_delphes_events.lhco'; lhco.write_text('0 1 0\n')
        self.report['samples']['signal']['formats'].append('lhco')
        self.report['outputs'].append({'path': lhco.name, 'bytes': lhco.stat().st_size, 'sha256': file_sha256(lhco)})
        self.write_report()
        config = copy.deepcopy(self.config); config['input'].pop('format', None)
        with self.assertRaisesRegex(ValueError, '--format'):
            plan_analysis(self.merged, config)
        self.assertEqual(self.plan(format='lhe')['format'], 'lhe')
        self.report['outputs'][0]['path'] = '../outside.lhe'
        self.write_report()
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            self.plan()

    def test_invalid_normalization_config(self):
        cases = [{'mode': 'cross_section'}, {**physical(), 'sumw_generated': 0},
                 {**physical(), 'weight_convention': 'pb'}, {**physical(), 'cross_section_pb': -1},
                 {**physical('luminosity'), 'luminosity_pb_inverse': float('inf')},
                 {**physical(), 'luminosity_pb_inverse': 100}, {'mode': 'raw', 'sumw_generated': 1}]
        for policy in cases:
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                config = copy.deepcopy(self.config); config['input']['normalization'] = policy
                validate_analysis_config(config)


@unittest.skipUnless(find_spec('uproot') and find_spec('matplotlib'), 'requires analysis extra')
class DatasetExecutionTests(DatasetFixture):

    def test_managed_event_totals_metadata_mutation_and_output_confinement(self):
        plan = self.plan()
        with self.assertRaisesRegex(ValueError, 'outside the managed'):
            run_analysis(plan, self.merged / 'analysis')
        self.report['samples']['signal']['metrics']['lhe_events'] = 3; self.write_report()
        with self.assertRaisesRegex(ValueError, 'missing or changed'):
            run_analysis(plan, self.root / 'stale')
        with self.assertRaisesRegex(ValueError, 'event total'):
            run_analysis(self.plan(), self.root / 'wrong-count')
        self.assertFalse((self.root / 'wrong-count').exists())

    def test_cross_section_scaled_stats_histograms_csv_and_saved_render(self):
        self.config['input']['normalization'] = physical()
        output = self.root / 'cross-section'
        report = run_analysis(self.plan(), output, plots=True)
        self.assertEqual(report['normalization_metadata']['factor'], 3)
        rows = json.loads((output / 'cutflow.json').read_text())
        self.assertEqual(rows[0]['sumw'], 2)
        self.assertEqual(rows[0]['normalized'], {'value': 6., 'variance': 18., 'uncertainty': 18**.5, 'unit': 'pb'})
        self.assertEqual(rows[-1]['normalized']['value'], 3)
        histogram = json.loads((output / 'histograms.json').read_text())['mass_plot']
        self.assertEqual(sum(histogram_plot_data(histogram)['values']), 3)
        self.assertEqual(histogram['normalized']['underflow']['value'], 3)
        with (output / 'cutflow.csv').open() as stream:
            first = next(csv.DictReader(stream))
        self.assertEqual(first['normalized_unit'], 'pb')
        self.assertEqual(float(first['normalized_value']), 6)
        self.path.unlink()
        (self.merged / 'merge-report.json').unlink()
        self.assertEqual(render_analysis(output, self.root / 'rendered')['normalization'], 'cross_section')

    def test_luminosity_preview_uses_full_external_denominator(self):
        self.config['input']['normalization'] = physical('luminosity')
        output = self.root / 'preview'
        config_path = self.root / 'analysis.yml'; config_path.write_text(yaml.safe_dump(self.config))
        printed = io.StringIO()
        with redirect_stdout(printed):
            status = main(['analyze', str(self.merged), '--config', str(config_path),
                           '--sample', 'signal', '--format', 'lhe', '--max-events', '1', '-o', str(output)])
        self.assertEqual(status, 0)
        self.assertIn('luminosity normalization', printed.getvalue())
        rows = json.loads((output / 'cutflow.json').read_text())
        self.assertEqual(rows[0]['normalized']['value'], 300)
        self.assertEqual(rows[0]['normalized']['unit'], 'events')
        self.assertEqual(rows[0]['normalized']['variance'], 90000)

    def test_unsupported_lhe_strategy_and_negative_signed_weights(self):
        self.path.write_text(document([event(2, 40), event(-1, 50)]).replace('-4 1', '-3 1'))
        self.config['input']['normalization'] = physical(denominator=1)
        plan = plan_analysis(self.path, self.config)
        output = self.root / 'signed'; run_analysis(plan, output)
        rows = json.loads((output / 'cutflow.json').read_text())
        self.assertEqual(rows[0]['normalized']['value'], 12)
        self.assertEqual(rows[0]['normalized']['variance'], 720)
        self.path.write_text(document([event(1, 40)]))  # IDWTUP=-4
        with self.assertRaisesRegex(ValueError, 'IDWTUP'):
            plan_analysis(self.path, self.config)

    def test_changed_metadata_during_execution_and_normalized_tampering(self):
        from mgpd.analysis.engine import calculate_analysis
        plan = self.plan()
        def changed(plan):
            result = calculate_analysis(plan)
            self.report['selection_sha256'] = 'changed'; self.write_report()
            return result
        with patch('mgpd.analysis.engine.calculate_analysis', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'changed'):
                run_analysis(plan, self.root / 'mutation')
        self.assertFalse((self.root / 'mutation').exists())
        self.config['input']['normalization'] = physical()
        output = self.root / 'valid'; run_analysis(self.plan(), output)
        report_path = output / 'analysis-report.json'; report = json.loads(report_path.read_text())
        report['normalization_metadata']['factor'] = 999; report_path.write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError, 'normalization metadata'):
            load_saved_results(output)

    def test_actual_merge_writer_report_is_accepted_without_scanning_attempts(self):
        from test_production_merging import ProductionMergingTests
        from mgpd.production_merging import merge_production
        helper = ProductionMergingTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        parent = helper.fixture()
        def native(command, check):
            completed = helper.fake_container(command, check)
            merged_files = list(Path(command[command.index('--bind') + 1][:-len(':/work')]).rglob('merged_unweighted_events.lhe.gz'))
            for path in merged_files:
                data = gzip.decompress(path.read_bytes()).decode().replace('<event>\n</event>\n', event(1, 40))
                path.write_bytes(gzip.compress(data.encode()))
            return completed
        with patch('mgpd.merge.subprocess.run', side_effect=native):
            merged = merge_production(parent.output_dir, backend='local-apptainer', keep_all=True)
        plan = plan_analysis(merged, self.config)
        self.assertEqual(len(plan['dataset']['jobs']), 2)
        self.assertEqual(len(plan['inputs']), 1)
        result = run_analysis(plan, self.root / 'actual-report')
        self.assertEqual(result['events_processed'], 4)

    def test_normalization_numeric_failures_and_serialized_validation(self):
        from mgpd.analysis.normalization import normalization_plan, scaled
        from mgpd.analysis.reporting import validate_numerical
        self.config['input']['normalization'] = physical()
        output = self.root / 'numbers'; run_analysis(self.plan(), output)
        rows = json.loads((output / 'cutflow.json').read_text())
        histograms = json.loads((output / 'histograms.json').read_text())
        config = self.plan()['config']
        histograms['mass_plot']['normalized']['values'][0] += 1
        with self.assertRaisesRegex(ValueError, 'Normalized histogram'):
            validate_numerical(config, rows, histograms)
        with self.assertRaisesRegex(ValueError, 'overflows'):
            scaled(1e300, 1e300, {'factor': 1e100, 'unit': 'pb'})
        policy = copy.deepcopy(config)
        policy['input']['normalization'] = physical(denominator=1e-300)
        with self.assertRaisesRegex(ValueError, 'factor'):
            normalization_plan(policy, 'root', [])

    def test_root_and_lhco_explicit_normalization(self):
        from test_analysis_root import RootAnalysisTests
        from test_analysis_lhco import LHCOTests
        for helper_type in (RootAnalysisTests, LHCOTests):
            with self.subTest(format=helper_type.__name__):
                helper = helper_type()
                helper.setUp(); self.addCleanup(helper.doCleanups)
                if isinstance(helper, RootAnalysisTests):
                    helper.write()
                helper.config['input']['normalization'] = physical()
                output = self.root / helper_type.__name__
                report = run_analysis(plan_analysis(helper.path, helper.config), output)
                self.assertEqual(report['normalization_metadata']['factor'], 3)
                rows = json.loads((output / 'cutflow.json').read_text())
                self.assertEqual(rows[0]['normalized']['value'], 3 * rows[0]['sumw'])
                self.assertEqual(rows[0]['normalized']['variance'], 9 * rows[0]['sumw2'])
