"""Focused contracts for immutable Artifact payloads."""
import hashlib
import io

import pytest

from yylo_ledger.artifacts import ArtifactPolicy, ArtifactStore
from yylo_ledger.records import RecordError


def store(tmp_path, **policy):
    return ArtifactStore(tmp_path / ".juno_task", policy=ArtifactPolicy(**policy))


def test_all_payload_modes_round_trip_and_verify(tmp_path):
    artifacts = store(tmp_path)
    inline = artifacts.create(record_id="In1ine", title="small", profile="report",
                              mode="inline", content=b"hello", media_type="text/plain")
    local = artifacts.create(record_id="Lo1cal", title="large", profile="receipt",
                             mode="local", content=b"object", media_type="application/json")
    digest = hashlib.sha256(b"remote").hexdigest()
    external = artifacts.create(record_id="Ex1ern", title="remote", profile="report",
                                mode="external", uri="https://example.test/evidence",
                                digest=digest, size=6, content=b"remote", media_type="text/plain")
    link = artifacts.create(record_id="Li1ink", title="pointer", profile="report",
                            mode="link", uri="https://example.test/mutable", media_type="text/html")

    assert artifacts.verify(inline["id"]) == b"hello"
    assert artifacts.verify(local["id"]) == b"object"
    assert artifacts.verify(external["id"], external_resolver=lambda _: b"remote") == b"remote"
    assert artifacts.verify(link["id"]) is None
    assert link["payload"]["immutable_bytes"] is False
    assert "sha256" not in link["payload"]
    assert link["system_metadata"]["artifact"]["durable_evidence"] is False


def test_local_deduplicates_without_aliasing_identity_and_tamper_fails(tmp_path):
    artifacts = store(tmp_path)
    first = artifacts.create(record_id="Aa1aaa", title="one", profile="report",
                             mode="local", content=b"same")
    second = artifacts.create(record_id="Bb2bbb", title="two", profile="receipt",
                              mode="local", content=b"same")
    assert first["id"] != second["id"]
    assert first["payload"]["path"] == second["payload"]["path"]
    objects = [path for path in (artifacts.root / "objects").glob("**/[0-9a-f]" + "*" * 63)
               if path.is_file()]
    assert len(objects) == 1
    objects[0].write_bytes(b"evil")
    with pytest.raises(RecordError, match="ARTIFACT_(SIZE|DIGEST)_MISMATCH"):
        artifacts.verify(first["id"])
    objects[0].unlink()
    with pytest.raises(RecordError, match="ARTIFACT_OBJECT_MISSING"):
        artifacts.verify(second["id"])


def test_capture_is_explicit_bounded_and_provenance_is_narrow(tmp_path):
    artifacts = store(tmp_path, max_capture_bytes=10)
    record = artifacts.capture_stdout(record_id="Ou1put", title="stdout", mode="inline",
                                      content=b"ok", media_type="text/plain",
                                      provenance={"agent": "pi", "model": "test", "session_id": "s",
                                                  "task_id": "Ta1ask", "workflow_id": "Wo1flo"})
    assert record["profile"] == "stdout"
    assert record["system_metadata"]["provenance"]["session_id"] == "s"
    assert set(record["system_metadata"]["provenance"]) == {
        "agent", "model", "session_id", "task_id", "workflow_id"}
    model = artifacts.create_from_stream(io.BytesIO(b"answer"), record_id="Mo1del",
                                         title="model", profile="model-output", mode="inline",
                                         media_type="text/plain")
    assert model["profile"] == "model-output"
    with pytest.raises(RecordError, match="ARTIFACT_TOO_LARGE"):
        artifacts.create_from_stream(io.BytesIO(b"x" * 11), record_id="Bi1igx",
                                     title="big", profile="stdout", mode="local")


def test_policy_rejects_secrets_bad_claims_uris_and_inline_overflow(tmp_path):
    artifacts = store(tmp_path, max_inline_bytes=3)
    with pytest.raises(RecordError, match="ARTIFACT_INLINE_TOO_LARGE"):
        artifacts.create(record_id="Bi1igg", title="big", profile="report", mode="inline", content=b"four")
    with pytest.raises(RecordError, match="ARTIFACT_SECRET_REJECTED"):
        artifacts.create(record_id="Se1ret", title="secret", profile="report", mode="local",
                         content=b"password=very-secret-value")
    with pytest.raises(RecordError, match="ARTIFACT_SCHEME_UNSUPPORTED"):
        artifacts.create(record_id="Fi1ile", title="file", profile="report", mode="link",
                         uri="file:///etc/passwd")
    with pytest.raises(RecordError, match="ARTIFACT_PATH_UNSAFE"):
        artifacts.create(record_id="Tr1ver", title="bad", profile="report", mode="external",
                         uri="https://example.test/a/../b", digest="0" * 64, size=1)
    with pytest.raises(RecordError, match="ARTIFACT_DIGEST_MISMATCH"):
        artifacts.create(record_id="Di1est", title="bad", profile="report", mode="external",
                         uri="https://example.test/x", digest="0" * 64, size=1, content=b"x")


def test_exact_revision_preserves_history_and_fault_rolls_back(tmp_path, monkeypatch):
    artifacts = store(tmp_path)
    first = artifacts.create(record_id="Re1ise", title="r", profile="report",
                             mode="inline", content=b"one")
    before_paths = set(artifacts.root.rglob("*"))
    with pytest.raises(RecordError, match="EXACT_MATCH_NOT_FOUND"):
        artifacts.revise("Re1ise", expected_revision=1, expected_payload={"backend": "inline"},
                         mode="local", content=b"two")
    assert set(artifacts.root.rglob("*")) == before_paths

    second = artifacts.revise("Re1ise", expected_revision=1, expected_payload=first["payload"],
                              mode="local", content=b"two")
    assert second["revision"] == 2
    assert artifacts.get("Re1ise", 1)["payload"] == first["payload"]
    assert artifacts.verify("Re1ise") == b"two"
    successor = artifacts.revise("Re1ise", expected_revision=2,
                                 expected_payload=second["payload"], successor_id="Su1ess",
                                 mode="inline", content=b"three")
    assert successor["revision"] == 1
    assert successor["relations"] == [{"type": "supersedes", "record_id": "Re1ise"}]

    def fail(point):
        if point == "after_manifest":
            raise OSError("fault")
    monkeypatch.setattr(artifacts, "_mutation_fault", fail)
    with pytest.raises(OSError, match="fault"):
        artifacts.create(record_id="Fa1ult", title="fault", profile="receipt",
                         mode="local", content=b"unique-fault-object")
    assert not list(artifacts._record_dir("Fa1ult").glob("*.json"))
    digest = hashlib.sha256(b"unique-fault-object").hexdigest()
    assert not artifacts.objects.path(digest).exists()
    assert not artifacts._event_path("Fa1ult", 1).exists()


def test_object_collision_symlink_and_retention_validation_fail_closed(tmp_path):
    artifacts = store(tmp_path)
    content = b"collision"
    digest = hashlib.sha256(content).hexdigest()
    path = artifacts.objects.path(digest)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not collision")
    with pytest.raises(RecordError, match="ARTIFACT_(SIZE|DIGEST)_MISMATCH"):
        artifacts.create(record_id="Co1ide", title="collision", profile="report",
                         mode="local", content=content)
    path.unlink()
    path.parent.rmdir()
    path.parent.symlink_to(tmp_path)
    with pytest.raises(RecordError, match="ARTIFACT_PATH_UNSAFE"):
        artifacts.create(record_id="Sy1ink", title="symlink", profile="report",
                         mode="local", content=content)
    with pytest.raises(RecordError, match="ARTIFACT_RETENTION_INVALID"):
        artifacts.create(record_id="Rt1ain", title="retention", profile="report",
                         mode="inline", content=b"ok", retention={"class": "forever-ish"})


def test_local_path_adapter_rejects_symlinks_and_root_escape(tmp_path):
    artifacts = store(tmp_path)
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    source = allowed / "report.txt"
    source.write_bytes(b"report")
    record = artifacts.create_from_path(source, allowed_root=allowed, record_id="Pa1ath",
                                        title="path", profile="report", mode="local")
    assert artifacts.verify(record["id"]) == b"report"
    link = allowed / "link"
    link.symlink_to(source)
    with pytest.raises(RecordError, match="ARTIFACT_PATH_UNSAFE"):
        artifacts.create_from_path(link, allowed_root=allowed, record_id="Ln1ink",
                                   title="link", profile="report", mode="local")
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    with pytest.raises(RecordError, match="ARTIFACT_PATH_UNSAFE"):
        artifacts.create_from_path(outside, allowed_root=allowed, record_id="Es1ape",
                                   title="escape", profile="report", mode="local")
