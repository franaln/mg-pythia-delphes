# mg-pythia-delphes-mcp

Use an MCP-compatible assistant to prepare productions, submit to HTCondor,
monitor jobs, validate results, recover selected jobs and merge outputs.
Requires Python 3.11+. Local production execution is available through the
[CLI](../../README.md), not through MCP.

## Installation

On the machine with Condor access, install from the repository root:

```bash
python3.11 -m venv .venv-mcp
source .venv-mcp/bin/activate
python -m pip install --upgrade pip
python -m pip install '.[validation]' ./packages/mg-pythia-delphes-mcp
```

If your administrator provides `mgpd-mcp`, use its executable path instead.

## Connect through SSH

First make sure `ssh jupiter` works without an interactive password prompt;
connect your VPN if required. Replace the host alias and paths below with yours.

For Codex, add this to the **local client's** `~/.codex/config.toml`:

```toml
[mcp_servers.mgpd_jupiter]
command = "/usr/bin/ssh"
args = [
  "-T", "-o", "BatchMode=yes", "jupiter",
  "/home/USER/mg-pythia-delphes/.venv-mcp/bin/mgpd-mcp",
  "--workspace", "/home/USER/mg-pythia-delphes",
  "--allow-prepare", "--allow-execute", "--allow-merge"
]
startup_timeout_sec = 30
tool_timeout_sec = 150
```

Other MCP clients can launch the same command and arguments using their own
configuration format. SSH runs on your local computer; the MCP executable runs
remotely as your SSH user. Restart the MCP connection after configuration changes.

`--workspace` must be an existing directory containing your configurations,
cards, productions and recoveries. Tool paths are relative to this directory.
Paths referenced inside YAML resolve from the tool's `base_directory`.

Without capability flags, the server only inspects and plans. Enable the actions
you need:

| Flag | Actions |
| --- | --- |
| `--allow-prepare` | Prepare productions and selected recoveries |
| `--allow-execute` | Submit prepared productions to Condor |
| `--allow-merge` | Merge validated outputs |

## End-to-end example

With the repository root as the workspace, use the existing parton-level example.
Choose a new production directory for each run. These requests can be written
in your own words; tool names are included to make the steps explicit.

1. **Validate and prepare.** Ask:

   > Validate config.yml with base_directory examples/example7_parton_only.
   > Prepare it in production-01, then show the jobs, seeds, image and plan checksum.
   > Do not submit yet.

   The assistant uses `validate_config`, `prepare_production` and
   `inspect_production`. This example prepares two jobs of 100 events.

2. **Submit after reviewing the plan.** Ask:

   > Submit production-01 using its reviewed plan checksum, and give me the operation ID.

   `execute_production` requires `path` and `plan_sha256`. Keep the returned
   `operation_id` to check submission with `operation_status` after reconnecting.

3. **Monitor and validate.** Ask:

   > Check the submission operation and the Condor jobs for production-01.
   > Once all jobs finish, validate their results and summarize each sample.

   The assistant uses `operation_status`, `production_status` and
   `validate_results`. `submitted` means accepted by Condor; result validation
   determines whether the outputs are usable. Ask again later if jobs are running.

4. **Review the merge.** Once results are valid, ask:

   > Plan the merge of production-01, including registered recoveries.
   > Show the selected jobs, any omissions, output directory and selection checksum.

   `plan_merge` defaults to `production-01/merged`. Request a different `output`
   if that directory already exists. Use `samples` to select particular samples.

5. **Merge and inspect the result.** Ask:

   > Run the reviewed merge with the same options and selection checksum.
   > Give me its operation ID, then check its status and report the event totals and provenance.

   The assistant uses `merge_production`, `operation_status` and `merge_report`.
   The full report is saved as `merge-report.json` in the output directory.

## Recover selected jobs

If validation finds failed or missing jobs, ask:

> Plan recovery for production-01 and explain which jobs are eligible.
> Do not prepare or submit anything yet.

After reviewing `plan_recovery`, select the job IDs explicitly:

> Prepare only JOB_ID_1 and JOB_ID_2 in retry-01, preserving the original seeds.
> Show the prepared plan before submission.

`prepare_recovery` requires `path`, a new `output`, and a nonempty `job_ids` list.
Review, submit and validate the retry using the same steps above. Keep the parent
and recovery directories in place; merge from the original production directory.
Active, held, or unknown jobs are not eligible for recovery.

## Options and interrupted operations

Validation policies include `event_count` (`exact`, `nonempty`, `ignore`),
`require_positive_cross_section` and `check_seed`. Use the same policies when
planning and performing recovery or merging.

Merging supports `samples`, `output`, `backend`, `image`, `extract_lhe` and
`keep_all`. Missing or invalid jobs block merging unless you explicitly request
`allow_partial=true`. Review omissions before accepting a partial dataset.
Use the same options and `selection_sha256` for merge planning and execution.

Existing output directories are not overwritten. Submission and merge operations
survive an MCP disconnect: reconnect and inspect `operation_status` with the saved
ID. If an outcome is `unknown`, inspect the operation and production before taking
action. After interrupted preparation, inspect the requested directory before
trying again.