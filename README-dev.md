# Developer and maintainer guide

For users, see [README.md](README.md) and the [example catalog](examples/README.md).
This guide contains development setup, automated and live tests, release checks,
and shared deployment. The implementation roadmap is maintained locally.

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

The public core API is exported by `src/mgpd/__init__.py`; the roadmap is
maintained locally.

After a Condor test, inspect the production without submitting more jobs:

```bash
python3 scripts/run_mg_pythia_delphes.py \
  --status test-results/condor-full-01/example8_smoke_full/production --json
```

Selective recovery can be tested separately on a disposable completed smoke
production. Use `--plan-recovery`, then `--prepare-recovery` to a new directory;
see [README.md](README.md#selective-recovery). Recovery
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

## Production-directory merge checks

The regression suite exercises real prepared plans and local/Condor artifact
readers, with only external merge tools faked. It covers multi-sample separation,
filtering, incomplete/unfinished jobs, explicit partial datasets, recovery chains,
original-attempt preference, ownership/input corruption, per-file provenance,
ROOT/LHE event totals and failure without publishing any samples.

The live merger driver above now also invokes the production-directory interface
and checks its report against input event totals, in addition to explicit-archive
and LHCO-only paths. On an existing disposable validated production:

```bash
mgpd merge production/ --backend local-apptainer
cat production/merged/merge-report.json
```

A single selected sample uses the output root; multiple selected samples use
separate subdirectories. The report belongs to the entire selection. For a retry
family, invoke this command on the original directory: only registered children
with matching identities, exact job definitions and frozen inputs are accepted.
Merging does not require a live scheduler query, but Condor attempts must have
recorded successful exit evidence. Use fresh destinations when repeating tests.

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
| `merging` | Merge explicit output archives and dispatch merge CLI |
| `production_merging` | Validate/select recovery attempts and merge per sample with provenance |
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
extra and run all regressions. Check `mgpd --version` and `python -m mgpd` outside the repository. Package refactoring does not
require rebuilding existing schema-2 productions or resetting attempt guards.

The example driver remains in the repository and runs with its Python
interpreter's package version. Examples are not installed with the wheel;
regular users supply their own YAML/cards. Use the intended environment
explicitly when testing releases or current development. The examples driver
uses `python -m mgpd`; the old standalone runner script has been removed.

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
  --wheel dist/mg_pythia_delphes-0.2.0-py3-none-any.whl \
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

## MCP adapter development

The adapter is a separate distribution in `packages/mg-pythia-delphes-mcp`.
Core supports Python 3.9 without the SDK; MCP requires Python 3.11+. For release
0.2.0 the adapter requires core `>=0.2.0,<0.3` and SDK `>=2.3,<3`.
User connection instructions and the end-to-end workflow are in the
[adapter README](packages/mg-pythia-delphes-mcp/README.md).

### Environment and automated tests

From the repository root:

```bash
python3.11 -m venv .venv-mcp
source .venv-mcp/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[validation]' -e packages/mg-pythia-delphes-mcp build
python -m unittest discover -s packages/mg-pythia-delphes-mcp/tests -v
```

Tests use real SDK stdio transport, core APIs and file fixtures, with fake
scheduler/container executables. Coverage includes preparation parity and seed
preservation, workspace checks, recovery ownership, duplicate/concurrent
requests, detached workers, reconnects, failed launches, killed workers, merge
selection/checksums, partial merging, event totals and provenance. These tests
never submit real jobs. Run the independent core suite in Python 3.9 without the
MCP SDK as well; running core tests alone does not exercise the adapter.

Build each distribution independently:

```bash
python -m build --outdir dist/core
python -m build packages/mg-pythia-delphes-mcp --outdir dist/mcp
```

Install both wheels in a fresh Python 3.11+ environment and run tests from
outside the checkout, so subprocesses import installed wheels:

```bash
python -m pip install 'dist/core/mg_pythia_delphes-0.2.0-py3-none-any.whl[validation]' \
  dist/mcp/mg_pythia_delphes_mcp-0.2.0-py3-none-any.whl
cd /tmp
/absolute/path/to/test-env/bin/python -m unittest discover \
  -s /absolute/path/to/repository/packages/mg-pythia-delphes-mcp/tests -v
```

Check dependency consistency with `python -m pip check`, version metadata and
wheel/source contents. The local `development.md` must remain untracked and
excluded from source distributions.

### JUPITER connectivity and live workflow checks

Run as the normal user with Condor access, never with sudo:

```bash
.venv-mcp/bin/python packages/mg-pythia-delphes-mcp/scripts/check_server.py \
  --workspace /home/USER/mg-pythia-delphes \
  --production production > mcp-check.json
```

This launches the installed server with a real SDK client, discovers tools and
inspects an existing completed production. It does not submit work or modify
production files. Omit `--production` for discovery only; add `--config` and
`--base-directory` to validate YAML. Nonzero exit indicates tool/query errors or
invalid, incomplete or unknown results. Preserve the JSON report for diagnosis.

For the live workflow use a disposable parton-level example, then a full-chain
example when needed. Follow the user guide's SSH workflow with all three action
flags enabled and check:

1. Preparation has the expected jobs/seeds/image, submits nothing and refuses
   an existing output directory.
2. Submission records scheduler IDs. Reconnect and recover the operation;
   repeated requests must not submit again.
3. Wait for tagged, successful completion and validate the outputs.
4. After completion, withhold one disposable job output into a backup location.
   Plan and prepare only that job; verify its seed/cards match, good outputs
   remain unchanged and duplicate recovery ownership is refused. Submit and
   validate the child. Restore the withheld output after the test.
5. Plan merging from the original production. Verify one attempt per job,
   sample separation and event totals. Reconnect while merging, repeat the
   request and check the same operation ID and provenance report. Use a fresh
   destination when previous tests already created `merged/`.

Keep manifests, scheduler IDs, operation logs and reports. Manual success
validates the environment used, not every fault scenario. Do not withhold or
remove actual analysis results just to exercise recovery.

The 0.2.0 release passed 114 core regressions on Python 3.9 without MCP and 27
adapter tests against installed wheels. The user also reported successful
manual JUPITER preparation, execution, validation, recovery and merging tests.

### Shared MCP deployment on JUPITER

Build and test wheels as a normal account. Install the tested core and adapter
wheels together in a new, administrator-owned Python 3.11+ environment. The core
`deploy_shared.py` helper installs the CLI distribution only; it does not deploy
the MCP adapter. For example, with wheel files staged at `/path/to/wheels`:

```bash
sudo /usr/bin/python3.11 -m venv /opt/venvs/mgpd-mcp-0.2.0
sudo /opt/venvs/mgpd-mcp-0.2.0/bin/python -m pip install \
  '/path/to/wheels/mg_pythia_delphes-0.2.0-py3-none-any.whl[validation]' \
  /path/to/wheels/mg_pythia_delphes_mcp-0.2.0-py3-none-any.whl
sudo /opt/venvs/mgpd-mcp-0.2.0/bin/python -m pip check
/opt/venvs/mgpd-mcp-0.2.0/bin/mgpd-mcp --version
```

Adjust the Python path to the available interpreter. The new directory must not
already exist. Never use editable installs or grant ordinary users write access
to shared environments. Installation uses sudo; MCP servers and jobs run as each
user. No shared daemon or HTTP service is needed: each SSH client starts its
own stdio server with that user's workspace and Condor identity.

After testing as a normal user, select the environment through a stable link:

```bash
sudo ln -s /opt/venvs/mgpd-mcp-0.2.0 /opt/venvs/mgpd-mcp-current.new
sudo mv -Tf /opt/venvs/mgpd-mcp-current.new /opt/venvs/mgpd-mcp-current
```

Users point their SSH MCP command at
`/opt/venvs/mgpd-mcp-current/bin/mgpd-mcp`; their workspace remains user-owned.
Choose the allowed capabilities in the client arguments. Keep previous
versioned environments for rollback and running workers. Rollback selects the
previous target using the same link replacement. Reconnect clients after an
upgrade; do not overwrite an environment used by existing servers/workers.

SSH must support noninteractive authentication (`BatchMode=yes`), and shell
startup must not print banners to stdout. Configure explicit SSH on the local
client; do not combine the documented entry with
`experimental_environment="remote"`. Ensure the remote noninteractive environment
can find Condor, Apptainer and other required executables. Do not start a second
server manually inside the SSH command's protocol stream.

### Protocol and operation internals

The `mgpd://capabilities` resource reports versions, workspace, enabled actions
and timeout. Requests run in isolated core workers; their cwd and diagnostic
stdout cannot interfere with the server. Diagnostics go to stderr; protocol
stdout contains MCP messages only.

Adapter responses contain matching text JSON and structured data:

- Success: `{"schema_version":1,"ok":true,"data":{...}}`.
- Failure: `{"schema_version":1,"ok":false,"error":{"code":"...","message":"..."}}`
  with the MCP error flag. Scheduler query failures retain observations in `data`.
- SDK argument-validation/unknown-tool failures use SDK-native errors.
- Invalid output is an observation (`data.state="invalid"`), not a protocol error.

Error codes include `PATH_OUTSIDE_WORKSPACE`, `NOT_FOUND`, `VALIDATION_ERROR`,
`QUERY_FAILED`, `TOOL_TIMEOUT`, `WORKER_FAILED`, `OUTPUT_EXISTS`,
`PREPARATION_UNKNOWN`, `EXECUTION_UNKNOWN` and `MERGE_UNKNOWN`. Listing `limit`
is 1–1000, default 100. Totals/truncation flags bound returned lists, not the
underlying validation work. Log pagination uses `log_offset`, `log_limit`
(maximum 16000 bytes) and `log_next_offset`.

`--timeout` defaults to 120 seconds. Read/preparation workers and subprocesses
are killed on timeout/disconnection; interrupted preparation may leave partial
files or recovery ownership. There is no automatic rollback/reset. Preparation
can invoke system `tar`, but never engines or scheduler submission. Recursively
checked inputs, output paths and registered parent/child paths must stay within
the workspace. These path checks are not an execution sandbox or a Unix-user
authorization boundary; physics cards/models remain user-supplied code.

Submission and merge workers detach from the request and survive MCP/SSH
shutdown. Submission reserves `.mgpd-mcp-operation/` in the production. Merge
reserves `.mgpd-mcp-operation-merge-<output-hash>/`, independently per destination.
Each reservation has atomic `operation.json`, `worker.lock` and `submission.log`
(the filename is shared by both operation kinds). Submitted is not physics
completion; completed merge means its output checks passed.

A queued operation may appear unknown briefly before acquiring its lock.
Interrupted workers, failed launches and ambiguous submissions remain unknown;
inspect operation/production records and queue/history before taking action.
An identical repeated request retrieves the existing reservation; it does not
restart work. Partial reservations without metadata also block launch. There is
no automatic retry/reset. Reboot, logout policies or administrative signals can
kill workers; core attempt guards remain in effect.

Merge checksums bind source bytes, recovery identity, selected jobs, omissions,
options and destination. The worker revalidates under core locks and verifies
copied bytes. Container failures do not publish staged results; interruption
during final publication can leave a destination requiring inspection. Planning
may read large ROOT/HepMC files: increase both server/client timeouts beforehand
when needed.

## Core workflow implementation notes

Schema-2 execution verifies frozen inputs and the immutable plan checksum, then
writes a durable attempt marker under a nonblocking POSIX lock before launching
work. `--force` cannot reset attempts. Earlier manifests may be readable but
cannot be safely executed without new preparation. Direct `condor_submit`
bypasses the runner's guards. Container tags are recorded, not verified as
immutable image digests.

Condor status checks exact IDs in queue then history and verifies identity tags.
Missing records or exit details remain unknown; successful execution requires
normal exit with code zero. Query errors produce CLI exit code 1; job failures
are observations in status data. Local status reports saved execution state.

LHE validation reads the complete gzip stream and checks counts, seed, finite
cross section and structure. ROOT validation requires uproot and a nonempty
Delphes tree; it reads metadata rather than every event basket. HepMC validation
checks gzip integrity and nonempty content, not physics. Known execution failure
invalidates readable outputs; missing validation dependencies do not justify
regeneration. Cross sections from replica jobs remain separate measurements.

Recovery clones frozen inputs/seeds, records parent/child ownership and rechecks
eligibility on execution. Terminal tagged Condor history is required; active,
held, unknown or untagged jobs and query errors are blocked. Keep registered paths
and parents available. Retry failed children from the child. There is no automatic
owner reassignment/reset for deleted or ambiguous children, or a distributed
exactly-once guarantee over manual scheduler actions.

Managed merging verifies registered descendants and selects the original valid
stopped attempt, otherwise the first valid descendant in parent-first order.
It validates copied snapshots and merged LHE/ROOT event totals, stages all samples
before publishing, and records provenance/omissions/hashes. Different samples
stay separate. Explicit-archive merging cannot establish sample compatibility
or detect renamed duplicates; users select distinct jobs themselves. MadGraph's
merge tool, ROOT `hadd` and Delphes conversion execute in containers. HepMC and
pre-decay LHE are not merged; unsupported auxiliary files require `--keep-all`.
The host does not need ROOT, but managed ROOT validation requires uproot.

For writable model additions, generate into the job working directory:

```text
import model sm-lepton_masses
add model taudecay_UFO --output=./models_tmp
import ./models_tmp-lepton_masses
```

For custom cuts, include `user_cuts.f` in the complete input directory and
reference `user_cuts.f = custom_fcts` from the run card. `expert.ncores` controls
MadGraph; automatic matching of Condor resource requests is not implemented.
