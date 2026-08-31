"""Preservation, recovery, and drift contracts for native Record migration."""
import json
from pathlib import Path

import pytest

from yylo_ledger.artifacts import ArtifactStore
from yylo_ledger.documents import DocumentStore
from yylo_ledger.migration import (RecordMigration, inventory, make_plan,
                                   status_summary, write_inventory, write_plan)
from yylo_ledger.records import RecordError


def fixture(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "controller"
    receipts = tmp_path / "receipts"
    (source / "wiki").mkdir(parents=True)
    (source / "reports").mkdir()
    destination.mkdir()
    receipts.mkdir()
    (source / "wiki" / "guide.md").write_bytes("# Café\n\nexact bytes\n".encode())
    (source / "reports" / "run.json").write_bytes(b'{"ok":true}\n')
    declarations = [{"kind": "artifact", "path": "reports/run.json", "profile": "report",
                     "mode": "local", "media_type": "application/json"}]
    inv = inventory(source, declarations, wiki_roots=["wiki"])
    documents = DocumentStore(destination / ".juno_task")
    artifacts = ArtifactStore(destination / ".juno_task")
    plan = make_plan(inv, destination_root=destination, documents=documents, artifacts=artifacts)
    inventory_path = receipts / "inventory.json"
    plan_path = receipts / "plan.json"
    status_path = receipts / "status.json"
    write_inventory(inventory_path, inv, source)
    write_plan(plan_path, plan, source)
    migration = RecordMigration(source_root=source, juno_root=destination / ".juno_task")
    return source, receipts, inv, plan, plan_path, status_path, migration


def test_inventory_plan_apply_one_resume_and_verify_preserve_sources(tmp_path):
    source, _, inv, plan, plan_path, status_path, migration = fixture(tmp_path)
    assert inv["summary"] == {"total": 2, "documents": 1, "artifacts": 1, "bytes": 33}
    assert plan["deletes_source"] is False
    assert len({item["record_id"] for item in plan["items"]}) == 2
    loaded = migration.load_plan(plan_path)
    for item in loaded["items"]:
        result = migration.apply(loaded, status_path, record_ids=[item["record_id"]])
        assert result["failures"] == []
    status = migration.status(loaded, status_path)
    assert status_summary(status)["states"] == {"verified": 2}
    assert migration.verify(loaded, status_path)["ok"] is True
    wiki = next(item for item in loaded["items"] if item["kind"] == "document")
    artifact = next(item for item in loaded["items"] if item["kind"] == "artifact")
    document = migration.documents.get(wiki["record_id"])
    report = migration.artifacts.get(artifact["record_id"])
    assert document["payload"]["text"].encode() == (source / wiki["source_path"]).read_bytes()
    assert document["custom_metadata"]["migration.yylo"]["plan_sha256"] == loaded["plan_sha256"]
    assert report["system_metadata"]["migration"]["source_path"] == artifact["source_path"]
    assert (source / "wiki" / "guide.md").exists()
    assert (source / "reports" / "run.json").exists()

    retried = migration.apply(loaded, status_path, record_ids=[wiki["record_id"]])
    assert retried["applied"][0]["status"]["idempotent_reuse"] is True
    assert migration.status(loaded, status_path)["items"][wiki["record_id"]]["attempts"] == 2


def test_source_plan_destination_and_status_drift_fail_closed(tmp_path):
    source, receipts, _, plan, plan_path, status_path, migration = fixture(tmp_path)
    loaded = migration.load_plan(plan_path)
    target = loaded["items"][0]
    (source / target["source_path"]).write_bytes(b"changed\n")
    with pytest.raises(RecordError, match="MIGRATION_SOURCE_DRIFT"):
        migration.apply(loaded, status_path, record_ids=[target["record_id"]])
    assert migration.status(loaded, status_path)["items"][target["record_id"]]["state"] == "failed"

    tampered = json.loads(plan_path.read_text())
    tampered["items"][0]["title"] = "tampered"
    tampered_path = receipts / "tampered.json"
    tampered_path.write_text(json.dumps(tampered))
    with pytest.raises(RecordError, match="MIGRATION_PLAN_TAMPERED"):
        migration.load_plan(tampered_path)

    other = tmp_path / "other"
    other.mkdir()
    wrong_destination = RecordMigration(source_root=source, juno_root=other / ".juno_task")
    with pytest.raises(RecordError, match="MIGRATION_DESTINATION_DRIFT"):
        wrong_destination.load_plan(plan_path)

    status_path.write_text(json.dumps({"schema_version": "yylo_ledger_record_migration_status.v1",
                                       "plan_sha256": "0" * 64, "items": {}}))
    with pytest.raises(RecordError, match="MIGRATION_STATUS_TAMPERED"):
        migration.status(plan, status_path)


def test_inventory_rejects_symlinks_duplicates_secrets_crlf_and_runtime(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "page.md").write_text("ok\n")
    (root / ".env.deploy").write_text("TOKEN=secret\n")
    (root / "crlf.md").write_bytes(b"bad\r\n")
    link = root / "link.md"
    link.symlink_to(root / "page.md")
    (root / ".juno_task" / "runtime").mkdir(parents=True)
    (root / ".juno_task" / "runtime" / "trace.md").write_text("private\n")
    with pytest.raises(RecordError, match="MIGRATION_SOURCE_DUPLICATE"):
        inventory(root, [{"kind": "wiki", "path": "page.md"}] * 2)
    for path, code in ((".env.deploy", "MIGRATION_SECRET_REJECTED"),
                       (".juno_task/runtime/trace.md", "MIGRATION_PATH_EXCLUDED"),
                       ("link.md", "MIGRATION_PATH_UNSAFE"),
                       ("crlf.md", "DOCUMENT_PAYLOAD_INVALID")):
        with pytest.raises(RecordError, match=code):
            inventory(root, [{"kind": "wiki", "path": path}])


def test_collision_fault_recovery_and_mixed_success_are_observable(tmp_path, monkeypatch):
    source, receipts, inv, first_plan, _, status_path, migration = fixture(tmp_path)
    first = first_plan["items"][0]
    if first["kind"] == "document":
        migration.documents.create(record_id=first["record_id"], title="occupied", profile="wiki",
                                   media_type="text/markdown", text="different\n")
    else:
        migration.artifacts.create(record_id=first["record_id"], title="occupied", profile="report",
                                   mode="inline", content=b"different")
    replanned = make_plan(inv, destination_root=migration.juno_root.parent,
                          documents=migration.documents, artifacts=migration.artifacts)
    assert replanned["items"][0]["record_id"] != first["record_id"]
    plan_path = receipts / "replanned.json"
    write_plan(plan_path, replanned, source)
    loaded = migration.load_plan(plan_path)

    original = migration._create
    monkeypatch.setattr(migration, "_create", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("fault")))
    with pytest.raises(OSError, match="fault"):
        migration.apply(loaded, status_path, record_ids=[loaded["items"][0]["record_id"]])
    assert migration.status(loaded, status_path)["items"][loaded["items"][0]["record_id"]]["state"] == "failed"
    monkeypatch.setattr(migration, "_create", original)

    bad = loaded["items"][0]
    (source / bad["source_path"]).write_bytes(b"drifted\n")
    result = migration.apply(loaded, status_path, record_ids=[], all_items=True, continue_on_error=True)
    assert len(result["failures"]) == 1
    assert len(result["applied"]) == 1
    states = status_summary(result["status"])["states"]
    assert states == {"failed": 1, "verified": 1}


def test_selection_and_receipt_location_are_explicit(tmp_path):
    source, _, _, plan, plan_path, status_path, migration = fixture(tmp_path)
    loaded = migration.load_plan(plan_path)
    with pytest.raises(RecordError, match="MIGRATION_SELECTION_INVALID"):
        migration.apply(loaded, status_path, record_ids=[])
    with pytest.raises(RecordError, match="MIGRATION_SELECTION_INVALID"):
        migration.apply(loaded, status_path, record_ids=[loaded["items"][0]["record_id"]], all_items=True)
    with pytest.raises(RecordError, match="MIGRATION_RECEIPT_INSIDE_SOURCE"):
        write_inventory(source / "inventory.json", inventory(source, []), source)
