# mgpd: mg-pythia-delphes

Run MadGraph, optionally MadSpin, Pythia8 and Delphes from a YAML configuration,
locally with Docker/Apptainer or through HTCondor.

Container images: [franaln/mg-pythia-delphes](https://hub.docker.com/r/franaln/mg-pythia-delphes).
The host runner requires a POSIX host (Linux/macOS), Python 3.9+ and PyYAML. MadGraph and the other physics
tools run inside the selected container.

## Installation

From a checkout, create a user-owned environment and install the package:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install '.[validation]'
mgpd --version
```

The distribution is `mg-pythia-delphes`; its command and Python import are
`mgpd`. The minimal installation (`pip install .`) needs only PyYAML. The optional
`validation` extra adds uproot for Delphes ROOT checks. Installation does not
install MadGraph, container engines or HTCondor: those remain in the existing
containers/system setup. This package is not published to PyPI; install from the
checkout or a built wheel.

On JUPITER, the package is installed and you can directly run mgpd command from anywhere.


## Quick start

With the environment active, use the checkout's small full-chain example:

```bash
cd examples/example8_smoke_full
mgpd -c config.yml -o output_test --mode condor --dry-run
```

Review `output_test/manifest.json` and the generated inputs, then submit them:

```bash
mgpd --execute-prepared output_test
```

This example requests two jobs of 100 events. Submission must run on a machine
with access to your HTCondor scheduler, such as JUPITER. To prepare and execute
in a single command, omit `--dry-run` from the first command.

## Configuration

A minimal parton-level example, with a supplied run card:

```yaml
run:
  name: dielectron
  mode: condor
  image: mg-pythia-delphes-3.5.6
  nevents: 100
  njobs: 2
  outputs: [lhe, log]

process: |
  import model sm
  generate p p > e+ e-

cards:
  run: run_card.dat

options:
  seed: 1001
  use_syst: false

expert:
  mode: single
  ncores: 1
```

Input paths are relative to the **current working directory**, not to the YAML
file. `nevents` is per job; `njobs` is per sample. Local jobs run sequentially.

| Setting | Options |
| --- | --- |
| `run.mode` / CLI `--mode` | `local` (default; hostname-based), `local-docker`, `local-apptainer`, `condor`, `jupiter` (Condor alias) |
| `run.image` for Docker | For example `franaln/mg-pythia-delphes:3.5.6`; defaults to `:latest` |
| `run.image` for Apptainer | Path to a `.sif`, or an installed supported name under `/opt/images` |
| `run.image` for Condor | `mg-pythia-delphes-3.3.2`, `mg-pythia-delphes-3.5.6`, or `mg-pythia-delphes-latest`, under `/opt/images/*.sif` |
| `run.outputs` | Condor selections: `lhe`, `hepmc`, `hepmc0`, `root`, `lhco`, `log`, `all`; default `[lhe, lhco, log]` |
| `run.email` | Optional HTCondor notification address |
| `run.requirements` | Optional HTCondor requirements expression |
| `options.ecm` | Centre-of-mass energy in GeV, with equal beam energies |
| `options.extra` | Additional MG commands, as a string or list |

Add `cards.pythia` and `cards.delphes` to enable those stages, and
`cards.madspin` for decays. The param card is optional. For ROOT/LHCO output,
provide a Delphes configuration. `hepmc0` retains HepMC only from the first job
of each sample in Condor mode. Local mode retains program outputs directly.

Use `--mode local` (or YAML `run.mode: local`) to select a local backend:

```bash
mgpd -c config.yml -o production --mode local --dry-run
```

`local` is the default when neither YAML nor the CLI specifies a mode. It selects
Apptainer on hostname `jupiter`, and
Docker elsewhere. Explicit `local-apptainer`, `local-docker` and `condor` choices
are preserved.
Choose an image appropriate for the selected backend in the YAML; the alias does
not convert Docker image references into Apptainer paths.

Preparation saves the concrete backend. `--execute-prepared` and recovery keep
that backend even if the hostname later changes.

Other supported input styles are external `cards.proc`, a mapping of parameter
cards under `cards.param`, `input_files`, `input_dir`, and a mapping of named
`input_dirs`. Complete input directories must contain `run.mg5` and its cards.
See the [example catalog](examples/README.md) for working configurations.

## Validation, preparation and execution

```bash
# Check configuration and required input files without writing output:
mgpd -c config.yml --validate-only

# Prepare a production, without running containers or submitting jobs:
mgpd -c config.yml -o production --dry-run

# Verify and execute the saved production, without needing the original YAML:
mgpd --execute-prepared production
```

Preparation saves the chosen seeds, cards, wrappers, submit file and checksums.
Execution rejects missing/changed inputs and records an attempt before launching
work. A second execution of that production is blocked, including after an
interrupted or failed attempt. `--force` can replace unexecuted preparation
files, but cannot reset a recorded execution attempt.

A failed or ambiguous Condor submission can still have queued jobs: inspect
`submission` and `execution_attempt` in the manifest and the scheduler before
deciding what to do. Recovery requires explicit planning and execution as described below. Directly
calling `condor_submit` bypasses the runner's duplicate guard.


## Check production status

From JUPITER, using the same user/scheduler environment used for submission:

```bash
mgpd --status production
mgpd --status production --json
```

The runner saves the submitted IDs and checks the queue, then history for jobs
no longer queued. It reports individual states, hold reasons and exit details.
Missing records or exit information remain `unknown`. `completed` means the job
exited normally with code zero; output/physics validation is separate.

Status checks are read-only and do not reset duplicate protection. They return
exit code 1 for query errors, while job failures are reported in the status data.
For local Apptainer/Docker runs, status shows recorded execution state only.

## Validate results and summarize samples

After jobs finish, check saved outputs without submitting jobs or contacting the
scheduler:

```bash
mgpd --validate-results production
mgpd --validate-results production --json > results.json
```

The report checks requested artifacts per job and summarizes each sample's
validated event totals and per-job cross sections. Cross sections from replica
jobs are **not added together**. Condor archives are read without extracting
their paths; local Apptainer results are read from the saved job directories.
Missing jobs/files yield `incomplete`, corrupt/failed outputs yield `invalid`,
and all passing checks yield `valid`. The command returns 0 only for `valid`.
Missing optional uproot is reported as `unavailable`, not as corrupt ROOT data.

Default LHE checks require the planned event count and seed, a finite positive
cross section, and complete file/event closing tags. Different productions can
choose explicit policies:

```bash
mgpd --validate-results production --event-count nonempty --allow-nonpositive-cross-section --json
```

`--event-count` accepts `exact` (default), `nonempty` or `ignore`.
`--skip-seed-check` allows LHE without the planned seed; the policy is included
in the report. Nonfinite cross sections and structurally incomplete LHE remain
invalid even under relaxed policies. Delphes checks require uproot and a
nonempty `Delphes` tree; they inspect tree metadata, not every event basket.
HepMC checks require a nonempty fully decompressible gzip stream; these do not
validate the event physics. Select explicit output types: validation of
`outputs: [all]` is not implemented.

Results checks are read-only and do not modify the manifest, retry jobs or
approve the physics analysis. They report execution evidence from local state
or Condor job logs separately; known failed execution fails validation even if
some files are readable. Unknown execution evidence is not inferred as success.

## Recover selected failed or missing jobs

First inspect recovery eligibility without changing the production:

```bash
mgpd --plan-recovery production --json
```

Prepare a separate retry production, review it, then execute explicitly:

```bash
mgpd --prepare-recovery production -o retry-01
mgpd --execute-prepared retry-01
```

Add repeated `--job-id JOB_ID` options to preparation to select a subset of the
eligible manifest job IDs. By default it selects all eligible jobs. Validation
policy flags work for both planning and preparation; use the same flags for both.

Recovery preserves exact inputs, seeds and image references and leaves original
outputs untouched. It records a parent/child history and reserves selected jobs
against duplicate retries. Keep the child at its registered path and retain its
parent: execution rechecks ownership and eligibility before launching.

Valid results are preserved. Condor retries require terminal, identity-tagged
scheduler history evidence (wait for jobs to leave the queue); active, held, unknown, untagged legacy jobs and query errors
are blocked. Local retries require a recorded stopped production. Missing
validation dependencies and unsupported checks are not reasons to regenerate.
For an ambiguous submission, inspect the scheduler first; no override guesses
that retrying is safe. Recovery repeats frozen inputs; changed cards/code need
a new production instead.

If a retry fails, plan recovery from **that child directory**, not from the
original again. No original jobs are cancelled or released by these commands.
Retry summaries cover child jobs only; use one validated attempt per logical
job when combining datasets.

## Seeds

A positive `options.seed` starts a deterministic sequence across all jobs and
samples. `0`, `RANDOM`, or an omitted seed chooses a starting point during
preparation. Seeds are distinct within each production (range 1–30080), not
across separate productions. Independent Pythia/Delphes seed settings are not
overridden. A fixed container tag is recorded but not verified as an immutable
image digest.

## Outputs and merging

Condor returns one `output_*.tar.gz` per job, plus scheduler stdout/stderr/logs.
Local results are under each job's `RUN/Events/` directory. A successful command
or submission does not itself validate the physics results.

Merge a completed production directly, including any registered recoveries:

```bash
mgpd merge production/
```

For one sample, results go into `production/merged/`. For multiple samples (such
as a parameter-card scan), each is merged separately under
`production/merged/<sample>/`. No event data from different samples are combined.
One valid original attempt is preferred per logical job; otherwise the first
valid registered recovery descendant is selected. Only stopped attempts with
valid requested outputs are used. Condor requires recorded successful job-exit
evidence. Merging reads saved files and does not contact or submit to Condor.
Use the original production directory so recovery-family selection is complete.

Missing, invalid or unfinished jobs block merging by default. `--allow-partial`
explicitly permits an incomplete dataset and records every omitted job. A
`merge-report.json` records selected attempts, seeds, source-file checksums,
validation metrics, output checksums and incompleteness. Existing output
folders are never overwritten. The destination is published only after all
selected sample merges succeed and the merged LHE/ROOT event totals are checked.

Optional controls:

```bash
mgpd merge production/ --sample dielectron -o merged-dielectron/
mgpd merge production/ --allow-partial --backend local-apptainer --image image.sif
```

Repeat `--sample` to select multiple samples. Selecting one sample writes its
files directly into the output directory. Production merging validates inputs;
install `[validation]` for productions requesting ROOT output. Input-validation
policies `--event-count`, `--allow-nonpositive-cross-section` and `--skip-seed-check`
are also available and are recorded in the report. Cross sections from replicas
remain separate input measurements; merging does not certify physics normalization.

For expert usage, explicitly selected archives from **one sample and distinct
jobs** remain supported, with a required output path:

```bash
mgpd merge output_sample_*.tar.gz -o output_merged.tar.gz \
  --backend local-apptainer --image /mnt/R5/images/mg-pythia-delphes-3.5.6.sif
```

Use `--backend local-docker` outside JUPITER. If omitted, the backend defaults to
Apptainer on hostname `jupiter` and Docker elsewhere; `--image` overrides the
backend's default 3.5.6 image. Production merging reuses the saved image when
compatible with the selected backend. LHE uses MadGraph's merge tool, ROOT uses `hadd`,
and LHCO uses Delphes conversion tools inside the container. The host does not
need ROOT itself. Explicit archive merging does not require Python validation
dependencies; production-directory merging validates ROOT with uproot.

For explicit archive inputs, an output ending in `.tar.gz` creates an archive;
any other name creates a new directory. Production merging always creates a
directory. Existing destinations are rejected. `--extract-lhe` decompresses
merged LHE output; `--keep-all` retains extracted inputs under `all/` in either
output format. Command failures stop the merge without publishing output.
HepMC and pre-decay LHE files are not merged. Inputs must have the same supported
output formats; unsupported auxiliary files are retained only with `--keep-all`.

For the explicit archive interface, validate jobs first and select exactly one
validated attempt per logical job yourself. The archive interface cannot
establish physics-sample compatibility
or detect renamed duplicate attempts. Do not combine samples or interpret summed
cross sections from replicas as a physical production cross section.

The Python helper `scripts/merge_mg_pythia_delphes_output.py` retains its existing
arguments and delegates to the package. The shell helper retains its historical
`OUTPUT INPUT...` order and delegates to the same implementation.

## Models and custom cuts

For model additions requiring a writable directory, use an output under the
job's working directory, for example:

```text
import model sm-lepton_masses
add model taudecay_UFO --output=./models_tmp
import ./models_tmp-lepton_masses
```

For custom cuts, include `user_cuts.f` with the complete input directory and
reference it from the run card, for example `user_cuts.f = custom_fcts`.

## Developer documentation

Development setup, testing, releases and shared deployment are documented in
[README-dev.md](README-dev.md). An optional MCP adapter is being
developed separately; see
[the MCP adapter guide](packages/mg-pythia-delphes-mcp/README.md).
