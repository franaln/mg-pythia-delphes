"""Reviewed YAML edits; no execution, cards editing or process-wide cwd changes."""
from contextlib import contextmanager
import difflib
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import yaml

from .config import validate_config

MAX_BYTES = 128 * 1024
class ConfigWriteUnknown(RuntimeError):
    """Atomic publication happened but durability/cleanup could not be confirmed."""


ROOT_KEYS = {'run', 'process', 'cards', 'options', 'expert', 'input_dir', 'input_dirs', 'input_files'}


def _digest(content):
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


def _bounded(content):
    if not isinstance(content, str) or len(content.encode('utf-8')) > MAX_BYTES:
        raise ValueError('Configuration text must be at most 128 KiB')
    return content


def _path(value, writable=False):
    path = Path(value).expanduser().absolute()
    # Refuse symlinks even inside a project: edits must name the actual file.
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError('Configuration paths must not contain symlinks')
    path = path.resolve()
    if writable:
        if path.suffix.lower() not in ('.yaml', '.yml'):
            raise ValueError('Destination must have a .yaml or .yml extension')
        if not path.parent.is_dir():
            raise FileNotFoundError('Destination parent must exist')
        for parent in path.parents:
            if (parent / 'manifest.json').exists():
                raise ValueError('Cannot edit files inside a prepared production')
    return path


def _read(path):
    with path.open('rb') as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('Configuration text must be at most 128 KiB')
    return data.decode('utf-8')


def _roundtrip(content):
    try:
        from ruamel.yaml import YAML
        from ruamel.yaml.util import load_yaml_guess_indent
        from ruamel.yaml.error import YAMLError
    except ImportError as exc:
        raise ValueError('Install mg-pythia-delphes[configuration] for YAML editing') from exc
    parser = YAML(typ='rt')
    parser.preserve_quotes = True
    parser.allow_duplicate_keys = False
    parser.width = 4096
    if '\r\n' in content:
        parser.line_break = '\r\n'
    try:
        document, indent, offset = load_yaml_guess_indent(content, yaml=parser)
    except YAMLError as exc:
        raise ValueError(f'Invalid YAML: {exc}') from exc
    if not isinstance(document, dict):
        raise ValueError('Configuration must be a YAML mapping')
    if indent:
        parser.indent(mapping=indent, sequence=indent, offset=offset or 0)
    return parser, document


def _data(content):
    value = yaml.safe_load(content)
    if not isinstance(value, dict):
        raise ValueError('Configuration must be a YAML mapping')
    json.dumps(value, allow_nan=False)  # Only JSON-representable configuration values.
    return value


def _validation(content, base_directory, input_validator=None):
    try:
        data = _data(content)
    except (ValueError, TypeError, yaml.YAMLError) as exc:
        return {'valid': False, 'errors': [str(exc)[:2000]]}
    if input_validator is not None:
        input_validator(data, base_directory)
    try:
        validate_config(data, base_directory=base_directory)
        return {'valid': True, 'errors': []}
    except (ValueError, TypeError, AttributeError, KeyError, OSError) as exc:
        return {'valid': False, 'errors': [str(exc)[:2000]]}


def inspect_config(path, *, base_directory=None, input_validator=None):
    """Read text/checksum and validate relative inputs against an explicit base."""
    source = _path(path)
    base = Path(base_directory or source.parent).resolve(strict=True)
    if not base.is_dir():
        raise ValueError('Base directory must be a directory')
    content = _read(source)
    validation = _validation(content, base, input_validator)
    try:
        _roundtrip(content)  # Also reject duplicate keys that PyYAML accepts.
        data = _data(content)
    except (ValueError, TypeError, yaml.YAMLError) as exc:
        data = None
        validation = {'valid': False, 'errors': [str(exc)[:2000]]}
    return {'path': str(source), 'base_directory': str(base), 'sha256': _digest(content),
            'content': content, 'config': data, 'validation': validation}


def _parts(value):
    if not isinstance(value, str) or not value:
        raise ValueError('Change paths must be nonempty strings')
    if value.startswith('/'):
        import re
        if re.search(r'~(?![01])', value):
            raise ValueError('Invalid JSON pointer escape')
        parts = tuple(item.replace('~1', '/').replace('~0', '~') for item in value[1:].split('/'))
    else:
        parts = tuple(value.split('.'))
    if any(not part for part in parts) or parts[0] not in ROOT_KEYS:
        raise ValueError(f'Unsupported configuration path: {value}')
    return parts


def _independent(value, seen=None):
    seen = set() if seen is None else seen
    if isinstance(value, (dict, list)):
        if id(value) in seen or getattr(value, 'merge', None):
            raise ValueError('Editing YAML aliases or merge keys is not supported; use independent mappings')
        seen.add(id(value))
        for child in value.values() if isinstance(value, dict) else value:
            _independent(child, seen)


def _literal(value):
    # The runner uses YAML 1.1 (PyYAML). Quote strings that YAML 1.2 would emit
    # plainly but the runner would interpret as booleans, numbers or dates.
    from ruamel.yaml.scalarstring import DoubleQuotedScalarString, LiteralScalarString
    if isinstance(value, str):
        if '\n' in value:
            return LiteralScalarString(value)
        try:
            plain = yaml.safe_load(value)
        except yaml.YAMLError:
            plain = None
        return value if isinstance(plain, str) and plain == value else DoubleQuotedScalarString(value)
    if isinstance(value, list):
        return [_literal(item) for item in value]
    if isinstance(value, dict):
        return {_literal(key): _literal(item) for key, item in value.items()}
    return value


def _parent(document, parts, create=False):
    parent = document
    for part in parts[:-1]:
        if part not in parent and create:
            parent[_literal(part)] = {}
        if part not in parent or not isinstance(parent[part], dict):
            raise ValueError('Change paths may traverse mappings only; replace lists as a whole')
        parent = parent[part]
    return parent


def _review_digest(proposal):
    return hashlib.sha256(json.dumps(proposal, sort_keys=True, allow_nan=False).encode()).hexdigest()


def plan_config_change(path, *, changes=None, remove=None, output=None, base_directory=None, input_validator=None):
    """Create a reviewable edit/save-as proposal from existing YAML; writes nothing.

    Paths use dotted mapping keys or JSON pointers (for keys containing dots).
    Lists and process strings are replaced as a whole. Invalid proposals can be
    reviewed but cannot be applied. Relative inputs use base_directory, which
    defaults to the source file's parent, not the destination's parent.
    """
    source = _path(path)
    target = _path(output or source, writable=True)
    base = Path(base_directory or source.parent).resolve(strict=True)
    if not base.is_dir():
        raise ValueError('Base directory must be a directory')
    if source != target and target.exists():
        raise FileExistsError('Destination already exists')
    original = _read(source)
    parser, document = _roundtrip(original)
    _independent(document)
    changes = {} if changes is None else changes
    remove = [] if remove is None else remove
    if not isinstance(changes, dict) or not isinstance(remove, list) or len(changes) + len(remove) > 200:
        raise ValueError('Use a changes mapping and remove list with at most 200 paths')
    # Reject non-JSON values, including NaN and cyclic structures.
    changes = json.loads(json.dumps(changes, allow_nan=False))
    edits = [(_parts(key), value) for key, value in changes.items()]
    removals = [_parts(key) for key in remove]
    paths = [parts for parts, _ in edits] + removals
    for index, parts in enumerate(paths):
        for other in paths[index + 1:]:
            if parts[:len(other)] == other or other[:len(parts)] == parts:
                raise ValueError('Overlapping or duplicate change/removal paths')
    before = _data(original)
    for parts, value in edits:
        _parent(document, parts, create=True)[_literal(parts[-1])] = _literal(value)
    for parts in removals:
        parent = _parent(document, parts)
        if parts[-1] not in parent:
            raise ValueError('Cannot remove a missing setting')
        del parent[parts[-1]]
    if source == target and json.dumps(document, sort_keys=True, allow_nan=False) == json.dumps(before, sort_keys=True, allow_nan=False):
        raise ValueError('No configuration values changed')
    if paths:
        stream = io.StringIO()
        parser.dump(document, stream)
        content = _bounded(stream.getvalue())
    else:
        content = original
    proposal = {'schema_version': 1, 'source': str(source), 'destination': str(target),
                'base_directory': str(base), 'source_sha256': _digest(original),
                'content_sha256': _digest(content), 'content': content,
                'action': 'edit' if source == target else 'create',
                'diff': ''.join(difflib.unified_diff(original.splitlines(True) if source == target else [],
                                                  content.splitlines(True), fromfile=str(source) if source == target else '/dev/null',
                                                  tofile=str(target))),
                'validation': _validation(content, base, input_validator)}
    return {**proposal, 'change_sha256': _review_digest(proposal)}


@contextmanager
def _lock(path):
    flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path.parent / ('.' + path.name + '.mgpd.lock'), flags, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('Another configuration change is in progress') from exc
        yield
    finally:
        os.close(descriptor)


def apply_config_change(proposal, change_sha256, *, input_validator=None):
    """Apply an exact reviewed proposal with source compare/check and atomic save.

    Advisory locks serialize cooperating writers. External editors do not honor
    them: source hashes are rechecked immediately before publication, but this
    is not a filesystem compare-and-swap against arbitrary external writers.
    """
    if not isinstance(proposal, dict):
        raise ValueError('A reviewed proposal is required')
    candidate = {key: value for key, value in proposal.items() if key != 'change_sha256'}
    if candidate.get('schema_version') != 1 or _review_digest(candidate) != change_sha256:
        raise ValueError('Change checksum does not match the reviewed proposal')
    if proposal.get('change_sha256') != change_sha256:
        raise ValueError('Proposal checksum is missing or changed')
    source = _path(candidate['source'])
    target = _path(candidate['destination'], writable=True)
    if candidate['action'] != ('edit' if source == target else 'create'):
        raise ValueError('Invalid configuration action')
    content = _bounded(candidate['content'])
    if _digest(content) != candidate['content_sha256']:
        raise ValueError('Proposed content changed')
    with _lock(target):
        if _digest(_read(source)) != candidate['source_sha256']:
            raise ValueError('Source changed since review; plan again')
        if source != target and (target.exists() or target.is_symlink()):
            raise FileExistsError('Destination already exists')
        _roundtrip(content)
        base = Path(candidate['base_directory']).resolve(strict=True)
        if not base.is_dir():
            raise ValueError('Base directory must be a directory')
        validation = _validation(content, base, input_validator)
        if not validation['valid']:
            raise ValueError('Configuration is invalid: ' + '; '.join(validation['errors']))
        descriptor, temporary = tempfile.mkstemp(prefix='.' + target.name + '.', dir=target.parent)
        published = False
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8', newline='') as stream:
                stream.write(content)
                os.fchmod(stream.fileno(), source.stat().st_mode & 0o777 if source == target else 0o600)
                stream.flush()
                os.fsync(stream.fileno())
            _path(target, writable=True)
            if _digest(_read(source)) != candidate['source_sha256']:
                raise ValueError('Source changed while applying; plan again')
            if source == target:
                os.replace(temporary, target)
            else:
                os.link(temporary, target)  # Atomic create, refuses an existing name.
            published = True
            descriptor = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            if published:
                raise ConfigWriteUnknown('Configuration was published but durability is uncertain; inspect its checksum') from exc
            raise
        finally:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError as exc:
                if published:
                    raise ConfigWriteUnknown('Configuration was published but temporary-file cleanup failed; inspect its checksum') from exc
                raise
    return {'path': str(target), 'sha256': candidate['content_sha256'],
            'base_directory': candidate['base_directory'], 'action': candidate['action'],
            'change_sha256': change_sha256, 'validation': validation}
