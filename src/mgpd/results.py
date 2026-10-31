"""Read-only artifact validation and per-sample summaries."""
import shutil
import re
from pathlib import Path
from datetime import datetime, timezone
from contextlib import contextmanager
from collections import Counter
import gzip
import zlib
import math
import tarfile
import tempfile
import xml.etree.ElementTree as ET
from .storage import production_path, read_production_manifest
from .condor import parse_condor_job_ids


def parse_lhe_init(block):
    """Read HEPRUP records, excluding optional nested XML metadata (LHEF 3)."""

    try:
        root = ET.fromstring(block)
    except ET.ParseError as exc:
        raise ValueError(f'Malformed LHE init block: {exc}') from exc
    # Only direct text belongs to HEPRUP. Generator/weight descriptions inside
    # child elements are metadata, even when their text looks numeric.
    text = '\n'.join([root.text or ''] + [child.tail or '' for child in root])
    rows = [line.split('#', 1)[0].strip() for line in text.splitlines()]
    rows = [line.split() for line in rows if line]
    if not rows or len(rows[0]) != 10:
        raise ValueError('Malformed LHE init beam record: expected 10 fields')
    try:
        nprocess = int(rows[0][9])
    except ValueError as exc:
        raise ValueError('Malformed LHE init NPRUP: expected an integer') from exc
    if nprocess < 1 or len(rows) != nprocess + 1:
        raise ValueError(f'Incomplete or inconsistent LHE cross-section block: '
                         f'NPRUP={nprocess}, found {len(rows) - 1} process records '
                         '(optional XML metadata excluded)')
    cross_sections = []
    for index, row in enumerate(rows[1:], 1):
        if len(row) != 4:
            raise ValueError(f'Malformed LHE process record {index}: expected 4 fields')
        try:
            values = [float(value.replace('D', 'E').replace('d', 'e')) for value in row[:3]]
            int(row[3])
        except ValueError as exc:
            raise ValueError(f'Malformed numeric LHE process record {index}') from exc
        if not all(math.isfinite(value) for value in values):
            raise ValueError(f'Nonfinite values in LHE process record {index}')
        cross_sections.append(values[0])
    return sum(cross_sections)


def check_lhe(stream, job, event_count='exact', require_positive_cross_section=True, check_seed=True):
    events, seed, init, in_init = 0, None, [], False
    init_closed = False
    event_ends = 0
    lhe_closed = False
    with gzip.GzipFile(fileobj=stream) as source:
        for raw in source:
            line = raw.decode('utf-8', errors='replace').strip()
            events += bool(re.fullmatch(r'<event(?:\s[^>]*)?>', line))
            event_ends += line == '</event>'
            lhe_closed = lhe_closed or line == '</LesHouchesEvents>'
            match = re.match(r'(\d+)\s*=\s*iseed\b', line)
            if match:
                seed = int(match[1])
            if re.match(r'<init(?:\s[^>]*)?>', line):
                if init:
                    raise ValueError('Duplicate LHE init block')
                in_init = True
            if in_init:
                init.append(line)
                if '</init>' in line:
                    in_init = False
                    init_closed = True
    if events != event_ends or not lhe_closed:
        raise ValueError('Incomplete LHE event/file closing tags')
    if event_count not in ('exact', 'nonempty', 'ignore'):
        raise ValueError(f'Unsupported event-count policy: {event_count}')
    if event_count == 'nonempty' and events < 1:
        raise ValueError('LHE contains no events')
    if event_count == 'exact' and events != job['nevents']:
        raise ValueError(f"Expected {job['nevents']} LHE events, found {events}")
    if check_seed and seed != job['seed']:
        raise ValueError(f"LHE seed {seed} differs from planned seed {job['seed']}")
    if not init:
        raise ValueError('LHE init block is missing')
    if not init_closed:
        raise ValueError('LHE init block is missing its closing tag')
    cross_section = parse_lhe_init('\n'.join(init))
    if not math.isfinite(cross_section) or (require_positive_cross_section and cross_section <= 0):
        raise ValueError('Nonpositive/nonfinite cross section')

    return {'lhe_events': events, 'seed': seed, 'cross_section_pb': cross_section}


def inspect_outputs(files, job, **policy):
    """files maps basenames to functions opening binary streams; never unpack tar paths."""

    checks = {}
    suffixes = {
        'lhe': '_unweighted_events.lhe.gz',
        'root': '_delphes_events.root',
        'hepmc': '_pythia8_events.hepmc.gz',
        'lhco': '_delphes_events.lhco',
        'log': '_logs.tar'
    }

    for kind in job['outputs']:
        if kind not in suffixes:
            raise ValueError(f'Output validation not implemented for {kind}')
        matches = [name for name in files if name.endswith(suffixes[kind])]
        if len(matches) != 1:
            raise ValueError(f'Expected one {kind} output, found {matches}')
        with files[matches[0]]() as source:
            if kind == 'lhe':
                checks.update(check_lhe(source, job, **policy))
            elif kind == 'root':
                import uproot
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / 'events.root'
                    with path.open('wb') as target:
                        shutil.copyfileobj(source, target)
                    with uproot.open(path) as root:
                        entries = root['Delphes'].num_entries
                        if entries <= 0:
                            raise ValueError('Delphes tree is empty')
                        checks['delphes_events'] = entries
            elif kind == 'hepmc':
                with gzip.GzipFile(fileobj=source) as hepmc:
                    size = 0
                    for block in iter(lambda: hepmc.read(1024 * 1024), b''):
                        size += len(block)
                    if not size:
                        raise ValueError('Empty HepMC output')
                    checks['hepmc_bytes'] = size
            elif kind == 'log':
                with tarfile.open(fileobj=source, mode='r:') as logs:
                    checks['log_files'] = len(logs.getmembers())
            elif not source.read(1):
                raise ValueError(f'Empty {kind} output')

    return checks


def check_condor_job(output, job, scheduler_id):
    manifest = {'mode': 'condor'}
    if result_execution_state(output, manifest, job, scheduler_id) != 'completed':
        raise ValueError(f'Job {scheduler_id} did not terminate normally with exit code zero')
    with result_files(output, manifest, job, scheduler_id) as (files, location, logs):
        return inspect_outputs(files, job)


@contextmanager
def result_files(output_dir, manifest, job, scheduler_id=None):
    """Open job artifacts without extracting untrusted tar member paths."""
    if manifest['mode'] in ('condor', 'jupiter'):
        if not scheduler_id or not re.fullmatch(r'\d+\.\d+', scheduler_id):
            raise LookupError('No reliable scheduler ID is recorded for this job')
        cluster, process = scheduler_id.split('.')
        archive_path = production_path(output_dir, f"output_{job['sample']}_{cluster}_{process}.tar.gz")
        with tarfile.open(archive_path) as archive:
            files = {}
            for member in archive.getmembers():
                if member.isfile():
                    name = Path(member.name).name
                    if name in files:
                        raise ValueError(f'Duplicate output basename: {name}')
                    files[name] = lambda member=member: archive.extractfile(member)
            yield files, str(archive_path), None
    else:
        run_dir = production_path(output_dir, job['run_file']).parent / 'RUN'
        events = run_dir / 'Events'
        events = events / ('run_01_decayed_1' if (events / 'run_01_decayed_1').is_dir() else 'run_01')
        files = {'local_' + path.name: lambda path=path: path.open('rb')
                 for path in events.iterdir() if path.is_file()}
        yield files, str(events), len(list(run_dir.rglob('*.log')))


def result_execution_state(output_dir, manifest, job, scheduler_id):
    """Recorded/local or job-log evidence only; no live scheduler calls."""
    if manifest['mode'] not in ('condor', 'jupiter'):
        if manifest.get('state') == 'execution_finished':
            return 'completed'
        if manifest.get('failed_job_id') == job['job_id']:
            return 'failed'
        return 'unknown'
    if not scheduler_id or not re.fullmatch(r'\d+\.\d+', scheduler_id):
        return 'unknown'
    cluster, process = scheduler_id.split('.')
    log = production_path(output_dir, f"job_{job['sample']}_{cluster}_{process}.log")
    if not log.is_file():
        return 'unknown'
    text = log.read_text(errors='replace')
    terminal = list(re.finditer(r'Normal termination \(return value (-?\d+)\)|Abnormal termination|Job was aborted', text))
    if not terminal:
        return 'unknown'
    return 'completed' if terminal[-1].group(1) == '0' else 'failed'


def validate_production(output_dir, event_count='exact', require_positive_cross_section=True, check_seed=True):
    """Read-only artifact validation and per-sample summary, not physics approval."""

    if event_count not in ('exact', 'nonempty', 'ignore'):
        raise ValueError(f'Unsupported event-count policy: {event_count}')

    output_dir = str(Path(output_dir).expanduser().resolve())

    manifest = read_production_manifest(output_dir)
    policy = {
        'event_count': event_count,
        'require_positive_cross_section': require_positive_cross_section,
        'check_seed': check_seed
    }

    report = {
        'production_id': manifest.get('production_id'),
        'recorded_state': manifest.get('state'),
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'policy': policy,
        'jobs': [],
        'samples': {},
        'metadata_errors': []
    }
    mappings = manifest.get('submission', {}).get('jobs', [])
    if not mappings and manifest.get('state') == 'submitted' and manifest.get('submission', {}).get('stdout'):
        try:
            ids = parse_condor_job_ids(manifest['submission']['stdout'], len(manifest['jobs']))
            mappings = [{'job_id': job['job_id'], 'scheduler_id': value}
                        for job, value in zip(manifest['jobs'], ids)]
        except ValueError as exc:
            report['metadata_errors'].append(str(exc))

    mapped = {entry['job_id']: entry['scheduler_id'] for entry in mappings}
    if len(mapped) != len(mappings) or len(set(mapped.values())) != len(mapped):
        raise ValueError('Invalid duplicate scheduler mapping')
    suffixes = {'lhe': '_unweighted_events.lhe.gz', 'root': '_delphes_events.root',
                'hepmc': '_pythia8_events.hepmc.gz', 'lhco': '_delphes_events.lhco', 'log': '_logs.tar'}

    for job in manifest['jobs']:
        scheduler_id = mapped.get(job['job_id'])
        item = {
            'job_id': job['job_id'],
            'sample': job['sample'],
            'scheduler_id': scheduler_id,
            'requested_events': job['nevents'],
            'metrics': {},
            'outputs': {}
        }
        item['execution_state'] = result_execution_state(output_dir, manifest, job, scheduler_id)
        try:
            with result_files(output_dir, manifest, job, scheduler_id) as (files, location, local_logs):
                item['location'] = location
                for kind in job['outputs']:
                    entry = {}
                    item['outputs'][kind] = entry
                    if kind == 'log' and local_logs is not None:
                        entry.update(state='valid', metrics={'log_files': local_logs})
                        item['metrics']['log_files'] = local_logs
                        continue
                    if kind not in suffixes:
                        entry.update(state='invalid', error=f'Unsupported output selection: {kind}; select explicit types for validation')
                        continue
                    matches = [name for name in files if name.endswith(suffixes[kind])]
                    if not matches:
                        entry.update(state='missing', error=f'Missing requested {kind} output')
                        continue
                    try:
                        metrics = inspect_outputs(files, dict(job, outputs=[kind]), **policy)
                        entry.update(state='valid', metrics=metrics)
                        item['metrics'].update(metrics)
                    except ImportError as exc:
                        entry.update(state='unavailable', error=f'Missing validation dependency: {exc}')
                    except (OSError, ValueError, KeyError, EOFError, tarfile.TarError, zlib.error) as exc:
                        entry.update(state='invalid', error=f'{type(exc).__name__}: {exc}')
                states = {entry['state'] for entry in item['outputs'].values()}
                item['outputs_state'] = next((state for state in ('invalid', 'missing', 'unavailable', 'valid') if state in states), 'invalid')
        except FileNotFoundError as exc:
            item.update(outputs_state='missing', error=str(exc))
        except LookupError as exc:
            item.update(outputs_state='unavailable', error=str(exc))
        except (OSError, ValueError, EOFError, tarfile.TarError, zlib.error) as exc:
            item.update(outputs_state='invalid', error=f'{type(exc).__name__}: {exc}')

        item['state'] = 'invalid' if item['execution_state'] == 'failed' else item['outputs_state']
        report['jobs'].append(item)
        sample = report['samples'].setdefault(job['sample'], {'planned_jobs': 0, 'requested_events': 0,
                                                              'validated_lhe_events': 0, 'validated_delphes_events': 0,
                                                              'cross_sections_pb': [], 'counts': {}})
        sample['planned_jobs'] += 1
        sample['requested_events'] += job['nevents']
        sample['counts'][item['state']] = sample['counts'].get(item['state'], 0) + 1
        if item['state'] == 'valid':
            sample['validated_lhe_events'] += item['metrics'].get('lhe_events', 0)
            sample['validated_delphes_events'] += item['metrics'].get('delphes_events', 0)
            if 'cross_section_pb' in item['metrics']:
                sample['cross_sections_pb'].append({'job_id': job['job_id'], 'value': item['metrics']['cross_section_pb']})
    report['counts'] = dict(Counter(job['state'] for job in report['jobs']))
    report['state'] = ('invalid' if report['metadata_errors'] or 'invalid' in report['counts'] else
                       'valid' if report['jobs'] and set(report['counts']) == {'valid'} else 'incomplete')

    return report
