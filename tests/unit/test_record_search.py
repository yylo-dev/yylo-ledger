import json

import pytest

from yylo_ledger.documents import create_document
from yylo_ledger.record_search import (IndexedRecord, RecordSearchIndex,
                                       RecordSearchPolicy, RecordSearchQuery)
from yylo_ledger.records import RecordError, task_record_projection


def document(record_id, title, text, **values):
    return create_document(record_id=record_id, title=title, profile="wiki",
                           media_type="text/markdown", text=text,
                           timestamp=values.pop("timestamp", "2026-08-01T00:00:00Z"), **values)


def fixtures():
    task = task_record_projection({
        "id": "Ta1sk2", "body": "Deploy blue service", "status": "todo",
        "feature_tags": ["ops"], "created_date": "2026-07-01T00:00:00Z",
        "last_modified": "2026-07-03T00:00:00Z", "related_tasks": [], "blocked_by": [],
        "custom_metadata": {"com.example": {"team": "blue"}},
        "system_metadata": {"provenance": {"agent": "pi", "model": "m1"}},
    })
    wiki = document("Do1cu2", "Runbook", "Blue deploy instructions", aliases=["old-runbook"],
                    relations=[{"type": "related", "record_id": "Ta1sk2"}],
                    custom_metadata={"com.example": {"team": "blue"}})
    wiki["system_metadata"]["creation_context"] = {"git": {
        "schema_version": 1, "captured_at": "2026-08-01T00:00:00Z", "repositories": [{
            "roles": ["controller", "project"], "head_sha": "a" * 40,
            "ref": "refs/heads/main", "worktree_dirty": False}]}}
    artifact = {
        "id": "Ar1ti2", "slug": "Ar1ti2-report", "aliases": [], "kind": "artifact",
        "profile": "report", "title": "Build report", "namespace": "default",
        "lifecycle": "archived", "tier": "archive", "schema_version": 2,
        "media_type": "application/octet-stream",
        "payload": {"backend": "external", "sha256": "b" * 64, "size": 42},
        "created_date": "2026-06-01T00:00:00Z", "last_modified": "2026-06-02T00:00:00Z",
        "revision": 1, "relations": [], "system_metadata": {}, "custom_metadata": {},
    }
    secret = document("Se1cr2", "Private token=do-not-leak", "password=hunter22")
    secret["system_metadata"]["sensitive"] = True
    return task, wiki, artifact, secret


def make_index(tmp_path, *, git_dirty=False):
    values = fixtures()
    values[1]["system_metadata"]["creation_context"]["git"]["repositories"][0]["worktree_dirty"] = git_dirty
    canonical = {}
    sources = []
    for record in values:
        tier = record.get("tier", "hot")
        locator = f"{tier}:{record['id']}"
        canonical[(tier, locator, record["id"])] = record
        sources.append(IndexedRecord(record, tier=tier, locator=locator))
    index = RecordSearchIndex(tmp_path / "records.sqlite3", policy=RecordSearchPolicy(
        custom_metadata_paths=frozenset({"com.example.team"}), max_page_size=20))
    index.rebuild(sources)
    return index, canonical, lambda tier, locator, record_id: canonical[(tier, locator, record_id)]


def test_generic_and_typed_filters_share_one_deterministic_engine(tmp_path):
    index, _, reader = make_index(tmp_path)
    generic = index.search(RecordSearchQuery(text="Blue", scope="hot", limit=10), reader)
    typed = index.search(RecordSearchQuery(text="Blue", scope="hot", kinds=["task", "document"], limit=10), reader)
    assert [item["id"] for item in generic.records] == [item["id"] for item in typed.records] == ["Do1cu2", "Ta1sk2"]

    combined = index.search(RecordSearchQuery(
        scope="all", slug="old-runbook", profiles=["wiki"], tags=(),
        relation_type="related", relation_id="Ta1sk2", provenance={},
        creation_git={"role": "controller", "head_sha": "a" * 40},
        custom_equals={"com.example.team": "blue"}, limit=10), reader)
    assert [item["id"] for item in combined.records] == ["Do1cu2"]

    artifact = index.search(RecordSearchQuery(scope="archive", kinds=["artifact"],
                            digest="b" * 64, backend="external", size_min=40,
                            size_max=50, limit=10), reader)
    assert [item["id"] for item in artifact.records] == ["Ar1ti2"]


@pytest.mark.parametrize("dirty", [False, True])
def test_creation_git_dirty_filter_uses_canonical_column(tmp_path, dirty):
    index, _, reader = make_index(tmp_path, git_dirty=dirty)

    matching = index.search(RecordSearchQuery(
        scope="all", creation_git={"worktree_dirty": dirty}, limit=10), reader)
    opposite = index.search(RecordSearchQuery(
        scope="all", creation_git={"worktree_dirty": not dirty}, limit=10), reader)

    assert [item["id"] for item in matching.records] == ["Do1cu2"]
    assert opposite.records == []


def test_cursor_is_bounded_stable_and_fails_after_revision_change(tmp_path):
    index, canonical, reader = make_index(tmp_path)
    first = index.search(RecordSearchQuery(scope="all", limit=2, projection="metadata"), reader)
    assert first.next_cursor
    second = index.search(RecordSearchQuery(scope="all", limit=2, projection="metadata",
                                           cursor=first.next_cursor), reader)
    assert not ({item["id"] for item in first.records} & {item["id"] for item in second.records})

    changed = list(fixtures())
    changed[0]["title"] = "Changed"
    index.rebuild([IndexedRecord(item, tier=item.get("tier", "hot"),
                                 locator=f"{item.get('tier', 'hot')}:{item['id']}") for item in changed])
    with pytest.raises(RecordError, match="SEARCH_CURSOR_STALE"):
        index.search(RecordSearchQuery(scope="all", limit=2, projection="metadata",
                                      cursor=first.next_cursor), reader)


def test_canonical_tamper_is_detected_and_sensitive_content_never_indexes_or_projects(tmp_path):
    index, canonical, reader = make_index(tmp_path)
    sensitive = index.search(RecordSearchQuery(scope="hot", ids=["Se1cr2"],
                                               projection="full", limit=10), reader)
    encoded = json.dumps(sensitive.records)
    assert "hunter22" not in encoded and "do-not-leak" not in encoded
    assert sensitive.records[0]["sensitive"] is True
    assert index.search(RecordSearchQuery(scope="hot", text="hunter22", limit=10), reader).records == []

    key = next(key for key in canonical if key[2] == "Do1cu2")
    canonical[key]["title"] = "tampered"
    with pytest.raises(RecordError, match="SEARCH_INDEX_STALE"):
        index.search(RecordSearchQuery(scope="hot", ids=["Do1cu2"], limit=10), reader)


def test_missing_or_corrupt_disposable_index_rebuilds_from_canonical_source(tmp_path):
    values = fixtures()
    sources = lambda: [IndexedRecord(item, tier=item.get("tier", "hot"), locator=item["id"])
                       for item in values]
    canonical = {(item.get("tier", "hot"), item["id"], item["id"]): item for item in values}
    index = RecordSearchIndex(tmp_path / "cache" / "records.sqlite3", record_source=sources)
    reader = lambda tier, locator, record_id: canonical[(tier, locator, record_id)]
    assert index.search(RecordSearchQuery(ids=["Do1cu2"]), reader).records[0]["id"] == "Do1cu2"
    index.path.write_bytes(b"not sqlite")
    assert index.search(RecordSearchQuery(ids=["Ta1sk2"]), reader).records[0]["id"] == "Ta1sk2"


def test_unallowlisted_paths_and_output_overflow_fail_without_echoing_values(tmp_path):
    index, _, reader = make_index(tmp_path)
    with pytest.raises(RecordError, match="SEARCH_FIELD_UNINDEXED") as error:
        index.search(RecordSearchQuery(custom_equals={"private.password": "super-secret"}), reader)
    assert "super-secret" not in str(error.value)
    with pytest.raises(RecordError, match="SEARCH_OUTPUT_BUDGET"):
        index.search(RecordSearchQuery(ids=["Do1cu2"], projection="full",
                                      output_byte_budget=10), reader)
