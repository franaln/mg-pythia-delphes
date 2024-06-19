mg-pythia-delphes
=================

Dockerfile image with MadGraph + Pythia8 + Delphes and scripts to use with htcondor

Images available on: [franaln/mg-pythia-delphes](https://hub.docker.com/r/franaln/mg-pythia-delphes)


## Run with htcondor

`
run_mg_pythia_delphes_with_condor.py -c config.yml -o output_dir
`

where the configuration file should be in yaml format like the following example (more examples in examples):

```
run:
    name: ttbb
    image: mg-pythia-delphes-3_3_2
    nevents: 10000
    njobs: 10
    outputs: [lhco, lhe]

process: |-
    import model sm

    generate p p > t t~ b b~

cards:
    run: run_card.dat
    param: param_card.dat
    pythia: pythia8_card.dat
    delphes: delphes_card_ATLAS.dat

options:
    seed: 0
    use_syst: False
```

- run:
    - name: name of the run (required)
    - image: image to use, mg-pythia-delphes-3_3_2 or mg-pythia-delphes-latest (default)
    - nevents: number of events for each job (default = 10K)
    - njobs: number of jobs (default = 1)
    - outputs: list of outputs to save including [lhe, hepmc, hepmc0, root, lhco] (default = [lhe, lhco]). hepmc0 will save the hepmc output only for the first job

- Madgraph process and cards can be specified in different ways:
    - Using process + cards (param cards can be a list)
    - Using only cards (including proc_card.dat)
    - Using input_dir or list of input_files. They should include a "run.mg5" and all the needed files to run

- options [optional] (these options will replace run_card values)
    - seed: random seed
    - use_syst: save systematic output
    - ecm: center of mass energy


## Check jobs status

`
condor_q
`

## Output

Merge lhe, root and lhco outputs after jobs finished:

`
merge_mg_pythia_delphes_output.sh [output_file] [input_files]
`

for example:

`
merge_mg_pythia_delphes_output.sh output_ttbb_merged.tar.gz output_ttbb_*.tar.gz
`