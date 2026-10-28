# Development plan and change log

Keep user-facing usage in `README.md`, example/test instructions in
`examples/README.md`, and architecture, progress and future work here. Update
this file when completing a stage or changing the plan.

## Next steps

| Stage | Status | Scope and acceptance criterion |
| --- | --- | --- |
| 1. Consolidate runner/tests | Implemented; live coverage partial | Configuration validation, failure reporting, examples and an LHE metadata regression. Record further actual JUPITER runs as they happen. |
| 2. Separate responsibilities | Complete | Reusable load/validate, prepare and execute functions; unchanged CLI/YAML. Preparation equivalence checked across all 36 example/backend combinations. |
| 3. Execute saved preparations safely | Implemented; Apptainer/Condor examples reported passing | Reload saved inputs without original source files; reject changes; lock concurrent callers; persist an execution attempt before launch; block automatic duplicate/retry. |
| 4. Track HTCondor jobs | Implemented; live status check pending | Parse returned job IDs and associate them with manifest jobs; query queue/history; distinguish pending/running/held/completed/failed/unknown after restarting the client. |
| 5. Validate and summarize outputs | Implemented; live validation check pending | Move the example driver's checks into reusable result functions; produce sample-level summaries and distinguish execution success from valid output. |
| 6. Recover failed jobs | Implemented; live recovery test pending | Retry only missing/failed jobs with preserved inputs/seeds and an explicit attempt history, without duplicating good outputs. |
| 7. Package and shared deployment | Implemented; JUPITER deployment pending | Installable package, mgpd CLI, optional ROOT validation, legacy wrapper, tested wheel and source distribution. |
| 8. Add a thin MCP interface | **Next** | Wrap the same validated preparation, execution, status and result functions; no duplicated workflow implementation. |

Cross-cutting work:

- **Planned: recovery-aware merge selection.** Select exactly one validated
  attempt per logical job across a production family. The packaged archive merge
  interface requires users to select inputs explicitly; it cannot establish
  sample compatibility or identify renamed duplicate attempts.

- **Planned: site-aware `local` alias.** Accept `--run_mode local` in the runner
  and `--mode local` in the example driver. On configured JUPITER hosts resolve
  it to `local-apptainer`; outside JUPITER resolve it to `local-docker`. Prefer an
  explicit site setting / configured hostname aliases over guessing from the
  presence of container executables (JUPITER has Podman, not Docker). Explicit
  concrete modes must always win. Resolve only during preparation and persist
  the concrete mode; never reinterpret `--execute-prepared` based on the host.
  Preserve the existing `jupiter` mode as a Condor alias. Add tests for JUPITER,
  other hosts, overrides and saved-plan portability before implementing this.

- Add YAML CPU/memory/disk requests and check consistency with MG core counts.
- Record runner revision and verified container identity when available.
- Keep statistical/physics checks configurable: positive cross sections and exact
  event counts are appropriate smoke-test assumptions, not universal requirements
  for all weighted/filtered productions.
- Avoid changing physics cards, broadening scans or introducing an agent
  framework as part of infrastructure refactors.

## Test coverage and remaining gaps

A passing unit-test count is not evidence that each backend produces every
requested artifact. Before the LHCO incident, all examples were prepared for all
backends, but successful local wrapper output generation was not exercised.
The fake scheduler wrote completed archives itself, and merge tests started from
existing files. Both bypassed the missing local conversion.

| Layer | Automated evidence | What it does not establish |
| --- | --- | --- |
| Preparation | All 13 catalog examples prepare for Docker, Apptainer and Condor | Physics programs execute or produce requested files |
| Worker output contract | Real preparation and Bash; 3 worker modes × 9 output selections × normal/decayed directory layouts; checks concrete artifacts and executed seed | Real MadSpin dynamics, ROOT validity, container setup or live scheduling |
| Worker failure contract | Missing/failed/silent/empty/partial LHCO converter across 3 modes; missing LHE/ROOT/HepMC in Condor | Every filesystem or external-tool failure |
| Results and recovery | Real format fixtures, identity/ownership checks, fake scheduler responses | Live Condor lifecycle and production-family physics consistency |
| Merging | Archive safety, backend command construction, checked failures and generated fixture outputs | Actual MG merge.pl/hadd/Delphes converter behavior in the image |
| Package | Wheel/source builds and entry-point/legacy checks recorded above | A shared JUPITER installation or current container contents |
| Live integration | Opt-in example and merger drivers save reports/logs | Passing status until the actual image/backend is run and recorded |

`tests/test_output_contract.py` fakes native MG output, not completed worker
outputs: LHCO is created only by the generated wrapper. Local modes use the real
saved-plan executor with only the container launch replaced; Condor runs the
prepared worker and inspects the actual produced tar archive, without a scheduler.
Its ROOT files are placeholders deliberately restricted to artifact-contract tests.

Validation: all 91 tests pass, including 54 successful worker combinations and
18 injected failure cases. Substituting the historical local wrapper in memory
caused 12 regression failures, confirming detection of the original LHCO bug.

The new failure cases exposed Condor accepting empty/partially failed LHCO
conversions and reporting success after missing LHE/HepMC collection; these paths
now return nonzero. The `all` archive path now archives the output directory itself
rather than expanding a wildcard against the worker directory.

For future changes, add a behavioral regression at the layer where the failure
occurs, execute affected worker contracts, then the full suite. For image/tool
changes also run the live JUPITER example and merge test. Record the image/backend,
commands and reports; keep pending live coverage explicit. Remaining extensions:

- Full input-source × output × worker combinations (current worker contracts use
  the inline-process source; other source modes have preparation coverage).
- Multi-job `hepmc0` worker execution (first-job selection has preparation tests).
- Real weighted/negative-weight LHE merge normalization, not just the positive SM
  replica smoke check; full ROOT branch integrity and LHCO object preservation.
- Live container/tool failures and Condor execution/recovery, using recorded reports.

## Changes implemented

### Live Condor lifecycle/recovery test preparation

- Added `scripts/test_condor_workflow.py --execute`: isolated two-job parton-only
  production plus a one-job recovery. Runs saved-plan CLI execution, duplicate
  guards, read-only tagged status polling, validation, selection and ownership.
- Temporarily withholds one test-owned output, preserves a checksum of the good
  job, verifies child job/seed/image identity, validates recovery and restores the
  original archive on errors/interrupts. No existing productions are accepted.
- Reports retain scheduler IDs, status observations and command logs. No automatic
  cancellation or unsafe fallback when status is unknown. Usage and limits are in
  `examples/README.md`. Real JUPITER verification remains pending (SSH blocked).
- Validation: all 94 local tests pass; no real scheduler jobs were submitted.
- Fake-scheduler integration covers success, failed recovery submission with
  backup restoration, query failure without recovery, duplicate guards and
  refusal to overwrite an existing test directory.


### Local LHCO generation fix

- The first live local merge production reported missing LHCO. Inspection found
  that ROOT-to-LHCO conversion existed only in the Condor wrapper; local wrappers
  stopped after MadGraph/Pythia/Delphes even when LHCO was requested.
- Local preparation now freezes the requested output list in each wrapper and
  explicitly runs `root2lhco` when LHCO is requested. It checks converter presence,
  exit status and nonempty output, retains the ROOT input, and prepends the MG
  banner when available. Standard and MadSpin output directories are supported.
- Bash regressions execute generated wrappers with fake MG/converter executables,
  covering successful conversion, no-LHCO selection, missing converter, nonzero
  exit, missing/empty outputs and paths containing spaces.
- All 88 local tests pass; real container conversion remains to be retested.
- Existing prepared productions keep their frozen scripts; prepare into a fresh
  directory to use this fix. No original production or saved guard was reset.


### Live merging test preparation

- Added `example11_merge_smoke`: two SM electron-pair jobs with LHE, ROOT and
  LHCO output, and a `scripts/test_merging.py` live integration checker.
- The checker supports completed local/Condor productions, validates inputs,
  checks summed event counts and a replica cross-section sanity bound, records
  finite weight statistics and exercises LHCO-only conversion separately.
- Reports, input snapshots and command logs are written to a fresh test directory;
  original productions remain untouched. Commands are in `examples/README.md`.
- Local validation: all 85 tests pass, including preparation of all 13 examples
  for three backends and regressions for event counting/normalization/reporting.
- SSH to JUPITER from this workspace was blocked (`Operation not permitted`);
  no remote simulation or merge was performed. Live verification remains pending.

### Packaged output merging

- Added reusable `mgpd.merge_outputs` and `mgpd merge INPUT... -o OUTPUT`.
  Legacy Python and shell helpers delegate to the same implementation.
- Explicit Docker/Apptainer backend and image overrides; retained JUPITER default.
- LHE uses MG merge.pl, ROOT uses hadd, and LHCO uses Delphes converters.
  Fixed inverted LHCO-only conversion logic in the old Python helper.
- Checked command return codes and output existence; private temporary workspaces,
  safe regular-file archive extraction, duplicate name/path and partial-format
  rejection, and no overwriting existing destinations. Inputs remain untouched.
- Directory/archive output supports decompressed LHE and retained input files.
  HepMC/pre-decay LHE merging and automatic recovery-family selection are deferred.
- Validation: all 80 tests pass; wheel and source distribution build successfully,
  and all 9 merger tests pass against the installed wheel outside the checkout.
- Container orchestration is covered by mocked regressions; real LHE/ROOT/LHCO
  merges with the JUPITER image still need live verification. No physics tools or
  scheduler jobs were run during these tests.


### Baseline validation and provenance

- `db0ee58`: configuration validation without side effects, checked external
  commands, wrapper failure reporting and optional Delphes-output handling.
- `0483b17`: per-job concrete seeds, production manifests, local `njobs` support.
- `c280e88`: executable example catalog and opt-in integration driver; SM examples
  for multiple input directories, parton-only, shower-only, full-chain and MadSpin.
- `b8791f5`: parse LHE init records separately from optional XML metadata such as
  `<generator>`; retain checks for genuinely missing/malformed process records.

The user reported that real Condor tests pass after the LHE parser fix. Exact
per-example live coverage was not recorded; this is not evidence that every
example, backend or subsequent change has passed on the real cluster.

### Stage 2: reusable runner (`e0ff753`)

- Reduced `main()` to argument handling and orchestration.
- Extracted input loading/validation, image resolution, sample rendering, job
  planning, packaging, wrappers, submit-file generation and execution.
- Added `PreparedProduction`, local command construction and manifest reading.
- Preparation does not mutate caller configuration or change cwd.
- Compared pre/post-refactor files, archive contents and manifests for all 12
  YAML examples and three backends, ignoring creation timestamps: equivalent.
- 22 regression tests passed at that stage; no new real jobs were submitted.

### Stage 3: saved execution and documentation split

- Added `--execute-prepared DIRECTORY` for local Docker/Apptainer and Condor.
- Added schema-2 manifest metadata: production ID, relative execution descriptor,
  SHA-256 of final execution inputs and a checksum of immutable plan metadata.
  Final snapshots include shared archives, seeded commands, wrappers and `.sub`.
- Loader reconstructs execution paths after moving a production and does not
  re-read the original YAML/cards, resolve another image or assign seeds.
- Added an advisory file lock and persisted an attempt marker before any launch.
  Reloading authoritative state inside the lock rejects stale callers, repeated
  execution, and automatic retry after an ambiguous submission or local failure.
- `--force` cannot erase a recorded attempt. A new production has its own ID;
  stale in-memory objects cannot execute a replacement at the same path.
- Condor submission uses `-terse` and a job-count guard, records raw stdout/stderr,
  and distinguishes submission success from unknown outcome. Structured ID
  mapping and queue/history queries were added in stage 4 below.
- Integration driver now goes through the guarded CLI instead of submitting or
  launching containers directly. Local timeout applies to the whole example.
- Moved this history, roadmap and internal API notes out of the README.
- 31 regression tests pass locally, including the 36 example/backend preparation
  combinations and guarded execution against a fake scheduler. New real Condor
  execution of this stage remains pending.


### Stage 4: scheduler IDs and read-only status

- Save a one-to-one ordered mapping of planned jobs to `cluster.proc` IDs after
  parsing `condor_submit -terse`; reject duplicates, malformed ranges or a count
  mismatch. Save raw stdout/stderr before parsing so ambiguous submissions remain
  diagnosable and the once-only guard still blocks retry.
- Add `MGProductionId` and `MGJobId` ClassAds to newly prepared submit files.
  Queue/history queries check these identities as well as the recorded IDs.
- Add `--status DIRECTORY` and `--status DIRECTORY --json`, backed by the
  reusable `production_status` function. Query queue first, then history for
  missing IDs, in bounded batches with timeouts.
- Report pending/running/held/transferring/suspended/removed, completed or failed
  according to exit information. Missing ads or exit information stay unknown;
  nonzero exits and signals fail. Hold/remove reasons are retained.
- Queries are read-only: no submissions, input mutation, attempt resets or
  changes to the manifest. Return scheduler observations separately from the
  last recorded execution/submission state.
- Already-submitted schema-2 manifests with only raw terse stdout can be queried
  without migration; identity checks are explicitly unavailable for these legacy
  submissions. No mapping is guessed for partial/ambiguous submissions.
- The integration driver now uses the runner's saved ID mapping, removing its
  duplicate terse-output parser. Regression tests cover submission, a fresh
  status CLI process, and history output using a fake scheduler.
- 40 regression tests pass locally, including fake-scheduler submission and a
  fresh status CLI process. No live scheduler queries were performed here.
- The user reports successful Apptainer and Condor example tests on JUPITER.
  Manual two-command execution/duplicate protection was not separately confirmed.
  Real queue/history status queries for this stage remain to be tested there.

### Stage 5: reusable output validation and sample summaries

- Moved LHE/init parsing and artifact checks from the example driver into the
  runner; the driver uses the same implementation and includes the production
  report in `report.json`, avoiding duplicate reads of large outputs.
- Added `validate_production` and `--validate-results DIRECTORY [--json]`.
  Validation reads actual artifacts without contacting Condor or modifying the
  execution manifest/guard. Reports include policy, per-output checks, job
  states, execution evidence, and per-sample event totals and cross sections.
- Replica cross sections remain separate per-job measurements; no sum or
  combined uncertainty/estimate is invented.
- Added explicit LHE policies for exact/nonempty/ignored event counts,
  nonpositive finite cross sections and optional seed matching. Missing closing
  tags, nonfinite records, corrupt gzip, and mismatches under the selected policy
  remain failures. Policy overrides are report metadata, not manifest edits.
- Handle missing artifacts (`missing`) and missing optional validation libraries
  (`unavailable`) separately from invalid data. Preserve metrics from readable
  files when another requested artifact is missing or bad; sample validated
  totals count only valid jobs.
- Condor outputs are opened using the recorded job mapping (with strict legacy
  raw-output fallback), never guessed from a broad glob. Tar contents are read
  without extracting paths; duplicate basenames are rejected. Local results use
  saved job directories, including MadSpin's decayed directory when present.
- Separate `outputs_state` from `execution_state`; known failed execution marks
  the job invalid even if readable files remain. Unknown execution evidence is
  reported as unknown, not promoted to scheduler completion.
- HepMC streams are decompressed through EOF for CRC/truncation checks. ROOT
  validation checks tree metadata/positive entries, not a full branch/basket
  scan. Explicit output selections are required; `all` validation remains a gap.
- 53 regression tests pass, including real ROOT fixtures, truncated LHE/gzip,
  missing/corrupt artifacts, policy changes, sample summaries and fake-scheduler
  integration. No new real simulation or JUPITER runs were performed here.

### Stage 6: selective recovery with parent/child ownership

- Added read-only `plan_recovery` / `--plan-recovery DIRECTORY [--json]`.
  Combine reusable output checks with terminal scheduler evidence; never infer
  that a missing artifact means its job stopped. Valid jobs are preserved.
- Added `prepare_recovery` / `--prepare-recovery DIRECTORY -o NEW_DIRECTORY`,
  optionally selecting repeated `--job-id` values. Preparation does not execute.
  It clones frozen inputs, wrappers, seeds and image references for only eligible
  jobs, rendering a new Condor submit file with the child's production identity.
- Condor recovery requires identity-tagged terminal history evidence and successful
  queue/history queries. Active/held/unknown jobs, ambiguous submissions and
  untagged legacy jobs remain blocked. Terminal jobs still in the queue must
  reach history before retry, avoiding finalization windows. Local parent state
  must be stopped.
  Missing validation dependencies or unsupported output checks block resimulation.
- Use the same explicit validation policy flags for planning and preparation;
  record the policy and selected scheduler/output evidence in the child and
  parent history. Inputs and source config are not regenerated.
- Reserve selected jobs under the parent lock before finalizing the child.
  Parent `recoveries` stores child ID/path, jobs and preparation state; execution
  state lives in the child's ordinary manifest. Preparing a second sibling for
  an owned job is rejected. Failed preparation without a child manifest releases
  that reservation but keeps its history; an ambiguous/finalized child keeps it.
- Child execution rechecks parent identity/input-plan checksum, registered path,
  ownership and eligibility before recording an execution attempt. It blocks if
  original jobs become active, results become valid, scheduler queries fail, or
  a copied child is used at an unregistered path. `--force` cannot erase recovery
  ownership. Plain productions retain their existing portability semantics.
- Verify copied file hashes against the parent snapshot before finalizing a
  child, not only against newly computed child checksums.
- Further retries form a chain from the failed child. No successful outputs are
  copied/overwritten and no original jobs are cancelled, released or reset.
- Extend immutable plan checksums with recovery metadata only when present;
  existing schema-2 plans still verify with their original digest algorithm.
- 66 tests pass locally, including a full fake-scheduler CLI flow from missing
  output through planning, child preparation, explicit execution and result
  validation. Selection, active-job rechecks, copying failures, duplicate
  ownership and repeated recovery chains are covered. No real jobs were sent.

### Packaging before MCP: `mg-pythia-delphes` / `mgpd` 0.1.0

- Added PEP 517/621 `pyproject.toml` with a `src/mgpd` package and `mgpd` console
  command. `python -m mgpd` is equivalent. Version metadata has one source in
  `mgpd/_version.py`; no PyPI publication or cluster deployment was performed.
- Split configuration, preparation, execution, Condor, output validation,
  recovery, storage, production models, templates and CLI into separate modules.
  Preserve the existing workflow functions and file formats.
- Replaced the old monolithic script with a compatibility entry point. Historical
  helper/template imports are re-exported through `mgpd.compat`; new callers use
  the public package API. Prefer installed code, falling back to source only if
  the package is unavailable. Developers must use an editable installation to
  test current edits rather than an older installed release.
- PyYAML is the core dependency; `validation` adds uproot, and `dev` adds build
  tooling and ROOT validation. No runtime Python HTCondor binding is required.
  Container execution remains external and unchanged.
- Build wheel and source distribution; include examples, tests, compatibility
  scripts and Dockerfiles in the source distribution, not as installed runtime
  package data.
- Port mocks to the modules owning their dependencies; retain regression
  coverage. Add installed entry-point, copied-wrapper, public-API and metadata
  tests, plus a frozen schema-2 fixture generated by the pre-package runner.
- Compared all 36 example/backend preparations to the monolithic implementation:
  equivalent inputs, archive contents, scripts and manifest semantics, ignoring
  creation timestamps, production UUIDs and timestamp-dependent archive hashes.
- 71 tests pass with the editable install and with a wheel installed into a fresh
  venv, running tests from the extracted sdist outside the checkout. The minimal
  wheel installation has only PyYAML (no uproot/NumPy); its suite passes with the
  optional ROOT test skipped. Installing `[validation]` passes all 71 tests.
  Built wheel code was checked byte-for-byte against the current package modules.

## Internal API

The implementation lives in `src/mgpd/`; the old script is a compatibility
wrapper. Installed Python callers from any directory can use:

```python
from mgpd import (
    load_config, prepare_production, load_prepared_production,
    execute_production, read_production_manifest, production_status, validate_production,
    plan_recovery, prepare_recovery,
)

config = load_config("config.yml", run_mode="condor")
prepared = prepare_production(config, "production")  # Writes files; no launch.

# In a separate Python session, without the original config/cards:
prepared = load_prepared_production("production")  # Verifies saved inputs.
# execute_production(prepared)                     # Explicit one-time attempt.
metadata = read_production_manifest("production") # No integrity/scheduler query.
# snapshot = production_status("production")        # Read-only scheduler query.
# results = validate_production("production")        # Read-only artifact checks.
```

Relative source input paths retain historical cwd semantics during preparation.
Saved execution paths are relative to the production root. Helpers raise
exceptions rather than exiting the interpreter. `main(argv=None)` is the CLI.

`PreparedProduction` contains a resolved output directory, manifest, local
sample directories and optional submit filename. Execution re-reads disk state;
editing an old object does not modify the saved execution plan. On success,
execution returns the updated manifest and refreshes that object's metadata.
`production_status(output_dir, timeout=30)` instead returns a fresh observation
without changing the saved manifest.

Useful lower-level helpers: `prepare_sample_inputs`, `prepare_job_plan`,
`package_sample_inputs`, `prepare_wrappers`, `prepare_condor_submission`,
`build_local_command`, `file_sha256` and `plan_sha256`.

## Manifest and execution semantics

Schema 1 remains readable with `read_production_manifest`, but cannot be safely
replayed: its hashes predate packaging and omit execution wrappers/submit files.
There is no automatic migration; prepare a new directory for unexecuted work.
Never regenerate already-submitted jobs merely to upgrade a manifest.

Schema 2 retains `prepared_input_sha256` for input provenance and adds
`execution_input_sha256` for final, physically present inputs. `plan_sha256`
checks immutable metadata, excluding mutable state and execution history.
The manifest is finalized only after all execution files have been prepared.

| State | Meaning |
| --- | --- |
| `prepared` | Inputs ready; no execution attempt recorded |
| `submitting` | Attempt recorded before invoking Condor; outcome may be unknown |
| `submitted` | `condor_submit` returned success; job completion is not established |
| `submission_unknown` | Submission failed/interrupted; some jobs may have been queued |
| `running` | Local execution attempt started |
| `failed` | Local command failed/interrupted; partial outputs may remain |
| `execution_finished` | Local commands returned success; outputs not yet scientifically validated |

A hard process interruption may leave `submitting` or `running`; those states
still block automatic replay. No automatic reset flag is provided. Stage 6 must
supply explicit recovery after inspecting scheduler/outputs.

The lock uses POSIX `flock` on `.execution.lock`; test cross-client lock behavior
on the actual JUPITER shared filesystem. Checksums catch accidental input edits,
not deliberate rewriting of files and their checksums. They do not prevent
noncooperating/manual `condor_submit`, editing files during execution, copying a
never-executed preparation into multiple directories, or loss of state after a
storage/power failure. This is not an exactly-once distributed transaction.
Image references are recorded, not yet verified immutable container digests.

## Validation and real test handoff

```bash
python3 -B -m unittest discover -s tests -v
python3 scripts/test_examples.py --output test-results/preparation-01
```

Regression coverage includes all example/backend preparation combinations,
mock-scheduler execution, output parsing, missing/changed input rejection,
plan edits, relocation without source files, stale callers, lock contention,
duplicate attempts, interrupted submissions and legacy-manifest rejection.

On JUPITER, repeat the small parton-only test followed by full-chain/MadSpin
checks using the commands in `examples/README.md`. The live driver exercises
`--execute-prepared`, then waits for completion and checks actual artifacts.
Also try executing the same production a second time: it must refuse before
calling Condor. Inspect scheduler state before creating replacement productions
when testing interrupted submissions. No new real jobs were launched from the
development environment for stage 3.

## Status interpretation and live handoff

Run on JUPITER under the same scheduler/user configuration used to submit:

```bash
python3 scripts/run_mg_pythia_delphes.py --status production
python3 scripts/run_mg_pythia_delphes.py --status production --json
```

JSON reports `recorded_state`, current `state`, per-job observations, counts,
query errors, and the recorded submission host when available. The summary state
prioritizes failures/removals/holds/unknowns over active jobs. Only all-completed
jobs yield `completed`; the per-state counts describe mixed productions.
`completed` requires `JobStatus=4`, `ExitBySignal=false` and `ExitCode=0` and is
not a claim that outputs are present or scientifically valid. History may be
expired or temporarily delayed; absence is unknown, never success.

Queries select exact recorded IDs, additionally restricting new submissions by
production identity. History is queried only for IDs absent from the queue.
Local status is recorded metadata only, not a live process check. The status CLI
returns 1 on query/metadata errors and 0 for a successful observation, including
observations of failed or unknown jobs. JSON `query_errors` preserves details.
Remote scheduler selection and automatic polling are not implemented.

The live acceptance test is: submit a new small production, observe idle/running
states where possible, query again after completion and after restarting the
client, and confirm that the reported IDs match the manifest and Condor. Also
confirm that the command does not change the execution-attempt marker or allow
another submission. No real jobs or scheduler queries were performed from the
development environment for this stage.

## Output-validation interpretation and handoff

```bash
python3 scripts/run_mg_pythia_delphes.py --validate-results production --json
```

This command returns 0 for `valid` and 1 for invalid, incomplete, missing
metadata or unavailable required checks. It does not equate job completion with
artifact validity, does not write status into the manifest, and does not clear
attempts. `metadata_errors` describes unreliable legacy submission mappings;
jobs without a reliable scheduler ID are unavailable, not guessed from files.

The report's per-job `outputs_state` describes artifact checks; `execution_state`
is normal/failed/unknown according to the saved local state or final terminal
record in the job's Condor log. `state` is invalid for known failed execution,
otherwise it follows artifact state. Unknown execution evidence can coexist
with valid artifacts and remains explicit. Per-sample totals include only jobs
whose combined state is valid; metrics from other readable files remain in the
individual job report. Zero totals can also mean an output type wasn't requested.

Default smoke policies are not universal analysis requirements. Use explicit
CLI/API policy overrides for weighted/filtered samples, and keep the resulting
policy in exported reports. ROOT/HepMC checks are integrity smoke checks, not
full event-schema/payload/physics validation. No aggregate cross-section
estimator or merged result is provided.

Run validation on already completed JUPITER local-Apptainer and Condor
productions, compare event totals and per-job cross sections to their banners,
and confirm no additional jobs are submitted. This command can also inspect
older manifests when artifact paths and a reliable job-ID mapping are available;
no preparation/resubmission is required merely to validate existing results.

For stage 6, recovery must use these per-job outcomes plus scheduler evidence,
not simply resubmit every missing artifact: an active or held job can still
produce it. Preserve inputs/seeds and make retry ownership/history explicit.

## Recovery semantics and real-test handoff

```bash
python3 scripts/run_mg_pythia_delphes.py --plan-recovery production --json
python3 scripts/run_mg_pythia_delphes.py --prepare-recovery production -o retry-01
# Review retry-01/manifest.json before the explicit launch:
python3 scripts/run_mg_pythia_delphes.py --execute-prepared retry-01
```

Plans are read-only snapshots. Preparation recomputes eligibility inside the
parent's lock, so use the same policy flags and inspect the saved selection.
Only original execution-input snapshots are copied, not generated outputs or
source paths referenced by the old YAML. Local directories retain original job
IDs and seeded MG files. Condor children share only the selected samples' input
archives and selected jobs' seeded commands. The config records original
settings; `jobs` is authoritative for the actual selected job count.

Recovery lineage is immutable child metadata; parent `recoveries` is mutable
allocation/history metadata excluded from the original plan checksum. The
parent's execution marker and outputs stay intact. Child execution requires
access to the registered parent and the child's registered path. Moving/copying
recovery directories is not supported automatically. To retry a failed child,
plan from that child; its original parent's reservation stays in place.

Preparation failures before a child manifest exists release the job reservation
but retain the failure record and partial output directory. Use a new output
name for another attempt. Once a child may be executable, its reservation stays
blocked even if recording the final parent update fails. There is no force-reset
or automatic owner reassignment for deleted/ambiguous child directories; audit
and reconcile those cases before manual recovery. This is not a distributed
exactly-once transaction and does not control manual scheduler actions.

For the first real test, use a disposable small production with one known failed
job or deliberately withheld test output after confirmed completion. Plan first:
valid jobs must be preserved; active/held/unknown jobs must be blocked. Prepare a
child and compare its seeds/cards and selected queue count with the parent,
then explicitly execute and validate it. A second preparation of the same job
from the original parent must fail; a second execution of the child must also
fail. Check the original good artifacts remain unchanged. Do not remove real
analysis outputs just to exercise this test. Real recovery has not been tested
on JUPITER from this development session.

Recovery does not merge a production family or replace the original summary.
Dataset assembly must choose one validated attempt per logical job, combining
original good jobs with validated child jobs without double-counting retries.

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
| `cli` | Parse arguments and coordinate public operations |
| `compat` | Historical script function/template exports |

For development, from the checkout:

```bash
python3 -m venv .venv-dev
source .venv-dev/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
python -B -m unittest discover -s tests -v
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

As an administrator, build and test a wheel, then create a **new** versioned
venv. For example, from the tested checkout in an administrator shell:

```bash
umask 022
python3 -m venv /opt/venvs/mgpd-0.1.0
/opt/venvs/mgpd-0.1.0/bin/python -m pip install --upgrade pip
/opt/venvs/mgpd-0.1.0/bin/python -m pip install \
  'dist/mg_pythia_delphes-0.1.0-py3-none-any.whl[validation]'
/opt/venvs/mgpd-0.1.0/bin/mgpd --version
```

Keep this environment administrator-owned, readable/executable by users and not
writable by ordinary accounts. Validate local Apptainer and a disposable Condor
smoke test under an ordinary user before selecting the release as default.

```bash
# Administrator: choose the validated release.
ln -sfn /opt/venvs/mgpd-0.1.0 /opt/venvs/mgpd-current

# Users: use the shared executable without activating a venv.
export PATH="/opt/venvs/mgpd-current/bin:$PATH"
mgpd --version
```

Do not overwrite an environment in use. Install the next release in another
versioned directory; change `mgpd-current` only after validation. Rollback points
it back to the previous environment. Existing processes retain their interpreter
and imported package; saved manifests, seeds and scheduler IDs stay in users'
production directories. Do not remove old environments while processes use them.
No shared environment was created or modified from this development session.
