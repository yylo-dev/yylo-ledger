"""Focused Wave 1 tests for native Record identity and exact replacement."""
import hashlib
import json
from copy import deepcopy

import pytest

from yylo_ledger.config import Config
from yylo_ledger.models import Task
from yylo_ledger.records import (
    RecordError, RevisionProvenance, default_slug, exact_replace,
    task_record_projection, validate_record,
)
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


def canonical_bytes(storage, task_id):
    path = storage.task_path(task_id)
    ledgers = storage.ledger.segments(task_id)
    return path.read_bytes(), [item.read_bytes() for item in ledgers]


def test_legacy_task_projects_complete_native_envelope_without_rewrite(storage):
    storage.write_task(Task(id="Ab1Cd2", body="# Ship Records", status="todo",
                            related_tasks=["Xy9Za8"], blocked_by=["Qr7St6"]))
    before = storage.task_path("Ab1Cd2").read_bytes()
    record = storage.get_record("Ab1Cd2")
    validate_record(record)
    assert record["id"] == "Ab1Cd2"
    assert record["slug"] == default_slug("Ab1Cd2", "Ship Records")
    assert record["kind"] == "task"
    assert record["revision"] == 1
    assert record["payload"] == {"backend": "inline", "field": "body"}
    assert record["relations"] == [
        {"type": "related", "record_id": "Xy9Za8"},
        {"type": "blocked_by", "record_id": "Qr7St6"},
    ]
    assert storage.task_path("Ab1Cd2").read_bytes() == before


def test_projection_preserves_unknown_namespaced_metadata():
    task = Task(id="Ab1Cd2", body="Task", custom_metadata={"acme.example": {"rank": 7}},
                future_extension={"kept": True})
    projected = task_record_projection(task.to_dict())
    assert projected["custom_metadata"] == {"acme.example": {"rank": 7}}
    assert projected["future_extension"] == {"kept": True}


def test_pure_exact_replace_is_type_sensitive_and_substrings_are_unambiguous():
    record = {"status": 1, "body": "one two one"}
    with pytest.raises(RecordError, match="EXACT_MATCH_NOT_FOUND"):
        exact_replace(record, path="/status", expected=True, replacement=2)
    with pytest.raises(RecordError, match="EXACT_MATCH_AMBIGUOUS"):
        exact_replace(record, path="/body", expected="one", replacement="x", mode="substring")
    assert record == {"status": 1, "body": "one two one"}
    nested = {"metadata": {"enabled": 1}}
    with pytest.raises(RecordError, match="EXACT_MATCH_NOT_FOUND"):
        exact_replace(nested, path="/metadata", expected={"enabled": True},
                      replacement={"enabled": False})
    with pytest.raises(RecordError, match="EXACT_MATCH_AMBIGUOUS"):
        exact_replace({"body": "aaa"}, path="/body", expected="aa",
                      replacement="x", mode="substring")


def test_exact_replace_records_revision_match_digests_and_provenance(storage):
    storage.write_task(Task(id="Ab1Cd2", body="alpha beta", status="todo"))
    receipt = storage.exact_replace_record(
        "Ab1Cd2", path="/body", expected="alpha", replacement="gamma",
        expected_revision=1, mode="substring",
        provenance=RevisionProvenance(actor_type="agent", agent="pi", model="gpt-test",
                                      session_id="session-1", run_id="run-1",
                                      invocation_id="invocation-1"),
    )
    assert receipt.task_id == "Ab1Cd2"
    record = storage.get_record("Ab1Cd2")
    assert record["body"] == "gamma beta"
    assert record["revision"] == 2
    event = storage.ledger.latest("Ab1Cd2")
    assert event["operation"] == "exact-replace"
    assert event["revision"] == 2
    assert event["exact_match"]["path"] == "/body"
    assert event["exact_match"]["mode"] == "substring"
    assert event["exact_match"]["before_sha256"] != event["exact_match"]["after_sha256"]
    assert event["provenance"]["agent"] == "pi"
    assert event["provenance"]["invocation_id"] == "invocation-1"


def test_stale_missing_ambiguous_and_digest_refusals_change_no_bytes(storage):
    storage.write_task(Task(id="Ab1Cd2", body="same same", status="todo"))
    before = canonical_bytes(storage, "Ab1Cd2")
    cases = [
        dict(path="/status", expected="backlog", replacement="done", expected_revision=1),
        dict(path="/body", expected="same", replacement="x", expected_revision=1, mode="substring"),
        dict(path="/body", expected="same same", replacement="x", expected_revision=2, mode="payload"),
        dict(path="/body", expected="same same", replacement="x", expected_revision=1,
             expected_digest="0" * 64, mode="payload"),
    ]
    codes = ["EXACT_MATCH_NOT_FOUND", "EXACT_MATCH_AMBIGUOUS", "REVISION_CONFLICT", "REVISION_CONFLICT"]
    for kwargs, code in zip(cases, codes):
        with pytest.raises(RecordError) as caught:
            storage.exact_replace_record("Ab1Cd2", **kwargs)
        assert caught.value.code == code
        assert canonical_bytes(storage, "Ab1Cd2") == before


def test_whole_payload_preimage_and_digest_are_utf8_byte_exact(storage):
    body = "café\n"
    storage.write_task(Task(id="Ab1Cd2", body=body, status="todo"))
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    storage.exact_replace_record("Ab1Cd2", path="/body", expected=body,
                                 replacement="日本語\n", expected_revision=1,
                                 expected_digest=digest, mode="payload")
    assert storage.get_record("Ab1Cd2")["body"] == "日本語\n"


def test_slug_rename_retains_alias_and_both_resolve_to_id(storage):
    storage.write_task(Task(id="Ab1Cd2", body="Original title", status="todo"))
    old_slug = storage.get_record("Ab1Cd2")["slug"]
    storage.exact_replace_record(old_slug, path="/slug", expected=old_slug,
                                 replacement="team-record", expected_revision=1)
    assert storage.resolve_record_id("team-record") == "Ab1Cd2"
    assert storage.resolve_record_id(old_slug) == "Ab1Cd2"
    assert storage.get_record("Ab1Cd2")["aliases"] == [old_slug]


def test_slug_collision_and_ambiguous_legacy_alias_fail_closed(storage):
    storage.write_task(Task(id="Ab1Cd2", body="First", status="todo", slug="shared"))
    storage.write_task(Task(id="Xy9Za8", body="Second", status="todo", aliases=["legacy"]))
    storage.write_task(Task(id="Qr7St6", body="Third", status="todo", aliases=["legacy"]))
    before = canonical_bytes(storage, "Xy9Za8")
    with pytest.raises(RecordError) as conflict:
        storage.exact_replace_record("Xy9Za8", path="/slug",
                                     expected=storage.get_record("Xy9Za8")["slug"],
                                     replacement="shared", expected_revision=1)
    assert conflict.value.code == "SLUG_CONFLICT"
    assert canonical_bytes(storage, "Xy9Za8") == before
    with pytest.raises(RecordError) as ambiguous:
        storage.resolve_record_id("legacy")
    assert ambiguous.value.code == "RECORD_IDENTITY_AMBIGUOUS"


def test_fault_after_snapshot_activation_rolls_back_snapshot_and_event(storage, monkeypatch):
    storage.write_task(Task(id="Ab1Cd2", body="before", status="todo"))
    before = canonical_bytes(storage, "Ab1Cd2")

    def fail(point):
        if point == "after_activate_0":
            raise OSError("injected exact-replace interruption")

    monkeypatch.setattr(storage, "_mutation_fault", fail)
    with pytest.raises(OSError, match="injected"):
        storage.exact_replace_record("Ab1Cd2", path="/body", expected="before",
                                     replacement="after", expected_revision=1, mode="payload")
    assert canonical_bytes(storage, "Ab1Cd2") == before


def test_id_kind_and_slug_relations_are_rejected_without_mutation(storage):
    storage.write_task(Task(id="Ab1Cd2", body="Task", status="todo"))
    before = canonical_bytes(storage, "Ab1Cd2")
    for path, expected, replacement in (("/id", "Ab1Cd2", "Xy9Za8"),
                                        ("/kind", "task", "document")):
        with pytest.raises(RecordError) as caught:
            storage.exact_replace_record("Ab1Cd2", path=path, expected=expected,
                                         replacement=replacement, expected_revision=1)
        assert caught.value.code == "IMMUTABLE_FIELD"
        assert canonical_bytes(storage, "Ab1Cd2") == before
