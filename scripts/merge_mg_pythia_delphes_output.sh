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

image=franaln/mg-pythia-delphes

# Merge lhe
count=`ls -1 ${tmpdir}/all/*unweighted_events.lhe.gz 2>/dev/null | wc -l`
if [ $count != 0 ] ; then
    echo "Merging lhe files"
    cmd_merge_lhe="source /setup_mg_pythia_delphes.sh && /mg_pythia_delphes/MG5_aMC/Template/LO/bin/internal/merge.pl share/all/*unweighted_events.lhe.gz share/merged/merged_unweighted_events.lhe.gz share/banner.txt"
    docker run --rm -u $UID:$GROUPS -v $PWD/$tmpdir:/home/docker/work/share $image "$cmd_merge_lhe"
fi

# Merge root
count=`ls -1 ${tmpdir}/all/*_delphes_events.root 2>/dev/null | wc -l`
if [ $count != 0 ] ; then
    echo "Merging root files"
    cmd_merge_root="source /setup_mg_pythia_delphes.sh && hadd share/merged/merged_delphes_events.root share/all/*_delphes_events.root"
    docker run --rm -u $UID:$GROUPS -v $PWD/$tmpdir:/home/docker/work/share $image "$cmd_merge_root"
fi

# Merge lhco
count=`ls -1 ${tmpdir}/all/*_delphes_events.lhco 2>/dev/null | wc -l`
if [ $count != 0 ] ; then
    echo "Merging lhco files"
    if [ ! -f $tmpdir/merged/merged_delphes_events.root ] ; then
        cmd_merge_lhco="source /setup_mg_pythia_delphes.sh && lhco2root share/merged_delphes_events.root share/all/*_delphes_events.lhco && root2lhco share/merged_delphes_events.root share/merged/merged_delphes_events.lhco"
    else
        cmd_merge_lhco="source /setup_mg_pythia_delphes.sh && root2lhco share/merged/merged_delphes_events.root share/merged/merged_delphes_events.lhco"
    fi
    docker run --rm -u $UID:$GROUPS -v $PWD/$tmpdir:/home/docker/work/share $image "$cmd_merge_lhco"
fi

tar -czf ${output_file} -C $tmpdir/merged .

rm -r $tmpdir