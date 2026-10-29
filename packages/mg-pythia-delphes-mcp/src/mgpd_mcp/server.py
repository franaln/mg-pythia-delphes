"""Read-only stdio MCP entry point for one user's configured workspace."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import signal
import sys
from typing import Annotated, Literal

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field
import mgpd
from . import __version__
from .paths import Workspace
from .worker import error

Limit = Annotated[int, Field(ge=1, le=1000)]
Policy = Literal['exact', 'nonempty', 'ignore']


def create_server(workspace, timeout=120):
    root = Workspace(workspace).root
    server = MCPServer('mg-pythia-delphes', version=__version__,
                       instructions='Read-only workflow tools. Paths are relative to the configured workspace. '
                       'No tools here prepare, submit, recover or merge data. Scheduler completion and valid outputs are separate.')
    annotations = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                  idempotent_hint=True, open_world_hint=False)

    async def call(operation, **parameters):
        request = {'workspace': str(root), 'operation': operation, **parameters}
        process = await asyncio.create_subprocess_exec(
            sys.executable, '-m', 'mgpd_mcp.worker', cwd=root,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(json.dumps(request).encode()), timeout)
            if stderr:
                print(stderr.decode(errors='replace')[:4000], file=sys.stderr)
            if process.returncode:
                payload = error('WORKER_FAILED', f'Core worker exited with code {process.returncode}')
            else:
                try:
                    payload = json.loads(stdout)
                except (ValueError, UnicodeError):
                    payload = error('WORKER_FAILED', 'Core worker returned invalid JSON')
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.communicate()
            if isinstance(exc, asyncio.CancelledError):
                raise
            payload = error('TOOL_TIMEOUT', 'Read-only operation exceeded its time limit; no jobs were submitted')
        return CallToolResult(content=[TextContent(type='text', text=json.dumps(payload))],
                              structured_content=payload, is_error=not payload['ok'])

    @server.resource('mgpd://capabilities', mime_type='application/json')
    def capabilities() -> str:
        return json.dumps({'adapter_version': __version__, 'core_version': mgpd.__version__,
                           'schema_version': 1, 'workspace': str(root), 'transport': 'stdio',
                           'read_only': True, 'timeout_seconds': timeout,
                           'tools': ['validate_config', 'inspect_production', 'production_status',
                                     'validate_results', 'plan_recovery']})

    @server.tool(annotations=annotations, structured_output=False)
    async def validate_config(path: str, base_directory: str = '.') -> CallToolResult:
        """Validate existing YAML and input paths without creating files. Paths in YAML resolve from base_directory."""
        return await call('validate_config', path=path, base_directory=base_directory)

    @server.tool(annotations=annotations, structured_output=False)
    async def inspect_production(path: str, limit: Limit = 100) -> CallToolResult:
        """Inspect a saved production and verify its frozen inputs. Does not execute work."""
        return await call('inspect_production', path=path, limit=limit)

    @server.tool(annotations=annotations, structured_output=False)
    async def production_status(path: str, limit: Limit = 100) -> CallToolResult:
        """Read local recorded state or Condor queue/history. Query errors remain errors; unknown is never success."""
        return await call('production_status', path=path, limit=limit)

    @server.tool(annotations=annotations, structured_output=False)
    async def validate_results(path: str, event_count: Policy = 'exact',
                               require_positive_cross_section: bool = True,
                               check_seed: bool = True, limit: Limit = 100) -> CallToolResult:
        """Validate saved output files and summarize samples without querying the scheduler or submitting jobs."""
        return await call('validate_results', path=path, event_count=event_count,
                          require_positive_cross_section=require_positive_cross_section,
                          check_seed=check_seed, limit=limit)

    @server.tool(annotations=annotations, structured_output=False)
    async def plan_recovery(path: str, event_count: Policy = 'exact',
                            require_positive_cross_section: bool = True,
                            check_seed: bool = True, limit: Limit = 100) -> CallToolResult:
        """Explain recovery eligibility using existing checks. Never prepares or executes a recovery."""
        return await call('plan_recovery', path=path, event_count=event_count,
                          require_positive_cross_section=require_positive_cross_section,
                          check_seed=check_seed, limit=limit)
    return server


def main():
    parser = argparse.ArgumentParser(description='Read-only per-user MCP server (stdio)')
    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    parser.add_argument('--workspace', type=Path, required=True, help='Existing root for all tool-managed paths')
    parser.add_argument('--timeout', type=float, default=120, help='Maximum seconds per read-only operation')
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    try:
        server = create_server(args.workspace, timeout=args.timeout)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    server.run(transport='stdio')


if __name__ == '__main__':
    main()
