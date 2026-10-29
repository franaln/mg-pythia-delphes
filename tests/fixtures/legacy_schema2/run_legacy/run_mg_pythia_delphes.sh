#!/bin/bash

run_name=$1
run_dir=$2

echo -e ">>> Running run_mg_pythia_delphes.sh with the following configuration:
"
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

