mg-pythia-delphes
=================

Dockerfile image with MadGraph + Pythia8 + Delphes and scripts to use with htcondor

Images available on: [franaln/mg-pythia-delphes](https://hub.docker.com/r/franaln/mg-pythia-delphes)


## Run with htcondor

`
run_mg_pythia_delphes_with_condor.py -c config.yml -o output_dir
`

where the configuration file should be in yaml format like the following example (more examples in example/):

```
run:
    name: ttbb
    image: mg-pythia-delphes-3_3_2
    nevents: 10000
    njobs: 10
    outputs: [lhco, lhe]

process: |
    import model sm

    generate p p > t t~ b b~

cards:
    run: run_card.dat
    param: param_card.dat
    pythia: pythia8_card.dat
    delphes: delphes_card_ATLAS.dat
```

## Output

`
merge_mg_pythia_delphes_output.sh [output_file] [input_files]
`