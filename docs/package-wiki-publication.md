# Package-owned wiki publication

The native Ledger API can **stage** package-owned wiki revisions. Publication is
not controller-generation activation, package authentication, package release,
or authorization to replace live instructions. The generation owner must first
authenticate the release artifact and later bind the complete publication receipt
inside its authenticated transition. Do not replace that boundary with an
unverified manifest hash or a mutable `latest` lookup.

```text
Authenticated release inputs
          |
          v
Explicit manifest --plan-package--> exact CAS plan
          |                              |
          +----------publish-package-----+
                         |
                         v
               Immutable wiki revisions
                         |
                         v
               Complete pinned receipt
                         |
                generation admission
                (separate integration)
                         |
                         v
           Active generation reads exact revisions

Rollback: select the previous receipt; preserve all staged revisions.
```

## Commands and byte contract

```sh
yy ledger wiki plan-package --manifest-file manifest.json > plan.json
yy ledger wiki publish-package --manifest-file manifest.json --plan-file plan.json > receipt.json
yy ledger wiki bind-package --manifest-file manifest.json --plan-file plan.json > binding.json
yy ledger wiki verify-package --binding-file binding.json
yy ledger wiki get RECORD_ID --revision REVISION --source
# Only after the controller generation selects its authenticated binding:
yy ledger wiki get-package controller/task_dependency_hydration.md --source
```

`yy wiki` delegates the same arguments. Save receipts outside source inputs and
verify the command exit status before treating redirected output as a receipt.
There is no filesystem-wiki fallback in these APIs.

A v1 manifest contains exactly:

```json
{
  "schema_version": "yylo_package_wiki_manifest.v1",
  "package": "@yylo/cli",
  "version": "1.0.0",
  "artifact_sha256": "<SHA256 of authenticated release artifact>",
  "entries": [
    {
      "key": "controller/task_dependency_hydration.md",
      "title": "Task dependency hydration",
      "text": "<exact LF Markdown source>",
      "sha256": "<SHA256 of UTF-8 text>"
    }
  ]
}
```

Keys must be normalized relative Markdown paths. Input limits are 512 explicitly
listed entries, 1 MiB per entry, and 8 MiB total payload. Entries are canonicalized
by key before hashing. Unknown fields, duplicate keys/ID collisions, invalid
payloads, unsafe keys and source digest mismatches fail closed. There is no
implicit directory scan or payload interpolation. Typed `record:` links must
resolve to existing Documents or another entry in the same bundle.

## Ownership and concurrency

- Stable IDs derive from package name and source key, not version or title.
- Records live in `package:<name>` namespace and carry protected system ownership,
  package/version, artifact and manifest digests, and managed revision identity.
- A project Record at a derived identity is a conflict, never an adoption request.
- Generic local edits, including title/metadata changes, block subsequent package
  publication; the edited bytes remain intact.
- Reusing a package version/artifact identity for different manifest bytes is a
  conflict. Distinct authenticated artifacts may share a semantic version; their
  explicit CAS plans still create separate revisions with distinct provenance.
- Planning is read-only. Publication locks participating IDs in deterministic
  order, checks the entire bundle before writing, and uses exact revision and
  Record digest expectations. A concurrent publisher or editor invalidates the
  stale plan rather than being overwritten.
- Removing an entry from a later manifest does not archive/delete its Record.

A complete receipt pins every key to ID, revision, Record digest, and payload
digest. `PackageWikiStore.read_binding(receipt, key)` validates and reads that
exact revision even when newer revisions exist. Missing, ambiguous, altered, or
unavailable pins fail; readers never substitute the latest revision.

## Interruption and recovery

Multi-Record publication is a resumable staging operation, not an atomic activation.
A crash can leave an immutable prefix; no complete receipt is emitted until the
whole bundle is verified. Replay the **same manifest and plan** to reuse those
exact revisions without duplicates. A hard exit between revision and history
publication is recovered only for the same package publication. Conflicting
history is preserved and refused. Finish the previous exact publication before
upgrading an entry with incomplete history.

Do not delete partially staged Records, reset revisions, manufacture receipts, or
activate a generation from a partial stdout stream. Generation integration must
retain plans/receipts and use the controller's existing admission/rollback fence.
The CLI's authenticated generation adapter performs that integration; this native
API never selects a generation or retires legacy runtime files. `get-package`
uses the generation binding (default `.juno_task/config/package-wiki.json`) and
verifies the complete pinned set before reading one page.

## Legacy classification

The existing explicit migration declaration accepts `kind: pdr` for revisable
requirements, alongside `wiki`, `workflow`, and `artifact`. PDRs are not inferred
from filenames or scanned wiki directories. Existing mapping/plan/status files
remain authoritative for replay; migration preserves source bytes. The explicit
`migration plan --reuse-plan PRIOR_PLAN` option reuses a sealed prior mapping
(e.g. the `3jBAJA` dogfood plan) after checking source/destination identities,
classifications and content. Changed sources or mapped Records conflict rather
than receive duplicate IDs. This does not execute old runtime policy. A bootstrap
approval Artifact remains evidence until a reviewed, explicit PDR promotion and
task revision-adoption step. No automatic conversion or package-ownership claim
is made over previously imported project Records.

## Focused tests

```sh
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/unit/test_package_wiki.py \
  tests/integration/test_package_wiki_cli.py \
  tests/integration/test_record_cli.py \
  tests/integration/test_record_migration_cli.py \
  tests/integration/test_pdr_cli.py
```
