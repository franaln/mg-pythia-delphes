#!/usr/bin/env python3
"""Install the checked-out release into a new shared venv and select it as current."""
import argparse
from email.parser import BytesParser
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid
import zipfile


def deploy(wheel, prefix, python, bin_dir=None):
    with zipfile.ZipFile(wheel) as archive:
        metadata = [name for name in archive.namelist() if name.endswith('.dist-info/METADATA')]
        if len(metadata) != 1:
            raise ValueError('Expected one wheel distribution')
        package = BytesParser().parsebytes(archive.read(metadata[0]))
    if package['Name'] != 'mg-pythia-delphes':
        raise ValueError('Wheel must contain mg-pythia-delphes')
    version = package['Version']
    if not version or '/' in version or '\\' in version or version in ('.', '..'):
        raise ValueError('Invalid wheel version')
    prefix.mkdir(parents=True, exist_ok=True)
    environment = prefix / f'mgpd-{version}'
    current = prefix / 'mgpd-current'
    if current.exists() and not current.is_symlink():
        raise ValueError(f'Current path must be a symlink: {current}')
    # mkdir reserves the final path; venv launchers must never be moved afterward.
    environment.mkdir()
    previous = os.readlink(current) if current.is_symlink() else None
    try:
        subprocess.run([python, '-m', 'venv', str(environment)], check=True)
        interpreter = str(environment / 'bin/python')
        subprocess.run([interpreter, '-m', 'pip', 'install', f'{wheel}[validation]'], check=True)
        subprocess.run([interpreter, '-m', 'pip', 'check'], check=True)
        subprocess.run([str(environment / 'bin/mgpd'), '--version'], check=True)
        subprocess.run([interpreter, '-c',
                        'import mgpd, uproot; import sys; '
                        'assert mgpd.__version__ == sys.argv[1]', version], check=True)
        if bin_dir is not None:
            bin_dir.mkdir(parents=True, exist_ok=True)
            wrapper = bin_dir / 'mgpd'
            # Never overwrite an existing administrator/user command.
            if not wrapper.exists() and not wrapper.is_symlink():
                import shlex
                with wrapper.open('x') as stream:
                    stream.write('#!/bin/sh\nexec ' + shlex.quote(str(current / 'bin/mgpd')) + ' "$@"\n')
                wrapper.chmod(0o755)
            else:
                raise FileExistsError(f'Wrapper already exists: {wrapper}; omit --bin-dir when upgrading')
        temporary = prefix / f'.mgpd-current-{uuid.uuid4().hex}'
        try:
            temporary.symlink_to(environment)
            os.replace(temporary, current)
        finally:
            if temporary.is_symlink():
                temporary.unlink()
    except BaseException:
        print(f'Installation failed; environment retained for diagnosis: {environment}', file=sys.stderr)
        raise
    print(f'Current: {current} -> {environment}')
    if previous:
        print(f'Previous target (rollback): {previous}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', type=Path, default=Path('/opt/venvs'))
    parser.add_argument('--python', default=sys.executable, help='Python interpreter used to create the venv')
    parser.add_argument('--wheel', type=Path, help='Use a tested wheel; default: build the current checkout')
    parser.add_argument('--bin-dir', type=Path, help='First install only: create an mgpd wrapper in this directory')
    args = parser.parse_args()
    os.umask(0o022)
    try:
        with tempfile.TemporaryDirectory(prefix='mgpd-release-') as directory:
            if args.wheel:
                wheel = args.wheel.resolve(strict=True)
            else:
                subprocess.run([args.python, '-m', 'build', '--wheel', '--outdir', directory,
                                str(Path(__file__).resolve().parents[1])], check=True)
                wheel, = Path(directory).glob('*.whl')
            deploy(wheel, args.prefix.resolve(), args.python,
                   args.bin_dir.resolve() if args.bin_dir else None)
    except (OSError, ValueError, zipfile.BadZipFile, subprocess.CalledProcessError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
