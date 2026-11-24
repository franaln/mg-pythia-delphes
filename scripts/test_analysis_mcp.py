#!/usr/bin/env python3
"""Opt-in stdio analysis acceptance on existing data; no simulation or submission."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from mcp import Client, StdioServerParameters
from mgpd.analysis import analysis_example


async def check(args, report):
    config = args.output / 'analysis.yml'
    config.write_text(analysis_example('dileptons-' + args.format))
    options = {'inputs': [args.input], 'config': str(config), 'output': str(args.output/'mcp'),
               'format': args.format, 'sample': args.sample, 'chunk_size': args.chunk_size,
               'max_events': args.max_events, 'plots': args.plots}
    base = ['-m', 'mgpd_mcp.server', '--workspace', str(args.workspace)]
    env = {'MPLCONFIGDIR': str(args.output/'mpl-cache')}

    def client(allow=False):
        return Client(StdioServerParameters(command=sys.executable,
                      args=base+(['--allow-analysis'] if allow else []), env=env), read_timeout_seconds=150)

    async def call(session, tool, parameters):
        result = await session.call_tool(tool, parameters)
        payload = result.structured_content
        report['calls'].append({'tool': tool, 'parameters': parameters, 'response': payload})
        (args.output/'report.json').write_text(json.dumps(report, indent=2)+'\n')
        if result.is_error:
            raise ValueError(f'{tool}: {payload}')
        return payload['data']

    async with client(True) as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert 'start_analysis' in tools and 'execute_production' not in tools
        report['capabilities'] = json.loads((await session.read_resource('mgpd://capabilities')).contents[0].text)
        schema = json.loads((await session.read_resource('mgpd://analysis')).contents[0].text)
        assert 'dileptons-'+args.format+'.yml' in schema['examples']
        review = await call(session, 'plan_analysis', options)
        operation = await call(session, 'start_analysis', {**options, 'analysis_sha256': review['analysis_sha256']})
    # Close the starting connection and observe through a new read-only server.
    async with client() as session:
        deadline = time.monotonic()+args.timeout
        while True:
            status = await call(session, 'analysis_status', {'output': options['output'], 'operation_id': operation['operation_id']})
            if 'finished_at' in status:
                if status['state'] != 'completed':
                    raise ValueError(f'Analysis terminal state: {status}')
                break
            if time.monotonic() > deadline:
                raise TimeoutError('Observation timed out; operation persists. Inspect it; do not restart automatically.')
            await asyncio.sleep(.2)
        summary = await call(session, 'analysis_report', {'output': options['output'], 'operation_id': operation['operation_id'], 'limit': 1})
        assert len(summary['cutflow']) <= 1 and len(summary['histograms']) <= 1
    command = [sys.executable, '-m', 'mgpd', 'analyze', args.input, '--config', str(config),
               '--format', args.format, '--chunk-size', str(args.chunk_size), '-o', str(args.output/'cli')]
    if args.sample:
        command += ['--sample', args.sample]
    if args.max_events:
        command += ['--max-events', str(args.max_events)]
    if args.plots:
        command.append('--plots')
    report['cli_command'] = command
    with (args.output/'cli.log').open('w') as log:
        subprocess.run(command, cwd=args.workspace, env=dict(os.environ, **env), stdout=log, stderr=log, check=True)
    for name in ('cutflow.json', 'histograms.json'):
        assert json.loads((args.output/'mcp'/name).read_text()) == json.loads((args.output/'cli'/name).read_text()), name
    async with client(True) as session:
        repeat = await call(session, 'start_analysis', {**options, 'analysis_sha256': review['analysis_sha256']})
        assert repeat['operation_id'] == operation['operation_id'] and repeat['state'] == 'completed'
    report.update(state='passed', numerical_parity=True, reconnected=True,
                  operation_id=operation['operation_id'], events=summary['events_processed'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, default=Path.cwd())
    parser.add_argument('--input', required=True, help='Explicit file or managed dataset inside workspace')
    parser.add_argument('--format', choices=['lhe', 'root', 'lhco'], required=True)
    parser.add_argument('--sample')
    parser.add_argument('--output', type=Path, required=True, help='New evidence directory inside workspace')
    parser.add_argument('--max-events', type=int)
    parser.add_argument('--chunk-size', type=int, default=128)
    parser.add_argument('--plots', action='store_true')
    parser.add_argument('--timeout', type=float, default=120)
    args = parser.parse_args()
    args.workspace = args.workspace.resolve(strict=True)
    args.output = args.output.resolve()
    if (not args.output.is_relative_to(args.workspace) or args.timeout <= 0 or args.chunk_size < 1
            or args.max_events is not None and args.max_events < 1):
        parser.error('Use a new output inside workspace, positive timeout/event limit/chunk size')
    args.output.mkdir(parents=True, exist_ok=False)
    report = {'state': 'running', 'calls': [], 'python': sys.executable}
    try:
        asyncio.run(check(args, report))
    except Exception as exc:
        report.update(state='failed', error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        (args.output/'report.json').write_text(json.dumps(report, indent=2)+'\n')
    print(f'Analysis MCP acceptance passed: {args.output}/report.json')


if __name__ == '__main__':
    main()
