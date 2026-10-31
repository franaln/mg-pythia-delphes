"""Verified one-time execution of saved production plans."""
import sys
import subprocess
import shlex
from pathlib import Path
from datetime import datetime, timezone
import socket

from .models import PreparedProduction
from .storage import (
    execution_lock,
    file_sha256,
    plan_sha256,
    production_path,
    read_production_manifest,
    write_manifest,
)
from .condor import parse_condor_job_ids


def build_local_command(run_mode, image, sample, run_dir):
    """Return argv for the same container command used by the CLI."""

    inner = 'source /setup_mg_pythia_delphes.sh && cd /local && '
    inner += shlex.join(['./run_mg_pythia_delphes.sh', sample, f'run_{sample}'])

    if run_mode == 'local-docker':
        return ['docker', 'run', '--rm', '-v', f'{run_dir}:/local', image, inner]
    if run_mode == 'local-apptainer':
        return ['apptainer', 'exec', '--bind', f'{run_dir}:/local', image, '/bin/bash', '-l', '-c', inner]

    raise ValueError(f'Not a local execution mode: {run_mode}')


def load_prepared_production(output_dir):
    """Load and verify a saved plan without its original YAML/cards or cwd."""

    output_dir = str(Path(output_dir).expanduser().resolve())
    manifest = read_production_manifest(output_dir)
    if manifest.get('schema_version') != 2:
        raise ValueError('This manifest cannot be executed safely; prepare a new production with the current runner')
    if manifest.get('plan_sha256') != plan_sha256(manifest):
        raise ValueError('Production plan metadata changed; prepare a new production')
    hashes = manifest['execution_input_sha256']
    if not isinstance(hashes, dict) or not hashes:
        raise ValueError('Production has no execution-input checksums')
    for relative, digest in hashes.items():
        path = production_path(output_dir, relative)
        if not path.is_file() or file_sha256(path) != digest:
            raise ValueError(f'Prepared input missing or changed: {relative}')
    mode = manifest['mode']
    local_dirs = {}
    submit_file = manifest['execution']['submit_file']
    if mode in ('condor', 'jupiter'):
        production_path(output_dir, submit_file)
        if submit_file not in hashes or 'run_mg_pythia_delphes.sh' not in hashes:
            raise ValueError('Missing submit-file/wrapper integrity record')
    elif mode in ('local-docker', 'local-apptainer'):
        for job in manifest['jobs']:
            run_file = production_path(output_dir, job['run_file'])
            wrapper = str(Path(job['run_file']).parent / 'run_mg_pythia_delphes.sh')
            if job['run_file'] not in hashes or wrapper not in hashes:
                raise ValueError('Missing job-input/wrapper integrity record')
            local_dirs[job['job_id']] = (job['sample'], str(run_file.parent))
    else:
        raise ValueError(f'Unsupported saved execution mode: {mode}')
    return PreparedProduction(output_dir, manifest, local_dirs, submit_file)


def execute_production(production):
    """Verify and execute a saved production once, with a durable attempt marker.

    Failed/ambiguous attempts are not retried automatically. Scheduler tracking
    and selective recovery remain separate development stages.
    """
    with execution_lock(production.output_dir):

        current = load_prepared_production(production.output_dir)

        manifest = current.manifest
        if production.manifest.get('plan_sha256') != manifest['plan_sha256']:
            raise ValueError('Production was replaced after it was loaded')
        if manifest.get('execution_attempt') or manifest.get('state') != 'prepared':
            raise ValueError('Production has already been attempted; inspect its state before creating a new production')
        if manifest.get('recovery'):
            # Recovery depends on the plan loader; import only when needed.
            from .recovery import recheck_recovery_parent
            recheck_recovery_parent(current)

        mode = manifest['mode']
        manifest['execution_attempt'] = {'started_at': datetime.now(timezone.utc).isoformat()}
        manifest['state'] = 'submitting' if mode in ('condor', 'jupiter') else 'running'
        write_manifest(current.output_dir, manifest)

        try:
            if mode in ('condor', 'jupiter'):
                submitted = subprocess.run(['condor_submit', '-terse', '-maxjobs', str(len(manifest['jobs'])),
                                            current.submit_file], cwd=current.output_dir,
                                           capture_output=True, text=True, check=True)
                manifest['submission'] = {'stdout': submitted.stdout, 'stderr': submitted.stderr,
                                          'host': socket.gethostname()}
                # Save raw output before parsing: jobs may exist even if parsing fails.
                write_manifest(current.output_dir, manifest)
                ids = parse_condor_job_ids(submitted.stdout, expected_count=len(manifest['jobs']))
                tagged = '+MGProductionId' in Path(current.output_dir, current.submit_file).read_text()
                manifest['submission']['identity_tagged'] = tagged
                manifest['submission']['jobs'] = [
                    {'job_id': job['job_id'], 'sample': job['sample'], 'scheduler_id': scheduler_id}
                    for job, scheduler_id in zip(manifest['jobs'], ids)
                ]
                manifest['state'] = 'submitted'
                if submitted.stdout:
                    print(submitted.stdout, end='')
                if submitted.stderr:
                    print(submitted.stderr, end='', file=sys.stderr)
            else:
                for job_id, (sample, run_dir) in current.local_dirs.items():
                    manifest['execution_attempt']['current_job_id'] = job_id
                    write_manifest(current.output_dir, manifest)
                    command = build_local_command(mode, manifest['image'], sample, run_dir)
                    print(command)
                    subprocess.run(command, check=True)
                manifest['state'] = 'execution_finished'
        except BaseException as exc:
            manifest['state'] = 'submission_unknown' if mode in ('condor', 'jupiter') else 'failed'
            manifest['execution_attempt']['error'] = f'{type(exc).__name__}: {exc}'
            if isinstance(exc, subprocess.CalledProcessError) and mode in ('condor', 'jupiter'):
                manifest['submission'] = {'stdout': exc.stdout, 'stderr': exc.stderr}
            if mode not in ('condor', 'jupiter'):
                manifest['failed_job_id'] = manifest['execution_attempt'].get('current_job_id')
            write_manifest(current.output_dir, manifest)
            raise
        manifest['execution_attempt']['finished_at'] = datetime.now(timezone.utc).isoformat()

        write_manifest(current.output_dir, manifest)

        production.manifest.clear()
        production.manifest.update(manifest)

        return manifest
