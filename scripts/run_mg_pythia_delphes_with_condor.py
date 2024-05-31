#! /usr/bin/env python3

import os
import re
import sys
import argparse
import string
import yaml
import htcondor

template_job = """# MG+Pythia+Delphes - job submission file

universe = container
container_image = ${container_image}

executable = ${executable}
arguments  = ${output_name}_$$(Cluster)_$$(Process)

output      = job_$$(Cluster)_$$(Process).out
error       = job_$$(Cluster)_$$(Process).err
log         = job_$$(Cluster)_$$(Process).log

should_transfer_files = YES
transfer_input_files = ${run_input}

transfer_output_files = ${output_name}_$$(Cluster)_$$(Process).tar.gz
when_to_transfer_output = ON_EXIT

queue ${njobs}

"""

template_run_script = """#!/bin/bash

run_name=${run_name}
run_input=${run_input}
run_mg_file=${run_mg_file}
output_name=$$1

echo -e "\n>>> Running run_mg_pythia_delphes.sh with the following configuration:"
echo "date          = "$$(date)
echo "hostname      = "$$HOSTNAME
echo "current dir   = "$$PWD
echo ""
echo "run_name      = "$${run_name}
echo "run_input     = "$${run_input}
echo "run_mg_file   = "$${run_mg_file}
echo "output_name   = "$${output_name}
echo ""

# ------
# Config
# ------
job_dir=$$PWD

echo -e "\n>>> Setup MG+Pythia+Delphes\n"
source /setup_mg_pythia_delphes.sh

mg_bin=$${MG_DIR}/MG5_aMC/bin/mg5_aMC
delphes_root2lhco_bin=$${MG_DIR}/Delphes/root2lhco

run_tar_file=$${run_input}
run_dir=$${job_dir}/RUN
run_output_dir=$${run_dir}/Events/run_01
run_output_root_file=$${run_output_dir}/$${run_name}_delphes_events.root

output_tar_file=$${output_name}.tar.gz
output_lhco_tmp_file=$${output_name}_tmp.lhco
output_lhco_file=$${output_name}.lhco
output_banner_file=$${run_output_dir}/run_01_$${run_name}_banner.txt

mg_debug_file=$${run_dir}/run_01_$${run_name}_debug.log
pythia_debug_file=$${run_dir}/$${run_name}_pythia8.log

# ??
# export HOME=$${job_dir}

# ------------
# Input files
# ------------
echo -e "\n>>> Preparing input files (run.mg5 and cards)\n"

tar -xzmf $${run_tar_file}
rm $${run_tar_file}
ls

# ---
# Run
# ---
cd $${job_dir}

echo -e "\n>>> Runnning MG+Pythia+Delphes\n"
echo "> $${mg_bin} $${run_mg_file}"
$${mg_bin} $${run_mg_file}

# Check if something failed (in that case save debug output and exit)
sc=$$?
if [ $$sc -ne 0 ] || [ -f "$${mg_debug_file}" ] ;  then
    echo "ERROR running MG. Exiting ..."
    cat $${mg_debug_file}
    cat $${pythia_debug_file}
    tar -cvzf $${output_tar_file} $${run_output_dir}/*
    exit 1
fi

# ------
# Output
# ------
cd $${job_dir}

echo -e "\n>>> Creating lhco output\n"

echo "> $${delphes_root2lhco_bin} $${run_output_root_file} $${output_lhco_tmp_file}"
$${delphes_root2lhco_bin} $${run_output_root_file} $${output_lhco_tmp_file}

# Merge lhco and banner (copied from run_delphes3)
echo -e "\n>>> Merging and compressing output\n"

if [ ! -e $${output_lhco_tmp_file} ]; then
    echo "ERROR: no lhco output file. Exiting ..."
    echo "> ls"
    ls
    echo "> ls "$${run_output_dir}
    ls $${run_output_dir}
    tar -cvzf $${output_tar_file} $${job_dir}/*
    exit 1
fi

sed -e "s/^/# /g" $${output_banner_file} > $$output_lhco_file
echo "" >> $${output_lhco_file}
cat $${output_lhco_tmp_file} >> $${output_lhco_file}
cp $${run_output_dir}/unweighted_events.lhe.gz $${job_dir}
tar -cvzf $${output_tar_file} $${output_lhco_file} unweighted_events.lhe.gz

echo -e "\nFinished OK, $$(date)
"""

template_run_mg = """${process}

output RUN
launch RUN

shower=Pythia8

${param_card}
${run_card}
${pythia_card}
${delphes_card}

done
"""

def replace_run_card_options(path, substitution_dict):

    regex = r"^\s*(?P<value>\S*)\s*=\s* (?P<option>iseed)\s*! (?P<comment>.*)$"

    run_card_txt = open(path).read()

    for option, value in substitution_dict.items():
        new_line = f'{value} = {option}'
        run_card_txt = re.sub(regex, run_card_txt, new_line, 0, re.MULTILINE)

    return run_card_txt

def main():

    parser = argparse.ArgumentParser(description='run_mg_pythia_delphes_with_condor.py')

    # Main required arguments
    parser.add_argument('-n', '--name',   required=True, help='Run name')
    #parser.add_argument('-i', '--input',  required=True, help='Directory with input cards and mg5 file')
    parser.add_argument('-o', '--output', required=True, help='Run/Output directory')
    parser.add_argument('-c', '--config', required=True, help='Configuration file')

    # Container image
    #parser.add_argument('--image', default='mg-pythia-delphes-latest.sif', help='Image name')

    # Files
    #parser.add_argument('--run_mg', help='MG run file. If not specified it will be guess from ".mg5" extension')
    #parser.add_argument('--run_card', help='Run card file. If not specified it will be guess from name cotaining "run_card"')

    # Number of events/jobs
    parser.add_argument('-e', '--nevents', type=int, help='Number of events for each job')
    parser.add_argument('-j', '--njobs',   default=1, type=int, help='Number of jobs')

    # Run card options to replace
    # parser.add_argument('--seed', default='random', help='Seed')
    # parser.add_argument('--ecms', default=13000, help='Center of mass energy in GeV. Default 13000 GeV')
    # parser.add_argument('--use_syst', action='store_true', help='Use systematics')
    # parser.add_argument('--pdf', default=13000, help='Center of mass energy in GeV. Default 13000 GeV')

    # Submission options
    parser.add_argument('--nosub', action='store_true', help='Prepare directory and files but don\' submit jobs')
    parser.add_argument('--oldskool', action='store_true', help='Use submisison files instead of python api')


    args = parser.parse_args()

    run_name = args.name
    #input_dir  = args.input_dir
    output_dir = args.output
    config_file = args.config

    #nevents = args.nevents
    #njobs = args.njobs

    # Read configuration
    with open(config_file) as f:
        config = yaml.safe_load(f)

    # Create working directory
    print(f'# Using working/output dir: {output_dir}')
    os.system(f'mkdir {output_dir}')

    # Copy cards to cards directory
    os.system(f'mkdir {output_dir}/inputs')


    if 'run_file' in config:
        os.system(f'cp {config["run_file"]} {output_dir}/inputs/run.mg5.orig')
    if 'run_card' in config:
        os.system(f'cp {config["run_card"]} {output_dir}/inputs/run_card.dat.orig')
    if 'param_card' in config:
        os.system(f'cp {config["param_card"]} {output_dir}/inputs/param_card.dat')
    if 'pythia_card' in config:
        os.system(f'cp {config["pythia_card"]} {output_dir}/inputs/pythia8_card.dat')
    if 'delphes_card' in config:
        os.system(f'cp {config["delphes_card"]} {output_dir}/inputs/delphes_card.dat')


    # for input_file in ['run_file', 'run_card', 'param_card', 'pythia_card', 'delphes_card']:
    #     # check if not given use default?
    #     if input_file in config:
    #         path = config[input_file]
    #         basename = os.path.basename(path)
    #         input_files_names[input_file] = basename
    #         Os.System(F'cp {Path} {Output_Dir}/Cards/{Basename}')


    # ----------
    #  Run card
    # ----------

    run_card_orig = f'{output_dir}/inputs/run_card.dat.orig'
    run_card_path = f'{output_dir}/inputs/run_card.dat'

    run_card_replace_options = {}
    if 'seed' in config:
        run_card_replace_options['iseed'] = config['seed']
    if 'ecm' in config:
        run_card_replace_options['ebeam1'] = float(config['ecm']) / 2
        run_card_replace_options['ebeam2'] = float(config['ecm']) / 2
    if 'use_syst' in config:
        run_card_replace_options['use_syst'] = config['use_syst']
    #if 'pdf' in config:
    #    'pdf':,

    nevents = None
    if args.nevents is not None:
        nevents = args.nevents
    elif 'nevents' in config:
        nevents = config['nevents']
    if nevents is not None:
            run_card_replace_options['nevents'] = nevents

    run_card_str = replace_run_card_options(run_card_orig, run_card_replace_options)

    with open(run_card_path, 'w') as f:
        f.write(run_card_str)


    # -------------
    #  MG Run file
    # -------------
    run_mg_path = f'{output_dir}/inputs/run.mg5'
    run_mg_orig = f'{output_dir}/inputs/run.mg5.orig'

    if 'run_file' in config and os.path.exists(run_mg_orig):
        process_str = open(run_mg_orig).read()
    elif 'process' in config:
        process_str = config['process']
    else:
        print('run.mg5 not found and process not defined. Exiting...')
        sys.exit(1)

    template = string.Template(template_run_mg)
    run_mg_str = template.substitute(
        {
            'process': process_str,
            'param_card': 'param_card.dat',
            'run_card': 'run_card.dat',
            'pythia_card': 'pythia8_card.dat',
            'delphes_card': 'delphes_card.dat'
        }
    )

    with open(run_mg_path, 'w') as f:
        f.write(run_mg_str)

    # ------------
    #  Run script
    # ------------
    script_path = f'{output_dir}/run_mg_pythia_delphes.sh'

    print(f'# Preparing run script: {script_path}')

    template = string.Template(template_run_script)
    run_script_replaced = template.substitute(
        {
            'run_name': run_name,
            'run_input': f'inputs_{run_name}.tar.gz',
            'run_mg_file': 'run.mg5',
            'output_name': f'output_{run_name}_$(Cluster)_$(Process)',
        }
    )

    with open(script_path, 'w') as f:
        f.write(run_script_replaced)





    # Docker/Apptainer image
    image_dir = '/mnt/R5/images'
    if config['image'] is None:
        print('No image was configured. Using latest: mg-pythia-delphes-latest.sif')
        container_image = f'{image_dir}/mg-pythia-delphes-latest.sif'
    else:
        container_image = f'{image_dir}/{config["image"]}'
        if not os.path.exists(container_image):
            print(f'Image {container_image} does not exist. Use one of the existint images:')
            print('     mg-pythia-delphes-3_3_2.sif')
            print('     mg-pythia-delphes-latest.sif')
            sys.exit(1)

    # Input Files (guess based on names)
    # print(f'# Using input dir: {input_dir}')

    # files = [ os.path.basename(f) for f in glob.glob(f'{input_dir}/*') ]

    # run_mg file
    # if args.run_mg is not None:
    #     file_run_mg = args.run_mg
    #     print(f'Using provided mg run file: {file_run_mg}')
    # else:
    #     file_run_mg = None
    #     for f in files:
    #         if f.endswith('.mg5'):
    #             if file_run_mg is not None:
    #                 print('More than one run files. Exiting...')
    #                 sys.exit(1)
    #             else:
    #                 print(f'Found mg run file: {f}')
    #                 file_run_mg = f

    # if file_run_mg is None:
    #     print(f'Input MG run file missing. Exiting...')
    #     sys.exit(1)


    # run card file
    # if args.run_card is not None:
    #     file_run_card = args.run_card
    #     print(f'Using provided run card file: {file_run_card}')
    # else:
    #     file_run_card = None
    #     for f in files:
    #         if 'run_card' in f:
    #             if file_run_card is not None:
    #                 print('More than one run card. Exiting...')
    #                 sys.exit(1)
    #             else:
    #                 print(f'Found run card: {f}')
    #                 file_run_card = f

    #     # If not given use default card
    #     if file_run_card is None:
    #         file_run


    # Prepare input files
    print(f'# Compressing input files here: {output_dir}/inputs_{run_name}.tar.gz')
    os.system(f'tar -cvzf {output_dir}/inputs_{run_name}.tar.gz -C {output_dir}/inputs .')

    # Copy run script
    script_path = f'{output_dir}/run_mg_pythia_delphes.sh'

    template = string.Template(template_run_script)
    run_script_replaced = template.substitute(
        {
            'run_name': run_name,
            'run_input': f'inputs_{run_name}.tar.gz',
            'run_mg_file': 'run.mg5',
            'output_name': f'output_{run_name}_$(Cluster)_$(Process)',
        }
    )

    print(f'# Preparing run script: {script_path}')
    with open(script_path, 'w') as f:
        f.write(run_script_replaced)

    os.chmod(script_path, 0o755)



    prev_dir = os.getcwd()
    # os.chdir(output_dir)

    if args.oldskool:

        # Job script
        template = string.Template(template_job)
        job_file_str = template.substitute(
            {
                'container_image': container_image,
                'executable': 'run_mg_pythia_delphes.sh',
                'run_input': f'inputs_{run_name}.tar.gz',
                'output_name': f'output_{run_name}',
                'njobs': args.njobs,
            }
        )

        # Save job.sub file
        job_file = f'job_{run_name}.sub'

        print(f'# Preparing job file: {output_dir}/{job_file}')
        with open(f'{output_dir}/{job_file}', 'w') as f:
            f.write(job_file_str)

        # Send job
        if not args.nosub:
            print(f'# Submitting job to condor ...')
            os.chdir(output_dir)
            os.system(f'condor_submit {job_file}')

    else:

        # Prepare and send jobs
        os.chdir(output_dir)

        job_dict = {
            'universe': 'container',
            'container_image': container_image,

            'executable': 'run_mg_pythia_delphes.sh',
            #'arguments':  = '$(run_name) $(run_input) $(run_mg_file) $(run_card_file) $(output_name)'

            'output':      'job_$(Cluster)_$(Process).out',
            'error':       'job_$(Cluster)_$(Process).err',
            'log':         'job_$(Cluster)_$(Process).log',

            'should_transfer_files': 'YES',
            'transfer_input_files': f'inputs_{run_name}.tar.gz',

            'transfer_output_files': f'{output_name}.tar.gz',
            'when_to_transfer_output': 'ON_EXIT',
        }

        job = htcondor.Submit(job_dict)

        print(job)

        schedd = htcondor.Schedd()
        submit_result = schedd.submit(job, count=args.njobs)

        print(submit_result.cluster())


if __name__ == '__main__':
    main()
