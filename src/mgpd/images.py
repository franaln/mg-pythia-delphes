"""Discover container images and freeze their identity for prepared productions."""
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import quote, urlparse
from urllib.request import urlopen

from .storage import file_sha256

IMAGE_DIRECTORY = Path('/opt/images')
REPOSITORY = 'franaln/mg-pythia-delphes'
_DIGEST = r'sha256:[a-f0-9]{64}'


def apptainer_reference(reference, directory=None):
    """Expand packaged names/version selectors; preserve explicit file paths."""
    directory = Path(directory) if directory is not None else IMAGE_DIRECTORY
    value = str(reference)
    if '/' not in value and not value.endswith('.sif'):
        if value.startswith('mg-pythia-delphes:'):
            value = value.replace(':', '-', 1)
        elif re.fullmatch(r'\d+\.\d+\.\d+(?:-[A-Za-z0-9_.-]+)?|latest', value):
            value = 'mg-pythia-delphes-' + value
        if value.startswith('mg-pythia-delphes-'):
            return str(directory / (value + '.sif'))
        raise ValueError(f'Unknown Apptainer image name: {value}; use a version or a SIF path')
    return value


def docker_reference(reference):
    value = str(reference)
    if re.fullmatch(r'\d+\.\d+\.\d+(?:-[A-Za-z0-9_.-]+)?|latest', value):
        return REPOSITORY + ':' + value
    return value


def pin_image(reference, mode):
    """Resolve installed bytes only: never pull or run a container at preparation."""
    if mode != 'local-docker':
        path = Path(reference).expanduser().resolve(strict=True)
        if not path.is_file():
            raise ValueError(f'Image is not a file: {path}')
        return str(path), {'kind': 'sif', 'requested': reference, 'path': str(path),
                           'sha256': file_sha256(path)}
    try:
        result = subprocess.run(['docker', 'image', 'inspect', reference], check=True,
                                capture_output=True, text=True, timeout=60)
    except subprocess.CalledProcessError as exc:
        raise ValueError(f'Docker image is not available locally: {reference}; run docker pull first') from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError('Docker image inspection timed out') from exc
    data = json.loads(result.stdout)[0]
    image_id = data['Id']
    if not re.fullmatch(_DIGEST, image_id):
        raise ValueError('Docker did not return an immutable image ID')
    repository = reference.split('@')[0]
    if ':' in repository.rsplit('/', 1)[-1]:
        repository = repository.rsplit(':', 1)[0]
    digests = [item for item in data.get('RepoDigests', [])
               if item.startswith(repository + '@') and re.fullmatch(_DIGEST, item.split('@')[-1])]
    pinned = sorted(digests)[0] if digests else image_id
    return pinned, {'kind': 'docker', 'requested': reference, 'reference': pinned, 'image_id': image_id}


def verify_image(reference, identity):
    """Check SIF bytes before execution; Docker references already address immutable content."""
    if not isinstance(identity, dict):
        raise ValueError('Production has no pinned image identity; prepare a new production')
    if identity.get('kind') == 'sif':
        path = Path(reference)
        if (str(path) != identity.get('path') or str(path.resolve()) != str(path)
                or not path.is_file() or file_sha256(path) != identity.get('sha256')):
            raise ValueError('Prepared SIF image is missing or changed; restore the original image')
    elif identity.get('kind') == 'docker':
        if reference != identity.get('reference') or not re.fullmatch(r'(?:[^@]+@)?' + _DIGEST, reference):
            raise ValueError('Prepared Docker image reference is not immutable')
    else:
        raise ValueError('Unsupported image identity')


def list_sif_images(directory=None):
    directory = Path(directory) if directory is not None else IMAGE_DIRECTORY
    if not directory.is_dir():
        raise FileNotFoundError(f'Image directory does not exist: {directory}')
    entries = []
    for path in sorted(directory.glob('mg-pythia-delphes-*.sif')):
        name = path.name[len('mg-pythia-delphes-'):-4]
        entries.append({'name': name, 'reference': str(path), 'target': str(path.resolve()),
                        'recommended': path.is_symlink() and bool(re.fullmatch(r'\d+\.\d+\.\d+', name)),
                        'available': path.is_file()})
    return entries


def list_docker_images(repository=REPOSITORY, remote=False):
    if remote:
        if not re.fullmatch(r'[a-z0-9_-]+/[a-z0-9_.-]+', repository):
            raise ValueError('Remote listing requires a Docker Hub namespace/repository')
        url = f'https://hub.docker.com/v2/repositories/{quote(repository, safe="/")}/tags?page_size=100'
        entries, seen = [], set()
        while url:
            parsed = urlparse(url)
            if parsed.scheme != 'https' or parsed.netloc != 'hub.docker.com' or url in seen or len(seen) >= 100:
                raise ValueError('Invalid Docker Hub pagination')
            seen.add(url)
            try:
                with urlopen(url, timeout=30) as response:
                    data = json.load(response)
            except OSError as exc:
                raise ValueError(f'Cannot query Docker Hub: {exc}') from exc
            for tag in data['results']:
                name = tag['name']
                entries.append({'name': name, 'reference': f'{repository}:{name}',
                                'digest': tag.get('digest'),
                                'recommended': bool(re.fullmatch(r'\d+\.\d+\.\d+', name))})
            url = data.get('next')
        return sorted(entries, key=lambda entry: entry['name'])
    try:
        result = subprocess.run(['docker', 'image', 'ls', '--digests', '--format', '{{json .}}', repository],
                                check=True, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired as exc:
        raise ValueError('Docker image listing timed out') from exc
    entries = []
    for line in result.stdout.splitlines():
        item = json.loads(line)
        if item['Tag'] == '<none>':
            continue
        entries.append({'name': item['Tag'], 'reference': f"{item['Repository']}:{item['Tag']}",
                        'digest': item.get('Digest'),
                        'recommended': bool(re.fullmatch(r'\d+\.\d+\.\d+', item['Tag']))})
    return sorted(entries, key=lambda entry: entry['name'])
