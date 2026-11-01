#!/usr/bin/env python3
"""Build, check, convert, publish and install versioned MGPD container images."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid

REPOSITORY = Path(__file__).resolve().parents[1]
VERSIONS = ('3.3.2', '3.5.6')

# A quick installation check, not an event-generation / physics validation.
CHECK = '''source /setup_mg_pythia_delphes.sh
for tool in mg5_aMC root hadd root2lhco lhco2root; do
    command -v "$tool"
done
test -s "$MG_DIR/MG5_aMC/VERSION"
cat "$MG_DIR/MG5_aMC/VERSION"
"$MG_DIR/bin/fastjet-config" --version
"$MG_DIR/bin/lhapdf-config" --version
python -c 'import ROOT; print("ROOT", ROOT.gROOT.GetVersion())'
for pdf in NNPDF23_lo_as_0130_qed NNPDF23_nlo_as_0119; do
    test -s "$MG_DIR/share/LHAPDF/$pdf/$pdf.info"
    test -s "$MG_DIR/share/LHAPDF/$pdf/${pdf}_0000.dat"
done
'''


def check_script(version):
    version_pattern = re.escape(version)
    return CHECK + "python -c " + shlex.quote(
        "from pathlib import Path; import sys,re; text=Path(sys.argv[1]).read_text(); "
        f"assert re.search(r'(?<![0-9.]){version_pattern}(?![0-9.])', text), text"
    ) + ' "$MG_DIR/MG5_aMC/VERSION"\n'


def run(command, cwd=None, capture=False):
    command = [str(item) for item in command]
    print('+ ' + shlex.join(command), flush=True)
    result = subprocess.run(command, cwd=cwd, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None)
    if capture:
        print(result.stdout, end='', flush=True)
        return result.stdout


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, value):
    """Replace metadata only after a complete same-directory write."""
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     prefix='.' + path.name, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def publish_file(source, destination, replace=False, expected_sha256=None):
    """Stage the entire file on the target filesystem before publishing it."""
    if destination.is_symlink():
        raise ValueError(f'Refusing symlink destination: {destination}')
    if destination.exists() and not replace:
        raise FileExistsError(f'Already exists: {destination}; use --replace to replace it')
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.' + destination.name,
                                     delete=False) as stream:
        temporary = Path(stream.name)
        try:
            digest = hashlib.sha256()
            with Path(source).open('rb') as original:
                for chunk in iter(lambda: original.read(1024 * 1024), b''):
                    stream.write(chunk)
                    digest.update(chunk)
            if expected_sha256 is not None and digest.hexdigest() != expected_sha256:
                raise ValueError('Source image changed during copying; destination was not published')
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.chmod(0o644)
        if replace:
            temporary.replace(destination)
        else:
            os.link(temporary, destination)  # Exclusive publication, including concurrent callers.
    finally:
        temporary.unlink(missing_ok=True)


def parser():

    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--version', choices=VERSIONS, action='append', required=True,
                        help='MadGraph version; repeat to process both supported versions')
    result.add_argument('--revision', help='Optional image revision, e.g. r1 (tag becomes 3.5.6-r1)')
    result.add_argument('--engine', choices=('podman', 'docker'), default='podman')
    result.add_argument('--build-jobs', type=int, help='Parallel compilation jobs')
    result.add_argument('--source-lock', default='images/sources.lock.json', help='Reviewed download lock relative to build context')
    result.add_argument('--source', type=Path, default=REPOSITORY, help='Repository/build context')
    result.add_argument('--output-dir', type=Path, default=REPOSITORY / 'dist/images')
    result.add_argument('--repository', default='franaln/mg-pythia-delphes', help='Destination OCI repository')
    result.add_argument('--build', action='store_true', help='Build a new local candidate')
    result.add_argument('--sif', action='store_true', help='Convert the exact local image using a temporary Docker archive')
    result.add_argument('--check', action='store_true', help='Check tools, ROOT import and two common PDF sets; no event generation')
    result.add_argument('--push', action='store_true', help='Publish checked OCI image; requires --check in this invocation')
    result.add_argument('--install-dir', type=Path, help='Install a checked SIF and metadata into an existing shared directory')
    result.add_argument('--replace', action='store_true', help='Allow replacing output SIF/shared installed files')
    result.add_argument('--no-cache', action='store_true', help='Disable build cache (requires --build)')
    result.add_argument('--dry-run', action='store_true', help='Print selected actions without commands, writes or tool requirements')

    return result


def dockerfile_path(args, version):
    folder = args.source / 'images'
    return folder / f'mg-pythia-delphes-{version}.Dockerfile'


def validate(args):
    if not any((args.build, args.sif, args.check, args.push, args.install_dir)):
        raise ValueError('Select --build, --sif, --check, --push or --install-dir')
    if args.build_jobs is not None and (args.build_jobs < 1 or not args.build):
        raise ValueError('--build-jobs requires --build and a positive integer')
    if args.push and not args.check:
        raise ValueError('--push requires --check in the same invocation')
    if args.no_cache and not args.build:
        raise ValueError('--no-cache requires --build')
    if args.revision and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', args.revision):
        raise ValueError('Invalid revision')
    if not re.fullmatch(r'[a-z0-9][a-z0-9./:_-]*', args.repository) or args.repository.endswith('/'):
        raise ValueError('Invalid repository')
    if ':' in args.repository.rsplit('/', 1)[-1]:
        raise ValueError('Repository must not include an image tag')
    args.source = args.source.expanduser().resolve()
    args.output_dir = args.output_dir.expanduser().resolve()
    if args.build:
        lock = (args.source / args.source_lock).resolve()
        if not lock.is_relative_to(args.source.resolve()) or not lock.is_file():
            raise ValueError('Checked build requires a source lock inside the build context; run images/sources.py lock first')
    if args.install_dir is not None:
        args.install_dir = args.install_dir.expanduser().resolve(strict=True)
        if not args.install_dir.is_dir():
            raise ValueError('Install destination must be a directory')
    for version in args.version:
        if args.build and not dockerfile_path(args, version).is_file():
            raise FileNotFoundError(f'Missing Dockerfile for {version}')
        tag = version + ('-' + args.revision if args.revision else '')
        name = f'mg-pythia-delphes-{tag}.sif'
        if (args.output_dir / (name + '.json')).is_symlink():
            raise ValueError('Image metadata must not be a symlink')
        if args.sif and ((args.output_dir / name).exists() or (args.output_dir / name).is_symlink()) and not args.replace:
            raise FileExistsError(f'Output already exists: {name}; use --replace')
        if args.install_dir:
            for filename in (name, name + '.json'):
                destination = args.install_dir / filename
                if destination.is_symlink() or destination.exists() and not args.replace:
                    raise FileExistsError(f'Install destination already exists: {destination}')


def process(args, version):

    tag = version + ('-' + args.revision if args.revision else '')

    reference = f'{args.repository}:{tag}'
    sif = args.output_dir / f'mg-pythia-delphes-{tag}.sif'

    metadata_path = sif.with_name(sif.name + '.json')
    if args.dry_run:
        actions = [name for name in ('build', 'sif', 'check', 'push') if getattr(args, name)]
        if args.install_dir:
            actions.append('install')
        print(f'{reference}: {", ".join(actions)}; engine={args.engine}; SIF={sif}')
        if args.install_dir:
            print(f'  install -> {args.install_dir / sif.name}')
        return

    needs_engine = args.build or args.sif or args.push or args.check
    for tool in ([args.engine] if needs_engine else []) + (['apptainer'] if args.sif or args.check and sif.exists() else []):
        if not shutil.which(tool):
            raise FileNotFoundError(f'Required tool unavailable: {tool}')
    if args.install_dir and not args.sif and not sif.is_file():
        raise FileNotFoundError(f'Build the SIF first: {sif}')
    if args.check or args.sif or args.build:
        args.output_dir.mkdir(parents=True, exist_ok=True)
    metadata = {'schema_version': 1, 'tag': tag, 'reference': reference, 'madgraph_version': version}
    if metadata_path.is_file():
        saved = json.loads(metadata_path.read_text())
        if not isinstance(saved, dict) or saved.get('schema_version') != 1:
            raise ValueError('Unsupported image metadata')
        metadata.update(saved)
        if metadata['tag'] != tag or metadata['reference'] != reference:
            raise ValueError('Existing SIF metadata belongs to another image reference')
    candidate = None
    candidate_built = False
    check = check_script(version)
    try:
        if needs_engine:
            local_reference = reference
            if args.build:
                candidate = f'localhost/mgpd-build:{uuid.uuid4().hex}'
                dockerfile = dockerfile_path(args, version)
                command = [args.engine, 'build']
                if args.engine == 'podman':
                    # OCI format drops Dockerfile SHELL metadata on affected Podman/Buildah versions.
                    command += ['--format', 'docker']
                command += ['--tag', candidate, '--file', dockerfile]
                command += ['--build-arg', 'SOURCE_LOCK=' + args.source_lock]
                if args.build_jobs is not None:
                    command += ['--build-arg', f'BUILD_JOBS={args.build_jobs}']
                metadata['source_lock_sha256'] = sha256(args.source / args.source_lock)
                if args.no_cache:
                    command.append('--no-cache')
                run([*command, args.source], cwd=args.source)
                candidate_built = True
                local_reference = candidate
                metadata['dockerfile_sha256'] = sha256(dockerfile)
            image_id = run([args.engine, 'image', 'inspect', '--format', '{{.Id}}', local_reference], capture=True).strip()
            if not re.fullmatch(r'(sha256:)?[a-fA-F0-9]{64}', image_id):
                raise ValueError('Engine did not return an immutable image ID')
            if metadata.get('image_id') != image_id:
                metadata.pop('oci_check', None)
            metadata['image_id'] = image_id
            if args.check:
                metadata['oci_check'] = run([args.engine, 'run', '--rm', '--entrypoint', '/bin/bash',
                                              image_id, '-e', '-o', 'pipefail', '-c', check], capture=True)[-32000:]
        if args.sif:
            # Conversion uses local bytes, never a mutable remote tag or a prior push.
            with tempfile.TemporaryDirectory(prefix='.mgpd-image-', dir=args.output_dir) as temporary:
                temporary = Path(temporary)
                archive, staged = temporary / 'image.tar', temporary / 'image.sif'
                command = [args.engine, 'save']
                if args.engine == 'podman':
                    command += ['--format', 'docker-archive']
                run([*command, '--output', archive, image_id])
                run(['apptainer', 'build', staged, 'docker-archive:' + str(archive)])
                if not staged.is_file() or not staged.stat().st_size:
                    raise ValueError('Apptainer produced no SIF')
                metadata.pop('sif_checked_sha256', None)
                if args.check:
                    metadata['sif_check'] = run(['apptainer', 'exec', '--cleanenv', staged,
                                                '/bin/bash', '-e', '-o', 'pipefail', '-c', check], capture=True)[-32000:]
                    metadata['sif_checked_sha256'] = sha256(staged)
                publish_file(staged, sif, args.replace)
            metadata['sif_sha256'] = sha256(sif)
            metadata['sif_image_id'] = image_id
        elif args.check and sif.is_file():
            if metadata.get('sif_sha256') != sha256(sif):
                raise ValueError('SIF checksum does not match metadata; rebuild with --sif')
            if metadata.get('sif_image_id') != image_id:
                raise ValueError('SIF belongs to another image')
            metadata['sif_check'] = run(['apptainer', 'exec', '--cleanenv', sif,
                                        '/bin/bash', '-e', '-o', 'pipefail', '-c', check], capture=True)[-32000:]
            metadata['sif_checked_sha256'] = sha256(sif)
        if args.build:
            run([args.engine, 'tag', image_id, reference])
        if args.push:
            # Retag the checked immutable ID immediately before publishing.
            run([args.engine, 'tag', image_id, reference])
            run([args.engine, 'push', reference])
        if needs_engine:
            metadata['updated_at'] = datetime.now(timezone.utc).isoformat()
            write_json(metadata_path, metadata)
        if args.install_dir:
            if metadata.get('sif_checked_sha256') != sha256(sif):
                raise ValueError('Install requires a checked, unchanged SIF; first run --sif --check')
            destination = args.install_dir / sif.name
            publish_file(sif, destination, args.replace, expected_sha256=metadata['sif_checked_sha256'])
            publish_file(metadata_path, destination.with_name(destination.name + '.json'), args.replace)
            print(f'Installed {destination}; SHA256 {metadata["sif_checked_sha256"]}')
    finally:
        if candidate_built:
            # Cleanup must not mask a failed build/check or alter the published tag.
            try:
                subprocess.run([args.engine, 'image', 'rm', candidate], check=False)
            except OSError:
                pass


def main(argv=None):

    arguments = parser()
    args = arguments.parse_args(argv)

    try:
        args.version = list(dict.fromkeys(args.version))
        validate(args)
        for version in args.version:
            process(args, version)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
