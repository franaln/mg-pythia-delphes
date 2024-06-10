#! /usr/bin/env python3

import os
import sys
import argparse
import string
import yaml
import shutil


template_run_mg = """${process}

output RUN
launch RUN

${run_madspin}
${run_pythia}

${cards}

${options}

done
"""

template_job_desc = """# MG+Pythia+Delphes - job submission file

universe = container
container_image = ${container_image}

executable = run_mg_pythia_delphes.sh

input_file = inputs_$$(run_name).tar.gz
job_name = $$(Cluster)_$$(Process)
output_name = output_$$(run_name)_$$(job_name)

arguments  = $$(run_name) $$(input_file) $$(outputs) $$(output_name)

output      = job_$$(run_name)_$$(job_name).out
error       = job_$$(run_name)_$$(job_name).err
log         = job_$$(run_name)_$$(job_name).log

should_transfer_files = YES
transfer_input_files = inputs_$$(run_name).tar.gz

transfer_output_files = output_$$(run_name)_$$(job_name).tar.gz
when_to_transfer_output = ON_EXIT
"""

template_run_script = """#!/bin/bash

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

echo "> Runnning MG+Pythia+Delphes "

source /setup_mg_pythia_delphes.sh

run_dir=${job_dir}/RUN
output_dir=${run_dir}/Events/run_01
mg_debug_file=${run_dir}/run_01_${run_name}_debug.log
pythia_log_file=${output_dir}/${run_name}_pythia8.log

mg5_aMC run.mg5

# Check if something failed (in that case save debug output and exit)
sc=$?
if [ $sc -ne 0 ] || [ -f "${mg_debug_file}" ] ;  then
    echo "ERROR running MG. Exiting ..."
    echo "-- MG DEBUG --"
    cat ${mg_debug_file}
    echo "-- Pythia8 DEBUG --"
    cat ${pythia_log_file}
    tar -czf ${output_file} -C ${output_dir} *
    exit 1
fi

ls ${output_dir}
cat ${pythia_log_file}
echo "Finished running MG+Pythia+Delphes, $(date)"

# Output
echo "> Preparing outputs"

output_file_root=${output_name}_delphes_events.root
mv ${output_dir}/${run_name}_delphes_events.root ${output_dir}/${output_file_root}


all_output_files=()

if [[ "${outputs}" =~ "hepmc" ]] ; then
    output_file_hepmc=${output_name}_pythia8_events.hepmc.gz
    mv ${output_dir}/${run_name}_pythia8_events.hepmc.gz ${output_dir}/${output_file_hepmc}
    all_output_files+=(${output_file_hepmc})
fi

if [[ "${outputs}" =~ "lhe" ]] ; then
    output_file_lhe=${output_name}_unweighted_events.lhe.gz
    mv ${output_dir}/unweighted_events.lhe.gz ${output_dir}/${output_file_lhe}
    all_output_files+=(${output_file_lhe})
fi

if [[ "${outputs}" =~ "lhco" ]] ; then

    echo "> Creating lhco output "

    output_file_tmp_lhco=${output_name}_delphes_events_tmp.lhco
    output_file_lhco=${output_name}_delphes_events.lhco
    output_file_banner=run_01_${run_name}_banner.txt

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
            config_options.append(f'set iseed = {opts["seed"]}')
        if 'ecm' in opts:
            config_options.append(f'set ebeam1 = {float(opts["ecm"]) / 2}')
            config_options.append(f'set ebeam2 = {float(opts["ecm"]) / 2}')
        if 'use_syst' in opts:
            config_options.append(f'set use_syst = {opts["use_syst"]}')

    return config_options


def main():

    parser = argparse.ArgumentParser(description='run_mg_pythia_delphes_with_condor.py')

    # Main required arguments
    parser.add_argument('-c', '--config', required=True, help='Configuration file')
    parser.add_argument('-o', '--output', required=True, help='Output directory')

    # Submission options
    parser.add_argument('--nosub', action='store_true', help='Prepare directory and files but don\' submit jobs')

    args = parser.parse_args()


    # Read configuration
    config_file = args.config
    with open(config_file) as f:
        config = yaml.safe_load(f)


    # ------------
    #  Run config
    # ------------
    config_run = config['run']

    run_name = config_run['name']

    print(f'> Running mg-pythia-delphes with condor. Run name = {run_name}. Configuration = {config_file}')

    ## Docker/Apptainer image
    image_dir = '/mnt/R5/images'
    available_images = [
        'mg-pythia-delphes-3_3_2',
        'mg-pythia-delphes-latest',
    ]

    if 'image' not in config_run:
        print('- No image was configured. Using latest: mg-pythia-delphes-latest')
        container_image = f'{image_dir}/mg-pythia-delphes-latest.sif'
    else:
        container_image = f'{image_dir}/{config_run["image"]}.sif'
        if not os.path.exists(container_image):
            print(f'Error: Image {config_run["image"]} does not exist. Use one of the existint images:')
            print('\n'.join(available_images))
            sys.exit(1)

    run_nevents = config_run['nevents'] if 'nevents' in config_run else 10_000
    run_njobs   = config_run['njobs'] if 'njobs' in config_run else 1
    run_outputs = config_run['outputs'] if 'outputs' in config_run else ['lhco']


    # Create working directory
    output = args.output
    if output.startswith('~/') or output.startswith('/'):
        output_dir = output
    else:
        output_dir = os.path.join(os.getcwd(), output)

    print(f'- Using working/output dir: {output_dir}')
    if not os.path.exists(output_dir):
        mkdir(output_dir)


    # Inputs
    inputs_dirs = {}

    #  Custom input
    if 'input_files' in config or 'input_dir' in config:

        inputs_dir = f'{output_dir}/inputs_{run_name}'
        inputs_dirs[run_name] = inputs_dir

        if 'input_dir' in config:
            shutil.copytree(config['input_dir'], inputs_dir)
        else:
            mkdir(inputs_dir)
            for f in config['input_files']:
                shutil.copy(f, inputs_dir)

        if not os.path.exists(f'{inputs_dir}/run.mg5'):
            print('error')
            sys.exit(1)

        run_mg5_str = open(f'{inputs_dir}/run.mg5').read()

        options = [
            f'set run_tag = {run_name}',
            f'set nevents= {run_nevents}',
        ]

        options += get_config_options(config)

        options_str = '\n'.join(options)

        print('Adding the following options to run.mg5')
        print(options_str)
        if 'done' in run_mg5_str:
            run_mg5_str = run_mg5_str.replace('done', '')

        run_mg5_str += options_str
        run_mg5_str += '\n\ndone\n'


        with open(f'{inputs_dir}/run.mg5', 'w') as f:
            f.write(run_mg5_str)


    else:

        #  Cards
        config_cards = config['cards']

        run_madspin = False
        run_pythia = False
        run_delphes = False


        ## Param card
        ## allow loop through param cards
        if isinstance(config_cards['param'], dict):

            for name, card in config_cards['param'].items():
                model_name = f'{run_name}_{name}'
                inputs_dir_model = f'{output_dir}/inputs_{model_name}'

                inputs_dirs[model_name] = inputs_dir_model

                mkdir(inputs_dir_model)
                mkdir(f'{inputs_dir_model}/cards')

                shutil.copyfile(card, f'{inputs_dir_model}/cards/param_card.dat')

        else:
            inputs_dir = f'{output_dir}/inputs_{run_name}'
            inputs_dirs[run_name] = inputs_dir

            #if not os.path.exists(inputs_dir):
            mkdir(inputs_dir)
            mkdir(f'{inputs_dir}/cards')

            shutil.copyfile(config_cards["param"], f'{inputs_dir}/cards/param_card.dat')

        ## Other cards
        run_madspin = 'madspin' in config_cards
        run_pythia  = 'pythia' in config_cards
        run_delphes = 'delphes' in config_cards

        for name, inputs_dir in inputs_dirs.items():
            shutil.copyfile(config_cards['run'], f'{inputs_dir}/cards/run_card.dat')
            if run_madspin:
                shutil.copyfile(config_cards["madspin"], f'{inputs_dir}/cards/madspin_card.dat')
            if run_pythia:
                shutil.copyfile(config_cards["pythia"],  f'{inputs_dir}/cards/pythia8_card.dat')
            if run_delphes:
                shutil.copyfile(config_cards["delphes"], f'{inputs_dir}/cards/delphes_card.dat')

        cards_str = 'cards/run_card.dat\n'
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

        for name, inputs_dir in inputs_dirs.items():

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
                    'cards': cards_str,
                    'options': '\n'.join(options),
                }
            )

            with open(f'{inputs_dir}/run.mg5', 'w') as f:
                f.write(run_mg_str)



    # Prepare input files
    for name, inputs_dir in inputs_dirs.items():
        print(f'- Compressing input files here: {output_dir}/inputs_{name}.tar.gz')
        os.system(f'tar -czf {output_dir}/inputs_{name}.tar.gz -C {inputs_dir} .')


    # -----------
    # Run script
    # -----------
    script_path = f'{output_dir}/run_mg_pythia_delphes.sh'

    print(f'- Preparing run script: {script_path}')
    with open(script_path, 'w') as f:
        f.write(template_run_script)

    os.chmod(script_path, 0o755)


    # Prepare and submit jobs

    # Job script
    template = string.Template(template_job_desc)
    job_desc = template.substitute(
        {
            'container_image': container_image,
        }
    )


    for name in inputs_dirs.keys():

        if 'hepmc0' in run_outputs and run_njobs > 1:
            outputs_str = ','.join([ o for o in run_outputs if o != 'hepmc0' ])

            job_desc += f'run_name = {name}\n'
            job_desc += f'outputs = {outputs_str},hepmc\n'
            job_desc += f'queue\n'

            job_desc += f'run_name = {name}\n'
            job_desc += f'outputs = {outputs_str}\n'
            job_desc += f'queue {run_njobs-1}\n'

        else:
            job_desc += f'run_name = {name}\n'
            job_desc += f'outputs = {",".join(run_outputs)}\n'
            job_desc += f'queue {run_njobs}\n'


    # Save job.sub file
    job_file = f'job_{run_name}.sub'

    print(f'- Saving job submission description in {output_dir}/{job_file}')
    with open(f'{output_dir}/{job_file}', 'w') as f:
        f.write(job_desc)


    if not args.nosub:
        # not using htcondor python api because it does nto support multiple queue in the same job?
        os.chdir(output_dir)
        os.system(f'condor_submit {job_file}')



if __name__ == '__main__':
    main()
