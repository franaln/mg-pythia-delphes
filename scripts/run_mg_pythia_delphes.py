#! /usr/bin/env python3

import os
import sys
import argparse
import string
import yaml
import shutil
import subprocess
import shlex
import re
from pathlib import Path
import copy
import hashlib
import json
import secrets
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional
from contextlib import contextmanager
import fcntl
import uuid
import socket
from collections import Counter


template_run_mg = """# run.mg5

# Config options
${expert_options}

# Process
${process}

# Output dir
output RUN
launch RUN

# MadSpin/Pythia/Delphes
${run_madspin}
${run_pythia}
${run_delphes}

# Cards
${cards}

# Run Options
${options}

done
"""

template_job_desc = """# MG+Pythia+Delphes - job submission file

universe = container
container_image = ${container_image}

executable = run_mg_pythia_delphes.sh

input_file = ${input_file}
job_name = $$(Cluster)_$$(Process)
output_name = output_$$(run_name)_$$(job_name)

arguments  = ${arguments}

output      = job_$$(run_name)_$$(job_name).out
error       = job_$$(run_name)_$$(job_name).err
log         = job_$$(run_name)_$$(job_name).log

${notifications}

should_transfer_files = YES
transfer_input_files = $$(input_file),$$(seed_file)

transfer_output_files = $$(output_name).tar.gz
when_to_transfer_output = ON_EXIT

${requirements}

${jobs}

"""

template_run_local_script = """#!/bin/bash

run_name=$1
run_dir=$2

echo -e ">>> Running run_mg_pythia_delphes.sh with the following configuration:\n"
echo "date          = "$(date)
echo "hostname      = "$HOSTNAME
echo "current dir   = "$PWD
echo ""
echo "run_name      = "${run_name}
echo "run_dir       = "${run_dir}
echo ""

job_dir=$PWD

# echo "> Moving to run_directory ${run_dir}"
# cd ${run_dir}


echo "> Runnning MG+Pythia+Delphes "

if [ -z ${MG_DIR+x} ] ; then
    source /setup_mg_pythia_delphes.sh
fi

run_dir=${job_dir}/RUN

if ! mg5_aMC run.mg5; then
    echo "ERROR running MadGraph; preserving working directory for diagnosis" >&2
    exit 1
fi

if [ -d ${run_dir}/Events/run_01_decayed_1 ] ; then
    output_dir_name=run_01_decayed_1
elif  [ -d ${run_dir}/Events/run_01 ] ; then
    output_dir_name=run_01
fi

output_dir=${run_dir}/Events/${output_dir_name}


if [ -z "${output_dir_name}" ] || [ ! -d "${output_dir}" ]; then
    echo "ERROR: MadGraph output directory is missing" >&2
    exit 1
fi

ls ${output_dir}

echo "Finished running MG+Pythia+Delphes, $(date)"

"""

template_run_condor_script = """#!/bin/bash

run_name=$1
input_file=$2
outputs=$3
output_name=$4

output_file=${output_name}.tar.gz

echo -e ">>> Running run_mg_pythia_delphes.sh with the following configuration:\n"
echo "date          = "$(date)
echo "hostname      = "$HOSTNAME
echo "current dir   = "$PWD
echo ""
echo "run_name      = "${run_name}
echo "input_file    = "${input_file}
echo "outputs       = "${outputs}
echo "output_name   = "${output_name}
echo "output_file   = "${output_file}
echo ""

job_dir=$PWD

echo "> Preparing input files "
tar -xzmf "${input_file}" || exit 1
if [ -n "${5:-}" ]; then
    cp -- "$5" run.mg5 || exit 1
fi
rm "${input_file}"
ls


echo "> Runnning MG+Pythia+Delphes "

if [ -z ${MG_DIR+x} ] ; then
    source /setup_mg_pythia_delphes.sh
fi

run_dir=${job_dir}/RUN

if ! mg5_aMC run.mg5; then
    echo "ERROR running MadGraph; preserving working directory for diagnosis" >&2
    exit 1
fi

if [ -d ${run_dir}/Events/run_01_decayed_1 ] ; then
    output_dir_name=run_01_decayed_1
elif  [ -d ${run_dir}/Events/run_01 ] ; then
    output_dir_name=run_01
fi

output_dir=${run_dir}/Events/${output_dir_name}

if [ -z "${output_dir_name}" ] || [ ! -d "${output_dir}" ]; then
    echo "ERROR: MadGraph output directory is missing" >&2
    tar -czf "${output_file}" RUN run.mg5
    exit 1
fi

ls ${output_dir}

echo "Finished running MG+Pythia+Delphes, $(date)"


# Outputs
echo "> Preparing outputs"

output_file_root=${output_name}_delphes_events.root
if [[ "${outputs}" =~ root|lhco|all ]]; then
    mv "${output_dir}/${run_name}_delphes_events.root" "${output_dir}/${output_file_root}" || exit 1
fi

all_output_files=()

## logs
if [[ "${outputs}" =~ "log" ]] ; then
    output_file_logs=${output_name}_logs.tar
    find ${run_dir} -iname '*.log' -print0 | tar -cvf ${output_dir}/${output_file_logs} --null -T -
    all_output_files+=(${output_file_logs})
fi

## delphes .root
if [[ "${outputs}" =~ "root" ]] ; then
    all_output_files+=(${output_file_root})
fi

## HEPMC (Pythia8 output)
if [[ "${outputs}" =~ "hepmc" ]] ; then
    output_file_hepmc=${output_name}_pythia8_events.hepmc.gz
    mv ${output_dir}/${run_name}_pythia8_events.hepmc.gz ${output_dir}/${output_file_hepmc}
    all_output_files+=(${output_file_hepmc})
fi

## LHE
if [[ "${outputs}" =~ "lhe" ]] ; then
    output_file_lhe=${output_name}_unweighted_events.lhe.gz

    mv ${output_dir}/unweighted_events.lhe.gz ${output_dir}/${output_file_lhe}
    all_output_files+=(${output_file_lhe})

    # if madspin output also save lhe before (useful or not?)
    if [[ "${output_dir_name}" =~ "decayed" && -f ${run_dir}/Events/run_01/unweighted_events.lhe ]] ; then
        output_file_lhe_before=${output_name}_unweighted_events_before_decay.lhe.gz
        gzip ${run_dir}/Events/run_01/unweighted_events.lhe
        mv ${run_dir}/Events/run_01/unweighted_events.lhe.gz ${output_dir}/${output_file_lhe_before}
        all_output_files+=(${output_file_lhe_before})
    fi

fi

## LHCO (Delphes output)
if [[ "${outputs}" =~ "lhco" ]] ; then

    echo "> Creating lhco output "

    output_file_tmp_lhco=${output_name}_delphes_events_tmp.lhco
    output_file_lhco=${output_name}_delphes_events.lhco
    output_file_banner=${output_dir_name}_${run_name}_banner.txt

    root2lhco ${output_dir}/${output_file_root} ${output_dir}/${output_file_tmp_lhco}

    if [ ! -e ${output_dir}/${output_file_tmp_lhco} ]; then
        echo "ERROR: no lhco output file. Exiting ..."
        ls ${output_dir}
        tar -czf ${output_file} -C ${job_dir} *
        exit 1
    fi

    # Merge lhco and banner (copied from run_delphes3)
    sed -e "s/^/# /g" ${output_dir}/${output_file_banner} > ${output_dir}/${output_file_lhco}
    echo "" >> ${output_dir}/${output_file_lhco}
    cat ${output_dir}/${output_file_tmp_lhco} >> ${output_dir}/${output_file_lhco}

    all_output_files+=(${output_file_lhco})
fi

if [[ "${outputs}" =~ "all" ]] ; then
    tar -czf ${output_file} -C ${output_dir} *
else
    tar -cvzf ${output_file} -C ${output_dir} ${all_output_files[@]}
fi

echo "Finished OK, $(date)"
"""


def mkdir(path):
    try:
        os.mkdir(path)
    except FileExistsError:
        pass

def get_config_options(config):
    config_options = []
    if 'options' in config:
        opts = config['options']
        if 'seed' in opts:
            if isinstance(opts['seed'], str) and opts['seed'].strip().upper() == 'RANDOM':
                config_options.append(f'set iseed = RANDOM')
            else:
                config_options.append(f'set iseed = {opts["seed"]}')
        if 'ecm' in opts:
            config_options.append(f'set ebeam1 = {float(opts["ecm"]) / 2}')
            config_options.append(f'set ebeam2 = {float(opts["ecm"]) / 2}')
        if 'use_syst' in opts:
            config_options.append(f'set use_syst = {opts["use_syst"]}')
        if 'extra' in opts:
            if isinstance(opts['extra'], str):
                config_options.append(opts['extra'])
            elif isinstance(opts['extra'], list):
                config_options.extend(opts['extra'])
        if 'user_cuts' in opts:
            config_options.append('set user_cut_module user_modules.py')

    return config_options

def get_expert_options(config):
    config_options = []
    if 'expert' in config:
        opts = config['expert']
        if 'mode' in opts:
            if opts["mode"] == 'single':
                config_options.append('set run_mode 0')
            elif opts['mode'] == 'multi':
                config_options.append('set run_mode 2')
        if 'ncores' in opts:
            ncores = 'None' if str(opts['ncores']).lower() == 'all' else opts['ncores']
            config_options.append(f'set nb_core {ncores}')

    return config_options

# MG5 uses positive seeds smaller than 30081. Allocate without replacement.
MAX_SEED = 30080


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


def write_manifest(output_dir, manifest):
    """Atomically record production metadata beside prepared inputs."""
    path = Path(output_dir) / 'manifest.json'
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
    temporary.replace(path)


def validate_config(config):
    """Validate inputs without creating directories or submitting work.

    Relative input paths retain the CLI's historical current-directory semantics.
    """
    if not isinstance(config, dict) or not isinstance(config.get('run'), dict):
        raise ValueError("Configuration must contain a 'run' mapping")
    run = config['run']
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', str(run.get('name', ''))):
        raise ValueError('run.name must be a nonempty filesystem-safe name')
    for key in ('nevents', 'njobs'):
        value = run.get(key, 10000 if key == 'nevents' else 1)
        if type(value) is not int or value <= 0:
            raise ValueError(f'run.{key} must be a positive integer')
    modes = ('local-docker', 'local-apptainer', 'condor', 'jupiter')
    if run.get('mode', 'local-docker') not in modes:
        raise ValueError(f'run.mode must be one of {modes}')
    outputs = run.get('outputs', ['lhe', 'lhco', 'log'])
    if not isinstance(outputs, list) or not outputs or any(
        x not in ('lhe', 'lhco', 'root', 'hepmc', 'hepmc0', 'log', 'all') for x in outputs
    ):
        raise ValueError('run.outputs must be a nonempty list of supported outputs')
    options = config.get('options', {})
    if not isinstance(options, dict):
        raise ValueError('options must be a mapping')
    seed = options.get('seed')
    if not (seed is None or type(seed) is int and 0 <= seed <= MAX_SEED
            or isinstance(seed, str) and seed.strip().upper() == 'RANDOM'):
        raise ValueError(f'options.seed must be 0, RANDOM, or an integer from 1 to {MAX_SEED}')
    samples = len(config.get('input_dirs', {})) if 'input_dirs' in config else 1
    if isinstance(config.get('cards', {}).get('param'), dict):
        samples = len(config['cards']['param'])
    if not 1 <= samples * run.get('njobs', 1) <= MAX_SEED:
        raise ValueError(f'Production must contain between 1 and {MAX_SEED} jobs')
    paths = []
    if 'input_dirs' in config:
        if not isinstance(config['input_dirs'], dict) or not config['input_dirs']:
            raise ValueError('input_dirs must be a nonempty name-to-directory mapping')
        for name, directory in config['input_dirs'].items():
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', str(name)):
                raise ValueError('input_dirs names must be filesystem-safe')
            paths.append(Path(directory) / 'run.mg5')
    elif 'input_dir' in config:
        paths.append(Path(config['input_dir']) / 'run.mg5')
    elif 'input_files' in config:
        paths.extend(Path(x) for x in config['input_files'])
        if not any(x.name == 'run.mg5' for x in paths):
            raise ValueError('input_files must include run.mg5')
    else:
        cards = config.get('cards')
        if not isinstance(cards, dict) or 'run' not in cards:
            raise ValueError('cards.run is required')
        if 'proc' not in cards and not config.get('process'):
            raise ValueError('process or cards.proc is required')
        for card in cards.values():
            paths.extend(Path(x) for x in (card.values() if isinstance(card, dict) else [card]))
        if any(x in outputs for x in ('root', 'lhco')) and 'delphes' not in cards:
            raise ValueError('ROOT/LHCO output requires a Delphes card')
    for path in paths:
        if not path.is_file():
            raise ValueError(f'Input file does not exist: {path}')
    return config


@dataclass(frozen=True)
class PreparedProduction:
    """Resolved execution plan loaded from a saved production."""
    output_dir: str
    manifest: dict
    local_dirs: dict
    submit_file: Optional[str] = None


def load_config(config_file, run_mode=None):
    """Load and validate YAML. Relative inputs retain current-directory semantics."""
    with open(config_file) as source:
        config = yaml.safe_load(source)
    if run_mode is not None and isinstance(config, dict) and isinstance(config.get('run'), dict):
        config['run']['mode'] = run_mode
    return validate_config(config)


def resolve_container_image(config_run, run_mode):
    """Resolve a configured image without modifying the caller's configuration."""
    config_run = copy.deepcopy(config_run)
    available_images = [
        'mg-pythia-delphes-3.3.2',
        'mg-pythia-delphes-3.5.6',
        'mg-pythia-delphes-latest',
    ]

    ## Docker/Apptainer image
    if run_mode in ('condor', 'jupiter'):

        image_dir = '/opt/images'

        if 'image' not in config_run:
            print('- No image was configured. Using latest: mg-pythia-delphes-latest')
            container_image = 'mg-pythia-delphes-latest'
        else:
            if ':' in config_run['image']:
                config_run['image'] = config_run['image'].replace(':', '-')

            if config_run['image'] not in available_images:
                raise ValueError(f'Image {config_run["image"]} is not available; choose from {available_images}')
            else:
                container_image = config_run["image"]

        container_image_path = f'{image_dir}/{container_image}.sif'
        print(f'- Running with condor using image: {container_image_path}')

    elif run_mode == 'local-docker':
        if 'image' in config_run:
            container_image_path = config_run['image']
        else:
            container_image_path = 'franaln/mg-pythia-delphes:latest'

        print(f'- Running locally with docker using image: {container_image_path}')

    elif run_mode == 'local-apptainer':

        if 'image' in config_run:
            if config_run['image'] in available_images and os.path.exists(f'/opt/images/{config_run["image"]}.sif'):
                container_image_path = f'/opt/images/{config_run["image"]}.sif'
            else:
                container_image_path = config_run['image']
        else:
            raise ValueError('No default Apptainer image is available')

        print(f'- Running locally with apptainer using image: {container_image_path}')

    return container_image_path


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

            template = string.Template(template_run_mg)
            run_mg_str = template.substitute(
                {
                    'process': process_str,
                    'run_madspin': 'madspin=ON' if run_madspin else '', #madspin=OFF',
                    'run_pythia': 'shower=Pythia8' if run_pythia else 'shower=OFF',
                    'run_delphes': 'detector=Delphes' if run_delphes else '',
                    'cards': cards_str,
                    'options': '\n'.join(options),
                    'expert_options': '\n'.join(expert_options) if expert_options else ''
                }
            )

            with open(f'{run_dir}/run.mg5', 'w') as f:
                f.write(run_mg_str)



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


def prepare_wrappers(output_dir, local_dirs, run_mode):
    """Write executable wrappers without starting containers or submitting jobs."""
    # -----------
    # Run script
    # -----------
    if run_mode in ('condor', 'jupiter'):
        script_path = f'{output_dir}/run_mg_pythia_delphes.sh'
        print(f'- Preparing run script: {script_path}')
        with open(script_path, 'w') as f:
            f.write(template_run_condor_script)
        os.chmod(script_path, 0o755)
    elif run_mode in ('local-docker', 'local-apptainer'):
        for name, run_dir in local_dirs.values():
            script_path = f'{run_dir}/run_mg_pythia_delphes.sh'
            print(f'- Preparing run script: {script_path}')
            with open(script_path, 'w') as f:
                f.write(template_run_local_script)
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

    template = string.Template(template_job_desc)
    job_desc = template.substitute(job_replace_dict)

    print(f'- Saving job submission description in {output_dir}/{job_file}')
    with open(f'{output_dir}/{job_file}', 'w') as f:
        f.write(job_desc)

    return job_file


def _prepare_production(config, output, force=False):
    """Prepare files and return an execution plan, without launching work.

    Does not mutate config or change the process working directory. Relative
    inputs are resolved from the caller's cwd, matching the historical CLI.
    """
    validate_config(config)
    original_config = copy.deepcopy(config)
    config = copy.deepcopy(config)
    run_mode = config['run'].get('mode', 'local-docker')
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
    prepare_wrappers(output_dir, local_dirs, run_mode)
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


def build_local_command(run_mode, image, sample, run_dir):
    """Return argv for the same container command used by the CLI."""
    inner = 'source /setup_mg_pythia_delphes.sh && cd /local && '
    inner += shlex.join(['./run_mg_pythia_delphes.sh', sample, f'run_{sample}'])
    if run_mode == 'local-docker':
        return ['docker', 'run', '--rm', '-v', f'{run_dir}:/local', image, inner]
    if run_mode == 'local-apptainer':
        return ['apptainer', 'exec', '--bind', f'{run_dir}:/local', image, '/bin/bash', '-l', '-c', inner]
    raise ValueError(f'Not a local execution mode: {run_mode}')


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def plan_sha256(manifest):
    """Checksum of immutable plan metadata, excluding execution state/history."""
    keys = ('schema_version', 'production_id', 'config', 'mode', 'image', 'jobs',
            'prepared_input_sha256', 'execution', 'execution_input_sha256')
    try:
        data = {key: manifest[key] for key in keys}
    except KeyError as exc:
        raise ValueError(f'Missing production metadata: {exc}') from exc
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def production_path(output_dir, relative):
    """Resolve saved relative paths inside this production, including symlinks."""
    root = Path(output_dir).resolve()
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise ValueError(f'Invalid saved input path: {relative}')
    path = root / relative
    if not path.resolve().is_relative_to(root):
        raise ValueError(f'Saved input path escapes production: {relative}')
    return path


@contextmanager
def execution_lock(output_dir):
    """Nonblocking advisory lock shared by cooperating runner processes."""
    with open(Path(output_dir) / '.execution.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('Another process is using this production') from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def prepare_production(config, output, force=False):
    """Prepare a new plan; --force cannot erase an execution attempt."""
    output_dir = Path(output).expanduser().resolve()
    if output_dir.exists() and force:
        with execution_lock(output_dir):
            manifest_path = output_dir / 'manifest.json'
            if manifest_path.exists():
                saved = read_production_manifest(output_dir)
                if saved.get('execution_attempt') or saved.get('state') != 'prepared':
                    raise ValueError('Cannot overwrite an attempted production; use a new output directory')
            return _prepare_production(config, str(output_dir), force=True)
    return _prepare_production(config, str(output_dir), force=force)


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


CONDOR_STATUS_ATTRIBUTES = ('ClusterId,ProcId,JobStatus,ExitCode,ExitBySignal,ExitSignal,'
                            'HoldReason,HoldReasonCode,RemoveReason,MGProductionId,MGJobId')


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
            records = json.loads(response.stdout)
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


def read_production_manifest(output_dir):
    """Read recorded metadata only; does not query Condor or validate outputs."""
    with open(Path(output_dir) / 'manifest.json') as source:
        return json.load(source)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='run_mg_pythia_delphes.py')

    # Main required arguments
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('-c', '--config', help='Configuration file')
    source.add_argument('--status', metavar='DIRECTORY', help='Read production status from the queue/history')
    parser.add_argument('--json', action='store_true', dest='json_output', help='Print status as JSON')
    source.add_argument('--execute-prepared', metavar='DIRECTORY', help='Verify and execute an existing production once')
    parser.add_argument('-o', '--output', required=False, help='Output directory')
    parser.add_argument('-f', '--force', help='Force overwrite of output files', action='store_true')

    # Run options
    parser.add_argument('--run_mode', default=None, choices=['local-docker', 'local-apptainer', 'condor', 'jupiter'], help='Run mode')
    parser.add_argument('--dry-run', action='store_true', help='Prepare directory and files but don\'t run or submit jobs')

    parser.add_argument("--validate-only", action="store_true", help="Validate inputs without writing files or launching jobs")
    args = parser.parse_args(argv)
    if (args.execute_prepared or args.status) and (args.output or args.force or args.dry_run or args.validate_only or args.run_mode):
        parser.error("Saved-production actions cannot be combined with configuration/preparation options")
    if args.json_output and not args.status:
        parser.error("--json requires --status")
    if not args.execute_prepared and not args.status and not args.validate_only and not args.output:
        parser.error("--output is required unless --validate-only is used")


    return parser, args


def main(argv=None):
    parser, args = parse_args(argv)
    if args.status:
        try:
            status = production_status(args.status)
        except (OSError, ValueError) as exc:
            print(f'Error: {exc}', file=sys.stderr)
            return 1
        if args.json_output:
            print(json.dumps(status, indent=2, sort_keys=True))
        else:
            print(f"Production: {status['state']} (recorded: {status['recorded_state']})")
            for job in status['jobs']:
                print(f"  {job['job_id']} [{job['scheduler_id'] or '-'}]: {job['state']}"
                      + (f" — {job['HoldReason']}" if job.get('HoldReason') else ''))
            if status.get('note'):
                print(status['note'])
            for error in status['query_errors']:
                print(f"Query error ({error['command']}): {error['error']}", file=sys.stderr)
        return int(bool(status['query_errors']))
    if args.execute_prepared:
        try:
            execute_production(load_prepared_production(args.execute_prepared))
        except (ValueError, FileNotFoundError) as exc:
            print(f'Error: {exc}', file=sys.stderr)
            return 1
        return
    try:
        config = load_config(args.config, args.run_mode)
    except (ValueError, TypeError) as exc:
        parser.error(str(exc))
    if args.validate_only:
        print('Configuration is valid')
        return
    print(f'> Running mg-pythia-delphes with run_mode = {config["run"].get("mode", "local-docker")}. '
          f'Run name = {config["run"]["name"]}. Configuration = {args.config}')
    try:
        production = prepare_production(config, args.output, force=args.force)
    except (ValueError, FileExistsError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1
    if not args.dry_run:
        execute_production(production)


if __name__ == '__main__':
    sys.exit(main())
