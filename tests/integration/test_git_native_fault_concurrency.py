"""Fault, race, and real-Git acceptance gates for per-task storage."""
import fcntl
import json
import multiprocessing
import os
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pytest

from kanban.config import Config
from kanban.storage import ConflictError, TaskStorage


def make_storage(root: Path) -> TaskStorage:
    tasks = root / ".juno_task/tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    cfg = deepcopy(Config.DEFAULT_CONFIG)
    cfg["storage"]["base_path"] = ".juno_task/tasks"
    path = tasks / "config.json"
    if not path.exists():
        path.write_text(json.dumps(cfg), encoding="utf-8")
    loaded = Config(str(path))
    # Keep the committed fixture portable while making runtime ownership
    # absolute, so an ambient JUNO_TASK_ROOT cannot redirect test writes.
    loaded.config["storage"]["base_path"] = str(tasks)
    return TaskStorage(loaded)


@pytest.mark.parametrize("point", [
    "after_stage_0", "after_stage_1", "before_intent", "after_intent",
    "before_activate_0", "after_activate_0",
    "before_activate_1", "after_activate_1", "before_complete",
])
def test_fault_at_every_activation_boundary_restores_exact_bytes(tmp_path, monkeypatch, point):
    storage = make_storage(tmp_path)
    task = storage.create_task(id="Ab1Cd2", body="x", status="todo")
    before = {str(path.relative_to(storage.juno_root)): path.read_bytes()
              for path in storage.juno_root.rglob("*") if path.is_file()
              and "cache" not in path.parts and "locks" not in path.parts}
    monkeypatch.setenv("YYLO_LEDGER_FAULT_POINT", point)
    with pytest.raises(OSError, match="injected mutation fault"):
        storage.update_task(task.id, {"status": "in_progress"})
    monkeypatch.delenv("YYLO_LEDGER_FAULT_POINT")
    after = {str(path.relative_to(storage.juno_root)): path.read_bytes()
             for path in storage.juno_root.rglob("*") if path.is_file()
             and "cache" not in path.parts and "locks" not in path.parts}
    assert after == before
    assert storage.find_task(task.id)["status"] == "todo"
    assert storage.doctor() == []


def test_real_process_interruption_is_doctor_visible_then_exactly_recovered(tmp_path):
    storage = make_storage(tmp_path)
    task = storage.create_task(id="Ab1Cd2", body="x", status="todo")
    revision = storage.normalized_hash(storage.find_task(task.id))
    package_root = Path(__file__).parents[2]
    code = (
        "from kanban.config import Config; from kanban.storage import TaskStorage; "
        f"c=Config({str(storage.tasks_root / 'config.json')!r}); "
        f"c.config['storage']['base_path']={str(storage.tasks_root)!r}; "
        "s=TaskStorage(c); s.update_task('Ab1Cd2', {'status':'in_progress'})"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(package_root / "src")
    env["YYLO_LEDGER_CRASH_POINT"] = "after_activate_0"
    child = subprocess.run([sys.executable, "-c", code], cwd=package_root, env=env)
    assert child.returncode == 91
    failures = storage.doctor()
    assert any(item.get("diagnosis") == "abandoned_canonical_mutation" for item in failures)
    # The next writer rolls the interrupted plan back before checking the CAS.
    storage.update_task(task.id, {"status": "done"}, expected_revision=revision)
    assert storage.find_task(task.id)["status"] == "done"
    assert storage.doctor() == []


def test_ledger_append_drains_partial_os_writes(tmp_path, monkeypatch):
    storage = make_storage(tmp_path)
    import kanban.ledger as ledger_module
    real_write = ledger_module.os.write

    def partial_write(fd, payload):
        return real_write(fd, payload[:7])

    monkeypatch.setattr(ledger_module.os, "write", partial_write)
    task = storage.create_task(id="Ab1Cd2", body="partial-write-safe")
    assert storage.history(task.id, include_content=True)[0]["operation"] == "create"
    assert storage.doctor() == []


def test_concurrent_reconcile_appends_exactly_one_event(tmp_path):
    storage = make_storage(tmp_path)
    task = storage.create_task(id="Ab1Cd2", body="before")
    path = Path(storage.find_task_file(task.id))
    path.write_text(path.read_text().replace("before", "manual"), encoding="utf-8")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: storage.reconcile(), range(2)))
    assert sum(task.id in result for result in results) == 1
    assert [event["operation"] for event in storage.history(task.id)] == ["create", "reconcile"]


def test_cache_refresh_fault_cannot_change_canonical_truth(tmp_path, monkeypatch):
    storage = make_storage(tmp_path)
    task = storage.create_task(id="Ab1Cd2", body="x", status="todo")
    monkeypatch.setattr(storage.cache, "upsert", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("cache fault")))
    assert storage.update_task(task.id, {"status": "done"})
    assert storage.find_task(task.id)["status"] == "done"
    assert storage.history(task.id)[-1]["operation"] == "update"


def _hold_cache_refresh(cache_path: str, lock_path: str, ready, hold_seconds: float):
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock_handle, sqlite3.connect(cache_path, timeout=0.1) as db:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("BEGIN EXCLUSIVE")
        ready.set()
        time.sleep(hold_seconds)
        db.rollback()
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def test_read_only_query_waits_for_cache_refresh_owner_without_replacing_cache(tmp_path, monkeypatch):
    """A read-induced refresh must coordinate separately from task mutation locks.

    Without the cache-refresh lease, readers hit SQLite's short busy bound and
    can treat the live cache as stale, unlink it under the writer, or return a lock
    error. The real concurrent CLI fixture requires both searches to wait for the
    bounded refresh owner while exact get still returns canonical truth.
    """
    storage = make_storage(tmp_path)
    storage.create_task(id="Ab1Cd2", body="x", status="todo", commit_hash="abc1234")
    storage.rebuild_cache()
    lock_path = storage.juno_root / "locks" / "cache-refresh.lock"
    ready = multiprocessing.Event()
    holder = multiprocessing.Process(
        target=_hold_cache_refresh,
        args=(str(storage.cache.path), str(lock_path), ready, 1.2),
    )
    monkeypatch.setenv("YYLO_LEDGER_CACHE_TIMEOUT_SECONDS", "0.1")
    monkeypatch.setenv("YYLO_LEDGER_CACHE_REFRESH_TIMEOUT_SECONDS", "2")
    holder.start()
    processes = []
    try:
        assert ready.wait(2)
        package_root = Path(__file__).parents[2]
        env = os.environ.copy()
        env.pop("JUNO_TASK_ROOT", None)
        env.pop("JUNO_CONTROLLER_BRANCH", None)
        env["PYTHONPATH"] = str(package_root / "src")
        env["YYLO_LEDGER_CACHE_TIMEOUT_SECONDS"] = "0.1"
        env["YYLO_LEDGER_CACHE_REFRESH_TIMEOUT_SECONDS"] = "2"
        config = storage.tasks_root / "config.json"
        commands = [
            ["-f", "json", "--raw", "get", "Ab1Cd2"],
            ["-f", "json", "--raw", "search", "--commit", "abc1234", "--limit", "10"],
            ["-f", "json", "--raw", "search", "--commit", "abc1234", "--limit", "10"],
        ]
        started = time.monotonic()
        for command in commands:
            processes.append(subprocess.Popen(
                [sys.executable, "-m", "kanban.cli", "-c", str(config), *command],
                cwd=package_root, env=env, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            ))
        results = [process.communicate(timeout=3) for process in processes]
        elapsed = time.monotonic() - started
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
        holder.join(3)
        if holder.is_alive():
            holder.terminate()
            holder.join()
    assert holder.exitcode == 0
    assert elapsed >= 0.65
    assert all(process.returncode == 0 for process in processes), results
    assert all(json.loads(stdout.splitlines()[0])[0]["id"] == "Ab1Cd2" for stdout, _ in results)
    assert all("database is locked" not in stderr.lower() for _, stderr in results[1:]), results


def test_mutation_lock_wait_is_bounded_and_names_owner_without_killing_holder(tmp_path, monkeypatch):
    storage = make_storage(tmp_path)
    storage.create_task(id="Ab1Cd2", body="before", status="todo")
    lock_path = storage.juno_root / "locks" / "ab" / "ab1cd2.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("YYLO_LEDGER_LOCK_TIMEOUT_SECONDS", "0.1")
    with lock_path.open("a+b") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        holder.seek(0)
        holder.truncate()
        holder.write(b'{"pid":4242,"role":"test-producer"}')
        holder.flush()
        started = time.monotonic()
        with pytest.raises(TimeoutError, match=r"lock_wait_timeout:.*pid.*4242"):
            storage.update_task("Ab1Cd2", {"body": "should-not-write"})
        assert time.monotonic() - started < 0.75
        # The holder remains alive and owns the lock; timeout only aborts the waiter.
        holder.seek(0, 2)
        holder.write(b" ")
        holder.flush()
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
    assert storage.find_task("Ab1Cd2")["body"] == "before"


def test_different_task_parallel_updates_and_same_task_stale_writer(tmp_path):
    storage = make_storage(tmp_path)
    ids = ["Ab1Cd2", "Xy9Za8", "Qr7St6", "Uv5Wx4"]
    for task_id in ids:
        storage.create_task(id=task_id, body=task_id, status="todo")
    before = {task_id: storage.normalized_hash(storage.find_task(task_id)) for task_id in ids}
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(pool.map(lambda task_id: storage.update_task(task_id, {"status": "done"}), ids))
    assert all(storage.find_task(task_id)["status"] == "done" for task_id in ids)
    with pytest.raises(ConflictError):
        storage.update_task(ids[0], {"status": "todo"}, expected_revision=before[ids[0]])


def git(cwd: Path, *args: str, check=True):
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=check)


def test_direct_cli_storage_refuses_registered_stale_worktree_fallback(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    make_storage(repo)
    (repo / ".gitignore").write_text(".juno_task/cache/\n.juno_task/locks/\n.juno_task/transactions/\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    stale = tmp_path / "stale"
    git(repo, "worktree", "add", "-b", "feature", str(stale))
    git(stale, "config", "--local", "juno.controller.path", str(repo))
    git(stale, "config", "--local", "juno.controller.branch", "refs/heads/main")
    monkeypatch.setenv("YYLO_LEDGER_INVOCATION_ROOT", str(stale))
    with pytest.raises(ValueError, match="local fallback refused"):
        make_storage(stale).create_task(id="Ab1Cd2", body="must-not-fork")
    assert not list((stale / ".juno_task/tasks").glob("*/*.md"))
    canonical = make_storage(repo)
    canonical.create_task(id="Ab1Cd2", body="canonical")
    assert canonical.find_task("Ab1Cd2")["body"] == "canonical"


def test_real_git_worktree_merge_matrix(tmp_path, monkeypatch):
    monkeypatch.delenv("JUNO_TASK_ROOT", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    storage = make_storage(repo)
    storage.create_task(id="Ab1Cd2", body="A", status="todo")
    storage.create_task(id="Xy9Za8", body="B", status="todo")
    (repo / ".gitignore").write_text(".juno_task/cache/\n.juno_task/locks/\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")

    a, b = tmp_path / "a", tmp_path / "b"
    git(repo, "worktree", "add", "-b", "task-a", str(a))
    git(repo, "worktree", "add", "-b", "task-b", str(b))
    make_storage(a).update_task("Ab1Cd2", {"status": "done"})
    make_storage(b).update_task("Xy9Za8", {"status": "done"})
    for tree in (a, b):
        git(tree, "add", ".juno_task/tasks", ".juno_task/ledger")
        git(tree, "commit", "-m", tree.name)
    git(repo, "merge", "--no-edit", "task-a")
    git(repo, "merge", "--no-edit", "task-b")
    assert git(repo, "status", "--porcelain").stdout == ""
    assert git(repo, "ls-files", ".juno_task/cache").stdout == ""
    assert not any("ledger" in str(task) for task in make_storage(repo).read_all_tasks())
    # Status transitions retain the exact path; no rename is staged.
    task_a_changes = git(repo, "diff", "--name-status", "task-a^", "task-a").stdout
    assert "M\t.juno_task/tasks/ab/Ab1Cd2.md" in task_a_changes
    assert "R" not in task_a_changes

    # Different new IDs created in independent worktrees merge without conflicts.
    n1, n2 = tmp_path / "new-1", tmp_path / "new-2"
    git(repo, "worktree", "add", "-b", "new-1", str(n1))
    git(repo, "worktree", "add", "-b", "new-2", str(n2))
    make_storage(n1).create_task(id="Qr7St6", body="new one")
    make_storage(n2).create_task(id="Uv5Wx4", body="new two")
    for tree in (n1, n2):
        git(tree, "add", ".juno_task/tasks", ".juno_task/ledger")
        git(tree, "commit", "-m", tree.name)
    git(repo, "merge", "--no-edit", "new-1")
    git(repo, "merge", "--no-edit", "new-2")
    assert make_storage(repo).find_task("Qr7St6") and make_storage(repo).find_task("Uv5Wx4")

    c, d = tmp_path / "c", tmp_path / "d"
    git(repo, "worktree", "add", "-b", "same-c", str(c))
    git(repo, "worktree", "add", "-b", "same-d", str(d))
    make_storage(c).update_task("Ab1Cd2", {"body": "C"})
    make_storage(d).update_task("Ab1Cd2", {"body": "D"})
    for tree in (c, d):
        git(tree, "add", ".juno_task/tasks", ".juno_task/ledger")
        git(tree, "commit", "-m", tree.name)
    git(repo, "merge", "--no-edit", "same-c")
    conflict = git(repo, "merge", "--no-edit", "same-d", check=False)
    assert conflict.returncode != 0
    conflicts = git(repo, "diff", "--name-only", "--diff-filter=U").stdout.splitlines()
    assert conflicts
    assert all("Ab1Cd2" in path for path in conflicts)
    git(repo, "merge", "--abort")


def test_casefold_colliding_creates_share_a_lock(tmp_path):
    storage = make_storage(tmp_path)
    outcomes = []

    def create(task_id):
        try:
            storage.create_task(id=task_id, body=task_id)
            outcomes.append("created")
        except ValueError as exc:
            outcomes.append(str(exc))

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(create, ("Ab1Cd2", "aB1cD2")))
    assert outcomes.count("created") == 1
    assert sum("case-insensitive task ID collision" in result for result in outcomes) == 1
    assert len(list(storage.tasks_root.glob("*/*.md"))) == 1


def test_unrelated_large_task_and_ledger_bytes_are_untouched(tmp_path):
    storage = make_storage(tmp_path)
    sentinel = storage.create_task(id="Xy9Za8", body="sentinel" * 100_000)
    storage.create_task(id="Ab1Cd2", body="target", status="todo")
    sentinel_path = Path(storage.find_task_file(sentinel.id))
    ledger_paths = storage.ledger.segments(sentinel.id)
    before = (sentinel_path.read_bytes(), [path.read_bytes() for path in ledger_paths])
    storage.update_task("Ab1Cd2", {"status": "done"})
    assert (sentinel_path.read_bytes(), [path.read_bytes() for path in ledger_paths]) == before


def test_doctor_diagnoses_stale_invocation_board_without_mutating_it(tmp_path, monkeypatch):
    canonical = make_storage(tmp_path / "canonical")
    canonical.create_task(id="Ab1Cd2", body="canonical")
    stale = tmp_path / "stale" / ".juno_task" / "tasks" / "xy"
    stale.mkdir(parents=True)
    stale_file = stale / "Xy9Za8.md"
    stale_file.write_bytes(b"legacy stale bytes\n")
    monkeypatch.setenv("YYLO_LEDGER_INVOCATION_ROOT", str(tmp_path / "stale"))
    failures = canonical.doctor()
    finding = next(item for item in failures if item.get("diagnosis") == "legacy_stale_local_board")
    assert finding["canonical_authority"] == str((tmp_path / "canonical").resolve())
    assert stale_file.read_bytes() == b"legacy stale bytes\n"


def test_stale_controller_binding_refuses_before_task_or_ledger_bytes(tmp_path, monkeypatch):
    storage = make_storage(tmp_path)
    identity = storage._git_mutation_identity()
    identity["controller_head"] = "0" * 40
    monkeypatch.setenv("YYLO_LEDGER_CONTROLLER_BINDING", json.dumps(identity))
    with pytest.raises(ValueError, match="binding changed at controller_head"):
        storage.create_task(id="Ab1Cd2", body="must-not-land")
    assert not list(storage.tasks_root.glob("*/*.md"))
    assert not list((storage.juno_root / "ledger").glob("*/*/*.ndjson"))
    assert not (storage.juno_root / "transactions").exists()


def test_oversized_ledger_event_is_refused_before_current_state_write(tmp_path):
    storage = make_storage(tmp_path)
    storage.ledger.max_segment_bytes = 512
    with pytest.raises(Exception, match="ledger event.*exceeds"):
        storage.create_task(id="Ab1Cd2", body="x" * 2000)
    assert storage.find_task("Ab1Cd2") is None
    assert not storage.ledger.segments("Ab1Cd2")


def test_doctor_reports_orphan_ledger(tmp_path):
    storage = make_storage(tmp_path)
    task = storage.create_task(id="Ab1Cd2", body="x")
    path = Path(storage.find_task_file(task.id))
    path.unlink()
    failures = storage.doctor()
    assert any(item["error"] == "ledger has no canonical current task" for item in failures)
