"""Real stdio and detached workers with a fake Condor executable."""
import asyncio
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

import mgpd
from mcp import Client, StdioServerParameters


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / 'card.dat').write_text('')
        config = {'run': {'name': 'smoke', 'mode': 'condor', 'image': 'mg-pythia-delphes-3.5.6',
                          'njobs': 2, 'nevents': 2, 'outputs': ['lhe']},
                  'cards': {'run': str(self.root / 'card.dat')},
                  'process': 'generate p p > e+ e-', 'options': {'seed': 101}}
        with redirect_stdout(io.StringIO()):
            self.production = mgpd.prepare_production(config, self.root / 'production')
        tools = self.root / 'tools'; tools.mkdir()
        self.executable = tools / 'condor_submit'
        self.fake('echo "97.0 - 97.1"')
        self.env = {'PATH': str(tools) + os.pathsep + os.environ['PATH']}
        self.args = {'path': 'production', 'plan_sha256': self.production.manifest['plan_sha256']}

    def fake(self, action):
        self.executable.write_text(f'#!/bin/sh\necho called >> "{self.root}/calls"\n{action}\n')
        self.executable.chmod(0o755)

    def client(self, allow=True):
        args = ['-m', 'mgpd_mcp.server', '--workspace', str(self.root)]
        if allow:
            args += ['--allow-execute']
        return Client(StdioServerParameters(command=sys.executable, args=args, env=self.env))

    async def poll(self, client, operation_id):
        for _ in range(100):
            result = await client.call_tool('operation_status', {'path': 'production', 'operation_id': operation_id})
            self.assertFalse(result.is_error, result)
            data = result.structured_content['data']
            if data['state'] in ('submitted', 'unknown') and 'finished_at' in data:
                return data
            await asyncio.sleep(.05)
        self.fail('Detached operation did not finish')

    async def test_opt_in_checksum_and_workspace_guards(self):
        async with self.client(False) as client:
            self.assertNotIn('execute_production', [tool.name for tool in (await client.list_tools()).tools])
        async with self.client() as client:
            for args in ({**self.args, 'plan_sha256': 'wrong'}, {**self.args, 'path': '../outside'}):
                result = await client.call_tool('execute_production', args)
                self.assertTrue(result.is_error)
        self.assertFalse((self.root / 'calls').exists())
        self.assertFalse((self.root / 'production/.mgpd-mcp-operation').exists())

    async def test_simultaneous_first_requests_submit_once(self):
        async with self.client() as client:
            results = await asyncio.gather(*[client.call_tool('execute_production', self.args) for _ in range(2)])
            successes = [result.structured_content['data'] for result in results if not result.is_error]
            self.assertTrue(successes)
            self.assertEqual(len({item['operation_id'] for item in successes}), 1)
            data = await self.poll(client, successes[0]['operation_id'])
            self.assertEqual(data['state'], 'submitted')
        self.assertEqual((self.root / 'calls').read_text().splitlines(), ['called'])

    async def test_local_mode_and_symlink_state_are_rejected(self):
        config = dict(self.production.manifest['config'])
        config['run'] = {**config['run'], 'mode': 'local-apptainer', 'image': 'test.sif'}
        with redirect_stdout(io.StringIO()):
            local = mgpd.prepare_production(config, self.root / 'local')
        async with self.client() as client:
            result = await client.call_tool('execute_production',
                {'path': 'local', 'plan_sha256': local.manifest['plan_sha256']})
            self.assertTrue(result.is_error)
            (self.root / 'production/.mgpd-mcp-operation').symlink_to(self.root, target_is_directory=True)
            result = await client.call_tool('execute_production', self.args)
            self.assertTrue(result.is_error)
        self.assertFalse((self.root / 'calls').exists())

    async def test_tampered_inputs_and_prior_attempt_are_rejected(self):
        relative = next(iter(self.production.manifest['execution_input_sha256']))
        path = self.root / 'production' / relative
        original = path.read_bytes()
        path.write_bytes(original + b'changed')
        async with self.client() as client:
            result = await client.call_tool('execute_production', self.args)
            self.assertTrue(result.is_error)
            path.write_bytes(original)
            manifest = mgpd.read_production_manifest(self.root / 'production')
            manifest['execution_attempt'] = {'started_at': 'previous'}
            from mgpd.storage import write_manifest
            write_manifest(self.root / 'production', manifest)
            result = await client.call_tool('execute_production', self.args)
            self.assertTrue(result.is_error)
        self.assertFalse((self.root / 'calls').exists())
        self.assertFalse((self.root / 'production/.mgpd-mcp-operation').exists())

    async def test_failed_launch_keeps_reservation(self):
        from unittest.mock import patch
        from mgpd_mcp import operations
        from mgpd_mcp.paths import Workspace
        with patch.object(operations.subprocess, 'Popen', side_effect=OSError('launch unavailable')):
            first = operations.start(Workspace(self.root), **{'path': 'production', 'checksum': self.args['plan_sha256']})
        self.assertEqual(first['state'], 'unknown')
        async with self.client() as client:
            result = await client.call_tool('execute_production', self.args)
            self.assertEqual(result.structured_content['data']['operation_id'], first['operation_id'])
            self.assertEqual(result.structured_content['data']['state'], 'unknown')
        self.assertFalse((self.root / 'calls').exists())

    async def test_disconnect_repeat_and_persisted_logs(self):
        self.fake('sleep 1\necho "97.0 - 97.1"')
        async with self.client() as client:
            result = await client.call_tool('execute_production', self.args)
            self.assertFalse(result.is_error, result)
            operation_id = result.structured_content['data']['operation_id']
        # First server and SSH-equivalent stdio connection have ended.
        async with self.client(False) as client:
            data = await self.poll(client, operation_id)
            self.assertEqual(data['state'], 'submitted')
            self.assertEqual(data['production_state'], 'submitted')
            self.assertIn('97.0', data['log_text'])
            bad = await client.call_tool('operation_status', {'path': 'production', 'operation_id': 'wrong'})
            self.assertTrue(bad.is_error)
        async with self.client() as client:
            results = await asyncio.gather(*[client.call_tool('execute_production', self.args) for _ in range(2)])
            for result in results:
                self.assertEqual(result.structured_content['data']['operation_id'], operation_id)
        self.assertEqual((self.root / 'calls').read_text().splitlines(), ['called'])
        manifest = mgpd.read_production_manifest(self.root / 'production')
        self.assertEqual([job['scheduler_id'] for job in manifest['submission']['jobs']], ['97.0', '97.1'])

    async def test_uncertain_submission_is_never_retried(self):
        self.fake('echo ambiguous\nexit 1')
        async with self.client() as client:
            result = await client.call_tool('execute_production', self.args)
            operation_id = result.structured_content['data']['operation_id']
            data = await self.poll(client, operation_id)
            self.assertEqual(data['state'], 'unknown')
            self.assertEqual(data['production_state'], 'submission_unknown')
            again = await client.call_tool('execute_production', self.args)
            self.assertEqual(again.structured_content['data']['operation_id'], operation_id)
        self.assertEqual((self.root / 'calls').read_text().splitlines(), ['called'])

    async def test_interrupted_worker_remains_unknown(self):
        # Crash after the durable attempt marker, with no fake scheduler child left running.
        self.fake('kill -9 "$PPID"\nexit 0')
        async with self.client() as client:
            result = await client.call_tool('execute_production', self.args)
            operation_id = result.structured_content['data']['operation_id']
            for _ in range(100):
                status = await client.call_tool('operation_status', {'path': 'production', 'operation_id': operation_id})
                data = status.structured_content['data']
                if data['production_state'] == 'submitting' and data['state'] == 'unknown':
                    break
                await asyncio.sleep(.05)
            else:
                self.fail('Missing interrupted-worker observation')
            again = await client.call_tool('execute_production', self.args)
            self.assertEqual(again.structured_content['data']['state'], 'unknown')
        self.assertEqual((self.root / 'calls').read_text().splitlines(), ['called'])


if __name__ == '__main__':
    unittest.main()
