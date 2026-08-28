"""Independent aggregate storage gates for native Records.

These tests intentionally cross codec, canonical files, history, archive, cache,
real Git, and content-object boundaries.  They are acceptance tests rather than
implementation-unit tests: a failure identifies a storage invariant that needs a
separate production repair.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from yylo_ledger.artifacts import ArtifactStore
from yylo_ledger.documents import DocumentStore
from yylo_ledger.record_search import IndexedRecord, RecordSearchIndex, RecordSearchQuery
from yylo_ledger.records import RecordError


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *args], text=True, stderr=subprocess.DEVNULL
    ).strip()


def make_repository(root: Path) -> Path:
    root.mkdir(parents=True)
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Storage acceptance"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "storage@example.invalid"], check=True)
    (root / "tracked.txt").write_bytes("committed π\n".encode())
    subprocess.run(["git", "-C", str(root), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)
    return root


def canonical_bytes(root: Path) -> dict[str, bytes]:
    """Hash-bound canonical snapshot which deliberately excludes disposable state."""
    excluded = {"cache", "locks"}
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not excluded.intersection(path.relative_to(root).parts)
    }


def test_unicode_document_round_trip_archive_and_corrupt_cache_rebuild(tmp_path):
    repo = make_repository(tmp_path / "repo")
    store = DocumentStore(repo / ".juno_task", repository_ids={"controller": "acceptance"})
    text = "# 雪 🚀\n\n--- is Markdown, not front matter\n`:` and ! stay data\n"
    created = store.create(
        record_id="Do1cüm".replace("ü", "u"),
        title="雪: ! quoted",
        profile="wiki",
        media_type="text/markdown",
        text=text,
        slug="unicode-guide",
        aliases=["legacy-guide"],
        relations=[{"type": "related", "record_id": "Ta1sk2"}],
        custom_metadata={"example.audit": {"unknown": "値", "nested": [None, True, "---"]}},
        required_git_roles=("controller",),
    )
    context = created["system_metadata"]["creation_context"]
    revised = store.update(
        "legacy-guide",
        path="/payload/text",
        expected=text,
        replacement=text + "second revision\n",
        expected_revision=1,
        expected_payload_digest=created["payload"]["sha256"],
        mode="payload",
    )
    assert revised["system_metadata"]["creation_context"] == context
    assert revised["custom_metadata"] == created["custom_metadata"]

    store.archive("unicode-guide", expected_revision=2)
    cold = store.get("legacy-guide")
    assert cold["payload"]["text"].endswith("second revision\n")
    assert cold["system_metadata"]["creation_context"] == context
    assert [event["operation"] for event in store.history(cold["id"])].count("snapshot") == 2

    canonical = {("archive", "sealed", cold["id"]): cold}
    sources = lambda: [IndexedRecord(cold, tier="archive", locator="sealed")]
    index = RecordSearchIndex(tmp_path / "cache" / "records.sqlite3", record_source=sources)
    reader = lambda tier, locator, record_id: canonical[(tier, locator, record_id)]
    query = RecordSearchQuery(
        ids=[cold["id"]], scope="all",
        creation_git={"role": "controller", "head_sha": git(repo, "rev-parse", "HEAD")},
    )
    assert index.search(query, reader).records[0]["id"] == cold["id"]
    index.path.write_bytes(b"corrupt disposable sqlite")
    assert index.search(query, reader).records[0]["custom_metadata"] == created["custom_metadata"]


def test_dirty_git_context_reconstructs_head_but_not_uncommitted_bytes(tmp_path):
    repo = make_repository(tmp_path / "dirty")
    committed = (repo / "tracked.txt").read_bytes()
    (repo / "tracked.txt").write_bytes(b"uncommitted secret bytes\n")
    store = DocumentStore(repo / ".juno_task")
    record = store.create(
        record_id="Gi1rec", title="context", profile="wiki",
        media_type="text/markdown", text="head only\n",
        required_git_roles=("controller",),
    )
    entry = record["system_metadata"]["creation_context"]["git"]["repositories"][0]
    assert entry["worktree_dirty"] is True
    assert "uncommitted secret bytes" not in json.dumps(record)

    checkout = tmp_path / "checkout"
    subprocess.run(["git", "clone", "-q", "--no-local", str(repo), str(checkout)], check=True)
    subprocess.run(["git", "-C", str(checkout), "checkout", "-q", "--detach", entry["head_sha"]], check=True)
    assert (checkout / "tracked.txt").read_bytes() == committed
    assert (checkout / "tracked.txt").read_bytes() != (repo / "tracked.txt").read_bytes()


@pytest.mark.parametrize("fault_point", ["after_object", "after_manifest", "after_event"])
def test_artifact_revision_faults_preserve_canonical_bytes_and_history(tmp_path, monkeypatch, fault_point):
    store = ArtifactStore(tmp_path / ".juno_task")
    first = store.create(
        record_id="Ar1fac", title="evidence", profile="receipt",
        mode="local", content=b"first", retention={"class": "permanent"},
    )
    before = canonical_bytes(store.root)
    monkeypatch.setenv("YYLO_ARTIFACT_FAULT_POINT", fault_point)
    with pytest.raises(OSError, match="injected artifact fault"):
        store.revise(
            first["id"], expected_revision=1, expected_payload=first["payload"],
            mode="local", content=("unique-" + fault_point).encode(),
        )
    monkeypatch.delenv("YYLO_ARTIFACT_FAULT_POINT")

    assert canonical_bytes(store.root) == before
    assert store.get(first["id"])["revision"] == 1
    assert store.verify(first["id"]) == b"first"
    assert store.doctor() == []


def test_artifact_orphan_is_reported_without_endangering_retained_reference(tmp_path):
    store = ArtifactStore(tmp_path / ".juno_task")
    record = store.create(
        record_id="Re1ain", title="retained", profile="receipt", mode="local",
        content=b"retained bytes", retention={"class": "permanent", "legal_hold": True},
    )
    orphan_content = b"orphan review only"
    orphan_digest = hashlib.sha256(orphan_content).hexdigest()
    orphan = store.objects.path(orphan_digest)
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(orphan_content)

    plan = store.garbage_collection_plan()
    assert plan == [{"path": str(orphan), "sha256": orphan_digest, "size": len(orphan_content)}]
    assert any(item["path"] == str(orphan) for item in store.doctor())
    assert store.verify(record["id"]) == b"retained bytes"
    assert store.objects.path(record["payload"]["sha256"]).exists()


def test_real_process_exact_update_conflict_has_one_revision_two(tmp_path):
    root = tmp_path / ".juno_task"
    store = DocumentStore(root)
    created = store.create(
        record_id="Co1cur", title="before", profile="wiki",
        media_type="text/markdown", text="body\n",
    )
    package_root = Path(__file__).resolve().parents[2]
    program = """
import sys
from pathlib import Path
from yylo_ledger.documents import DocumentStore
from yylo_ledger.records import RecordError
store = DocumentStore(Path(sys.argv[1]))
try:
    store.update('Co1cur', path='/title', expected='before', replacement=sys.argv[2], expected_revision=1)
except RecordError as exc:
    print(exc.code)
    raise SystemExit(23)
"""
    env = {**os.environ, "PYTHONPATH": str(package_root / "src")}
    children = [
        subprocess.Popen(
            [sys.executable, "-c", program, str(root), title], cwd=package_root,
            env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        for title in ("writer-a", "writer-b")
    ]
    results = [child.communicate(timeout=10) for child in children]
    assert sorted(child.returncode for child in children) == [0, 23], results
    assert sum("REVISION_CONFLICT" in stdout for stdout, _ in results) == 1
    assert store.get(created["id"])["revision"] == 2
    assert len(list(store._directory(created["id"]).glob("*.json"))) == 2
    assert len(list(store._event_directory(created["id"]).glob("*.json"))) == 2


def test_artifact_creation_context_is_immutable_across_revision_and_archive(tmp_path):
    """Every revision and cold round-trip must retain revision-1 Git identity."""
    repo = make_repository(tmp_path / "artifact-repo")
    store = ArtifactStore(repo / ".juno_task")
    created = store.create(
        record_id="Gi1art", title="revision one", profile="receipt",
        mode="inline", content=b"one", required_git_roles=("controller",),
    )
    context = created["system_metadata"]["creation_context"]
    revised = store.revise(
        created["id"], expected_revision=1, expected_payload=created["payload"],
        mode="local", content=b"two",
    )
    assert revised["system_metadata"]["creation_context"] == context

    store.archive_record(created["id"], expected_revision=2)
    cold = store.get(created["id"])
    assert cold["system_metadata"]["creation_context"] == context
    assert store.verify(created["id"]) == b"two"
    assert store.archive_history(created["id"])[0]["record"]["system_metadata"]["creation_context"] == context
