"""Durable, one-shot Condor submission operations; independent of MCP lifetime."""
import fcntl
import json
import os
import re
import hashlib
from pathlib import Path
import socket
import subprocess
import sys
import uuid
from datetime import datetime, timezone

import mgpd
from .paths import Workspace

DIRECTORY = '.mgpd-mcp-operation'


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def location(workspace, production, operation_id=None):
    name = DIRECTORY
    if operation_id and operation_id.startswith('merge-'):
        if not re.fullmatch(r'merge-[0-9a-f]{64}', operation_id):
            raise ValueError('Invalid merge operation ID')
        name += '-' + operation_id
    raw_directory = production / name
    if raw_directory.is_symlink():
        raise ValueError('Operation directory must not be a symlink')
    directory = workspace.path(raw_directory)
    # Never follow links for adapter-owned state or logs.
    for name in ('', 'operation.json', 'worker.lock', 'submission.log'):
        raw = directory / name
        if raw.is_symlink():
            raise ValueError('Operation paths must not be symlinks')
        workspace.path(raw)
    return directory


def save(directory, record):
    temporary = directory / 'operation.tmp'
    with open(temporary, 'x') as stream:
        json.dump(record, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(directory / 'operation.json')
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read(workspace, production, operation_id=None):
    directory = location(workspace, production, operation_id)
    record = json.loads((directory / 'operation.json').read_text())
    record = dict(record)
    if record['state'] in ('queued', 'running'):
        # A released worker lock without a terminal record is ambiguous.
        with open(directory / 'worker.lock', 'rb') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                record['state'] = 'unknown'
                record['message'] = 'Worker is not holding its lock; it may be starting or interrupted. Inspect operation and production state. Do not retry automatically.'
    record['log'] = str((directory / 'submission.log').relative_to(workspace.root))
    return record


def start(workspace, path, checksum):
    production = workspace.production(path)
    prepared = mgpd.load_prepared_production(production)
    manifest = prepared.manifest
    if checksum != manifest['plan_sha256']:
        raise ValueError('Plan checksum differs from the reviewed plan')
    if manifest['mode'] not in ('condor', 'jupiter'):
        raise ValueError('MCP execution currently supports Condor only')
    directory = location(workspace, production)
    if directory.exists():
        record = read(workspace, production)
        if record['production_id'] != manifest['production_id'] or record['plan_sha256'] != checksum:
            raise ValueError('Existing operation belongs to a different production plan')
        return record
    if manifest.get('execution_attempt') or manifest['state'] != 'prepared':
        raise ValueError('Production already attempted; inspect its state')
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        raise ValueError('Operation is being reserved; inspect operation_status, do not resubmit')
    (directory / 'worker.lock').touch(exist_ok=False)
    descriptor = os.open(production, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    record = {'operation_id': uuid.uuid4().hex, 'production_id': manifest['production_id'],
              'plan_sha256': checksum, 'path': str(production.relative_to(workspace.root)),
              'state': 'queued', 'created_at': timestamp(), 'host': socket.gethostname(),
              'kind': 'condor_submission'}
    save(directory, record)
    # Reservation persists even if launching fails. Never remove it or retry execution.
    with open(directory / 'submission.log', 'xb', buffering=0) as log:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            subprocess.Popen([sys.executable, '-m', 'mgpd_mcp.operations',
                              str(workspace.root), str(production), record['operation_id']],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             cwd=workspace.root, start_new_session=True, close_fds=True)
        except OSError as exc:
            record.update(state='unknown', error=f'Worker launch failed: {exc}', finished_at=timestamp())
            save(directory, record)
    return {**record, 'log': str((directory / 'submission.log').relative_to(workspace.root))}


def status(workspace, path, operation_id, log_offset=0, log_limit=4000):
    production = workspace.production(path)
    record = read(workspace, production, operation_id)
    if operation_id != record['operation_id']:
        raise ValueError('Operation ID does not match this production')
    prepared = mgpd.load_prepared_production(production)
    if (prepared.manifest['production_id'] != record['production_id'] or
            prepared.manifest['plan_sha256'] != record['plan_sha256']):
        raise ValueError('Production no longer matches operation identity')
    with open(location(workspace, production, operation_id) / 'submission.log', 'rb') as log:
        log.seek(log_offset)
        content = log.read(log_limit)
        record['log_next_offset'] = log.tell()
    record['log_text'] = content.decode(errors='replace')
    record['production_state'] = prepared.manifest['state']
    return record


def run(root, path, operation_id):
    workspace = Workspace(root)
    production = workspace.production(path)
    directory = location(workspace, production, operation_id)
    with open(directory / 'worker.lock', 'rb') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        record = json.loads((directory / 'operation.json').read_text())
        if record['operation_id'] != operation_id or record['state'] != 'queued':
            raise ValueError('Operation cannot be restarted')
        record.update(state='running', started_at=timestamp(), pid=os.getpid())
        save(directory, record)
        try:
            prepared = mgpd.load_prepared_production(production)
            if (prepared.manifest['plan_sha256'] != record['plan_sha256'] or
                    prepared.manifest['production_id'] != record['production_id']):
                raise ValueError('Prepared production changed before execution')
            if record['kind'] == 'merge':
                from .merge_tools import checked_output
                output = checked_output(workspace, production, record['options']['output'])
                options = {**record['options'], 'output': str(output)}
                result = mgpd.merge_production(production, selection_sha256=record['selection_sha256'], **options)
                record.update(state='completed', output=result,
                              report=str(Path(result) / 'merge-report.json'))
            else:
                if prepared.manifest['mode'] not in ('condor', 'jupiter'):
                    raise ValueError('MCP execution supports Condor only')
                mgpd.execute_production(prepared)
                record['state'] = 'submitted'
        except BaseException as exc:
            record.update(state='unknown', error=f'{type(exc).__name__}: {exc}'[:2000])
        record['finished_at'] = timestamp()
        save(directory, record)


def start_merge(workspace, path, selection_sha256, options):
    from .merge_tools import checked_output
    production = workspace.production(path)
    output = workspace.path(options.get('output') or production / 'merged')
    operation_id = 'merge-' + hashlib.sha256(str(output).encode()).hexdigest()
    directory = location(workspace, production, operation_id)
    if directory.exists():
        record = read(workspace, production, operation_id)
        manifest = mgpd.load_prepared_production(production).manifest
        if record['production_id'] != manifest['production_id'] or record['plan_sha256'] != manifest['plan_sha256']:
            raise ValueError('Production no longer matches merge operation')
        if record.get('selection_sha256') != selection_sha256 or record.get('options') != {**options, 'output': str(output)}:
            raise ValueError('Output reserved for a different merge request')
        return record
    output = checked_output(workspace, production, str(output))
    options = {**options, 'output': str(output)}
    review = mgpd.plan_production_merge(production, **options)
    if review['selection_sha256'] != selection_sha256:
        raise ValueError('Merge selection changed since review; plan again')
    directory.mkdir(mode=0o700)
    (directory / 'worker.lock').touch(exist_ok=False)
    prepared = mgpd.load_prepared_production(production)
    record = {'operation_id': operation_id, 'kind': 'merge', 'production_id': prepared.manifest['production_id'],
              'plan_sha256': prepared.manifest['plan_sha256'], 'selection_sha256': selection_sha256,
              'path': str(production.relative_to(workspace.root)), 'options': options,
              'state': 'queued', 'created_at': timestamp(), 'host': socket.gethostname()}
    save(directory, record)
    descriptor = os.open(production, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    with open(directory / 'submission.log', 'xb', buffering=0) as log:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            subprocess.Popen([sys.executable, '-m', 'mgpd_mcp.operations',
                              str(workspace.root), str(production), operation_id],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             cwd=workspace.root, start_new_session=True, close_fds=True)
        except OSError as exc:
            record.update(state='unknown', error=f'Worker launch failed: {exc}', finished_at=timestamp())
            save(directory, record)
    return record


if __name__ == '__main__':
    run(*sys.argv[1:])
