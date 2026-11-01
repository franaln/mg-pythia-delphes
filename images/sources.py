#!/usr/bin/env python3
"""Capture an explicit source lock, or fetch one archive against that lock."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import tarfile
import tempfile
from urllib.request import urlopen

TOOLS = {
    '3.3.2': {
        'root_v6.24.02.Linux-ubuntu20-x86_64-gcc9.3.tar.gz': 'https://root.cern/download/root_v6.24.02.Linux-ubuntu20-x86_64-gcc9.3.tar.gz',
        'MG5_aMC_v3.3.2.tar.gz': 'https://launchpad.net/mg5amcnlo/3.0/3.3.x/+download/MG5_aMC_v3.3.2.tar.gz',
        'HepMC-HEPMC_02_06_09.tar.gz': 'https://gitlab.cern.ch/hepmc/HepMC/-/archive/HEPMC_02_06_09/HepMC-HEPMC_02_06_09.tar.gz',
        'fastjet-3.3.4.tar.gz': 'https://fastjet.fr/repo/fastjet-3.3.4.tar.gz',
        'LHAPDF-6.3.0.tar.gz': 'https://lhapdf.hepforge.org/downloads/?f=LHAPDF-6.3.0.tar.gz',
        # 'pythia8306.tgz': 'https://pythia.org/download/pythia83/pythia8306.tgz',
        'pythia8306.tgz': 'https://gitlab.com/Pythia8/releases/-/archive/pythia8306/releases-pythia8306.tar.gz',
        # 'Delphes-3.5.0.tar.gz': 'http://cp3.irmp.ucl.ac.be/downloads/Delphes-3.5.0.tar.gz',
        'Delphes-3.5.0.tar.gz': 'https://github.com/delphes/delphes/archive/refs/tags/3.5.0.tar.gz',
        # 'MG5aMC_PY8_interface_V1.3.tar.gz': 'https://madgraph.phys.ucl.ac.be/Downloads/MG5aMC_PY8_interface/MG5aMC_PY8_interface_V1.3.tar.gz',
        'MG5aMC_PY8_interface_V1.3.tar.gz': 'https://madgraph.mi.infn.it/Downloads/MG5aMC_PY8_interface/MG5aMC_PY8_interface_V1.3.tar.gz',
    },

    '3.5.6': {
        'root_v6.30.06.Linux-ubuntu20.04-x86_64-gcc9.4.tar.gz':'https://root.cern/download/root_v6.30.06.Linux-ubuntu20.04-x86_64-gcc9.4.tar.gz',
        'MG5_aMC_v3.5.6.tar.gz': 'https://launchpad.net/mg5amcnlo/3.0/3.5.x/+download/MG5_aMC_v3.5.6.tar.gz',
        'HepMC-HEPMC_02_06_09.tar.gz': 'https://gitlab.cern.ch/hepmc/HepMC/-/archive/HEPMC_02_06_09/HepMC-HEPMC_02_06_09.tar.gz',
        'fastjet-3.4.2.tar.gz': 'https://fastjet.fr/repo/fastjet-3.4.2.tar.gz',
        'LHAPDF-6.5.1.tar.gz': 'https://lhapdf.hepforge.org/downloads/?f=LHAPDF-6.5.1.tar.gz',
        # 'pythia8306.tgz': 'https://pythia.org/download/pythia83/pythia8306.tgz',
        'pythia8306.tgz': 'https://gitlab.com/Pythia8/releases/-/archive/pythia8306/releases-pythia8306.tar.gz',
        # 'Delphes-3.5.0.tar.gz': 'http://cp3.irmp.ucl.ac.be/downloads/Delphes-3.5.0.tar.gz',
        'Delphes-3.5.0.tar.gz': 'https://github.com/delphes/delphes/archive/refs/tags/3.5.0.tar.gz',
        # 'MG5aMC_PY8_interface_V1.3.tar.gz': 'https://madgraph.phys.ucl.ac.be/Downloads/MG5aMC_PY8_interface/MG5aMC_PY8_interface_V1.3.tar.gz',
        'MG5aMC_PY8_interface_V1.3.tar.gz': 'https://madgraph.mi.infn.it/Downloads/MG5aMC_PY8_interface/MG5aMC_PY8_interface_V1.3.tar.gz',
        'TAUOLA.1.1.9-LHC.tar.gz': 'https://tauolapp.web.cern.ch/resources/TAUOLA.1.1.9/TAUOLA.1.1.9-LHC.tar.gz',
    }
}

PDFS = [
    'NNPDF23_nlo_as_0119',
    'NNPDF23_lo_as_0130_qed',
    'NNPDF30_nlo_as_0118',
    'NNPDF30_nlo_as_0118_hessian',
    'PDF4LHC15_nnlo_mc',
    'PDF4LHC21_mc',
]


def catalog(version):

    if version not in ('3.3.2', '3.5.6'):
        raise ValueError('Unsupported MadGraph version')

    files = TOOLS[version].copy()

    files.update({name + '.tar.gz': f'https://lhapdfsets.web.cern.ch/lhapdfsets/current/{name}.tar.gz' for name in PDFS if version == '3.5.6' or name != 'PDF4LHC21_mc'})

    return files


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def download(url, output, expected=None):
    if not url.startswith('https://'):
        raise ValueError('Source downloads require HTTPS')
    if output.exists():
        if expected is None or digest(output) == expected:
            with tarfile.open(output):
                pass
            return digest(output)
        raise ValueError(f'Cached source checksum mismatch: {output}')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, prefix='.source-', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            with urlopen(url, timeout=120) as response:
                if not response.geturl().startswith('https://'):
                    raise ValueError('Source redirected to an insecure URL')
                for chunk in iter(lambda: response.read(1024 * 1024), b''):
                    stream.write(chunk)
            stream.close()
            checksum = digest(temporary)
            if expected is not None and checksum != expected:
                raise ValueError(f'Source checksum mismatch: {url}')
            with tarfile.open(temporary):
                pass  # Reject HTML/error pages before any extraction.
            temporary.replace(output)
            return checksum
        finally:
            temporary.unlink(missing_ok=True)


def fetch(lock_path, filename, url, output):
    lock = json.loads(lock_path.read_text())
    if lock.get('schema_version') != 1:
        raise ValueError('Unsupported source lock')
    entry = lock['files'].get(filename)
    if not entry or entry['url'] != url or not re.fullmatch('[a-f0-9]{64}', entry['sha256']):
        raise ValueError(f'Missing/mismatching source lock entry: {filename}')
    return download(url, output, entry['sha256'])


def main(argv=None):

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)

    lock = commands.add_parser('lock', help='Capture hashes from HTTPS sources/cache; review before trusting')
    lock.add_argument('--version', choices=('3.3.2', '3.5.6'), action='append', required=True)
    lock.add_argument('--output', type=Path, required=True)
    lock.add_argument('--cache', type=Path, required=True)

    fetcher = commands.add_parser('fetch')
    fetcher.add_argument('--lock', type=Path, required=True)
    fetcher.add_argument('--file', required=True)
    fetcher.add_argument('--url', required=True)
    fetcher.add_argument('--output', type=Path, required=True)

    args = parser.parse_args(argv)

    try:
        if args.command == 'fetch':
            fetch(args.lock, args.file, args.url, args.output)
        else:
            if args.output.exists():
                raise FileExistsError('Source lock already exists; use a new output to review an update')
            files = {}
            for version in args.version:
                files.update(catalog(version))
            entries = {}
            for filename, url in files.items():
                print(f'Capturing {filename}', flush=True)
                entries[filename] = {'url': url, 'sha256': download(url, args.cache / filename)}
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open('x') as stream:
                json.dump({'schema_version': 1, 'files': entries}, stream, indent=2)
                stream.write('\n')
    except (OSError, ValueError, KeyError, tarfile.TarError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main())
