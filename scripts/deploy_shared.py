#!/usr/bin/env python3
"""Install versioned CLI/MCP environments, select current, or create launchers."""
import argparse
from email.parser import BytesParser
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import uuid
import zipfile


def wheel_version(wheel, distribution):
    with zipfile.ZipFile(wheel) as archive:
        names = [name for name in archive.namelist() if name.endswith('.dist-info/METADATA')]
        if len(names) != 1:
            raise ValueError('Expected one wheel distribution')
        metadata = BytesParser().parsebytes(archive.read(names[0]))
    if metadata['Name'] != distribution:
        raise ValueError(f'Wheel must contain {distribution}')
    return checked_version(metadata['Version'])


def checked_version(version):
    if not version or any(character not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.!+-_' for character in version):
        raise ValueError('Invalid environment version')
    if version in ('.', '..'):
        raise ValueError('Invalid environment version')
    return version


def environment_path(prefix, version):
    environment = prefix / f'mgpd-{checked_version(version)}'
    if environment.is_symlink():
        raise ValueError('Versioned environment must not be a symlink')
    return environment


def validate_environment(environment, version, require_mcp=False):
    interpreter = environment / 'bin/python'
    if not interpreter.is_file():
        raise ValueError(f'Environment is not installed: {environment}')
    ready = environment / '.mgpd-install-complete'
    if (environment / '.mgpd-installing').exists() or (ready.exists() and ready.read_text().strip() != version):
        raise ValueError('Environment installation is incomplete; inspect it before activation')
    has_mcp = (environment / 'bin/mgpd-mcp').is_file()
    if require_mcp and not has_mcp:
        raise ValueError('Environment has no MCP adapter')
    subprocess.run([str(interpreter), '-m', 'pip', 'check'], check=True)
    subprocess.run([str(environment / 'bin/mgpd'), '--version'], check=True)
    code = 'import importlib.metadata as m, mgpd, uproot, sys; '
    condition = 'm.version("mg-pythia-delphes") == mgpd.__version__ == sys.argv[1]'
    if has_mcp:
        subprocess.run([str(environment / 'bin/mgpd-mcp'), '--version'], check=True)
        code += 'import mgpd_mcp, mcp; from mgpd_mcp.server import create_server; '
        condition += ' and m.version("mg-pythia-delphes-mcp") == mgpd_mcp.__version__ == sys.argv[1]'
    code += 'sys.exit(0 if (' + condition + ') else "Installed versions do not match requested version")'
    subprocess.run([str(interpreter), '-c', code, version], check=True)
    return has_mcp


def deploy(wheel, prefix, python, *, mcp_wheel=None, version=None):
    """Install and validate a new environment. Never change current or wrappers."""
    actual = wheel_version(wheel, 'mg-pythia-delphes')
    if version is not None and checked_version(version) != actual:
        raise ValueError(f'Expected version {version}, core wheel is {actual}')
    if mcp_wheel is not None and wheel_version(mcp_wheel, 'mg-pythia-delphes-mcp') != actual:
        raise ValueError('Core and MCP wheel versions must match')
    minimum = (3, 11) if mcp_wheel is not None else (3, 9)
    subprocess.run([python, '-c', 'import sys; sys.exit(0 if sys.version_info >= '
                    + repr(minimum) + ' else "Python ' + '.'.join(map(str, minimum)) + '+ is required")'], check=True)
    prefix.mkdir(parents=True, exist_ok=True)
    environment = environment_path(prefix, actual)
    environment.mkdir()  # Reserve final location; never move venv launchers.
    marker = environment / '.mgpd-installing'
    marker.write_text(actual + '\n')
    try:
        subprocess.run([python, '-m', 'venv', str(environment)], check=True)
        command = [str(environment / 'bin/python'), '-m', 'pip', 'install', f'{wheel}[validation]']
        if mcp_wheel is not None:
            command.append(str(mcp_wheel))
        subprocess.run(command, check=True)
        # Keep the incomplete marker until every check has passed.
        marker.unlink()
        try:
            validate_environment(environment, actual, require_mcp=mcp_wheel is not None)
        except BaseException:
            marker.write_text(actual + '\n')
            raise
        (environment / '.mgpd-install-complete').write_text(actual + '\n')
    except BaseException:
        print(f'Installation failed; environment retained: {environment}', file=sys.stderr)
        raise
    print(f'Installed: {environment} (current unchanged)')
    return actual


def current_target(prefix):
    current = prefix / 'mgpd-current'
    if current.exists() and not current.is_symlink():
        raise ValueError(f'Current path must be a symlink: {current}')
    return current


def select_current(prefix, version, require_mcp=False):
    environment = environment_path(prefix, version)
    validate_environment(environment, version, require_mcp=require_mcp)
    current = current_target(prefix)
    previous = os.readlink(current) if current.is_symlink() else None
    temporary = prefix / f'.mgpd-current-{uuid.uuid4().hex}'
    try:
        temporary.symlink_to(environment)
        os.replace(temporary, current)
    finally:
        if temporary.is_symlink():
            temporary.unlink()
    print(f'Current: {current} -> {environment}')
    if previous:
        print(f'Previous target (rollback): {previous}')


def wrapper_contents(prefix, has_mcp):
    current = current_target(prefix)
    names = ['mgpd', 'mgpd-mcp'] if has_mcp else ['mgpd']
    contents = {name: '#!/bin/sh\nexec ' + shlex.quote(str(current / 'bin' / name)) + ' "$@"\n' for name in names}
    return contents


def preflight_wrappers(prefix, bin_dir, has_mcp):
    contents = wrapper_contents(prefix, has_mcp)
    if bin_dir.exists() and not bin_dir.is_dir():
        raise ValueError('Wrapper destination must be a directory')
    # Preflight both names before writing either; keep unrelated commands intact.
    for name, content in contents.items():
        path = bin_dir / name
        if path.is_symlink() or (path.exists() and (not path.is_file() or path.read_text() != content or not os.access(path, os.X_OK))):
            raise FileExistsError(f'Unmanaged wrapper already exists: {path}')


def create_wrappers(prefix, bin_dir, has_mcp):
    if not current_target(prefix).is_dir():
        raise ValueError('Select a current environment before creating wrappers')
    preflight_wrappers(prefix, bin_dir, has_mcp)
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name, content in wrapper_contents(prefix, has_mcp).items():
        path = bin_dir / name
        if path.exists():
            print(f'Existing managed wrapper: {path}')
            continue
        with path.open('x') as stream:
            stream.write(content)
        path.chmod(0o755)
        print(f'Created wrapper: {path}')


def build_wheels(source, outdir, with_mcp=False, version=None):
    """Build/check wheel metadata and publish without replacing existing files."""
    source = source.resolve(strict=True)
    outdir = outdir.resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.mgpd-build-', dir=outdir.parent) as directory:
        staging = Path(directory)
        core_out = staging / 'core'
        subprocess.run([sys.executable, '-m', 'build', '--wheel', '--outdir', str(core_out), str(source)], check=True)
        core, = core_out.glob('*.whl')
        actual = wheel_version(core, 'mg-pythia-delphes')
        if version is not None and actual != checked_version(version):
            raise ValueError(f'Expected version {version}, built core is {actual}')
        wheels = [core]
        if with_mcp:
            mcp_out = staging / 'mcp'
            subprocess.run([sys.executable, '-m', 'build', '--wheel', '--outdir', str(mcp_out),
                            str(source / 'packages/mg-pythia-delphes-mcp')], check=True)
            adapter, = mcp_out.glob('*.whl')
            if wheel_version(adapter, 'mg-pythia-delphes-mcp') != actual:
                raise ValueError('Built core and MCP wheel versions must match')
            wheels.append(adapter)
        destinations = [outdir / wheel.name for wheel in wheels]
        for destination in destinations:
            if destination.exists() or destination.is_symlink():
                raise FileExistsError(f'Wheel already exists: {destination}; choose a new --outdir')
        published = []
        try:
            for wheel, destination in zip(wheels, destinations):
                os.link(wheel, destination)  # Same filesystem; atomic create, no overwrite.
                published.append(destination)
        except BaseException:
            for destination in published:
                destination.unlink()
            raise
    for destination in destinations:
        print(f'Built: {destination}')
    return destinations[0], destinations[1] if with_mcp else None


def main(argv=None):

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', action='store_true', help='Only build release wheels as a normal user; no shared venv or current changes')
    parser.add_argument('--outdir', type=Path, help='Build-only output directory (default: ./dist)')
    parser.add_argument('--source', type=Path, help='Source checkout for building; default: checkout containing this script')
    parser.add_argument('--deploy-venv', action='store_true', help='Install a new versioned venv; do not select it unless requested')
    parser.add_argument('--update-current', action='store_true', help='Validate and select the requested installed version')
    parser.add_argument('--create-wrapper', action='store_true', help='Create mgpd and, if installed, mgpd-mcp launchers')
    parser.add_argument('--prefix', type=Path, default=Path('/opt/venvs'))
    parser.add_argument('--python', default=sys.executable, help='Venv interpreter; Python 3.11+ is required with MCP')
    parser.add_argument('--version', help='Installed version to select, or expected version of wheels being built/deployed')
    parser.add_argument('--wheel', type=Path, help='Tested core wheel; otherwise build the current checkout')
    parser.add_argument('--mcp-wheel', type=Path, help='Tested MCP wheel; must match the core wheel version')
    parser.add_argument('--with-mcp', action='store_true', help='Include MCP (build it if no --mcp-wheel); require it on activation')
    parser.add_argument('--bin-dir', type=Path, help='Launcher directory; requires --create-wrapper')

    args = parser.parse_args(argv)

    if not (args.build or args.deploy_venv or args.update_current or args.create_wrapper):
        parser.error('Choose --build, --deploy-venv, --update-current and/or --create-wrapper')
    if args.build and (args.deploy_venv or args.update_current or args.create_wrapper):
        parser.error('--build is a standalone action; install/activate the resulting wheels separately')
    if args.outdir is not None and not args.build:
        parser.error('--outdir requires --build')
    if args.source is not None and not (args.build or (args.deploy_venv and not args.wheel)):
        parser.error('--source requires building from source')
    if bool(args.bin_dir) != args.create_wrapper:
        parser.error('--create-wrapper and --bin-dir must be supplied together')
    if not args.deploy_venv and (args.wheel or args.mcp_wheel):
        parser.error('Wheel arguments require --deploy-venv')
    if not args.build and not args.deploy_venv and not args.version:
        parser.error('--version is required when not deploying a venv')
    if args.wheel and args.with_mcp and not args.mcp_wheel:
        parser.error('Supply --mcp-wheel with a tested --wheel; do not mix wheel and checkout releases')
    if args.mcp_wheel and not args.wheel:
        parser.error('--mcp-wheel requires the matching --wheel')
    os.umask(0o022)
    try:
        version = checked_version(args.version) if args.version else None
        source = args.source or Path(__file__).resolve().parents[1]
        if args.build:
            build_wheels(source, args.outdir or Path('dist'), with_mcp=args.with_mcp, version=version)
            return 0
        prefix = args.prefix.resolve()
        require_mcp = args.with_mcp or args.mcp_wheel is not None
        with tempfile.TemporaryDirectory(prefix='mgpd-release-') as directory:
            if args.deploy_venv:
                if args.wheel:
                    wheel = args.wheel.resolve(strict=True)
                    mcp_wheel = args.mcp_wheel.resolve(strict=True) if args.mcp_wheel else None
                else:
                    wheel, mcp_wheel = build_wheels(source, Path(directory) / 'wheels',
                                                   with_mcp=args.with_mcp, version=version)
                version = deploy(wheel, prefix, args.python, mcp_wheel=mcp_wheel, version=version)
            environment = environment_path(prefix, version)
            if args.create_wrapper:
                has_mcp = validate_environment(environment, version, require_mcp=require_mcp)
                if not args.update_current and current_target(prefix).resolve() != environment:
                    raise ValueError('Requested version is not current; select it with --update-current first')
                preflight_wrappers(prefix, args.bin_dir.resolve(), has_mcp)
            if args.update_current:
                select_current(prefix, version, require_mcp=require_mcp)
            if args.create_wrapper:
                create_wrappers(prefix, args.bin_dir.resolve(), has_mcp)
    except (OSError, ValueError, zipfile.BadZipFile, subprocess.CalledProcessError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
