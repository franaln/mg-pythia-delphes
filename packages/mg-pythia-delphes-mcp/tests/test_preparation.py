"""Real MCP preparation, real core plans; physics/submission executables forbidden."""
import asyncio
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import Client, StdioServerParameters
import mgpd
import yaml
import test_stdio as fixtures


class PreparationTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.StdioTests.setUp
    snapshot = fixtures.StdioTests.snapshot

    def client(self, allow=True, timeout=30):
        tools = self.root / 'tools'
        tools.mkdir(exist_ok=True)
        for name in ('mg5_aMC', 'condor_submit', 'docker', 'apptainer'):
            executable = tools / name
            executable.write_text(f'#!/bin/sh\ntouch "{self.root}/FORBIDDEN_EXECUTION"\nexit 99\n')
            executable.chmod(0o755)
        args = ['-m', 'mgpd_mcp.server', '--workspace', str(self.root), '--timeout', str(timeout)]
        if allow:
            args.append('--allow-prepare')
        return Client(StdioServerParameters(command=sys.executable, args=args,
                                            env={'PATH': str(tools) + os.pathsep + os.environ['PATH']}))

    def config(self, mode='local-apptainer', scan=False):
        base = self.root / 'project'
        base.mkdir(exist_ok=True)
        (base / 'card.dat').write_text('')
        data = {'run': {'name': 'sample', 'mode': mode, 'image': 'mg-pythia-delphes-3.5.6',
                        'njobs': 2, 'nevents': 2, 'outputs': ['lhe']},
                'cards': {'run': 'card.dat'}, 'process': 'generate p p > e+ e-',
                'options': {'seed': 101}}
        if scan:
            data['cards']['param'] = {'a': 'card.dat', 'b': 'card.dat'}
        (base / 'config.yml').write_text(yaml.safe_dump(data))
        return data

    async def test_opt_in_tools_annotations_and_no_execution_tools(self):
        async with self.client(allow=False) as client:
            self.assertNotIn('prepare_production', [tool.name for tool in (await client.list_tools()).tools])
            result = await client.call_tool('prepare_production', {'path': 'config.yml', 'output': 'out'})
            self.assertTrue(result.is_error)
        async with self.client() as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            self.assertEqual(len(tools), 12)
            for name in ('prepare_production', 'prepare_recovery'):
                self.assertFalse(tools[name].annotations.read_only_hint)
                self.assertFalse(tools[name].annotations.idempotent_hint)
            self.assertNotIn('start_execution', tools)
            cap = json.loads((await client.read_resource('mgpd://capabilities')).contents[0].text)
            self.assertTrue(cap['preparation_enabled'])
            self.assertFalse(cap['read_only'])

    async def test_preparation_parity_for_local_and_condor_param_scan(self):
        for mode in ('local-apptainer', 'local-docker', 'condor'):
            with self.subTest(mode=mode):
                data = self.config(mode, scan=True)
                output = f'prepared-{mode}'
                async with self.client() as client:
                    prepared = await client.call_tool('prepare_production',
                        {'path': 'config.yml', 'base_directory': 'project', 'output': output, 'limit': 1})
                self.assertFalse(prepared.is_error, prepared)
                result = prepared.structured_content['data']
                self.assertEqual(result['state'], 'prepared')
                self.assertFalse(result['submitted'])
                self.assertEqual(result['jobs_total'], 4)
                self.assertTrue(result['jobs_truncated'])
                self.assertEqual(result['samples'], ['sample_a'])
                self.assertEqual(result['samples_total'], 2)
                # Baseline core preparation with absolute source paths produces
                # the same worker/card/seed files, without changing global cwd.
                data['cards'] = {key: ({name: str(self.root / 'project' / value) for name, value in values.items()}
                                     if isinstance(values, dict) else str(self.root / 'project' / values))
                                 for key, values in data['cards'].items()}
                with redirect_stdout(io.StringIO()):
                    reference = mgpd.prepare_production(data, self.root / f'baseline-{mode}')
                actual = mgpd.load_prepared_production(self.root / output)
                self.assertEqual(actual.manifest['jobs'], reference.manifest['jobs'])
                for job in actual.manifest['jobs']:
                    self.assertEqual((Path(actual.output_dir) / job['run_file']).read_bytes(),
                                     (Path(reference.output_dir) / job['run_file']).read_bytes())
                self.assertNotIn('execution_attempt', actual.manifest)
                self.assertNotIn('submission', actual.manifest)
                self.assertFalse((self.root / 'FORBIDDEN_EXECUTION').exists())

    async def test_new_output_only_concurrent_requests_and_workspace_boundaries(self):
        self.config()
        async with self.client() as client:
            calls = await asyncio.gather(*[client.call_tool('prepare_production',
                {'path': 'config.yml', 'base_directory': 'project', 'output': 'same-output'}) for _ in range(2)])
            self.assertEqual(sum(not result.is_error for result in calls), 1)
            before = {str(p): p.read_bytes() for p in (self.root / 'same-output').rglob('*') if p.is_file()}
            repeated = await client.call_tool('prepare_production',
                {'path': 'config.yml', 'base_directory': 'project', 'output': 'same-output'})
            self.assertEqual(repeated.structured_content['error']['code'], 'OUTPUT_EXISTS')
            after = {str(p): p.read_bytes() for p in (self.root / 'same-output').rglob('*') if p.is_file()}
            self.assertEqual(before, after)
            for output in ('../outside', 'production/nested'):
                rejected = await client.call_tool('prepare_production',
                    {'path': 'config.yml', 'base_directory': 'project', 'output': output})
                self.assertTrue(rejected.is_error)
            with tempfile.TemporaryDirectory() as outside:
                (self.root / 'escape').symlink_to(outside, target_is_directory=True)
                rejected = await client.call_tool('prepare_production',
                    {'path': 'config.yml', 'base_directory': 'project', 'output': 'escape/new'})
                self.assertEqual(rejected.structured_content['error']['code'], 'PATH_OUTSIDE_WORKSPACE')

    async def test_complete_input_sources_and_nested_output_rejection(self):
        base = self.root / 'inputs'; base.mkdir()
        (base / 'run.mg5').write_text('generate p p > e+ e-\noutput RUN\nlaunch RUN\ndone\n')
        (base / 'card.dat').write_text('')
        forms = [{'input_dir': 'inputs'}, {'input_dirs': {'a': 'inputs', 'b': 'inputs'}},
                 {'input_files': ['inputs/run.mg5', 'inputs/card.dat']}]
        async with self.client() as client:
            for i, source in enumerate(forms):
                config = {'run': {'name': 'inputs', 'mode': 'local-apptainer', 'image': 'test.sif',
                                  'outputs': ['lhe'], 'njobs': 1, 'nevents': 2}, **source}
                (self.root / f'inputs{i}.yml').write_text(yaml.safe_dump(config))
                result = await client.call_tool('prepare_production', {'path': f'inputs{i}.yml', 'output': f'source{i}'})
                self.assertFalse(result.is_error, result)
                self.assertEqual(result.structured_content['data']['jobs_total'], 2 if i == 1 else 1)
            bad = await client.call_tool('prepare_production', {'path': 'inputs0.yml', 'output': 'inputs/nested'})
            self.assertTrue(bad.is_error)
            self.assertFalse((base / 'nested').exists())

    async def test_preparation_timeout_reports_unknown_without_submitting(self):
        self.config()
        async with self.client(timeout=0.001) as client:
            result = await client.call_tool('prepare_production',
                {'path': 'config.yml', 'base_directory': 'project', 'output': 'interrupted'})
            self.assertTrue(result.is_error)
            self.assertEqual(result.structured_content['error']['code'], 'PREPARATION_UNKNOWN')
        self.assertFalse((self.root / 'FORBIDDEN_EXECUTION').exists())

    async def test_recursive_input_symlink_is_rejected_before_creating_output(self):
        base = self.root / 'inputs'; base.mkdir()
        (base / 'run.mg5').write_text('generate p p > e+ e-\nlaunch RUN\n')
        with tempfile.TemporaryDirectory() as outside:
            (base / 'escape').symlink_to(outside, target_is_directory=True)
            config = {'run': {'name': 'input', 'mode': 'local-apptainer', 'image': 'test.sif', 'outputs': ['lhe']},
                      'input_dir': 'inputs'}
            (self.root / 'input.yml').write_text(yaml.safe_dump(config))
            async with self.client() as client:
                result = await client.call_tool('prepare_production', {'path': 'input.yml', 'output': 'out'})
                self.assertEqual(result.structured_content['error']['code'], 'PATH_OUTSIDE_WORKSPACE')
            self.assertFalse((self.root / 'out').exists())

    async def test_explicit_recovery_preserves_inputs_and_reserves_only_selected_jobs(self):
        job = self.production.manifest['jobs'][0]
        missing = self.root / 'production' / Path(job['run_file']).parent / 'RUN/Events/run_01/unweighted_events.lhe.gz'
        missing.unlink()
        other = self.production.manifest['jobs'][1]
        good = self.root / 'production' / Path(other['run_file']).parent / 'RUN/Events/run_01/unweighted_events.lhe.gz'
        good_bytes = good.read_bytes()
        async with self.client() as client:
            plan = await client.call_tool('plan_recovery', {'path': 'production'})
            self.assertEqual(plan.structured_content['data']['eligible_job_ids'], [job['job_id']])
            result = await client.call_tool('prepare_recovery',
                {'path': 'production', 'output': 'retry', 'job_ids': [job['job_id']]})
            self.assertFalse(result.is_error, result)
            info = result.structured_content['data']
            self.assertEqual(info['jobs_total'], 1)
            self.assertEqual(info['jobs'][0], job)
            self.assertFalse(info['submitted'])
            child = mgpd.load_prepared_production(self.root / 'retry')
            self.assertNotIn('execution_attempt', child.manifest)
            parent = mgpd.read_production_manifest(self.root / 'production')
            self.assertEqual(parent['recoveries'][0]['job_ids'], [job['job_id']])
            duplicate = await client.call_tool('prepare_recovery',
                {'path': 'production', 'output': 'retry2', 'job_ids': [job['job_id']]})
            self.assertTrue(duplicate.is_error)
            self.assertFalse((self.root / 'retry2').exists())
        self.assertEqual(good.read_bytes(), good_bytes)
        self.assertFalse((self.root / 'FORBIDDEN_EXECUTION').exists())
