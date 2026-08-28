"""Focused native Record hot-to-cold archival contracts."""
import json
from copy import deepcopy

import pytest

from yylo_ledger.archive import ArchiveFormatError
from yylo_ledger.artifacts import ArtifactStore
from yylo_ledger.config import Config
from yylo_ledger.documents import DocumentStore
from yylo_ledger.models import Task
from yylo_ledger.records import RecordError
from yylo_ledger.storage import TaskStorage


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.delenv("JUNO_TASK_ROOT", raising=False)
    tasks = tmp_path / ".juno_task" / "tasks"
    tasks.mkdir(parents=True)
    config = deepcopy(Config.DEFAULT_CONFIG)
    config["storage"]["base_path"] = str(tasks)
    path = tasks / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return TaskStorage(Config(str(path)))


def test_document_archive_preserves_alias_payload_history_and_is_immutable(tmp_path):
    store = DocumentStore(tmp_path / ".juno_task")
    record = store.create(record_id="Wi1iki", title="Guide", profile="wiki",
                          media_type="text/markdown", text="# exact\n", aliases=["old-guide"])
    updated = store.update(record["slug"], path="/payload/text", expected="# exact\n",
                           replacement="# revised\n", expected_revision=1, mode="payload",
                           expected_payload_digest=record["payload"]["sha256"])
    receipt = store.archive("old-guide", expected_revision=2)

    assert receipt["record_id"] == "Wi1iki"
    assert receipt["source_revision"].startswith("sha256:")
    cold = store.get("old-guide")
    assert cold["payload"]["text"] == "# revised\n"
    assert cold["tier"] == "cold" and cold["lifecycle"] == "archived"
    history = store.history("Wi1iki")
    assert [item["operation"] for item in history].count("snapshot") == 2
    assert history[-1]["operation"] == "archive"
    with pytest.raises(RecordError, match="archived Documents are immutable"):
        store.update("Wi1iki", path="/title", expected=updated["title"], replacement="No",
                     expected_revision=2)
    assert not list((store.records_root / "wi" / "Wi1iki").glob("*.json"))


def test_artifact_archive_keeps_verified_owned_object_and_refuses_same_id(tmp_path):
    store = ArtifactStore(tmp_path / ".juno_task")
    record = store.create(record_id="Ar1ive", title="receipt", profile="receipt",
                          mode="local", content=b"evidence", media_type="application/json",
                          retention={"class": "permanent"})
    receipt = store.archive_record("Ar1ive", expected_revision=1)

    assert receipt["owned_objects"][0]["sha256"] == record["payload"]["sha256"]
    assert store.verify("Ar1ive") == b"evidence"
    assert store.get("Ar1ive")["system_metadata"]["artifact"]["retention"]["class"] == "permanent"
    assert store.doctor() == []
    assert store.garbage_collection_plan() == []
    with pytest.raises(RecordError, match="already exists"):
        store.create(record_id="Ar1ive", title="again", profile="receipt",
                     mode="inline", content=b"x")


@pytest.mark.parametrize("boundary,cold", [
    ("before_seal", False), ("after_seal", False),
    ("after_manifest_publication", True), ("after_hot_removal", True),
    ("after_cache_refresh", True),
])
def test_archive_faults_converge_to_exactly_one_authoritative_tier(tmp_path, boundary, cold):
    store = DocumentStore(tmp_path / boundary / ".juno_task")
    store.create(record_id="Fa1ult", title="Fault", profile="wiki",
                 media_type="text/markdown", text="safe")

    def fail(point):
        if point == boundary:
            raise RuntimeError(point)

    with pytest.raises(RuntimeError, match=boundary):
        store.archive("Fa1ult", expected_revision=1, fault=fail)
    hot = bool(list((store.records_root / "fa" / "Fa1ult").glob("*.json")))
    packs = list((store.root / "archive").glob("**/pack-*.ndjson"))
    assert hot is (not cold)
    assert bool(packs) is cold
    assert bool(list((store.root / "archive-receipts").glob("**/Fa1ult.json"))) is cold
    assert store.get("Fa1ult")["tier"] == ("cold" if cold else "hot")


def test_task_record_archives_immediately_without_legacy_status_rewrite(storage):
    storage.write_task(Task(id="Ts1ask", body="active task", status="todo", slug="task-slug"))
    receipt = storage.archive_record("task-slug", expected_revision=1)
    assert receipt["kind"] == "task"
    assert storage.find_task("Ts1ask") is None
    cold = storage.get_record("task-slug")
    assert cold["status"] == "todo"  # compatibility state is preserved, not rewritten
    assert cold["tier"] == "cold" and cold["lifecycle"] == "archived"
    assert storage.history("Ts1ask", include_content=True)[-1]["operation"] == "archive"
    with pytest.raises(ValueError, match="immutable cold archive"):
        storage.update_task("Ts1ask", {"body": "no"})


def test_cold_exact_read_rejects_pack_tamper(tmp_path):
    store = DocumentStore(tmp_path / ".juno_task")
    store.create(record_id="Ta1per", title="Tamper", profile="wiki",
                 media_type="text/markdown", text="sealed")
    store.archive("Ta1per", expected_revision=1)
    pack = next((store.root / "archive").glob("**/pack-*.ndjson"))
    pack.chmod(0o644)
    payload = json.loads(pack.read_text(encoding="utf-8"))
    payload["task"]["payload"]["text"] = "changed"
    pack.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8")
    with pytest.raises(ArchiveFormatError):
        store.get("Ta1per")
