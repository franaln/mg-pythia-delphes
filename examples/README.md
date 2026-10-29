# Executable examples

Run the YAML runner from the example's directory: input paths are relative to
that directory. Use the installed `mgpd` command with the supplied YAML/cards.

| Example | Purpose | Dependencies beyond the standard container |
| --- | --- | --- |
| `example0_sub` | Historical standalone submit file, not the YAML runner | Site-specific paths; manual only |
| `example1_process` | Inline process and individual cards | None |
| `example2_proc_card` | External process card; `hepmc0` output selection | None |
| `example3_multiple_models` | Three parameter cards / samples | None |
| `example4_input_files` | Explicit list of complete input files | None |
| `example5_input_dir` | Complete input directory | None |
| `example6_input_dirs` | Two named input directories, electron and muon pairs | None |
| `example7_parton_only` | Small SM sample, no shower/detector; optional param card omitted | None |
| `example8_smoke_full` | Small SM sample, Pythia + Delphes + first-job HepMC | None |
| `example9_smoke_madspin` | Small SM Z production, MadSpin decay to electrons, Pythia + Delphes | None |
| `example10_shower_only` | Pythia without Delphes | None |
| `example11_merge_smoke` | Two SM jobs with LHE, ROOT and LHCO for live merging checks | None |
| `example_with_madspin` | Original loop-induced Higgs-pair production with MadSpin | Custom UFO model |
| `example_hh_loop` | Original loop-induced Higgs-pair production | Custom UFO model |

The SM smoke examples request 100 events per job, two jobs per sample, one
core, and image version `3.5.6`. `example6_input_dirs` has two samples (four
jobs total). The original physics examples retain their configurations and may
be expensive even when the event count is reduced. In particular, the original
Higgs examples import `SM_LQHH_MU2-LAM3-LAM1-LAM2`; preparation does not establish
that this UFO model is installed in a selected container.

## Running an example

From the repository root, with the package installed:

```bash
cd examples/example8_smoke_full
mgpd -c config.yml -o production --mode condor --dry-run
mgpd --execute-prepared production
```

For a local run on JUPITER, use `--mode local-apptainer` and set `run.image`
to the installed Apptainer image path. Use `local-docker` on a Docker host.
See the [user guide](../README.md) for configuration, status, validation,
recovery and merging. Developer testing instructions are in
[README-dev.md](../README-dev.md).
