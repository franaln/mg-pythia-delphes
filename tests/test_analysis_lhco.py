"""Independent standard-LHCO fixtures; native converter acceptance is separate."""
from contextlib import redirect_stdout
import gzip
from importlib.util import find_spec
import io
import math
from pathlib import Path
import tempfile
import unittest

import yaml
from mgpd.analysis import analysis_example, plan_analysis, run_analysis, render_analysis
from mgpd.analysis.engine import calculate_analysis
from mgpd.analysis.lhco_reader import iter_lhco_batches, iter_lhco_events
from mgpd.cli import main


def row(index, kind, pt=40, phi=0, ntrk=0, mass=0, eta=0):
    return f'{index} {kind} {eta} {phi} {pt} {mass} {ntrk} 0 0 0 0\n'


class LHCOTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / 'events.lhco'
        self.text = ('# Standard LHCO\n0 101 0\n' + row(1, 1, ntrk=-1) + row(2, 1, phi=math.pi, ntrk=1)
                     + '\n0 102 0 # empty event\n0 103 0\n' + row(1, 1, pt=20, ntrk=-1)
                     + row(2, 1, pt=20, phi=math.pi, ntrk=1))
        self.path.write_text(self.text)
        self.config = yaml.safe_load(analysis_example('lhco-dileptons'))

    def plan(self, **options):
        return plan_analysis(self.path, self.config, **options)

    def test_reader_mass_charge_empty_events_and_gzip(self):
        events = list(iter_lhco_events(self.path))
        self.assertEqual([e['lhco_event_number'] for e in events], [101, 102, 103])
        self.assertEqual(events[1]['collections']['Electron'], [])
        first = events[0]['collections']['Electron']
        self.assertEqual([p.charge for p in first], [-1, 1])
        self.assertAlmostEqual(first[0].px, 40)
        compressed = self.directory / 'events.lhco.gz'
        compressed.write_bytes(gzip.compress(self.text.encode()))
        self.assertEqual([e['object_count'] for e in iter_lhco_events(compressed)], [2, 0, 2])
        batches = list(iter_lhco_batches(compressed, self.plan()['config'], 1, 2))
        self.assertEqual([len(b) for b in batches], [1, 1])

    def test_analytic_cutflow_histogram_and_chunk_invariance(self):
        first = calculate_analysis(self.plan(chunk_size=1))
        second = calculate_analysis(self.plan(chunk_size=7))
        self.assertEqual(first['cutflow'], second['cutflow'])
        self.assertEqual(first['histograms'], second['histograms'])
        self.assertEqual([r['n'] for r in first['cutflow']], [3, 2, 2, 1])
        self.assertEqual([r['sumw'] for r in first['cutflow']], [3, 2, 2, 1])
        hist = first['histograms']['m_ee_before_window']
        self.assertEqual(sum(hist['sumw']), 2)
        self.assertEqual(first['cutflow'][-1]['cumulative']['counts']['value'], 1/3)

    def test_explicit_unit_policy_and_unsupported_fields(self):
        for weights in ({'mode': 'nominal'}, {'mode': 'nominal', 'branch': 'Event.Weight'}):
            self.config['input']['weights'] = weights
            with self.assertRaisesRegex(ValueError, 'unit explicitly'):
                self.plan()
        self.config['input']['weights'] = {'mode': 'unit'}
        for field in ('pdg_id', 'status'):
            self.config['objects']['electrons']['select'] = f'{field} == 1'
            with self.assertRaisesRegex(ValueError, 'unsupported LHCO fields'):
                self.plan()
        self.config['objects']['electrons'] = {'source': 'Particle'}
        with self.assertRaisesRegex(ValueError, 'unsupported LHCO collection'):
            self.plan()

    def test_pair_charge_requirement_through_alias(self):
        self.config['objects']['electrons'] = {'source': 'Jet'}
        self.config['variables']['alias'] = 'electrons'
        self.config['variables']['dilepton'] = 'leading_os_pair(alias)'
        with self.assertRaisesRegex(ValueError, 'charge'):
            self.plan()

    def test_types_masses_and_missing_charge(self):
        self.path.write_text('0 1 0\n' + ''.join(row(i+1, kind, pt=3, mass=4, ntrk=0, eta=0)
                                               for i, kind in enumerate((0, 1, 2, 3, 4, 6))))
        event = next(iter_lhco_events(self.path))
        for source, values in event['collections'].items():
            particle = values[0]
            self.assertIsNone(particle.charge)
            self.assertAlmostEqual(particle.energy, 3 if source == 'MissingET' else 5)
        self.config['objects']['electrons'] = {'source': 'Tau'}
        self.assertEqual(calculate_analysis(self.plan())['cutflow'][-1]['n'], 0)

    def test_invalid_rows_and_errors_have_context(self):
        cases = [(row(1, 1), 'before LHCO'), ('0 1\n', 'header'),
                 ('0 1 0\n1 1 0 0 5\n', 'eleven'), ('0 1 0\n'+row(2, 1), 'consecutive'),
                 ('0 1 0\n'+row(1, 9), 'object type'), ('0 1 0\n'+row(1, 1, pt=-1), 'negative'),
                 ('0 1 0\n'+row(1, 1, pt='nan'), 'nonfinite'),
                 ('0 1 0\n'+row(1, 1, eta=10000), 'range')]
        for text, message in cases:
            with self.subTest(text=text):
                self.path.write_text(text)
                with self.assertRaisesRegex(ValueError, f'event .*line .*{message}'):
                    list(iter_lhco_events(self.path))

    def test_empty_file_and_final_event_without_newline(self):
        self.path.write_text('# no events\n')
        self.assertEqual(calculate_analysis(self.plan())['events_processed'], 0)
        self.path.write_text('0 1 0\n'+row(1, 1).rstrip())
        self.assertEqual(len(list(iter_lhco_events(self.path))), 1)

    def test_multiple_files_preview_mixed_and_duplicate_guards(self):
        other = self.directory / 'other.lhco'; other.write_text(self.text)
        plan = plan_analysis([self.path, other], self.config, max_events=4, chunk_size=2)
        self.assertEqual(calculate_analysis(plan)['events_processed'], 4)
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            plan_analysis([self.path, self.path], self.config)
        root = self.directory / 'other.root'; root.write_bytes(b'')
        with self.assertRaisesRegex(ValueError, 'representation'):
            plan_analysis([self.path, root], self.config)

    def test_missing_charge_is_not_inferred(self):
        self.path.write_text('0 1 0\n'+row(1, 1, ntrk=0)+row(2, 1, phi=math.pi, ntrk=1))
        with self.assertRaisesRegex(ValueError, 'known charges'):
            calculate_analysis(self.plan())
        self.config['objects']['electrons']['select'] = 'is_valid(charge)'
        result = calculate_analysis(self.plan())
        self.assertEqual([r['n'] for r in result['cutflow']], [1, 0, 0, 0])

    @unittest.skipUnless(find_spec('matplotlib') and find_spec('uproot'), 'requires analysis extra')
    def test_cli_plots_and_saved_rendering(self):
        config_path = self.directory / 'analysis.yml'; config_path.write_text(yaml.safe_dump(self.config))
        output = self.directory / 'output'
        with redirect_stdout(io.StringIO()):
            code = main(['analyze', str(self.path), '--config', str(config_path), '--plots', '-o', str(output)])
        self.assertEqual(code, 0)
        self.path.unlink()
        self.assertEqual(render_analysis(output, self.directory / 'rendered')['events_processed'], 3)

    @unittest.skipUnless(find_spec('uproot'), 'requires analysis extra')
    def test_malformed_later_event_publishes_no_output(self):
        self.path.write_text(self.text + '\n0 104 0\n1 1 broken\n')
        plan = self.plan()
        with self.assertRaisesRegex(ValueError, 'line'):
            run_analysis(plan, self.directory / 'failed')
        self.assertFalse((self.directory / 'failed').exists())
