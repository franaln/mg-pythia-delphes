#!/usr/bin/env python3
"""Exercise the installed read-only server through a real stdio MCP client."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

from mcp import Client, StdioServerParameters


async def check(args):
    report = {'state': 'passed', 'calls': {}}
    parameters = StdioServerParameters(command=sys.executable,
                                      args=['-m', 'mgpd_mcp.server', '--workspace', str(args.workspace.resolve())])
    async with Client(parameters) as client:
        report['tools'] = [tool.name for tool in (await client.list_tools()).tools]
        resource = await client.read_resource('mgpd://capabilities')
        report['capabilities'] = json.loads(resource.contents[0].text)
        calls = []
        if args.config:
            calls.append(('validate_config', {'path': args.config, 'base_directory': args.base_directory}))
        if args.production:
            calls.extend((tool, {'path': args.production}) for tool in
                         ('inspect_production', 'production_status', 'validate_results', 'plan_recovery'))
        for tool, arguments in calls:
            result = await client.call_tool(tool, arguments)
            payload = result.structured_content
            report['calls'][tool] = payload or [item.model_dump(mode='json', by_alias=True) for item in result.content]
            if result.is_error or (payload and payload.get('data', {}).get('state') in
                                   ('invalid', 'incomplete', 'unknown', 'failed', 'held')):
                report['state'] = 'failed'
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--production', help='Optional completed production, relative to workspace')
    parser.add_argument('--config', help='Optional YAML, relative to base directory')
    parser.add_argument('--base-directory', default='.')
    args = parser.parse_args()
    try:
        report = asyncio.run(check(args))
    except Exception as exc:
        report = {'state': 'failed', 'error': f'{type(exc).__name__}: {exc}'}
    print(json.dumps(report, indent=2))
    return int(report['state'] != 'passed')


if __name__ == '__main__':
    sys.exit(main())
