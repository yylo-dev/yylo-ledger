# Juno Ledger Git-native storage operations

Juno Ledger's canonical current state is `.juno_task/tasks/<case-sensitive-prefix>/<ID>.md`. Segmented hash-chained history is under `.juno_task/ledger/`; `.juno_task/cache/` and `.juno_task/locks/` are ignored per-worktree state. Runtime commands never scan or write legacy `backlog.ndjson`.

Examples use the preferred `juno-ledger` executable. The legacy
`juno-kanban`, `juno-feedback`, and `kanban-juno` commands remain fully
supported, as do the `juno-kanban` distribution name and existing storage,
configuration, environment-variable, receipt, and migration identifiers.

## Conversion and rollback

Dry-run validates every row and semantic hash without activation:

```bash
juno-ledger convert .juno_task/backlog.ndjson --dry-run --report /receipts/conversion-dry-run.json
```

Historical v1 rows may omit `blocked_by` or `related_tasks`; v2 materializes either empty state as null. Conversion treats only omitted-versus-null as semantically equivalent and still preserves real dependency/link lists and strictly compares every other field. The regression fixture `test_conversion_dry_run_accepts_legacy_related_tasks_nullability` covers omitted, explicit-null, and non-empty related-task states plus the sealed dry-run receipt. It prevents a valid old board from failing at its first pre-link-schema row while enforcing one lossless current-state meaning. Validate with:

```bash
python3 -m pytest -q tests/unit/test_git_native_storage.py -k 'conversion_dry_run_accepts_legacy_related_tasks_nullability or conversion_dry_run_and_lossless_export'
python3 -m pytest -q tests/integration/test_git_native_review_contracts.py
```

Canonical task files remain LF-only. Legacy body CR/CRLF disposition is an explicit, externally receipted board-preparation decision before conversion; the converter never silently normalizes body bytes or weakens codec validation.

Production activation has no dirty/force path. It requires a clean task-storage tree, a named tag resolving to current HEAD, an external backup destination, the exact retained legacy wheel, the executing new package version, a checksummed installed-public-CLI 140k benchmark, and an external receipt path:

```bash
juno-ledger convert .juno_task/backlog.ndjson \
  --pre-cutover-tag juno-kanban-precutover-20260722 \
  --backup-path /verified-external-backups/juno \
  --legacy-package /release-assets/juno_kanban-1.42.0-py3-none-any.whl \
  --new-package-version 2.0.0 \
  --benchmark-receipt /receipts/installed-cli-140k.json \
  --report /receipts/conversion.json
```

The command itself verifies the freeze, tag, source/config identity, wheel identity, compressed NDJSON/config backup, checksum manifest, restore rehearsal, benchmark identity/gates, staging round trip, doctor, cache rebuild, active-NDJSON removal, and external report writability. It activates Markdown/config/ledger and records `cutover.json` in one cutover commit. Every pre-commit conversion fault restores the original assets.

Before any later mutation, immediate rollback accepts only that machine-generated conversion receipt. It verifies the receipt, committed cutover metadata, exact tag/parent/cutover ancestry, clean unchanged HEAD, and then creates a normal Git revert commit:

```bash
juno-ledger rollback immediate \
  --conversion-receipt /receipts/conversion.json \
  --report /receipts/immediate-rollback.json
```

After writes, rollback is executable rather than instructions-only. It freezes current writes, losslessly exports current Markdown, archives and checksums the complete task/ledger extension state outside Git, installs the exact legacy wheel into a fresh external venv without an index, and runs machine-parsed list/get/search/ready/dependency/status-summary parity. Only then does it activate NDJSON, the legacy config, exact-runtime identity, and an executable `kanban-runtime` launcher in one rollback commit. The external receipt records source/rollback commits, every artifact hash, package/entrypoint identity, and command result hash:

```bash
juno-ledger rollback post-write \
  --legacy-wheel /release-assets/juno_kanban-1.42.0-py3-none-any.whl \
  --legacy-runtime-dir /rollback/runtime-1.42.0 \
  --archive /rollback/extensions.tar.gz \
  --report /rollback/receipt.json
```

Archive/report paths must be new and outside the repository. Unsupported current fields refuse downgrade. There is no lossy force, arbitrary planned-commit, caller-parity, dual-runtime, or instructions-only path.

## Seven-day acceptance

Acceptance can be generated only after the configured end instant has actually elapsed. It requires exactly the conversion, mutation-conflict, reconciliation, cache-parity, real-worktree-merge, privacy, installed-CLI performance, and executable rollback-rehearsal artifacts. Each artifact is content-addressed; current gates must identify machine commands, exit/output hashes, current commit/config/snapshot, active window, and non-future completion time.

```bash
juno-ledger compatibility accept \
  --evidence conversion_parity=/receipts/conversion.json \
  --evidence mutation_conflicts=/receipts/mutation-conflicts.json \
  --evidence reconciliation=/receipts/reconciliation.json \
  --evidence cache_parity=/receipts/cache-parity.json \
  --evidence worktree_merges=/receipts/worktree-merges.json \
  --evidence privacy=/receipts/privacy.json \
  --evidence performance=/receipts/installed-cli-140k.json \
  --evidence rollback_rehearsal=/receipts/rollback-rehearsal.json \
  --report /receipts/seven-day-acceptance.json
juno-ledger compatibility lift \
  --acceptance-receipt /receipts/seven-day-acceptance.json \
  --report /receipts/window-lift.json
```

Lift rechecks elapsed time, current HEAD/config/current-state hash, and every bound evidence file hash. Future timestamps, caller booleans, arbitrary commits, changed evidence, and stale snapshots fail closed.

## Integrity, cache, and receipts

```bash
juno-ledger reconcile --check
juno-ledger reconcile
juno-ledger doctor
juno-ledger cache rebuild
juno-ledger history TASKID --limit 20
juno-ledger update TASKID --status done --receipt-file /receipts/update.json
```

Mutations lock one task, compare optional `--expected-revision`, atomically replace current state first, append a ledger event, verify persistence, and refresh the disposable cache. Lock waits are bounded by `JUNO_KANBAN_LOCK_TIMEOUT_SECONDS` (default 5 seconds) and timeout errors name the resource and recorded owner; no timeout kills a producer after it acquires the lock. Create/update/mark/archive can emit complete task-scoped receipts. If ledger/cache work is interrupted, canonical current state wins and the next mutation/reconcile converges.

Controller-routed writes keep an exact controller path/ref/common-directory/HEAD
binding throughout transaction planning and activation. Under the board lock,
before reading mutation inputs, a stale HEAD may be revalidated once if the old
commit is a known ancestor and the committed diff contains only canonical
`.juno_task/tasks/<shard>/<ID>.md` and
`.juno_task/ledger/<shard>/<ID>/<segment>.ndjson` files. This admits a single
caller's metadata checkpoint without retrying a write. Configuration, policy,
runtime, lease, other-path and non-fast-forward changes still refuse; an unknown
HEAD is not refresh authority. Each Git comparison is bounded to five seconds.
A second HEAD movement during revalidation refuses rather than looping.

Task revision CAS, abandoned-transaction recovery and the exact pre-activation
identity guard remain mandatory. A stale task response is never replayed after
another writer wins. Live registration is checked again before activation;
activation failures roll back as before and are never automatically retried.
The refreshed binding is scoped to one locked operation and never overwrites
inherited environment inputs. A remaining `controller_head` refusal calls for
inspection and a freshly resolved invocation, not removal of the guard.

Collection reads use SQLite only after schema/config/HEAD/working-tree freshness checks. Git changes refresh changed paths; non-Git boards compare path metadata. Cache deletion or corruption triggers canonical rebuild. SQLite waits are bounded by `JUNO_KANBAN_CACHE_TIMEOUT_SECONDS` (default 0.25 seconds). Hot exact `get` reads the identity path directly; optional dependency enrichment that cannot read the derived cache is omitted with `exact_get_enrichment_unavailable`, never allowed to hide canonical task truth. Set `JUNO_KANBAN_DIAGNOSTICS=1` for exact-get phase timings.

Opaque cursors are opt-in with `--show-cursor`; context-efficient automation should normally use `--offset`. Opted-in cursor values contain normalized last-sort key, query identity, cache-secret HMAC, and exact cache revision; they are neither offset cursors nor reusable after mutation.

## Immutable cold archive operations

The hot tier contains one current Markdown file and segmented ledger per active/recent task. An archived task instead exists exactly once in a sealed NDJSON pack; manifests, checksums, and SQLite are verified/derived indexes rather than alternate truth. Exact `get`/`history` resolve both tiers, while normal collection commands stay hot-only and `archive-search` is explicit, bounded, projected, and redacted.

An authorized operator first writes an external `archive-pack plan` receipt and independently checks its HEAD/config/policy hashes, selected revisions, 1,000-task cap, and pack estimates. `archive-pack create` requires the same clean tree/index and exact source facts, owns only selected hot paths plus new archive paths, creates one Git commit, runs archive/global doctors and retrieval parity, then writes an external receipt with verified-revert instructions. A stale plan, linked-worktree selected-task change, recovery freeze, duplicate ID, corrupt ledger, or pack/manifest mismatch fails closed. Before commit faults restore the hot tree; after commit recovery is verified Git history, never a mutable overlay.

Do not automate archival, use force/lossy flags, edit sealed files, or reopen/restore the same ID. A follow-up is a new hot task related to the archived ID. Production execution, package publishing, push/deploy, and post-deploy E2E are independent authorization boundaries.

## Query and output safety

```bash
juno-ledger search --field customer=enterprise
juno-ledger search --field-before due_date=2026-08-01
juno-ledger search --overdue
juno-ledger list --projection metadata --fields id,status,last_modified
juno-ledger list --full
juno-ledger list --limit 20 --offset 20
juno-ledger list --limit 20 --show-cursor  # opt-in; pass the emitted value with --cursor
```

Broad outputs default to bounded, configured-pattern/credential/email-redacted summaries before every renderer. Exact-ID `get` remains the full retrieval path. JSON and decoded task mappings emit known fields in the stable order `id`, `status`, `body`, `created_date`, `last_modified`, then remaining core/schema/extension fields. Canonical YAML uses the same applicable order while body/response remain in their lossless Markdown sections; YAML input remains order-independent.

V2 never reads legacy NDJSON during normal operation. If `tasks/*.ndjson` coexists with canonical V2 Markdown, `doctor` fails with the named `mixed_v1_v2_storage` diagnosis so ignored legacy state cannot silently affect operator assumptions.

## Executable gates

```bash
python3 -m pytest tests/unit/test_git_native_storage.py tests/unit/test_codec_property.py -q
python3 -m pytest tests/integration/test_git_native_fault_concurrency.py -q
python3 -m pytest tests/integration/test_git_native_review_contracts.py -q
python3 -m pytest -q
python3 scripts/benchmark_git_native.py --tasks 14000 --report /tmp/benchmark-14k.json
python3 scripts/benchmark_git_native.py --tasks 140000 --report /tmp/benchmark-140k.json
python3 scripts/verify_wheel_install.py
python3 scripts/benchmark_cold_archive.py --tasks 10000 --report evidence/cold-archive-10k.json
python3 scripts/benchmark_cold_archive.py --tasks 100000 --report evidence/cold-archive-100k.json
```

The benchmark invokes installed/public command dispatch and enforces warm get/mutation/list/search, cold rebuild time/RSS, blob, write-amplification, and ledger-output-independence gates. Rollback tests build and install a reproducible exact legacy wheel fixture, parse all parity command output, inject every conversion/rollback boundary fault, and verify canonical current truth remains recoverable and frozen.
