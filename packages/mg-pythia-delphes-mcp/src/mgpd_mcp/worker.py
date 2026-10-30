"""One core API request per process; cwd and diagnostic output are isolated."""
from contextlib import redirect_stdout
import json
from pathlib import Path
import sys

import mgpd
from mgpd.configuration import ConfigWriteUnknown
import yaml
from .paths import PathError, Workspace

WRITE_OPERATIONS = {'prepare_production', 'prepare_recovery'}
OPERATIONS = {'inspect_config', 'plan_config_change', 'apply_config_change', 'execute_production', 'operation_status', 'plan_merge', 'merge_production', 'merge_report'} | WRITE_OPERATIONS | {'validate_config', 'inspect_production', 'production_status', 'validate_results', 'plan_recovery'}


def error(code, message):
    return {'schema_version': 1, 'ok': False, 'error': {'code': code, 'message': str(message)[:2000]}}


def prepared_summary(prepared, workspace, limit):
    manifest = prepared.manifest
    samples = sorted({job['sample'] for job in manifest['jobs']})
    recovery = manifest.get('recovery')
    if recovery:
        recovery = {key: recovery[key] for key in
                    ('parent_directory', 'parent_id', 'parent_plan_sha256', 'policy')}
    return {'production_id': manifest['production_id'], 'plan_sha256': manifest['plan_sha256'],
            'directory': str(Path(prepared.output_dir).relative_to(workspace.root)),
            'state': manifest['state'], 'mode': manifest['mode'], 'image': manifest['image'],
            'jobs': manifest['jobs'][:limit], 'jobs_total': len(manifest['jobs']),
            'jobs_truncated': len(manifest['jobs']) > limit,
            'samples': samples[:limit], 'samples_total': len(samples), 'samples_truncated': len(samples) > limit,
            'submitted': False, 'review_required': True,
            'recovery': recovery}


def dispatch(request):
    workspace = Workspace(request['workspace'])
    operation = request['operation']
    if operation not in OPERATIONS:
        raise ValueError('Unsupported operation')
    if operation in WRITE_OPERATIONS and not request.get('allow_prepare', False):
        return error('PREPARATION_DISABLED', 'Server preparation capability is disabled')
    if operation in ('inspect_config', 'plan_config_change', 'apply_config_change'):
        if operation == 'apply_config_change' and not request.get('allow_config_write', False):
            return error('CONFIG_WRITE_DISABLED', 'Server configuration writes are disabled')
        from . import config_tools
        data = config_tools.dispatch(workspace, request)
        return {'schema_version': 1, 'ok': True, 'data': data}
    if operation in ('plan_merge', 'merge_production', 'merge_report'):
        from . import merge_tools, operations
        limit = request.get('limit', 100)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Invalid limit')
        if operation == 'merge_report':
            data = merge_tools.report(workspace, request['path'], request['operation_id'], limit)
        else:
            options = {key: request[key] for key in ('output', 'samples', 'allow_partial', 'backend', 'image',
                       'extract_lhe', 'keep_all', 'event_count', 'require_positive_cross_section', 'check_seed') if key in request}
            if options.get('samples') == []:
                raise ValueError('Sample selection must be nonempty or omitted')
            if operation == 'merge_production':
                if not request.get('allow_merge', False):
                    return error('MERGE_DISABLED', 'Server merge capability is disabled')
                data = operations.start_merge(workspace, request['path'], request['selection_sha256'], options)
            else:
                data = merge_tools.plan(workspace, request['path'], options, limit)
        return {'schema_version': 1, 'ok': True, 'data': data}
    if operation in ('execute_production', 'operation_status'):
        from . import operations
        if operation == 'execute_production':
            if not request.get('allow_execute', False):
                return error('EXECUTION_DISABLED', 'Server execution capability is disabled')
            data = operations.start(workspace, request['path'], request['plan_sha256'])
        else:
            offset, count = request.get('log_offset', 0), request.get('log_limit', 4000)
            if type(offset) is not int or offset < 0 or type(count) is not int or not 1 <= count <= 16000:
                raise ValueError('Invalid log pagination')
            data = operations.status(workspace, request['path'], request['operation_id'], offset, count)
        return {'schema_version': 1, 'ok': True, 'data': data}
    limit = request.get('limit', 100)
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('limit must be between 1 and 1000')
    if operation in ('validate_config', 'prepare_production'):
        base = workspace.path(request.get('base_directory', '.'))
        if not base.is_dir():
            raise FileNotFoundError('Base directory does not exist')
        path = workspace.path(request['path'], base)
        config = yaml.safe_load(path.read_text())
        if not isinstance(config, dict):
            raise ValueError('Configuration must contain a mapping')
        import os
        os.chdir(base)  # This worker handles exactly one request, never concurrent calls.
        workspace.config_inputs(config, base)
        mgpd.validate_config(config)
        if operation == 'prepare_production':
            output = workspace.new_output(request['output'])
            workspace.preparation_inputs(config, base, output)
            prepared = mgpd.prepare_production(config, str(output))
            return {'schema_version': 1, 'ok': True, 'data': prepared_summary(prepared, workspace, limit)}
        data = {'valid': True, 'config_path': str(path.relative_to(workspace.root)),
                'base_directory': str(base.relative_to(workspace.root)),
                'run': {key: value for key, value in config['run'].items()
                        if key in ('name', 'mode', 'image', 'nevents', 'njobs', 'outputs')},
                'resolved_mode': mgpd.resolve_run_mode(config['run'].get('mode', 'local'))}
    else:
        path = workspace.production(request['path'])
        if operation == 'prepare_recovery':
            output = workspace.new_output(request['output'])
            job_ids = request.get('job_ids')
            if (not isinstance(job_ids, list) or not job_ids or
                    any(not isinstance(value, str) or not value for value in job_ids)):
                raise ValueError('Select explicit nonempty job_ids')
            policy = {'event_count': request.get('event_count', 'exact'),
                      'require_positive_cross_section': request.get('require_positive_cross_section', True),
                      'check_seed': request.get('check_seed', True)}
            prepared = mgpd.prepare_recovery(str(path), str(output), job_ids=job_ids, **policy)
            return {'schema_version': 1, 'ok': True, 'data': prepared_summary(prepared, workspace, limit)}
        if operation == 'inspect_production':
            prepared = mgpd.load_prepared_production(path)
            manifest = prepared.manifest
            data = {key: manifest.get(key) for key in ('schema_version', 'production_id', 'plan_sha256',
                                                       'mode', 'image', 'state', 'jobs')}
            data['integrity_checked'] = True
            data['samples'] = sorted({job['sample'] for job in manifest['jobs']})
        elif operation == 'production_status':
            data = mgpd.production_status(path)
        else:
            policy = {'event_count': request.get('event_count', 'exact'),
                      'require_positive_cross_section': request.get('require_positive_cross_section', True),
                      'check_seed': request.get('check_seed', True)}
            data = (mgpd.validate_production(path, **policy) if operation == 'validate_results'
                    else mgpd.plan_recovery(path, **policy))
    for key in ('jobs', 'eligible_job_ids', 'query_errors'):
        if isinstance(data.get(key), list):
            data[key + '_total'] = len(data[key])
            data[key] = data[key][:limit]
            data[key + '_truncated'] = data[key + '_total'] > limit
    if isinstance(data.get('samples'), dict):
        data['samples_total'] = len(data['samples'])
        data['samples'] = dict(list(data['samples'].items())[:limit])
        for sample in data['samples'].values():
            sections = sample.get('cross_sections_pb', [])
            sample['cross_sections_total'] = len(sections)
            sample['cross_sections_pb'] = sections[:limit]
            sample['cross_sections_truncated'] = len(sections) > limit
        data['samples_truncated'] = data['samples_total'] > limit
    elif isinstance(data.get('samples'), list):
        data['samples_total'] = len(data['samples'])
        data['samples'] = data['samples'][:limit]
        data['samples_truncated'] = data['samples_total'] > limit
    if data.get('query_errors'):
        result = error('QUERY_FAILED', 'Scheduler query failed; see data.query_errors')
        result['data'] = data
        return result
    return {'schema_version': 1, 'ok': True, 'data': data}


def main():
    try:
        request = json.load(sys.stdin)
        with redirect_stdout(sys.stderr):
            result = dispatch(request)
    except ConfigWriteUnknown as exc:
        result = error('CONFIG_WRITE_UNKNOWN', exc)
    except PathError as exc:
        result = error('PATH_OUTSIDE_WORKSPACE', exc)
    except FileExistsError as exc:
        result = error('OUTPUT_EXISTS', exc)
    except FileNotFoundError as exc:
        result = error('NOT_FOUND', exc)
    except (ValueError, TypeError, KeyError, OSError, yaml.YAMLError) as exc:
        result = error('VALIDATION_ERROR', exc)
    except Exception as exc:
        result = error('INTERNAL_ERROR', f'{type(exc).__name__}: {exc}')
    json.dump(result, sys.stdout)
    sys.stdout.write('\n')


if __name__ == '__main__':
    main()
