#!/bin/bash

run_name=$1
run_dir=$2
# MGPD_LOCAL_OUTPUTS

echo -e ">>> Running run_mgpd_local.sh with the following configuration:\n"
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
        echo "WARNING: ROOT input for LHCO conversion is missing or empty: ${root_file}; keeping other outputs" >&2
    elif ! command -v root2lhco >/dev/null 2>&1; then
        echo "WARNING: root2lhco is unavailable; omitting LHCO and keeping other requested outputs" >&2
    elif ! root2lhco "${root_file}" "${temporary_lhco}"; then
        echo "WARNING: root2lhco failed; omitting LHCO and keeping other requested outputs" >&2
    elif [ ! -s "${temporary_lhco}" ]; then
        echo "WARNING: root2lhco produced no LHCO output; keeping other requested outputs" >&2
    else
        banner="${output_dir}/${output_dir_name}_${run_name}_banner.txt"
        staged_lhco="${final_lhco}.pending"
        if {
            if [ -f "${banner}" ]; then
                sed -e 's/^/# /g' "${banner}" && echo ""
            else
                true
            fi && cat "${temporary_lhco}"
        } > "${staged_lhco}" && mv -- "${staged_lhco}" "${final_lhco}"; then
            : # Complete LHCO published.
        else
            echo "WARNING: LHCO assembly failed; omitting LHCO and keeping other requested outputs" >&2
        fi
        rm -f -- "${staged_lhco}" || echo "WARNING: could not remove staged LHCO" >&2
    fi
    rm -f -- "${temporary_lhco}" || echo "WARNING: could not remove temporary LHCO" >&2
fi

ls "${output_dir}"

echo "Finished running MG+Pythia+Delphes, $(date)"
