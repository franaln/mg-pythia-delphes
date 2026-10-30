"""Real SDK stdio/client tests; only external scheduler programs are faked."""
from contextlib import redirect_stdout
import asyncio
import gzip
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import Client, StdioServerParameters
import mgpd
from mgpd.storage import write_manifest


def lhe(seed):
    return gzip.compress((f'<LesHouchesEvents>\n<header>\n{seed} = iseed\n</header>\n'
                          '<init>\n2212 2212 6500 6500 0 0 0 0 3 1\n10 0.1 1 1\n</init>\n'
                          '<event>\n</event>\n<event>\n</event>\n</LesHouchesEvents>\n').encode())


class StdioTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        card = self.root / 'run.dat'; card.write_text('')
        config = {'run': {'name': 'sample', 'mode': 'local-apptainer', 'image': 'image.sif',
                          'njobs': 2, 'nevents': 2, 'outputs': ['lhe']},
                  'cards': {'run': str(card)}, 'process': 'generate p p > e+ e-', 'options': {'seed': 101}}
        with redirect_stdout(io.StringIO()):
            self.production = mgpd.prepare_production(config, self.root / 'production')
        self.production.manifest['state'] = 'execution_finished'
        write_manifest(self.production.output_dir, self.production.manifest)
        for job in self.production.manifest['jobs']:
            folder = self.root / 'production' / Path(job['run_file']).parent / 'RUN/Events/run_01'
            folder.mkdir(parents=True)
            (folder / 'unweighted_events.lhe.gz').write_bytes(lhe(job['seed']))

    def client(self, timeout=30, env=None):
        return Client(StdioServerParameters(command=sys.executable,
                                             args=['-m', 'mgpd_mcp.server', '--workspace', str(self.root),
                                                   '--timeout', str(timeout)], env=env), read_timeout_seconds=30)

    def snapshot(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob('*') if path.is_file()}

    async def test_negotiation_tools_resource_and_core_parity_without_writes(self):
        before = self.snapshot()
        async with self.client() as client:
            tools = (await client.list_tools()).tools
            self.assertEqual({tool.name for tool in tools}, {'validate_config', 'inspect_production',
                             'production_status', 'validate_results', 'plan_recovery', 'operation_status', 'plan_merge', 'merge_report', 'inspect_config', 'plan_config_change'})
            for tool in tools:
                self.assertTrue(tool.annotations.read_only_hint)
                self.assertFalse(tool.annotations.destructive_hint)
                self.assertIn('path', tool.input_schema['properties'])
            resources = await client.list_resources()
            self.assertIn('mgpd://capabilities', [str(resource.uri) for resource in resources.resources])
            resource = await client.read_resource('mgpd://capabilities')
            self.assertEqual(json.loads(resource.contents[0].text)['core_version'], mgpd.__version__)
            inspected = await client.call_tool('inspect_production', {'path': 'production', 'limit': 1})
            self.assertFalse(inspected.is_error)
            self.assertTrue(inspected.structured_content['data']['integrity_checked'])
            self.assertEqual(inspected.structured_content['data']['jobs_total'], 2)
            self.assertTrue(inspected.structured_content['data']['jobs_truncated'])
            validated = await client.call_tool('validate_results', {'path': 'production', 'limit': 1})
            data = validated.structured_content['data']
            expected = mgpd.validate_production(self.production.output_dir)
            self.assertEqual(data['state'], expected['state'])
            self.assertEqual(data['counts'], expected['counts'])
            self.assertEqual(data['samples']['sample']['validated_lhe_events'], 4)
            self.assertEqual(data['samples']['sample']['cross_sections_total'], 2)
            self.assertEqual(len(data['samples']['sample']['cross_sections_pb']), 1)
            planned = await client.call_tool('plan_recovery', {'path': 'production'})
            self.assertEqual(planned.structured_content['data']['eligible_job_ids'], [])
            status = await client.call_tool('production_status', {'path': 'production'})
            self.assertEqual(status.structured_content['data']['recorded_state'], 'execution_finished')
        self.assertEqual(self.snapshot(), before)

    async def test_config_relative_paths_and_concurrent_base_directories(self):
        for name in ('a', 'b'):
            base = self.root / name; base.mkdir()
            (base / 'card.dat').write_text('')
            (base / 'config.yml').write_text(f'run:\n  name: {name}\n  outputs: [lhe]\nprocess: generate p p > e+ e-\ncards:\n  run: card.dat\n')
        before = self.snapshot()
        async with self.client() as client:
            first, second = await asyncio.gather(*[
                client.call_tool('validate_config', {'path': 'config.yml', 'base_directory': name}) for name in ('a', 'b')])
            self.assertFalse(first.is_error)
            self.assertFalse(second.is_error)
            self.assertEqual(first.structured_content['data']['run']['name'], 'a')
            self.assertEqual(second.structured_content['data']['run']['name'], 'b')
        self.assertEqual(self.snapshot(), before)

    async def test_path_traversal_symlinks_external_cards_and_missing_files(self):
        with tempfile.TemporaryDirectory() as outside:
            outside = Path(outside)
            (outside / 'card.dat').write_text('outside')
            (self.root / 'escape').symlink_to(outside, target_is_directory=True)
            (self.root / 'bad.yml').write_text(f'run:\n  name: bad\n  outputs: [lhe]\nprocess: generate p p > e+ e-\ncards:\n  run: {outside / "card.dat"}\n')
            async with self.client() as client:
                for path in ('../outside', 'escape', str(outside)):
                    response = await client.call_tool('inspect_production', {'path': path})
                    self.assertTrue(response.is_error)
                    self.assertEqual(response.structured_content['error']['code'], 'PATH_OUTSIDE_WORKSPACE')
                response = await client.call_tool('validate_config', {'path': 'bad.yml'})
                self.assertEqual(response.structured_content['error']['code'], 'PATH_OUTSIDE_WORKSPACE')
                response = await client.call_tool('inspect_production', {'path': 'missing'})
                self.assertEqual(response.structured_content['error']['code'], 'NOT_FOUND')
                response = await client.call_tool('inspect_production', {'path': 'production', 'limit': 0})
                self.assertTrue(response.is_error)

    async def test_invalid_yaml_and_changed_prepared_inputs_are_tool_errors(self):
        (self.root / 'invalid.yml').write_text('run: [')
        job = self.production.manifest['jobs'][0]
        path = self.root / 'production' / job['run_file']
        path.write_text(path.read_text() + '# changed\n')
        async with self.client() as client:
            invalid = await client.call_tool('validate_config', {'path': 'invalid.yml'})
            self.assertTrue(invalid.is_error)
            self.assertEqual(invalid.structured_content['error']['code'], 'VALIDATION_ERROR')
            changed = await client.call_tool('inspect_production', {'path': 'production'})
            self.assertTrue(changed.is_error)
            self.assertEqual(changed.structured_content['error']['code'], 'VALIDATION_ERROR')

    async def test_invalid_results_are_observations_and_policies_match_core(self):
        path = next((self.root / 'production').glob('*/RUN/Events/run_01/*.lhe.gz'))
        path.write_bytes(lhe(999))
        async with self.client() as client:
            invalid = await client.call_tool('validate_results', {'path': 'production'})
            self.assertFalse(invalid.is_error)
            self.assertEqual(invalid.structured_content['data']['state'], 'invalid')
            relaxed = await client.call_tool('validate_results', {'path': 'production', 'check_seed': False})
            self.assertEqual(relaxed.structured_content['data']['state'], 'valid')
            unknown = await client.call_tool('execute_production', {'path': 'production'})
            self.assertTrue(unknown.is_error)

    async def test_output_and_recovery_links_cannot_escape_workspace(self):
        with tempfile.TemporaryDirectory() as outside:
            path = next((self.root / 'production').glob('*/RUN/Events/run_01/*.lhe.gz'))
            path.unlink(); path.symlink_to(Path(outside) / 'data.lhe.gz')
            async with self.client() as client:
                result = await client.call_tool('validate_results', {'path': 'production'})
                self.assertEqual(result.structured_content['error']['code'], 'PATH_OUTSIDE_WORKSPACE')
            path.unlink(); path.write_bytes(lhe(101))
            manifest = self.production.manifest
            manifest['recoveries'] = [{'directory': outside, 'blocks_jobs': True}]
            write_manifest(self.production.output_dir, manifest)
            async with self.client() as client:
                result = await client.call_tool('plan_recovery', {'path': 'production'})
                self.assertEqual(result.structured_content['error']['code'], 'PATH_OUTSIDE_WORKSPACE')

    async def test_connectivity_script_exercises_existing_production_without_changes(self):
        before = self.snapshot()
        script = Path(__file__).resolve().parents[1] / 'scripts/check_server.py'
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(script), '--workspace', str(self.root), '--production', 'production',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
        self.assertEqual(process.returncode, 0, stderr.decode())
        report = json.loads(stdout)
        self.assertEqual(report['state'], 'passed')
        self.assertEqual(set(report['calls']), {'inspect_production', 'production_status',
                                               'validate_results', 'plan_recovery'})
        self.assertEqual(self.snapshot(), before)

    async def test_scheduler_query_error_is_not_success_and_timeout_is_bounded(self):
        tools = self.root / 'tools'; tools.mkdir()
        for name in ('condor_q', 'condor_history'):
            tool = tools / name
            tool.write_text('#!/bin/sh\nexit 1\n'); tool.chmod(0o755)
        manifest = self.production.manifest
        manifest['mode'] = 'condor'; manifest['state'] = 'submitted'
        manifest['submission'] = {'identity_tagged': True, 'jobs': [
            {'job_id': job['job_id'], 'scheduler_id': f'123.{index}'}
            for index, job in enumerate(manifest['jobs'])]}
        write_manifest(self.production.output_dir, manifest)
        env = {'PATH': str(tools) + os.pathsep + os.environ['PATH']}
        async with self.client(env=env) as client:
            result = await client.call_tool('production_status', {'path': 'production'})
            self.assertTrue(result.is_error)
            self.assertEqual(result.structured_content['error']['code'], 'QUERY_FAILED')
            self.assertEqual(result.structured_content['data']['counts'], {'unknown': 2})
        async with self.client(timeout=0.001) as client:
            result = await client.call_tool('production_status', {'path': 'production'})
            self.assertTrue(result.is_error)
            self.assertEqual(result.structured_content['error']['code'], 'TOOL_TIMEOUT')
