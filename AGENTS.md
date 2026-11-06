# Working on mg-pythia-delphes

These instructions apply to this repository and its MCP subpackage. Work from
this checkout's root; do not assume a particular user, hostname or checkout path.

## Purpose and architecture

mgpd runs reproducible MadGraph + Pythia8 + Delphes simulations using YAML,
local Docker/Apptainer or HTCondor. It prepares immutable production plans,
executes them, validates outputs, manages recovery and merges samples. The
optional `analyze` module explores outputs without running physics generators.

Keep shared behavior in the core Python API. CLI and MCP are adapters, not
independent implementations of production, validation, merging or analysis.

| Location | Responsibility |
| --- | --- |
| `src/mgpd/cli.py` | Argument parsing and CLI dispatch; shell completion derives from argparse |
| `config.py`, `configuration.py`, `projects.py`, `templates.py` | Generation configuration, reviewed edits, project creation and resources |
| `preparation.py`, `execution.py`, `storage.py`, `models.py` | Frozen plans, execution lifecycle, persistence and integrity |
| `condor.py`, `results.py`, `recovery.py` | Scheduler status, output validation and logical-job recovery |
| `merge.py`, `production_merging.py` | Native file merging and recovery-aware sample assembly |
| `src/mgpd/analysis/` | Analysis configuration/expressions, readers, statistics, datasets, normalization and reporting |
| `src/mgpd/data/` | Packaged templates, cards and analysis YAML examples |
| `packages/mg-pythia-delphes-mcp/` | Separately packaged per-user MCP server and its tests |
| `images/`, `scripts/create_images.py` | Image recipes, locked source cache, image builds/install/recommendation |
| `scripts/deploy_shared.py` | Wheel builds and versioned shared deployment |
| `tests/`, `examples/`, `scripts/test_*.py` | Unit/contract tests and explicit integration drivers |

Module filenames in this table are relative to `src/mgpd/` unless otherwise shown.

## Start each task

- Read `git status`, the current branch and relevant code/tests before editing.
  Preserve unrelated edits and untracked files. Stage only changes for the task.
- Consult `README-dev.md` for current development/live-test commands, and
  `development.md` if present for the active plan and checkpoints. Code and
  current tests take precedence over historical progress notes.
- `development.md` is intentionally local and Git-ignored: a clone will not
  contain it. Do not assume it migrated with the repository. This file includes
  the current analysis handoff below so work can continue without that roadmap.
- Use the existing branch/workflow unless the user requests a new one. Keep
  coherent commits when commits are authorized. Do not push, merge releases,
  replace tags or deploy shared environments as an incidental part of coding.

## Development environment

Core supports Python 3.9+. MCP requires Python 3.11+; Python 3.12 is convenient
for developing both. Preserve core Python 3.9 syntax and dependency compatibility.
Use a user-owned development venv, separate from the shared installation:

```bash
python3.12 -m venv .venv-dev
source .venv-dev/bin/activate
python -m pip install -e '.[dev]' -e packages/mg-pythia-delphes-mcp
```

Use an available compatible interpreter if `python3.12` is absent. Omit the MCP
package for core-only work. The `dev` extra includes the optional analysis stack.
Use an actual editable/wheel install for subprocess/entry-point tests; a relative
`PYTHONPATH=src` breaks when subprocesses change directory.

Prefer imports at module scope. Optional dependencies must still load only at
the feature boundary: importing mgpd, displaying help or running generation
must not import NumPy/Awkward/Vector/Uproot/Matplotlib or the MCP SDK. A module
with top-level optional imports must itself be imported lazily by its caller.
Do not solve circular imports by loading the entire numerical stack eagerly.

## Verification

Run focused tests for the change, then the relevant full suites. Add tests for
meaningful behavior and independently known results, rather than reproducing
implementation details or merely changing expectations to make failures disappear.

```bash
python -B -m unittest discover -s tests -p 'test_analysis*.py' -v
python -B -m unittest discover -s tests -v
python -B -m unittest discover -s packages/mg-pythia-delphes-mcp/tests -v
python -m pip check
```

MCP tests need its SDK. For packaging changes, build wheels, check packaged
resources and exercise installed commands outside the checkout. For optional
imports, use a separate base-only environment; the dev venv cannot prove that
missing dependencies are handled correctly. Python 3.9 grammar checks do not
replace a real Python 3.9 runtime test.

Mocks and synthetic ROOT trees test contracts; they do not establish that real
MadGraph, Delphes, Apptainer, Docker or Condor works. Report exactly which layer
was tested and leave unperformed live checks explicitly pending. Record commands,
package/image identities, input provenance, reports and any material limitations.

## Working directly on Jupiter

- Prefer Apptainer for local physics runs. The `local` alias is site-aware;
  inspect its resolved backend and image rather than assuming it means Docker.
- Discover available images, tools, interpreters and paths. Storage commonly
  uses `/mnt/R5/<user>` and scratch areas; image build installation defaults and
  runtime lookup paths can differ. Consult `mgpd images` and current script help.
- Read-only inspection and authorized tests can proceed normally. Scope live
  tests to new, clearly identified user-owned directories and small event/job
  counts. Reuse the user's existing authorization; do not invent approval pauses.
  If scope is missing, prepare the exact command/config and explain the missing
  decision rather than launching an unspecified large production.
- Never overwrite existing production/analysis outputs or silently alter shared
  images, recommended aliases, system configuration or `current` venv links.
  Avoid sudo and shared deployment changes unless the task requests them.
- Scheduler jobs outlive clients. Do not resubmit/retry automatically after a
  disconnect or ambiguous outcome; inspect persistent state first. Do not cancel
  jobs or remove production data as routine cleanup.

## Integrity and physics contracts

- Preparation and execution remain distinct. Preserve frozen cards, seeds,
  plan/input checksums, locks, execution-attempt records and duplicate-launch
  guards. Never bypass integrity checks to make a test or an old plan execute.
- Recovery preserves logical jobs and seeds. Merge from the original production
  family, following registered/verified descendants and selecting one valid
  stopped attempt per logical job. Never glob original and recovered outputs
  together or sum duplicate attempts.
- Preserve no-overwrite, path/archive safety and staged output publication.
  Fail explicitly on malformed input or missing outputs; never fabricate success.
- Do not change physics selections/cards, cross sections, PDFs, weight
  conventions or normalization implicitly to resolve an infrastructure problem.
- MCP workspace checks constrain managed paths, including symlinks; they do not
  sandbox programs launched with the user's permissions. Preserve explicit
  server capabilities, reviewed config/plan checksums, bounded responses,
  durable operations and clean protocol stdout. Do not add arbitrary shell or
  agent-supplied Python execution to the analysis expression language.
- Keep image revisions reproducible. Recommended SIF symlinks/Docker tags are
  explicitly promoted aliases, not an instruction to select the largest revision.
  Prepared runs pin the concrete SIF/checksum or immutable Docker identity.

## Analysis handoff — 2026-11-06

Development has been on `dev_analysis`, following release `v0.3.0`. Do not move
that release tag or assume analysis changes are deployed on Jupiter. The latest
analysis implementation checkpoint is commit `2516a15`; verify actual HEAD.

Implemented:

- Strict versioned YAML and allowlisted expressions (no Python `eval`), variable
  dependencies, object selection/sorting, OS pairs, ordered cuts and histograms.
- Bounded plain/gzip LHE, split-branch Delphes ROOT and standard 11-column LHCO
  readers. CLI/API support validation-only, previews, chunk sizes and explicit
  files of one sample/representation. Packaged examples: `lhe-dileptons`,
  `delphes-dileptons`, `lhco-dileptons`.
- Raw JSON/YAML/CSV results; `--plots` adds PNG/PDF. `--from-results` regenerates
  plots/CSV without original event files. Outputs are new directories, inputs
  are fingerprinted around execution, and failures do not publish completed data.
- Signed weights, sumw/sumw2, separate flow/missing counts, count intervals and
  weighted subset errors with covariance. Preserve raw statistics. Weighted
  signed ratios are not probabilities; correlated LHE eventgroups are rejected.
- Managed `merge-report.json` discovery with sample/format selection, hashes,
  unique logical-job provenance and known event totals. A production root uses
  its matching default merge; other locations must be explicit. Partial merges
  are rejected and retained per-job copies are excluded. No automatic merge,
  recursive production scan or archive extraction occurs during analysis.
- Explicit cross-section/luminosity normalization, requiring dimensionless-weight
  convention, `cross_section_pb`, full-sample `sumw_generated` and, for yields,
  `luminosity_pb_inverse`. Raw statistics/efficiencies remain available; physical
  values are additional normalized fields with scaled statistical variances.
  External normalization inputs are fixed, with their uncertainties excluded.
  Never infer denominators from previews, average/sum same-sample job cross
  sections, or guess filter/branching-fraction corrections.

Current reader/normalization limits:

- ROOT: supported collections are Electron, Muon, Photon, Jet, Particle,
  MissingET. Electron/Muon use fixed masses; Jet/Particle use stored Mass.
  Photon/MissingET are massless proxies. Do not claim arbitrary ROOT support.
- LHCO: standard headers/object rows only; explicit unit weights. Types
  0/1/2/3/4/6 map to Photon/Electron/Muon/Tau/Jet/MissingET. Visible masses are
  stored values; MET is transverse/massless. Lepton charge uses sign(ntrk), with
  zero unknown; OS pairing requires known charges. No generator PID/status,
  jet charge or exposed tagging/isolation fields. EOF closes the final event;
  loss of whole final rows cannot be detected structurally.
- Physical LHE normalization supports IDWTUP=3 or -3 with nominal weights, and
  only 3 with unit weights. Other/mixed strategies remain raw. ROOT/LHCO also
  need explicit external conventions/denominators. Unit weights cannot recover
  lost signed/weighted information.

Latest local verification: 306 core tests, including 82 analysis tests, passed;
base-only analysis passed 27 and skipped 55 optional tests. These are historical
results, not evidence of a new checkout's status or real Jupiter execution.

Next: **stage 6 — Jupiter acceptance, packaging and performance**.

1. Install the analysis wheel in a separate user venv; exercise installed CLI,
   resources and optional dependencies without touching shared deployment.
2. Use a small real validated production/merge. Analyze LHE, ROOT and LHCO
   separately and compare managed discovery with explicit-file results.
3. Compare ROOT with its native Delphes-converted LHCO only for information
   retained in both; account for text precision, mass and collection conventions.
   Verify the actual converter dialect/source before claiming compatibility.
4. Independently check cutflows/observables and one justified normalization case.
   Repeat with a different chunk size and a preview; verify unchanged sources.
5. Measure memory and throughput on a representative larger input and record
   exact input/image/package identities. Readers are bounded, but the engine
   currently computes per event; vectorized throughput has not been established.

Real Delphes TClonesArray compatibility, native LHCO conversion comparison and
large-sample Jupiter measurements remain pending. If actual files expose a
mismatch, fix it with a focused regression and record the real evidence.

After that: **stage 7 — MCP analysis adapter**. Reuse the core API for read-only
planning and separately enabled analysis writes, with frozen-plan review,
workspace confinement, durable operation/status tracking and bounded report/
artifact access. Analysis is not yet exposed through the MCP server.

## Documentation and delivery

Keep user usage in `README.md`/`examples/README.md`, developer testing, image and
deployment instructions in `README-dev.md`, and implementation progress in local
`development.md` when present. Do not rewrite user-facing READMEs incidentally;
current analysis operational examples belong in README-dev until requested.
Update documentation alongside behavior. Summarize the outcome, relevant tests
and remaining real-world checks; distinguish a local commit from a push/deploy.
