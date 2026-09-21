"""Universal retrieval contracts, including operation-count performance guards."""
import io
import json
from unittest.mock import patch

import pytest

from yylo_ledger.artifacts import ArtifactStore
from yylo_ledger.cli import TaskCLI
from yylo_ledger.config import Config
from yylo_ledger.documents import DocumentStore
from yylo_ledger.models import Task, parse_related_task_ids
from yylo_ledger.record_identity import new_id, identity_kind
from yylo_ledger.records import RecordError
from yylo_ledger.storage import TaskStorage
from tests.integration.test_record_cli import run_cli


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("JUNO_TASK_ROOT", str(tmp_path))
    return tmp_path


@pytest.mark.parametrize("group,prefix,text", [
    ("wiki", "doc_", "# Wiki\n"), ("pdr", "doc_", "# Historical PDR\n"),
    ("workflow", "doc_", "schema_version: v1\nworkflow_id: flow\nsteps: []\n"),
    ("artifact", "artifact_", "# New PDR\n"),
])
def test_generated_ids_and_universal_text(project, group, prefix, text):
    source = project / "source.txt"
    source.write_text(text)
    args = [group, "create", "--title", "Example", "--file", str(source)]
    if group == "artifact":
        args += ["--profile", "report", "--mode", "local", "--media-type", "text/markdown"]
    code, output, error = run_cli(args)
    assert code == 0, error
    record = json.loads(output)
    assert record["id"].startswith(prefix)
    for command in ("get", "show"):
        code, output, error = run_cli([command, record["id"], "-f", "json"])
        assert code == 0, error
        got = json.loads(output)[0]
        assert got["id"] == record["id"] and got["kind"] == record["kind"]
        assert got["profile"] == record["profile"] and got["revision"] == 1
        assert got["content"]["text"] == text
    assert run_cli([group, "history", record["id"]])[0] == 0
    assert run_cli(["record", "search", "--id", record["id"]])[0] == 0


def test_generated_task_and_legacy_task_shape(project):
    code, output, error = run_cli(["create", "example task"])
    assert code == 0, error
    task = json.loads(output)[0]
    assert task["id"].startswith("task_")
    code, output, error = run_cli(["get", task["id"], "--compact", "-f", "json"])
    assert code == 0, error
    assert json.loads(output)[0] == {**task, "fields": task.get("fields", {})}
    assert run_cli(["update", task["id"], "--body", "new body"])[0] == 0
    assert parse_related_task_ids(f"[task_id]{task['id']}[/task_id]") == [task["id"]]
    assert parse_related_task_ids("[task_id]doc_Ab1Cd2[/task_id]") == []


@pytest.mark.parametrize("kind", ["task", "document", "artifact"])
def test_generation_kind(kind):
    assert identity_kind(new_id(kind)) == kind


@pytest.mark.parametrize("record_id", ["Ab1Cd2", "doc_Ab1Cd2"])
def test_legacy_and_prefixed_document_exact_read_never_scans(project, record_id):
    store = DocumentStore(project / ".juno_task")
    store.create(record_id=record_id, title="Example", profile="wiki", media_type="text/markdown", text="ok")
    with patch.object(DocumentStore, "resolve", side_effect=AssertionError("identity scan")):
        assert run_cli(["get", record_id])[0] == 0


def test_prefixed_artifact_hit_and_miss_only_visit_artifact_store(project):
    store = ArtifactStore(project / ".juno_task")
    store.create(record_id="artifact_Ab1Cd2", title="Report", profile="report", mode="inline",
                 content=b"ok", media_type="text/plain")
    with patch.object(TaskStorage, "get_record", side_effect=AssertionError("task probe")), \
         patch.object(DocumentStore, "get", side_effect=AssertionError("document probe")), \
         patch.object(ArtifactStore, "_resolve_archive_id", side_effect=AssertionError("artifact scan")):
        assert run_cli(["get", "artifact_Ab1Cd2"])[0] == 0
        code, _, error = run_cli(["get", "artifact_Zz9Zz9"])
        assert code != 0 and "RECORD_NOT_FOUND" in error


@pytest.mark.parametrize("identity", ["pdr_Ab1Cd2", "doc_bad", "artifact_123456"])
def test_malformed_prefix_errors(project, identity):
    code, _, error = run_cli(["get", identity])
    assert code != 0 and "RECORD_ID_INVALID" in error


def test_kind_mismatch_and_ambiguous_old_identity(project):
    documents = DocumentStore(project / ".juno_task")
    with pytest.raises(RecordError, match="RECORD_KIND_MISMATCH"):
        documents.create(record_id="artifact_Ab1Cd2", title="Wrong", profile="wiki", media_type="text/markdown", text="x")
    documents.create(record_id="Ab1Cd2", title="Doc", profile="wiki", media_type="text/markdown", text="x")
    ArtifactStore(project / ".juno_task").create(record_id="Ab1Cd2", title="Artifact", profile="report", mode="inline", content=b"x", media_type="text/plain")
    code, _, error = run_cli(["get", "Ab1Cd2"])
    assert code != 0 and "RECORD_IDENTITY_AMBIGUOUS" in error


@pytest.mark.parametrize("mode,media_type,content,reason", [
    ("local", "text/plain", b"x" * 65537, "too_large"),
    ("inline", "application/octet-stream", b"\x00\xff", "binary"),
    ("local", "text/plain", b"\xff", "non_utf8"),
])
def test_omitted_payloads_and_explicit_bytes(project, mode, media_type, content, reason):
    store = ArtifactStore(project / ".juno_task")
    store.create(record_id="artifact_Ab1Cd2", title="Report", profile="report", mode=mode,
                 content=content, media_type=media_type)
    code, output, error = run_cli(["get", "artifact_Ab1Cd2"])
    assert code == 0, error
    got = json.loads(output)
    assert got["content"]["reason"] == reason and "data" not in got["payload"]
    target = io.BytesIO()
    stdout = io.TextIOWrapper(target, encoding="utf-8")
    with patch("sys.stdout", stdout):
        code = TaskCLI().run(["get", "artifact_Ab1Cd2", "--content", "--max-content-bytes", "100000"])
    stdout.flush()
    assert code == 0 and target.getvalue() == content


def test_external_never_downloads(project):
    ArtifactStore(project / ".juno_task").create(record_id="artifact_Ab1Cd2", title="External", profile="report",
        mode="external", uri="https://example.org/report", digest="a" * 64, size=12, media_type="text/plain")
    code, output, _ = run_cli(["get", "artifact_Ab1Cd2"])
    assert code == 0 and json.loads(output)["content"]["reason"] == "external_reference"
    code, _, error = run_cli(["get", "artifact_Ab1Cd2", "--content"])
    assert code != 0 and "CONTENT_UNAVAILABLE" in error


def test_corrupt_local_payload_and_untrusted_size_are_bounded(project):
    store = ArtifactStore(project / ".juno_task")
    record = store.create(record_id="artifact_Ab1Cd2", title="Report", profile="report", mode="local",
                          content=b"ok", media_type="text/plain")
    path = store.objects.path(record["payload"]["sha256"])
    path.write_bytes(b"NO")
    code, _, error = run_cli(["get", record["id"]])
    assert code != 0 and "ARTIFACT_DIGEST_MISMATCH" in error
    path.write_bytes(b"x" * 70000)
    code, _, error = run_cli(["get", record["id"]])
    assert code != 0 and "CONTENT_TOO_LARGE" in error


def test_document_limit_and_multi_id_content_error(project):
    DocumentStore(project / ".juno_task").create(record_id="doc_Ab1Cd2", title="Long", profile="wiki",
        media_type="text/markdown", text="x" * 65537)
    code, output, _ = run_cli(["get", "doc_Ab1Cd2"])
    assert code == 0
    result = json.loads(output)
    assert result["content"]["reason"] == "too_large" and "text" not in result["payload"]
    assert run_cli(["get", "doc_Ab1Cd2", "Ab1Cd2", "--content"])[0] != 0
    assert run_cli(["get", "doc_Ab1Cd2", "--max-content-bytes", "-1"])[0] != 0


@pytest.mark.parametrize("kind,prefix", [("document", "doc_"), ("artifact", "artifact_"), ("task", "task_")])
@pytest.mark.parametrize("prefixed", [False, True])
def test_exact_cold_get_and_history(project, kind, prefix, prefixed):
    record_id = (prefix if prefixed else "") + "Ab1Cd2"
    if kind == "document":
        store = DocumentStore(project / ".juno_task")
        store.create(record_id=record_id, title="Cold", profile="wiki", media_type="text/markdown", text="cold")
        store.archive(record_id, expected_revision=1)
    elif kind == "artifact":
        store = ArtifactStore(project / ".juno_task")
        store.create(record_id=record_id, title="Cold", profile="report", mode="local", content=b"cold", media_type="text/plain")
        store.archive_record(record_id, expected_revision=1)
    else:
        store = TaskStorage(Config())
        store.write_task(Task(id=record_id, body="cold"))
        store.archive_record(record_id, expected_revision=1)
    with patch("yylo_ledger.archive.iter_archive_envelopes", side_effect=AssertionError("whole archive scan")):
        code, output, error = run_cli(["get", record_id, "--compact", "-f", "json"])
    assert code == 0, error
    assert json.loads(output)[0]["id"] == record_id
    assert run_cli(["record", "history", record_id])[0] == 0


def test_prefixed_document_update_and_relations(project):
    store = DocumentStore(project / ".juno_task")
    record = store.create(record_id="doc_Ab1Cd2", title="Links", profile="wiki", media_type="text/markdown",
                          text="[task](record:task_Ef3Gh4)")
    updated = store.update(record["id"], path="/payload/text", expected=record["payload"]["text"],
                           replacement="new", expected_revision=1, mode="payload")
    assert updated["id"] == record["id"] and updated["revision"] == 2


def test_plain_alias_retained(project):
    store = DocumentStore(project / ".juno_task")
    store.create(record_id="doc_Ab1Cd2", title="Alias", profile="wiki", media_type="text/markdown",
                 text="ok", aliases=["old_guide"])
    assert run_cli(["get", "old_guide"])[0] == 0
