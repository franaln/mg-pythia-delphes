#!/bin/bash

run_name=$1
input_file=$2
outputs=$3
output_name=$4

output_file=${output_name}.tar.gz

echo -e ">>> Running run_mgpd_condor.sh with the following configuration:\n"
echo "date          = "$(date)
echo "hostname      = "$HOSTNAME
echo "current dir   = "$PWD
echo ""
echo "run_name      = "${run_name}
echo "input_file    = "${input_file}
echo "outputs       = "${outputs}
echo "output_name   = "${output_name}
echo "output_file   = "${output_file}
echo "seed          = "${5:-}
echo ""

job_dir=$PWD

echo "> Preparing input files "
tar -xzmf "${input_file}" || exit 1
seed=${5:-}
if [[ ! "$seed" =~ ^[1-9][0-9]{0,4}$ ]] || (( seed > 30080 )); then
    echo "ERROR: seed must be an integer from 1 to 30080" >&2
    exit 1
fi
if [ "$(grep -Eic '^[[:space:]]*set[[:space:]]+iseed([[:space:]]|=)' run.mg5)" != 1 ] ||
   [ "$(grep -c '^set iseed = XXXX$' run.mg5)" != 1 ]; then
    echo "ERROR: expected exactly one seed command: set iseed = XXXX" >&2
    exit 1
fi
sed "s/^set iseed = XXXX$/set iseed = ${seed}/" run.mg5 > run.mg5.tmp || exit 1
mv -- run.mg5.tmp run.mg5 || exit 1
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

all_output_files=()

# A format failure must not prevent returning other files. Keep the original
# name if renaming fails; result validation still checks the requested contract.
prepare_output() {
    local source=$1 target=$2
    prepared_output=""
    if [ ! -f "${output_dir}/${source}" ]; then
        echo "WARNING: missing output ${source}; keeping other requested outputs" >&2
    elif mv -- "${output_dir}/${source}" "${output_dir}/${target}"; then
        prepared_output=$target
    else
        echo "WARNING: could not rename ${source}; returning original file" >&2
        if [ -f "${output_dir}/${source}" ]; then
            prepared_output=$source
        fi
    fi
}

root_file="${output_dir}/${run_name}_delphes_events.root"
if [[ "${outputs}" =~ root|lhco|all ]]; then
    prepare_output "${run_name}_delphes_events.root" "${output_name}_delphes_events.root"
    if [ -n "${prepared_output}" ]; then
        root_file="${output_dir}/${prepared_output}"
        if [[ "${outputs}" =~ "root" ]]; then
            all_output_files+=("${prepared_output}")
        fi
    fi
fi

## logs
if [[ "${outputs}" =~ "log" ]]; then
    output_file_logs=${output_name}_logs.tar
    if (set -o pipefail
        find "${run_dir}" -iname '*.log' -print0 |
            tar -cvf "${output_dir}/${output_file_logs}.pending" --null -T -
       ) && mv -- "${output_dir}/${output_file_logs}.pending" "${output_dir}/${output_file_logs}"; then
        all_output_files+=("${output_file_logs}")
    else
        echo "WARNING: could not prepare logs; keeping other requested outputs" >&2
    fi
    rm -f -- "${output_dir}/${output_file_logs}.pending" || echo "WARNING: could not remove temporary logs" >&2
fi

## HEPMC (Pythia8 output)
if [[ "${outputs}" =~ "hepmc" ]]; then
    prepare_output "${run_name}_pythia8_events.hepmc.gz" "${output_name}_pythia8_events.hepmc.gz"
    if [ -n "${prepared_output}" ]; then
        all_output_files+=("${prepared_output}")
    fi
fi

## LHE
if [[ "${outputs}" =~ "lhe" ]]; then
    prepare_output unweighted_events.lhe.gz "${output_name}_unweighted_events.lhe.gz"
    if [ -n "${prepared_output}" ]; then
        all_output_files+=("${prepared_output}")
    fi

    if [[ "${output_dir_name}" =~ "decayed" && -f "${run_dir}/Events/run_01/unweighted_events.lhe" ]]; then
        output_file_lhe_before=${output_name}_unweighted_events_before_decay.lhe.gz
        if gzip -c "${run_dir}/Events/run_01/unweighted_events.lhe" > "${output_dir}/${output_file_lhe_before}.pending" &&
           mv -- "${output_dir}/${output_file_lhe_before}.pending" "${output_dir}/${output_file_lhe_before}"; then
            all_output_files+=("${output_file_lhe_before}")
        else
            echo "WARNING: could not prepare pre-decay LHE; keeping other requested outputs" >&2
        fi
        rm -f -- "${output_dir}/${output_file_lhe_before}.pending" || echo "WARNING: could not remove temporary pre-decay LHE" >&2
    fi
fi

## LHCO (Delphes output)
if [[ "${outputs}" =~ "lhco" ]] ; then

    echo "> Creating lhco output "

    output_file_tmp_lhco=${output_name}_delphes_events_tmp.lhco
    output_file_lhco=${output_name}_delphes_events.lhco
    output_file_banner=${output_dir_name}_${run_name}_banner.txt

    if [ ! -s "${root_file}" ]; then
        echo "WARNING: ROOT input unavailable for LHCO; keeping other requested outputs" >&2
    elif ! root2lhco "${root_file}" "${output_dir}/${output_file_tmp_lhco}"; then
        echo "WARNING: root2lhco failed; omitting LHCO and keeping other requested outputs" >&2
    elif [ ! -s "${output_dir}/${output_file_tmp_lhco}" ]; then
        echo "WARNING: root2lhco produced no LHCO output; keeping other requested outputs" >&2
    else
        # Stage banner and events; publish only if every LHCO step succeeds.
        staged_lhco="${output_dir}/${output_file_lhco}.pending"
        if { sed -e "s/^/# /g" "${output_dir}/${output_file_banner}" &&
             echo "" &&
             cat "${output_dir}/${output_file_tmp_lhco}"; } > "${staged_lhco}" &&
           mv -- "${staged_lhco}" "${output_dir}/${output_file_lhco}"; then
            all_output_files+=("${output_file_lhco}")
        else
            echo "WARNING: LHCO assembly failed; omitting LHCO and keeping other requested outputs" >&2
        fi
        rm -f -- "${staged_lhco}" || echo "WARNING: could not remove staged LHCO" >&2
    fi
    rm -f -- "${output_dir}/${output_file_tmp_lhco}" || echo "WARNING: could not remove temporary LHCO" >&2
fi

if [[ "${outputs}" =~ "all" ]] ; then
    tar -czf "${output_file}" -C "${output_dir}" . || exit 1
elif [ "${#all_output_files[@]}" -eq 0 ]; then
    # All requested formats may be unavailable; return a valid empty archive.
    tar -czf "${output_file}" --files-from /dev/null || exit 1
else
    tar -cvzf "${output_file}" -C "${output_dir}" "${all_output_files[@]}" || exit 1
fi

echo "Finished OK, $(date)"
