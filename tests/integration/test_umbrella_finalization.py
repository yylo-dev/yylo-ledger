"""Authoritative completion and receipt-bound umbrella lifecycle regressions."""
import json
from pathlib import Path

import pytest

from kanban.cli import TaskCLI
from kanban.storage import TaskStorage, UnmetBlockersError
from tests.integration.test_git_native_fault_concurrency import make_storage


def _write_receipt(path: Path, operation: str, **values) -> Path:
    payload = TaskStorage._seal_receipt({
        "receipt_version": 2,
        "operation": operation,
        "verdict": "pass",
        **values,
    })
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _admission(storage, path, umbrella_id, child_ids):
    return _write_receipt(
        path,
        "umbrella-admission",
        umbrella_id=umbrella_id,
        umbrella_revision=storage.normalized_hash(storage.find_task(umbrella_id)),
        children=[{
            "task_id": child_id,
            "owner_id": umbrella_id,
            "admitted": True,
            "expected_revision": storage.normalized_hash(storage.find_task(child_id)),
        } for child_id in child_ids],
    )


def _evidence(path, umbrella_id, commit_hash="abc1234"):
    return _write_receipt(path, "test-evidence", umbrella_id=umbrella_id,
                          commit_hash=commit_hash, checks={"tests": "pass"})


def _board_bytes(storage):
    return {str(path.relative_to(storage.juno_root)): path.read_bytes()
            for root in (storage.tasks_root, storage.juno_root / "ledger")
            if root.exists() for path in root.rglob("*") if path.is_file()}


def _sequential_board(storage):
    first = storage.create_task(id="Ch1Ld1", body="first", status="backlog")
    second = storage.create_task(id="Ch2Ld2", body="second", status="backlog",
                                 blocked_by=[first.id])
    independent = storage.create_task(id="In3Dep", body="related but independently owned",
                                      status="backlog")
    umbrella = storage.create_task(id="Um4Bre", body="umbrella", status="in_progress",
                                   blocked_by=[first.id, second.id],
                                   related_tasks=[first.id, second.id, independent.id])
    return umbrella, first, second, independent


def test_umbrella_finalize_is_recognized_by_public_cli(capsys):
    with pytest.raises(SystemExit) as exc_info:
        TaskCLI().run(["umbrella-finalize", "--help"])
    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "umbrella-finalize [-h]" in output
    assert "--admission-receipt" in output


def test_generic_done_refuses_unmet_blocker_with_zero_partial_writes(tmp_path):
    storage = make_storage(tmp_path)
    blocker = storage.create_task(id="Bl1Ckr", body="open", status="todo")
    task = storage.create_task(id="Ta2Skk", body="blocked", status="in_progress",
                               blocked_by=[blocker.id])
    before = _board_bytes(storage)
    with pytest.raises(UnmetBlockersError, match="Bl1Ckr"):
        storage.update_task(task.id, {"status": "done", "agent_response": "not yet"},
                            operation="mark")
    assert _board_bytes(storage) == before
    assert storage.find_task(task.id)["status"] == "in_progress"
    assert [event["operation"] for event in storage.history(task.id)] == ["create"]


def test_admitted_sequential_umbrella_is_atomic_and_preserves_independent_related_task(tmp_path):
    storage = make_storage(tmp_path)
    umbrella, first, second, independent = _sequential_board(storage)
    admission = _admission(storage, tmp_path / "admission.json", umbrella.id,
                           [first.id, second.id])
    evidence = _evidence(tmp_path / "evidence.json", umbrella.id)

    receipt = storage.finalize_umbrella(admission, evidence, "abc1234")

    assert receipt["replayed"] is False
    assert all(storage.find_task(task_id)["status"] == "done"
               for task_id in (umbrella.id, first.id, second.id))
    assert storage.find_task(independent.id)["status"] == "backlog"
    assert all([event["operation"] for event in storage.history(task_id)].count(
        "umbrella-finalize") == 1 for task_id in (umbrella.id, first.id, second.id))
    assert all(status == "done" for _, status in storage.dependency_info(umbrella.id)["blockers"])
    ready = storage.query_collection(filters={}, ready=True, limit=0)["tasks"]
    assert independent.id in {row["id"] for row in ready}
    assert umbrella.id not in {row["id"] for row in ready}
    assert storage.doctor() == []


def test_umbrella_replay_and_concurrent_replay_append_no_duplicate_transitions(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    storage = make_storage(tmp_path)
    umbrella, first, second, _ = _sequential_board(storage)
    admission = _admission(storage, tmp_path / "admission.json", umbrella.id,
                           [first.id, second.id])
    evidence = _evidence(tmp_path / "evidence.json", umbrella.id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(
            lambda _: storage.finalize_umbrella(admission, evidence, "abc1234"), range(2)))
    assert sorted(receipt["replayed"] for receipt in receipts) == [False, True]
    replay = storage.finalize_umbrella(admission, evidence, "abc1234")
    assert replay["replayed"] is True
    for task_id in (umbrella.id, first.id, second.id):
        assert [event["operation"] for event in storage.history(task_id)].count(
            "umbrella-finalize") == 1


def test_incomplete_child_or_activation_fault_has_zero_partial_writes(tmp_path, monkeypatch):
    storage = make_storage(tmp_path)
    external = storage.create_task(id="Ex1Ter", body="external blocker", status="todo")
    child = storage.create_task(id="Ch1Ld1", body="child", status="todo",
                                blocked_by=[external.id])
    umbrella = storage.create_task(id="Um4Bre", body="umbrella", status="in_progress",
                                   blocked_by=[child.id])
    admission = _admission(storage, tmp_path / "admission.json", umbrella.id, [child.id])
    evidence = _evidence(tmp_path / "evidence.json", umbrella.id)
    before = _board_bytes(storage)
    with pytest.raises(UnmetBlockersError, match="Ex1Ter"):
        storage.finalize_umbrella(admission, evidence, "abc1234")
    assert _board_bytes(storage) == before

    storage.update_task(external.id, {"status": "done"})
    admission = _admission(storage, tmp_path / "admission-2.json", umbrella.id, [child.id])
    before = _board_bytes(storage)
    monkeypatch.setenv("YYLO_LEDGER_FAULT_POINT", "after_activate_2")
    with pytest.raises(OSError, match="injected mutation fault"):
        storage.finalize_umbrella(admission, evidence, "abc1234")
    monkeypatch.delenv("YYLO_LEDGER_FAULT_POINT")
    assert _board_bytes(storage) == before
    assert storage.find_task(child.id)["status"] == "todo"
    assert storage.find_task(umbrella.id)["status"] == "in_progress"
    assert storage.doctor() == []
