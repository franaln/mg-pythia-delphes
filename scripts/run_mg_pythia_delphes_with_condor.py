#! /usr/bin/env python3.8

import os
import sys
import glob
import json
import argparse
import string

template_job = """# MG+Pythia+Delphes - job submission file

# ------------------------------------------------------
# config
run_name      = ${run_name}
run_input     = ${run_input}
run_mg_file   = ${run_mg_file}
run_card_file = ${run_card_file}
output_name   = output_$$(run_name)_$$(Cluster)_$$(Process)
nevents       = ${nevents}
njobs         = ${njobs}
# ------------------------------------------------------

executable = run_mg.sh
arguments  = $$(run_name) $$(run_input) $$(run_mg_file) $$(run_card_file) $$(output_name) $$(nevents)

output      = job_$$(Cluster)_$$(Process).out
error       = job_$$(Cluster)_$$(Process).err
log         = job_$$(Cluster)_$$(Process).log

should_transfer_files = YES
transfer_input_files = $$(run_input)

transfer_output_files = $$(output_name).tar.gz
when_to_transfer_output = ON_EXIT

queue $$(njobs)

"""

template_run_script = """#!/bin/bash

# run_mg.sh {run_name} {run_input} {run_mg_file} {run_card_file} {output_name} {nevents}

run_name=$1
run_input=$2
run_mg_file=$3
run_card_file=$4
output_name=$5
nevents=$6

echo -e "\n>>> Running run_mg.sh with the following configuration:"
echo "date          = "$(date)
echo "hostname      = "${HOSTNAME}
echo "current dir   = "${PWD}
echo ""
echo "run_name      = "${run_name}
echo "run_input     = "${run_input}
echo "run_mg_file   = "${run_mg_file}
echo "run_card_file = "${run_card_file}
echo "output_name   = "${output_name}
echo "nevents       = "${nevents}
echo ""

# ------
# Config
# ------
job_dir=$PWD

mg_dir=/mnt/R5/hep_tools/mg_pythia_delphes
mg_bin=${mg_dir}/MG5_aMC/bin/mg5_aMC
delphes_root2lhco_bin=${mg_dir}/Delphes/root2lhco
mg_setup_file=${mg_dir}/setup_mg_pythia_delphes.sh

run_tar_file=${run_input}
run_dir=${job_dir}/RUN
run_output_dir=${run_dir}/Events/run_01
run_output_root_file=${run_output_dir}/${run_name}_delphes_events.root

output_tar_file=${output_name}.tar.gz
output_lhco_tmp_file=${output_name}_tmp.lhco
output_lhco_file=${output_name}.lhco
output_banner_file=${run_output_dir}/run_01_${run_name}_banner.txt

mg_debug_file=${run_dir}/run_01_${run_name}_debug.log
pythia_debug_file=${run_dir}/${run_name}_pythia8.log

# ??
export HOME=${job_dir}

# ------------
# Input files
# ------------
echo -e "\n>>> Preparing input files (run.mg5 and cards)\n"

tar -xzmf ${run_tar_file}
rm ${run_tar_file}

ls 

# -----
# Setup
# -----
echo -e "\n>>> Setup MG+Pythia+Delphes\n"
source ${mg_setup_file}

# ---
# Run
# ---
cd ${job_dir}

# Set variables (tag, nevents and random seed) in run card
seed=${RANDOM}

echo -e "\n>>> Modifying run_card with the following values:"
echo "run_name = "${run_name}
echo "nevents  = "${nevents}
echo "seed     = "${seed}
echo ""

sed -i "s|.* = run_tag.*| ${run_name} = run_tag|g" ${run_card_file}
sed -i "s|.* = nevents.*| ${nevents} = nevents|g" ${run_card_file}
sed -i "s|.* = iseed.*| ${seed} = iseed|g" ${run_card_file}

# Run MG
echo -e "\n>>> Runnning MG+Pythia+Delphes\n"
echo "> ${mg_bin} ${run_mg_file}"
${mg_bin} ${run_mg_file}

# Check if something failed (in that case save debug output and exit)
sc=$?
if [ $sc -ne 0 ] || [ -f "${mg_debug_file}" ] ;  then 
    echo "ERROR running MG. Exiting ..."
    cat ${mg_debug_file}
    tar -cvzf ${output_tar_file} ${run_output_dir}/*
    exit 1
fi

cat ${pythia_debug_file}

# ------
# Output
# ------
cd ${job_dir}

echo -e "\n>>> Creating lhco output\n"

echo "> ${delphes_root2lhco_bin} ${run_output_root_file} ${output_lhco_tmp_file}"
${delphes_root2lhco_bin} ${run_output_root_file} ${output_lhco_tmp_file}

# Merge lhco and banner (copied from run_delphes3)
echo -e "\n>>> Merging and compressing output\n"

if [ ! -e ${output_lhco_tmp_file} ]; then
    echo "ERROR: no lhco output file. Exiting ..."
    echo "> ls"
    ls
    echo "> ls "${run_output_dir}
    ls ${run_output_dir}
    tar -cvzf ${output_tar_file} ${job_dir}/*
    exit 1
fi

sed -e "s/^/# /g" ${output_banner_file} > ${output_lhco_file}
echo "" >> ${output_lhco_file}
cat ${output_lhco_tmp_file} >> ${output_lhco_file}
cp ${run_output_dir}/unweighted_events.lhe.gz ${job_dir}
tar -cvzf ${output_tar_file} ${output_lhco_file} unweighted_events.lhe.gz

echo -e "\nFinished OK, $(date)"
"""

def main():

    parser = argparse.ArgumentParser(description='run_mg_pythia_delphes_with_condor.py')

    parser.add_argument('-n', '--name', required=True, help='Run name')

    parser.add_argument('-i', '--input_dir', required=True, help='Directory with input cards and mg5 file')
    parser.add_argument('-o', '--output_dir', required=True, help='Run/Output directory')

    parser.add_argument('--run_mg', help='MG run file. If not specified it will be guess from ".mg5" extension')
    parser.add_argument('--run_card', help='Run card file. If not specified it will be guess from name cotaining "run_card"')

    parser.add_argument('-e', '--nevents', default=50_000, type=int, help='Number of events for each job')
    parser.add_argument('-j', '--njobs', default=1, type=int, help='Number of jobs')

    parser.add_argument('--nosub', action='store_true', help='Prepare directory and files but don\' submit jobs')

    args = parser.parse_args()

    run_name = args.name
    input_dir  = args.input_dir
    output_dir = args.output_dir
    nevents = args.nevents
    njobs = args.njobs

    # Input Files (guess based on names)
    print(f'# Using input dir: {input_dir}')
    
    files = [ os.path.basename(f) for f in glob.glob(f'{input_dir}/*') ]

    # run_mg file
    if args.run_mg is not None:
        file_run_mg = args.run_mg
        print(f'Using provided mg run file: {file_run_mg}')
    else:
        file_run_mg = None
        for f in files:
            if f.endswith('.mg5'):
                if file_run_mg is not None:
                    print('More than one run files. Exiting...')
                    sys.exit(1)
                else:
                    print(f'Found mg run file: {f}')
                    file_run_mg = f

        
    # run card file
    if args.run_card is not None:
        file_run_card = args.run_card
        print(f'Using provided run card file: {file_run_card}')
    else:
        file_run_card = None
        for f in files:
            if 'run_card' in f:
                if file_run_card is not None:
                    print('More than one run card. Exiting...')
                    sys.exit(1)
                else:
                    print(f'Found run card: {f}')
                    file_run_card = f

    if file_run_mg is None:
        print(f'Input MG run file missing. Exiting...')
        sys.exit(1)

    if file_run_card is None:
        print(f'Input run card file missing. Exiting...')
        sys.exit(1)



    # Create run directory
    print(f'# Using output dir: {output_dir}')
    os.system(f'mkdir {output_dir}')

    # Prepare input files
    print(f'# Compressing input files here: {output_dir}/input_files_{run_name}.tar.gz')
    os.system(f'tar -cvzf {output_dir}/input_files_{run_name}.tar.gz -C {input_dir} .')


    # Copy run script
    script_path = f'{output_dir}/run_mg.sh'

    print(f'# Preparing run script: {script_path}')
    with open(script_path, 'w') as f:
        f.write(template_run_script)

    os.chmod(script_path, 0o755)


    # Job script
    template = string.Template(template_job)
    job_file_str = template.substitute(
        {
            'run_name': run_name,
            'run_input': f'input_files_{run_name}.tar.gz',
            'run_mg_file': file_run_mg,
            'run_card_file': file_run_card,
            'nevents': nevents,
            'njobs': njobs,
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


if __name__ == '__main__':
    main()
