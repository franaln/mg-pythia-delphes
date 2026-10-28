# mg-pythia-delphes

Run MadGraph, optionally MadSpin, Pythia8 and Delphes from a YAML configuration,
locally with Docker/Apptainer or through HTCondor.

Container images: [franaln/mg-pythia-delphes](https://hub.docker.com/r/franaln/mg-pythia-delphes).
The host runner requires a POSIX host (Linux/macOS), Python 3.9+ and PyYAML. MadGraph and the other physics
tools run inside the selected container.

## Quick start

From the repository root, prepare the small full-chain example:

```bash
cd examples/example8_smoke_full
python3 ../../scripts/run_mg_pythia_delphes.py \
  -c config.yml -o output_test --run_mode condor --dry-run
```

Review `output_test/manifest.json` and the generated inputs, then submit them:

```bash
python3 ../../scripts/run_mg_pythia_delphes.py --execute-prepared output_test
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
| `run.mode` / CLI `--run_mode` | `local-docker` (default), `local-apptainer`, `condor`, `jupiter` (Condor alias) |
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
`expert.ncores` configures MadGraph; automatic matching of HTCondor resource
requests to it is not implemented yet.

Other supported input styles are external `cards.proc`, a mapping of parameter
cards under `cards.param`, `input_files`, `input_dir`, and a mapping of named
`input_dirs`. Complete input directories must contain `run.mg5` and its cards.
See the [example catalog](examples/README.md) for working configurations.

## Validation, preparation and execution

```bash
# Check configuration and required input files without writing output:
python3 scripts/run_mg_pythia_delphes.py -c config.yml --validate-only

# Prepare a production, without running containers or submitting jobs:
python3 scripts/run_mg_pythia_delphes.py -c config.yml -o production --dry-run

# Verify and execute the saved production, without needing the original YAML:
python3 scripts/run_mg_pythia_delphes.py --execute-prepared production
```

Preparation saves the chosen seeds, cards, wrappers, submit file and checksums.
Execution rejects missing/changed inputs and records an attempt before launching
work. A second execution of that production is blocked, including after an
interrupted or failed attempt. `--force` can replace unexecuted preparation
files, but cannot reset a recorded execution attempt.

A failed or ambiguous Condor submission can still have queued jobs: inspect
`submission` and `execution_attempt` in the manifest and the scheduler before
deciding what to do. Automatic retry/recovery is not implemented. Directly
calling `condor_submit` bypasses the runner's duplicate guard.

The guarded execution command requires a newly prepared schema-2 manifest.
Older productions remain readable, but must be prepared again in a **new
output directory** to use this command; do not regenerate already-running jobs.

## Check production status

From JUPITER, using the same user/scheduler environment used for submission:

```bash
python3 scripts/run_mg_pythia_delphes.py --status production
python3 scripts/run_mg_pythia_delphes.py --status production --json
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
python3 scripts/run_mg_pythia_delphes.py --validate-results production
python3 scripts/run_mg_pythia_delphes.py --validate-results production --json > results.json
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
python3 scripts/run_mg_pythia_delphes.py --validate-results production \
  --event-count nonempty --allow-nonpositive-cross-section --json
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

## Seeds and outputs

A positive `options.seed` starts a deterministic sequence across all jobs and
samples. `0`, `RANDOM`, or an omitted seed chooses a starting point during
preparation. Seeds are distinct within each production (range 1–30080), not
across separate productions. Independent Pythia/Delphes seed settings are not
overridden. A fixed container tag is recorded but not verified as an immutable
image digest.

Condor returns one `output_*.tar.gz` per job, plus scheduler stdout/stderr/logs.
Local results are under each job's `RUN/Events/` directory. A successful command
or submission does not itself validate the physics results.

To merge returned outputs:

```bash
scripts/merge_mg_pythia_delphes_output.sh output_merged.tar.gz output_sample_*.tar.gz
```

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

## Testing and development

See [examples/README.md](examples/README.md) for preparation checks and opt-in
real Condor/container tests. The full-chain smoke test checks LHE, HepMC and
Delphes outputs and requires host uproot in addition to PyYAML.

The development history, implementation details and next steps live in
[development.md](development.md).
