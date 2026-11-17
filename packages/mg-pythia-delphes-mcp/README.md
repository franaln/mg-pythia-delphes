# mg-pythia-delphes-mcp

Use an MCP-compatible assistant to create and edit configurations, prepare productions, submit to HTCondor,
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

## Connect a client

The MCP server always runs on Jupiter, where your files and Condor are. What
changes between clients is **where the client runs**, and therefore how it
starts the server and which directory becomes the **workspace** (the root for
all paths that tools read or write):

| Client | Where it runs | Server registered in | Workspace |
| --- | --- | --- | --- |
| Claude Code in a Jupiter terminal | Jupiter | `~/.claude.json` on Jupiter | Automatic: the session's project directory |
| Claude desktop app, Code tab, SSH session to Jupiter | Jupiter | `~/.claude.json` on Jupiter | Automatic: the folder chosen for the session |
| Claude Desktop chat, Codex or Claude Code on your computer | Your computer | The local client's config, launching `ssh` | Fixed with `--workspace` |

Prefer the first two: register once and work in any project directory. Use the
`ssh` setup only for clients that cannot run on Jupiter.

The examples use the shared installation `/opt/venvs/mgpd-current/bin/mgpd-mcp`;
replace it with your own `.venv-mcp/bin/mgpd-mcp` if you installed one. Enable
only the [capability flags](#workspace-and-capabilities) you need.

### Recommended: Claude Code running on Jupiter

This covers both the `claude` command in a Jupiter terminal and SSH sessions to
Jupiter started from the Code tab of the Claude desktop app; in both, Claude
Code runs on Jupiter.

1. On **Jupiter**, register the server once in your user scope, without
   `--workspace`:

   ```bash
   claude mcp add --scope user mgpd -- /opt/venvs/mgpd-current/bin/mgpd-mcp \
     --allow-config-write --allow-prepare --allow-analysis
   ```

2. Start a session in a project directory: run `claude` there, or select that
   folder when creating the SSH session in the desktop app.
3. Check it with `/mcp` inside the session (or `claude mcp list`). The
   `mgpd://capabilities` resource reports the active workspace.

The server takes the workspace from `CLAUDE_PROJECT_DIR`, which Claude Code sets,
or else from its launch directory. It refuses `/` and your home directory as an
implicit workspace, so start sessions in a project directory, not directly in
`~`. Change flags by removing and adding the server again
(`claude mcp remove --scope user mgpd`).

Do **not** also add an `ssh` entry for this server to the desktop app's local
`claude_desktop_config.json`. The app forwards those local servers into Code
sessions too. Started through `ssh`, the server receives neither
`CLAUDE_PROJECT_DIR` nor the project directory and fails with
`... is too broad for an implicit workspace`. Use a different name if you keep
one for the chat (next section).

### Clients on your computer, through `ssh`

Claude Desktop **chat**, Codex, or Claude Code running locally cannot start a
program on Jupiter by themselves. They launch `ssh` on your computer, which
runs `mgpd-mcp` on Jupiter as your user. The server then starts in your home
directory without knowing your project, so `--workspace` is required. Pointing
it to another project means editing the configuration and restarting the client.

First make sure `ssh jupiter` works without an interactive password prompt;
connect your VPN if required. Replace the host alias and paths with yours.

Claude Desktop chat: edit `claude_desktop_config.json` (Settings → Developer →
Edit Config) on your computer and restart the app:

```json
{
  "mcpServers": {
    "mgpd-jupiter": {
      "command": "/usr/bin/ssh",
      "args": [
        "-T", "-o", "BatchMode=yes", "jupiter",
        "/opt/venvs/mgpd-current/bin/mgpd-mcp",
        "--workspace", "/mnt/R5/USER/my-study",
        "--allow-config-write", "--allow-prepare"
      ]
    }
  }
}
```

The app does not run `ssh` through your login shell: use the full path to
`ssh` and make sure the `jupiter` alias and keys work non-interactively from
`~/.ssh/config`.

Codex: add this to the local `~/.codex/config.toml`:

```toml
[mcp_servers.mgpd_jupiter]
command = "/usr/bin/ssh"
args = [
  "-T", "-o", "BatchMode=yes", "jupiter",
  "/opt/venvs/mgpd-current/bin/mgpd-mcp",
  "--workspace", "/mnt/R5/USER/my-study",
  "--allow-config-write", "--allow-prepare", "--allow-execute", "--allow-merge"
]
startup_timeout_sec = 30
tool_timeout_sec = 150
```

Claude Code on your computer:
`claude mcp add --scope user mgpd-jupiter -- /usr/bin/ssh -T -o BatchMode=yes jupiter /opt/venvs/mgpd-current/bin/mgpd-mcp --workspace /mnt/R5/USER/my-study ...`.
Other MCP clients can launch the same command and arguments in their own
configuration format.

A broader workspace such as `/mnt/R5/USER` avoids reconfiguring per project,
at the cost of a weaker path boundary and longer paths.

### Workspace and capabilities

The workspace must be an existing directory containing your configurations,
cards, productions and recoveries. Tool paths are relative to it. Paths
referenced inside YAML resolve from the tool's `base_directory`. Operations
started in one workspace are only visible to a server with the same workspace.
The workspace limits managed paths; it does not sandbox programs the server
starts with your permissions.

Without capability flags, the server only inspects and plans. Enable the actions
you need:

| Flag | Actions |
| --- | --- |
| `--allow-config-write` | Create/copy projects, create analysis starters and save reviewed YAML edits |
| `--allow-prepare` | Prepare productions and selected recoveries |
| `--allow-execute` | Submit prepared productions to Condor |
| `--allow-merge` | Merge validated outputs |
| `--allow-analysis` | Run reviewed analyses of existing outputs (never generation or submission) |

## Create and edit configurations

With `--allow-config-write`, ask for example:

> Create a detector-level project in my-config using Condor, with two jobs
> of 100 events. Show its configuration before preparing anything.

`create_project(output="my-config", level="detector", mode="condor", njobs=2)`
copies editable Standard Model cards and YAML. Levels are `parton` (default),
`particle`, and `detector`; the supported starter template is `proc`.

To reuse an existing project:

> Copy my-config to another-config, change the sample name to new-sample
> and use five jobs. Keep all other settings.

Use `create_project(output="another-config", copy_from="my-config",
name="new-sample", njobs=5)`. Only supplied options override the source.
Pass `process="generate p p > ta+ ta-"` to create or copy a project with a
specific process. A complete command block with newline characters is accepted,
including model imports, definitions and `add process`. Omit `process` to retain
the starter/copied process. An override replaces any process-card reference;
complete input-directory/file configurations cannot override their process.
Models other than `sm` require matching cards. MadGraph syntax is checked when
MadGraph runs, not during creation.

`template` and `copy_from` are mutually exclusive. Outputs must be new directories
inside the workspace. Project creation never prepares or submits jobs.

Inspect YAML with `inspect_config(path="config.yml", base_directory="my-config")`.
For edits, use `plan_config_change` with the same paths, review its returned diff,
then save the complete proposal with `apply_config_change` and its
`change_sha256`. YAML paths resolve from `base_directory`; card contents can be
edited separately. After a timeout or unknown creation/save outcome, inspect the
output before retrying.

## End-to-end example

With the repository root as the workspace, use the existing parton-level example.
Alternatively, create a project as above and use `my-config` as `base_directory`
with `path="config.yml"` for validation and preparation.
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

### Create an analysis starter

With `--allow-config-write`, call `create_analysis()` to copy the basic Delphes
ROOT analysis into `analysis.yml`. Use, for example,
`create_analysis(output="my-analysis.yml", format="lhe", base_directory="project")`
for another name, input format (`root`, `lhe`, `lhco`) or existing directory.
Paths stay inside the configured workspace and existing files are never
overwritten. Edit the starter for your sample, then call `plan_analysis` with
the returned configuration path. This tool processes no events and does not
require `--allow-analysis`; that separate permission enables analysis execution.
