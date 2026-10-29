"""One core API request per process; cwd and diagnostic output are isolated."""
from contextlib import redirect_stdout
import json
import sys

import mgpd
import yaml
from .paths import PathError, Workspace

OPERATIONS = {'validate_config', 'inspect_production', 'production_status', 'validate_results', 'plan_recovery'}


def error(code, message):
    return {'schema_version': 1, 'ok': False, 'error': {'code': code, 'message': str(message)[:2000]}}


def dispatch(request):
    workspace = Workspace(request['workspace'])
    operation = request['operation']
    if operation not in OPERATIONS:
        raise ValueError('Unsupported read-only operation')
    limit = request.get('limit', 100)
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('limit must be between 1 and 1000')
    if operation == 'validate_config':
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
        data = {'valid': True, 'config_path': str(path.relative_to(workspace.root)),
                'base_directory': str(base.relative_to(workspace.root)),
                'run': {key: value for key, value in config['run'].items()
                        if key in ('name', 'mode', 'image', 'nevents', 'njobs', 'outputs')},
                'resolved_mode': mgpd.resolve_run_mode(config['run'].get('mode', 'local'))}
    else:
        path = workspace.production(request['path'])
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
    except PathError as exc:
        result = error('PATH_OUTSIDE_WORKSPACE', exc)
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
