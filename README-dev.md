# Developer and maintainer guide

For users, see [README.md](README.md) and the [example catalog](examples/README.md).
This guide contains development setup, automated and live tests, release checks,
and shared deployment. Architecture, decisions, change history and next steps
remain in [development.md](development.md).

## Development environment

Run commands from the repository root unless stated otherwise:

```bash
python3 -m venv .venv-dev
source .venv-dev/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

The editable installation reflects current source changes; the `dev` extra adds
build tools and uproot for ROOT validation. Physics programs and container engines
are external dependencies. Local regression tests use fake external tools and
never submit real jobs; commands marked `--execute` launch real work.

## Preparation checks anywhere

From the repository root, with Python 3.9+ and PyYAML:

```bash
python3 scripts/test_examples.py --list
python3 scripts/test_examples.py --mode condor --output test-results/preparation-condor
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
examples live in `examples/catalog.json`; the regression suite checks that every YAML
example is registered, so newly added examples cannot silently escape testing.

## Hostname-based local default

The runner accepts `--mode local` and YAML `run.mode: local`. When no mode is
specified, `local` is the default. It selects Apptainer if the short hostname is
`jupiter`, and Docker on any other host. Explicit concrete modes take precedence.
There are no site environment variables or configurable hostname aliases.

The example driver also defaults to `--mode local`. It resolves the hostname
before executable checks, image translation and execution, and records requested
and resolved modes. For example, on JUPITER:

```bash
python scripts/test_examples.py --mode local \
  --example example8_smoke_full --image /mnt/R5/images/mg-pythia-delphes-3.5.6.sif \
  --output test-results/preparation-local-01
```

This prepares only; add `--execute` to run the selected example. Use `--mode condor`
explicitly for Condor tests. Saved productions retain the concrete backend and
are not reinterpreted when executed on another host.

## Real integration tests on JUPITER

Use the development environment above. ROOT checks require the included uproot dependency.

First test MadGraph and Condor without showering or detector simulation:

```bash
python scripts/test_examples.py \
  --execute --mode condor --example example7_parton_only \
  --output test-results/condor-parton-01 --timeout 1800
```

Then test the full chain:

```bash
python scripts/test_examples.py \
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
python scripts/test_examples.py \
  --execute --mode local-docker --example example8_smoke_full \
  --output test-results/docker-full-01
```

For local Docker the test driver translates catalogued image names to
`franaln/mg-pythia-delphes:<version>`; for Apptainer it uses `/opt/images/*.sif`.
`--image` can override the Docker reference or Apptainer path. Local jobs run
sequentially, with `--timeout` applied to the whole local example. Local mode
validates native outputs and converts ROOT to LHCO when requested. Local logs
remain in the working directory rather than being packaged as Condor log archives.

## Automated regression suite

```bash
python -B -m unittest discover -s tests -v
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
[development.md](development.md).

After a Condor test, inspect the production without submitting more jobs:

```bash
python3 scripts/run_mg_pythia_delphes.py \
  --status test-results/condor-full-01/example8_smoke_full/production --json
```

Selective recovery can be tested separately on a disposable completed smoke
production. Use `--plan-recovery`, then `--prepare-recovery` to a new directory;
see [README.md](README.md#recover-selected-failed-or-missing-jobs). Recovery
is never enabled automatically by this test driver. The regression suite also
covers the complete recovery CLI with a fake scheduler, without real jobs.

For developing/testing current edits, install `-e ".[dev]"` instead of a fixed
release. The example driver uses the package installed in its Python environment;
see [package release checks](#package-layout-and-release-checks).

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

## Live saved-execution and recovery test on JUPITER

From the repository root, in an environment with the current package installed
and access to your normal Condor scheduler:

```bash
python scripts/test_condor_workflow.py --execute \
  --image mg-pythia-delphes-3.5.6 \
  --output test-results/condor-workflow-01
```

This submits **two parton-only SM jobs and one selective recovery job**, with 100
events per job (override with `--events`, limited to 1000). It:

1. Prepares the example, loads and executes its saved plan, then checks that a
   duplicate execution is rejected without changing the manifest.
2. Polls read-only `--status` until both jobs have tagged, successful completion
   records in history; validates results and checks no recovery is needed.
3. Moves one test-owned archive into `withheld/`, verifies the missing result is
   detected, and prepares a child containing exactly that job with its original
   seed, job configuration and image.
4. Checks recovery ownership prevents a second delegation, executes the saved
   child once, rejects duplicate execution, and validates the replacement output.
5. Checks the other parent's output is unchanged, restores the withheld archive,
   and validates the original production again.

Everything lives under a **new** output directory. Existing user productions are
never accepted as input. Parent and recovery submissions/IDs, CLI logs, scheduler
observations and failures are retained in `report.json` and numbered logs. Status
queries must leave manifests unchanged. Recovery preparation must not submit.

The per-production wait defaults to 1800 seconds (`--timeout`). Queue delays or
history finalization can require more time. Query errors, held/failed jobs and
unknown states never trigger speculative recovery. The test does not cancel jobs
on failure; inspect recorded IDs and manifests before any manual scheduler action.
The withheld archive is restored on ordinary errors/interrupts; after a process
kill, inspect the backup/original paths recorded in `report.json` before moving it.
Both original and recovered outputs remain available after success: they are
alternate attempts for the same logical job and must not both enter a merge.

This deliberately uses a cheap parton-only example to isolate scheduler/recovery
behavior. Pythia/Delphes and merging are exercised by the separate merge smoke
test. A fake-scheduler regression tests the driver itself; real JUPITER coverage
is established only by the report from this command.

If this test fails during initial parent-status observation, update the package
and resume the same test directory without resubmitting the parent:

```bash
python scripts/test_condor_workflow.py --execute --resume \
  --output test-results/condor-workflow-01
```

Resume is limited to a failed test with one recorded submitted parent, before
withholding or recovery preparation. It verifies the saved identity and scheduler
mapping and preserves prior logs. Failed/ambiguous child submissions require
inspection rather than automatic resubmission. The resumed parent keeps its
original image, configuration and seeds.

## Package layout and release checks

| Module | Responsibility |
| --- | --- |
| `config` | Load/validate YAML and resolve image references |
| `models`, `templates` | Production model, seed range and generated templates |
| `storage` | Manifests, checksums, path confinement and locks |
| `preparation` | Render sample/job inputs, package and freeze execution files |
| `execution` | Reload and execute guarded plans |
| `condor` | Parse IDs and query scheduler state |
| `results` | Inspect artifacts and summarize validated results |
| `recovery` | Plan/prepare/recheck parent-child recovery ownership |
| `merging` | Merge selected output archives through container tools |
| `cli` | Parse arguments and coordinate public operations |
| `compat` | Historical script function/template exports |

With the development environment active and the regression suite passing:

```bash
python -m build
```

Editable installs require a modern pip (PEP 660); upgrade pip before installation
on AlmaLinux environments with older bundled versions. Do not use editable
installs for the shared production environment.

`python -m build` creates an sdist and builds the wheel from that sdist. Before
releasing, install the wheel in a fresh environment and test outside the source
checkout. Confirm core commands work without uproot, then install the validation
extra and run all regressions. Check `mgpd --version`, `python -m mgpd`, and a
copy of the legacy wrapper outside the repository. Package refactoring does not
require rebuilding existing schema-2 productions or resetting attempt guards.

The example driver remains in the repository and runs with its Python
interpreter's package version. Examples are not installed with the wheel;
regular users supply their own YAML/cards. The legacy wrapper chooses an
installed package over the source tree; use the intended environment explicitly
when testing releases or current development.

## Shared JUPITER deployment

Run the deployment helper as the account that owns the shared installation.
By default it builds the **current checkout**, installs its version with the
`validation` extra into `/opt/venvs/mgpd-VERSION`, checks dependencies/imports and
`mgpd --version`, then atomically selects it through `mgpd-current`:

```bash
python scripts/deploy_shared.py --prefix /opt/venvs --bin-dir /opt/bin
```

The caller's Python needs `build` (included in the development extra). This does
not fetch Git changes or publish to PyPI. Update/review the checkout first. To
install a wheel already tested on JUPITER instead:

```bash
python scripts/deploy_shared.py \
  --wheel dist/mg_pythia_delphes-0.1.0-py3-none-any.whl \
  --prefix /opt/venvs --bin-dir /opt/bin
```

`--bin-dir` is optional and for the first installation only: it creates an `mgpd`
wrapper pointing through `mgpd-current`. Users need `/opt/bin` on `PATH`; they
can then run `mgpd` without activating the shared venv or changing their Python.
The helper refuses to overwrite an existing wrapper. On upgrades omit
`--bin-dir`; the existing wrapper follows the updated `mgpd-current` link.

An existing `mgpd-VERSION` is never reused or overwritten: increment the package
version before deploying another release. Installation/check failures leave the
current link unchanged and retain the new environment for diagnosis. The helper
prints the previous target for rollback. An alternate root such as a shared NFS
directory is supported via `--prefix`; use `--python /path/to/python3` to select
the interpreter. Python must support venv creation and package installation.

Keep environments administrator-owned, readable/executable by users and not
writable by ordinary accounts. Test the wheel with the documented local Apptainer
and Condor smoke tests before deployment; the helper's import/CLI checks do not
run physics simulations or submit jobs.

Do not overwrite an environment in use. Install the next release in another
versioned directory; change `mgpd-current` only after validation. Rollback points
it back to the previous environment. Existing processes retain their interpreter
and imported package; saved manifests, seeds and scheduler IDs stay in users'
production directories. Do not remove old environments while processes use them.
No shared environment was created or modified from this development session.
