"""Analytic Delphes split-tree execution fixtures (not a live Delphes test)."""
from contextlib import redirect_stdout
from importlib.util import find_spec
import io
import math
from pathlib import Path
import tempfile
import unittest

import yaml

from mgpd.analysis import plan_analysis, run_analysis, analysis_example, render_analysis
from mgpd.cli import main
from mgpd.analysis.engine import calculate_analysis
from test_analysis_engine import analysis_config


@unittest.skipUnless(find_spec('uproot') and find_spec('awkward'), 'requires analysis extra')
class RootAnalysisTests(unittest.TestCase):
    def setUp(self):
        import awkward as ak
        import uproot
        self.ak, self.uproot = ak, uproot
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / 'events.root'
        self.config = analysis_config()
        self.config['input']['format'] = 'root'
        self.config['objects']['electrons'] = {'source': 'Electron'}
        self.columns = {
            'Electron.PT': [[40., 40.], [50., 50.], [], [30., 30.]],
            'Electron.Eta': [[0., 0.], [0., 0.], [], [0., 0.]],
            'Electron.Phi': [[0., math.pi], [0., math.pi], [], [0., math.pi]],
            'Electron.Charge': [[-1., 1.], [-1., 1.], [], [-1., 1.]],
            'Event.Weight': [[2.], [-1.], [3.], [.5]],
        }

    def write(self, scalar=False, empty=False):
        types = {key: 'var * float64' for key in self.columns}
        if scalar:
            types['Event.Weight'] = 'float64'
        with self.uproot.recreate(self.path) as root:
            tree = root.mktree('Delphes', types)
            if not empty:
                tree.extend({key: self.ak.Array(value) if types[key].startswith('var') else [v[0] for v in value]
                             for key, value in self.columns.items()})

    def calculate(self, **options):
        return calculate_analysis(plan_analysis(self.path, self.config, **options))

    def test_weights_mass_cutflow_and_chunks(self):
        self.write()
        first = self.calculate(chunk_size=1)
        second = self.calculate(chunk_size=3)
        self.assertEqual(first['cutflow'], second['cutflow'])
        self.assertEqual(first['histograms'], second['histograms'])
        self.assertEqual([row['n'] for row in first['cutflow']], [4, 3, 3, 1])
        self.assertEqual([row['sumw'] for row in first['cutflow']], [4.5, 1.5, 1.5, 2.])
        self.assertEqual(first['cutflow'][0]['sumw2'], 14.25)
        self.assertEqual(first['batches_processed'], 4)
        self.assertEqual(self.calculate(max_events=2)['events_processed'], 2)
        result = run_analysis(plan_analysis(self.path, self.config), self.directory / 'output')
        self.assertEqual(result['state'], 'completed')

    def test_scalar_weight_and_explicit_unit_policy(self):
        self.write(scalar=True)
        self.assertEqual(self.calculate()['cutflow'][0]['sumw'], 4.5)
        del self.columns['Event.Weight']
        self.write()
        with self.assertRaisesRegex(ValueError, 'Event.Weight'):
            self.calculate()
        self.config['input']['weights'] = {'mode': 'unit'}
        self.assertEqual(self.calculate()['cutflow'][0]['sumw'], 4)

    def test_malformed_shapes_weights_and_nonfinite_fail_without_output(self):
        for branch, value, message in [('Electron.Eta', [0.], 'lengths'),
                                       ('Event.Weight', [], 'exactly one'),
                                       ('Event.Weight', [1., 2.], 'exactly one'),
                                       ('Electron.PT', [float('nan'), 40.], 'nonfinite')]:
            with self.subTest(branch=branch, value=value):
                original = self.columns[branch][0]
                self.columns[branch][0] = value
                self.write()
                with self.assertRaisesRegex(ValueError, message):
                    run_analysis(plan_analysis(self.path, self.config), self.directory / 'failed')
                self.assertFalse((self.directory / 'failed').exists())
                self.columns[branch][0] = original

    def test_empty_tree_and_packaged_example(self):
        self.write(empty=True)
        self.assertEqual(self.calculate()['events_processed'], 0)
        plan = plan_analysis(self.path, yaml.safe_load(analysis_example('delphes-dileptons')))
        self.assertTrue(plan['execution_available'])

    def test_other_collections_and_derived_fields(self):
        from mgpd.analysis.root_reader import iter_root_batches
        for source, mass in [('Muon', .1056583755), ('Jet', 5.), ('Particle', 2.),
                             ('Photon', 0.), ('MissingET', 0.)]:
            with self.subTest(source=source):
                columns = {f'{source}.{field}': [[value]] for field, value in
                           [('MET' if source == 'MissingET' else 'PT', 3.), ('Eta', 0.), ('Phi', 0.)]}
                if source in ('Muon', 'Jet', 'Particle'):
                    columns[f'{source}.Charge'] = [[-1.]]
                if source in ('Jet', 'Particle'):
                    columns[f'{source}.Mass'] = [[mass]]
                self.columns = columns
                self.write()
                config = {'schema_version': 1, 'input': {'weights': {'mode': 'unit'}},
                          'objects': {'items': {'source': source, 'select': 'energy > 0'}},
                          'variables': {'m': 'items.mass'},
                          'histograms': [{'name': 'm', 'variable': 'm', 'fill': 'objects', 'bins': [0, 10]}]}
                plan = plan_analysis(self.path, config)
                batch = next(iter_root_batches(self.path, plan['config'], plan['required_fields'], 2))
                particle = batch[0]['items'][0]
                self.assertAlmostEqual(particle.mass, mass, places=7)
                self.assertAlmostEqual(particle.energy, math.hypot(3., mass))
                self.assertEqual(calculate_analysis(plan)['objects']['items']['selected_objects'], 1)

    def test_objectless_unit_count_and_multifile_limit(self):
        self.write()
        config = {'schema_version': 1, 'input': {'weights': {'mode': 'unit'}},
                  'objects': {}, 'variables': {'one': '1'},
                  'histograms': [{'name': 'one', 'variable': 'one', 'bins': [0, 2]}]}
        self.assertEqual(calculate_analysis(plan_analysis(self.path, config))['events_processed'], 4)
        second = self.directory / 'second.root'
        second.write_bytes(self.path.read_bytes())
        plan = plan_analysis([self.path, second], self.config, max_events=5, chunk_size=2)
        self.assertEqual(calculate_analysis(plan)['events_processed'], 5)

    def test_custom_weight_branch(self):
        self.columns['Generator.Weight'] = self.columns.pop('Event.Weight')
        self.write()
        self.config['input']['weights'] = {'mode': 'nominal', 'branch': 'Generator.Weight'}
        self.assertEqual(self.calculate()['cutflow'][0]['sumw'], 4.5)

    def test_cli_plots_and_render_without_original_root(self):
        self.write()
        config_path = self.directory / 'analysis.yml'
        config_path.write_text(yaml.safe_dump(self.config))
        output = self.directory / 'cli'
        with redirect_stdout(io.StringIO()):
            code = main(['analyze', str(self.path), '--config', str(config_path),
                         '--plots', '-o', str(output)])
        self.assertEqual(code, 0)
        self.assertTrue(list((output / 'plots').glob('*.png')))
        self.path.unlink()
        rendered = render_analysis(output, self.directory / 'rendered')
        self.assertEqual(rendered['events_processed'], 4)
