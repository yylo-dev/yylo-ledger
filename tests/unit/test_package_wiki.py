import copy
import hashlib
import json
import multiprocessing
import os

import pytest

from yylo_ledger.documents import DocumentStore
from yylo_ledger.package_wiki import (MANIFEST_SCHEMA, PackageWikiStore,
                                     package_record_id)
from yylo_ledger.records import RecordError, value_digest


def manifest(version="1.0.0", texts=("# Hydration\n", "# Recovery\n")):
    return {"schema_version": MANIFEST_SCHEMA, "package": "@yylo/cli", "version": version,
            "artifact_sha256": hashlib.sha256(version.encode()).hexdigest(),
            "entries": [{"key": f"controller/{index}.md", "title": f"Runbook {index}", "text": text,
                         "sha256": hashlib.sha256(text.encode()).hexdigest()}
                        for index, text in enumerate(texts)]}


def test_idempotent_publication_and_revision_pinned_upgrade(tmp_path):
    store = PackageWikiStore(tmp_path / ".juno_task")
    first = manifest()
    plan = store.plan(first)
    assert not store.root.exists(), "planning must not create Ledger paths"
    receipt = store.publish(first, plan)
    assert store.publish(first, plan) == receipt
    assert store.publish(first, store.plan(first)) == receipt
    second = manifest("1.1.0", ("# Exact-lock hydration\n", "# Recovery\n"))
    next_receipt = store.publish(second, store.plan(second))
    assert [row["id"] for row in next_receipt["records"]] == [row["id"] for row in receipt["records"]]
    for old, new in zip(receipt["records"], next_receipt["records"]):
        assert new["revision"] == 2
        assert value_digest(store.get(old["id"], old["revision"])) == old["record_sha256"]
        assert len(store.history(old["id"])) == 2
        assert store.read_binding(receipt, old["key"])["revision"] == 1
        assert store.read_binding(next_receipt, new["key"])["revision"] == 2
    assert store.get(receipt["records"][0]["id"])["namespace"] == "package:@yylo/cli"


def test_secret_guidance_and_malformed_binding_plans_are_refused(tmp_path):
    store = PackageWikiStore(tmp_path / 'store')
    secret = manifest(texts=('password=synthetic-test-credential\n',))
    with pytest.raises(RecordError, match='PACKAGE_WIKI_SECRET_REJECTED'):
        store.plan(secret)
    with pytest.raises(RecordError, match='PACKAGE_WIKI_PLAN_INVALID'):
        store.generation_binding(manifest(), [])
    assert not store.root.exists()


def test_distinct_artifacts_with_same_version_require_explicit_revision_plan(tmp_path):
    store = PackageWikiStore(tmp_path)
    source = manifest()
    first = store.publish(source, store.plan(source))
    source['artifact_sha256'] = 'f' * 64
    second = store.publish(source, store.plan(source))
    assert second['version'] == first['version']
    assert second['records'][0]['revision'] == 2
    assert store.read_binding(first, first['records'][0]['key'])['revision'] == 1


def test_project_collision_is_never_adopted(tmp_path):
    store = PackageWikiStore(tmp_path)
    bundle = manifest()
    identity = package_record_id(bundle["package"], bundle["entries"][0]["key"])
    original = store.create(record_id=identity, title="My own wiki", profile="wiki",
                            media_type="text/markdown", text="# Project\n")
    with pytest.raises(RecordError, match="project Record"):
        store.plan(bundle)
    assert store.get(identity) == original


@pytest.mark.parametrize("path,expected,replacement", [
    ("/payload/text", "# Hydration\n", "# User change\n"),
    ("/title", "Runbook 0", "Personal guidance"),
    ("/custom_metadata", {}, {"project.notes": {"keep": True}}),
])
def test_local_edits_block_whole_bundle_before_writing(tmp_path, path, expected, replacement):
    store = PackageWikiStore(tmp_path)
    first = manifest()
    receipt = store.publish(first, store.plan(first))
    next_bundle = manifest("2.0.0")
    next_plan = store.plan(next_bundle)
    identity = receipt["records"][0]["id"]
    edited = store.update(identity, path=path, expected=expected, replacement=replacement,
                          expected_revision=1)
    with pytest.raises(RecordError, match="preserve modified"):
        store.publish(next_bundle, next_plan)
    assert store.get(identity) == edited
    assert store.get(receipt["records"][1]["id"])["revision"] == 1


def test_retry_after_interruption_preserves_identity(tmp_path):
    store = PackageWikiStore(tmp_path)
    bundle = manifest()
    plan = store.plan(bundle)
    def crash(_):
        raise RuntimeError("interruption")
    with pytest.raises(RuntimeError, match="interruption"):
        store.publish(bundle, plan, after_record=crash)
    receipt = store.publish(bundle, plan)
    assert len(receipt["records"]) == 2
    for row in receipt["records"]:
        assert row["revision"] == 1
        assert len(store.history(row["id"])) == 1


def test_stale_plan_and_same_version_different_bytes_refused(tmp_path):
    store = PackageWikiStore(tmp_path)
    first = manifest()
    old_plan = store.plan(first)
    store.publish(first, old_plan)
    second = manifest("2.0.0")
    stale_plan = store.plan(second)
    third = manifest("3.0.0")
    store.publish(third, store.plan(third))
    with pytest.raises(RecordError, match="[A-Za-z0-9]{6}") as failure:
        store.publish(second, stale_plan)
    assert failure.value.code == "PACKAGE_WIKI_REVISION_CONFLICT"
    conflicting = manifest("3.0.0", ("changed\n", "unchanged\n"))
    with pytest.raises(RecordError, match="same package version"):
        store.publish(conflicting, store.plan(conflicting))


@pytest.mark.parametrize("change", [
    lambda m: m.update(artifact_sha256="invalid"),
    lambda m: m["entries"][0].update(sha256="0" * 64),
    lambda m: m["entries"][0].update(key="../secrets.md"),
    lambda m: m["entries"][0].update(key="workflow.yaml"),
    lambda m: m["entries"].append(copy.deepcopy(m["entries"][0])),
    lambda m: m.update(extra="not admitted"),
])
def test_invalid_manifest_is_non_mutating(tmp_path, change):
    store = PackageWikiStore(tmp_path / "store")
    bundle = manifest()
    change(bundle)
    with pytest.raises(RecordError):
        store.plan(bundle)
    assert not store.root.exists()


def test_typed_links_validate_before_any_publication(tmp_path):
    store = PackageWikiStore(tmp_path)
    bundle = manifest(texts=("[unknown](record:ZZZZZZ)\n",))
    with pytest.raises(RecordError, match="does not exist"):
        store.publish(bundle, store.plan(bundle))
    assert not list(store.records_root.glob("*/*/*.json"))
    second_id = package_record_id(bundle["package"], "controller/1.md")
    valid = manifest(texts=(f"[recovery](record:{second_id})\n", "# Recovery\n"))
    assert len(store.publish(valid, store.plan(valid))["records"]) == 2


def test_retired_entries_are_preserved(tmp_path):
    store = PackageWikiStore(tmp_path)
    first = manifest()
    receipt = store.publish(first, store.plan(first))
    retired = store.get(receipt["records"][1]["id"])
    second = manifest("2.0.0", ("# New hydration\n",))
    store.publish(second, store.plan(second))
    assert DocumentStore(tmp_path).get(retired["id"]) == retired


def test_hard_exit_between_revision_and_event_can_resume_exact_plan(tmp_path):
    store = PackageWikiStore(tmp_path)
    bundle = manifest()
    plan = store.plan(bundle)
    def worker():
        original = store._atomic_create
        def cut(path, value):
            original(path, value)
            if "documents" in path.parts:
                os._exit(23)
        store._atomic_create = cut
        store.publish(bundle, plan)
    child = multiprocessing.get_context("fork").Process(target=worker)
    child.start()
    child.join(10)
    if child.is_alive():
        child.kill()
        child.join(5)
        pytest.fail("publication child did not settle")
    assert child.exitcode == 23
    receipt = store.publish(bundle, plan)
    for row in receipt["records"]:
        assert len(store.history(row["id"])) == 1
        assert store.history(row["id"])[0]["record_sha256"] == row["record_sha256"]


def test_history_conflict_is_preserved(tmp_path):
    store = PackageWikiStore(tmp_path)
    bundle = manifest()
    plan = store.plan(bundle)
    receipt = store.publish(bundle, plan)
    event = store._event_directory(receipt["records"][0]["id"]) / "00000001.json"
    event.write_text('{"unexpected":"preserve"}')
    with pytest.raises(RecordError, match="conflicting publication event"):
        store.publish(bundle, plan)
    assert json.loads(event.read_text()) == {"unexpected": "preserve"}


def test_receipt_cannot_change_revision_or_fallback_to_latest(tmp_path):
    store = PackageWikiStore(tmp_path)
    bundle = manifest()
    receipt = store.publish(bundle, store.plan(bundle))
    changed = copy.deepcopy(receipt)
    changed["records"][0]["revision"] = 2
    with pytest.raises(RecordError, match="seal"):
        store.read_binding(changed, "controller/0.md")
    changed["receipt_sha256"] = value_digest({k: v for k, v in changed.items() if k != "receipt_sha256"})
    with pytest.raises(RecordError, match="does not exist"):
        store.read_binding(changed, "controller/0.md")


def test_plan_seal_and_manifest_binding(tmp_path):
    store = PackageWikiStore(tmp_path)
    first = manifest()
    plan = store.plan(first)
    plan["records"][0]["revision"] = 99
    with pytest.raises(RecordError, match="exact manifest"):
        store.publish(first, plan)
    with pytest.raises(RecordError, match="exact manifest"):
        store.publish(manifest("2.0.0"), store.plan(first))
