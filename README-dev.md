# Developer and maintainer guide

User instructions are in [README.md](README.md), the
[example catalog](examples/README.md), and the [MCP guide](packages/mg-pythia-delphes-mcp/README.md).
This guide covers environment setup, tests, releases, shared deployment and
container builds. Roadmaps, implementation notes and historical test results
belong in the local, untracked `development.md`.

Run commands from the repository root unless stated otherwise. Use new output
directories for tests. Commands with `--execute` run real work; automated unit
tests use fake external tools and never submit real jobs.

## Development environment

Core requires Python 3.9+; use Python 3.11+ to develop/test the MCP adapter too.
Physics programs, container engines and HTCondor are external dependencies.

```bash
python3.12 -m venv .venv-dev
source .venv-dev/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]' -e packages/mg-pythia-delphes-mcp
```

The `dev` extra installs build tools, uproot and round-trip YAML support.
For core-only development, omit the adapter. Shared installations must use
versioned environments installed from wheels, not editable checkouts.

## Automated tests

```bash
python -B -m unittest discover -s tests -v
python -B -m unittest discover -s packages/mg-pythia-delphes-mcp/tests -v
python -m pip check
```

Run the core suite separately on Python 3.9 without the MCP SDK when checking
minimum-version support. MCP tests require its SDK and use real stdio transport
with fake scheduler/container executables. Passing these suites does not prove
that real images or cluster jobs work.

Targeted checks:

```bash
python -m unittest discover -s tests -p test_configuration.py -v
python -m unittest discover -s tests -p test_completion.py -v
python -m unittest discover -s tests -p test_create_images.py -v
python -m unittest discover -s tests -p test_checked_images.py -v
```

## Example preparation checks

```bash
python scripts/test_examples.py --list
python scripts/test_examples.py --mode condor --output test-results/preparation-condor
python scripts/test_examples.py --mode local  --output test-results/preparation-local
```

New Condor preparations package a shared `run.mg5` with one
`set iseed = XXXX` command. Each job's frozen numeric seed is passed as the fifth
wrapper argument; the worker validates it (1–30080) and substitutes the marker
before invoking MadGraph. No per-job `.mg5` file is generated or transferred.
Local jobs retain their fully rendered `run.mg5`. Existing saved Condor plans
and their recoveries retain their original wrapper and per-job command files;
do not replace frozen files manually.

These validate/prepare the examples in `examples/catalog.json`, checking job and
sample counts, seeds, frozen files, archives and Bash syntax. They do not launch
containers or contact Condor. Add new examples to the catalog. `--events` changes
events per job; the configured job counts are retained. Results include
`report.json`, configurations, prepared productions and per-example logs.

`local` resolves to Apptainer on hostname `jupiter`, Docker elsewhere. Use an
explicit concrete mode when testing another backend. Saved productions keep
the resolved backend and image; execution does not reinterpret the hostname.

## Real container and cluster tests

Run these as a normal user. Install the current package and uproot in the
interpreter running the test scripts. Use images available on the execution
host/workers. Jupiter uses Apptainer locally and under Condor; Docker is for
external hosts. Local execution is sequential.

### Full-chain test


```bash
python scripts/test_examples.py --execute --mode local --example example8_smoke_full --output test-results/apptainer-full-01
```
```bash
python scripts/test_examples.py --execute --mode condor --example example8_smoke_full --output test-results/condor-full-01 --timeout 1800
```

`condor_submit` and `condor_wait` must be available. The selected named image
must exist under `/opt/images/` on the workers. Version selectors (including revisions) and explicit SIF paths are supported.
Preparation resolves symlinks and records the actual SIF path and SHA256; that
exact file must remain available to every worker. Do not substitute the nonfunctional `latest` image.

Select `example9_smoke_madspin` to test decayed output directories. Real execution
requires explicit examples and defaults to at most eight jobs per example;
`--max-jobs` raises this limit when justified. Heavy examples may need extra
models and longer timeouts.

The driver waits for successful job termination and checks returned outputs:
LHE event counts/seeds/cross sections, readable nonempty ROOT trees and expected
HepMC/LHCO/log files. Use explicit outputs; `outputs: [all]` is not supported by
result validation. These are integrity checks, not physics certification.

Failed/timed-out Condor jobs remain queued for diagnosis. `--remove-on-failure`
removes only the recorded test job IDs and requires `condor_rm`. Inspect
`submitted_jobs.json`, logs and the scheduler if submission was ambiguous.

### Merge test

Generate two full-chain jobs, then merge their outputs in the real container:

```bash
python scripts/test_examples.py --execute --mode local --example example11_merge_smoke --output test-results/merge-production-01
python scripts/test_merging.py  --production test-results/merge-production-01/example11_merge_smoke/production --backend local-apptainer --output test-results/merge-check-01
```

The checker requires two valid jobs from one sample. It checks merged LHE,
ROOT and LHCO event totals, finite weights, an unweighted-replica normalization
sanity check, LHCO-only conversions and the production-directory merge report.
It retains reports/logs and leaves source artifacts untouched. A completed
Condor production can be checked the same way. For recovery families, merge
from the original production so that alternate attempts are not double-counted.

### Saved execution and selective recovery

```bash
python scripts/test_condor_workflow.py --execute \
  --image mg-pythia-delphes-3.5.6 --output test-results/condor-workflow-01
```

This submits two parton-level jobs and one selected recovery job. It checks
saved-plan execution, duplicate-attempt refusal, read-only status, tagged history,
result validation, recovery ownership and preservation of inputs/seeds. It
withholds one disposable archive and restores it afterward. Never use actual
analysis results for this exercise.

`--events` defaults to 100 and is limited to 1000; `--timeout` defaults to 1800
seconds per production. Held/unknown jobs or query errors block recovery. The
driver does not cancel jobs on failure. After a process kill, inspect the
original/backup paths in `report.json` before restoring anything.

A failed test with one submitted parent and no recovery/withholding can resume:

```bash
python scripts/test_condor_workflow.py --execute --resume \
  --output test-results/condor-workflow-01
```

Resume preserves identity, image, seeds and logs. Ambiguous child submissions
require inspection instead of another submission. Keep manifests, scheduler IDs
and numbered logs for diagnosis. To inspect an existing production without
running more work, use `mgpd status DIRECTORY --json` and
`mgpd validate-results DIRECTORY --json`.

## Release build and wheel validation

Update `src/mgpd/_version.py` and
`packages/mg-pythia-delphes-mcp/pyproject.toml` together, including the adapter's
core dependency bound. Build each distribution:

```bash
python -m build --outdir dist/core
python -m build packages/mg-pythia-delphes-mcp --outdir dist/mcp
```

Install both wheels in a fresh Python 3.11+ environment. For example, for 0.4.0:

```bash
python -m pip install 'dist/core/mg_pythia_delphes-0.4.0-py3-none-any.whl[validation]' \
  dist/mcp/mg_pythia_delphes_mcp-0.4.0-py3-none-any.whl
python -m pip check
```

Run tests from outside the checkout to exercise the installed package:

```bash
cd /tmp
/absolute/path/to/test-env/bin/python -m unittest discover \
  -s /absolute/path/to/repository/tests -v
/absolute/path/to/test-env/bin/python -m unittest discover \
  -s /absolute/path/to/repository/packages/mg-pythia-delphes-mcp/tests -v
```

Also check core commands without optional validation/configuration dependencies,
CLI/module versions, project creation from packaged cards, and completion
output. Check the source archive includes repository test/build inputs and
excludes the ignored local `development.md`. Image recipes require the repository
root as context because they also copy files from `data/`.

## Shared Jupiter deployment

`scripts/deploy_shared.py` installs versioned environments, with both CLI/MCP
launchers following `/opt/venvs/mgpd-current`. Build as a normal user; install
and select shared environments as their administrator.

| Action | Effect |
| --- | --- |
| `--build` | Build wheels only, without installing/selecting an environment |
| `--deploy-venv` | Install/check a new environment without selecting it |
| `--update-current` | Validate/select an existing environment |
| `--create-wrapper --bin-dir DIR` | Create managed CLI/MCP launchers |

Build the intended checkout (0.4.0 example):

```bash
python scripts/deploy_shared.py --build --with-mcp --version 0.4.0 --outdir dist/0.4.0
```

Use `--source PATH` to select another checkout, including a detached worktree at
a release tag. `--version` verifies package metadata; the script does not fetch
Git changes. Existing wheels are not overwritten. `--build` cannot be combined
with installation/activation. `--python` selects the installed runtime, not the
wheel builder. Core-only installations support Python 3.9+; MCP requires 3.11+.

Stage the tested pair without changing current:

```bash
sudo python3.11 scripts/deploy_shared.py --deploy-venv --version 0.4.0 \
  --wheel dist/0.4.0/mg_pythia_delphes-0.4.0-py3-none-any.whl \
  --mcp-wheel dist/0.4.0/mg_pythia_delphes_mcp-0.4.0-py3-none-any.whl \
  --prefix /opt/venvs --python /usr/bin/python3.11
```

Existing environments are not reused or removed. Failed installations remain
for diagnosis and cannot be activated. Test the staged commands as a normal
user before selection:

```bash
/opt/venvs/mgpd-0.4.0/bin/mgpd --version
/opt/venvs/mgpd-0.4.0/bin/mgpd-mcp --version
/opt/venvs/mgpd-0.4.0/bin/python packages/mg-pythia-delphes-mcp/scripts/check_server.py \
  --workspace /home/USER/mg-pythia-delphes --production production
```

Activate both commands and create launchers:

```bash
sudo python3.11 scripts/deploy_shared.py --update-current --version 0.4.0 \
  --with-mcp --prefix /opt/venvs --create-wrapper --bin-dir /usr/local/bin
```

Omit `--create-wrapper` for later activation/rollback. Change the version to an
already installed release to roll back. Wrapper-only actions require the version
to be current; unrelated commands/symlinks are refused. Users need the launcher
directory on PATH and do not activate the venv. Keep old environments available
while servers or detached workers still use them. Ordinary users must not have
write permission to shared environments.

## MCP connectivity and acceptance

Use the combined shared installation; set the remote command to
`/usr/local/bin/mgpd-mcp` in each user's SSH client configuration. The
[adapter guide](packages/mg-pythia-delphes-mcp/README.md) has the connection syntax.
SSH must work noninteractively (`BatchMode=yes`), startup must not print banners
to stdout, and the remote environment must find Condor/Apptainer. Servers and
jobs run as the normal SSH user. Reconnect clients after switching environments.

Run the read-only SDK connectivity check against a production you own:

```bash
python packages/mg-pythia-delphes-mcp/scripts/check_server.py \
  --workspace /home/USER/mg-pythia-delphes --production production > mcp-check.json
```

Omit `--production` for discovery only. `--config` and `--base-directory` validate
a YAML file. A nonzero exit means a tool/query error or invalid/incomplete result;
retain the JSON report. Version/import checks alone do not test SSH or engines.

Enable only the actions needed for the live acceptance test:

| Flag | Actions |
| --- | --- |
| `--allow-config-write` | Create/copy projects and apply reviewed YAML edits |
| `--allow-prepare` | Prepare productions and explicit selected recoveries |
| `--allow-execute` | Submit reviewed saved Condor plans |
| `--allow-merge` | Execute reviewed managed merges |

Use disposable projects to check:

1. Create a small custom-process project, then copy it with explicit overrides.
   Check cards, retained settings and the unchanged source. Validate YAML and
   inspect the prepared `run.mg5` before execution.
2. Plan/edit/save YAML through MCP, reviewing the diff/checksum. Use the returned
   `base_directory` for subsequent validation/preparation. Check refusal of
   stale reviews, existing create destinations and paths outside the workspace.
3. Prepare without submitting; verify jobs, seeds, image and plan checksum.
4. Submit once, reconnect and inspect the recorded operation. Repeated requests
   must not submit again. Wait for successful history and validate outputs.
5. Test selected recovery on disposable missing output as described in the
   Condor recovery test. Restore withheld output afterward.
6. Plan/execute a merge from the original production; check event totals,
   provenance, omitted jobs and one attempt per logical job. Reconnect while
   merging and verify repeated requests retrieve the same operation.

Keep manifests and operation IDs/logs. On unknown save/create/submit/merge
outcomes, inspect checksums, directories and scheduler records before retrying.
Do not unlink active lock files or reset attempt/ownership guards. Increase
client/server timeouts for large validations. Card contents remain user-supplied
code; workspace checks are not a Unix execution sandbox.


### Copyable MCP acceptance-test prompt

Connect the client to the candidate 0.4.0 MCP installation on Jupiter, with the
four capability flags above enabled. Use an existing workspace with enough
space for disposable projects and productions; the selected image must also be
available to Condor workers. Replace `mgpd_jupiter` below with the configured
server name and, if necessary, replace the SIF path with an installed revision.
The test submits two small detector-level jobs and runs a real merge.

Paste this prompt into the MCP client:

```text
Test the candidate mgpd 0.4.0 MCP server named mgpd_jupiter on Jupiter.
Use this image: /opt/images/mg-pythia-delphes-3.5.6.sif.

Use only that server's MCP tools/resources for this test. Do not fall back to
SSH commands, shell tools, direct Python calls or direct file edits to bypass a
missing or failing MCP feature. Report unsupported checks as NOT TESTED.

Read mgpd://capabilities first. Report the server version, workspace and enabled
capabilities. Stop if configuration writing, preparation, execution or merging
is unavailable, or if this is not the intended candidate installation.

Choose a unique prefix such as mcp03-YYYYMMDD-HHMMSS. Create the following as
separate direct children of the configured workspace, using that prefix:
-original, -copy and -production. Never reuse or overwrite an existing path.
Do not modify any unrelated project, production, image, alias or server setting.

1. Create an original project with create_project:
   template=proc, level=detector, mode=condor, name=mcp03_original,
   nevents=10, njobs=1, seed=101 and the image above.
   Use this explicit process block:
   import model sm
   generate p p > e+ e-
   Inspect and validate the resulting config with path=config.yml and the
   returned project base_directory. For every config operation, resolve path
   relative to that base; do not prepend the project directory twice. Record
   its checksum, settings and validation result.

2. Use create_project with copy_from pointing to the original project, without
   template, to create the copy. Override name=mcp03_copy, njobs=2 and seed=201.
   Leave process, level, image and nevents unspecified so they are inherited.
   Inspect/validate both configs. Verify that the original checksum has not
   changed and that the copy retains the process and 10 events per job.

3. Plan an in-place change to the copied YAML using plan_config_change:
   changes={"run.nevents": 20}. Use the copy's returned base_directory.
   Show the diff, validation and change_sha256, then WAIT for my approval.
   After approval, apply that exact proposal with apply_config_change.
   Reinspect/validate the copy and confirm nevents=20, njobs=2 and seed=201.
   Confirm once more that the original config checksum has not changed.

4. Prepare the copy into the new -production directory with prepare_production.
   Inspect the saved production. Show production_id, plan_sha256, resolved
   image, mode, samples, outputs and every job's seed and event count.
   Expect two Condor jobs, 20 events each, distinct seeds 201 and 202, and
   detector outputs including LHE, ROOT and LHCO. Do not execute if the plan
   differs or validation fails. Preparation alone must not submit jobs.
   Show the exact plan to execute and WAIT for my approval to submit it.

5. After approval, call execute_production with that exact saved plan_sha256.
   Record operation_id. Use operation_status to observe submission and
   production_status for the scheduler state. Report scheduler IDs and errors.
   Poll at reasonable intervals; do not busy-loop. If waiting is interrupted,
   return the production path and operation ID so I can ask you to continue.
   Never create another production or resubmit to compensate for a timeout or
   unknown outcome. Submission success alone does not mean the jobs completed.

6. When both jobs have completed successfully, call validate_results with
   event_count=exact, check_seed=true and require_positive_cross_section=true.
   Require valid requested outputs for both jobs, 20 LHE events per job and
   40 total. Report ROOT/LHCO validation metrics when returned by the tools.
   Call plan_recovery and expect no eligible jobs for this healthy production.
   Do not prepare a recovery or remove/rename outputs in this smoke test.

7. Call plan_merge on the original -production directory, using its default
   merged output, backend=local-apptainer, extract_lhe=true and
   allow_partial=false. Inspect the selected sample/jobs, omissions and
   selection_sha256. Expect both logical jobs exactly once and no omissions.
   Show the merge plan and WAIT for my approval to execute it.
   After approval, call merge_production with that exact selection_sha256 and
   identical options. Record operation_id, poll operation_status and inspect
   merge_report. Require successful completion and 40 merged LHE events;
   also report the ROOT/LHCO results and provenance exposed by the report.

At any unexpected failure, stop the dependent steps and report the tool name,
arguments, error, known state and available log/operation references. Preserve
all test files and jobs for inspection. Do not loosen validation, permit a
partial merge, fabricate missing evidence, cancel jobs or clean up automatically.

Finish with a PASS / FAIL / NOT TESTED checklist for creation, copying,
configuration editing, preparation, submission, scheduler completion, result
validation, recovery planning and merging. Include project/production paths,
checksums, operation/scheduler IDs, pinned image information when exposed,
event totals and any remaining uncertainty.
```

Approve each proposed change/plan only after reviewing it. To resume after a
client interruption, provide the same production path and recorded operation
IDs and ask the client to inspect their current state before continuing.
This smoke test checks healthy recovery planning only; exercise actual selective
recovery separately with the disposable-output procedure above. Reconnection,
stale-review rejection and path-boundary checks remain separate acceptance cases.

## Container image builds and distribution

The active recipes and helpers are in `images/`; `create_images.sh`, `dockerfiles/`
and their old helpers are retained until real-image validation is complete.
Use Python 3.9+ for `scripts/create_images.py`. Podman is the default engine;
select `--engine docker` elsewhere. Podman builds explicitly use `--format docker`
so the Dockerfile `SHELL` instruction is preserved. If building directly with
Podman, pass the same flag; OCI-format cached layers may have lost the Bash
configuration, so use `--no-cache` once when retrying a build affected by this. The recipes use x86_64 ROOT binaries, so
build on Jupiter. Supported MadGraph versions are 3.3.2 and 3.5.6; do not use
`latest`.

### Capture/review the source lock

```bash
python images/sources.py lock --version 3.3.2 --version 3.5.6 \
  --cache dist/image-source-cache --output images/sources.lock.json
```

This downloads sizeable scientific archives/PDF sets and records URLs/SHA256.
It reuses cache files and refuses an existing output lock. The initial capture
trusts those bytes: review it and compare with trusted upstream checksums where
available. Keep/commit the reviewed lock; use a new output file when reviewing
an update. Builds require the lock inside the build context and reject changed
bytes before extraction. HTTP/error pages are not silently accepted. A failed
HTTPS endpoint requires review of a replacement URL.

Before compiling, check the reviewed lock offline:

```bash
python images/sources.py check --version 3.5.6 --lock images/sources.lock.json \
  --cache dist/image-source-cache
```

The constructor runs this validation automatically before starting Podman/Docker.
Missing entries, URL differences and malformed SHA256 values are reported
separately. If a lock is out of date, capture a new file with `lock --output
images/sources-new.lock.json` using the existing cache, review its diff and use
`--source-lock images/sources-new.lock.json` for a test build. Do not edit a
checksum merely to bypass a failed verification.

If the reviewed lock exists but the host cache is absent (for example on CI),
populate it without changing the lock or its checksums:

```bash
python images/sources.py cache --version 3.5.6 --lock images/sources.lock.json \
  --cache dist/image-source-cache
```

### Build and check candidates

```bash
python scripts/create_images.py --version 3.5.6 --revision r1 \
  --build-jobs 4 --build --sif --check --dry-run
python scripts/create_images.py --version 3.5.6 --revision r1 \
  --build-jobs 4 --build --sif --check
```

Repeat `--version` for both versions. Commands work from any current directory;
default output is this checkout's `dist/images/`. The example produces OCI tag
`franaln/mg-pythia-delphes:3.5.6-r1`, SIF `mg-pythia-delphes-3.5.6-r1.sif` and a
neighboring `.sif.json`. `--source` changes the build context; `--source-lock`
selects a relative lock file inside it. `--build-jobs` requires `--build` and a
positive integer. `--no-cache` disables construction-layer reuse, not the local
source archives. `--source-cache` selects a cache directory inside the build
context (default `dist/image-source-cache`); a custom directory must not be
excluded by `.dockerignore`.

The constructor verifies all required cached hashes before starting the engine.
Both Dockerfiles use a separate source-archive stage, mounted read-only only in
installation steps. `fetch-source` copies the requested verified archive from
that mount; missing or corrupted cache files fail rather than triggering another
download. The archive cache is not copied into final runtime layers, although the
engine can retain the intermediate source stage in its local build cache.
Docker builds use BuildKit for these mounts; the constructor enables it. Podman
must support multi-stage `RUN --mount=type=bind,from=...`. `apt`, `pip` and optional
MadGraph installers may still need network access.

Actions are independent. `--sif` without `--build` converts an existing local
OCI image via a temporary Docker archive, without pushing first. `--check`
checks tools, ROOT import, MadGraph version, FastJet/LHAPDF and two common PDF
sets, against OCI and any generated/existing SIF. Existing SIFs must match their
recorded checksum/identity. `--replace` explicitly permits replacing outputs.

These checks do not generate events. Run the local full-chain and merge tests
above against the candidate SIF as a normal user. Test Condor before replacing
the recommended cluster alias. Use an explicit revision or candidate SIF path
for acceptance tests before promotion.
The image inventory/lock is under `/mg_pythia_delphes/share/mgpd-image/`; this
lock does not pin apt/pip packages or internal MadGraph installer downloads.

### Publish and install

After the real acceptance tests:

```bash
python scripts/create_images.py --version 3.5.6 --revision r1 --check --push
sudo python3 scripts/create_images.py --version 3.5.6 --revision r1 \
  --output-dir "$PWD/dist/images" --install --recommend
```

Authenticate with the engine beforehand; `--repository` changes the registry
repository. `--push` requires checks in the same invocation and retags the exact
checked image ID. Install-only needs neither engine nor Apptainer/rootless-store
access; it verifies checked SIF bytes and publishes complete temporary copies.
The SIF and metadata are published individually, so inspect both after an
interruption. The destination defaults to `/mnt/R5/images` and must exist. Use
`--install-dir /opt/images` to choose another directory. Setting `--install-dir`
alone does not install anything; installation requires `--install`. No
synchronization between `/mnt/R5/images` and `/opt/images` is assumed. Keep metadata with its SIF.

Use `--recommend` with an explicit revision to select the validated default.
For a SIF it atomically updates `mg-pythia-delphes-VERSION.sif` to a relative
symlink targeting the revision in `--install-dir` (default `/mnt/R5/images`).
Recommendation does not require `--install` if the checked revision and its
metadata are already installed; no build output or engine is needed. An ordinary file at the alias is never overwritten;
move it to a revision name first. Previous revision files remain available.
A previously checked SIF can also be promoted without engines or rebuilding:

```bash
python scripts/create_images.py --version 3.5.6 --revision r1 \
  --install-dir /mnt/R5/images --recommend
```

With `--check --push --recommend`, both `:VERSION-REVISION` and `:VERSION` are
published from the same checked image ID. Omit `--recommend` to publish a
candidate without moving the default. Keep published revision names unchanged.

### Discover and freeze production images

```bash
mgpd images                         # Apptainer on jupiter; Docker elsewhere
mgpd images --backend local-apptainer --directory /opt/images --json
mgpd images --backend local-docker  # Locally installed Docker tags
mgpd images --remote                # Published Docker Hub tags; needs network
```

The version aliases are the recommended images, rather than the highest
revision discovered. `run.image` may select `3.5.6`, `3.5.6-r1`, a SIF path, or
an explicit Docker reference appropriate to the backend. Preparation requires
an installed image: pull Docker images beforehand with `docker pull REFERENCE`.
Docker preparation inspects metadata only and pins a repository digest, or the
local content ID for images without a repository digest. Locally cached tags
are used as installed; pull again explicitly to refresh a moved remote alias.
SIF preparation resolves the real path and hashes the file. Hashing large SIFs
adds preparation time. Execution checks the SIF again before running/submitting.
Updating a recommended alias affects future preparations, not saved plans or
recoveries. Keep the referenced revision files available and unchanged.

Old prepared plans without an immutable image identity remain inspectable,
but must be prepared again before execution. Discovery and validation do not
pull images, launch containers or submit jobs. Docker Hub listing uses
`--repository namespace/name` (default `franaln/mg-pythia-delphes`).

The GitHub workflow uses this same constructor with Docker and requires a
reviewed `images/sources.lock.json` committed before running. It publishes image
artifacts/releases under image-specific names, separate from package tags.
Neither the local tests nor a dry run build/publish real images.

## Shell completion

Bash (`~/.bashrc`):

```bash
eval "$(mgpd completion bash)"
```

Zsh (`~/.zshrc`, after completion initialization):

```zsh
autoload -Uz compinit
compinit
eval "$(mgpd completion zsh)"
```

Do not duplicate an existing `compinit`. Use `/usr/local/bin/mgpd` explicitly for
shared installs when needed. To avoid startup generation, save the output to a
file and source it; regenerate after CLI upgrades. Completion uses shell functions
at Tab time, including argument choices and paths with spaces, and performs no
scheduler/container queries. Free-form process strings and IDs are not guessed.

## Analysis development and LHE/Delphes/LHCO checks

Install the optional stack and run the analysis regressions:

```bash
python -m pip install -e '.[dev]'
python -m unittest discover -s tests -p 'test_analysis*.py' -v
```

Generate the packaged dilepton starter, then edit its object selection,
variables, cuts, bins and labels for the simulated process:

```bash
mgpd analyze --example dileptons-lhe > analysis.yml
```

Use a validated LHE file from a small merged production. Change the PDG/object
selection and cuts to match the actual process before interpreting the result.

```bash
mgpd analyze /path/to/merged_unweighted_events.lhe.gz --config analysis.yml --validate-only
mgpd analyze /path/to/merged_unweighted_events.lhe.gz --config analysis.yml -o analysis-results
mgpd analyze /path/to/merged_unweighted_events.lhe.gz --config analysis.yml --plots -o analysis-with-plots
mgpd analyze /path/to/merged_unweighted_events.lhe.gz --config analysis.yml \
  --max-events 100 --chunk-size 10 -o analysis-preview
```

Each output must be new. Inputs can be plain/gzip LHE, Delphes ROOT, or plain/gzip LHCO files,
including multiple explicit files asserted to belong to one sample. Analyze each
representation separately. Managed merge directories are also supported. Analysis
never launches containers/jobs.

Analyze one sample/format from a managed merge:

```bash
mgpd analyze /path/to/production/merged --sample signal --format lhe \
  --config analysis.yml --plots -o analysis-signal
```

A production root resolves only its default `merged/merge-report.json`, checking
production/plan identity. Custom merge locations must be supplied explicitly.
A selected sample subdirectory is also accepted if its parent has the report.
Omit --sample/--format only when metadata/configuration makes the choice unique.
For explicit files, --sample remains a label and asserts they belong to one sample.

The adapter uses the merge output inventory and selected logical-job provenance;
it never recursively scans production/recovery directories or Condor archives.
Per-job copies retained by --keep-all are ignored. Partial/omitted merges,
repeated logical job IDs, paths escaping the merge directory, and checksum/size
mismatches are rejected. Metadata hashes are frozen and checked before and after
analysis. A completed read checks known merged event totals; LHCO uses LHE/ROOT
totals when present, otherwise its report records no known expected count.
The analysis output must be outside the managed merge directory.

For reconstructed Delphes electrons:

```bash
mgpd analyze --example dileptons-root > analysis-delphes.yml
mgpd analyze /path/to/merged_delphes_events.root --config analysis-delphes.yml --validate-only
mgpd analyze /path/to/merged_delphes_events.root --config analysis-delphes.yml --plots -o analysis-delphes
```

The ROOT adapter reads only required split branches in event-count blocks. The
supported collections are Electron, Muon, Photon, Jet, Particle and MissingET;
object sources refer to those exact branch prefixes. Jet uses its Mass
branch, electrons/muons use fixed masses (0.00051099895/0.1056583755 GeV), and
Photon/MissingET are massless. For Particle, native Px/Py/Pz/E take precedence
when all four branches are available; PT/Eta/Phi/Mass are the fallback. This
preserves beam-particle vectors whose pseudorapidity is stored as a sentinel.
Inspect each input's metadata.required_fields in the frozen plan to check the
selected representation. MissingET is represented as a massless vector
using MET/Eta/Phi; it is not a reconstructed neutrino four-vector. Cartesian
components and energy are derived from those conventions. Unknown PID/status
fields for reconstructed objects are rejected during planning.

For cylindrical inputs, a negative signed mass with magnitude at most
`8 * sqrt(double epsilon) * |p|` is treated as zero to handle cancellation
roundoff in native Delphes output. Larger negative masses fail. Inspect
`objects.COLLECTION.mass_roundoff_corrections` in the analysis report for the
number of corrected objects before selection; the per-input plan metadata also
records this policy. Native Particle Cartesian inputs use their stored energy
and reject negative/nonfinite energy instead of reconstructing it from mass.

Nominal weights use Event.Weight by default, accepting a scalar or exactly one
value per event. Set input.weights.branch to select another nominal branch, or
choose input.weights.mode: unit explicitly when no event weights are available.
Missing/ambiguous/nonfinite weights and inconsistent object lengths fail; there
is no implicit fallback to unit weights. Empty collections and trees are valid.

To test native Delphes compatibility, run the starter on a real file with two
chunk sizes and compare numerical cutflows/histograms. Measure peak memory and
throughput on a larger sample. Synthetic split TTrees alone do not establish
compatibility with every Delphes TClonesArray layout.

For standard LHCO files:

```bash
mgpd analyze --example dileptons-lhco > analysis-lhco.yml
mgpd analyze /path/to/merged_delphes_events.lhco.gz --config analysis-lhco.yml --validate-only
mgpd analyze /path/to/merged_delphes_events.lhco.gz --config analysis-lhco.yml --plots -o analysis-lhco
```

The LHCO reader accepts three-column headers (`0 event-number trigger`) and
11-column object rows, comments and blank lines. Object indices restart at one
and must be consecutive within each event. Types 0/1/2/3/4/6 map to
Photon/Electron/Muon/Tau/Jet/MissingET. Other types or dialects fail explicitly.
All visible four-vectors use the mass stored in the file (which can differ from
the fixed lepton masses used for ROOT). MissingET is a massless transverse proxy
with pz=0, regardless of placeholder eta/mass columns.

Use `input.weights: {mode: unit}` explicitly: no nominal weights or cross sections
are inferred from LHCO event headers. The sign of nonzero ntrk gives lepton
charge; zero means unknown, and opposite-sign pairing then fails. If appropriate
for your selection, remove unknown-charge objects with `is_valid(charge)`.
Jet charge, generator PID/status, ancestry and tag/isolation fields are not
exposed by this adapter. Tau objects form a separate collection and are not
silently included in Jet. Photon/MissingET charge is not exposed.

Empty events are retained; EOF closes the last event without requiring a newline.
LHCO has no event-length record/trailer, so a file cut exactly between valid rows
cannot be distinguished from a complete shorter final event. Planning inspects
the first event; execution validates all consumed rows and reports file/event/line
on malformed input. Preview mode does not validate the unread tail.

To test the native Delphes converter on Jupiter, compare ROOT and its converted
LHCO using explicit unit weights, only shared object content,
and tolerances for text precision/mass conventions. Record the image/converter
identity. Synthetic standard-format fixtures do not establish every dialect.

The result contains `analysis.yml`, `analysis-report.json`, `cutflow.json`,
`cutflow.csv` and `histograms.json`. Add `--plots` to generate `plots/` in the same
execution, with each histogram as PNG/PDF and cumulative count/weight cutflows.
Without `--plots`, no graphical renderer is loaded. For Python use
`run_analysis(plan, output, plots=True)` to request the same behavior. Plots use a headless
Agg canvas, explicit normalization labels and linear axes that retain negative
bins. Underflow, overflow and missing entries are annotated, not folded into
visible bins. Histogram labels in YAML should include the observable's units.
Per-fill errors are sqrt(sumw2); object-level fills do not account for additional
within-event object correlations.

The report records the frozen plan, full input hashes, dependency versions,
processed events, compute/render times and preview/completeness flags. Every
artifact except the report itself has a SHA256/size inventory in the report.
Inputs are checked before and after processing; failures publish no completed output. Inspect raw bin values
and cutflows, not just the final number of events, when testing a new sample.

Regenerate plots and CSV into a new directory using saved numerical results:

```bash
mgpd analyze --from-results analysis-results -o analysis-rendered
```

This does not read original event files, rerun selections or query the scheduler.
Keep `analysis-report.json`, `analysis.yml`, `cutflow.json` and `histograms.json`
together; original plots are not required. The YAML/plan, numerical shapes and
stored hashes are checked before rendering. Earlier numerical-only results
without artifact hashes are accepted with an explicit integrity warning.
New reports record source-artifact hashes and rendering provenance. Sources are
preserved, and the destination must be new and outside the source directory.
For Python use `mgpd.analysis.render_analysis(source, output)`.

CSV contains the same counters, relative/cumulative efficiencies, intervals,
weighted uncertainties, methods and null reasons as JSON. Undefined values are
empty CSV fields with explicit reason columns. PNG/PDF generation uses exactly
the stored bin values and variances; it applies no extra normalization or cuts.

Cutflow rows contain N, sumw, sumw2, positive/negative/zero weight counts and
relative/cumulative count efficiencies and weighted ratios. Count intervals use
Wilson z=1. Weighted errors include pass/total covariance and assume independent
events; correlated LHE eventgroups are rejected. A cancelling weighted denominator
returns null with a reason. Signed-weight ratios are not probabilities and are
not clipped to [0,1]. Normalization defaults to raw; nominal event weights are not
automatically converted to cross sections or luminosity yields. `mode: unit`
explicitly replaces weights by one; malformed/nonfinite event records still fail.

Histograms use left-inclusive/right-exclusive bins with an inclusive final edge,
separate flow bins and missing-value counts. `fill: events` requires a scalar
variable. For variables such as `electrons.pt`, use `fill: objects`; every object
receives its parent event weight and the result is an object-level distribution.
Variables/cuts never substitute zero for missing pairs or undefined arithmetic.
Object cuts filter particles; event cuts filter events cumulatively in YAML order.

Pairs select the highest scalar-pT-sum opposite-sign combination, breaking ties
by original particle indices. Known SM/common hadron charges are mapped explicitly;
unknown BSM charges require an appropriate selection rather than guessing signs.
The reader preserves original LHE mother indices, supports optional weight XML
(nominal XWGTUP only), rejects malformed/truncated events and processes bounded
batches. Computation currently uses a scalar event loop; batch-size changes must
leave numerical results unchanged. For memory/performance tests on Jupiter,
record peak memory and throughput at multiple sample and chunk sizes. Review all
requested observables for the process.


### Explicit physical normalization

Set this within the existing analysis `input` block (numbers below are illustrative):

```yaml
input:
  weights: {mode: nominal}
  normalization:
    mode: luminosity               # raw (default), cross_section, luminosity
    weight_convention: dimensionless
    cross_section_pb: 12.0
    sumw_generated: 40000.0         # full generated sample BEFORE analysis cuts
    luminosity_pb_inverse: 1000.0   # required only for luminosity mode
```

For cross_section, omit luminosity_pb_inverse. Both physical modes require an
explicit positive cross section and finite nonzero generated sum of weights;
no denominator is estimated from a preview, selected events, merged event count
or per-job cross sections. `weight_convention: dimensionless` asserts that the
weights and denominator share the convention needed for this formula:

- cross-section bin/cut value = sigma_pb * raw_sumw / sumw_generated;
- expected events = that value * luminosity_pb_inverse;
- statistical variance = raw_sumw2 * scale_factor**2.

LHE physical normalization is limited to IDWTUP=3, or -3 with nominal signed
weights; other/mixed strategies remain raw. ROOT/LHCO require the explicit
convention and external denominator as well; LHCO always uses unit weights.
Unit weighting does not recover information lost from weighted or signed samples.
Only use it for physical yields when a count denominator is appropriate for the
underlying sample. No filter-efficiency or branching-fraction correction is guessed.
User-supplied cross sections are not replaced by, summed from or averaged over
job/merge init values. External sigma, luminosity and denominator are treated as
fixed inputs; their uncertainties are not included in the reported errors.

Raw counters, sums, bin values/variances and efficiency ratios remain unchanged.
Physical values are additional `normalized` objects in cutflow/histogram JSON,
with scale/provenance in `normalization_metadata` and normalized columns in CSV.
Plots use the normalized values and label pb or events; flow bins scale too.
Saved-result rendering revalidates these against raw data/configuration and never
needs original events or merge metadata. A preview contributes only its processed
subset using the full external denominator; it is not extrapolated to the full sample.

Jupiter acceptance: analyze an actual managed merge in one selected representation,
compare raw statistics with explicit-file analysis, and independently check a
cross-section/yield example whose dimensionless weights and generated denominator
are known. Repeat with a preview and another chunk size; retain the source hashes
and report. Synthetic merge-writer/reader tests do not replace this live check.

### Native ROOT acceptance and resource measurements

Run the opt-in driver with a Python environment containing the installed wheel
and its `analysis` extra:

```bash
/path/to/analysis-venv/bin/python scripts/test_analysis_acceptance.py \
  --input /path/to/native_delphes_events.root \
  --output test-results/analysis-native-01 \
  --chunks 128 10000 --preview 10000 --collections-preview 1000
```

Use a new output directory and an independent native Delphes sample. The driver
reads the input without launching containers or scheduler jobs, compares full-file
dilepton results across chunk sizes, checks cutflows and histogram bins/flows
against independent calculations, and checks a preview of all six supported ROOT
collections. It also compares input SHA256 before and after execution. Input hashing
reads the full file several times; account for this I/O when selecting a sample.

`report.json` records the input identity, interpreter installation, commands,
counts and resource measurements. Per-command logs and timing files retain CLI
errors, elapsed time and peak RSS in KiB. The driver writes a failed report and
returns nonzero on discrepancies. Compare peak memory for the preview and full
run at the same chunk size, and compare full runs across chunk sizes. Elapsed
CLI times include planning and integrity checks, so they measure end-to-end
throughput. The driver requires Linux `/usr/bin/time`; each selected collection
must have the branches required by the analysis reader. Use raw normalization
for this acceptance test; physical normalization requires a separate verified
weight convention and full generated denominator.

### MCP analysis development and acceptance

Install the core and adapter in the same Python 3.11+ environment. The adapter's
`analysis` extra supplies the numerical and plotting dependencies:

```bash
python -m pip install -e '.[dev]' -e 'packages/mg-pythia-delphes-mcp[analysis]'
python -m unittest discover -s packages/mg-pythia-delphes-mcp/tests -v
```

Configure the stdio client to launch `mgpd-mcp --workspace /path/to/workspace`.
Add `--allow-analysis` to enable analysis writes. This flag is independent of
configuration writes, preparation, submission and merging. Without it,
`plan_analysis`, `analysis_status`, `analysis_report` and the `mgpd://analysis`
resource remain available; `start_analysis` is not exposed. The analysis resource
contains the expression vocabulary and packaged `dileptons-lhe.yml`,
`dileptons-root.yml` and `dileptons-lhco.yml` starters. Save the selected YAML in
the workspace and adapt it to the sample before planning.

Use this tool sequence:

1. Call `plan_analysis` with `inputs`, `config`, a new `output`, and explicit
   `format`/`sample` for ambiguous managed data. Set `base_directory` when paths
   are relative to a project. Review cuts, variables, normalization, preview,
   input hashes, output and plots. Use `offset`/`limit` and returned totals to
   inspect additional summary pages without changing the review checksum.
2. Call `start_analysis` with its `analysis_sha256` and identical analysis
   options, including output and plots. YAML byte changes, event/merge metadata
   changes and changed options invalidate the review. No files are created
   during planning. Execution reserves the output before launching a detached
   worker and returns a durable operation ID.
3. Reconnect as needed and call `analysis_status` with that operation ID and
   the workspace-relative output returned by planning. Status/report paths are
   relative to the workspace, independent of the planning base directory.
   Paginate logs with `log_offset`/`log_limit`. Disconnecting does not stop the
   analysis. A startup/interruption without terminal evidence is `unknown`;
   inspect the recorded operation instead of automatically starting another.
4. After `completed`, call `analysis_report` for paginated cutflows, histogram
   metadata and artifact paths. The adapter verifies the saved numerical data
   and completed-report identity without reopening the original simulation
   inputs. JSON contains full histogram bins; PNG/PDF paths are artifact
   references, not images automatically transferred to a remote client.

All managed paths, including YAML, merge metadata references, outputs and
symlinks, must stay inside the configured workspace. Repeating an identical
start returns the existing operation; failed/unknown operations are not relaunched.
Use a new reviewed output only after inspecting the previous outcome. Numerical
readers, cuts, normalization and publication run through the same core API as the
CLI; the adapter does not accept Python snippets or execute simulation programs.

Run the real stdio acceptance driver on existing data using the installed
core/adapter candidate wheels and SDK:

```bash
python scripts/test_analysis_mcp.py \
  --workspace "$PWD" --input /path/inside/workspace/merged \
  --format root --sample signal --plots \
  --output test-results/analysis-mcp-root-01
```

Use a new output for each representation. The driver records tool calls,
review/operation identities and CLI logs, disconnects and reconnects, verifies a
repeated start returns the same completed operation, and compares numerical
JSON with a separate CLI analysis. It uses existing inputs and creates only
analysis/test artifacts; it never creates simulation jobs or invokes a container.

### Export analysis histograms to ROOT

Use the analysis extra (NumPy and Uproot; PyROOT is not required):

```bash
mgpd analyze /path/to/events.lhe.gz --config analysis.yml --export-root -o analysis-root
mgpd analyze --from-results analysis-results --export-root -o exported-root
```

The second command verifies saved numerical JSON/YAML and exports without reading
simulation inputs or loading Matplotlib. Add `--plots` to produce PNG/PDF too.
Without `--export-root`, the existing `--from-results` behavior regenerates plots.
API equivalents are `run_analysis(plan, output, export_root=True)` and
`render_analysis(source, output, plots=False, export_root=True)`.

`histograms.root` contains TH1D objects under `raw/NAME` and, when physical
normalization is configured, `normalized/NAME`. Bin edges, signed bin contents,
Sumw2 variances and separate underflow/overflow bins come from the verified
numerical data; normalization scales variance by the square of the scale factor.
`fEntries` counts finite fills including flows, excluding missing observables.
For object histograms this is an object-fill count, and variances retain the
per-fill model without additional within-event correlations.

The ROOT `metadata` string records source numerical SHA256, original plan,
normalization, missing/selected counts and export conventions. ROOT statistical
moments use bin centers and exclude flows: original unbinned coordinates are not
available from saved histograms. ROOT distinguishes these binned statistics from
fill-time statistics; see the [TH1 reference](https://root.cern.ch/doc/master/classTH1.html).
The analysis final-edge-inclusive convention is preserved in stored bin contents;
refilling a ROOT histogram follows ROOT's own edge convention.

Export is staged with the other artifacts. A failure publishes no completed
output; existing destinations and input overlap remain rejected. The analysis
report inventories the ROOT artifact with size and SHA256. Run the round-trip,
signed-weight, normalization and publication tests with:

```bash
python -m unittest discover -s tests -p test_analysis_root_export.py -v
```
