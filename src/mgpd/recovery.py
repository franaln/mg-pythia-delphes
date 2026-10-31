"""Selective parent/child recovery preserving frozen inputs and ownership."""
import shutil
from pathlib import Path
import copy
from datetime import datetime, timezone
import uuid
from .storage import execution_lock, file_sha256, plan_sha256, production_path, write_manifest
from .preparation import prepare_condor_submission
from .execution import load_prepared_production
from .condor import production_status
from .results import validate_production


def plan_recovery(output_dir, ignore_recovery_id=None, **policy):
    """Read-only recovery selection; unknown/active jobs are never retry candidates."""

    production = load_prepared_production(output_dir)
    manifest = production.manifest

    validation = validate_production(production.output_dir, **policy)

    condor = manifest['mode'] in ('condor', 'jupiter')

    status = production_status(production.output_dir) if condor and manifest['state'] == 'submitted' else None

    scheduler = {job['job_id']: job for job in status['jobs']} if status else {}

    reserved = {}
    for attempt in manifest.get('recoveries', []):
        if attempt['production_id'] == ignore_recovery_id or not attempt.get('blocks_jobs', True):
            continue
        for job_id in attempt['job_ids']:
            reserved[job_id] = attempt['directory']

    result = {
        'production_id': manifest['production_id'],
        'directory': production.output_dir,
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'policy': validation['policy'],
        'jobs': [],
        'eligible_job_ids': [],
        'query_errors': status['query_errors'] if status else []
    }

    for job in validation['jobs']:
        observed = scheduler.get(job['job_id'], {}).get('state', 'unknown')
        row = {'job_id': job['job_id'], 'sample': job['sample'], 'outputs_state': job['outputs_state'],
               'validation_state': job['state'], 'execution_state': job['execution_state'],
               'scheduler_state': observed if condor else None, 'state': 'blocked'}
        if job['state'] == 'valid':
            row.update(state='preserved', reason='Existing results pass validation')
        elif job['job_id'] in reserved:
            row['reason'] = f"Already delegated to {reserved[job['job_id']]}"
        elif validation['metadata_errors']:
            row['reason'] = 'Output metadata is unreliable'
        elif any(value['state'] == 'unavailable' for value in job['outputs'].values()):
            row['reason'] = 'A required validation dependency is unavailable; do not regenerate data'
        elif any('Unsupported output selection' in value.get('error', '') for value in job['outputs'].values()):
            row['reason'] = 'Output selection is unsupported by validation; regeneration cannot fix it'
        elif condor:
            if manifest['state'] != 'submitted':
                row['reason'] = 'Original submission was not confirmed; reconcile it before retrying'
            elif result['query_errors']:
                row['reason'] = 'Scheduler query failed; active jobs cannot be excluded'
            elif not status.get('identity_checks_enabled'):
                row['reason'] = 'Legacy scheduler IDs lack verified production identity tags'
            elif observed not in ('completed', 'failed', 'removed'):
                row['reason'] = f'Scheduler state is {observed}; terminal evidence is required'
            elif scheduler[job['job_id']].get('source') != 'history':
                row['reason'] = 'Terminal job is still in the queue; wait for history finalization'
            else:
                row.update(state='eligible', reason='Terminal job with missing or invalid results')
        elif manifest['state'] not in ('failed', 'execution_finished'):
            row['reason'] = 'Local execution is not recorded as stopped'
        else:
            row.update(state='eligible', reason='Stopped local production with missing or invalid results')
        if row['state'] == 'eligible':
            result['eligible_job_ids'].append(job['job_id'])
        result['jobs'].append(row)

    return result


def prepare_recovery(output_dir, output, job_ids=None, **policy):
    """Reserve selected jobs and clone their immutable inputs to a new child plan."""
    unknown_policy = set(policy) - {'event_count', 'require_positive_cross_section', 'check_seed'}
    if unknown_policy:
        raise ValueError(f'Unsupported recovery validation policy: {sorted(unknown_policy)}')
    parent_dir = str(Path(output_dir).expanduser().resolve())
    child_dir = Path(output).expanduser().resolve()
    if child_dir == Path(parent_dir) or child_dir.is_relative_to(parent_dir):
        raise ValueError('Recovery output must be outside the original production directory')
    with execution_lock(parent_dir):
        parent = load_prepared_production(parent_dir)
        plan = plan_recovery(parent_dir, **policy)
        selected = list(job_ids) if job_ids is not None else plan['eligible_job_ids']
        if not selected or len(set(selected)) != len(selected):
            raise ValueError('Select at least one distinct eligible job; inspect mgpd recover --plan')
        if set(selected) - set(plan['eligible_job_ids']):
            raise ValueError('Selection includes ineligible jobs; inspect mgpd recover --plan')
        child_dir.mkdir()  # Never overwrite another production or recovery.
        identity = uuid.uuid4().hex
        jobs = [copy.deepcopy(job) for job in parent.manifest['jobs'] if job['job_id'] in selected]
        reservation = {'production_id': identity, 'directory': str(child_dir),
                       'job_ids': [job['job_id'] for job in jobs], 'state': 'preparing',
                       'blocks_jobs': True, 'created_at': datetime.now(timezone.utc).isoformat(),
                       'selection': [row for row in plan['jobs'] if row['job_id'] in selected]}
        parent.manifest.setdefault('recoveries', []).append(reservation)
        write_manifest(parent_dir, parent.manifest)
        finalized = False
        try:
            def copy_input(relative):
                if relative not in parent.manifest['execution_input_sha256']:
                    raise ValueError(f'Input has no saved checksum: {relative}')
                source = production_path(parent_dir, relative)
                target = production_path(child_dir, relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                if file_sha256(target) != parent.manifest['execution_input_sha256'][relative]:
                    raise ValueError(f'Input changed or was corrupted while copying: {relative}')
            mode = parent.manifest['mode']
            if mode in ('condor', 'jupiter'):
                for sample in {job['sample'] for job in jobs}:
                    copy_input(f'run_{sample}.tar.gz')
                copy_input('run_mg_pythia_delphes.sh')
                for job in jobs:
                    copy_input(job['run_file'])
            else:
                roots = [Path(job['run_file']).parent for job in jobs]
                for relative in parent.manifest['execution_input_sha256']:
                    if any(Path(relative).is_relative_to(root) for root in roots):
                        copy_input(relative)
            inputs = {str(path.relative_to(child_dir)): file_sha256(path)
                      for path in child_dir.rglob('*') if path.is_file()}
            child = {'schema_version': 2, 'production_id': identity,
                     'created_at': datetime.now(timezone.utc).isoformat(), 'state': 'prepared',
                     'config': copy.deepcopy(parent.manifest['config']), 'mode': mode,
                     'image': parent.manifest['image'], 'image_identity': parent.manifest['image_identity'],
                     'jobs': jobs, 'prepared_input_sha256': inputs,
                     'recovery': {'parent_directory': parent_dir, 'parent_id': parent.manifest['production_id'],
                                  'parent_plan_sha256': parent.manifest['plan_sha256'],
                                  'job_ids': reservation['job_ids'], 'policy': plan['policy'],
                                  'selection': reservation['selection']}}
            submit_file = None
            if mode in ('condor', 'jupiter'):
                submit_file = prepare_condor_submission(child['config'], str(child_dir), child['image'], jobs, identity)
            child['execution'] = {'submit_file': submit_file}
            child['execution_input_sha256'] = {str(path.relative_to(child_dir)): file_sha256(path)
                                               for path in child_dir.rglob('*') if path.is_file()}
            child['plan_sha256'] = plan_sha256(child)
            write_manifest(child_dir, child)
            finalized = True
            reservation['state'] = 'prepared'
            write_manifest(parent_dir, parent.manifest)
            return load_prepared_production(child_dir)
        except BaseException as exc:
            # A finalized child may already be executable: never release its jobs.
            finalized = finalized or (child_dir / 'manifest.json').exists()
            reservation.update(state='preparation_unknown' if finalized else 'preparation_failed',
                               blocks_jobs=finalized, error=f'{type(exc).__name__}: {exc}')
            write_manifest(parent_dir, parent.manifest)
            raise


def recheck_recovery_parent(production):
    """A registered child must still own eligible jobs immediately before launch."""
    recovery = production.manifest['recovery']
    parent_dir = recovery['parent_directory']
    with execution_lock(parent_dir):
        parent = load_prepared_production(parent_dir)
        if (parent.manifest['production_id'] != recovery['parent_id'] or
                parent.manifest['plan_sha256'] != recovery['parent_plan_sha256']):
            raise ValueError('Recovery parent identity/inputs changed')
        owned = [entry for entry in parent.manifest.get('recoveries', [])
                 if entry['production_id'] == production.manifest['production_id'] and entry.get('blocks_jobs')]
        if len(owned) != 1 or Path(owned[0]['directory']).resolve() != Path(production.output_dir).resolve():
            raise ValueError('Recovery ownership/path is not registered in its parent')
        if set(owned[0]['job_ids']) != set(recovery['job_ids']):
            raise ValueError('Recovery ownership does not match selected jobs')
        plan = plan_recovery(parent_dir, ignore_recovery_id=production.manifest['production_id'], **recovery['policy'])
        if set(recovery['job_ids']) - set(plan['eligible_job_ids']):
            raise ValueError('Recovery jobs are no longer eligible; inspect parent status/results')
