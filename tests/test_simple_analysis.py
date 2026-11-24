"""SimpleAnalysis backend contracts with faked containers (real runs are live checks)."""
import ast
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from mgpd.analysis import simple_analysis as sa
from mgpd.cli import parse_args
from mgpd.templates import _resource

LISTING = """
RestFrames v1.0.0 -- Developed by Christopher Rogan
xAOD::Init                INFO    Environment initialised for data access
MCWeightIndex = 0
Select analyses among:
 ExampleAnalysis
 Diphoton2018
 PhotonJets2016

"""
RESULT = """SR,events,acceptance,err
All,2000,1029.76,531.267
Cat0,154,0.0770771,0.00621105
Cat1,0,0,0
"""


class FakeContainers:
    """Emulate the converter and simpleAnalysis by mapping /work to the staged directory."""

    def __init__(self, *, convert_rc=0, sa_rc=0, entries=3, skip_outputs=()):
        self.convert_rc, self.sa_rc, self.entries, self.skip_outputs = convert_rc, sa_rc, entries, skip_outputs
        self.commands = []

    def __call__(self, arguments, *, timeout=None, log=None):
        self.commands.append(arguments)
        work = Path(next(item.split(':')[0] for item in arguments if item.endswith(':/work')))
        shell = arguments[-1]
        if log is not None:
            log.write_text('log\n', encoding='utf-8')
        if 'delphes2slim.py' in shell:
            if self.convert_rc == 0:
                (work / 'result/slim/ntuple.root').write_bytes(b'slim')
                (work / 'result/slim/conversion.json').write_text(json.dumps({
                    'entries': self.entries, 'limit_reached': False, 'sumw': 1029.7622, 'sumw2': 531.267,
                    'branches': {'ScalarHT': True, 'FatJet': False, 'GenMissingET': True}}), encoding='utf-8')
            return subprocess.CompletedProcess(arguments, self.convert_rc, 'converted', 'missing required Delphes branches: Jet')
        if 'simpleAnalysis -l' in shell:
            return subprocess.CompletedProcess(arguments, 0, LISTING, '')
        names = shell.split(' -a ')[1].split()[0].split(',')
        for name in names:
            for suffix in ('.txt', '.root'):
                if name + suffix not in self.skip_outputs:
                    text = RESULT.replace('2000', str(self.entries))
                    (work / 'result/simple-analysis' / (name + suffix)).write_text(text, encoding='utf-8')
        return subprocess.CompletedProcess(arguments, self.sa_rc, '', 'TList::Clear ... already deleted')


class SimpleAnalysisTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.input = self.root / 'events_delphes_events.root'
        self.input.write_bytes(b'delphes')
        self.fake = FakeContainers()
        for target, value in [('_run', self.fake),
                              ('_pin', lambda reference, backend: (reference, {'kind': 'sif', 'path': reference, 'sha256': 'x'})),
                              ('verify_image', lambda reference, identity: None)]:
            patcher = patch.object(sa, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def plan(self, **options):
        defaults = {'backend': 'local-apptainer', 'image': '/images/mgpd.sif', 'sa_image': '/images/sa.sif'}
        return sa.plan_simple_analysis([self.input], **{**defaults, **options})

    def test_catalog_and_result_parsing(self):
        self.assertEqual(sa.parse_catalog(LISTING), ['ExampleAnalysis', 'Diphoton2018', 'PhotonJets2016'])
        for bad in ('no catalog here', 'Select analyses among:\n\n', 'Select analyses among:\n A\n A\n'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                sa.parse_catalog(bad)
        parsed = sa.parse_results(RESULT)
        self.assertEqual(parsed['all'], {'region': 'All', 'events': 2000, 'sumw': 1029.76, 'sumw2': 531.267})
        self.assertEqual(parsed['regions'][0], {'region': 'Cat0', 'events': 154, 'acceptance': 0.0770771,
                                                'acceptance_error': 0.00621105})
        for bad in ('', 'SR,yield\nAll,1,1,1', 'SR,events,acceptance,err\nAll,1,nan,1', 'SR,events,acceptance,err\nAll,1'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                sa.parse_results(bad)

    def test_container_commands_bind_inputs_read_only(self):
        command = sa.container_command('local-apptainer', '/i.sif', 'source /s.sh', 'run', work='/w',
                                       binds=[('/data/a b.root', '/input/000.root')])
        self.assertEqual(command[:5], ['apptainer', 'exec', '--bind', '/w:/work', '--bind'])
        self.assertEqual(command[5], '/data/a b.root:/input/000.root:ro')
        self.assertEqual(command[-3:-1], ['/bin/bash', '-c'])
        docker = sa.container_command('local-docker', 'img@sha256:' + 'a' * 64, 'setup', 'run', work='/w')
        self.assertEqual(docker[:3], ['docker', 'run', '--rm'])
        self.assertIn('/w:/work', docker)
        with self.assertRaises(ValueError):
            sa.container_command('podman', 'x', 'setup', 'run', work='/w')

    def test_met_convention_follows_card_provenance(self):
        atlas = sa._packaged_atlas_card_sha256()
        self.assertEqual(sa.resolve_met(None, None), 'muon-corrected')
        self.assertEqual(sa.resolve_met(None, {'delphes_cards': {'cards/delphes_card.dat': atlas}}), 'muon-corrected')
        other = {'delphes_cards': {'cards/delphes_card.dat': '0' * 64}}
        with self.assertRaisesRegex(ValueError, '--met'):
            sa.resolve_met(None, other)
        self.assertEqual(sa.resolve_met('delphes', other), 'delphes')
        with self.assertRaises(ValueError):
            sa.resolve_met('none', None)

    def test_plan_validates_names_inputs_and_catalog(self):
        plan = self.plan(analyses='Diphoton2018,PhotonJets2016', max_events=10)
        self.assertEqual(plan['execution']['analyses'], ['Diphoton2018', 'PhotonJets2016'])
        self.assertEqual(plan['execution']['arguments'], ['simpleAnalysis', '-a', 'Diphoton2018,PhotonJets2016', '-w', '0'])
        self.assertEqual(plan['conversion']['met'], 'muon-corrected')
        self.assertEqual(plan['conversion']['contract']['name'], 'delphes-slim-v1')
        self.assertEqual(plan['inputs'][0]['path'], str(self.input))
        self.assertEqual(len(plan['warnings']), 2)
        conversion_only = self.plan(sa_image=None)
        self.assertIsNone(conversion_only['execution'])
        for options, message in [({'analyses': 'Nope'}, 'catalog'), ({'analyses': 'A;rm'}, 'identifiers'),
                                 ({'analyses': 'Diphoton2018,Diphoton2018'}, 'Duplicate'),
                                 ({'analyses': 'Diphoton2018', 'sa_image': None}, 'sa-image'),
                                 ({'slim_input': True}, 'at least one analysis'),
                                 ({'slim_input': True, 'analyses': 'Diphoton2018', 'met': 'delphes'}, 'slim'),
                                 ({'max_events': 0}, 'positive'), ({'backend': 'podman'}, 'backend')]:
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, message):
                self.plan(**options)

    def test_run_publishes_complete_directory(self):
        plan = self.plan(analyses='Diphoton2018,ExampleAnalysis')
        report = sa.run_simple_analysis(plan, self.root / 'out')
        out = self.root / 'out'
        files = {str(path.relative_to(out)) for path in out.rglob('*') if path.is_file()}
        self.assertEqual(files, {'simple-analysis-report.json', 'results.json', 'slim/ntuple.root', 'slim/conversion.json',
                                 'logs/conversion.log', 'logs/simple-analysis.log',
                                 *(f'simple-analysis/{name}{suffix}' for name in ('Diphoton2018', 'ExampleAnalysis')
                                   for suffix in ('.txt', '.root'))})
        saved = json.loads((out / 'simple-analysis-report.json').read_text())
        self.assertEqual(saved['plan_sha256'], plan['plan_sha256'])
        self.assertEqual(report['results']['Diphoton2018']['regions'][0]['events'], 154)
        self.assertEqual({entry['path'] for entry in saved['artifacts']}, files - {'simple-analysis-report.json'})
        # All-row events (3) equal converted entries; sumw agrees within text precision.
        self.assertTrue(all(report['input_sumw_matches'].values()))
        self.assertFalse(any('differ from' in warning for warning in report['warnings']))
        listing, converter, runner = self.fake.commands
        self.assertIn("simpleAnalysis -l", listing[-1])
        self.assertIn(f'{self.input}:/input/000.root:ro', converter)
        self.assertIn('-w 0 /work/result/slim/ntuple.root', runner[-1])
        self.assertNotIn(' -L', runner[-1])
        self.assertEqual(list(self.root.glob('.mgpd-sa-*')), [])
        with self.assertRaises(FileExistsError):
            sa.run_simple_analysis(plan, out)

    def test_slim_inputs_skip_conversion(self):
        plan = self.plan(analyses='Diphoton2018', slim_input=True, image=None, max_events=5)
        self.assertIsNone(plan['conversion'])
        sa.run_simple_analysis(plan, self.root / 'out')
        runner = self.fake.commands[-1]
        self.assertEqual(len(self.fake.commands), 2)  # catalog listing, then the run
        self.assertIn('--nevents 5 --input-files /input/000.root', runner[-1])

    def test_failures_publish_nothing(self):
        cases = [(FakeContainers(convert_rc=1), 'Conversion failed'),
                 (FakeContainers(sa_rc=1), 'SimpleAnalysis failed'),
                 (FakeContainers(skip_outputs=('Diphoton2018.root',)), 'missing')]
        for fake, message in cases:
            with self.subTest(message=message), patch.object(sa, '_run', fake):
                plan = self.plan(analyses='Diphoton2018')
                with self.assertRaisesRegex(ValueError, message):
                    sa.run_simple_analysis(plan, self.root / 'out')
                self.assertFalse((self.root / 'out').exists())
                self.assertEqual(list(self.root.glob('.mgpd-sa-*')), [])

    def test_changed_plan_or_input_is_refused(self):
        plan = self.plan(analyses='Diphoton2018')
        tampered = {**plan, 'max_events': 1}
        with self.assertRaisesRegex(ValueError, 'changed'):
            sa.run_simple_analysis(tampered, self.root / 'out')
        self.input.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'changed'):
            sa.run_simple_analysis(plan, self.root / 'out')
        self.assertFalse((self.root / 'out').exists())

    def test_managed_event_count_mismatch_is_an_error(self):
        plan = self.plan(analyses='Diphoton2018')
        plan['dataset'] = {'directory': str(self.root / 'merged'), 'expected_events': 4, 'metadata': []}
        plan['plan_sha256'] = sa._checksum(plan)
        with self.assertRaisesRegex(ValueError, 'records 4'):
            sa.run_simple_analysis(plan, self.root / 'out')
        self.assertFalse((self.root / 'out').exists())

    def test_cli_option_rules(self):
        valid = [['analyze', 'in.root', '--simple-analysis', 'A', '--sa-image', 'sa.sif', '-o', 'out'],
                 ['analyze', 'in.root', '--to-slim', '--met', 'delphes', '-o', 'out'],
                 ['analyze', 'in.root', '--slim', '--simple-analysis', 'A', '--sa-image', 'sa.sif', '--validate-only'],
                 ['analyze', '--sa-list', '--sa-image', 'sa.sif']]
        for argv in valid:
            with self.subTest(argv=argv):
                parse_args(argv)
        invalid = [['analyze', 'in.root', '--simple-analysis', 'A', '-o', 'out'],
                   ['analyze', 'in.root', '--to-slim', '--simple-analysis', 'A', '--sa-image', 's', '-o', 'o'],
                   ['analyze', 'in.root', '--slim', '--simple-analysis', 'A', '--sa-image', 's', '--met', 'delphes', '-o', 'o'],
                   ['analyze', 'in.root', '--to-slim', '--config', 'a.yml', '-o', 'out'],
                   ['analyze', 'in.root', '--to-slim', '--plots', '-o', 'out'],
                   ['analyze', 'in.root', '--to-slim', '--format', 'lhe', '-o', 'out'],
                   ['analyze', 'in.root', '--config', 'a.yml', '--met', 'delphes', '-o', 'out'],
                   ['analyze', 'in.root', '--to-slim'],
                   ['analyze', '--sa-list'],
                   ['analyze', 'in.root', '--sa-list', '--sa-image', 's']]
        for argv in invalid:
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parse_args(argv)

    def test_packaged_converter_matches_contract(self):
        source = _resource(sa.CONVERTER).read_text(encoding='utf-8')
        tree = ast.parse(source)
        constants = {node.targets[0].id: ast.literal_eval(node.value) for node in tree.body
                     if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                     and node.targets[0].id in ('GOOD_JET', 'BTAG_ALL', 'ALL_ID', 'TAU_ALL', 'REQUIRED_BRANCHES')}
        self.assertEqual(constants['GOOD_JET'], 0x0007D700)
        self.assertFalse(constants['GOOD_JET'] & (1 << 11), 'LessThan3Tracks must not be set')
        self.assertEqual(constants['BTAG_ALL'], 0x00F000FF)
        self.assertEqual(constants['ALL_ID'], 0x7FFFFFFF)
        self.assertNotIn('ScalarHT', constants['REQUIRED_BRANCHES'])
        self.assertIn('--summary', source)
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'Delphes2SA.py'
        if script.exists():
            self.assertEqual(script.read_bytes(), source.encode())


if __name__ == '__main__':
    unittest.main()
