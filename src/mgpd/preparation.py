"""Render and package frozen production inputs without launching work."""
import os
import string
import shutil
import shlex
import subprocess
import re
from pathlib import Path
import copy
import hashlib
import secrets
from datetime import datetime, timezone
import uuid
from .templates import create_file_from_template, create_file_from_script_template
from .models import MAX_SEED, PreparedProduction
from .storage import (
    execution_lock,
    file_sha256,
    plan_sha256,
    read_production_manifest,
    write_manifest,
)
from .config import get_config_options, get_expert_options, resolve_container_image, validate_config, resolve_run_mode


def mkdir(path):
    try:
        os.mkdir(path)
    except FileExistsError:
        pass


def allocate_seeds(seed, count):
    """A numeric seed starts a deterministic sequence; 0/RANDOM selects a start."""
    if count < 1 or count > MAX_SEED:
        raise ValueError(f'Production needs between 1 and {MAX_SEED} jobs')
    if seed is None or seed == 0 or (isinstance(seed, str) and seed.upper().strip() == 'RANDOM'):
        start = secrets.randbelow(MAX_SEED) + 1
    elif type(seed) is int and 1 <= seed <= MAX_SEED:
        start = seed
    else:
        raise ValueError(f'options.seed must be 0, RANDOM, or an integer from 1 to {MAX_SEED}')
    return [((start - 1 + index) % MAX_SEED) + 1 for index in range(count)]


def render_seeded_run(text, seed):
    """Replace seed overrides and insert one explicit seed before final done."""
    lines = [line for line in text.splitlines()
             if not re.match(r'^\s*set\s+iseed(?:\s|=)', line, re.IGNORECASE)]
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[-1].strip().lower() == 'done':
        lines.pop()
    return '\n'.join(lines + [f'set iseed = {seed}', 'done', ''])


def prepare_sample_inputs(config, output_dir):
    """Copy sample inputs and render unseeded MG commands; never run physics tools."""
    # Inputs
    run_name = config['run']['name']
    run_nevents = config['run'].get('nevents', 10_000)
    run_dirs = {}

    #  Custom input
    if 'input_files' in config or 'input_dir' in config or 'input_dirs' in config:

        if 'input_dirs' in config:

            for name, input_dir in config['input_dirs'].items():

                model_name = f'{run_name}_{name}'
                run_dir_model = f'{output_dir}/run_{model_name}'
                run_dirs[model_name] = run_dir_model

                shutil.copytree(input_dir, run_dir_model)

        elif 'input_dir' in config:

            run_dir = f'{output_dir}/run_{run_name}'
            run_dirs[run_name] = run_dir

            shutil.copytree(config['input_dir'], run_dir)

        else:

            run_dir = f'{output_dir}/run_{run_name}'
            run_dirs[run_name] = run_dir

            mkdir(run_dir)
            for f in config['input_files']:
                shutil.copy(f, run_dir)


        for name, run_dir in run_dirs.items():

            if not os.path.exists(f'{run_dir}/run.mg5'):
                raise Exception('Error in input files: run.mg5 not found in input dir')

            run_mg5_str = Path(run_dir, 'run.mg5').read_text()

            options = [
                f'set run_tag = {name}',
                f'set nevents = {run_nevents}',
            ]

            options += get_config_options(config)

            options_str = '\n'.join(options)

            print('Adding the following options to run.mg5')
            print(options_str)
            if 'done' in run_mg5_str:
                run_mg5_str = run_mg5_str.replace('done', '')

            run_mg5_str += options_str
            run_mg5_str += '\n\ndone\n'

            with open(f'{run_dir}/run.mg5', 'w') as f:
                f.write(run_mg5_str)


    else:

        #  Cards
        config_cards = config['cards']

        run_madspin = False
        run_pythia = False
        run_delphes = False

        ## Param card
        ## FIX: param card could be optional
        ## allow loop through param cards
        if 'param' in config_cards and isinstance(config_cards['param'], dict):

            for name, card in config_cards['param'].items():
                model_name = f'{run_name}_{name}'
                run_dir_model = f'{output_dir}/run_{model_name}'

                run_dirs[model_name] = run_dir_model

                mkdir(run_dir_model)
                mkdir(f'{run_dir_model}/cards')

                shutil.copyfile(card, f'{run_dir_model}/cards/param_card.dat')

        else:
            run_dir = f'{output_dir}/run_{run_name}'
            run_dirs[run_name] = run_dir

            #if not os.path.exists(inputs_dir):
            mkdir(run_dir)
            mkdir(f'{run_dir}/cards')

            if 'param' in config_cards:
                shutil.copyfile(config_cards["param"], f'{run_dir}/cards/param_card.dat')

        ## Other cards
        run_madspin = 'madspin' in config_cards
        run_pythia  = 'pythia' in config_cards
        run_delphes = 'delphes' in config_cards

        for name, run_dir in run_dirs.items():
            shutil.copyfile(config_cards['run'], f'{run_dir}/cards/run_card.dat')
            if run_madspin:
                shutil.copyfile(config_cards["madspin"], f'{run_dir}/cards/madspin_card.dat')
            if run_pythia:
                shutil.copyfile(config_cards["pythia"],  f'{run_dir}/cards/pythia8_card.dat')
            if run_delphes:
                shutil.copyfile(config_cards["delphes"], f'{run_dir}/cards/delphes_card.dat')

        cards_str = 'cards/run_card.dat\n'
        if 'param' in config_cards:
            cards_str += 'cards/param_card.dat\n'
        if run_madspin:
            cards_str += 'cards/madspin_card.dat\n'
        if run_pythia:
            cards_str += 'cards/pythia8_card.dat\n'
        if run_delphes:
            cards_str += 'cards/delphes_card.dat\n'


        # -------------
        #  MG Run file
        # -------------
        print(f'- Preparing run.mg5 file')
        if 'proc' in config_cards:
            print(f'- Using proc_card from {config_cards["proc"]}')
            process_str = Path(config_cards['proc']).read_text()
        elif 'process' in config:
            print(f'- Using process from config file')
            process_str = config['process']

        config_options = get_config_options(config)
        expert_options = get_expert_options(config)

        if 'set run_card custom_fcts /local/user_cuts.f' in config_options:
            # copy user cut module to each run dir
            shutil.copyfile('user_cuts.f', f'{run_dir}/user_cuts.f')

        for name, run_dir in run_dirs.items():

            options = [
                f'set run_tag = {name}',
                f'set nevents = {run_nevents}',
            ]

            options += config_options

            replace_dict = {
                'process': process_str,
                'run_madspin': 'madspin=ON' if run_madspin else '', #madspin=OFF',
                'run_pythia': 'shower=Pythia8' if run_pythia else 'shower=OFF',
                'run_delphes': 'detector=Delphes' if run_delphes else '',
                'cards': cards_str,
                'options': '\n'.join(options),
                'expert_options': '\n'.join(expert_options) if expert_options else ''
            }

            create_file_from_template('run.mg5', f'{run_dir}/run.mg5', replace_dict)


    return run_dirs


def prepare_job_plan(config, output_dir, run_dirs, run_mode):
    """Assign concrete seeds and write exact per-job command files."""

    run_njobs = config['run'].get('njobs', 1)
    run_nevents = config['run'].get('nevents', 10_000)
    run_outputs = config['run'].get('outputs', ['lhe', 'lhco', 'log'])
    seeds = iter(allocate_seeds(config.get('options', {}).get('seed'), len(run_dirs) * run_njobs))

    planned_jobs = []
    local_dirs = {}

    for sample, directory in run_dirs.items():
        text = (Path(directory) / 'run.mg5').read_text()
        for index in range(run_njobs):
            seed = next(seeds)
            job_id = f'{sample}_{index:05d}'
            seeded_text = render_seeded_run(text, seed)
            outputs = [value for value in run_outputs if value != 'hepmc0']
            if 'hepmc0' in run_outputs and index == 0 and 'hepmc' not in outputs:
                outputs.append('hepmc')
            if run_mode in ('condor', 'jupiter'):
                seed_path = Path(output_dir) / f'{job_id}.mg5'
            else:
                job_directory = directory if run_njobs == 1 else f'{directory}_job{index:05d}'
                if job_directory != directory:
                    shutil.copytree(directory, job_directory)
                local_dirs[job_id] = (sample, job_directory)
                seed_path = Path(job_directory) / 'run.mg5'
            seed_path.write_text(seeded_text)

            planned_jobs.append({'job_id': job_id, 'sample': sample, 'index': index,
                                 'seed': seed, 'nevents': run_nevents, 'outputs': outputs or ['log'],
                                 'run_file': str(seed_path.relative_to(output_dir))})

    if local_dirs and run_njobs > 1:
        for directory in run_dirs.values():
            shutil.rmtree(directory)

    return planned_jobs, local_dirs


def package_sample_inputs(output_dir, run_dirs, run_mode):
    """Package shared Condor inputs, retaining local directories in local modes."""
    # Prepare input files
    if run_mode in ('condor', 'jupiter'):
        for name, run_dir in run_dirs.items():
            print(f'- Compressing input files here: {output_dir}/run_{name}.tar.gz')
            subprocess.run(['tar', '-czf', f'{output_dir}/run_{name}.tar.gz', '-C', run_dir, '.'], check=True)
            shutil.rmtree(run_dir)
    else:
        print('- Running locally, no need to compress input files')


def prepare_wrappers(output_dir, local_dirs, run_mode, outputs=None):
    """Write executable wrappers without starting containers or submitting jobs."""

    if run_mode in ('condor', 'jupiter'):
        script_path = f'{output_dir}/run_mg_pythia_delphes.sh'
        print(f'- Preparing run script: {script_path}')
        create_file_from_script_template('run_mgpd_condor.sh', script_path)
        os.chmod(script_path, 0o755)
    elif run_mode in ('local-docker', 'local-apptainer'):
        for name, run_dir in local_dirs.values():
            script_path = f'{run_dir}/run_mg_pythia_delphes.sh'
            print(f'- Preparing run script: {script_path}')
            create_file_from_script_template(
                'run_mgpd_local.sh',
                script_path,
                { '# MGPD_LOCAL_OUTPUTS': 'outputs=' + shlex.quote(' '.join(outputs or [])) }
            )
            os.chmod(script_path, 0o755)


def prepare_condor_submission(config, output_dir, container_image_path, planned_jobs, production_id=None):
    """Render the submit file; no scheduler access is performed here."""
    config_run = config['run']
    run_name = config_run['name']
    run_email = config_run.get('email', '')
    job_replace_dict = {}

    job_replace_dict['container_image'] = container_image_path

    if run_email:
        job_replace_dict['notifications'] = f"notification = always\nnotify_user = {run_email}"
    else:
        job_replace_dict['notifications'] = ''


    # job requirements
    if 'requirements' in config_run:
        requirements = config_run['requirements']
        job_replace_dict['requirements'] = f'requirements = {requirements}'
    else:
        job_replace_dict['requirements'] = ''

    job_replace_dict['input_file'] = 'run_$(run_name).tar.gz'
    job_replace_dict['arguments'] = '$(run_name) $(input_file) $(outputs) $(output_name) $(seed_file)'

    jobs = ''
    for job in planned_jobs:
        if production_id:
            jobs += f'+MGProductionId = "{production_id}"\n'
            jobs += f'+MGJobId = "{job["job_id"]}"\n'
        jobs += f"run_name = {job['sample']}\n"
        jobs += f"seed_file = {job['run_file']}\n"
        jobs += f"outputs = {','.join(job['outputs'])}\n"
        jobs += 'queue 1\n'

    job_replace_dict['jobs'] = jobs

    # Save job.sub file
    job_file = f'job_{run_name}.sub'
    output_path = f'{output_dir}/{job_file}'

    print(f'- Saving job submission description in {output_path}')
    create_file_from_template('job.sub', output_path, job_replace_dict)

    return job_file


def _prepare_production(config, output, force=False):
    """Prepare files and return an execution plan, without launching work.

    Does not mutate config or change the process working directory. Relative
    inputs are resolved from the caller's cwd, matching the historical CLI.
    """

    validate_config(config)

    original_config = copy.deepcopy(config)
    config = copy.deepcopy(config)

    run_mode = resolve_run_mode(config['run'].get('mode', 'local'))
    config['run']['mode'] = run_mode

    image = resolve_container_image(config['run'], run_mode)

    output_dir = os.path.abspath(os.path.expanduser(output))

    print(f'- Using working/output dir: {output_dir}')
    if os.path.exists(output_dir):
        if not force:
            raise FileExistsError(f'Output directory already exists: {output_dir}')
        print('Output dir already exists. Removing it.')
        shutil.rmtree(output_dir)
    os.mkdir(output_dir)

    run_dirs = prepare_sample_inputs(config, output_dir)
    jobs, local_dirs = prepare_job_plan(config, output_dir, run_dirs, run_mode)

    prepared_files = {}
    for path in sorted(Path(output_dir).rglob('*')):
        if path.is_file():
            prepared_files[str(path.relative_to(output_dir))] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {'schema_version': 2, 'production_id': uuid.uuid4().hex, 'created_at': datetime.now(timezone.utc).isoformat(),
                'state': 'prepared', 'config': original_config,
                'image': image, 'mode': run_mode,
                'image_identity': 'configured reference; not a verified immutable digest',
                'jobs': jobs, 'prepared_input_sha256': prepared_files}
    package_sample_inputs(output_dir, run_dirs, run_mode)
    prepare_wrappers(output_dir, local_dirs, run_mode, config['run'].get('outputs', ['lhe', 'lhco', 'log']))

    submit_file = None
    if run_mode in ('condor', 'jupiter'):
        submit_file = prepare_condor_submission(config, output_dir, image, jobs, manifest['production_id'])
    manifest['execution'] = {'submit_file': submit_file}
    manifest['execution_input_sha256'] = {
        str(path.relative_to(output_dir)): file_sha256(path)
        for path in sorted(Path(output_dir).rglob('*')) if path.is_file()
    }
    manifest['plan_sha256'] = plan_sha256(manifest)

    write_manifest(output_dir, manifest)

    return PreparedProduction(output_dir, manifest, local_dirs, submit_file)


def prepare_production(config, output, force=False):
    """Prepare a new plan; --force cannot erase an execution attempt."""

    output_dir = Path(output).expanduser().resolve()

    if output_dir.exists() and force:

        with execution_lock(output_dir):
            manifest_path = output_dir / 'manifest.json'
            if manifest_path.exists():
                saved = read_production_manifest(output_dir)
                if saved.get('recovery') or saved.get('recoveries'):
                    raise ValueError('Cannot overwrite a production with recovery ownership; use a new output directory')
                if saved.get('execution_attempt') or saved.get('state') != 'prepared':
                    raise ValueError('Cannot overwrite an attempted production; use a new output directory')

            return _prepare_production(config, str(output_dir), force=True)

    return _prepare_production(config, str(output_dir), force=force)
