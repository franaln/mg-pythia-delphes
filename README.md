mg-pythia-delphes
=================

(WIP) Dockerfile image with MadGraph + Pythia8 + Delphes (+ root) based on centos8

For the moment, to use in local cluster:
- Install MadGraph + Pythia8 + Delphes (and some other needed tools) in custom directory with install_mg.sh. It will also creates setup file, configuration and scripts
- To run with condor you can use `run_mg_pythia_delphes_with_condor.py`

`
run_mg_pythia_delphes_with_condor.py -n RUN_NAME -i INPUT_DIR -o OUTPUT_DIR --nevents 10000 --njobs 5
`
