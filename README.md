# mg-pythia-delphes

Run MadGraph, optionally MadSpin, Pythia8 and Delphes from a YAML configuration,
locally with Docker/Apptainer or through HTCondor.

## Installation

Requires Linux/macOS and Python 3.9+. Install from the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install '.[validation]'
```

The `validation` extra enables Delphes ROOT checks. You also need Docker or
Apptainer and a [workflow container image](https://hub.docker.com/r/franaln/mg-pythia-delphes).
Condor execution requires access to an HTCondor scheduler.

On JUPITER, the package is already installed and you can directly run `mgpd` command.


## Quick start

From the repository root, prepare the small parton-level example:

```bash
cd examples/example7_parton_only
mgpd -c config.yml -o production --mode condor --dry-run
```

Review the generated inputs in `production/`, then submit from your Condor
submission host:

```bash
mgpd --execute-prepared production
mgpd --status production
```

After the jobs finish, validate and merge their outputs:

```bash
mgpd --validate-results production
mgpd merge production
```

Merged files and `merge-report.json` are written to `production/merged/`.
Use a new output directory for each production. To prepare and run in one step,
omit `--dry-run` from the preparation command.

See the [example catalog](examples/README.md) for Pythia, Delphes, MadSpin,
parameter scans and complete input directories.

## Configuration

A minimal configuration uses an existing run card:

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
```

Input paths are relative to the **current working directory**, not the YAML
file. `nevents` is per job; `njobs` is per sample. A positive `seed` gives a
repeatable sequence; omit it or use `RANDOM` for a random starting point.

| Setting | Usage |
| --- | --- |
| `run.mode` / `--mode` | `local`, `local-docker`, `local-apptainer`, or `condor` |
| `run.image` | Docker image reference, Apptainer `.sif` path, or supported Condor image name |
| `run.outputs` | Select `lhe`, `hepmc`, `hepmc0`, `root`, `lhco`, or `log` |
| `cards.pythia`, `cards.delphes` | Enable showering and detector simulation |
| `cards.madspin` | Enable MadSpin decays |
| `cards.param` | Parameter card, or a mapping of names to cards for a scan |
| `run.email`, `run.requirements` | Condor notification address and requirements expression |
| `options.ecm`, `options.extra` | Centre-of-mass energy in GeV and additional MadGraph commands |

`local` is the default: it selects Apptainer on hostname `jupiter`, Docker
elsewhere. Choose an image appropriate for that backend; local jobs run
sequentially. For example:

```bash
mgpd -c config.yml -o production-local --mode local-apptainer --dry-run
```

ROOT/LHCO output requires a Delphes card. `hepmc0` keeps HepMC only from the
first job of each sample in Condor mode. Use explicit output types when you
intend to validate results; `outputs: [all]` is not supported by validation.

## Status and validation

```bash
mgpd -c config.yml --validate-only
mgpd --status production --json
mgpd --validate-results production --json > results.json
```

Run status queries from the submission host with the same user account.
`completed` describes job execution; validate results separately. Result states
are `valid`, `incomplete`, `invalid`, or `unavailable` when a required dependency
is missing. Cross sections from repeated jobs are not added together.

Validation defaults to the planned event count and seed and a positive cross
section. Override these policies when appropriate:

```bash
mgpd --validate-results production --event-count nonempty \
  --allow-nonpositive-cross-section --skip-seed-check
```

`--event-count` accepts `exact`, `nonempty`, or `ignore`. Execution refuses changed
prepared inputs and repeated attempts. If submission has an unknown outcome,
inspect the scheduler before attempting recovery.

## Selective recovery

Plan recovery, then prepare only the eligible jobs you select:

```bash
mgpd --plan-recovery production --json
mgpd --prepare-recovery production -o retry-01 --job-id JOB_ID
mgpd --execute-prepared retry-01
mgpd --status retry-01
mgpd --validate-results retry-01
```

Repeat `--job-id` for multiple jobs; omit it to select all eligible jobs.
Use the same validation policies for planning and preparation. Recovery keeps
the original inputs and seeds and preserves good results. Keep parent and retry
directories in place. Active, held, or unknown Condor jobs cannot be recovered.
If a retry fails, plan the next recovery from that retry directory.

## Merging

Always merge from the original production directory to include its recoveries:

```bash
mgpd merge production
mgpd merge production --sample dielectron -o merged-dielectron
```

One valid attempt is selected per job. Different samples are kept separate,
under `merged/<sample>/` when several samples are selected. Repeat `--sample`
to choose several. Existing destinations are never overwritten.

Missing or invalid jobs block merging by default. Use `--allow-partial` explicitly
for an incomplete dataset; omissions are recorded in `merge-report.json`.
`--backend local-apptainer` or `--backend local-docker` and `--image` override
the merge container. `--extract-lhe` decompresses LHE output; `--keep-all`
retains the input files. LHE, ROOT and LHCO are merged; HepMC is not.

For manually selected archives from **one sample and distinct jobs**:

```bash
mgpd merge output_sample_*.tar.gz -o merged.tar.gz \
  --backend local-apptainer --image /path/to/image.sif
```

With explicit archives, select one validated attempt per job yourself. An output
ending in `.tar.gz` creates an archive; any other name creates a directory.

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

## Using an assistant

The optional [MCP adapter](packages/mg-pythia-delphes-mcp/README.md) provides
SSH configuration and an end-to-end example for the same Condor workflow.
