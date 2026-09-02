# YYLO Ledger

YYLO Ledger is a Git-native task and Record store with a shell-friendly CLI. It is for developers, automation authors, and coding-agent workflows that need reviewable current state, append-only history, dependency-aware work, and bounded queries without making a database the source of truth.

- Package: [`yylo-ledger`](https://pypi.org/project/yylo-ledger/)
- CLI: `yylo-ledger`
- Python import: `yylo_ledger`
- Source: [yylo-dev/yylo-ledger](https://github.com/yylo-dev/yylo-ledger)

[![Source version](https://img.shields.io/badge/version-0.2.1rc4-blue.svg)](https://pypi.org/project/yylo-ledger/)

The badge identifies this source checkout; the stable and prerelease install channels are separated below.

Ledger owns Records and task history. [YYLO CLI](https://github.com/yylo-dev/yylo) owns coding-agent and task/merge/release orchestration. [YYLO Benchmark](https://github.com/yylo-dev/yylo-benchmark) owns evaluation runs and their private evidence registry.

## Quick start

**Prerequisites:** Python 3.8 or newer and a shell. Use a virtual environment so the command and package version stay explicit.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install 'yylo-ledger==0.2.0'
yylo-ledger --version

mkdir ledger-demo
cd ledger-demo
yylo-ledger create --title "First task" --body "Verify the Ledger quick start" --tags onboarding
yylo-ledger list --limit 5 --format table
yylo-ledger doctor
```

A successful run prints `yylo-ledger 0.2.0`, creates a six-character task ID, shows that task in the table, and exits cleanly from `doctor`.

### Stable and prerelease channels

`0.2.0` is the stable PyPI release. Install the current release candidate only when you intentionally want prerelease behavior:

```bash
python -m pip install 'yylo-ledger==0.2.1rc4'
yylo-ledger --version
```

The `0.2.1rc4` source/tag is a prerelease; it is not the stable install. Pin exact versions in automation.

Next: [manage tasks](#task-workflow), [use native Records](#native-records), or read the [storage contract](docs/git-native-storage.md).

## Capabilities

| Need | Public surface | Boundary |
| --- | --- | --- |
| Task work | `create`, `list`, `search`, `get`, `update`, `mark`, `archive`, `tags` | Archive is a status transition; there is no destructive task delete command. |
| Dependencies | `deps`, `ready`, `order` | Cycles and invalid terminal transitions fail before task/ledger writes. |
| Native Records | `record`, `task`, `wiki`, `workflow`, `artifact` | Profile rules determine allowed actions; there is no Record `remove`. |
| Legacy-file migration | `migration inventory|plan|apply|status|verify` | Copy-only, plan-bound, per-item status; source deletion is never performed. |
| History | `history ID`; native profile history | Current state and append-only history are separate canonical artifacts. |
| Cold records | `archive-search`, `archive-pack plan|create|doctor` | Normal discovery is hot-only. Pack creation is explicit, revision-bound maintenance. |
| Local reads | `host` | Read-only; loopback by default; no write, workflow-execution, or remote-fetch API. |
| Cross-project routing | `project add|list|remove`, `--project ALIAS` | Disabled by default; the destination wrapper and policy remain authoritative. |
| Integrity | `doctor`, rebuildable `cache` | Markdown/ledgers are canonical; SQLite is disposable. |

Use `yylo-ledger --help` and `yylo-ledger COMMAND --help` as the exact command inventory for your installed version.

## Task workflow

### Create and inspect

```bash
yylo-ledger create --body "Implement OAuth callback validation" --status todo --tags backend security
yylo-ledger list --status todo in_progress --sort asc
yylo-ledger search --status todo --tags backend --format json
yylo-ledger tags --status todo in_progress --format table
yylo-ledger get TASK_ID
```

Replace `TASK_ID` with the ID returned by `create`. For rich Markdown or shell-sensitive text, use file/stdin transport rather than inline shell arguments:

```bash
yylo-ledger create --body-file task.md --status todo --tags backend
printf '%s\n' 'Completed focused tests.' | \
  yylo-ledger mark done --id TASK_ID --response-file - --commit COMMIT_SHA
```

`COMMIT_SHA` is a placeholder for the commit that delivered the work.

### Update status and dependencies

```bash
yylo-ledger mark in_progress --id TASK_ID --response "Started implementation"
yylo-ledger update TASK_ID --response "Callback validation is covered"
yylo-ledger deps add --id DEPENDENT_ID --blocked-by BLOCKER_ID
yylo-ledger ready --sort asc
yylo-ledger order --scores
yylo-ledger mark done --id TASK_ID --response "Implemented and tested" --commit COMMIT_SHA
```

Inline body relations are also supported:

```text
[blocked_by]BLOCKER_ID[/blocked_by]
[task_id]RELATED_ID[/task_id]
```

A blocker must exist and be terminal before its dependent can become `done`. Reopening a blocker that would invalidate a completed dependent is refused.

### Output for people and scripts

The default stream format is NDJSON. Select JSON, XML, or table output explicitly:

```bash
yylo-ledger search --status todo --format ndjson
yylo-ledger search --status todo --format json
yylo-ledger search --status todo --format xml
yylo-ledger search --status todo --format table
```

Use `--raw` for compact machine output and `--pretty` for human-oriented rendering. Structured output is bounded; request only the projection and limit you need.

## Native Records

The ID-first v2 API exposes general Records and typed task, wiki, workflow, and artifact profiles. The legacy flat task commands above remain the supported 0.x compatibility surface.

```bash
yylo-ledger wiki create --title Guide --file guide.md
yylo-ledger wiki get RECORD_ID --raw
yylo-ledger workflow create --title Build --file workflow.yaml
yylo-ledger workflow get RECORD_ID --validated
yylo-ledger artifact create --title Report --profile report --mode local --file report.bin
yylo-ledger record search --scope all --profile wiki --projection summary --limit 20 --format json
```

Replace `RECORD_ID` with the immutable ID returned by creation. Slugs and retained aliases resolve to that ID, and structured results identify what input was resolved.

### Safe updates and queries

Record updates are compare-and-replace operations. Use the revision/preimage controls shown by the relevant nested help:

```bash
yylo-ledger wiki update --help
yylo-ledger workflow update --help
yylo-ledger artifact update --help
yylo-ledger record search --help
```

Broad search is bounded by record count and rendered bytes. Choose `--scope hot|archive|all` and `--projection metadata|summary|full` deliberately. Summary output omits payload bytes, and sensitive Records expose only safe identity metadata. Full output is an audited opt-in, not the default.

### Copy legacy wikis and artifacts into Records

Migration is preservation-first: inventory and plan receipts are fresh files outside the source root, `apply` requires an explicit Record ID or `--all`, status is saved before and after every item, and neither apply nor verify removes a source file. Wiki/workflow roots are bounded extension-based scans; Artifact inputs are explicit declarations with a closed profile and payload mode.

```bash
cat > /external/receipts/declarations.json <<'JSON'
[
  {"kind":"artifact","path":"reports/run.json","profile":"report","mode":"local","media_type":"application/json"}
]
JSON
yylo-ledger migration inventory --source-root /project \
  --wiki-root .juno_task/wiki --declarations /external/receipts/declarations.json \
  --output /external/receipts/inventory.json
yylo-ledger migration plan --source-root /project \
  --inventory /external/receipts/inventory.json --output /external/receipts/plan.json
yylo-ledger migration apply --source-root /project --plan /external/receipts/plan.json \
  --status-file /external/receipts/status.json --id RECORD_ID
yylo-ledger migration status --source-root /project --plan /external/receipts/plan.json \
  --status-file /external/receipts/status.json
yylo-ledger migration verify --source-root /project --plan /external/receipts/plan.json \
  --status-file /external/receipts/status.json
```

The immutable plan contains source path, mode, size, SHA-256, tracked Git blob (when available), assigned Record ID, profile, namespace, retention, sensitivity, relations, runtime, source HEAD, and destination binding. Exact retries reuse only Records carrying matching migration source metadata. Source, runtime, plan, status, or destination drift fails closed. Secret-like names, symlinks, runtime/log/cache/object roots, duplicate declarations, non-UTF-8 Documents, and CRLF Document payloads are rejected.

## Current state, history, and archive boundaries

Hot task state lives in stable Markdown files under `.juno_task/tasks/`; segmented hash-chained ledgers retain mutation history. The SQLite query cache can be rebuilt and is never canonical.

```bash
yylo-ledger history TASK_ID --limit 20
yylo-ledger cache rebuild
yylo-ledger doctor
```

Default `list`, `search`, `ready`, and `order` inspect hot work only. Exact `get TASK_ID` and `history TASK_ID` can resolve either verified hot state or one immutable cold pack.

### Owner-authorized cold archive maintenance

Archival is never automatic. The repository and index must be clean, and plan/create reports must be durable new paths outside the repository:

```bash
yylo-ledger archive-pack plan --status done archive --older-than 90d \
  --max-tasks 1000 --target-bytes 26214400 --hard-max-bytes 47185920 \
  --report /external/receipts/archive-plan.json
# Independently review the plan before any mutation.
yylo-ledger archive-pack create --plan /external/receipts/archive-plan.json \
  --report /external/receipts/archive-create.json
yylo-ledger archive-pack doctor
yylo-ledger doctor
yylo-ledger archive-search --tag backend --before 2026-01-01 \
  --limit 20 --projection metadata
```

`/external/receipts/...` is illustrative and must be replaced with an owner-approved path outside the repository. A stale plan or task/worktree conflict fails closed: resolve it and make a new plan. Never edit or append sealed packs/manifests, restore an archived ID, or automate production archival. Create a new related hot task for follow-up work. Archive, push, deploy, and post-deploy checks are separate authorities.

## Read-only Record host

Serve bounded projections to local tools without exposing mutation or execution APIs:

```bash
yylo-ledger host --host 127.0.0.1 --port 8765 --access-policy local
```

Routes include `/record/ID`, `/record/ID/history`, `/wiki/ID`, `/workflow/ID`, and `/artifact/ID`. A non-loopback bind requires `--access-policy private`. External artifact redirects require an exact repeated `--allow-redirect-host HOST` approval and HTTPS. Traversal, symlinks, unsafe redirects, malformed archive truth, and unbounded ranges fail closed.

## Opt-in cross-project routing

A source project must enable the registry and allow each alias in `.juno_task/config.json`:

```json
{
  "kanbanRegistry": {
    "enabled": true,
    "allowedProjects": ["service-api"]
  }
}
```

Then register an initialized destination and route explicitly:

```bash
yylo-ledger project add service-api --path /absolute/path/to/service-api
yylo-ledger --project service-api list --status todo
yylo-ledger project list
```

The paths are placeholders. Enablement without an allowlist is deny-all. Missing, malformed, disallowed, stale, or recursive routes never fall back to the source board.

## Shell completion

```bash
# Current Bash session
source <(yylo-ledger completion bash)

# Fish
mkdir -p ~/.config/fish/completions
yylo-ledger completion fish > ~/.config/fish/completions/yylo-ledger.fish
```

Use `yylo-ledger completion zsh` for Zsh and source its output from your shell configuration.

## Development

```bash
git clone https://github.com/yylo-dev/yylo-ledger.git
cd yylo-ledger
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m pytest -q
```

The repository embeds Ledger as a real submodule in the YYLO monorepo; standalone Ledger commits and the parent gitlink are separate history. Do not flatten the submodule into parent-repository files.

## Help and links

- CLI: `yylo-ledger --help`
- Storage and recovery contract: [`docs/git-native-storage.md`](docs/git-native-storage.md)
- Issues: [yylo-dev/yylo-ledger/issues](https://github.com/yylo-dev/yylo-ledger/issues)
- PyPI: [pypi.org/project/yylo-ledger](https://pypi.org/project/yylo-ledger/)

## License

MIT — see [LICENSE](LICENSE).
