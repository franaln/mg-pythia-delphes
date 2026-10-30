"""MCP merge selection, durable operations and provenance with fake engines."""
import asyncio
import json
import os
from pathlib import Path
import sys
import unittest
from mcp import Client, StdioServerParameters
import mgpd
import test_stdio as fixtures


class MergeTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.StdioTests.setUp

    def client(self, allow=False, fail=False):
        tools = self.root / 'tools'; tools.mkdir(exist_ok=True)
        engine = tools / 'apptainer'
        engine.write_text('#!' + sys.executable + '\n' + '''import gzip, shlex, sys, time
from pathlib import Path
args = sys.argv[1:]
work = Path(args[args.index('--bind') + 1][:-len(':/work')])
command = shlex.split(args[-1].split(' && ')[-1])
time.sleep(.3)
''' + ("raise SystemExit(7)\n" if fail else '''count = sum(gzip.decompress((work / name).read_bytes()).count(b'<event>') for name in command[1:-2])
header = '<LesHouchesEvents>\\n<header>\\n101 = iseed\\n</header>\\n<init>\\n2212 2212 6500 6500 0 0 0 0 3 1\\n10 0.1 1 1\\n</init>\\n'
(work / command[-2]).write_bytes(gzip.compress((header + '<event>\\n</event>\\n' * count + '</LesHouchesEvents>\\n').encode()))
print('merged', count, flush=True)
'''))
        engine.chmod(0o755)
        args = ['-m', 'mgpd_mcp.server', '--workspace', str(self.root)]
        if allow:
            args.append('--allow-merge')
        return Client(StdioServerParameters(command=sys.executable, args=args,
            env={'PATH': str(tools) + os.pathsep + os.environ['PATH']}))

    async def plan(self, client, **extra):
        result = await client.call_tool('plan_merge', {'path': 'production', 'backend': 'local-apptainer', **extra})
        self.assertFalse(result.is_error, result)
        return result.structured_content['data']

    async def poll(self, client, operation_id):
        for _ in range(100):
            result = await client.call_tool('operation_status', {'path': 'production', 'operation_id': operation_id})
            self.assertFalse(result.is_error, result)
            data = result.structured_content['data']
            if 'finished_at' in data:
                return data
            await asyncio.sleep(.05)
        self.fail('Merge did not finish')

    async def test_review_execute_disconnect_repeat_and_report(self):
        before = (self.root / 'production/manifest.json').read_bytes()
        async with self.client(True) as client:
            plan = await self.plan(client, limit=1)
            self.assertEqual(plan['selection_total'], 2)
            self.assertTrue(plan['selection_truncated'])
            args = {'path': 'production', 'backend': 'local-apptainer', 'selection_sha256': plan['selection_sha256']}
            result = await client.call_tool('merge_production', args)
            self.assertFalse(result.is_error, result)
            operation_id = result.structured_content['data']['operation_id']
        async with self.client() as client:
            data = await self.poll(client, operation_id)
            self.assertEqual(data['state'], 'completed', data)
            report = await client.call_tool('merge_report', {'path': 'production', 'operation_id': operation_id, 'limit': 1})
            self.assertFalse(report.is_error, report)
            sample = report.structured_content['data']['samples']['sample']
            self.assertEqual(sample['metrics']['lhe_events'], 4)
            self.assertEqual(sample['jobs_total'], 2)
            self.assertTrue(sample['jobs_truncated'])
            self.assertNotIn('merge_production', [tool.name for tool in (await client.list_tools()).tools])
        async with self.client(True) as client:
            repeat = await client.call_tool('merge_production', args)
            self.assertFalse(repeat.is_error, repeat)
            self.assertEqual(repeat.structured_content['data']['operation_id'], operation_id)
        self.assertEqual((self.root / 'production/manifest.json').read_bytes(), before)

    async def test_partial_explicit_and_changed_selection_rejected(self):
        job = self.production.manifest['jobs'][0]
        output = self.root / 'production' / Path(job['run_file']).parent / 'RUN/Events/run_01/unweighted_events.lhe.gz'
        async with self.client(True) as client:
            plan = await self.plan(client)
            output.unlink()
            refused = await client.call_tool('plan_merge', {'path': 'production'})
            self.assertTrue(refused.is_error)
            partial = await self.plan(client, allow_partial=True)
            self.assertTrue(partial['partial'])
            self.assertEqual(partial['omitted_job_ids'], [job['job_id']])
            changed = await client.call_tool('merge_production', {'path': 'production', 'backend': 'local-apptainer',
                'allow_partial': True, 'selection_sha256': plan['selection_sha256']})
            self.assertTrue(changed.is_error)
            result = await client.call_tool('merge_production', {'path': 'production', 'backend': 'local-apptainer',
                'allow_partial': True, 'selection_sha256': partial['selection_sha256']})
            self.assertFalse(result.is_error, result)
            data = await self.poll(client, result.structured_content['data']['operation_id'])
            self.assertEqual(data['state'], 'completed', data)
        report = json.loads((self.root / 'production/merged/merge-report.json').read_text())
        self.assertTrue(report['partial'])
        self.assertEqual(report['samples']['sample']['metrics']['lhe_events'], 2)

    async def test_registered_recovery_is_selected_once(self):
        from contextlib import redirect_stdout
        import io
        from mgpd.storage import write_manifest
        job = self.production.manifest['jobs'][0]
        original = self.root / 'production' / Path(job['run_file']).parent / 'RUN/Events/run_01/unweighted_events.lhe.gz'
        original.unlink()
        with redirect_stdout(io.StringIO()):
            child = mgpd.prepare_recovery(self.root / 'production', self.root / 'retry', job_ids=[job['job_id']])
        child.manifest['state'] = 'execution_finished'
        write_manifest(child.output_dir, child.manifest)
        target = self.root / 'retry' / Path(job['run_file']).parent / 'RUN/Events/run_01'
        target.mkdir(parents=True)
        (target / 'unweighted_events.lhe.gz').write_bytes(fixtures.lhe(job['seed']))
        async with self.client(True) as client:
            plan = await self.plan(client)
            self.assertEqual(plan['family_total'], 2)
            recovered = next(row for row in plan['selection'] if row['job_id'] == job['job_id'])
            self.assertEqual(recovered['production_id'], child.manifest['production_id'])
            self.assertEqual(recovered['seed'], job['seed'])
            result = await client.call_tool('merge_production', {'path': 'production', 'backend': 'local-apptainer',
                'selection_sha256': plan['selection_sha256']})
            self.assertFalse(result.is_error, result)
            data = await self.poll(client, result.structured_content['data']['operation_id'])
            self.assertEqual(data['state'], 'completed', data)
        report = json.loads((self.root / 'production/merged/merge-report.json').read_text())
        rows = report['samples']['sample']['jobs']
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({row['job_id'] for row in rows}), 2)
        self.assertEqual(report['samples']['sample']['metrics']['lhe_events'], 4)

    async def test_output_path_sample_and_failure_guards(self):
        async with self.client(True, fail=True) as client:
            for options in ({'output': '../outside'}, {'output': 'production'},
                            {'output': 'production/run_sample_job00000/new'}, {'samples': ['absent']}):
                result = await client.call_tool('plan_merge', {'path': 'production', **options})
                self.assertTrue(result.is_error, result)
            plan = await self.plan(client)
            result = await client.call_tool('merge_production', {'path': 'production', 'backend': 'local-apptainer',
                'selection_sha256': plan['selection_sha256']})
            self.assertFalse(result.is_error, result)
            data = await self.poll(client, result.structured_content['data']['operation_id'])
            self.assertEqual(data['state'], 'unknown')
            self.assertFalse((self.root / 'production/merged').exists())
