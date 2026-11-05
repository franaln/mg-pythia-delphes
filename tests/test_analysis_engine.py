"""Independent analytic checks for stage-two LHE numerical execution."""
from contextlib import redirect_stderr, redirect_stdout
import gzip
import io
from importlib.util import find_spec
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml
from mgpd.analysis import plan_analysis, run_analysis
from mgpd.analysis.engine import calculate_analysis
from mgpd.analysis.evaluation import Expression, _pair
from mgpd.analysis.readers import Momentum, iter_lhe_batches
from mgpd.analysis.statistics import Counter, efficiency
from mgpd.cli import main
from test_analysis import config

HEADER = '<LesHouchesEvents version="3.0"><header/><init>\n2212 2212 6500 6500 0 0 0 0 -4 1\n2.5 0.1 1 1\n</init>\n'


def event(weight, momentum=None, same_sign=False, extra=''):
    rows = []
    if momentum is not None:
        rows = [f'11 1 0 0 0 0 {momentum} 0 0 {momentum} 0 0 9',
                f'{11 if same_sign else -11} 1 0 0 0 0 {-momentum} 0 0 {momentum} 0 0 9']
    return '<event>\n' + f'{len(rows)} 1 {weight} 91 0.0073 0.118\n' + '\n'.join(rows) + '\n' + extra + '\n</event>\n'


def document(events):
    return HEADER + ''.join(events) + '</LesHouchesEvents>\n'


EVENTS = [event(2, 40), event(-1, 50), event(3, 10, same_sign=True), event(.5, 30), event(0)]


def analysis_config():
    data = config()
    data['cuts'].insert(1, {'name': 'valid_pair', 'expression': 'is_valid(dilepton)'})
    data['cuts'][-1]['expression'] = '(m_ee > 70) & (m_ee < 90)'
    data['histograms'][0].update(stage='initial', bins=[60, 80, 100])
    return data


@unittest.skipUnless(all(find_spec(name) for name in ('numpy', 'awkward', 'vector', 'uproot', 'matplotlib')),
                     'Install the analysis/dev extra for execution tests')
class AnalysisEngineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / 'events.lhe'
        self.path.write_text(document(EVENTS))
        self.data = analysis_config()

    def plan(self, **options):
        return plan_analysis(self.path, self.data, **options)

    def test_known_dilepton_masses_weighted_cutflow_and_subset_error(self):
        result = calculate_analysis(self.plan())
        rows = result['cutflow']
        self.assertEqual([row['n'] for row in rows], [5, 4, 3, 1])
        self.assertEqual([row['sumw'] for row in rows], [4.5, 4.5, 1.5, 2])
        self.assertEqual([row['sumw2'] for row in rows], [14.25, 14.25, 5.25, 4])
        self.assertEqual(rows[-1]['relative']['counts']['value'], 1/3)
        weighted = rows[-1]['relative']['weighted']
        self.assertAlmostEqual(weighted['value'], 4/3)
        self.assertAlmostEqual(weighted['uncertainty'], math.sqrt(32/27))
        self.assertEqual(weighted['kind'], 'signed_weighted_ratio')
        self.assertEqual(rows[0]['negative_weights'], 1)
        self.assertEqual(rows[0]['zero_weights'], 1)
        histogram = result['histograms']['mass_plot']
        self.assertEqual(histogram['counts'], [1, 2])
        self.assertEqual(histogram['sumw'], [.5, 1])
        self.assertEqual(histogram['sumw2'], [.25, 5])
        self.assertEqual(histogram['missing_values'], 2)
        self.assertEqual(result['missing_variable_events']['m_ee'], 2)
        self.assertEqual(result['objects']['electrons']['selected_objects'], 8)

    def test_unit_weights_count_efficiency_and_wilson_interval(self):
        self.data['input']['weights'] = {'mode': 'unit'}
        rows = calculate_analysis(self.plan())['cutflow']
        self.assertEqual(rows[0]['sumw'], 5)
        self.assertEqual(rows[0]['sumw2'], 5)
        ratio = rows[-1]['cumulative']
        self.assertEqual(ratio['weighted']['value'], .2)
        self.assertAlmostEqual(ratio['weighted']['uncertainty'], math.sqrt(.2*.8/5))
        low, high = ratio['counts']['interval']
        self.assertLess(low, .2); self.assertGreater(high, .2)
        self.assertTrue(ratio['counts']['unit_weight_sample'])

    def test_chunk_sizes_and_compressed_input_have_identical_numbers(self):
        baseline = calculate_analysis(self.plan(chunk_size=1))
        for size in (2, 3, 100):
            result = calculate_analysis(self.plan(chunk_size=size))
            self.assertEqual(result['cutflow'], baseline['cutflow'])
            self.assertEqual(result['histograms'], baseline['histograms'])
        compressed = self.root / 'events.lhe.gz'
        compressed.write_bytes(gzip.compress(self.path.read_bytes()))
        result = calculate_analysis(plan_analysis(compressed, self.data, chunk_size=2))
        self.assertEqual(result['cutflow'], baseline['cutflow'])

    def test_reader_batches_optional_metadata_fortran_numbers_and_mothers(self):
        optional = '<rwgt><wgt id="variation">3.2</wgt></rwgt><scales muf="91"/>'
        self.path.write_text(document([event('2.0D+00', 40, extra=optional)] * 13))
        batches = list(iter_lhe_batches(self.path, 5))
        self.assertEqual([len(batch) for batch in batches], [5, 5, 3])
        particle = batches[0][0]['particles'][0]
        self.assertEqual(particle.mothers, (0, 0))
        self.assertEqual(particle.charge, -1)
        self.assertEqual(batches[0][0]['weight'], 2)
        self.assertEqual([len(batch) for batch in iter_lhe_batches(self.path, 5, max_events=7)], [5, 2])

    def test_pair_selection_and_angular_wrap(self):
        particles = [Momentum(40, 0, 0, 40, charge=1, index=0),
                     Momentum(-40, 0, 0, 40, charge=-1, index=1),
                     Momentum(0, -40, 0, 40, charge=-1, index=2)]
        self.assertEqual(_pair(list(reversed(particles))).mass, 80)
        first = Momentum(math.cos(3.1), math.sin(3.1), 0, 1)
        second = Momentum(math.cos(-3.1), math.sin(-3.1), 0, 1)
        wrapped = Expression('delta_phi(first, second)', 'test')({'first': first, 'second': second})
        self.assertAlmostEqual(wrapped, 6.2 - 2*math.pi)
        self.assertAlmostEqual(Expression('delta_r(first, second)', 'test')({'first': first, 'second': second}), abs(wrapped))
        self.assertIsNone(Momentum(0, 0, 10, 10).eta)
        with self.assertRaisesRegex(ValueError, 'known charges'):
            _pair([Momentum(1, 0, 0, 1), particles[0]])

    def test_object_histogram_weight_replication_and_flow_bins(self):
        self.data['variables']['electron_pts'] = 'electrons.pt'
        self.data['histograms'].append({'name': 'object_pts', 'variable': 'electron_pts',
                                        'stage': 'initial', 'fill': 'objects', 'bins': [20, 40]})
        histogram = calculate_analysis(self.plan())['histograms']['object_pts']
        self.assertEqual(histogram['counts'], [4])
        self.assertEqual(histogram['sumw'], [5])
        self.assertEqual(histogram['sumw2'], [8.5])
        self.assertEqual(histogram['underflow']['sumw'], 6)
        self.assertEqual(histogram['overflow']['sumw'], -2)
        self.data['histograms'][-1]['fill'] = 'events'
        with self.assertRaisesRegex(ValueError, 'fill: objects'):
            calculate_analysis(self.plan())

    def test_object_filters_are_distinct_from_event_selection(self):
        self.data['objects']['electrons']['select'] += ' & (pt >= 30)'
        result = calculate_analysis(self.plan())
        self.assertEqual(result['cutflow'][0]['n'], 5)
        self.assertEqual(result['cutflow'][1]['n'], 3)
        self.assertEqual(result['histograms']['mass_plot']['missing_values'], 2)

    def test_missing_observables_cuts_and_numeric_shape_errors(self):
        self.data['variables']['undefined'] = '1 / (n_electrons - 2)'
        self.data['histograms'][0]['variable'] = 'undefined'
        histogram = calculate_analysis(self.plan())['histograms']['mass_plot']
        self.assertEqual(histogram['missing_values'], 4)
        self.data = analysis_config()
        self.data['cuts'] = [self.data['cuts'][-1]]
        result = calculate_analysis(self.plan())
        self.assertEqual(result['cutflow'][-1]['missing_cut_values'], 2)
        self.data['cuts'][0]['expression'] = 'electrons.pt > 10'
        with self.assertRaisesRegex(ValueError, 'boolean scalar'):
            calculate_analysis(self.plan())
        with self.assertRaisesRegex(ValueError, 'shapes'):
            Expression('a + b', 'test')({'a': [1], 'b': [1, 2]})
        self.assertIsNone(Expression('(-1) ** 0.5', 'test')({}))
        self.assertEqual(Expression('2 ** -1', 'test')({}), .5)
        self.assertIsNone(Expression('((10 ** 8) ** 8) ** 8', 'test')({}))

    def test_zero_and_cancelling_weight_denominators_are_explicit_nulls(self):
        self.path.write_text(document([event(1, 40), event(-1, 50)]))
        rows = calculate_analysis(self.plan())['cutflow']
        self.assertIsNone(rows[-1]['cumulative']['weighted']['value'])
        self.assertEqual(rows[-1]['cumulative']['weighted']['reason'], 'zero weighted denominator')
        counter, passed = Counter(), Counter()
        for weight in (1e16, 1, -1e16):
            counter.add(weight)
        self.assertEqual(counter.sumw.value(), 1)
        passed.add(1)
        self.assertIn('cancellation', efficiency(passed, counter)['weighted']['reason'])

    def test_subset_uncertainty_preserves_small_rejected_contribution(self):
        passed, total = Counter(), Counter()
        passed.add(1e16); total.add(1e16); total.add(1)
        uncertainty = efficiency(passed, total)['weighted']['uncertainty']
        self.assertAlmostEqual(uncertainty / 1e-16, math.sqrt(2))
        for weight in (1e-200, 1e200):
            with self.subTest(weight=weight), self.assertRaises(ValueError):
                Counter().add(weight)

    def test_empty_inputs_and_zero_selected_are_successful(self):
        self.path.write_text(document([]))
        result = calculate_analysis(self.plan())
        self.assertEqual(result['events_processed'], 0)
        self.assertIsNone(result['cutflow'][0]['cumulative']['counts']['value'])
        self.path.write_text(document([event(1, 10)]))
        result = calculate_analysis(self.plan())
        self.assertEqual(result['cutflow'][-1]['n'], 0)
        self.assertEqual(result['cutflow'][-1]['cumulative']['counts']['value'], 0)

    def test_multiple_files_and_global_preview_limit(self):
        self.path.write_text(document(EVENTS[:2]))
        second = self.root / 'second.lhe'; second.write_text(document(EVENTS[2:]))
        plan = plan_analysis([self.path, second], self.data, max_events=3, chunk_size=2)
        result = calculate_analysis(plan)
        self.assertEqual(result['events_processed'], 3)
        self.assertEqual(result['cutflow'][0]['sumw'], 4)
        self.assertTrue(result['limit_reached'])
        self.assertFalse(result['input_exhaustion_verified'])
        self.assertEqual(len(result['files_read']), 2)

    def test_corrupt_event_truncated_xml_and_nonfinite_weights_fail(self):
        bad = [document([event(1, 40).replace('2 1 1 91', '3 1 1 91')]),
               document([event('nan', 40)]), document([event(1, 40)])[:-25],
               HEADER + '<eventgroup>' + event(1, 40) + '</eventgroup></LesHouchesEvents>']
        for text in bad:
            self.path.write_text(text)
            with self.subTest(text=text), self.assertRaises(ValueError):
                list(iter_lhe_batches(self.path, 1))

    def test_execution_publishes_only_numerical_artifacts_and_preserves_inputs(self):
        before = self.path.read_bytes()
        destination = self.root / 'results'
        report = run_analysis(self.plan(chunk_size=2), destination)
        self.assertEqual(report['state'], 'completed')
        self.assertEqual(report['events_processed'], 5)
        self.assertEqual(set(p.name for p in destination.iterdir()),
                         {'analysis.yml', 'analysis-report.json', 'cutflow.json', 'histograms.json'})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(json.loads((destination / 'cutflow.json').read_text())[-1]['sumw'], 2)
        self.assertFalse(list(self.root.glob('.mgpd-analysis-*')))
        with self.assertRaises(FileExistsError):
            run_analysis(self.plan(), destination)

    def test_changed_input_and_processing_failure_do_not_publish_output(self):
        plan = self.plan()
        self.path.write_text(document(EVENTS[:1]))
        with self.assertRaisesRegex(ValueError, 'input missing or changed'):
            run_analysis(plan, self.root / 'changed')
        self.path.write_text(document([event('nan', 40)]))
        with self.assertRaisesRegex(ValueError, 'nonfinite'):
            run_analysis(self.plan(), self.root / 'failed')
        self.assertFalse((self.root / 'changed').exists())
        self.assertFalse((self.root / 'failed').exists())

    def test_input_mutation_during_processing_is_detected(self):
        plan = self.plan()
        def mutate(saved):
            result = calculate_analysis(saved)
            self.path.write_text(document(EVENTS[:1]))
            return result
        with patch('mgpd.analysis.engine.calculate_analysis', side_effect=mutate):
            with self.assertRaisesRegex(ValueError, 'input missing or changed'):
                run_analysis(plan, self.root / 'failed')
        self.assertFalse((self.root / 'failed').exists())

    def test_failed_publication_cleans_reservation_and_staging(self):
        with patch('os.replace', side_effect=OSError('publication failed')):
            with self.assertRaisesRegex(OSError, 'publication failed'):
                run_analysis(self.plan(), self.root / 'failed')
        self.assertFalse((self.root / 'failed').exists())
        self.assertFalse(list(self.root.glob('.mgpd-analysis-*')))

    def test_publication_race_preserves_a_new_destination(self):
        from mgpd.analysis import _verify_inputs
        destination = self.root / 'occupied'
        plan = self.plan()
        checks = 0
        def occupy(saved):
            nonlocal checks
            _verify_inputs(saved)
            checks += 1
            if checks == 2:
                destination.mkdir()
                (destination / 'keep').write_text('user file')
        with patch('mgpd.analysis._verify_inputs', side_effect=occupy):
            with self.assertRaises(FileExistsError):
                run_analysis(plan, destination)
        self.assertEqual((destination / 'keep').read_text(), 'user file')
        self.assertEqual(list(destination.iterdir()), [destination / 'keep'])
        self.assertFalse(list(self.root.glob('.mgpd-analysis-*')))

    def test_stale_stage_one_plan_requires_replanning(self):
        from mgpd.analysis import _checksum
        plan = self.plan(); plan['execution_available'] = False; plan.pop('engine')
        plan['plan_sha256'] = _checksum(plan)
        with self.assertRaisesRegex(ValueError, 'plan the analysis again'):
            run_analysis(plan, self.root / 'old-plan')

    def test_cli_execution_and_root_limit_are_explicit(self):
        path = self.root / 'analysis.yml'; path.write_text(yaml.safe_dump(self.data))
        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            code = main(['analyze', str(self.path), '--config', str(path), '-o', str(self.root / 'cli')])
        self.assertEqual(code, 0)
        self.assertIn('5 events', stdout.getvalue())
        # A root plan remains useful, but cannot enter the LHE engine.
        from mgpd.analysis import _checksum
        plan = self.plan(); plan['format'] = 'root'; plan['execution_available'] = False
        plan['plan_sha256'] = _checksum(plan)
        with self.assertRaisesRegex(ValueError, 'ROOT event processing'):
            run_analysis(plan, self.root / 'root-output')
