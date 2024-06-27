#! /usr/bin/bash

# merge_mg_pythia_delphes_output.sh output_name input_files

USAGE_MSG="usage: merge_mg_pythia_delphes_output.sh [output_name] [input_files]"

if [[ ($# -lt 2) ]] ; then
    echo $USAGE_MSG
    exit 1
fi

if [[ ($# -eq 1) && ( ($1 == "-h") || ($1 == "--help") ) ]] ; then
    echo "$USAGE_MSG"
    exit 0
fi

output_file=$1
shift
input_files=$@

tmpdir=tmp_merge

echo "Running merge_mg_pythia_delphes_output with:"
echo output_file = $output_file
echo input_files = $input_files

# create tmp dir for uncompress files
mkdir -p $tmpdir/all $tmpdir/merged/

for file in ${input_files} ; do
    tar -xzf $file -C $tmpdir/all
done


if [[ "$HOSTNAME" == "jupiter.iflp.unlp.edu.ar" ]] ; then
    use_docker=false
    image=/mnt/R5/images/mg-pythia-delphes-latest.sif
else
    use_docker=true
    image=franaln/mg-pythia-delphes:latest
fi


# Merge lhe
count=`ls -1 ${tmpdir}/all/*unweighted_events.lhe.gz 2>/dev/null | wc -l`
if [ $count != 0 ] ; then
    echo "Merging lhe files"
    cmd_merge_lhe="/mg_pythia_delphes/MG5_aMC/Template/LO/bin/internal/merge.pl tmp_merge/all/*unweighted_events.lhe.gz tmp_merge/merged/merged_unweighted_events.lhe.gz tmp_merge/banner.txt"

    if [ "$use_docker" = true ]  ; then
        docker run --rm -u $UID:$GROUPS -v $PWD/$tmpdir:/home/docker/work/tmp_merge "$image" "$cmd_merge_lhe"
    else
        apptainer exec "$image" $cmd_merge_lhe
    fi
fi

# Merge root
count=`ls -1 ${tmpdir}/all/*_delphes_events.root 2>/dev/null | wc -l`
if [ $count != 0 ] ; then
    echo "Merging root files"

    cmd_merge_root="hadd tmp_merge/merged/merged_delphes_events.root tmp_merge/all/*_delphes_events.root"

    if [ "$use_docker" = true ] ; then
        docker run --rm -u $UID:$GROUPS -v $PWD/$tmpdir:/home/docker/work/tmp_merge "$image" "$cmd_merge_root"
    else
        apptainer exec $image /bin/bash -l -c "$cmd_merge_root"
    fi
fi

# Merge lhco
count=`ls -1 ${tmpdir}/all/*_delphes_events.lhco 2>/dev/null | wc -l`
if [ $count != 0 ] ; then
    echo "Merging lhco files"
    if [ ! -f $tmpdir/merged/merged_delphes_events.root ] ; then
        cmd_merge_lhco="lhco2root tmp_merge/merged_delphes_events.root tmp_merge/all/*_delphes_events.lhco && root2lhco tmp_merge/merged_delphes_events.root tmp_merge/merged/merged_delphes_events.lhco"
    else
        cmd_merge_lhco="root2lhco tmp_merge/merged/merged_delphes_events.root tmp_merge/merged/merged_delphes_events.lhco"
    fi

    if [ "$use_docker" = true ] ; then
        docker run --rm -u $UID:$GROUPS -v $PWD/$tmpdir:/home/docker/work/tmp_merge "$image" "$cmd_merge_lhco"
    else
        apptainer exec "$image" /bin/bash -l -c "$cmd_merge_lhco"
    fi

fi

tar -czf ${output_file} -C $tmpdir/merged .

rm -r $tmpdir