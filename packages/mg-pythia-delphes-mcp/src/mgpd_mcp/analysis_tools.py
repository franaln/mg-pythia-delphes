"""Confined analysis reviews and durable detached numerical operations."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sys

from mgpd.analysis import plan_analysis, run_analysis, check_output
from mgpd.analysis.integrity import checksum
from mgpd.analysis.saved import load_saved_results
from mgpd.storage import file_sha256
from . import operations
from .paths import Workspace, PathError

TOOLS = {'plan_analysis', 'start_analysis', 'analysis_status', 'analysis_report'}


def review_checksum(review):
    return checksum({key: value for key, value in review.items() if key != 'analysis_sha256'})

def relative(workspace, path):
    return str(workspace.path(path).relative_to(workspace.root))


def request_options(workspace, request):
    base = workspace.path(request.get('base_directory', '.'))
    if not base.is_dir():
        raise FileNotFoundError('Analysis base directory does not exist')
    inputs = request['inputs']
    if not isinstance(inputs, list) or not inputs or any(not isinstance(p, str) or not p for p in inputs):
        raise ValueError('Select explicit input paths or one managed dataset directory')
    plots = request.get('plots', False)
    if type(plots) is not bool:
        raise ValueError('plots must be a boolean')
    output = workspace.path(request['output'], base)
    if any(part.startswith('.mgpd') for part in output.relative_to(workspace.root).parts):
        raise ValueError('Reserved adapter output path')
    return {'inputs': [str(workspace.path(p, base)) for p in inputs],
            'config': str(workspace.path(request['config'], base)), 'output': str(output),
            'base_directory': str(base), 'format': request.get('format'), 'sample': request.get('sample'),
            'chunk_size': request.get('chunk_size', 10000), 'max_events': request.get('max_events'), 'plots': plots}


def check_dataset(workspace, path):
    """Check metadata references before the core opens any managed input."""
    if not path.is_dir():
        return
    if (path / 'merge-report.json').exists():
        report = workspace.path(path / 'merge-report.json')
    elif (path / 'manifest.json').exists():
        workspace.path(path / 'manifest.json')
        report = workspace.path(path / 'merged' / 'merge-report.json')
    elif (path.parent / 'merge-report.json').exists():
        report = workspace.path(path.parent / 'merge-report.json')
    else:
        return  # The core supplies its actionable missing-merge error.
    data = json.loads(report.read_text())
    root = report.parent
    for item in data.get('outputs', []):
        workspace.path(item['path'], root)
    if data.get('production_directory'):
        workspace.path(data['production_directory'])
    for item in data.get('family', []):
        workspace.path(item['directory'])
    for sample in data.get('samples', {}).values():
        workspace.path(sample['directory'], root)
        for job in sample.get('jobs', []):
            for key in ('directory', 'source_location'):
                if job.get(key):
                    workspace.path(job[key])


def identity(output):
    return 'analysis-' + hashlib.sha256(str(output).encode()).hexdigest()


def location(workspace, output, operation_id=None):
    expected = identity(output)
    if operation_id is not None and (not re.fullmatch(r'analysis-[0-9a-f]{64}', operation_id) or operation_id != expected):
        raise ValueError('Analysis operation ID does not match this output')
    directory = output.parent / ('.mgpd-mcp-' + expected)
    for name in ('', 'operation.json', 'worker.lock', 'analysis.log'):
        path = directory / name
        if path.is_symlink():
            raise ValueError('Analysis operation paths must not be symlinks')
        workspace.path(path)
    return directory


def checked_review(workspace, options):
    output = workspace.new_output(options['output'])
    if location(workspace, output).exists():
        raise FileExistsError('Analysis output is reserved; inspect analysis_status instead of retrying')
    config = workspace.path(options['config'])
    config_hash = file_sha256(config)
    for path in options['inputs']:
        check_dataset(workspace, Path(path))
    plan = plan_analysis(options['inputs'], config, **{key: options[key] for key in
                         ('format', 'sample', 'chunk_size', 'max_events')})
    for item in plan['inputs']:
        workspace.path(item['path'])
    for item in (plan.get('dataset') or {}).get('metadata', []):
        workspace.path(item['path'])
    check_output(output, plan)
    if file_sha256(config) != config_hash:
        raise ValueError('Analysis configuration changed while planning')
    review = {'schema_version': 1, 'plan': plan, 'options': options, 'config_sha256': config_hash}
    review['analysis_sha256'] = review_checksum(review)
    return review


def plan_summary(workspace, review, limit, offset=0):
    plan = review['plan']
    config = plan['config']
    dataset = plan.get('dataset')
    provenance = ({key: dataset[key] for key in ('production_id', 'production_plan_sha256',
                   'selection_sha256', 'expected_events', 'partial')} if dataset else None)
    return {'analysis_sha256': review['analysis_sha256'], 'plan_sha256': plan['plan_sha256'],
            'config_sha256': review['config_sha256'], 'config_path': relative(workspace, review['options']['config']),
            'format': plan['format'], 'sample': (plan['sample'] or '')[:512] or None,
            'output': relative(workspace, review['options']['output']), 'plots': review['options']['plots'],
            'operation_id': identity(Path(review['options']['output'])),
            'dataset': provenance, 'offset': offset, 'limit': limit,
            'inputs_total': len(plan['inputs']), 'inputs_truncated': len(plan['inputs']) > limit,
            'inputs': [{'path': relative(workspace, p['path']), 'sha256': p['sha256'], 'bytes': p['bytes']}
                       for p in plan['inputs'][offset:offset+limit]],
            'chunk_size': plan['chunk_size'], 'max_events': plan['max_events'],
            'normalization': plan['normalization'],
            'objects': dict(list(config['objects'].items())[offset:offset+limit]), 'objects_total': len(config['objects']),
            'variables': dict(list(config['variables'].items())[offset:offset+limit]), 'variables_total': len(config['variables']),
            'histograms': [{key: h[key] for key in ('name', 'variable', 'stage', 'fill')}
                           for h in config['histograms'][offset:offset+limit]], 'histograms_total': len(config['histograms']),
            'cuts': config['cuts'][offset:offset+limit], 'cuts_total': len(config['cuts']), 'review_required': True}


def read(workspace, output, operation_id):
    directory = location(workspace, output, operation_id)
    record = json.loads((directory / 'operation.json').read_text())
    if (record.get('operation_id') != operation_id or record.get('kind') != 'analysis'
            or record.get('output') != str(output) or record.get('workspace') != str(workspace.root)):
        raise ValueError('Analysis operation identity changed')
    if record['state'] in ('queued', 'running'):
        with (directory / 'worker.lock').open('rb') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                record = {**record, 'state': 'unknown',
                          'message': 'Worker may be starting or interrupted. No terminal evidence; do not retry automatically.'}
    return record


def public(workspace, record):
    data = {key: record[key] for key in ('operation_id', 'kind', 'state', 'analysis_sha256',
            'plan_sha256', 'created_at', 'started_at', 'finished_at', 'error', 'message') if key in record}
    data['output'] = relative(workspace, record['output'])
    data['log'] = relative(workspace, location(workspace, Path(record['output'])) / 'analysis.log')
    return data


def start(workspace, options, reviewed_checksum):
    output = Path(options['output'])
    directory = location(workspace, output)
    if directory.exists():
        record = read(workspace, output, identity(output))
        if record['analysis_sha256'] != reviewed_checksum or record['review']['options'] != options:
            raise ValueError('Output reserved for a different analysis review')
        return public(workspace, record)
    review = checked_review(workspace, options)
    if review['analysis_sha256'] != reviewed_checksum:
        raise ValueError('Analysis inputs/configuration/options changed since review; plan again')
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        raise ValueError('Analysis reservation in progress; inspect analysis_status, do not restart')
    (directory / 'worker.lock').touch(exist_ok=False)
    record = {'operation_id': identity(output), 'kind': 'analysis', 'state': 'queued',
              'workspace': str(workspace.root), 'output': str(output), 'review': review,
              'analysis_sha256': reviewed_checksum, 'plan_sha256': review['plan']['plan_sha256'],
              'created_at': operations.timestamp(), 'host': socket.gethostname()}
    operations.save(directory, record)
    operations.launch(workspace, directory, record, 'mgpd_mcp.analysis_tools',
                      [str(workspace.root), str(output), record['operation_id']], log_name='analysis.log')
    return public(workspace, record)


def status(workspace, output, operation_id, offset, count):
    record = read(workspace, output, operation_id)
    data = public(workspace, record)
    with (location(workspace, output, operation_id) / 'analysis.log').open('rb') as log:
        log.seek(offset)
        data['log_text'] = log.read(count).decode(errors='replace')
        data['log_next_offset'] = log.tell()
    return data


def report(workspace, output, operation_id, offset, limit):
    record = read(workspace, output, operation_id)
    if record['state'] != 'completed':
        raise ValueError('Analysis has no completed report; inspect analysis_status')
    for name in ('analysis-report.json', 'analysis.yml', 'cutflow.json', 'histograms.json'):
        workspace.path(output / name)
    if file_sha256(output / 'analysis-report.json') != record['report_sha256']:
        raise ValueError('Analysis report changed after completion')
    saved, _, cutflow, histograms, _ = load_saved_results(output)
    if saved['plan_sha256'] != record['plan_sha256']:
        raise ValueError('Analysis report belongs to a different plan')
    items = list(histograms.items())
    artifacts = saved.get('artifact_metadata', [])
    for item in artifacts:
        path = workspace.path(output / item['path'])
        if not path.is_relative_to(output):
            raise PathError('Analysis artifact leaves its result directory')
    return {**public(workspace, record), 'events_processed': saved['events_processed'],
            'normalization': saved['normalization_metadata'], 'preview_requested': saved['preview_requested'],
            'warnings': saved['warnings'][:limit], 'warnings_total': len(saved['warnings']),
            'cutflow': cutflow[offset:offset+limit], 'cutflow_total': len(cutflow),
            'histograms': [{'name': name, 'stage': h['stage'], 'fill': h['fill'], 'bins': len(h['counts']),
                            'selected_events': h['selected_events'], 'missing_values': h['missing_values']}
                           for name, h in items[offset:offset+limit]], 'histograms_total': len(items),
            'artifacts': [{'path': relative(workspace, output / item['path']), 'bytes': item['bytes'],
                           'sha256': item['sha256']} for item in artifacts[offset:offset+limit]],
            'artifacts_total': len(artifacts), 'offset': offset, 'limit': limit,
            'report_path': relative(workspace, output / 'analysis-report.json')}


def dispatch(workspace, request):
    tool = request['operation']
    limit, offset = request.get('limit', 20), request.get('offset', 0)
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        raise ValueError('Invalid analysis pagination')
    if tool in ('plan_analysis', 'start_analysis'):
        options = request_options(workspace, request)
        if tool == 'plan_analysis':
            return plan_summary(workspace, checked_review(workspace, options), limit, offset)
        return start(workspace, options, request['analysis_sha256'])
    output = workspace.path(request['output'])
    if tool == 'analysis_status':
        count = request.get('log_limit', 4000)
        position = request.get('log_offset', 0)
        if type(count) is not int or not 1 <= count <= 16000 or type(position) is not int or position < 0:
            raise ValueError('Invalid log pagination')
        return status(workspace, output, request['operation_id'], position, count)
    return report(workspace, output, request['operation_id'], offset, limit)


def run(root, output, operation_id):
    workspace = Workspace(root)
    output = workspace.path(output)
    directory = location(workspace, output, operation_id)
    with (directory / 'worker.lock').open('rb') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        record = read(workspace, output, operation_id)
        if record['state'] != 'queued':
            raise ValueError('Analysis operation cannot be restarted')
        record.update(state='running', started_at=operations.timestamp())
        operations.save(directory, record)
        try:
            review = record['review']
            if review['analysis_sha256'] != review_checksum(review) or review['analysis_sha256'] != record['analysis_sha256']:
                raise ValueError('Frozen analysis review changed')
            options = review['options']
            if file_sha256(workspace.path(options['config'])) != review['config_sha256']:
                raise ValueError('Analysis YAML changed before execution')
            for path in options['inputs']:
                check_dataset(workspace, workspace.path(path))
            for item in review['plan']['inputs']:
                workspace.path(item['path'])
            for item in (review['plan'].get('dataset') or {}).get('metadata', []):
                workspace.path(item['path'])
            workspace.new_output(output)
            run_analysis(review['plan'], output, plots=options['plots'])
            record.update(state='completed', report_sha256=file_sha256(output / 'analysis-report.json'))
        except Exception as exc:
            record.update(state='unknown' if output.exists() else 'failed', error=f'{type(exc).__name__}: {exc}'[:2000])
        except BaseException as exc:
            record.update(state='unknown', error=f'{type(exc).__name__}: {exc}'[:2000])
        record['finished_at'] = operations.timestamp()
        operations.save(directory, record)


if __name__ == '__main__':
    run(*sys.argv[1:])
