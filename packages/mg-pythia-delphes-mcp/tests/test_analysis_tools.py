"""Real stdio, real scalar engines and durable analysis lifecycle."""
import asyncio
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml
from mcp import Client, StdioServerParameters
from mgpd.analysis import analysis_example, plan_analysis, run_analysis
from mgpd_mcp import analysis_tools
from mgpd_mcp.paths import Workspace
from mgpd_mcp.worker import dispatch


def lhe():
    header = '<LesHouchesEvents><init>\n2212 2212 6500 6500 0 0 0 0 -3 1\n1 .1 1 1\n</init>\n'
    events = []
    for weight, p, opposite in [(2, 40, True), (-1, 50, True), (.5, 30, False)]:
        events.append(f'<event>\n2 1 {weight} 91 .007 .1\n11 1 0 0 0 0 {p} 0 0 {p} 0 0 9\n'
                      f'{-11 if opposite else 11} 1 0 0 0 0 {-p} 0 0 {p} 0 0 9\n</event>\n')
    return header + ''.join(events) + '</LesHouchesEvents>\n'


class AnalysisTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.input = self.root / 'events.lhe.gz'
        self.input.write_bytes(gzip.compress(lhe().encode()))
        self.config = self.root / 'analysis.yml'
        self.config.write_text(analysis_example('dileptons-lhe'))
        self.options = {'inputs': ['events.lhe.gz'], 'config': 'analysis.yml', 'output': 'result'}

    def client(self, allow=False, timeout=30):
        args = ['-m', 'mgpd_mcp.server', '--workspace', str(self.root), '--timeout', str(timeout)]
        if allow:
            args.append('--allow-analysis')
        return Client(StdioServerParameters(command=sys.executable, args=args,
                      env={'MPLCONFIGDIR': str(self.root / '.plot-cache')}), read_timeout_seconds=60)

    async def call(self, client, name, options):
        result = await client.call_tool(name, options)
        self.assertFalse(result.is_error, result)
        return result.structured_content['data']

    async def poll(self, client, output, operation_id):
        for _ in range(150):
            result = await self.call(client, 'analysis_status', {'output': output, 'operation_id': operation_id})
            if 'finished_at' in result:
                return result
            await asyncio.sleep(.05)
        self.fail('Analysis did not finish')

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    async def test_read_only_review_schema_and_base_directory_no_writes(self):
        before = self.snapshot()
        async with self.client() as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            self.assertNotIn('start_analysis', tools)
            self.assertTrue(tools['plan_analysis'].annotations.read_only_hint)
            schema = json.loads((await client.read_resource('mgpd://analysis')).contents[0].text)
            self.assertEqual(set(schema['examples']), {'dileptons-lhe.yml', 'dileptons-root.yml', 'dileptons-lhco.yml'})
            capability = json.loads((await client.read_resource('mgpd://capabilities')).contents[0].text)
            self.assertFalse(capability['analysis_enabled'])
            plan = await self.call(client, 'plan_analysis', {**self.options, 'limit': 1})
            expected = plan_analysis([self.input], self.config)
            self.assertEqual(plan['plan_sha256'], expected['plan_sha256'])
            self.assertEqual(plan['inputs'][0]['path'], 'events.lhe.gz')
            self.assertEqual(plan['histograms_total'], 2)
            self.assertEqual(len(plan['cuts']), 1)
            self.assertEqual(plan['cuts_total'], 3)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(dispatch({'workspace': str(self.root), 'operation': 'start_analysis'})['error']['code'], 'ANALYSIS_DISABLED')
        sub = self.root / 'project'; sub.mkdir()
        self.input.rename(sub / self.input.name); self.config.rename(sub / self.config.name)
        async with self.client() as client:
            plan = await self.call(client, 'plan_analysis', {**self.options, 'base_directory': 'project'})
            self.assertEqual(plan['output'], 'project/result')
            self.assertEqual(plan['config_path'], 'project/analysis.yml')

    async def test_disconnect_reconnect_duplicate_and_cli_api_parity(self):
        options = {**self.options, 'plots': True}
        before = hashlib.sha256(self.input.read_bytes()).hexdigest()
        async with self.client(True) as client:
            review = await self.call(client, 'plan_analysis', options)
            start = {**options, 'analysis_sha256': review['analysis_sha256']}
            operation = await self.call(client, 'start_analysis', start)
        async with self.client() as client:
            state = await self.poll(client, 'result', operation['operation_id'])
            self.assertEqual(state['state'], 'completed', state)
            report = await self.call(client, 'analysis_report', {'output': 'result', 'operation_id': operation['operation_id'], 'limit': 1})
            self.assertEqual(report['events_processed'], 3)
            self.assertEqual(report['histograms_total'], 2)
            self.assertEqual(len(report['cutflow']), 1)
            second = await self.call(client, 'analysis_report', {'output': 'result', 'operation_id': operation['operation_id'], 'offset': 1, 'limit': 1})
            self.assertEqual(second['cutflow'][0]['stage'], 'two_electrons')
        async with self.client(True) as client:
            again = await self.call(client, 'start_analysis', start)
            self.assertEqual(again['operation_id'], operation['operation_id'])
            self.assertEqual(again['state'], 'completed')
        self.assertEqual(before, hashlib.sha256(self.input.read_bytes()).hexdigest())
        self.assertTrue(list((self.root/'result/plots').glob('*.png')))
        api = self.root / 'api'
        run_analysis(plan_analysis([self.input], self.config), api)
        subprocess.run([sys.executable, '-m', 'mgpd', 'analyze', str(self.input), '--config', str(self.config),
                        '-o', str(self.root/'cli')], cwd=self.root, check=True, capture_output=True)
        for name in ('cutflow.json', 'histograms.json'):
            self.assertEqual(json.loads((api/name).read_text()), json.loads((self.root/'result'/name).read_text()))
            self.assertEqual(json.loads((api/name).read_text()), json.loads((self.root/'cli'/name).read_text()))
        self.input.unlink()
        async with self.client() as client:
            report = await self.call(client, 'analysis_report', {'output': 'result', 'operation_id': operation['operation_id']})
            self.assertEqual(report['events_processed'], 3)

    async def test_concurrent_starts_and_review_mutations(self):
        async with self.client(True) as client:
            review = await self.call(client, 'plan_analysis', self.options)
            start = {**self.options, 'analysis_sha256': review['analysis_sha256']}
            original = self.config.read_bytes()
            self.config.write_bytes(original + b'\n# changed source\n')
            rejected = await client.call_tool('start_analysis', start)
            self.assertTrue(rejected.is_error)
            self.config.write_bytes(original)
            original_input = self.input.read_bytes()
            self.input.write_bytes(gzip.compress(lhe().replace('2 1 2 ', '2 1 3 ').encode()))
            self.assertTrue((await client.call_tool('start_analysis', start)).is_error)
            self.input.write_bytes(original_input)
            self.assertTrue((await client.call_tool('start_analysis', {**start, 'plots': True})).is_error)
            self.assertFalse((self.root/'result').exists())
            self.assertFalse(list(self.root.glob('.mgpd-mcp-analysis-*')))
            results = await asyncio.gather(*[client.call_tool('start_analysis', start) for _ in range(2)])
            successes = [r.structured_content['data'] for r in results if not r.is_error]
            self.assertTrue(successes)
            self.assertEqual(len({r['operation_id'] for r in successes}), 1)
            state = await self.poll(client, 'result', successes[0]['operation_id'])
            self.assertEqual(state['state'], 'completed', state)
        self.assertEqual(len(list(self.root.glob('.mgpd-mcp-analysis-*'))), 1)

    async def test_failed_reader_no_publication_and_no_automatic_retry(self):
        self.input.write_bytes(gzip.compress(lhe().replace('</LesHouchesEvents>', '<event>\n').encode()))
        async with self.client(True) as client:
            review = await self.call(client, 'plan_analysis', self.options)
            start = {**self.options, 'analysis_sha256': review['analysis_sha256']}
            operation = await self.call(client, 'start_analysis', start)
            state = await self.poll(client, 'result', operation['operation_id'])
            self.assertEqual(state['state'], 'failed', state)
            self.assertFalse((self.root/'result').exists())
            again = await self.call(client, 'start_analysis', start)
            self.assertEqual(again['state'], 'failed')
            self.assertEqual(again['finished_at'], state['finished_at'])

    async def test_workspace_symlink_metadata_and_state_guards(self):
        with tempfile.TemporaryDirectory() as outside:
            outside = Path(outside)
            external = outside/'events.lhe.gz'; external.write_bytes(self.input.read_bytes())
            (self.root/'escape').symlink_to(outside, target_is_directory=True)
            dataset = self.root/'dataset'; dataset.mkdir()
            (dataset/'merge-report.json').write_text(json.dumps({'outputs': [{'path': str(external)}]}))
            async with self.client(True) as client:
                for changed in ({'inputs': ['escape/events.lhe.gz']}, {'config': 'escape/analysis.yml'},
                                {'output': '../result'}, {'inputs': ['dataset']}):
                    result = await client.call_tool('plan_analysis', {**self.options, **changed})
                    self.assertTrue(result.is_error)
                    self.assertEqual(result.structured_content['error']['code'], 'PATH_OUTSIDE_WORKSPACE')
                review = await self.call(client, 'plan_analysis', self.options)
                (self.root/('.mgpd-mcp-'+review['operation_id'])).symlink_to(outside, target_is_directory=True)
                self.assertTrue((await client.call_tool('start_analysis', {**self.options, 'analysis_sha256': review['analysis_sha256']})).is_error)
            self.assertFalse(list(outside.glob('result*')))

    async def test_root_lhco_and_explicit_physical_normalization(self):
        import awkward as ak
        import uproot
        for fmt, example in [('root', 'dileptons-root'), ('lhco', 'dileptons-lhco')]:
            config = yaml.safe_load(analysis_example(example))
            config['input']['weights'] = {'mode': 'unit'}
            config['input']['normalization'] = {'mode': 'luminosity', 'weight_convention': 'dimensionless',
                'cross_section_pb': 2., 'sumw_generated': 1., 'luminosity_pb_inverse': 100.}
            path = self.root/f'analysis-{fmt}.yml'; path.write_text(yaml.safe_dump(config))
            events = self.root/f'events.{fmt}'
            if fmt == 'root':
                columns = {'Electron.PT': [[40.,40.]], 'Electron.Eta': [[0.,0.]],
                           'Electron.Phi': [[0.,3.141592653589793]], 'Electron.Charge': [[-1.,1.]]}
                with uproot.recreate(events) as root:
                    tree = root.mktree('Delphes', {k:'var * float64' for k in columns})
                    tree.extend({k:ak.Array(v) for k,v in columns.items()})
            else:
                events.write_text('0 1 0\n1 1 0 0 40 0 -1 0 0 0 0\n2 1 0 3.141592653589793 40 0 1 0 0 0 0\n')
            options = {'inputs': [events.name], 'config': path.name, 'output': f'result-{fmt}', 'max_events': 1, 'chunk_size': 1}
            async with self.client(True) as client:
                review = await self.call(client, 'plan_analysis', options)
                operation = await self.call(client, 'start_analysis', {**options,'analysis_sha256':review['analysis_sha256']})
                state = await self.poll(client, options['output'], operation['operation_id'])
                self.assertEqual(state['state'], 'completed', state)
            expected = self.root/f'api-{fmt}'
            run_analysis(plan_analysis([events], path, max_events=1, chunk_size=1), expected)
            for name in ('cutflow.json','histograms.json'):
                self.assertEqual(json.loads((expected/name).read_text()),json.loads((self.root/options['output']/name).read_text()))

    def test_launch_failure_unknown_is_durable_and_never_relaunched(self):
        workspace = Workspace(self.root)
        review = dispatch({'workspace': str(self.root), 'operation': 'plan_analysis', **self.options})['data']
        request = {'workspace': str(self.root), 'operation': 'start_analysis', 'allow_analysis': True,
                   **self.options, 'analysis_sha256': review['analysis_sha256']}
        with patch('mgpd_mcp.operations.subprocess.Popen', side_effect=OSError('launch failed')) as launch:
            first = dispatch(request)['data']
            second = dispatch(request)['data']
            self.assertEqual(launch.call_count, 1)
        self.assertEqual(first['state'], 'unknown')
        self.assertEqual(first['operation_id'], second['operation_id'])
        self.assertFalse((self.root/'result').exists())

    async def test_managed_dataset_review_mutation_and_report_integrity(self):
        dataset = self.root/'dataset'; dataset.mkdir()
        merged = dataset/'events.lhe.gz'; merged.write_bytes(self.input.read_bytes())
        metadata = {'schema_version': 1, 'partial': False, 'omitted_jobs': [],
                    'production_id': 'parent', 'plan_sha256': 'plan', 'selection_sha256': 'selection',
                    'samples': {'signal': {'state': 'merged', 'directory': '.', 'formats': ['lhe'],
                        'metrics': {'lhe_events': 3}, 'jobs': [{'job_id': 'one', 'production_id': 'recovery'}]}},
                    'outputs': [{'path': merged.name, 'bytes': merged.stat().st_size,
                                 'sha256': hashlib.sha256(merged.read_bytes()).hexdigest()}]}
        report = dataset/'merge-report.json'; report.write_text(json.dumps(metadata))
        options = {**self.options, 'inputs': ['dataset'], 'sample': 'signal', 'format': 'lhe'}
        async with self.client(True) as client:
            review = await self.call(client, 'plan_analysis', options)
            start = {**options, 'analysis_sha256': review['analysis_sha256']}
            original = report.read_bytes(); report.write_bytes(original+b'\n')
            self.assertTrue((await client.call_tool('start_analysis', start)).is_error)
            report.write_bytes(original)
            operation = await self.call(client, 'start_analysis', start)
            state = await self.poll(client, 'result', operation['operation_id'])
            self.assertEqual(state['state'], 'completed', state)
            result_options = {'output': 'result', 'operation_id': operation['operation_id']}
            verified = await self.call(client, 'analysis_report', result_options)
            self.assertEqual(verified['events_processed'], 3)
            histogram = self.root/'result/histograms.json'
            histogram.write_bytes(histogram.read_bytes()+b'\n')
            result = await client.call_tool('analysis_report', result_options)
            self.assertTrue(result.is_error)
            log = self.root/('.mgpd-mcp-'+operation['operation_id'])/'analysis.log'
            log.write_text('0123456789')
            page = await self.call(client, 'analysis_status', {**result_options, 'log_offset': 2, 'log_limit': 3})
            self.assertEqual((page['log_text'],page['log_next_offset']), ('234',5))

    async def test_analysis_outlives_connection_and_per_call_timeout(self):
        text = lhe(); start=text.index('<event>'); stop=text.rindex('</event>')+len('</event>')
        self.input.write_bytes(gzip.compress((text[:start]+text[start:stop]*20000+'\n</LesHouchesEvents>').encode()))
        async with self.client(True, timeout=1.5) as client:
            review = await self.call(client, 'plan_analysis', self.options)
            operation = await self.call(client, 'start_analysis', {**self.options,'analysis_sha256':review['analysis_sha256']})
        async with self.client() as client:
            state = await self.poll(client, 'result', operation['operation_id'])
            self.assertEqual(state['state'], 'completed', state)
            report = await self.call(client, 'analysis_report', {'output':'result','operation_id':operation['operation_id']})
            self.assertEqual(report['events_processed'],60000)

    async def test_timeout_is_explicitly_unknown_for_start(self):
        async with self.client() as client:
            review = await self.call(client, 'plan_analysis', self.options)
        async with self.client(True, timeout=.0001) as client:
            response = await client.call_tool('start_analysis', {**self.options,'analysis_sha256':review['analysis_sha256']})
            self.assertTrue(response.is_error)
            self.assertEqual(response.structured_content['error']['code'], 'ANALYSIS_UNKNOWN')
        self.assertFalse((self.root/'result').exists())
