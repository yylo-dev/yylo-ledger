"""Real-Git, single-caller checkpoints and fail-closed mutation revalidation."""
import json
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from yylo_ledger.config import Config
from yylo_ledger.storage import ConflictError, TaskStorage


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          text=True, capture_output=True).stdout.strip()


@pytest.fixture
def board(tmp_path, monkeypatch):
    git(tmp_path, "init", "-b", "controller")
    git(tmp_path, "config", "user.name", "Fixture")
    git(tmp_path, "config", "user.email", "fixture@example.invalid")
    git(tmp_path, "config", "juno.controller.path", str(tmp_path))
    git(tmp_path, "config", "juno.controller.branch", "refs/heads/controller")
    monkeypatch.setenv("YYLO_LEDGER_INVOCATION_ROOT", str(tmp_path))
    monkeypatch.delenv("YYLO_LEDGER_CONTROLLER_BINDING", raising=False)
    tasks = tmp_path / ".juno_task/tasks"
    tasks.mkdir(parents=True)
    config = deepcopy(Config.DEFAULT_CONFIG)
    config["storage"]["base_path"] = str(tasks)
    path = tmp_path / ".juno_task/config.json"
    path.write_text(json.dumps(config))
    storage = TaskStorage(Config(str(path)))
    storage.create_task(id="Ab1Cd2", body="target", status="todo")
    git(tmp_path, "add", ".juno_task/config.json", ".juno_task/tasks", ".juno_task/ledger")
    git(tmp_path, "commit", "-m", "initial fixture")
    return storage


def checkpoint(storage):
    root = storage.project_root
    git(root, "add", ".juno_task/tasks", ".juno_task/ledger")
    git(root, "commit", "--allow-empty", "-m", "checkpoint metadata")


def snapshot(storage):
    return {str(path): path.read_bytes()
            for base in (storage.tasks_root, storage.juno_root / "ledger")
            for path in base.rglob("*") if path.is_file()}


def stale_after_checkpoint(storage, monkeypatch):
    expected = storage._git_mutation_identity()
    storage.create_task(id="Xy9Za8", body="unrelated checkpoint", status="todo")
    checkpoint(storage)
    raw = json.dumps(expected)
    monkeypatch.setenv("YYLO_LEDGER_CONTROLLER_BINDING", raw)
    return raw


def test_single_caller_checkpoint_revalidates_before_planning_once(board, monkeypatch):
    raw = stale_after_checkpoint(board, monkeypatch)
    revision = board.normalized_hash(board.find_task("Ab1Cd2"))
    calls = []
    verify = board._verify_controller_binding

    def strict(identity):
        calls.append(identity.copy())
        verify(identity)

    monkeypatch.setattr(board, "_verify_controller_binding", strict)
    board.update_task("Ab1Cd2", {"agent_response": "inspection persisted"}, expected_revision=revision)
    assert board.find_task("Ab1Cd2")["agent_response"] == "inspection persisted"
    assert [event["operation"] for event in board.history("Ab1Cd2")] == ["create", "update"]
    assert len(calls) == 2  # Planning and activation still check the same exact HEAD.
    assert all(identity["controller_head"] == git(board.project_root, "rev-parse", "HEAD") for identity in calls)
    assert board._mutation_binding is None
    assert __import__("os").environ["YYLO_LEDGER_CONTROLLER_BINDING"] == raw


def test_revalidation_never_replays_a_stale_task_revision(board, monkeypatch):
    old_revision = board.normalized_hash(board.find_task("Ab1Cd2"))
    expected = board._git_mutation_identity()
    board.update_task("Ab1Cd2", {"agent_response": "other writer"})
    checkpoint(board)
    monkeypatch.setenv("YYLO_LEDGER_CONTROLLER_BINDING", json.dumps(expected))
    before = snapshot(board)
    with pytest.raises(ConflictError):
        board.update_task("Ab1Cd2", {"agent_response": "stale replacement"}, expected_revision=old_revision)
    assert snapshot(board) == before


@pytest.mark.parametrize("path", [".juno_task/config.json", ".juno_task/config/task-workspace.json",
                                  ".juno_task/runtime/lease.json", "AGENTS.md",
                                  ".juno_task/tasks/config.json"])
def test_policy_runtime_and_noncanonical_metadata_movement_still_refuses(board, monkeypatch, path):
    expected = board._git_mutation_identity()
    changed = board.project_root / path
    changed.parent.mkdir(parents=True, exist_ok=True)
    changed.write_text("changed policy or authority\n")
    git(board.project_root, "add", path)
    git(board.project_root, "commit", "-m", "not a metadata-only checkpoint")
    monkeypatch.setenv("YYLO_LEDGER_CONTROLLER_BINDING", json.dumps(expected))
    before = snapshot(board)
    with pytest.raises(ValueError, match="changes extend beyond task/history"):
        board.update_task("Ab1Cd2", {"status": "done"})
    assert snapshot(board) == before


@pytest.mark.parametrize("key", ["controller_path", "controller_ref", "git_common_dir"])
def test_head_refresh_does_not_refresh_other_authority_fields(board, monkeypatch, key):
    expected = board._git_mutation_identity()
    expected[key] = "changed-authority"
    checkpoint(board)
    monkeypatch.setenv("YYLO_LEDGER_CONTROLLER_BINDING", json.dumps(expected))
    before = snapshot(board)
    with pytest.raises(ValueError, match=f"binding changed at {key}"):
        board.update_task("Ab1Cd2", {"status": "done"})
    assert snapshot(board) == before


def test_registration_change_is_not_revalidated(board, monkeypatch):
    stale_after_checkpoint(board, monkeypatch)
    git(board.project_root, "config", "juno.controller.branch", "refs/heads/not-controller")
    before = snapshot(board)
    with pytest.raises(ValueError, match="mutation authority"):
        board.update_task("Ab1Cd2", {"status": "done"})
    assert snapshot(board) == before


def test_unknown_head_is_not_refresh_authority(board, monkeypatch):
    expected = board._git_mutation_identity()
    expected["controller_head"] = "0" * 40
    monkeypatch.setenv("YYLO_LEDGER_CONTROLLER_BINDING", json.dumps(expected))
    before = snapshot(board)
    with pytest.raises(ValueError, match="unavailable or not an ancestor"):
        board.update_task("Ab1Cd2", {"status": "done"})
    assert snapshot(board) == before


def test_non_fast_forward_head_is_not_refresh_authority(board, monkeypatch):
    expected = board._git_mutation_identity()
    sibling = git(board.project_root, "commit-tree", git(board.project_root, "rev-parse", "HEAD^{tree}"),
                  "-m", "unrelated history")
    expected["controller_head"] = sibling
    monkeypatch.setenv("YYLO_LEDGER_CONTROLLER_BINDING", json.dumps(expected))
    before = snapshot(board)
    with pytest.raises(ValueError, match="not an ancestor"):
        board.update_task("Ab1Cd2", {"status": "done"})
    assert snapshot(board) == before


def test_second_head_movement_during_revalidation_refuses_without_loop(board, monkeypatch):
    stale_after_checkpoint(board, monkeypatch)
    identity = board._git_mutation_identity
    calls = []

    def moving_identity():
        calls.append(1)
        if len(calls) == 2:
            checkpoint(board)
        return identity()

    monkeypatch.setattr(board, "_git_mutation_identity", moving_identity)
    before = snapshot(board)
    with pytest.raises(ValueError, match="HEAD moved again"):
        board.update_task("Ab1Cd2", {"status": "done"})
    assert len(calls) == 2
    assert snapshot(board) == before


@pytest.mark.parametrize("change", ["head", "registration", "registration_removed", "binding"])
def test_movement_after_intent_is_never_retried(board, monkeypatch, change):
    stale_after_checkpoint(board, monkeypatch)
    before = snapshot(board)
    faults = []

    def fault(point):
        if point != "after_intent":
            return
        faults.append(point)
        if change == "head":
            checkpoint(board)
        elif change == "registration":
            git(board.project_root, "config", "juno.controller.branch", "refs/heads/elsewhere")
        elif change == "registration_removed":
            git(board.project_root, "config", "--unset", "juno.controller.branch")
            git(board.project_root, "config", "--unset", "juno.controller.path")
        else:
            monkeypatch.setenv("YYLO_LEDGER_CONTROLLER_BINDING", "{}")

    monkeypatch.setattr(board, "_mutation_fault", fault)
    with pytest.raises(ValueError):
        board.update_task("Ab1Cd2", {"status": "done"})
    assert faults == ["after_intent"]
    assert snapshot(board) == before
    assert board._mutation_binding is None


def test_git_revalidation_timeout_refuses_without_writes(board, monkeypatch):
    stale_after_checkpoint(board, monkeypatch)
    before = snapshot(board)
    run = subprocess.run

    def timeout(args, **kwargs):
        if "merge-base" in args:
            assert kwargs["timeout"] == 5
            raise subprocess.TimeoutExpired(args, 5)
        return run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(ValueError, match="bounded pre-write revalidation failed"):
        board.update_task("Ab1Cd2", {"status": "done"})
    assert snapshot(board) == before


def test_binding_input_change_during_refresh_is_not_adopted(board, monkeypatch):
    stale_after_checkpoint(board, monkeypatch)
    before = snapshot(board)
    identity = board._git_mutation_identity
    calls = []

    def changed_input():
        calls.append(1)
        if len(calls) == 2:
            monkeypatch.setenv("YYLO_LEDGER_CONTROLLER_BINDING", "{}")
        return identity()

    monkeypatch.setattr(board, "_git_mutation_identity", changed_input)
    with pytest.raises(ValueError, match="input changed during pre-write revalidation"):
        board.update_task("Ab1Cd2", {"status": "done"})
    assert snapshot(board) == before
