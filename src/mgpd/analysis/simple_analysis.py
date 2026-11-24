"""Delphes-to-slim conversion and ATLAS SimpleAnalysis execution in pinned containers.

This backend is separate from the YAML expression engine. It never imports
ROOT: the packaged converter runs inside the mg-pythia-delphes image (PyROOT
and Delphes), and ``simpleAnalysis`` runs inside an explicitly selected
SimpleAnalysis image. Plans pin both images and fingerprint every input;
execution publishes one new complete directory or nothing.
"""
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import tempfile

from .._version import __version__
from ..images import pin_image, verify_image
from ..merge import APPTAINER_IMAGE, DOCKER_IMAGE
from ..storage import file_sha256
from ..templates import _resource
from .datasets import resolve_dataset
from .integrity import checksum as _checksum

BACKENDS = ('local-apptainer', 'local-docker')
CONVERTER = 'simple_analysis/delphes2slim.py'
MG_SETUP = 'source /setup_mg_pythia_delphes.sh'
SA_SETUP = 'source /release_setup.sh'
_NAME = re.compile(r'[A-Za-z][A-Za-z0-9_]*')

# Conversion contract (milestone 2). It describes what the slim ntuple means;
# none of it is an ATLAS working point or efficiency.
CONTRACT = {
    'name': 'delphes-slim-v1',
    'tree': 'ntuple',
    'reader': 'SimpleAnalysis SlimReader (framework d9f8c704, AnalysisBase 21.2.242)',
    'units': {'momentum': 'GeV', 'angle': 'radian'},
    'weights': 'mcWeights = [Event.Weight]: nominal signed weight, no cross-section or luminosity scaling; SimpleAnalysis -w 0',
    'object_profile': {
        'name': 'all-pass-v1',
        'electron_muon_photon': 'every ID and isolation bit (0x7FFFFFFF)',
        'jet': 'GoodJet bits 8-10,12,14-18 (0x0007D700); every b-tag WP (0x00F000FF) when Jet.BTag',
        'tau': 'Jet.TauTag jets; every tau WP (0xFF) plus one/three-prong bit from NCharged',
        'mass': 'e/mu/tau fixed by the reader, photon 0, jet/fatjet from Delphes Mass',
    },
    'met': {'muon-corrected': 'MissingET minus every reconstructed muon (calorimeter-only MET cards, e.g. the packaged ATLAS card)',
            'delphes': 'Delphes MissingET unchanged (cards whose MET already includes muons, e.g. eflow-based)'},
    'sumet': 'ScalarHT.HT when written, else scalar pT sum of output Jet/Electron/Photon/Muon (identical for the packaged ATLAS card); an approximation of calorimeter SumET',
    'declared_empty': ['*_motherID = 0 (no truth ancestry)', 'genHT = 0', 'mcVetoCode = 0', 'susyChannel = 0',
                       'fatjet_* empty unless a FatJet branch exists',
                       'PDF scalars and mcChannel copied from Delphes Event (may be zero)'],
    'unsupported': ['truth-level/ancestry selections', 'track jets', 'B hadrons', 'displaced tracks',
                    'fat jets without a FatJet branch', 'ATLAS identification/isolation/tagging efficiencies'],
}


def default_backend():
    return 'local-apptainer' if socket.gethostname().split('.')[0].lower() == 'jupiter' else 'local-docker'


def container_command(backend, image, setup, command, *, work, binds=(), cwd='/work'):
    """Build an argument list; inputs are bound read-only at fixed container paths."""
    shell = f'{setup} >/dev/null && cd {shlex.quote(cwd)} && {command}'
    if backend == 'local-apptainer':
        arguments = ['apptainer', 'exec', '--bind', f'{work}:/work']
        for source, target in binds:
            arguments += ['--bind', f'{source}:{target}:ro']
        return arguments + [image, '/bin/bash', '-c', shell]
    if backend == 'local-docker':
        arguments = ['docker', 'run', '--rm', '-u', f'{os.getuid()}:{os.getgid()}', '-e', 'HOME=/work',
                     '-v', f'{work}:/work']
        for source, target in binds:
            arguments += ['-v', f'{source}:{target}:ro']
        return arguments + [image, '/bin/bash', '-c', shell]
    raise ValueError('backend must be local-apptainer or local-docker')


def parse_catalog(text):
    """Return analysis names printed after 'Select analyses among:' by simpleAnalysis -l."""
    lines = text.splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == 'Select analyses among:')
    except StopIteration:
        raise ValueError('simpleAnalysis -l output has no analysis catalog') from None
    names = []
    for line in lines[start + 1:]:
        name = line.strip()
        if not name:
            if names:
                break
            continue
        if not _NAME.fullmatch(name):
            break
        names.append(name)
    if not names or len(names) != len(set(names)):
        raise ValueError('simpleAnalysis -l returned an empty or duplicated catalog')
    return names


def _run(arguments, *, timeout=None, log=None):
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise ValueError(f'Container runtime not found: {arguments[0]}') from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f'Container command timed out after {timeout} s') from exc
    if log is not None:
        log.write_text(result.stdout + ('\n--- stderr ---\n' + result.stderr if result.stderr else ''), encoding='utf-8')
    return result


def _pin(reference, backend):
    if backend not in BACKENDS:
        raise ValueError('backend must be local-apptainer or local-docker')
    return pin_image(reference, backend if backend == 'local-docker' else 'local-apptainer')


def list_simple_analyses(sa_image, *, backend=None):
    """Run simpleAnalysis -l in the selected image; no inputs or outputs."""
    if not sa_image:
        raise ValueError('Select the SimpleAnalysis image explicitly (--sa-image)')
    backend = backend or default_backend()
    reference, identity = _pin(sa_image, backend)
    return {'image': identity, 'reference': reference, 'backend': backend,
            'analyses': _catalog(reference, backend)}


def _catalog(reference, backend):
    with tempfile.TemporaryDirectory(prefix='.mgpd-sa-list-') as work:
        result = _run(container_command(backend, reference, SA_SETUP, 'simpleAnalysis -l', work=work), timeout=600)
    if result.returncode != 0:
        raise ValueError(f'simpleAnalysis -l failed (exit {result.returncode}): {_tail(result)}')
    return parse_catalog(result.stdout)


def _analysis_names(analyses):
    if analyses is None:
        return []
    if isinstance(analyses, str):
        analyses = [item for item in analyses.split(',')]
    names = [item.strip() for item in analyses]
    if any(not _NAME.fullmatch(name) for name in names):
        raise ValueError('Analysis names must be identifiers, separated by commas')
    if len(names) != len(set(names)):
        raise ValueError('Duplicate analysis name')
    return names


def _packaged_atlas_card_sha256():
    return hashlib.sha256(_resource('cards/delphes_card_ATLAS.dat').read_bytes()).hexdigest()


def resolve_met(met, provenance):
    """Choose the MET convention; an unrecognized frozen card requires an explicit choice."""
    if met not in (None, 'muon-corrected', 'delphes'):
        raise ValueError('met: expected muon-corrected or delphes')
    cards = set((provenance or {}).get('delphes_cards', {}).values())
    if met is None and cards and cards != {_packaged_atlas_card_sha256()}:
        raise ValueError('The production Delphes card is not the packaged ATLAS card; choose the MET convention '
                         'explicitly: --met muon-corrected (calorimeter-only MissingET) or --met delphes '
                         '(MissingET already includes muons, e.g. eflow-based cards)')
    return met or 'muon-corrected'


def _card_provenance(dataset):
    """Return frozen Delphes card hashes from a production manifest, when one was selected."""
    for entry in (dataset or {}).get('metadata', []):
        if entry['path'].endswith('/manifest.json'):
            manifest = json.loads(Path(entry['path']).read_text(encoding='utf-8'))
            cards = {name: digest for name, digest in manifest.get('prepared_input_sha256', {}).items()
                     if name.endswith('delphes_card.dat')}
            return {'source': entry['path'], 'delphes_cards': cards}
    return None


def plan_simple_analysis(inputs, analyses=None, *, slim_input=False, sample=None, max_events=None,
                         met=None, backend=None, image=None, sa_image=None):
    """Validate inputs/images/catalog and return a checksummed plan, with no writes.

    ``analyses`` empty means conversion only. ``slim_input`` skips conversion
    for existing slim ntuples. Inputs are explicit ROOT files of one sample or
    one managed merge/production directory (its merged Delphes ROOT output).
    """
    names = _analysis_names(analyses)
    if slim_input and not names:
        raise ValueError('Slim inputs need at least one analysis to run')
    if max_events is not None and (type(max_events) is not int or max_events < 1):
        raise ValueError('max_events: expected a positive integer')
    if slim_input and met is not None:
        raise ValueError('The MET convention applies to conversion, not slim inputs')
    if sample is not None and (not isinstance(sample, str) or not sample.strip()):
        raise ValueError('sample: expected a nonempty label')
    backend = backend or default_backend()
    if backend not in BACKENDS:
        raise ValueError('backend must be local-apptainer or local-docker')

    paths, sample, dataset = resolve_dataset(inputs, format='root', sample=sample)
    if dataset and slim_input:
        raise ValueError('Managed merges contain Delphes ROOT; use explicit slim ntuple files with --slim')
    if not paths or len(paths) != len(set(paths)):
        raise ValueError('Provide at least one input, without duplicates (including symlinks)')
    files = []
    for path in paths:
        if not path.is_file() or not path.name.lower().endswith('.root'):
            raise ValueError(f'{path}: expected a ROOT file')
        before = path.stat()
        digest = file_sha256(path)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise ValueError(f'Input changed during planning: {path}')
        files.append({'path': str(path), 'bytes': after.st_size, 'sha256': digest})
    if dataset and files[0]['sha256'] != dataset['selected_output']['sha256']:
        raise ValueError('Managed input changed during planning')

    conversion = None
    if not slim_input:
        reference = image or (APPTAINER_IMAGE if backend == 'local-apptainer' else DOCKER_IMAGE)
        pinned, identity = _pin(reference, backend)
        script = _resource(CONVERTER).read_bytes()
        provenance = _card_provenance(dataset)
        conversion = {'image': identity, 'reference': pinned, 'contract': CONTRACT,
                      'met': resolve_met(met, provenance), 'met_requested': met,
                      'converter_sha256': hashlib.sha256(script).hexdigest(),
                      'card_provenance': provenance}

    execution = None
    if names:
        if not sa_image:
            raise ValueError('Select the SimpleAnalysis image explicitly (--sa-image)')
        catalog = list_simple_analyses(sa_image, backend=backend)
        unknown = sorted(set(names) - set(catalog['analyses']))
        if unknown:
            raise ValueError(f'Analyses not in the image catalog: {unknown}; list them with --sa-list')
        execution = {'image': catalog['image'], 'reference': catalog['reference'], 'analyses': names,
                     'catalog_size': len(catalog['analyses']), 'mc_weight_index': 0, 'smearing': 'none (no -L)',
                     'arguments': ['simpleAnalysis', '-a', ','.join(names), '-w', '0']}
        if slim_input and max_events is not None:
            execution['arguments'] += ['--nevents', str(max_events)]

    warnings = []
    if conversion and conversion['met_requested'] is None and not conversion['card_provenance']:
        warnings.append('MET muon correction assumes a calorimeter-only MissingET card; the card was not available to check '
                        '(use --met delphes if MissingET already includes muons).')
    if max_events is not None:
        warnings.append('Preview: event limit applied; results describe only the processed subset.')
    plan = {'schema_version': 1, 'kind': 'simple-analysis', 'mgpd_version': __version__, 'state': 'planned',
            'backend': backend, 'sample': sample, 'dataset': dataset, 'inputs': files,
            'input_kind': 'slim' if slim_input else 'delphes', 'max_events': max_events,
            'conversion': conversion, 'execution': execution,
            'normalization': 'raw (nominal event weights; no cross-section or luminosity scaling)',
            'warnings': warnings}
    plan['plan_sha256'] = _checksum(plan)
    return plan


def parse_results(text):
    """Parse SimpleAnalysis 'SR,events,acceptance,err' output (6 significant digits)."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or lines[0].split(',')[:4] != ['SR', 'events', 'acceptance', 'err']:
        raise ValueError('Unexpected SimpleAnalysis result header')
    total, regions = None, []
    for line in lines[1:]:
        fields = line.split(',')
        if len(fields) < 4:
            raise ValueError(f'Malformed SimpleAnalysis result row: {line}')
        name, events = fields[0], int(fields[1])
        first, second = float(fields[2]), float(fields[3])
        if not all(math.isfinite(value) for value in (first, second)):
            raise ValueError(f'Nonfinite SimpleAnalysis result: {line}')
        if total is None:
            # The first row is the 'All' counter: raw events, sumw, sumw2.
            total = {'region': name, 'events': events, 'sumw': first, 'sumw2': second}
        else:
            regions.append({'region': name, 'events': events, 'acceptance': first, 'acceptance_error': second})
    if total is None:
        raise ValueError('SimpleAnalysis result has no rows')
    return {'all': total, 'regions': regions}


def _verify(plan):
    if not isinstance(plan, dict) or plan.get('kind') != 'simple-analysis' or plan.get('plan_sha256') != _checksum(plan):
        raise ValueError('SimpleAnalysis plan is missing or changed; plan again')
    for entry in plan['inputs'] + (plan.get('dataset') or {}).get('metadata', []):
        path = Path(entry['path'])
        if str(path.resolve()) != entry['path'] or not path.is_file() or file_sha256(path) != entry['sha256']:
            raise ValueError(f'Input missing or changed: {path}; plan again')


def _destination(output, plan):
    if output is None:
        raise ValueError('Execution requires a new output directory')
    raw = Path(output).expanduser()
    if raw.exists() or raw.is_symlink():
        raise FileExistsError('Output already exists; choose a new directory')
    destination = raw.resolve()
    if not destination.parent.is_dir():
        raise FileNotFoundError('Output parent directory must exist')
    if plan.get('dataset') and destination.is_relative_to(Path(plan['dataset']['directory'])):
        raise ValueError('Output must be outside the managed dataset directory')
    if any(Path(item['path']).is_relative_to(destination) for item in plan['inputs']):
        raise ValueError('Output overlaps an input')
    return destination


def run_simple_analysis(plan, output, *, timeout=None):
    """Convert (unless slim input), run the selected analyses and publish one new directory."""
    _verify(plan)
    destination = _destination(output, plan)
    backend = plan['backend']
    for stage in (plan['conversion'], plan['execution']):
        if stage:
            verify_image(stage['reference'], stage['image'])
    binds = [(item['path'], f'/input/{index:03d}.root') for index, item in enumerate(plan['inputs'])]
    started = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory(prefix='.mgpd-sa-', dir=destination.parent) as temporary:
        work = Path(temporary)
        result = work / 'result'
        (result / 'logs').mkdir(parents=True)
        report = {'schema_version': 1, 'state': 'completed', 'kind': 'simple-analysis', 'mgpd_version': __version__,
                  'started_at': started.isoformat(), 'output': str(destination),
                  'plan_sha256': plan['plan_sha256'], 'plan': plan, 'warnings': list(plan['warnings'])}

        if plan['conversion']:
            conversion = plan['conversion']
            script = work / 'delphes2slim.py'
            script.write_bytes(_resource(CONVERTER).read_bytes())
            if file_sha256(script) != conversion['converter_sha256']:
                raise ValueError('Packaged converter differs from the plan; plan again')
            (result / 'slim').mkdir()
            command = ['python3', '/work/delphes2slim.py', '-i', *[target for _, target in binds],
                       '-o', '/work/result/slim/ntuple.root', '--summary', '/work/result/slim/conversion.json']
            if plan['max_events'] is not None:
                command += ['--max-events', str(plan['max_events'])]
            if conversion['met'] == 'delphes':
                command.append('--no-muon-met-correction')
            setup = (MG_SETUP + ' && export ROOT_INCLUDE_PATH="$DELPHES_PATH:$DELPHES_PATH/external"'
                     '${ROOT_INCLUDE_PATH:+:$ROOT_INCLUDE_PATH}')
            completed = _run(container_command(backend, conversion['reference'], setup, shlex.join(command),
                                               work=work, binds=binds),
                             timeout=timeout, log=result / 'logs' / 'conversion.log')
            ntuple = result / 'slim' / 'ntuple.root'
            summary_path = result / 'slim' / 'conversion.json'
            if completed.returncode != 0 or not ntuple.is_file() or not summary_path.is_file():
                raise ValueError(f'Conversion failed (exit {completed.returncode}); nothing was published:\n'
                                 + _tail(completed))
            summary = json.loads(summary_path.read_text(encoding='utf-8'))
            dataset = plan.get('dataset')
            if (dataset and not summary['limit_reached'] and dataset['expected_events'] is not None
                    and summary['entries'] != dataset['expected_events']):
                raise ValueError(f"Converted {summary['entries']} events, but the managed merge report records "
                                 f"{dataset['expected_events']}; nothing was published")
            report['conversion'] = summary
            if not summary['branches']['ScalarHT']:
                report['warnings'].append('No ScalarHT branch: sumet is the scalar pT sum of output Jet/Electron/Photon/Muon.')
            slim_target, slim_binds = '/work/result/slim/ntuple.root', []
            expected_events = summary['entries']
        else:
            slim_binds = binds
            slim_target = ','.join(target for _, target in binds)
            expected_events = None

        if plan['execution']:
            execution = plan['execution']
            (result / 'simple-analysis').mkdir()
            arguments = list(execution['arguments'])
            arguments += [slim_target] if plan['conversion'] else ['--input-files', slim_target]
            completed = _run(container_command(backend, execution['reference'], SA_SETUP, shlex.join(arguments),
                                               work=work, binds=slim_binds, cwd='/work/result/simple-analysis'),
                             timeout=timeout, log=result / 'logs' / 'simple-analysis.log')
            # ROOT prints harmless TList::Clear errors at teardown; judge by exit code and outputs.
            missing = [name for name in execution['analyses'] for suffix in ('.txt', '.root')
                       if not (result / 'simple-analysis' / (name + suffix)).is_file()]
            if completed.returncode != 0 or missing:
                raise ValueError(f'SimpleAnalysis failed (exit {completed.returncode}, missing {missing}); '
                                 'nothing was published:\n' + _tail(completed))
            analyses = {}
            for name in execution['analyses']:
                parsed = parse_results((result / 'simple-analysis' / (name + '.txt')).read_text(encoding='utf-8'))
                analyses[name] = parsed
                if expected_events is not None and parsed['all']['events'] != expected_events:
                    report['warnings'].append(f'{name}: All-row events {parsed["all"]["events"]} differ from the '
                                              f'{expected_events} converted entries')
            if plan['conversion']:
                sumw = report['conversion']['sumw']
                report['input_sumw_matches'] = {name: math.isclose(value['all']['sumw'], sumw, rel_tol=1e-5, abs_tol=1e-9)
                                                for name, value in analyses.items()}
                if not all(report['input_sumw_matches'].values()):
                    report['warnings'].append('Some analyses report an All-row sumw different from the input sumw '
                                              '(analyses may rescale event weights, e.g. ExampleAnalysis).')
            (result / 'results.json').write_text(json.dumps(analyses, indent=2, allow_nan=False) + '\n', encoding='utf-8')
            report['results'] = analyses

        _verify(plan)
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        files = sorted(path for path in result.rglob('*') if path.is_file())
        report['artifacts'] = [{'path': str(path.relative_to(result)), 'bytes': path.stat().st_size,
                                'sha256': file_sha256(path)} for path in files]
        (result / 'simple-analysis-report.json').write_text(json.dumps(report, indent=2, allow_nan=False) + '\n',
                                                            encoding='utf-8')
        destination.mkdir()
        try:
            os.replace(result, destination)
        except BaseException:
            try:
                destination.rmdir()
            except OSError:
                pass
            raise
    return report


def _tail(completed, lines=15):
    text = (completed.stderr or completed.stdout or '').strip().splitlines()
    text = [line for line in text if 'TList::Clear' not in line]
    return '\n'.join(text[-lines:]) or 'no output'


__all__ = ['CONTRACT', 'resolve_met', 'plan_simple_analysis', 'run_simple_analysis', 'list_simple_analyses', 'parse_catalog',
           'parse_results', 'container_command']
