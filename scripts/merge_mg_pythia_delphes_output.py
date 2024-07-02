#! /usr/bin/env python3

import os
import glob
import argparse


parser = argparse.ArgumentParser(description='merge_mg_pythia_delphes_output.py')

parser.add_argument('-i', '--inputs', nargs='+', required=True, help='Configuration file')
parser.add_argument('-o', '--output', required=True, help='Output directory')

parser.add_argument('-e', '--extract-lhe', action='store_true', help='Extract lhe.gz files')
parser.add_argument('-k', '--keep-all', action='store_true', help='Keep extracted job files')

args = parser.parse_args()

output_file = args.output
input_files = args.inputs

if output_file.endswith('.tar.gz'):
    tmpdir = 'tmp_output'
else:
    tmpdir = output_file


print("Running merge_mg_pythia_delphes_output with:")
print(f'output_file = {output_file}')
print(f'input_files = {input_files}')

# create tmp dir for uncompress files
try:
    os.mkdir(tmpdir)
    os.mkdir(f'{tmpdir}/all')
    os.mkdir(f'{tmpdir}/merged')
except FileExistsError:
    pass

# Uncompress output
for file in input_files:
     os.system(f'tar -xzf {file} -C {tmpdir}/all')



if os.environ['HOSTNAME'] == "jupiter.iflp.unlp.edu.ar":
    use_docker = False
    image = '/mnt/R5/images/mg-pythia-delphes-latest.sif'
else:
    use_docker = True
    image = 'franaln/mg-pythia-delphes:latest'

def run_cmd(cmd):
    if use_docker:
        os.system(f'docker run --rm -u $UID:$GROUPS -v $PWD/{tmpdir}:/home/docker/work/{tmpdir} {image} {cmd}')
    else:
        os.system(f'apptainer exec {image} /bin/bash -l -c "{cmd}"')

# Merge lhe
files_lhe = glob.glob(f'{tmpdir}/all/*unweighted_events.lhe.gz')
if len(files_lhe) > 0:

    print("Merging lhe files")

    cmd_merge_lhe = f"/mg_pythia_delphes/MG5_aMC/Template/LO/bin/internal/merge.pl {tmpdir}/all/*unweighted_events.lhe.gz {tmpdir}/merged/merged_unweighted_events.lhe.gz {tmpdir}/all/banner.txt"

    run_cmd(cmd_merge_lhe)

# Merge root
files_root = glob.glob(f'{tmpdir}/all/*_delphes_events.root')
if len(files_root) > 0:

    print("Merging root files")

    cmd_merge_root = f"hadd {tmpdir}/merged/merged_delphes_events.root {tmpdir}/all/*_delphes_events.root"

    run_cmd(cmd_merge_root)

# Merge lhco
files_lhco = glob.glob(f'{tmpdir}/all/*_delphes_events.lhco')
if len(files_lhco) > 0:

    print("Merging lhco files")

    if os.path.exists(f'{tmpdir}/merged/merged_delphes_events.root'):
        cmd_merge_lhco = f"lhco2root {tmpdir}/merged_delphes_events.root {tmpdir}/all/*_delphes_events.lhco && root2lhco {tmpdir}/merged_delphes_events.root {tmpdir}/merged/merged_delphes_events.lhco && rm {tmpdir}/merged_delphes_events.root"
    else:
        cmd_merge_lhco = f"root2lhco {tmpdir}/merged/merged_delphes_events.root {tmpdir}/merged/merged_delphes_events.lhco"

    run_cmd(cmd_merge_lhco)


if args.extract_lhe:
    if args.keep_all:
        lhe_gz_files = glob.glob(f'{tmpdir}/all/*.lhe.gz')
        for lhe in lhe_gz_files:
            os.system(f'gzip -d {lhe}')
    if os.path.exists(f'{tmpdir}/merged/merged_unweighted_events.lhe.gz'):
        os.system(f'gzip -d {tmpdir}/merged/merged_unweighted_events.lhe.gz')


if output_file.endswith('.tar.gz'):
    os.system(f'tar -czf {output_file} -C {tmpdir}/merged .')
    os.system(f'rm -r {tmpdir}')
else:
    os.system(f'mv {tmpdir}/merged/* {tmpdir}/')
    os.system(f'rm -r {tmpdir}/merged')

if not args.keep_all:
    os.system(f'rm -r {tmpdir}/all')