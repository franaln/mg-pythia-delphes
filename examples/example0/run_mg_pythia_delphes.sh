#!/bin/bash

# run_mg_pythia_delphes.sh {run_name} {run_input} {run_mg_file} {run_card_file} {output_name} {nevents}

if [[ ($# -lt 6) || ($# -gt 7) ]] ; then
    echo "run_mg_pythia_delphes.sh [run_name] [run_input] [run_mg_file] [run_card_file] [output_name] [nevents]"
    exit 1
fi

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

echo -e "\n>>> Setup MG+Pythia+Delphes\n"
source /setup_mg_pythia_delphes.sh

mg_dir=$MG_DIR
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


# ------------
# Input files
# ------------
echo -e "\n>>> Preparing input files (run.mg5 and cards)\n"

tar -xzmf ${run_tar_file}
rm ${run_tar_file}

ls

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
