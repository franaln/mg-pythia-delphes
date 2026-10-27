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
transfer_input_files = $$(input_file)

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

if grep -Fxq "set iseed = RANDOM" run.mg5 ; then
    random_seed=${RANDOM}
    echo "Setting random seed = ${random_seed}"
    sed -i "s|set iseed = RANDOM|set iseed = ${random_seed}|g" run.mg5
fi

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
tar -xzmf ${input_file}
rm ${input_file}
ls

if grep -Fxq "set iseed = RANDOM" run.mg5 ; then
    random_seed=${RANDOM}
    echo "Setting random seed = ${random_seed}"
    sed -i "s|set iseed = RANDOM|set iseed = ${random_seed}|g" run.mg5
fi

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
            if str(opts['ncores']).lower() == 'all':
                opts['ncores'] = 'None'
            config_options.append(f'set nb_core {opts["ncores"]}')

    return config_options

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


def main():

    parser = argparse.ArgumentParser(description='run_mg_pythia_delphes.py')

    # Main required arguments
    parser.add_argument('-c', '--config', required=True, help='Configuration file')
    parser.add_argument('-o', '--output', required=False, help='Output directory')
    parser.add_argument('-f', '--force', help='Force overwrite of output files', action='store_true')

    # Run options
    parser.add_argument('--run_mode', default=None, choices=['local-docker', 'local-apptainer', 'condor', 'jupiter'], help='Run mode')
    parser.add_argument('--dry-run', action='store_true', help='Prepare directory and files but don\'t run or submit jobs')

    parser.add_argument("--validate-only", action="store_true", help="Validate inputs without writing files or launching jobs")
    args = parser.parse_args()
    if not args.validate_only and not args.output:
        parser.error("--output is required unless --validate-only is used")


    # Read configuration
    config_file = args.config
    with open(config_file) as f:
        config = yaml.safe_load(f)
    try:
        if args.run_mode is not None and isinstance(config, dict) and isinstance(config.get("run"), dict):
            config["run"]["mode"] = args.run_mode
        validate_config(config)
    except (ValueError, TypeError) as exc:
        parser.error(str(exc))
    if args.validate_only:
        print("Configuration is valid")
        return


    # ------------
    #  Run config
    # ------------
    config_run = config['run']

    run_name = config_run['name']

    if args.run_mode is not None:
        run_mode = args.run_mode
    elif 'mode' in config_run:
        run_mode = config_run['mode']
    else:
        run_mode = 'local-docker'

    print(f'> Running mg-pythia-delphes with run_mode = {run_mode}. Run name = {run_name}. Configuration = {config_file}')

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
                print(f'Error: Image {config_run["image"]} is not available. Use one of the existint images:')
                print('\n'.join(available_images))
                sys.exit(1)
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
            print(f'Error: No default image is available.')
            sys.exit(1)

        print(f'- Running locally with apptainer using image: {container_image_path}')

    run_nevents = config_run['nevents'] if 'nevents' in config_run else 10_000
    run_njobs   = config_run['njobs'] if 'njobs' in config_run else 1
    run_outputs = config_run['outputs'] if 'outputs' in config_run else ['lhe', 'lhco', 'log']
    run_email   = config_run.get('email', '')


    # Create working directory
    output = os.path.expanduser(args.output)
    if output.startswith('~/') or output.startswith('/'):
        output_dir = output
    else:
        output_dir = os.path.join(os.getcwd(), output)

    print(f'- Using working/output dir: {output_dir}')
    if os.path.exists(output_dir):
        if args.force:
            print('Output dir already exists. Removing it.')
            shutil.rmtree(output_dir)
            mkdir(output_dir)
        else:
            print('Output dir already exist. Remove it or use another one. Exit.')
            sys.exit(1)
    else:
        mkdir(output_dir)


    # Inputs
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

            run_mg5_str = open(f'{run_dir}/run.mg5').read()

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
            process_str = open(config_cards['proc']).read()
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



    # Prepare input files
    if run_mode in ('condor', 'jupiter'):
        for name, run_dir in run_dirs.items():
            print(f'- Compressing input files here: {output_dir}/run_{name}.tar.gz')
            subprocess.run(['tar', '-czf', f'{output_dir}/run_{name}.tar.gz', '-C', run_dir, '.'], check=True)
            shutil.rmtree(run_dir)
    else:
        print('- Running locally, no need to compress input files')


    # # Configuration for each run dir
    # for name, run_dir in run_dirs.items():

    #     config_vars_dict = {}

    #     config_vars_dict['run_name'] = name

    #     if run_cluster:
    #         config_vars_dict['input_file'] = 'run_$(run_name).tar.gz'
    #     else:
    #         config_vars_dict['input_file'] = 'SKIP'

    #     # if 'hepmc0' in run_outputs and run_njobs > 1:
    #     #     outputs_str = ','.join([ o for o in run_outputs if o != 'hepmc0' ])

    #     #     jobs += f'outputs = {outputs_str},hepmc\n'
    #     #             jobs += f'queue\n'

    #     #             jobs += f'run_name = {name}\n'
    #     #             jobs += f'outputs = {outputs_str}\n'
    #     #             jobs += f'queue {run_njobs-1}\n'

    #     config_vars_dict['outputs'] = ','.join(run_outputs)

    #     # job_replace_dict['arguments']  = '$(run_name) $(input_file) $(outputs) $(output_name)'

    #     config_vars_str = '\n'.join([f'{key}={value}' for key, value in config_vars_dict.items()])

    #     with open(f'{run_dir}/CONFIG_VARS', 'w') as f:
    #         f.write(config_vars_str+'\n')

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
        for name, run_dir in run_dirs.items():
            script_path = f'{run_dir}/run_mg_pythia_delphes.sh'
            print(f'- Preparing run script: {script_path}')
            with open(script_path, 'w') as f:
                f.write(template_run_local_script)
            os.chmod(script_path, 0o755)


    #-----------
    # Local run
    #-----------
    if run_mode in ('local-docker', 'local-apptainer'):

        for run_name, run_dir in run_dirs.items():

            cmd = 'source /setup_mg_pythia_delphes.sh && cd /local && '
            cmd += shlex.join(['./run_mg_pythia_delphes.sh', run_name, f'run_{run_name}'])

            if run_mode == 'local-docker':
                cmd = ['docker', 'run', '--rm', '-v', f'{run_dir}:/local', container_image_path, cmd]
            elif run_mode == 'local-apptainer':
                cmd = ['apptainer', 'exec', '--bind', f'{run_dir}:/local', container_image_path, '/bin/bash', '-l', '-c', cmd]

            print(cmd)
            if not args.dry_run:
                subprocess.run(cmd, check=True)

    elif run_mode in ('condor', 'jupiter'):

        # Job script
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
        job_replace_dict['arguments']  = '$(run_name) $(input_file) $(outputs) $(output_name)'

        jobs = ''
        for name in run_dirs.keys():

            if 'hepmc0' in run_outputs and run_njobs > 1:
                outputs_str = ','.join([ o for o in run_outputs if o != 'hepmc0' ])

                jobs += f'run_name = {name}\n'
                jobs += f'outputs = {outputs_str},hepmc\n'
                jobs += f'queue\n'

                jobs += f'run_name = {name}\n'
                jobs += f'outputs = {outputs_str}\n'
                jobs += f'queue {run_njobs-1}\n'

            else:
                jobs += f'run_name = {name}\n'
                jobs += f'outputs = {",".join(run_outputs)}\n'
                jobs += f'queue {run_njobs}\n'


        job_replace_dict['jobs'] = jobs

        # Save job.sub file
        job_file = f'job_{run_name}.sub'

        template = string.Template(template_job_desc)
        job_desc = template.substitute(job_replace_dict)

        print(f'- Saving job submission description in {output_dir}/{job_file}')
        with open(f'{output_dir}/{job_file}', 'w') as f:
            f.write(job_desc)

        if not args.dry_run:
            # not using htcondor python api because it does nto support multiple queue in the same job?
            subprocess.run(['condor_submit', job_file], cwd=output_dir, check=True)



if __name__ == '__main__':
    main()
