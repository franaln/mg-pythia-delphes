#! /usr/bin/env python3

import os
import re
import sys
import argparse
import string
import yaml
import htcondor
from htcondor import dags

template_run_mg = """${process}

output RUN
launch RUN

shower=Pythia8

${param_card}
${run_card}
${pythia_card}
${delphes_card}

${other_options}

done
"""

template_job = """# MG+Pythia+Delphes - job submission file

universe = container
container_image = ${container_image}

executable = run_mg_pythia_delphes.sh

run_name = ${run_name}
job_name = $$(Cluster)_$$(Process)

arguments  = $$(run_name) $$(job_name)

output      = job_$$(run_name)_$$(job_name).out
error       = job_$$(run_name)_$$(job_name).err
log         = job_$$(run_name)_$$(job_name).log

should_transfer_files = YES
transfer_input_files = inputs_$$(run_name).tar.gz

transfer_output_files = output_$$(run_name)_$$(job_name).tar.gz
when_to_transfer_output = ON_EXIT

queue ${njobs}

"""

template_run_script = """#!/bin/bash

run_name=$1
job_name=$2

run_input=inputs_${run_name}.tar.gz
output_name=output_${run_name}_${job_name}
output_file=${output_name}.tar.gz

echo -e ">>> Running run_mg_pythia_delphes.sh with the following configuration:\n"
echo "date          = "$(date)
echo "hostname      = "$HOSTNAME
echo "current dir   = "$PWD
echo ""
echo "run_name      = "${run_name}
echo "job_name      = "${job_name}
echo "output_name   = "${output_name}
echo "run_input     = "${run_input}
echo "output_file   = "${output_file}
echo ""

job_dir=$PWD

echo "> Preparing input files "

tar -xzmf ${run_input}
rm ${run_input}
ls

echo "> Runnning MG+Pythia+Delphes "

source /setup_mg_pythia_delphes.sh

run_dir=${job_dir}/RUN
output_dir=${run_dir}/Events/run_01
mg_debug_file=${run_dir}/run_01_${run_name}_debug.log
pythia_log_file=${output_dir}/${run_name}_pythia8.log

cd ${job_dir}

mg5_aMC run.mg5

# Check if something failed (in that case save debug output and exit)
sc=$$?
if [ $$sc -ne 0 ] || [ -f "${mg_debug_file}" ] ;  then
    echo "ERROR running MG. Exiting ..."
    echo "-- MG DEBUG --"
    cat ${mg_debug_file}
    echo "--------------"
    echo "-- Pythia8 DEBUG --"
    cat ${pythia_log_file}
    echo "-------------------"
    tar -cvzf ${output_file} ${output_dir}/*
    exit 1
fi

ls ${output_dir}

cat ${pythia_log_file}

echo "Finished running MG+Pythia+Delphes, $(date)"

output_file_root=${run_name}_delphes_events.root

"""

template_script_prepare_lhco = """
echo "> Creating lhco output "

output_file_tmp_lhco=${output_name}_delphes_events_tmp.lhco
output_file_lhco=${output_name}_delphes_events.lhco

output_file_banner=run_01_${run_name}_banner.txt

cd ${job_dir}

root2lhco ${output_dir}/${output_file_root} ${output_dir}/${output_file_tmp_lhco}

if [ ! -e ${output_dir}/${output_file_tmp_lhco} ]; then
    echo "ERROR: no lhco output file. Exiting ..."
    ls
    ls ${output_dir}
    tar -cvzf ${output_file} ${job_dir}/*
    exit 1
fi

# Merge lhco and banner (copied from run_delphes3)
sed -e "s/^/# /g" ${output_dir}/${output_file_banner} > ${output_dir}/${output_file_lhco}
echo "" >> ${output_dir}/${output_file_lhco}
cat ${output_dir}/${output_file_tmp_lhco} >> ${output_dir}/${output_file_lhco}

"""

template_script_prepare_lhe = """
output_file_lhe=${output_name}_unweighted_events.lhe.gz

echo "> Preparing lhe file"
#gzip -d ${output_dir}/unweighted_events.lhe.gz
mv ${output_dir}/unweighted_events.lhe.gz ${output_dir}/${output_file_lhe}

"""

template_script_outputs_begin = """
echo "> Preparing outputs "

"""

template_script_outputs_end = """

# Add all together and compress
tar -cvzf ${output_file} -C ${output_dir} ${all_output_files[@]}

echo "Finished OK, $(date)"

"""

def main():

    parser = argparse.ArgumentParser(description='run_mg_pythia_delphes_with_condor.py')

    # Main required arguments
    parser.add_argument('-n', '--name',   required=True, help='Run name')
    parser.add_argument('-o', '--output', required=True, help='Run/Output directory')
    parser.add_argument('-c', '--config', required=True, help='Configuration file')

    # Submission options
    parser.add_argument('--nosub', action='store_true', help='Prepare directory and files but don\' submit jobs')
    parser.add_argument('--oldskool', action='store_true', help='Use submisison files instead of condor python API')


    args = parser.parse_args()

    run_name = args.name
    output_dir = args.output
    config_file = args.config

    # Read configuration
    with open(config_file) as f:
        config = yaml.safe_load(f)

    print(f'> Running mg-pythia-delphes with condor with name = {run_name} and configuration = {config_file}')


    # Create working directory
    print(f'- Using working/output dir: {output_dir}')
    if not os.path.exists(output_dir):
        os.system(f'mkdir {output_dir}')
        if not os.path.exists(f'{output_dir}/inputs'):
            os.system(f'mkdir {output_dir}/inputs')

    # Copy cards to cards directory

    ## original run file
    if 'run_file' in config:
        os.system(f'cp {config["run_file"]} {output_dir}/inputs/run.mg5.orig')

    ## cards
    if 'cards' in config:
        if 'run_card' in config['cards']:
            os.system(f'cp {config["cards"]["run_card"]} {output_dir}/inputs/run_card.dat')
        if 'param_card' in config['cards']:
            os.system(f'cp {config["cards"]["param_card"]} {output_dir}/inputs/param_card.dat')
        if 'pythia_card' in config['cards']:
            os.system(f'cp {config["cards"]["pythia_card"]} {output_dir}/inputs/pythia8_card.dat')
        if 'delphes_card' in config['cards']:
            os.system(f'cp {config["cards"]["delphes_card"]} {output_dir}/inputs/delphes_card.dat')


    # Other options (some of them replacing run_card options)
    other_options = []

    other_options.append(f'set run_tag = {run_name}')

    if 'options' in config:

        opts = config['options']

        if 'seed' in opts:
            other_options.append(f'set iseed = {opts["seed"]}')
        if 'ecm' in opts:
            other_options.append(f'set ebeam1 = {float(opts["ecm"]) / 2}')
            other_options.append(f'set ebeam2 = {float(opts["ecm"]) / 2}')
        if 'use_syst' in opts:
            other_options.append(f'set use_syst = {opts["use_syst"]}')
        if 'nevents' in opts:
            other_options.append(f'set nevents = {opts["nevents"]}')



    # -------------
    #  MG Run file
    # -------------

    run_mg_path = f'{output_dir}/inputs/run.mg5'
    run_mg_orig = f'{output_dir}/inputs/run.mg5.orig'

    print(f'- Preparing run.mg5 file: {run_mg_path}')
    if 'run_file' in config and os.path.exists(run_mg_orig):
        print(f'- Using run_file from {config["run_file"]}')
        process_str = open(run_mg_orig).read()
    elif 'process' in config:
        print(f'- Using process from config file ')
        process_str = config['process']
    else:
        print('Error: run.mg5 not found and process not defined. Exiting...')
        sys.exit(1)

    print(f'- Adding following options:')
    print('\n'.join(other_options))

    template = string.Template(template_run_mg)
    run_mg_str = template.substitute(
        {
            'process': process_str,
            'param_card': 'param_card.dat',
            'run_card': 'run_card.dat',
            'pythia_card': 'pythia8_card.dat',
            'delphes_card': 'delphes_card.dat',
            'other_options': '\n'.join(other_options),
        }
    )

    with open(run_mg_path, 'w') as f:
        f.write(run_mg_str)


    # Docker/Apptainer image
    image_dir = '/mnt/R5/images'
    if 'image' not in config:
        print('- No image was configured. Using latest: mg-pythia-delphes-latest.sif')
        container_image = f'{image_dir}/mg-pythia-delphes-latest.sif'
    else:
        container_image = f'{image_dir}/{config["image"]}'
        if not os.path.exists(container_image):
            print(f'Error: Image {container_image} does not exist. Use one of the existint images:')
            print('     mg-pythia-delphes-3_3_2.sif')
            print('     mg-pythia-delphes-latest.sif')
            sys.exit(1)


    # Prepare input files
    print(f'- Compressing input files here: {output_dir}/inputs_{run_name}.tar.gz')
    os.system(f'tar -czf {output_dir}/inputs_{run_name}.tar.gz -C {output_dir}/inputs .')


    # -----------
    # Run script
    # -----------
    script_path = f'{output_dir}/run_mg_pythia_delphes.sh'

    # outputs
    outputs = config['options']['outputs'] if 'outputs' in config['options'] else ['root']
    all_output_files = [ f'${{output_file_{output}}}' for output in outputs ]

    print(f'- Preparing run script: {script_path}')
    with open(script_path, 'w') as f:
        f.write(template_run_script)

        if 'lhe' in outputs:
            f.write(template_script_prepare_lhe)

        if 'lhco' in outputs:
            f.write(template_script_prepare_lhco)

        f.write(template_script_outputs_begin)
        f.write('all_output_files=(' + ' ' .join(all_output_files) + ')')
        f.write(template_script_outputs_end)


    os.chmod(script_path, 0o755)


    # Prepare and submit jobs
    print('- Preparing jobs to submit')

    njobs = config['options']['njobs'] if 'options' in config and 'njobs' in config['options'] else 1

    if args.oldskool:

        # Job script
        template = string.Template(template_job)
        job_file_str = template.substitute(
            {
                'container_image': container_image,
                'run_name': run_name,
                'njobs': njobs,
            }
        )

        # Save job.sub file
        job_file = f'job_{run_name}.sub'

        print(f'- Preparing job file: {output_dir}/{job_file}')
        with open(f'{output_dir}/{job_file}', 'w') as f:
            f.write(job_file_str)

        # Send job
        if not args.nosub:
            print(f'- Submitting {njobs} job(s) to condor using {job_file}')
            os.chdir(output_dir)
            os.system(f'condor_submit {job_file}')

    else:

        # Prepare and send jobs
        os.chdir(output_dir)

        job_dict = {
            'universe': 'container',
            'container_image': container_image,

            'executable': 'run_mg_pythia_delphes.sh',

            'run_name': run_name,
            'job_name': '$(Cluster)_$(Process)',

            'arguments': '$(run_name) $(job_name)',

            'output': 'job_$(run_name)_$(job_name).out',
            'error':  'job_$(run_name)_$(job_name).err',
            'log':    'job_$(run_name)_$(job_name).log',

            'should_transfer_files': 'YES',
            'transfer_input_files': 'inputs_$(run_name).tar.gz',

            'transfer_output_files': 'output_$(run_name)_$(job_name).tar.gz',
            'when_to_transfer_output': 'ON_EXIT',
        }

        job_desc = htcondor.Submit(job_dict)

        #print(job_desc)

        if not args.nosub:
            schedd = htcondor.Schedd()
            submit_result = schedd.submit(job_desc, count=njobs)
            print(f'{njobs} job(s) sumitted to cluster {submit_result.cluster()}')


if __name__ == '__main__':
    main()
