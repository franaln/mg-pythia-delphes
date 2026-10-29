"""HTCondor submission-ID parsing and read-only queue/history queries."""
import subprocess
import re
import json
from datetime import datetime, timezone
from collections import Counter
from .storage import read_production_manifest


def parse_condor_job_ids(stdout, expected_count=None):
    """Parse terse job-ID ranges in submission order; never guess a mapping."""
    result = []
    for line in stdout.splitlines():
        match = re.fullmatch(r'\s*(\d+)\.(\d+)(?:\s*-\s*(\d+)\.(\d+))?\s*', line)
        if not match:
            if line.strip():
                raise ValueError(f'Unrecognized condor_submit output: {line}')
            continue
        cluster, first = int(match[1]), int(match[2])
        last = int(match[4]) if match[4] is not None else first
        if (match[3] is not None and int(match[3]) != cluster) or last < first:
            raise ValueError('Invalid scheduler job-ID range')
        if expected_count is not None and len(result) + last - first + 1 > expected_count:
            raise ValueError('Submitted job count exceeds the production plan')
        result.extend(f'{cluster}.{process}' for process in range(first, last + 1))
    if not result or len(set(result)) != len(result):
        raise ValueError('Missing or duplicate submitted job IDs')
    if expected_count is not None and len(result) != expected_count:
        raise ValueError(f'Expected {expected_count} submitted jobs, found {len(result)}')
    return result


def query_condor_ads(command, ids, production_id=None, timeout=30):
    """Query only recorded IDs in bounded batches, preserving errors as unknown."""
    ads, errors = {}, []
    for start in range(0, len(ids), 100):
        batch = ids[start:start + 100]
        terms = []
        for scheduler_id in batch:
            if not re.fullmatch(r'\d+\.\d+', scheduler_id):
                raise ValueError(f'Invalid recorded scheduler ID: {scheduler_id}')
            cluster, process = scheduler_id.split('.')
            terms.append(f'(ClusterId == {int(cluster)} && ProcId == {int(process)})')
        constraint = '(' + ' || '.join(terms) + ')'
        if production_id:
            if not re.fullmatch(r'[a-f0-9]{32}', production_id):
                raise ValueError('Invalid production identity')
            constraint += f' && MGProductionId == "{production_id}"'
        argv = [command, '-json', '-attributes', CONDOR_STATUS_ATTRIBUTES, '-constraint', constraint]
        if command == 'condor_history':
            argv += ['-limit', str(len(batch))]
        try:
            response = subprocess.run(argv, capture_output=True, text=True, check=True, timeout=timeout)
            # Some clients return no text rather than [] for zero matching ads.
            # Accept only a successful, silent response; diagnostics and malformed
            # nonempty JSON remain errors. Absence never establishes completion.
            if not response.stdout.strip():
                if response.stderr.strip():
                    raise ValueError(f'Empty scheduler response with diagnostics: {response.stderr.strip()}')
                records = []
            else:
                try:
                    records = json.loads(response.stdout)
                except json.JSONDecodeError as exc:
                    raise ValueError(f'Invalid scheduler JSON: {exc}; '
                                     f'stdout={response.stdout[:1000]!r}; stderr={response.stderr[:1000]!r}') from exc
            if not isinstance(records, list) or any(not isinstance(ad, dict) for ad in records):
                raise ValueError('Expected a JSON list of ClassAds')
            selected = {}
            for ad in records:
                if type(ad.get('ClusterId')) is not int or type(ad.get('ProcId')) is not int:
                    raise ValueError('ClassAd is missing numeric ClusterId/ProcId')
                scheduler_id = f"{ad['ClusterId']}.{ad['ProcId']}"
                if scheduler_id not in batch:
                    continue
                if production_id and ad.get('MGProductionId') != production_id:
                    continue
                # History is newest-first. Keep the first matching ad.
                selected.setdefault(scheduler_id, ad)
            ads.update(selected)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            detail = getattr(exc, 'stderr', None) or str(exc)
            errors.append({'command': command, 'scheduler_ids': batch, 'error': detail})
    return ads, errors


def condor_ad_status(ad):
    """Completion requires exit information; missing ads/details remain unknown."""
    status = ad.get('JobStatus')
    if type(status) is not int:
        return 'unknown'
    states = {1: 'pending', 2: 'running', 3: 'removed', 5: 'held',
              6: 'transferring', 7: 'suspended'}
    if status == 4:
        if ad.get('ExitBySignal') is True:
            return 'failed'
        if ad.get('ExitBySignal') is False and type(ad.get('ExitCode')) is int:
            return 'completed' if ad['ExitCode'] == 0 else 'failed'
        return 'unknown'
    return states.get(status, 'unknown')


def production_status(output_dir, timeout=30):
    """Read-only scheduler snapshot; never submit, reset attempts or validate physics."""
    manifest = read_production_manifest(output_dir)
    result = {'production_id': manifest.get('production_id'), 'recorded_state': manifest.get('state'),
              'checked_at': datetime.now(timezone.utc).isoformat(), 'jobs': [], 'query_errors': []}
    if manifest.get('mode') not in ('condor', 'jupiter'):
        result.update(state=manifest.get('state', 'unknown'), counts={},
                      note='Recorded local state only; no live process or output validation')
        return result
    submission = manifest.get('submission', {})
    mappings = submission.get('jobs', [])
    if not mappings and submission.get('stdout') and manifest.get('state') == 'submitted':
        # Read-only compatibility with schema-2 preparations from before ID tracking.
        try:
            ids = parse_condor_job_ids(submission['stdout'], len(manifest['jobs']))
            mappings = [{'job_id': job['job_id'], 'sample': job['sample'], 'scheduler_id': value}
                        for job, value in zip(manifest['jobs'], ids)]
        except ValueError as exc:
            result['query_errors'].append({'command': 'parse_submission', 'error': str(exc)})
    expected = {job['job_id']: job for job in manifest.get('jobs', [])}
    mapped = {entry['job_id']: entry for entry in mappings}
    if len(mapped) != len(mappings) or any(key not in expected for key in mapped):
        raise ValueError('Recorded scheduler mapping does not match the production')
    ids = [entry['scheduler_id'] for entry in mappings]
    if len(set(ids)) != len(ids):
        raise ValueError('Duplicate recorded scheduler IDs')
    tagged_id = manifest.get('production_id') if submission.get('identity_tagged') else None
    if mappings and not tagged_id:
        result['note'] = 'Legacy submission: scheduler IDs have no production identity tags'
    queued, errors = query_condor_ads('condor_q', ids, tagged_id, timeout)
    result['query_errors'].extend(errors)
    missing = [value for value in ids if value not in queued]
    history, errors = query_condor_ads('condor_history', missing, tagged_id, timeout)
    result['query_errors'].extend(errors)
    for job_id, job in expected.items():
        mapping = mapped.get(job_id, {})
        scheduler_id = mapping.get('scheduler_id')
        ad = queued.get(scheduler_id) or history.get(scheduler_id)
        item = {'job_id': job_id, 'sample': job['sample'], 'scheduler_id': scheduler_id,
                'state': 'unknown', 'source': None}
        if ad:
            if tagged_id and ad.get('MGJobId') != job_id:
                item['reason'] = 'Scheduler job identity does not match recorded job'
            else:
                item.update(state=condor_ad_status(ad), source='queue' if scheduler_id in queued else 'history')
                for key in ('JobStatus', 'ExitCode', 'ExitBySignal', 'ExitSignal', 'HoldReason', 'HoldReasonCode', 'RemoveReason'):
                    if key in ad:
                        item[key] = ad[key]
        else:
            item['reason'] = 'No recorded scheduler ID' if scheduler_id is None else 'Not found or query unavailable; never treated as completed'
            if scheduler_id is None and manifest.get('state') == 'prepared':
                item['state'] = 'prepared'
        result['jobs'].append(item)
    result['counts'] = dict(Counter(item['state'] for item in result['jobs']))
    states = set(result['counts'])
    result['state'] = next((state for state in ('failed', 'removed', 'held', 'unknown', 'running',
                            'transferring', 'suspended', 'pending', 'prepared', 'completed') if state in states), 'unknown')
    result['submission_host'] = submission.get('host')
    result['identity_checks_enabled'] = bool(tagged_id)
    return result


CONDOR_STATUS_ATTRIBUTES = ('ClusterId,ProcId,JobStatus,ExitCode,ExitBySignal,ExitSignal,'
                            'HoldReason,HoldReasonCode,RemoveReason,MGProductionId,MGJobId')

