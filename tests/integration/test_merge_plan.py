"""Identity-bound merge preview/apply and bounded canonical mutation tests."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from kanban.config import Config
from kanban.cli import main
import kanban.merge as merge_module
from kanban.merge import TaskMerger
from kanban.storage import TaskStorage


def make_storage(root: Path) -> TaskStorage:
    tasks = root / ".juno_task" / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    config = deepcopy(Config.DEFAULT_CONFIG)
    config["storage"]["base_path"] = str(tasks)
    path = tasks / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return TaskStorage(Config(str(path)))


def canonical_hashes(root: Path):
    juno = root / ".juno_task"
    result = {}
    for kind in ("tasks", "ledger"):
        directory = juno / kind
        if not directory.exists():
            continue
        for path in sorted(item for item in directory.rglob("*") if item.is_file()):
            if path.name == "config.json":
                continue
            result[str(path.relative_to(juno))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def merge_paths(source: Path, target: Path):
    return str(source / ".juno_task"), str(target / ".juno_task")


def test_two_task_plan_changes_exactly_four_paths_on_large_target(tmp_path):
    target, source = tmp_path / "target", tmp_path / "source"
    target_storage, source_storage = make_storage(target), make_storage(source)
    for index in range(200):
        target_storage.create_task(id=f"T{index:05d}", body=f"kept-{index}", status="todo")
    source_storage.create_task(id="New001", body="first", status="todo")
    source_storage.create_task(id="New002", body="second", status="todo")
    source_path, target_path = merge_paths(source, target)
    plan_path, receipt_path = tmp_path / "merge-plan.json", tmp_path / "merge-receipt.json"
    merger = TaskMerger(target_storage.config)
    before = canonical_hashes(target)

    preview = merger.merge_files(
        [source_path], target_path, dry_run=True, plan_file=str(plan_path)
    )
    assert canonical_hashes(target) == before
    assert preview["statistics"]["tasks_kept"] == 200
    assert preview["statistics"]["tasks_added"] == 2
    assert preview["plan"]["changed_paths"] == [
        "ledger/ne/New001/000001.ndjson",
        "ledger/ne/New002/000001.ndjson",
        "tasks/ne/New001.md",
        "tasks/ne/New002.md",
    ]

    applied = merger.merge_files(
        [source_path], target_path, apply_plan=str(plan_path), receipt_file=str(receipt_path)
    )
    after = canonical_hashes(target)
    assert {path for path in after if before.get(path) != after[path]} == set(preview["plan"]["changed_paths"])
    assert all(after[path] == digest for path, digest in before.items())
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["changed_paths"] == preview["plan"]["changed_paths"]
    assert receipt["kept_ids"] == sorted(before_id for before_id in [f"T{i:05d}" for i in range(200)])
    assert applied["statistics"]["final_task_count"] == 202
    assert make_storage(target).doctor() == []


def test_apply_refuses_stale_target_before_mutation(tmp_path):
    target, source = tmp_path / "target", tmp_path / "source"
    target_storage, source_storage = make_storage(target), make_storage(source)
    target_storage.create_task(id="Keep01", body="before", status="todo")
    source_storage.create_task(id="New001", body="new", status="todo")
    source_path, target_path = merge_paths(source, target)
    plan_path, receipt_path = tmp_path / "merge-plan.json", tmp_path / "merge-receipt.json"
    merger = TaskMerger(target_storage.config)
    merger.merge_files([source_path], target_path, dry_run=True, plan_file=str(plan_path))
    target_storage.update_task("Keep01", {"body": "changed after preview"})
    before_apply = canonical_hashes(target)

    with pytest.raises(ValueError, match="merge plan is stale"):
        merger.merge_files(
            [source_path], target_path, apply_plan=str(plan_path), receipt_file=str(receipt_path)
        )
    assert canonical_hashes(target) == before_apply
    assert not receipt_path.exists()


def test_activation_fault_restores_target_bytes(tmp_path, monkeypatch):
    target, source = tmp_path / "target", tmp_path / "source"
    target_storage, source_storage = make_storage(target), make_storage(source)
    target_storage.create_task(id="Keep01", body="kept", status="todo")
    source_storage.create_task(id="New001", body="new", status="todo")
    source_path, target_path = merge_paths(source, target)
    plan_path, receipt_path = tmp_path / "merge-plan.json", tmp_path / "merge-receipt.json"
    merger = TaskMerger(target_storage.config)
    merger.merge_files([source_path], target_path, dry_run=True, plan_file=str(plan_path))
    before = canonical_hashes(target)
    monkeypatch.setenv("YYLO_LEDGER_MERGE_FAULT", "after_tasks_activation")

    with pytest.raises(RuntimeError, match="injected merge fault"):
        merger.merge_files(
            [source_path], target_path, apply_plan=str(plan_path), receipt_file=str(receipt_path)
        )
    assert canonical_hashes(target) == before
    assert not receipt_path.exists()
    assert make_storage(target).doctor() == []


def test_receipt_failure_rolls_back_applied_directories(tmp_path, monkeypatch):
    target, source = tmp_path / "target", tmp_path / "source"
    target_storage, source_storage = make_storage(target), make_storage(source)
    target_storage.create_task(id="Keep01", body="kept", status="todo")
    source_storage.create_task(id="New001", body="new", status="todo")
    source_path, target_path = merge_paths(source, target)
    plan_path, receipt_path = tmp_path / "merge-plan.json", tmp_path / "merge-receipt.json"
    merger = TaskMerger(target_storage.config)
    merger.merge_files([source_path], target_path, dry_run=True, plan_file=str(plan_path))
    before = canonical_hashes(target)
    original = merge_module._atomic_json

    def refuse_receipt(path, value):
        if Path(path) == receipt_path:
            raise OSError("injected receipt failure")
        return original(path, value)

    monkeypatch.setattr(merge_module, "_atomic_json", refuse_receipt)
    with pytest.raises(OSError, match="injected receipt failure"):
        merger.merge_files(
            [source_path], target_path, apply_plan=str(plan_path), receipt_file=str(receipt_path)
        )
    assert canonical_hashes(target) == before
    assert not receipt_path.exists()
    assert make_storage(target).doctor() == []


def test_cli_requires_and_reports_reviewed_plan(tmp_path, monkeypatch, capsys):
    target, source = tmp_path / "target", tmp_path / "source"
    make_storage(target).create_task(id="Keep01", body="kept", status="todo")
    make_storage(source).create_task(id="New001", body="new", status="todo")
    source_path, target_path = merge_paths(source, target)
    plan_path, receipt_path = tmp_path / "merge-plan.json", tmp_path / "merge-receipt.json"
    monkeypatch.delenv("JUNO_TASK_ROOT", raising=False)
    monkeypatch.chdir(target)

    assert main(["merge", source_path, "--into", target_path, "--dry-run"]) != 0
    assert "dry-run requires --plan-file" in capsys.readouterr().err
    assert main([
        "merge", source_path, "--into", target_path, "--dry-run",
        "--plan-file", str(plan_path),
    ]) == 0
    preview = capsys.readouterr().out
    assert "Plan SHA-256:" in preview
    assert "Planned changed paths: 2" in preview
    assert main([
        "merge", source_path, "--into", target_path,
        "--apply-plan", str(plan_path), "--receipt-file", str(receipt_path),
    ]) == 0
    applied = capsys.readouterr().out
    assert "Receipt SHA-256:" in applied
    assert receipt_path.exists()
    assert make_storage(target).doctor() == []


def test_change_during_staging_is_preserved_and_merge_refuses(tmp_path, monkeypatch):
    target, source = tmp_path / "target", tmp_path / "source"
    target_storage, source_storage = make_storage(target), make_storage(source)
    target_storage.create_task(id="Keep01", body="before", status="todo")
    source_storage.create_task(id="New001", body="new", status="todo")
    source_path, target_path = merge_paths(source, target)
    plan_path, receipt_path = tmp_path / "merge-plan.json", tmp_path / "merge-receipt.json"
    merger = TaskMerger(target_storage.config)
    merger.merge_files([source_path], target_path, dry_run=True, plan_file=str(plan_path))

    def concurrent_change(point):
        if point == "before_activation":
            target_storage.update_task("Keep01", {"body": "concurrent change"})

    monkeypatch.setattr(merger, "_merge_fault", concurrent_change)
    with pytest.raises(ValueError, match="became stale before activation"):
        merger.merge_files(
            [source_path], target_path, apply_plan=str(plan_path), receipt_file=str(receipt_path)
        )
    current = make_storage(target)
    assert current.find_task("Keep01")["body"] == "concurrent change"
    assert current.find_task("New001") is None
    assert not receipt_path.exists()
    assert current.doctor() == []
