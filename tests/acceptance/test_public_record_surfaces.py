"""Independent acceptance tests for native Record CLI, search, and HTTP surfaces."""
from __future__ import annotations

import io
import json
import subprocess
import time
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest

from yylo_ledger.artifacts import ArtifactStore
from yylo_ledger.cli import ExitCode, TaskCLI
from yylo_ledger.config import Config
from yylo_ledger.documents import DocumentStore, create_document
from yylo_ledger.hosting import HostPolicy, HostingApplication
from yylo_ledger.record_search import (
    IndexedRecord, RecordSearchIndex, RecordSearchPolicy, RecordSearchQuery,
)
from yylo_ledger.records import RecordError
from yylo_ledger.storage import TaskStorage


def run_cli(argv: list[str], stdin: str = "") -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with patch("sys.stdout", stdout), patch("sys.stderr", stderr), patch("sys.stdin", io.StringIO(stdin)):
        try:
            result = TaskCLI().run(argv)
        except SystemExit as exc:  # argparse's public --help behavior
            result = int(exc.code or 0)
    return result, stdout.getvalue(), stderr.getvalue()


def code(result: tuple[int, str, str]) -> str:
    assert result[0] == ExitCode.VALIDATION_ERROR
    return json.loads(result[2])["error"]["code"]


def storage_at(root: Path) -> TaskStorage:
    path = root / ".juno_task" / "tasks" / "config.json"
    path.parent.mkdir(parents=True)
    config = deepcopy(Config.DEFAULT_CONFIG)
    config["storage"]["base_path"] = str(path.parent)
    path.write_text(json.dumps(config), encoding="utf-8")
    return TaskStorage(Config(str(path), auto_create=False))


def test_cli_identity_action_matrix_search_parity_and_exact_refusals(tmp_path, monkeypatch):
    monkeypatch.setenv("JUNO_TASK_ROOT", str(tmp_path))
    wiki = tmp_path / "page.md"
    wiki.write_text("# Public\nacceptance needle\n", encoding="utf-8")
    workflow = tmp_path / "flow.yaml"
    workflow.write_text("schema_version: v1\nworkflow_id: release\nsteps: []\n", encoding="utf-8")
    output = tmp_path / "stdout.txt"
    output.write_text("explicit stdout capture\n", encoding="utf-8")

    created = run_cli(["wiki", "create", "--id", "Wi1Acc", "--title", "Public",
                       "--slug", "public-page", "--alias", "page-alias", "--file", str(wiki)])
    assert created[0] == 0 and json.loads(created[1])["id"] == "Wi1Acc"
    assert run_cli(["record", "create", "--kind", "document", "--profile", "workflow",
                    "--id", "Wo1Acc", "--title", "Release", "--slug", "release-flow",
                    "--file", str(workflow)])[0] == 0
    artifact = run_cli(["artifact", "create", "--id", "Ar1Acc", "--title", "stdout",
                        "--profile", "stdout", "--mode", "local", "--file", str(output),
                        "--provenance", "run_id=run-1"])
    assert artifact[0] == 0 and json.loads(artifact[1])["id"] == "Ar1Acc"

    for identity in ("Wi1Acc", "public-page", "page-alias"):
        result = run_cli(["wiki", "get", identity, "-f", "json"])
        assert result[0] == 0 and json.loads(result[1])["id"] == "Wi1Acc"

    old, new = tmp_path / "old.md", tmp_path / "new.md"
    old.write_text(wiki.read_text(), encoding="utf-8")
    new.write_text("# Public\nacceptance updated\n", encoding="utf-8")
    updated = run_cli(["record", "update", "page-alias", "--expected-revision", "1",
                       "--old-file", str(old), "--new-file", str(new)])
    assert updated[0] == 0 and json.loads(updated[1])["id"] == "Wi1Acc"
    history = [json.loads(line) for line in run_cli(["wiki", "history", "public-page"])[1].splitlines()]
    assert history and {item["id"] for item in history} == {"Wi1Acc"}

    typed = json.loads(run_cli(["wiki", "search", "--text", "updated", "-f", "json"])[1])
    generic = json.loads(run_cli(["record", "search", "--kind", "document", "--profile", "wiki",
                                  "--text", "updated", "-f", "json"])[1])
    assert [item["id"] for item in typed["records"]] == [item["id"] for item in generic["records"]] == ["Wi1Acc"]

    missing = run_cli(["wiki", "get", "missing"])
    unsafe = run_cli(["wiki", "update", "Wi1Acc", "--expected-revision", "2", "--path", "/title"])
    stale = run_cli(["wiki", "update", "Wi1Acc", "--expected-revision", "1",
                     "--old-file", str(new), "--new-file", str(old)])
    digest = run_cli(["wiki", "update", "Wi1Acc", "--expected-revision", "2",
                      "--expected-payload-digest", "0" * 64, "--new-file", str(old)])
    assert [code(item) for item in (missing, unsafe, stale, digest)] == [
        "RECORD_NOT_FOUND", "EXACT_PREIMAGE_REQUIRED", "REVISION_CONFLICT", "REVISION_CONFLICT"]
    assert "acceptance updated" not in stale[2] + digest[2]

    archived = run_cli(["wiki", "archive", "page-alias", "--expected-revision", "2"])
    assert archived[0] == 0 and json.loads(archived[1])["id"] == "Wi1Acc"
    assert json.loads(run_cli(["wiki", "get", "page-alias"])[1])["id"] == "Wi1Acc"
    assert code(run_cli(["wiki", "update", "page-alias", "--expected-revision", "2",
                         "--old-file", str(new), "--new-file", str(old)])) == "RECORD_ARCHIVED"

    actions_expected = {"create", "list", "search", "get", "update", "history", "archive"}
    for group in ("record", "task", "wiki", "workflow", "artifact"):
        help_result = run_cli([group, "--help"])
        actions = set(run_cli(["__complete", "--index", "2", "--", "yylo-ledger", group, ""])[1].splitlines())
        assert help_result[0] == 0 and "remove" not in help_result[1].casefold()
        assert actions_expected <= actions and "remove" not in actions
    assert not (tmp_path / ".juno_task" / "runtime").exists()
    assert not (tmp_path / ".juno_task" / "specs").exists()


def test_git_creation_context_dirty_true_false_filters_and_no_secret_leakage(tmp_path, monkeypatch):
    root = tmp_path / "repository"
    root.mkdir()
    for args in (("init", "-q"), ("config", "user.name", "Acceptance"),
                 ("config", "user.email", "acceptance@example.invalid")):
        subprocess.run(["git", "-C", str(root), *args], check=True)
    monkeypatch.setenv("JUNO_TASK_ROOT", str(root))

    clean_source = root / "clean.md"
    clean_source.write_text("clean payload\n", encoding="utf-8")
    # Materialize configuration and the per-ID advisory lock before committing
    # the clean baseline; lock acquisition itself must not make the fixture dirty.
    assert run_cli(["completion", "bash"])[0] == 0
    run_cli(["wiki", "list", "--limit", "1"])
    lock = root / ".juno_task" / "locks" / "documents" / "gc1acc.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.touch()
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "clean fixture"], check=True)
    subprocess.run(["git", "-C", str(root), "remote", "add", "origin",
                    "https://user:credential@example.invalid/private.git"], check=True)
    head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()

    clean = run_cli(["wiki", "create", "--id", "Gc1Acc", "--title", "Clean", "--file", str(clean_source),
                     "--require-git-role", "controller"])
    assert clean[0] == 0
    clean_repo = json.loads(clean[1])["system_metadata"]["creation_context"]["git"]["repositories"][0]
    assert clean_repo["head_sha"] == head and clean_repo["worktree_dirty"] is False

    secret_file = root / "untracked-secret-name.txt"
    secret_file.write_text("dirty secret bytes", encoding="utf-8")
    dirty_source = root / "dirty.md"
    dirty_source.write_text("dirty public payload\n", encoding="utf-8")
    dirty = run_cli(["wiki", "create", "--id", "Gd1Acc", "--title", "Dirty", "--file", str(dirty_source),
                     "--require-git-role", "controller"])
    assert dirty[0] == 0
    dirty_repo = json.loads(dirty[1])["system_metadata"]["creation_context"]["git"]["repositories"][0]
    assert dirty_repo["head_sha"] == head and dirty_repo["worktree_dirty"] is True

    for expected_id, state in (("Gc1Acc", "false"), ("Gd1Acc", "true")):
        result = run_cli(["record", "search", "--git-role", "controller", "--git-head", head,
                          "--git-dirty", state, "-f", "json"])
        assert result[0] == 0, result[2]
        assert [item["id"] for item in json.loads(result[1])["records"]] == [expected_id]
        serialized = result[1] + result[2]
        for forbidden in (str(root), "credential", "private.git", "untracked-secret-name", "dirty secret bytes"):
            assert forbidden not in serialized


def test_frontmatter_workflow_schema_and_explicit_model_artifact(tmp_path, monkeypatch):
    monkeypatch.setenv("JUNO_TASK_ROOT", str(tmp_path))
    page = tmp_path / "front.md"
    page.write_text("--- is payload\n# literal\n", encoding="utf-8")
    assert run_cli(["wiki", "create", "--id", "Fm1Acc", "--title", "Front", "--file", str(page)])[0] == 0
    exported = run_cli(["wiki", "get", "Fm1Acc", "--front-matter"])
    assert exported[0] == 0 and exported[1].startswith("---\nid: Fm1Acc\n")
    assert exported[1].endswith(page.read_text())

    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("schema_version: v1\nworkflow_id: bad\nsteps: nope\n", encoding="utf-8")
    refused = run_cli(["workflow", "create", "--id", "Wy1Acc", "--title", "Bad", "--file", str(invalid)])
    assert code(refused) == "WORKFLOW_SCHEMA_INVALID"
    assert not list((tmp_path / ".juno_task" / "documents").glob("**/Wy1Acc"))

    model = tmp_path / "model.txt"
    model.write_text("explicit model output", encoding="utf-8")
    captured = run_cli(["artifact", "create", "--id", "Mo1Acc", "--title", "Model",
                        "--profile", "model-output", "--mode", "inline", "--file", str(model)])
    payload = json.loads(captured[1])["payload"]
    assert captured[0] == 0 and payload["backend"] == "inline" and payload["size"] == len("explicit model output")
    assert not (tmp_path / ".juno_task" / "runtime").exists()


def test_search_scale_redaction_cursor_canonical_verification_and_output_budget(tmp_path):
    records = []
    for index in range(240):
        record = create_document(record_id=f"S{index:05d}", title=f"Record {index}", profile="wiki",
                                 media_type="text/markdown", text=f"search corpus team-{index % 4}",
                                 namespace="acceptance", timestamp=f"2026-08-{index % 28 + 1:02d}T00:00:00Z",
                                 custom_metadata={"com.example": {"team": index % 4}})
        records.append(record)
    secret = create_document(record_id="Se1Acc", title="api_key=title-secret", profile="wiki",
                             media_type="text/markdown", text="password=payload-secret")
    secret["system_metadata"]["classification"] = "restricted"
    records.append(secret)
    canonical = {("hot", item["id"], item["id"]): item for item in records}
    reader = lambda tier, locator, record_id: canonical[(tier, locator, record_id)]
    policy = RecordSearchPolicy(custom_metadata_paths=frozenset({"com.example.team"}),
                                max_candidates=300, max_page_size=100, max_output_bytes=32_000)
    index = RecordSearchIndex(tmp_path / "records.sqlite3", policy=policy)
    started = time.monotonic()
    index.rebuild(IndexedRecord(item, locator=item["id"]) for item in records)
    first = index.search(RecordSearchQuery(namespaces=("acceptance",), text="search corpus",
                         custom_equals={"com.example.team": 2}, created_after="2026-07-31T00:00:00Z",
                         projection="metadata", limit=30), reader)
    second = index.search(RecordSearchQuery(namespaces=("acceptance",), text="search corpus",
                          custom_equals={"com.example.team": 2}, created_after="2026-07-31T00:00:00Z",
                          projection="metadata", limit=30, cursor=first.next_cursor), reader)
    assert len(first.records) == len(second.records) == 30
    assert not ({item["id"] for item in first.records} & {item["id"] for item in second.records})
    assert first.output_bytes <= policy.max_output_bytes and time.monotonic() - started < 5

    sensitive = json.dumps(index.search(RecordSearchQuery(ids=("Se1Acc",), projection="full"), reader).records)
    assert "title-secret" not in sensitive and "payload-secret" not in sensitive
    with pytest.raises(RecordError, match="SEARCH_OUTPUT_BUDGET"):
        index.search(RecordSearchQuery(ids=("S00000",), projection="full", output_byte_budget=8), reader)
    canonical[("hot", "S00000", "S00000")]["title"] = "tampered"
    with pytest.raises(RecordError, match="SEARCH_INDEX_STALE"):
        index.search(RecordSearchQuery(ids=("S00000",)), reader)


def test_http_negotiation_security_ranges_and_read_only_boundary(tmp_path):
    storage = storage_at(tmp_path)
    documents = DocumentStore(storage.juno_root)
    documents.create(record_id="Ht1Acc", title="Hosted", profile="wiki", media_type="text/markdown",
                     text="# Safe\n<script>credential()</script>\n", slug="hosted")
    artifacts = ArtifactStore(storage.juno_root)
    artifacts.create(record_id="Ac1Acc", title="active", profile="report", mode="inline",
                     content=b"<script>active()</script>", media_type="text/html")
    artifacts.create(record_id="Rd1Acc", title="redirect", profile="report", mode="link",
                     uri="https://untrusted.example/secret", media_type="text/plain")
    app = HostingApplication(storage, HostPolicy(max_output_bytes=4096, max_range_bytes=4))
    before = {path.relative_to(storage.juno_root): path.read_bytes()
              for path in storage.juno_root.rglob("*") if path.is_file()}

    rendered = app.dispatch("GET", "/wiki/hosted", {"Accept": "text/html"})
    assert rendered.status == 200 and b"<script>" not in rendered.body and b"&lt;script&gt;" in rendered.body
    assert "default-src 'none'" in rendered.headers["Content-Security-Policy"]
    active = app.dispatch("GET", "/artifact/Ac1Acc/content", {"Range": "bytes=0-3"})
    assert active.status == 206 and active.headers["Content-Type"] == "application/octet-stream"
    assert active.headers["Content-Disposition"].startswith("attachment;")

    refusals = [
        app.dispatch("POST", "/record/Ht1Acc"),
        app.dispatch("GET", "/record/%2e%2e"),
        app.dispatch("GET", "/record/Ht1Acc?token=credential"),
        app.dispatch("GET", "/artifact/Ac1Acc/content", {"Range": "bytes=0-99"}),
        app.dispatch("GET", "/artifact/Rd1Acc/download"),
        app.dispatch("GET", "/wiki/Ht1Acc", {"Accept": "image/svg+xml"}),
    ]
    assert [item.status for item in refusals] == [405, 400, 400, 416, 403, 406]
    for item in refusals:
        assert b"credential" not in item.body and b"untrusted.example" not in item.body
    after = {path.relative_to(storage.juno_root): path.read_bytes()
             for path in storage.juno_root.rglob("*") if path.is_file()}
    assert after == before
