"""Stdio MCP entry point for one user's configured workspace."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import signal
import sys
from typing import Annotated, Literal, Any

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field
import mgpd
from . import __version__
from .paths import Workspace
from .worker import error, WRITE_OPERATIONS

Limit = Annotated[int, Field(ge=1, le=1000)]
Policy = Literal['exact', 'nonempty', 'ignore']


def create_server(workspace, timeout=120, allow_prepare=False, allow_execute=False, allow_merge=False, allow_config_write=False, allow_analysis=False):
    root = Workspace(workspace).root
    server = MCPServer('mg-pythia-delphes', version=__version__,
                       instructions='Workflow inspection and optional preparation tools. Paths are relative to the workspace. '
                       'Execution supports prepared Condor plans; merging is separately opt-in. '
                       'Analysis execution requires --allow-analysis, uses existing outputs only, and never launches physics jobs. Analysis starter creation requires --allow-config-write. '
                       'Review configuration diffs before applying changes; source configurations and frozen productions are separate. '
                       'Review returned identity, checksum and jobs. Scheduler completion and valid outputs are separate.')
    annotations = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                  idempotent_hint=True, open_world_hint=False)

    async def call(operation, **parameters):
        request = {'workspace': str(root), 'operation': operation, 'allow_prepare': allow_prepare, 'allow_execute': allow_execute, 'allow_merge': allow_merge, 'allow_config_write': allow_config_write, 'allow_analysis': allow_analysis, **parameters}
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
            if operation == 'create_project':
                payload = error('PROJECT_CREATE_UNKNOWN', 'Project creation outcome is unknown; inspect the output directory before retrying. No jobs were submitted.')
                payload['output'] = parameters.get('output')
            elif operation == 'apply_config_change':
                payload = error('CONFIG_WRITE_UNKNOWN', 'Configuration save outcome is unknown; inspect source/destination checksums before planning another change.')
            elif operation == 'start_analysis':
                payload = error('ANALYSIS_UNKNOWN', 'Analysis may have started; inspect analysis_status with the reviewed output and operation ID. Do not retry automatically.')
                payload['output'] = parameters.get('output')
            elif operation in ('execute_production', 'merge_production'):
                payload = error('MERGE_UNKNOWN' if operation == 'merge_production' else 'EXECUTION_UNKNOWN', 'Operation may have started; inspect operation_status and production state. Do not retry automatically.')
                payload['path'] = parameters.get('path')
            elif operation in WRITE_OPERATIONS:
                payload = error('PREPARATION_UNKNOWN', 'Preparation timed out; inspect the output and parent manifest before retrying. No jobs were submitted.')
                payload['output'] = parameters.get('output')
            else:
                payload = error('TOOL_TIMEOUT', 'Read-only operation exceeded its time limit; no jobs were submitted')
        if operation == 'create_project' and payload.get('error', {}).get('code') == 'WORKER_FAILED':
            payload = error('PROJECT_CREATE_UNKNOWN', 'Project creation outcome is unknown; inspect the output directory before retrying. No jobs were submitted.')
            payload['output'] = parameters.get('output')
        if operation == 'apply_config_change' and payload.get('error', {}).get('code') == 'WORKER_FAILED':
            payload = error('CONFIG_WRITE_UNKNOWN', 'Configuration save outcome is unknown; inspect source/destination checksums before planning another change.')
        if operation in ('execute_production', 'merge_production') and payload.get('error', {}).get('code') == 'WORKER_FAILED':
            payload = error('MERGE_UNKNOWN' if operation == 'merge_production' else 'EXECUTION_UNKNOWN', 'Operation outcome is unknown; inspect operation_status and production state. Do not retry automatically.')
        if operation == 'start_analysis' and payload.get('error', {}).get('code') == 'WORKER_FAILED':
            payload = error('ANALYSIS_UNKNOWN', 'Analysis outcome is unknown; inspect analysis_status with the reviewed output and operation ID. Do not retry automatically.')
            payload['output'] = parameters.get('output')
        if operation in WRITE_OPERATIONS and payload.get('error', {}).get('code') == 'WORKER_FAILED':
            payload = error('PREPARATION_UNKNOWN', 'Worker outcome is unknown; inspect the output and parent manifest before retrying. No jobs were submitted.')
            payload['output'] = parameters.get('output')
        return CallToolResult(content=[TextContent(type='text', text=json.dumps(payload))],
                              structured_content=payload, is_error=not payload['ok'])

    @server.resource('mgpd://capabilities', mime_type='application/json')
    def capabilities() -> str:
        return json.dumps({'adapter_version': __version__, 'core_version': mgpd.__version__,
                           'schema_version': 1, 'workspace': str(root), 'transport': 'stdio',
                           'read_only': not (allow_prepare or allow_execute or allow_merge or allow_config_write or allow_analysis), 'preparation_enabled': allow_prepare, 'execution_enabled': allow_execute, 'merge_enabled': allow_merge, 'config_write_enabled': allow_config_write, 'analysis_enabled': allow_analysis, 'timeout_seconds': timeout,
                           'tools': ['validate_config', 'inspect_production', 'production_status',
                                     'validate_results', 'plan_recovery', 'operation_status', 'plan_merge', 'merge_report', 'inspect_config', 'plan_config_change', 'plan_analysis', 'analysis_status', 'analysis_report'] + (['start_analysis'] if allow_analysis else []) + (['prepare_production', 'prepare_recovery'] if allow_prepare else []) + (['execute_production'] if allow_execute else []) + (['merge_production'] if allow_merge else []) + (['apply_config_change', 'create_project', 'create_analysis'] if allow_config_write else [])})

    @server.resource('mgpd://analysis', mime_type='application/json')
    def analysis_schema() -> str:
        from mgpd.analysis import analysis_example
        from mgpd.analysis.expressions import FUNCTIONS, FIELDS
        examples = ('dileptons-lhe', 'dileptons-root', 'dileptons-lhco')
        return json.dumps({'schema_version': 1, 'formats': ['lhe', 'root', 'lhco'],
                           'functions': sorted(FUNCTIONS), 'fields': sorted(FIELDS),
                           'examples': {name + '.yml': analysis_example(name) for name in examples},
                           'normalization_modes': ['raw', 'cross_section', 'luminosity'],
                           'execution_enabled': allow_analysis})

    @server.tool(annotations=annotations, structured_output=False)
    async def plan_analysis(inputs: list[str], config: str, output: str, base_directory: str = '.',
                            format: Literal['lhe', 'root', 'lhco'] | None = None, sample: str | None = None,
                            chunk_size: Annotated[int, Field(ge=1, le=1000000)] = 10000,
                            max_events: Annotated[int, Field(ge=1)] | None = None, plots: bool = False,
                            offset: Annotated[int, Field(ge=0)] = 0,
                            limit: Annotated[int, Field(ge=1, le=100)] = 20) -> CallToolResult:
        """Review an analysis without writes. Paths resolve from base_directory inside the workspace. Return analysis_sha256 binding input/YAML hashes, cuts, normalization, preview, output and plots. Read mgpd://analysis for starter YAML. Never launches generation, containers or jobs."""
        return await call('plan_analysis', inputs=inputs, config=config, output=output, base_directory=base_directory,
                          format=format, sample=sample, chunk_size=chunk_size, max_events=max_events, plots=plots, offset=offset, limit=limit)

    @server.tool(annotations=annotations, structured_output=False)
    async def analysis_status(output: str, operation_id: str,
                              log_offset: Annotated[int, Field(ge=0)] = 0,
                              log_limit: Annotated[int, Field(ge=1, le=16000)] = 4000) -> CallToolResult:
        """Read a durable analysis operation and bounded logs. output is workspace-relative, as returned by planning. A disconnected or interrupted operation is never restarted automatically."""
        return await call('analysis_status', output=output, operation_id=operation_id, log_offset=log_offset, log_limit=log_limit)

    @server.tool(annotations=annotations, structured_output=False)
    async def analysis_report(output: str, operation_id: str, offset: Annotated[int, Field(ge=0)] = 0,
                              limit: Annotated[int, Field(ge=1, le=100)] = 20) -> CallToolResult:
        """Read verified completed cutflows, histogram metadata and artifact paths with pagination. Never reopens simulation inputs. Remote PNG/PDF paths are references, not client-visible images."""
        return await call('analysis_report', output=output, operation_id=operation_id, offset=offset, limit=limit)

    if allow_analysis:
        analysis_writes = ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                         idempotent_hint=True, open_world_hint=False)

        @server.tool(annotations=analysis_writes, structured_output=False)
        async def start_analysis(analysis_sha256: str, inputs: list[str], config: str, output: str,
                                 base_directory: str = '.', format: Literal['lhe', 'root', 'lhco'] | None = None,
                                 sample: str | None = None,
                                 chunk_size: Annotated[int, Field(ge=1, le=1000000)] = 10000,
                                 max_events: Annotated[int, Field(ge=1)] | None = None, plots: bool = False) -> CallToolResult:
            """Start the exact reviewed analysis once in a detached worker. Requires --allow-analysis and identical plan_analysis options/checksum. Show cuts, normalization, preview, output and plots for review first. Poll analysis_status; inspect unknown outcomes without restarting. Creates only analysis artifacts, never physics jobs."""
            return await call('start_analysis', analysis_sha256=analysis_sha256, inputs=inputs, config=config,
                              output=output, base_directory=base_directory, format=format, sample=sample,
                              chunk_size=chunk_size, max_events=max_events, plots=plots)

    @server.tool(annotations=annotations, structured_output=False)
    async def inspect_config(path: str, base_directory: str = '.') -> CallToolResult:
        """Read existing YAML, checksum and validation issues. Input paths resolve from base_directory. Never writes."""
        return await call('inspect_config', path=path, base_directory=base_directory)

    @server.tool(annotations=annotations, structured_output=False)
    async def plan_config_change(path: str, changes: dict[str, Any] | None = None,
                                 remove: list[str] | None = None, output: str | None = None,
                                 base_directory: str = '.') -> CallToolResult:
        """Propose editing existing YAML or creating a NEW output from it. Mapping paths use dots or JSON pointers; replace lists/process strings as a whole. Return complete proposal, diff, validation and checksum for review. No writes. Preserve the base_directory for subsequent preparation."""
        return await call('plan_config_change', path=path, changes=changes, remove=remove,
                          output=output, base_directory=base_directory)

    if allow_config_write:
        config_writes = ToolAnnotations(read_only_hint=False, destructive_hint=True,
                                        idempotent_hint=False, open_world_hint=False)

        creation = ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                   idempotent_hint=False, open_world_hint=False)

        @server.tool(annotations=creation, structured_output=False)
        async def create_analysis(output: str = 'analysis.yml',
                                  format: Literal['root', 'lhe', 'lhco'] = 'root',
                                  base_directory: str = '.') -> CallToolResult:
            """Copy a NEW editable analysis YAML starter inside the workspace. Default: analysis.yml for Delphes ROOT. Never overwrite; edit selections, then review with plan_analysis before execution. No events are processed."""
            return await call('create_analysis', output=output, format=format, base_directory=base_directory)

        @server.tool(annotations=creation, structured_output=False)
        async def create_project(output: str, template: Literal['proc'] | None = None,
                                 copy_from: str | None = None, name: str | None = None,
                                 level: Literal['parton', 'particle', 'detector'] | None = None,
                                 mode: Literal['local', 'local-docker', 'local-apptainer', 'condor', 'jupiter'] | None = None,
                                 image: str | None = None, nevents: int | None = None,
                                 njobs: int | None = None, seed: int | None = None,
                                 process: str | None = None) -> CallToolResult:
            """Create a NEW editable project from the proc starter or copy_from directory, never both. Defaults for a starter: parton/local/100 events/1 job/random seed. Copies retain settings except explicit overrides; prepared productions are omitted. process accepts a complete inline MadGraph command block, replacing any process card. Model changes require matching cards; complete input-directory/file configurations cannot override process. Requires --allow-config-write. Review/edit YAML before preparing; no jobs run. On uncertainty inspect output before retrying."""
            return await call('create_project', output=output, template=template, copy_from=copy_from,
                              name=name, level=level, mode=mode, image=image,
                              nevents=nevents, njobs=njobs, seed=seed, process=process)

        @server.tool(annotations=config_writes, structured_output=False)
        async def apply_config_change(proposal: dict[str, Any], change_sha256: str) -> CallToolResult:
            """Save the complete reviewed proposal using its change_sha256. Rechecks validation/source checksum; refuses frozen production files and existing create destinations. Requires review of the diff. No preparation or submission. On uncertainty inspect checksums before retrying."""
            return await call('apply_config_change', proposal=proposal, change_sha256=change_sha256)

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
        """Inspect a persistent submission or merge operation and a bounded log page. Submitted is not physics completion. Never retries."""
        return await call('operation_status', path=path, operation_id=operation_id,
                          log_offset=log_offset, log_limit=log_limit)

    if allow_execute:
        execution = ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                    idempotent_hint=True, open_world_hint=True)

        @server.tool(annotations=execution, structured_output=False)
        async def execute_production(path: str, plan_sha256: str) -> CallToolResult:
            """Submit a reviewed prepared Condor plan once. Requires its exact checksum. Present jobs/seeds/image for user approval before calling. Returns a durable operation ID; poll operation_status and production_status. Never retry unknown submissions or treat submission as completion."""
            return await call('execute_production', path=path, plan_sha256=plan_sha256)

    @server.tool(annotations=annotations, structured_output=False)
    async def plan_merge(path: str, output: str | None = None, samples: list[str] | None = None,
                         allow_partial: bool = False, backend: Literal['local', 'local-apptainer', 'local-docker'] = 'local',
                         image: str | None = None, extract_lhe: bool = False, keep_all: bool = False,
                         event_count: Policy = 'exact', require_positive_cross_section: bool = True,
                         check_seed: bool = True, limit: Limit = 100) -> CallToolResult:
        """Review exact validated job selection across recoveries, file hashes and merge checksum. No engines run. Default output is production/merged. Partial merging is explicit."""
        return await call('plan_merge', **{key: value for key, value in locals().items() if key != 'call'})

    @server.tool(annotations=annotations, structured_output=False)
    async def merge_report(path: str, operation_id: str, limit: Limit = 100) -> CallToolResult:
        """Read bounded provenance and metrics from a completed merge operation."""
        return await call('merge_report', path=path, operation_id=operation_id, limit=limit)

    if allow_merge:
        merging = ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                  idempotent_hint=True, open_world_hint=True)

        @server.tool(annotations=merging, structured_output=False)
        async def merge_production(selection_sha256: str, path: str, output: str | None = None, samples: list[str] | None = None,
                         allow_partial: bool = False, backend: Literal['local', 'local-apptainer', 'local-docker'] = 'local',
                         image: str | None = None, extract_lhe: bool = False, keep_all: bool = False,
                         event_count: Policy = 'exact', require_positive_cross_section: bool = True,
                         check_seed: bool = True) -> CallToolResult:
            """Merge the reviewed selection in a detached operation. Require plan_merge checksum and identical options. Review omissions before authorizing allow_partial. No overwrite, no automatic retry; inspect operation_status then merge_report."""
            return await call('merge_production', **{key: value for key, value in locals().items() if key != 'call'})

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
    parser = argparse.ArgumentParser(description='Per-user MCP server (stdio); configuration writes, preparation, Condor submission and merging are separately opt-in')
    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    parser.add_argument('--workspace', type=Path, required=True, help='Existing root for all tool-managed paths')
    parser.add_argument('--allow-config-write', action='store_true', help='Enable project/analysis starter creation, project copying and reviewed YAML edits')
    parser.add_argument('--allow-analysis', action='store_true', help='Enable reviewed detached analysis writes; never enables generation or submission')
    parser.add_argument('--allow-merge', action='store_true', help='Enable reviewed managed merges in detached workers')
    parser.add_argument('--allow-execute', action='store_true', help='Enable one-shot prepared Condor submission; jobs run as the SSH user')
    parser.add_argument('--allow-prepare', action='store_true', help='Enable preparation tools; never enables execution')
    parser.add_argument('--timeout', type=float, default=120, help='Maximum seconds per operation; interrupted preparation may leave partial files')
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    try:
        server = create_server(args.workspace, timeout=args.timeout, allow_prepare=args.allow_prepare, allow_execute=args.allow_execute, allow_merge=args.allow_merge, allow_config_write=args.allow_config_write, allow_analysis=args.allow_analysis)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    server.run(transport='stdio')


if __name__ == '__main__':
    main()
