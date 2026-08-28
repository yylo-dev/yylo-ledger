"""Focused contracts for the Git-native benchmark corpus."""
import json
from copy import deepcopy

import pytest

from yylo_ledger.benchmark_git_native import generate_synthetic_tasks, synthetic_task_id
from yylo_ledger.config import Config
from yylo_ledger.storage import TaskStorage


def make_benchmark_storage(root):
    tasks = root / ".juno_task" / "tasks"
    tasks.mkdir(parents=True)
    cfg = deepcopy(Config.DEFAULT_CONFIG)
    cfg["storage"]["base_path"] = str(tasks)
    cfg["custom_fields"] = {"due_date": {"type": "date"}}
    config_path = tasks / "config.json"
    config_path.write_text(json.dumps(cfg), encoding="utf-8")
    return TaskStorage(Config(str(config_path)))


def test_synthetic_ids_are_bounded_stable_and_unique():
    assert [synthetic_task_id(index) for index in (0, 1, 255, 13999, 139999)] == [
        "T00000", "T00001", "T000FF", "T036AF", "T222DF"
    ]
    with pytest.raises(ValueError):
        synthetic_task_id(16 ** 5)


def test_generated_corpus_uses_canonical_paths_and_survives_cold_rebuild(tmp_path):
    storage = make_benchmark_storage(tmp_path / "first")
    generate_synthetic_tasks(storage, 260)

    paths = sorted(storage.tasks_root.glob("*/*.md"))
    assert len(paths) == 260
    assert all(path == storage.task_path(path.stem) for path in paths)
    assert storage.task_path("T000FF").relative_to(storage.tasks_root).as_posix() == "t0/T000FF.md"
    original_bytes = [path.read_bytes() for path in paths]

    # Rebuild reads every canonical file with strict ID/path verification.
    assert storage.rebuild_cache() == 260
    assert storage.find_task_exact("T00001")["body"].endswith("1")
    assert len(list(storage.read_all_tasks())) == 260
    assert {task["id"] for task in storage.query_fields(
        field_equals={"due_date": "2026-08-01"}
    )} == {"T00000", "T0001C", "T00038", "T00054", "T00070",
           "T0008C", "T000A8", "T000C4", "T000E0", "T000FC"}

    storage.update_task("T00001", {"status": "done"})
    assert storage.find_task_exact("T00001")["status"] == "done"

    other = make_benchmark_storage(tmp_path / "second")
    generate_synthetic_tasks(other, 260)
    assert [path.read_bytes() for path in sorted(other.tasks_root.glob("*/*.md"))] == original_bytes
