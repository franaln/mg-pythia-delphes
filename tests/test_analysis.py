"""Analysis stage-one contracts, using independently constructed input headers."""
from contextlib import redirect_stderr, redirect_stdout
import copy
import gzip
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml
from mgpd.analysis import plan_analysis, run_analysis
from mgpd.analysis.config import load_analysis_config
from mgpd.cli import main, _parser
from mgpd.completion import shell_completion

LHE = '''<LesHouchesEvents version="3.0">
<header></header>
<init>
2212 2212 6500 6500 0 0 0 0 -4 1
2.5 0.1 1 1
<generator name="MadGraph">metadata</generator>
</init>
<event>
2 1 2.5 91 0.0073 0.118
11 1 0 0 0 0 40 0 0 40 0 0 9
-11 1 0 0 0 0 -40 0 0 40 0 0 9
</event>
</LesHouchesEvents>
'''


def config():
    return {'schema_version': 1, 'input': {'format': 'lhe'},
            'objects': {'electrons': {'source': 'particles', 'select': '(abs(pdg_id) == 11) & (status == 1)'}},
            'variables': {'m_ee': 'mass(dilepton)', 'dilepton': 'leading_os_pair(electrons)',
                          'n_electrons': 'count(electrons)'},
            'cuts': [{'name': 'two_electrons', 'expression': 'n_electrons >= 2'},
                     {'name': 'mass_window', 'expression': '(m_ee > 60) & (m_ee < 120)'}],
            'histograms': [{'name': 'mass_plot', 'variable': 'm_ee',
                            'bins': {'count': 60, 'min': 0, 'max': 180}}]}


class AnalysisContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.lhe = self.root / 'events.lhe'; self.lhe.write_text(LHE)
        self.yaml = self.root / 'analysis.yml'; self.yaml.write_text(yaml.safe_dump(config()))

    def call(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = main(['analyze', str(self.lhe), '--config', str(self.yaml), *args])
        return result, stdout.getvalue(), stderr.getvalue()

    def test_plan_records_effective_policy_metadata_hashes_and_topology_without_writes(self):
        before = sorted(self.root.iterdir())
        original = config()
        plan = plan_analysis([self.lhe], original)
        self.assertEqual(original, config())
        self.assertEqual(plan['state'], 'planned')
        self.assertTrue(plan['execution_available'])
        self.assertEqual(plan['config']['input']['weights']['mode'], 'nominal')
        self.assertEqual(plan['config']['input']['normalization']['mode'], 'raw')
        self.assertEqual(plan['inputs'][0]['metadata']['cross_section_pb'], 2.5)
        self.assertEqual(plan['inputs'][0]['metadata']['weight_strategy'], -4)
        self.assertLess(plan['variable_order'].index('dilepton'), plan['variable_order'].index('m_ee'))
        self.assertEqual(sorted(self.root.iterdir()), before)
        self.assertEqual(plan, plan_analysis(self.lhe, config()))
        self.assertNotEqual(plan['plan_sha256'], plan_analysis(self.lhe, config(), chunk_size=2)['plan_sha256'])
        json.dumps(plan, allow_nan=False)

    def test_plain_and_gzip_headers_are_equivalent(self):
        compressed = self.root / 'events.lhe.gz'; compressed.write_bytes(gzip.compress(LHE.encode()))
        first = plan_analysis(self.lhe, config()); second = plan_analysis(compressed, config())
        self.assertEqual(first['inputs'][0]['metadata'], second['inputs'][0]['metadata'])
        self.assertEqual(second['format'], 'lhe')

    def test_duplicate_symlink_mixed_format_and_directory_inputs_fail(self):
        alias = self.root / 'alias.lhe'; alias.symlink_to(self.lhe)
        root = self.root / 'events.root'; root.write_bytes(b'not root')
        for inputs, message in [([self.lhe, alias], 'Duplicate'), ([self.lhe, root], 'representation'),
                                ([self.root], 'explicit files')]:
            with self.subTest(inputs=inputs), self.assertRaisesRegex(ValueError, message):
                plan_analysis(inputs, config())
        with self.assertRaisesRegex(ValueError, 'format conflicts'):
            plan_analysis(self.lhe, config(), format='root')

    def test_rejects_malformed_headers_and_wrong_xml_root(self):
        for text in ('<garbage/>', '<LesHouchesEvents><event/></LesHouchesEvents>',
                     '<LesHouchesEvents><init>bad</init></LesHouchesEvents>', '<LesHouchesEvents>'):
            self.lhe.write_text(text)
            with self.subTest(text=text), self.assertRaises(ValueError):
                plan_analysis(self.lhe, config())

    def test_rejects_unknown_keys_bad_bins_stage_names_and_duplicate_yaml(self):
        cases = []
        for key, value in [('schema_version', True), ('input', {'weights': {'mode': 'auto'}}),
                           ('input', {'normalization': {'mode': 'luminosity'}}), ('extra', 1)]:
            item = config(); item[key] = value; cases.append(item)
        for bins in ({'count': True, 'min': 0, 'max': 1}, [0, 2, 1], [0, float('nan')]):
            item = config(); item['histograms'][0]['bins'] = bins; cases.append(item)
        item = config(); item['histograms'][0]['stage'] = 'missing'; cases.append(item)
        item = config(); item['cuts'].append(copy.deepcopy(item['cuts'][0])); cases.append(item)
        for item in cases:
            with self.subTest(item=item), self.assertRaises(ValueError):
                load_analysis_config(item)
        self.yaml.write_text('schema_version: 1\nschema_version: 1\n')
        with self.assertRaisesRegex(ValueError, 'unique'):
            load_analysis_config(self.yaml)

    def test_expression_restrictions_and_cycles(self):
        for expression in ("__import__('os')", 'electrons.__class__', 'electrons.pt.real',
                           '[x for x in electrons]', 'electrons[0]', 'unknown + 1',
                           'mass()', 'mass(electrons, foo=1)', '1e999', 'abs', '2 ** 1000000'):
            item = config(); item['variables']['m_ee'] = expression
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                load_analysis_config(item)
        item = config(); item['variables']['dilepton'] = 'm_ee'
        with self.assertRaisesRegex(ValueError, 'cyclic'):
            load_analysis_config(item)

    def test_cli_validation_and_missing_runtime_never_create_outputs(self):
        result, output, error = self.call('--validate-only')
        self.assertEqual(result, 0, error)
        self.assertEqual(json.loads(output)['format'], 'lhe')
        destination = self.root / 'results'
        with patch('mgpd.analysis.dependencies.find_spec', return_value=None):
            result, _, error = self.call('-o', str(destination))
        self.assertEqual(result, 1)
        self.assertIn('mg-pythia-delphes[analysis]', error)
        self.assertFalse(destination.exists())
        # Execution is exercised with real optional dependencies in test_analysis_engine.


    def test_execution_refuses_changed_plan_existing_output_and_invalid_limits(self):
        plan = plan_analysis(self.lhe, config())
        changed = copy.deepcopy(plan); changed['chunk_size'] = 2
        with self.assertRaisesRegex(ValueError, 'changed'):
            run_analysis(changed, self.root / 'results')
        with self.assertRaises(FileExistsError):
            run_analysis(plan, self.root)
        for kwargs in ({'chunk_size': 0}, {'chunk_size': True}, {'max_events': -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                plan_analysis(self.lhe, config(), **kwargs)
        self.assertTrue(plan_analysis(self.lhe, config(), max_events=10)['preview_requested'])

    def test_object_momentum_filters_and_histogram_edges_are_valid(self):
        item = config()
        item['objects']['electrons']['select'] = '(pt > 10) & (abs(eta) < 2.5)'
        item['histograms'][0]['bins'] = [0, 60, 120, 180]
        effective, _ = load_analysis_config(item)
        self.assertEqual(effective['histograms'][0]['bins'], [0, 60, 120, 180])

    def test_root_required_branches_and_no_silent_weight_fallback(self):
        try:
            import uproot
        except ImportError:
            self.skipTest('ROOT planning test requires optional Uproot')
        path = self.root / 'events.root'
        with uproot.recreate(path) as root:
            root.mktree('Delphes', {'Electron.PT': 'var * float64', 'Electron.Eta': 'var * float64',
                                   'Electron.Phi': 'var * float64', 'Electron.Charge': 'var * float64'})
        item = config(); item['input']['format'] = 'root'
        item['objects']['electrons'] = {'source': 'Electron'}
        with self.assertRaisesRegex(ValueError, 'Event.Weight'):
            plan_analysis(path, item)
        item['input']['weights'] = {'mode': 'unit'}
        plan = plan_analysis(path, item)
        self.assertEqual(plan['inputs'][0]['metadata']['entries'], 0)
        self.assertIn('Electron.Charge', plan['required_fields']['electrons'])
        item['objects']['electrons']['select'] = 'pdg_id == 11'
        with self.assertRaisesRegex(ValueError, 'unsupported fields'):
            plan_analysis(path, item)

    def test_base_import_and_help_do_not_import_numerical_stack(self):
        code = '''import sys
import mgpd
from mgpd.cli import main
main(['analyze', '--help'])
'''
        # argparse exits on help, so inspect laziness separately.
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        code = "import mgpd; import mgpd.analysis; import sys; assert not {'numpy','awkward','vector','uproot','matplotlib'} & set(sys.modules)"
        subprocess.run([sys.executable, '-c', code], check=True, capture_output=True, text=True)
        for shell in ('bash', 'zsh'):
            completion = shell_completion(_parser(), shell)
            self.assertIn('analyze', completion)
            self.assertIn('--validate-only', completion)
