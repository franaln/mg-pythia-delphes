"""Stdio MCP entry point for one user's configured workspace."""
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
from .worker import error, WRITE_OPERATIONS

Limit = Annotated[int, Field(ge=1, le=1000)]
Policy = Literal['exact', 'nonempty', 'ignore']


def create_server(workspace, timeout=120, allow_prepare=False, allow_execute=False):
    root = Workspace(workspace).root
    server = MCPServer('mg-pythia-delphes', version=__version__,
                       instructions='Workflow inspection and optional preparation tools. Paths are relative to the workspace. '
                       'Execution is separately opt-in and supports prepared Condor plans only. '
                       'Review returned identity, checksum and jobs. Scheduler completion and valid outputs are separate.')
    annotations = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                  idempotent_hint=True, open_world_hint=False)

    async def call(operation, **parameters):
        request = {'workspace': str(root), 'operation': operation, 'allow_prepare': allow_prepare, 'allow_execute': allow_execute, **parameters}
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
            if operation == 'execute_production':
                payload = error('EXECUTION_UNKNOWN', 'Submission operation may have started; inspect operation_status and production_status. Do not resubmit.')
                payload['path'] = parameters.get('path')
            elif operation in WRITE_OPERATIONS:
                payload = error('PREPARATION_UNKNOWN', 'Preparation timed out; inspect the output and parent manifest before retrying. No jobs were submitted.')
                payload['output'] = parameters.get('output')
            else:
                payload = error('TOOL_TIMEOUT', 'Read-only operation exceeded its time limit; no jobs were submitted')
        if operation == 'execute_production' and payload.get('error', {}).get('code') == 'WORKER_FAILED':
            payload = error('EXECUTION_UNKNOWN', 'Submission outcome is unknown; inspect operation_status and production_status. Do not resubmit.')
        if operation in WRITE_OPERATIONS and payload.get('error', {}).get('code') == 'WORKER_FAILED':
            payload = error('PREPARATION_UNKNOWN', 'Worker outcome is unknown; inspect the output and parent manifest before retrying. No jobs were submitted.')
            payload['output'] = parameters.get('output')
        return CallToolResult(content=[TextContent(type='text', text=json.dumps(payload))],
                              structured_content=payload, is_error=not payload['ok'])

    @server.resource('mgpd://capabilities', mime_type='application/json')
    def capabilities() -> str:
        return json.dumps({'adapter_version': __version__, 'core_version': mgpd.__version__,
                           'schema_version': 1, 'workspace': str(root), 'transport': 'stdio',
                           'read_only': not (allow_prepare or allow_execute), 'preparation_enabled': allow_prepare, 'execution_enabled': allow_execute, 'timeout_seconds': timeout,
                           'tools': ['validate_config', 'inspect_production', 'production_status',
                                     'validate_results', 'plan_recovery', 'operation_status'] + (['prepare_production', 'prepare_recovery'] if allow_prepare else []) + (['execute_production'] if allow_execute else [])})

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
    @server.tool(annotations=annotations, structured_output=False)
    async def operation_status(path: str, operation_id: str,
                               log_offset: Annotated[int, Field(ge=0)] = 0,
                               log_limit: Annotated[int, Field(ge=1, le=16000)] = 4000) -> CallToolResult:
        """Inspect a persistent submission operation and a bounded log page. Submitted is not physics completion. Never retries."""
        return await call('operation_status', path=path, operation_id=operation_id,
                          log_offset=log_offset, log_limit=log_limit)

    if allow_execute:
        execution = ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                    idempotent_hint=True, open_world_hint=True)

        @server.tool(annotations=execution, structured_output=False)
        async def execute_production(path: str, plan_sha256: str) -> CallToolResult:
            """Submit a reviewed prepared Condor plan once. Requires its exact checksum. Present jobs/seeds/image for user approval before calling. Returns a durable operation ID; poll operation_status and production_status. Never retry unknown submissions or treat submission as completion."""
            return await call('execute_production', path=path, plan_sha256=plan_sha256)

    if allow_prepare:
        preparation = ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                      idempotent_hint=False, open_world_hint=False)

        @server.tool(annotations=preparation, structured_output=False)
        async def prepare_production(path: str, output: str, base_directory: str = '.',
                                     limit: Limit = 100) -> CallToolResult:
            """Prepare existing YAML into a NEW workspace-relative output directory. Never run containers or submit jobs. Review the returned saved plan."""
            return await call('prepare_production', path=path, output=output,
                              base_directory=base_directory, limit=limit)

        @server.tool(annotations=preparation, structured_output=False)
        async def prepare_recovery(path: str, output: str,
                                   job_ids: Annotated[list[str], Field(min_length=1)],
                                   event_count: Policy = 'exact', require_positive_cross_section: bool = True,
                                   check_seed: bool = True, limit: Limit = 100) -> CallToolResult:
            """Reserve and clone explicit eligible job_ids into a NEW recovery directory. Modifies parent ownership. Never executes/submits. Plan eligibility first."""
            return await call('prepare_recovery', path=path, output=output, job_ids=job_ids,
                              event_count=event_count, require_positive_cross_section=require_positive_cross_section,
                              check_seed=check_seed, limit=limit)
    return server


def main():
    parser = argparse.ArgumentParser(description='Per-user MCP server (stdio); preparation and Condor submission are separately opt-in')
    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    parser.add_argument('--workspace', type=Path, required=True, help='Existing root for all tool-managed paths')
    parser.add_argument('--allow-execute', action='store_true', help='Enable one-shot prepared Condor submission; jobs run as the SSH user')
    parser.add_argument('--allow-prepare', action='store_true', help='Enable preparation tools; never enables execution')
    parser.add_argument('--timeout', type=float, default=120, help='Maximum seconds per operation; interrupted preparation may leave partial files')
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    try:
        server = create_server(args.workspace, timeout=args.timeout, allow_prepare=args.allow_prepare, allow_execute=args.allow_execute)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    server.run(transport='stdio')


if __name__ == '__main__':
    main()
