mg-pythia-delphes
=================

Dockerfile image with MadGraph + Pythia8 + Delphes and scripts to use with htcondor

Images available on: [franaln/mg-pythia-delphes](https://hub.docker.com/r/franaln/mg-pythia-delphes)


## Run with htcondor

You can run with condor using the default way of writing the submission file [[example0](examples/example0)]


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

### Configuration

- The first section of the configuration is called "run" and is required:

```
run:
    name: name of the run (required)
    image: image to use, mg-pythia-delphes-3_3_2 or mg-pythia-delphes-latest (default=mg-pythia-delphes-latest)
    nevents: number of events for each job (default=10000)
    njobs: number of jobs (default=1)
    outputs: list of outputs to save including [lhe, hepmc, hepmc0, root, lhco] (default = [lhe, lhco]). hepmc0 will save the hepmc output only for the first job
```

- Madgraph process and cards can be specified in different ways:

1. Using process + cards. For example [[example1](examples/example1)]:

```
process:
    import model sm

    generate p p > t t~ b b~

cards:
    run: run_card.dat
    param: param_card.dat
    pythia: pythia8_card.dat
    delphes: delphes_card_ATLAS.dat
```

2. Replacing 'process' with a proc_card [[example2](examples/example2)]:

```
cards:
    proc: proc_card.dat
    run: run_card.dat
    param: param_card.dat
    pythia: pythia8_card.dat
    delphes: delphes_card_ATLAS.dat
```

3. Using input_dir or list of input_files. They should include a "run.mg5" and all the needed files to run

    - If you have an input directory <INPUT_PATH> with all the files needed to run (run.mg5, cards, ...) you can put the following option in the config file (without process and cards options) [[example5](examples/example5)]:
    ```
    input_dir: <INPUT_PATH>
    ```

    - Or instead of using a directory you can list the input files like [[example4](examples/example4)]:
    ```
    input_files:
      - run.mg5
      - run_card.dat
      - param_card.dat
      - pythia8_card.dat
      - delphes_card_ATLAS.dat
    ```

* It is possible to use a list of param cards. For example to produce similar process with different parameters [[example3](examples/example3)]


- Other options can be speciffied using "options". This is optional and they will replace run_card values. The default values are the ones in the run card used. For example:
```
options:
    seed: interger to use random seed or the word "RANDOM" to choose a random seed for each job
    use_syst: True/False (to save systematics output, default=False)
    ecm: center of mass energy
```

- Expert options
    - mode: single/multi (default=single)
    - ncores: number of cores to use (default=all)

For example to run in multicore (use with care in condor):
```
expert:
    mode: multi
```


## Output

Merge lhe, root and lhco outputs after jobs finished:

`
merge_mg_pythia_delphes_output.sh [output_file] [input_files]
`

for example:

`
merge_mg_pythia_delphes_output.sh output_ttbb_merged.tar.gz output_ttbb_*.tar.gz
`