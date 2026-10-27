mg-pythia-delphes
=================

Dockerfile image with MadGraph + Pythia8 + Delphes and scripts to use with htcondor

Images available on: [franaln/mg-pythia-delphes](https://hub.docker.com/r/franaln/mg-pythia-delphes)


## Run with htcondor

You can run with condor using the default way of writing the submission file [[example0_sub](examples/example0_sub)]

Or use the script with a config file:

`
run_mg_pythia_delphes.py -c config.yml -o output_dir
`

where the configuration file should be in yaml format like the following example (more examples in examples):

```
run:
    name: ttbb
    mode: condor
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
    xxxx: local/condor (default=local)
    image: image to use, mg-pythia-delphes-3_3_2 or mg-pythia-delphes-latest (default=mg-pythia-delphes-latest)
    nevents: number of events for each job (default=10000)
    njobs: number of jobs (default=1)
    email: xxx@gmail.com    (optional to get notifications)
    outputs: list of outputs to save including [lhe, hepmc, hepmc0, root, lhco] (default = [lhe, lhco]). hepmc0 will save the hepmc output only for the first job

```

- Madgraph process and cards can be specified in different ways:

1. Using process + cards. For example [[example1_process](examples/example1_process)]:

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

2. Replacing 'process' with a proc_card [[example2_proc_card](examples/example2_proc_card)]:

```
cards:
    proc: proc_card.dat
    run: run_card.dat
    param: param_card.dat
    pythia: pythia8_card.dat
    delphes: delphes_card_ATLAS.dat
```

3. Using input_dir or list of input_files. They should include a "run.mg5" and all the needed files to run

    - If you have an input directory <INPUT_PATH> with all the files needed to run (run.mg5, cards, ...) you can put the following option in the config file (without process and cards options) [[example5_input_dir](examples/example5_input_dir)]:
    ```
    input_dir: <INPUT_PATH>
    ```

    - Or instead of using a directory you can list the input files like [[example4_input_files](examples/example4_input_files)]:
    ```
    input_files:
      - run.mg5
      - run_card.dat
      - param_card.dat
      - pythia8_card.dat
      - delphes_card_ATLAS.dat
    ```

* It is possible to use a list of param cards. For example to produce similar process with different parameters [[example3_multiple_models](examples/example3_multiple_models)]

    ```
    cards:
      run: run_card.dat
      pythia: pythia8_card.dat
      delphes: delphes_card_ATLAS.dat
      param:
        model1: param_card1.dat
        model2: param_card2.dat
        model3: param_card3.dat
    ```

* Also a list of input_dirs:

    ```
    input_dirs:
      - name1: input_dir1
      - name2: input_dir2
      - name3: input_dir3
    ```

- Removing pythia or delphes card will remove Pythia/Delphes step

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





## Fix models addition and restriction permission

As you don't have permission to modify /models inside MG directory you need to use a tmp models directory:

```
import model sm-lepton_masses
add model taudecay_UFO --output=./models_tmp
import ./models_tmp-lepton_masses
```



## Using custom cuts

- Prepare a file "user_cuts.f" with the cuts in the same directory as the run.mg5
- You can use input parameters defined in the run card
- Then add to run card, for example:

```
## Custom cuts
user_cuts.f = custom_fcts
0 = min_mtata
20 = max_mtata
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

## Runner validation and failure reporting

Validate a configuration and its input files without creating output files,
starting containers, or submitting jobs:

```bash
python3 scripts/run_mg_pythia_delphes.py -c config.yml --validate-only
```

Relative input paths are resolved from the current working directory, as before.
Validation checks basic configuration structure, positive event/job counts,
supported modes/outputs, and required input files. It does not validate physics,
MadGraph syntax, container availability, or scheduler connectivity.

`--dry-run` remains a preparation operation: it writes files and, with `--force`,
can replace an existing output directory. Use `--validate-only` for inspection
without filesystem changes. Do not use `--force` on directories containing work
that must be preserved.

Container, packaging, and submission command failures now propagate as errors.
The generated wrappers stop when MadGraph returns a nonzero status and reject
missing event directories. These checks do not yet prove that every requested
physics output is complete or valid.

Run the lightweight regression suite (requires PyYAML):

```bash
python3 -B -m unittest discover -s tests -v
```

### Production manifests and seeds

Every prepared production writes `manifest.json` containing the configuration,
configured container reference, job/sample mapping, concrete MadGraph seeds,
requested events and outputs, and SHA-256 hashes of prepared input files.
Hashes describe the inputs before packaging; for Condor, sample inputs are
preserved inside `run_<sample>.tar.gz`. Each job's exact seeded `.mg5` file is
also retained separately and transferred to the worker.

Seed behavior:

- A positive `options.seed` starts a deterministic sequence across all samples
  and jobs, wrapping within 1–30080.
- `0`, `RANDOM`, or an omitted seed chooses a random starting point during
  preparation. The resulting seeds are recorded, not regenerated on workers.
- Seeds are distinct within a production; at most 30080 jobs are supported.
  Different productions can still overlap: use explicit nonoverlapping ranges
  when generating independent extensions of a sample.
- The seed controls MadGraph; independent Pythia/Delphes random-number settings
  in their cards are not overridden or tracked separately by this feature.

Local mode now honors `njobs`, running jobs sequentially in separate directories
when `njobs > 1`. HTCondor still shares the sample archive, with one seeded MG
command file per job. `hepmc0` selects HepMC output only for the first job of
each sample in Condor mode. Local output filtering is unchanged: all outputs
produced by the programs remain in their individual run directories.

The manifest initially has state `prepared`. Local execution updates it to
`running`, `failed`, or `execution_finished`; the latter means commands returned
success, not that physics outputs passed validation. Condor state tracking is
not implemented yet, so its manifest remains `prepared` after submission.
Container references such as `latest` are recorded but are not immutable image
digests. A manifest is provenance metadata, not a complete replay command or a
guarantee of bitwise reproducibility.

Structured scheduler tracking and the MCP server are the next stages.
