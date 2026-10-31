"""Real stdio project creation/copying and configuration-write capability checks."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from mcp import Client, StdioServerParameters
from mgpd_mcp.worker import dispatch
import mgpd


class ProjectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def client(self, allow=False, timeout=30):
        args = ['-m', 'mgpd_mcp.server', '--workspace', str(self.root), '--timeout', str(timeout)]
        if allow:
            args.append('--allow-config-write')
        return Client(StdioServerParameters(command=sys.executable, args=args))

    async def test_creation_is_separately_enabled_and_never_prepares(self):
        async with self.client() as client:
            self.assertNotIn('create_project', {tool.name for tool in (await client.list_tools()).tools})
        disabled = dispatch({'workspace': str(self.root), 'operation': 'create_project', 'output': 'disabled'})
        self.assertEqual(disabled['error']['code'], 'CONFIG_WRITE_DISABLED')
        self.assertFalse((self.root / 'disabled').exists())
        async with self.client(True) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            self.assertIn('create_project', tools)
            self.assertNotIn('prepare_production', tools)
            self.assertFalse(tools['create_project'].annotations.read_only_hint)
            result = await client.call_tool('create_project', {'output': 'starter', 'level': 'detector',
                                                               'mode': 'condor', 'njobs': 2})
            self.assertFalse(result.is_error, result)
            data = result.structured_content['data']
            self.assertEqual(data['config_path'], 'starter/config.yml')
            self.assertEqual(data['base_directory'], 'starter')
            self.assertFalse(data['submitted'])
            inspected = await client.call_tool('inspect_config', {'path': 'config.yml', 'base_directory': 'starter'})
            self.assertTrue(inspected.structured_content['data']['validation']['valid'])
            validated = await client.call_tool('validate_config', {'path': 'config.yml', 'base_directory': 'starter'})
            self.assertFalse(validated.is_error, validated)
            self.assertEqual(validated.structured_content['data']['run']['njobs'], 2)
        self.assertFalse(any(self.root.rglob('manifest.json')))

    async def test_copy_and_override_without_changing_source(self):
        mgpd.create_project(self.root / 'source', template='proc', mode='condor', njobs=5, seed=101)
        original = (self.root / 'source/config.yml').read_bytes()
        async with self.client(True) as client:
            for output, options, count in [('same', {}, 5), ('changed', {'njobs': 1, 'name': 'renamed'}, 1)]:
                result = await client.call_tool('create_project', {'output': output, 'copy_from': 'source', **options})
                self.assertFalse(result.is_error, result)
                self.assertEqual(result.structured_content['data']['run']['njobs'], count)
            duplicate = await client.call_tool('create_project', {'output': 'same'})
            self.assertEqual(duplicate.structured_content['error']['code'], 'OUTPUT_EXISTS')
        self.assertEqual((self.root / 'source/config.yml').read_bytes(), original)
        self.assertEqual(mgpd.load_project_config(self.root / 'changed')['options']['seed'], 101)

    async def test_scope_symlinks_invalid_options_and_frozen_outputs(self):
        mgpd.create_project(self.root / 'source', template='proc')
        frozen = self.root / 'production'; frozen.mkdir()
        (frozen / 'manifest.json').write_text('{}')
        with tempfile.TemporaryDirectory() as outside:
            async with self.client(True) as client:
                cases = [({'output': '../escape'}, 'PATH_OUTSIDE_WORKSPACE'),
                         ({'output': 'copy', 'copy_from': outside}, 'PATH_OUTSIDE_WORKSPACE'),
                         ({'output': 'production/child'}, 'VALIDATION_ERROR'),
                         ({'output': 'bad', 'njobs': 0}, 'VALIDATION_ERROR'),
                         ({'output': 'bad', 'template': 'proc', 'copy_from': 'source'}, 'VALIDATION_ERROR')]
                for options, code in cases:
                    result = await client.call_tool('create_project', options)
                    self.assertTrue(result.is_error, result)
                    self.assertEqual(result.structured_content['error']['code'], code)
                (self.root / 'source/extra').symlink_to(Path(outside))
                result = await client.call_tool('create_project', {'output': 'linked', 'copy_from': 'source'})
                self.assertTrue(result.is_error)
                self.assertFalse((self.root / 'linked').exists())
                (self.root / 'source/extra').unlink()
                config = self.root / 'source/config.yml'
                config.unlink(); config.symlink_to(Path(outside) / 'secret.yml')
                result = await client.call_tool('create_project', {'output': 'bad-config', 'copy_from': 'source'})
                self.assertTrue(result.is_error)
                self.assertFalse((self.root / 'bad-config').exists())

    async def test_custom_process_creation_copy_and_invalid_process(self):
        process = 'import model sm\ngenerate p p > ta+ ta-\nadd process p p > mu+ mu-\n'
        async with self.client(True) as client:
            for output, options in [('custom', {}), ('copied', {'copy_from': 'custom'})]:
                result = await client.call_tool('create_project', {'output': output, 'process': process, **options})
                self.assertFalse(result.is_error, result)
                self.assertEqual(mgpd.load_project_config(self.root / output)['process'], process)
            result = await client.call_tool('create_project', {'output': 'changed', 'copy_from': 'custom',
                                                               'process': 'generate p p > b b~'})
            self.assertFalse(result.is_error, result)
            self.assertEqual(mgpd.load_project_config(self.root / 'changed')['process'], 'generate p p > b b~')
            self.assertEqual(mgpd.load_project_config(self.root / 'custom')['process'], process)
            for output, options in [('bad', {}), ('bad-copy', {'copy_from': 'custom'})]:
                result = await client.call_tool('create_project', {'output': output, 'process': '  ', **options})
                self.assertTrue(result.is_error)
                self.assertFalse((self.root / output).exists())
        self.assertFalse(any(self.root.rglob('manifest.json')))

    async def test_creation_timeout_is_unknown(self):
        async with self.client(True, timeout=.000001) as client:
            result = await client.call_tool('create_project', {'output': 'uncertain'})
            self.assertEqual(result.structured_content['error']['code'], 'PROJECT_CREATE_UNKNOWN')
            self.assertFalse(any(self.root.rglob('manifest.json')))
