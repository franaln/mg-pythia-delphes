"""Merge explicitly selected output archives from one physics sample."""
import argparse
import gzip
import os
from pathlib import Path, PurePosixPath
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile

DOCKER_IMAGE = 'franaln/mg-pythia-delphes:3.5.6'
APPTAINER_IMAGE = '/mnt/R5/images/mg-pythia-delphes-3.5.6.sif'


def build_merge_command(workdir, command, backend, image):
    """Bind a private workspace at /work and initialize the container tools."""
    shell = 'source /setup_mg_pythia_delphes.sh && cd /work && ' + command
    if backend == 'local-docker':
        return ['docker', 'run', '--rm', '-u', f'{os.getuid()}:{os.getgid()}',
                '-v', f'{workdir}:/work', image, shell]
    if backend == 'local-apptainer':
        return ['apptainer', 'exec', '--bind', f'{workdir}:/work', image,
                '/bin/bash', '-l', '-c', shell]
    raise ValueError('Merge backend must be local-docker or local-apptainer')


def _extract(archive, destination):
    """Copy regular files only; reject traversal, links and duplicate members."""
    seen = set()
    with tarfile.open(archive, 'r:gz') as source:
        for member in source:
            name = PurePosixPath(member.name)
            if name.is_absolute() or '..' in name.parts:
                raise ValueError(f'Unsafe archive member: {member.name}')
            if member.isdir():
                continue
            if not member.isfile():
                raise ValueError(f'Unsupported archive member: {member.name}')
            target = destination.joinpath(*name.parts)
            if target in seen:
                raise ValueError(f'Duplicate archive member: {member.name}')
            seen.add(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(member) as stream, target.open('wb') as out:
                shutil.copyfileobj(stream, out)


def _decompress(path):
    with gzip.open(path, 'rb') as source, path.with_suffix('').open('wb') as out:
        shutil.copyfileobj(source, out)
    path.unlink()


def merge_outputs(inputs, output, *, backend=None, image=None,
                  extract_lhe=False, keep_all=False):
    """Merge one sample's archives, publishing only after all commands succeed.

    Inputs must represent distinct jobs from the same physics sample. This API
    does not infer recovery ownership or combine cross sections statistically.
    Existing destinations are never overwritten. HepMC merging is unsupported.
    """
    archives = [Path(value).resolve(strict=True) for value in inputs]
    if not archives or len(set(archives)) != len(archives):
        raise ValueError('Provide at least one archive, without duplicate paths')
    if any(not path.is_file() for path in archives):
        raise ValueError('Merge inputs must be archive files')
    destination = Path(output).absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f'Output already exists: {destination}')
    if not destination.parent.is_dir():
        raise ValueError('Output parent directory must exist')
    backend = backend or ('local-apptainer' if socket.gethostname().split('.')[0].lower() == 'jupiter'
                          else 'local-docker')
    if backend not in ('local-docker', 'local-apptainer'):
        raise ValueError('Merge backend must be local-docker or local-apptainer')
    image = image or (APPTAINER_IMAGE if backend == 'local-apptainer' else DOCKER_IMAGE)
    with tempfile.TemporaryDirectory(prefix='.mgpd-merge-', dir=destination.parent) as temporary:
        work = Path(temporary)
        all_dir = work / 'all'
        merged = work / 'merged'
        merged.mkdir()
        groups = {'lhe': [], 'root': [], 'lhco': []}
        identities = set()
        for index, archive in enumerate(archives):
            extracted = all_dir / f'{index:05d}'
            _extract(archive, extracted)
            found = {kind: sorted(extracted.rglob(pattern)) for kind, pattern in (
                ('lhe', '*unweighted_events.lhe.gz'),
                ('root', '*_delphes_events.root'), ('lhco', '*_delphes_events.lhco'))}
            if not any(found.values()):
                raise ValueError(f'No supported merge outputs in {archive}')
            for kind, files in found.items():
                if len(files) > 1:
                    raise ValueError(f'Multiple {kind} outputs in {archive}; select one sample/run per archive')
                for path in files:
                    identity = (kind, path.name)
                    if identity in identities:
                        raise ValueError(f'Duplicate job output name: {path.name}')
                    identities.add(identity)
                groups[kind].extend(files)
        # Reject partial collections rather than silently losing events in a format.
        for kind, files in groups.items():
            if files and len(files) != len(archives):
                raise ValueError(f'{kind} output is missing from some input archives')

        def relative(path):
            return str(path.relative_to(work))

        def run(arguments, expected):
            subprocess.run(build_merge_command(work, shlex.join(arguments), backend, image), check=True)
            if not expected.is_file() or expected.stat().st_size == 0:
                raise ValueError(f'Merge command produced no output: {expected.name}')

        if groups['lhe']:
            target = merged / 'merged_unweighted_events.lhe.gz'
            run(['/mg_pythia_delphes/MG5_aMC/Template/LO/bin/internal/merge.pl',
                 *map(relative, groups['lhe']), relative(target), 'banner.txt'], target)
        if groups['root']:
            target = merged / 'merged_delphes_events.root'
            run(['hadd', relative(target), *map(relative, groups['root'])], target)
        if groups['lhco']:
            root = merged / 'merged_delphes_events.root'
            if not groups['root']:
                root = work / 'lhco.root'
                run(['lhco2root', relative(root), *map(relative, groups['lhco'])], root)
            target = merged / 'merged_delphes_events.lhco'
            run(['root2lhco', relative(root), relative(target)], target)
        if extract_lhe:
            for path in merged.glob('*.lhe.gz'):
                _decompress(path)
            if keep_all:
                for path in all_dir.rglob('*.lhe.gz'):
                    _decompress(path)
        if keep_all:
            shutil.move(str(all_dir), str(merged / 'all'))
        # Reserve the destination without clobbering another caller's output.
        if str(destination).endswith('.tar.gz'):
            staged = work / 'result.tar.gz'
            with tarfile.open(staged, 'w:gz') as archive:
                for path in sorted(merged.iterdir()):
                    archive.add(path, arcname=path.name)
            os.link(staged, destination)
        else:
            destination.mkdir()
            try:
                for path in merged.iterdir():
                    shutil.move(str(path), str(destination / path.name))
            except BaseException:
                shutil.rmtree(destination)
                raise
    return str(destination)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Merge output archives from one sample and distinct jobs')
    parser.add_argument('inputs', nargs='+', help='Explicit job output archives (.tar.gz)')
    parser.add_argument('-o', '--output', required=True, help='New output directory or .tar.gz archive')
    parser.add_argument('-e', '--extract-lhe', action='store_true')
    parser.add_argument('-k', '--keep-all', action='store_true', help='Retain extracted inputs under all/')
    parser.add_argument('--backend', choices=['local-docker', 'local-apptainer'],
                        help='Default: Apptainer on jupiter, Docker elsewhere')
    parser.add_argument('--image', help='Override the container image/reference')
    args = parser.parse_args(argv)
    try:
        result = merge_outputs(args.inputs, args.output, backend=args.backend, image=args.image,
                               extract_lhe=args.extract_lhe, keep_all=args.keep_all)
    except (OSError, ValueError, tarfile.TarError, subprocess.CalledProcessError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1
    print(f'Merged outputs: {result}')
    return 0
