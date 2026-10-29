# mg-pythia-delphes-mcp (0.2 alpha)

Separate, optional MCP adapter for `mg-pythia-delphes`. The first milestone is
**read-only**: inspect existing work, validate configurations/results, observe
Condor and plan recovery. It cannot prepare, execute, submit or merge jobs yet.
The core CLI remains independent and supports Python 3.9; this adapter requires
Python 3.11+ and uses the official MCP Python SDK 2.x.

## Install from this repository

From the repository root, use a separate environment:

```bash
python3.11 -m venv .venv-mcp
source .venv-mcp/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[validation]' -e packages/mg-pythia-delphes-mcp
mgpd-mcp --version
```

This installs both distributions into the MCP environment. It does not alter the
shared `mgpd-current` installation. The declared core compatibility range is
`>=0.1.1,<0.2`; the adapter's development version is `0.2.0a1`.

## Connect a stdio client

Run as the normal user who owns the productions and Condor jobs:

```bash
mgpd-mcp --workspace /mnt/R5/falonso/project
```

The command waits for MCP requests on stdin/stdout; it is not an interactive
shell. Configure a stdio-capable client's MCP command, for example:

```json
{
  "mcpServers": {
    "mgpd": {
      "command": "/absolute/path/to/.venv-mcp/bin/mgpd-mcp",
      "args": ["--workspace", "/mnt/R5/falonso/project"]
    }
  }
}
```

Client configuration format varies. In a remote editor, launch this process on
JUPITER. A desktop stdio client may instead launch `ssh` with arguments `-T`,
`jupiter`, and the absolute remote command/arguments above. Ensure SSH is already
authenticated and remote shell startup does not write banners to stdout. No
HTTP listener, shared daemon, root account or model-provider API key is required.

## Tools and paths

| Tool | Purpose |
| --- | --- |
| `validate_config` | Validate existing YAML and input references |
| `inspect_production` | Verify frozen inputs and summarize a schema-2 production |
| `production_status` | Query the current user's Condor queue/history or local recorded state |
| `validate_results` | Validate outputs using the existing core policies |
| `plan_recovery` | Explain eligibility; never prepare/execute recovery |

`mgpd://capabilities` reports adapter/core versions, workspace, timeout and tools.
Paths are relative to the configured workspace; absolute paths inside it are
also accepted. Configuration references resolve from `base_directory` (default
workspace root), preserving the CLI's current-directory semantics. For example,
`validate_config(path="config.yml", base_directory="examples/example1_process")`.
The base directory itself is always relative to the workspace.

Parent/recovery directories, cards, job outputs and symlinks must stay inside the
workspace. Select a common project root if cards/productions are in sibling
directories. This is a path guard for these read-only operations, not a sandbox
for executing arbitrary physics code or an authorization boundary between users.

Responses contain structured data plus matching text JSON:

- Success: `{"schema_version":1,"ok":true,"data":{...}}`.
- Failure: `{"schema_version":1,"ok":false,"error":{"code":"...","message":"..."}}`
  and the MCP error flag. Scheduler failures retain observations under `data`.
- SDK argument-validation and unknown-tool errors use the SDK protocol error
  format; the envelopes above describe adapter-generated responses.
- Invalid results are a successful observation with `data.state="invalid"`;
  invalid configuration or missing paths produce tool errors.
- Errors include `PATH_OUTSIDE_WORKSPACE`, `NOT_FOUND`, `VALIDATION_ERROR`,
  `QUERY_FAILED`, `TOOL_TIMEOUT` and `WORKER_FAILED`.

Job listings are bounded by `limit` (1..1000, default 100), with totals and
truncation flags. This bounds the response, not the underlying validation work.
`--timeout` defaults to 120 seconds; timeout/disconnection kills read-only worker
processes and their query subprocesses, never submits work or resets a manifest.
Large datasets may need a longer timeout. Durable asynchronous operations are a
later milestone.

Tool execution uses one isolated core worker per request with a fixed cwd; it
does not change the MCP server's cwd. Diagnostics go to stderr, never protocol
stdout. Files/manifests are not modified. No write tools are registered.

## Development tests

From the repository root, in the MCP environment:

```bash
python -m unittest discover -s packages/mg-pythia-delphes-mcp/tests -v
```

Tests use the real SDK stdio transport, core APIs and file fixtures; only external
scheduler responses are faked. They do not establish that JUPITER connectivity
or a particular client's configuration works. See the repository's developer
guide for builds, independent core tests and installed-wheel checks.

## JUPITER connectivity check (no model required)

From the repository root with the MCP environment active:

```bash
python packages/mg-pythia-delphes-mcp/scripts/check_server.py \
  --workspace /mnt/R5/falonso/project \
  --production production > mcp-check.json
```

This uses a real SDK client to launch the installed server over stdio, discover
its tools/resource and inspect, query, validate and plan recovery for the existing
completed production. It never submits jobs or writes production files. Omit
`--production` to test only connection/discovery. Optionally add `--config` and
`--base-directory` to validate YAML. The JSON report retains tool responses;
query errors or invalid/incomplete/unknown results cause a nonzero exit status.
Run as the same user with scheduler access, not with sudo. A successful check is
live evidence only for the workspace, scheduler and environment actually tested.
