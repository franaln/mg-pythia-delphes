"""Generated MG, Bash and HTCondor templates."""



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
# MGPD_LOCAL_OUTPUTS

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

if [ -d "${run_dir}/Events/run_01_decayed_1" ] ; then
    output_dir_name=run_01_decayed_1
elif  [ -d "${run_dir}/Events/run_01" ] ; then
    output_dir_name=run_01
fi

output_dir=${run_dir}/Events/${output_dir_name}


if [ -z "${output_dir_name}" ] || [ ! -d "${output_dir}" ]; then
    echo "ERROR: MadGraph output directory is missing" >&2
    exit 1
fi

# Delphes produces ROOT output; LHCO needs an explicit conversion in local mode.
if [[ " ${outputs:-} " == *" lhco "* ]]; then
    root_file="${output_dir}/${run_name}_delphes_events.root"
    temporary_lhco="${output_dir}/${run_name}_delphes_events_tmp.lhco"
    final_lhco="${output_dir}/${run_name}_delphes_events.lhco"
    echo "> Creating lhco output from ${root_file}"
    if [ ! -s "${root_file}" ]; then
        echo "ERROR: ROOT input for LHCO conversion is missing or empty: ${root_file}" >&2
        exit 1
    fi
    if ! command -v root2lhco >/dev/null 2>&1; then
        echo "ERROR: root2lhco is unavailable in the container environment" >&2
        exit 1
    fi
    if ! root2lhco "${root_file}" "${temporary_lhco}"; then
        echo "ERROR: root2lhco failed; preserving ROOT input and partial output" >&2
        exit 1
    fi
    if [ ! -s "${temporary_lhco}" ]; then
        echo "ERROR: root2lhco produced no LHCO output: ${temporary_lhco}" >&2
        exit 1
    fi
    banner="${output_dir}/${output_dir_name}_${run_name}_banner.txt"
    if [ -f "${banner}" ]; then
        sed -e 's/^/# /g' "${banner}" > "${final_lhco}" || exit 1
        echo "" >> "${final_lhco}"
        cat "${temporary_lhco}" >> "${final_lhco}" || exit 1
        rm "${temporary_lhco}" || exit 1
    else
        mv "${temporary_lhco}" "${final_lhco}" || exit 1
    fi
fi

ls "${output_dir}"

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
    mv "${output_dir}/${run_name}_pythia8_events.hepmc.gz" "${output_dir}/${output_file_hepmc}" || exit 1
    all_output_files+=(${output_file_hepmc})
fi

## LHE
if [[ "${outputs}" =~ "lhe" ]] ; then
    output_file_lhe=${output_name}_unweighted_events.lhe.gz

    mv "${output_dir}/unweighted_events.lhe.gz" "${output_dir}/${output_file_lhe}" || exit 1
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

    if ! root2lhco "${output_dir}/${output_file_root}" "${output_dir}/${output_file_tmp_lhco}"; then
        echo "ERROR: root2lhco failed; preserving ROOT input and partial output" >&2
        exit 1
    fi

    if [ ! -s "${output_dir}/${output_file_tmp_lhco}" ]; then
        echo "ERROR: no lhco output file. Exiting ..."
        ls ${output_dir}
        tar -czf ${output_file} -C ${job_dir} *
        exit 1
    fi

    # Merge lhco and banner (copied from run_delphes3)
    sed -e "s/^/# /g" "${output_dir}/${output_file_banner}" > "${output_dir}/${output_file_lhco}" || exit 1
    echo "" >> ${output_dir}/${output_file_lhco}
    cat "${output_dir}/${output_file_tmp_lhco}" >> "${output_dir}/${output_file_lhco}" || exit 1

    all_output_files+=(${output_file_lhco})
fi

if [[ "${outputs}" =~ "all" ]] ; then
    tar -czf "${output_file}" -C "${output_dir}" . || exit 1
else
    tar -cvzf "${output_file}" -C "${output_dir}" "${all_output_files[@]}" || exit 1
fi

echo "Finished OK, $(date)"
"""

