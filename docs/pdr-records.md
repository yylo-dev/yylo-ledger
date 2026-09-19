# Revisable PDR Records

PDRs are Markdown Documents with profile `pdr`, not immutable report Artifacts.
Use `yylo-ledger pdr` standalone or `yy ledger pdr` from a YYLO controller.
Inspect command help for exact file transport and compare-and-replace options.

```sh
yylo-ledger pdr create --title 'Service requirements' --file requirements.md
yylo-ledger pdr get RECORD_ID --source
yylo-ledger pdr get RECORD_ID --revision 1
yylo-ledger pdr search --text 'Service' --projection summary
yylo-ledger pdr update RECORD_ID --expected-revision 1 --old-file approved.md --new-file revised.md
yylo-ledger pdr history RECORD_ID
```

Each update retains the immutable Record ID and creates a new immutable revision.
Stale revisions or preimages fail without changing canonical state. Source retrieval
returns the exact approved Markdown. Typed and common Record search use the same
storage; archive is a lifecycle transition, not deletion.

## Explicit task approval

A task's `fields.pdr_binding` has exactly three keys:

```json
{"record_id":"Abc123","revision":1,"payload_sha256":"<SHA-256 from that revision's payload>"}
```

Read the PDR revision before binding. Create through the compatible task command's
`--field 'pdr_binding=JSON'`, or explicitly adopt a different binding with native
`task update --path /fields --expected-revision N --expect-file old-fields.json
--value-file new-fields.json`. Preserve all unrelated fields. An existing nested
binding can also be changed using `/fields/pdr_binding` and exact old/new JSON.

Both compatible task `update --field` and native exact updates validate the ID,
PDR profile, revision and digest before writing. Prefer the native exact update
for concurrent approval changes. Invalid adoption performs no task/history writes.
A task's approved binding does not follow the PDR's latest revision. Unrelated task
status changes preserve that accepted historical binding, including after archival.

Bootstrap report Artifacts remain immutable approval evidence. Once this profile
is available in a reviewed installed release, explicitly create the PDR and link
its source artifact by immutable Record ID. Never run unreleased source against
live storage to emulate an upgrade.
