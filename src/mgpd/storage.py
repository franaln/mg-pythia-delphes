"""Manifest persistence, checksums and production locks."""
from pathlib import Path
import hashlib
import json
from contextlib import contextmanager
import fcntl


def write_manifest(output_dir, manifest):
    """Atomically record production metadata beside prepared inputs."""
    path = Path(output_dir) / 'manifest.json'
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
    temporary.replace(path)


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def plan_sha256(manifest):
    """Checksum of immutable plan metadata, excluding execution state/history."""
    keys = ('schema_version', 'production_id', 'config', 'mode', 'image', 'jobs',
            'prepared_input_sha256', 'execution', 'execution_input_sha256')
    try:
        data = {key: manifest[key] for key in keys}
    except KeyError as exc:
        raise ValueError(f'Missing production metadata: {exc}') from exc
    if 'recovery' in manifest:
        data['recovery'] = manifest['recovery']
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def production_path(output_dir, relative):
    """Resolve saved relative paths inside this production, including symlinks."""
    root = Path(output_dir).resolve()
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise ValueError(f'Invalid saved input path: {relative}')
    path = root / relative
    if not path.resolve().is_relative_to(root):
        raise ValueError(f'Saved input path escapes production: {relative}')
    return path


@contextmanager
def execution_lock(output_dir):
    """Nonblocking advisory lock shared by cooperating runner processes."""
    with open(Path(output_dir) / '.execution.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('Another process is using this production') from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def read_production_manifest(output_dir):
    """Read recorded metadata only; does not query Condor or validate outputs."""
    with open(Path(output_dir) / 'manifest.json') as source:
        return json.load(source)

