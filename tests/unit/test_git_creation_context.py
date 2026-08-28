"""Real-Git contracts for immutable revision-1 creation context."""
from __future__ import annotations

import json
import subprocess
from copy import deepcopy

import pytest

from yylo_ledger.config import Config
from yylo_ledger.documents import DocumentStore, create_document
from yylo_ledger.git_creation import capture_creation_context
from yylo_ledger.record_search import IndexedRecord, RecordSearchIndex, RecordSearchQuery
from yylo_ledger.records import RecordError
from yylo_ledger.storage import TaskStorage


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def repository(root, *, committed_file=True):
    root.mkdir(parents=True)
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
    if committed_file:
        (root / "tracked.txt").write_text("one\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "initial"], check=True)
    return root


def test_shared_repository_merges_roles_and_captures_branch_clean_dirty_and_detached(tmp_path):
    root = repository(tmp_path / "repo")
    context = capture_creation_context(controller_root=root, project_root=root,
                                       required_roles=("controller", "project"))
    entry = context["git"]["repositories"][0]
    assert entry == {"roles": ["controller", "project"], "head_sha": git(root, "rev-parse", "HEAD"),
                     "ref": git(root, "symbolic-ref", "HEAD"), "worktree_dirty": False}
    assert len(entry["head_sha"]) == len(git(root, "hash-object", "tracked.txt"))

    (root / "untracked-secret-name.txt").write_text("private bytes", encoding="utf-8")
    dirty = capture_creation_context(controller_root=root)["git"]["repositories"][0]
    assert dirty["worktree_dirty"] is True
    assert "untracked-secret-name" not in json.dumps(dirty)
    (root / "untracked-secret-name.txt").unlink()
    subprocess.run(["git", "-C", str(root), "checkout", "--detach", "-q"], check=True)
    detached = capture_creation_context(controller_root=root)["git"]["repositories"][0]
    assert "ref" not in detached and detached["head_sha"] == entry["head_sha"]


def test_distinct_repositories_preserve_role_ids_and_document_context_is_immutable(tmp_path):
    controller = repository(tmp_path / "controller")
    project = repository(tmp_path / "project")
    subprocess.run(["git", "-C", str(project), "commit", "--allow-empty", "-qm", "project"], check=True)
    store = DocumentStore(controller / ".juno_task", project_root=project,
                          repository_ids={"controller": "control", "project": "target"})
    created = store.create(record_id="Ab1Cd2", title="Page", profile="wiki",
                           media_type="text/markdown", text="# Page\n",
                           required_git_roles=("controller", "project"))
    entries = created["system_metadata"]["creation_context"]["git"]["repositories"]
    assert [(item["roles"], item["repository_id"], item["head_sha"]) for item in entries] == [
        (["controller"], "control", git(controller, "rev-parse", "HEAD")),
        (["project"], "target", git(project, "rev-parse", "HEAD")),
    ]
    before = created["system_metadata"]["creation_context"]
    updated = store.update("Ab1Cd2", path="/title", expected="Page", replacement="New",
                           expected_revision=1)
    assert updated["system_metadata"]["creation_context"] == before
    with pytest.raises(RecordError, match="IMMUTABLE_FIELD"):
        store.update("Ab1Cd2", path="/system_metadata/creation_context", expected=before,
                     replacement={}, expected_revision=2)
    with pytest.raises(RecordError, match="SYSTEM_METADATA_RESERVED"):
        create_document(record_id="Xy9Za8", title="Spoof", profile="wiki",
                        media_type="text/markdown", text="x",
                        system_metadata={"creation_context": before})


def test_non_git_unborn_and_strict_refusal_leave_no_record_or_history(tmp_path):
    nongit = tmp_path / "plain"; nongit.mkdir()
    ordinary = DocumentStore(nongit / ".juno_task")
    created = ordinary.create(record_id="Ab1Cd2", title="Page", profile="wiki",
                              media_type="text/markdown", text="x")
    assert created["system_metadata"]["creation_context"]["git"]["repositories"] == []

    unborn = repository(tmp_path / "unborn", committed_file=False)
    strict = DocumentStore(unborn / ".juno_task")
    with pytest.raises(RecordError) as caught:
        strict.create(record_id="Xy9Za8", title="No", profile="wiki",
                      media_type="text/markdown", text="x", required_git_roles=("controller",))
    assert caught.value.code == "GIT_CONTEXT_REQUIRED"
    assert not strict._directory("Xy9Za8").exists()
    assert not strict._event_directory("Xy9Za8").exists()


def test_strict_head_movement_race_is_atomic(tmp_path, monkeypatch):
    import yylo_ledger.git_creation as module
    root = repository(tmp_path / "race")
    store = DocumentStore(root / ".juno_task")
    original = module._git
    verifies = 0

    def moving(repo, *args):
        nonlocal verifies
        if args == ("rev-parse", "--verify", "HEAD"):
            verifies += 1
            if verifies == 2:
                subprocess.run(["git", "-C", str(root), "commit", "--allow-empty", "-qm", "moved"], check=True)
        return original(repo, *args)

    monkeypatch.setattr(module, "_git", moving)
    with pytest.raises(RecordError, match="GIT_CONTEXT_REQUIRED"):
        store.create(record_id="Ab1Cd2", title="Race", profile="wiki",
                     media_type="text/markdown", text="x", required_git_roles=("controller",))
    assert not store._directory("Ab1Cd2").exists()
    assert not store._event_directory("Ab1Cd2").exists()


def test_task_projection_receipt_and_search_index_creation_git(tmp_path):
    root = repository(tmp_path / "tasks")
    tasks = root / ".juno_task" / "tasks"; tasks.mkdir(parents=True)
    config = deepcopy(Config.DEFAULT_CONFIG)
    config["storage"]["base_path"] = str(tasks)
    config["git_creation_context"] = {"repository_ids": {"controller": "ledger"}}
    path = tasks / "config.json"; path.write_text(json.dumps(config), encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", ".juno_task/tasks/config.json"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "config"], check=True)
    storage = TaskStorage(Config(str(path)))
    task = storage.create_task(id="Ab1Cd2", body="Task", required_git_roles=("controller",))
    record = storage.get_record(task.id)
    sha = record["system_metadata"]["creation_context"]["git"]["repositories"][0]["head_sha"]
    assert sha == git(root, "rev-parse", "HEAD")
    assert record.get("commit_hash") in (None, "")

    index = RecordSearchIndex(tmp_path / "search.sqlite3")
    index.rebuild([IndexedRecord(record, tier="archive", locator="cold")])
    page = index.search(RecordSearchQuery(scope="all", creation_git={"role": "controller", "head_sha": sha}),
                        lambda tier, locator, record_id: record)
    assert [item["id"] for item in page.records] == ["Ab1Cd2"]
