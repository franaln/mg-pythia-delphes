# Development plan and change log

Keep user-facing usage in `README.md`, example/test instructions in
`examples/README.md`, and architecture, progress and future work here. Update
this file when completing a stage or changing the plan.

## Next steps

| Stage | Status | Scope and acceptance criterion |
| --- | --- | --- |
| 1. Consolidate runner/tests | Implemented; live coverage partial | Configuration validation, failure reporting, examples and an LHE metadata regression. Record further actual JUPITER runs as they happen. |
| 2. Separate responsibilities | Complete | Reusable load/validate, prepare and execute functions; unchanged CLI/YAML. Preparation equivalence checked across all 36 example/backend combinations. |
| 3. Execute saved preparations safely | Implemented; new live test pending | Reload saved inputs without original source files; reject changes; lock concurrent callers; persist an execution attempt before launch; block automatic duplicate/retry. |
| 4. Track HTCondor jobs | **Next** | Parse returned job IDs and associate them with manifest jobs; query queue/history; distinguish pending/running/held/completed/failed/unknown after restarting the client. |
| 5. Validate and summarize outputs | Planned | Move the example driver's checks into reusable result functions; produce sample-level summaries and distinguish execution success from valid output. |
| 6. Recover failed jobs | Planned | Retry only missing/failed jobs with preserved inputs/seeds and an explicit attempt history, without duplicating good outputs. |
| 7. Add a thin MCP interface | Planned | Wrap the same validated preparation, execution, status and result functions; no duplicated workflow implementation. |

Cross-cutting work:

- Add YAML CPU/memory/disk requests and check consistency with MG core counts.
- Record runner revision and verified container identity when available.
- Keep statistical/physics checks configurable: positive cross sections and exact
  event counts are appropriate smoke-test assumptions, not universal requirements
  for all weighted/filtered productions.
- Avoid changing physics cards, broadening scans or introducing an agent
  framework as part of infrastructure refactors.

## Changes implemented

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
  mapping and queue/history queries remain stage 4.
- Integration driver now goes through the guarded CLI instead of submitting or
  launching containers directly. Local timeout applies to the whole example.
- Moved this history, roadmap and internal API notes out of the README.
- 31 regression tests pass locally, including the 36 example/backend preparation
  combinations and guarded execution against a fake scheduler. New real Condor
  execution of this stage remains pending.

## Internal API

The implementation remains in `scripts/run_mg_pythia_delphes.py` so the existing
CLI path and imports stay available. Python callers from the repository root:

```python
from scripts.run_mg_pythia_delphes import (
    load_config, prepare_production, load_prepared_production,
    execute_production, read_production_manifest,
)

config = load_config("config.yml", run_mode="condor")
prepared = prepare_production(config, "production")  # Writes files; no launch.

# In a separate Python session, without the original config/cards:
prepared = load_prepared_production("production")  # Verifies saved inputs.
# execute_production(prepared)                     # Explicit one-time attempt.
metadata = read_production_manifest("production") # No integrity/scheduler query.
```

Relative source input paths retain historical cwd semantics during preparation.
Saved execution paths are relative to the production root. Helpers raise
exceptions rather than exiting the interpreter. `main(argv=None)` is the CLI.

`PreparedProduction` contains a resolved output directory, manifest, local
sample directories and optional submit filename. Execution re-reads disk state;
editing an old object does not modify the saved execution plan. On success,
execution returns the updated manifest and refreshes that object's metadata.

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
