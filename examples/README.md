# Executable examples

Run the YAML runner from the example's directory: input paths are relative to
that directory. The test driver handles this automatically and never edits the
source examples.

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

## Preparation checks anywhere

From the repository root, with Python 3.9+ and PyYAML:

```bash
python3 scripts/test_examples.py --list
python3 scripts/test_examples.py --output test-results/preparation-condor
python3 scripts/test_examples.py --mode local-docker --output test-results/preparation-docker
python3 scripts/test_examples.py --mode local-apptainer --output test-results/preparation-apptainer
```

These commands validate and prepare every catalogued YAML example, then check
sample/job counts, distinct seeds, concrete seeded commands, checksums,
packaged inputs and Bash syntax. They do not start containers or contact
HTCondor. They exercise preparation, not MadGraph command syntax or physics.
The test driver uses 100 events/job by default; `--events` overrides this.
It retains the documented job counts.

Outputs must go into a **new directory**. Each invocation saves `report.json`,
per-example logs, a test configuration and the prepared production. Catalogued
examples live in `catalog.json`; the regression suite checks that every YAML
example is registered, so newly added examples cannot silently escape testing.

## Real integration tests on JUPITER

Use a host Python environment with PyYAML and, for ROOT checks, uproot:

```bash
python3 -m venv .venv-tests
.venv-tests/bin/python -m pip install --upgrade pip
.venv-tests/bin/python -m pip install '.[validation]'
```

First test MadGraph and Condor without showering or detector simulation:

```bash
.venv-tests/bin/python scripts/test_examples.py \
  --execute --mode condor --example example7_parton_only \
  --output test-results/condor-parton-01 --timeout 1800
```

Then test the full chain:

```bash
.venv-tests/bin/python scripts/test_examples.py \
  --execute --mode condor --example example8_smoke_full \
  --output test-results/condor-full-01 --timeout 1800
```

`condor_submit` and `condor_wait` must be available in the submitting shell.
The default SM container must exist on the workers as
`/opt/images/mg-pythia-delphes-3.5.6.sif`. You can select another runner-supported
image name with `--image mg-pythia-delphes-latest`.

Add `--example example9_smoke_madspin` to also test the decayed output directory,
or select any other catalogued example explicitly. Execution never defaults to
all examples. `--max-jobs` defaults to 8 **per example**; this guard counts every
sample, not just jobs per sample. Heavy examples require the relevant UFO model
and may require a higher limit and timeout.

The driver:

1. Prepares and checks inputs, then executes them through the guarded
   `--execute-prepared` CLI. Both the runner and driver enforce job-count limits.
2. Uses the runner's exact scheduler-ID mapping in the production manifest,
   also saving `submitted_jobs.json` and submission output in `submit.log`.
3. Waits for each test job, with one overall Condor wait deadline per example.
4. Checks normal termination with exit code zero and returned archives.
5. Checks requested outputs: LHE event counts and actual seed, a positive finite
   cross section, nonempty HepMC/LHCO, readable log archives, and a readable
   nonempty Delphes tree for ROOT output.
6. Writes the report and returns a nonzero exit code if any example fails.

These are software/integrity checks, not a validation of cross sections against
reference calculations or detector physics. The driver does not implement the
`outputs: [all]` validation mode; select explicit output types for tests.

Timed-out or failed jobs are **left in the queue by default** for diagnosis.
Add `--remove-on-failure` to remove only the exact recorded test job IDs;
`condor_rm` must then also be available. Check `submitted_jobs.json`,
`submit.log`, and `condor_q` if submission itself was interrupted or its output
could not be parsed: the driver cannot clean up IDs it did not obtain.

## Real local container tests

For example:

```bash
.venv-tests/bin/python scripts/test_examples.py \
  --execute --mode local-docker --example example8_smoke_full \
  --output test-results/docker-full-01
```

For local Docker the test driver translates catalogued image names to
`franaln/mg-pythia-delphes:<version>`; for Apptainer it uses `/opt/images/*.sif`.
`--image` can override the Docker reference or Apptainer path. Local jobs run
sequentially, with `--timeout` applied to the whole local example. Local mode
validates the outputs that the runner creates; it does not synthesize Condor log archives or
convert ROOT to LHCO. Existing LHCO-requesting examples can therefore expose a
local-output feature gap. Prefer the new SM examples for initial local tests.

## Automated regression suite

```bash
.venv-tests/bin/python -B -m unittest discover -s tests -v
```

This runs all YAML examples through preparation for all three backends, tests
failure handling, and exercises the integration-test driver against a fake
scheduler and generated LHE archives. It never submits real jobs. Successful
mock tests do not establish that a real cluster/container run succeeds.

Output parsing and production validation are shared with the runner. The test
report includes `validation` with job details and sample summaries. Default
smoke policies remain strict. To inspect an existing result without another run:

```bash
python3 scripts/run_mg_pythia_delphes.py \
  --validate-results test-results/condor-full-01/example8_smoke_full/production --json
```

The implementation roadmap and internal API are documented in
[development.md](../development.md).

After a Condor test, inspect the production without submitting more jobs:

```bash
python3 scripts/run_mg_pythia_delphes.py \
  --status test-results/condor-full-01/example8_smoke_full/production --json
```

Selective recovery can be tested separately on a disposable completed smoke
production. Use `--plan-recovery`, then `--prepare-recovery` to a new directory;
see [README.md](../README.md#recover-selected-failed-or-missing-jobs). Recovery
is never enabled automatically by this test driver. The regression suite also
covers the complete recovery CLI with a fake scheduler, without real jobs.

For developing/testing current edits, install `-e ".[dev]"` instead of a fixed
release. The example driver uses the package installed in its Python environment;
see [development.md](../development.md#package-layout-and-release-checks).

## Live merging smoke test on JUPITER

Install the current development checkout in the environment used for testing:

```bash
python -m pip install -e '.[dev]'
```

Generate two small jobs with LHE, ROOT and LHCO outputs. Run from the repository
root; the example driver handles card paths. This actually launches simulations:

```bash
python scripts/test_examples.py --execute --mode local-apptainer \
  --example example11_merge_smoke \
  --image /mnt/R5/images/mg-pythia-delphes-3.5.6.sif \
  --output test-results/merge-production-01
```

Then run the real container merge and inspect the report:

```bash
python scripts/test_merging.py \
  --production test-results/merge-production-01/example11_merge_smoke/production \
  --backend local-apptainer \
  --image /mnt/R5/images/mg-pythia-delphes-3.5.6.sif \
  --output test-results/merge-check-01
cat test-results/merge-check-01/report.json
```

For Condor, generate with `--mode condor` and an appropriate Condor image reference
(e.g. `--image mg-pythia-delphes-3.5.6`), then pass that completed production to
the same merge checker. The checker itself never submits jobs. It copies selected
artifacts into its new test directory and leaves the production untouched.

The checker requires two valid jobs from one sample. It checks exact summed LHE,
ROOT and LHCO event counts, finite event weights, and a replica normalization
sanity check: the merged LHE cross section must remain within the input cross
section range, allowing rounding. It records the event-weighted reference and
input/merged weight statistics for inspection; it does not assume that raw event
weights must add across every LHE weight convention. This is a smoke test for this
positive unweighted SM example, not certification of arbitrary weighted samples.
It also tests LHCO-only merging through `lhco2root` and `root2lhco` separately.

`report.json`, `merge.log`, and `merge-lhco-only.log` retain results and failures.
Use fresh output paths for every test. A passing report is live evidence only for
the backend/image and input production actually exercised.
