"""Real stdio configuration review/write protocol; no preparation or engines."""
import asyncio
from pathlib import Path
import json
import sys
import tempfile
import unittest
from mcp import Client, StdioServerParameters


class ConfigurationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        project = self.root / 'project'; project.mkdir()
        (project / 'card.dat').write_text('')
        self.source = project / 'config.yml'
        self.original = ('# source config\nrun:\n  name: "sample"\n  mode: condor\n'
                         '  outputs: [lhe]\n  nevents: 10 # keep comment\n'
                         'cards:\n  run: card.dat\nprocess: |\n  generate p p > e+ e-\n')
        self.source.write_text(self.original)

    def client(self, allow=False, timeout=30):
        args = ['-m', 'mgpd_mcp.server', '--workspace', str(self.root), '--timeout', str(timeout)]
        if allow:
            args.append('--allow-config-write')
        return Client(StdioServerParameters(command=sys.executable, args=args))

    async def plan(self, client, **parameters):
        result = await client.call_tool('plan_config_change',
            {'path': 'config.yml', 'base_directory': 'project', **parameters})
        self.assertFalse(result.is_error, result)
        return result.structured_content['data']

    async def apply(self, client, proposal, **extra):
        return await client.call_tool('apply_config_change',
            {'proposal': proposal, 'change_sha256': proposal['change_sha256'], **extra})

    async def test_default_inspection_planning_and_write_opt_in(self):
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        async with self.client() as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            self.assertIn('inspect_config', tools)
            self.assertIn('plan_config_change', tools)
            self.assertNotIn('apply_config_change', tools)
            inspected = await client.call_tool('inspect_config', {'path': 'config.yml', 'base_directory': 'project'})
            self.assertTrue(inspected.structured_content['data']['validation']['valid'])
            proposal = await self.plan(client, changes={'run.nevents': 20})
            self.assertIn('+  nevents: 20 # keep comment', proposal['diff'])
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        async with self.client(True) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            self.assertTrue(tools['apply_config_change'].annotations.destructive_hint)
            self.assertFalse(tools['apply_config_change'].annotations.read_only_hint)
            result = await self.apply(client, proposal)
            self.assertFalse(result.is_error, result)
            self.assertEqual(result.structured_content['data']['sha256'], proposal['content_sha256'])
        self.assertIn('nevents: 20 # keep comment', self.source.read_text())
        self.assertFalse(any(self.root.rglob('manifest.json')))

    async def test_create_from_existing_reconnect_and_refuse_overwrite(self):
        async with self.client() as client:
            proposal = await self.plan(client, output='copy.yml', changes={'run.njobs': 2})
        async with self.client(True) as client:
            result = await self.apply(client, proposal)
            self.assertFalse(result.is_error, result)
            self.assertEqual(self.source.read_text(), self.original)
            inspected = await client.call_tool('inspect_config', {'path': str(self.root / 'copy.yml'), 'base_directory': 'project'})
            self.assertTrue(inspected.structured_content['data']['validation']['valid'])
            again = await self.apply(client, proposal)
            self.assertEqual(again.structured_content['error']['code'], 'OUTPUT_EXISTS')
            wrong = await self.apply(client, proposal, change_sha256='wrong')
            self.assertTrue(wrong.is_error)

    async def test_stale_proposal_invalid_config_and_frozen_destination(self):
        async with self.client(True) as client:
            proposal = await self.plan(client, changes={'run.nevents': 20})
            self.source.write_text(self.original + '# external edit\n')
            stale = await self.apply(client, proposal)
            self.assertTrue(stale.is_error)
            invalid = await self.plan(client, changes={'run.nevents': 0})
            self.assertFalse(invalid['validation']['valid'])
            result = await self.apply(client, invalid)
            self.assertTrue(result.is_error)
            folder = self.root / 'production'; folder.mkdir()
            (folder / 'manifest.json').write_text('{}')
            rejected = await client.call_tool('plan_config_change', {'path': 'config.yml',
                'base_directory': 'project', 'output': 'production/copy.yml'})
            self.assertTrue(rejected.is_error)
        self.assertTrue(self.source.read_text().endswith('# external edit\n'))

    async def test_workspace_input_and_symlink_guards(self):
        with tempfile.TemporaryDirectory() as external:
            (Path(external) / 'outside.dat').write_text('')
            async with self.client(True) as client:
                for parameters in ({'path': '../../outside.yml'}, {'output': '../outside.yml'},
                                   {'changes': {'cards.run': str(Path(external) / 'outside.dat')}}):
                    result = await client.call_tool('plan_config_change',
                        {'path': 'config.yml', 'base_directory': 'project', **parameters})
                    self.assertEqual(result.structured_content['error']['code'], 'PATH_OUTSIDE_WORKSPACE')
                link = self.root / 'linked.yml'; link.symlink_to(self.source)
                result = await client.call_tool('plan_config_change', {'path': 'linked.yml', 'changes': {'run.nevents': 20}})
                self.assertTrue(result.is_error)
                proposal = await self.plan(client, changes={'run.nevents': 20})
                proposal['destination'] = str(Path(external) / 'copy.yml')
                result = await self.apply(client, proposal)
                self.assertEqual(result.structured_content['error']['code'], 'PATH_OUTSIDE_WORKSPACE')
        self.assertEqual(self.source.read_text(), self.original)

    async def test_concurrent_apply_uses_one_review(self):
        async with self.client(True) as client:
            proposal = await self.plan(client, changes={'run.nevents': 20})
            results = await asyncio.gather(*[self.apply(client, proposal) for _ in range(2)])
            self.assertEqual(sum(not result.is_error for result in results), 1)
            inspected = await client.call_tool('inspect_config', {'path': 'config.yml', 'base_directory': 'project'})
            self.assertEqual(inspected.structured_content['data']['sha256'], proposal['content_sha256'])

    async def test_write_timeout_is_unknown_not_safe_retry(self):
        async with self.client() as client:
            proposal = await self.plan(client, changes={'run.nevents': 20})
        async with self.client(True, timeout=.000001) as client:
            result = await self.apply(client, proposal)
            self.assertEqual(result.structured_content['error']['code'], 'CONFIG_WRITE_UNKNOWN')
        # The worker may have been killed before or after publication; only these
        # complete versions are acceptable, never a partial configuration.
        self.assertIn(self.source.read_text(), (self.original, proposal['content']))
