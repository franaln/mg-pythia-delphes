mg-pythia-delphes
=================

Dockerfile image with MadGraph + Pythia8 + Delphes and scripts to use with htcondor

Images available on: [franaln/mg-pythia-delphes](https://hub.docker.com/r/franaln/mg-pythia-delphes)


## Run with htcondor

`
run_mg_pythia_delphes_with_condor.py -c config.yml -o output_dir
`

where the configuration file should be in yaml format like the following example (more examples in example/test*.yml):

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
    - name: name of the run
    - image: image to use (mg-pythia-delphes-3_3_2 or mg-pythia-delphes-latest)
    - nevents: number of events for each job
    - njobs: number of jobs
    - outputs: list of outputs to save including [lhe, hepmc, hepmc0, root, lhco]

- Madgraph process and cards can be specified in different ways:
    - Using process + cards (param cards can be a list)
    - Using only cards (including proc_card.dat)
    - Using input_dir or list of input_files. They should include a "run.mg5" and all the needed files to run

- Options
    - seed: random seed
    - use_syst: save systematic output
    - ecm: center of mass energy


## Output

`
merge_mg_pythia_delphes_output.sh [output_file] [input_files]
`