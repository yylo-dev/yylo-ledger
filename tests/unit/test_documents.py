"""Focused Wave 2A Document/profile/Markdown/YAML contracts."""
import hashlib
import io
from copy import deepcopy

import pytest
from ruamel.yaml import YAML

from yylo_ledger.documents import (
    create_document, exact_update_document, extract_record_links, validate_record_links,
)
from yylo_ledger.frontmatter import (
    emit_wiki_frontmatter, import_wiki_frontmatter, parse_wiki_frontmatter,
)
from yylo_ledger.profiles import (
    WORKFLOW_SCHEMA_V1, RecordProfile, default_profile_registry,
)
from yylo_ledger.records import RecordError, RevisionProvenance, payload_digest, value_digest
from yylo_ledger.workflow_yaml import (
    emit_workflow_yaml, normalize_workflow_yaml, parse_workflow_yaml,
)


def wiki(**overrides):
    values = dict(
        record_id="Ab1Cd2", title="Café 日本語", profile="wiki",
        media_type="text/markdown", text="# Héllo\n\nline one\nline two\n",
        namespace="engineering", aliases=("old-page",),
        relations=({"type": "related", "record_id": "Xy9Za8"},),
        custom_metadata={"acme.example": {"rank": 7, "note": "multi\nline"}},
        provenance=RevisionProvenance(actor_type="agent", agent="pi", model="test-model",
                                      session_id="s1", run_id="r1", invocation_id="i1"),
        timestamp="2026-08-28T00:00:00Z",
    )
    values.update(overrides)
    return create_document(**values)


def workflow(text=None, **overrides):
    text = text or "schema_version: v1\nworkflow_id: deploy\nsteps:\n  - id: build\n    run: 'echo never'\n"
    values = dict(record_id="Wf1Ab2", title="Deploy", profile="workflow",
                  media_type="application/yaml", schema_ref=WORKFLOW_SCHEMA_V1,
                  text=text, timestamp="2026-08-28T00:00:00Z")
    values.update(overrides)
    return create_document(**values)


def dump_front(metadata, body):
    stream = io.StringIO()
    yaml = YAML(typ="safe", pure=True)
    yaml.default_flow_style = False
    yaml.dump(metadata, stream)
    return "---\n%s---\n%s" % (stream.getvalue(), body)


def test_profiles_are_closed_declarations_not_executable_plugins():
    registry = default_profile_registry()
    wiki_profile = registry.get("wiki")
    assert wiki_profile.native_kind == "document"
    assert wiki_profile.media_types == ("text/markdown",)
    assert wiki_profile.renderer == "markdown-safe-v1"
    assert "update" in wiki_profile.allowed_operations
    assert registry.get("workflow").schema_ref == WORKFLOW_SCHEMA_V1
    with pytest.raises(RecordError, match="PROFILE_INVALID"):
        RecordProfile("frozen", "document", ("text/plain",), None, "text", (), False, ("update",))
    with pytest.raises(RecordError, match="PROFILE_UNSUPPORTED"):
        registry.get("python.plugin")


def test_wiki_unicode_multiline_alias_metadata_relation_and_provenance_round_trip():
    record = wiki()
    source = emit_wiki_frontmatter(record)
    metadata, body = parse_wiki_frontmatter(source)
    assert body == record["payload"]["text"]
    assert metadata["aliases"] == ["old-page"]
    assert metadata["custom_metadata"] == record["custom_metadata"]
    assert metadata["relations"] == record["relations"]
    imported, event = import_wiki_frontmatter(
        source, record, expected_revision=1, expected_preimage=payload_digest(source))
    assert imported == record
    assert event is None
    assert imported["system_metadata"]["revision_provenance"][0]["invocation_id"] == "i1"


def test_frontmatter_import_is_one_exact_revision_and_keeps_canonical_system_truth():
    record = wiki()
    source = emit_wiki_frontmatter(record)
    metadata, _ = parse_wiki_frontmatter(source)
    metadata["title"] = "Renamed"
    metadata["slug"] = "renamed-page"
    body = "Unicode Δ\n\nsecond line\n"
    metadata["content_sha256"] = payload_digest(body)
    updated, event = import_wiki_frontmatter(
        dump_front(metadata, body), record, expected_revision=1,
        expected_preimage=payload_digest(source))
    assert updated["revision"] == 2
    assert updated["title"] == "Renamed"
    assert updated["slug"] == "renamed-page"
    assert record["slug"] in updated["aliases"]
    assert updated["payload"]["text"] == body
    assert updated["system_metadata"]["revision_provenance"][0]["agent"] == "pi"
    assert event["operation"] == "wiki-front-matter-import"


def test_duplicate_reserved_and_stale_frontmatter_fail_without_mutation():
    record = wiki()
    before = deepcopy(record)
    duplicate = "---\nid: Ab1Cd2\nid: Xy9Za8\n---\nbody"
    with pytest.raises(RecordError, match="FRONT_MATTER_INVALID"):
        parse_wiki_frontmatter(duplicate)
    with pytest.raises(RecordError, match="FRONT_MATTER_RESERVED"):
        parse_wiki_frontmatter("---\nid: Ab1Cd2\nsystem_command: rm -rf /\n---\nbody")
    source = emit_wiki_frontmatter(record)
    with pytest.raises(RecordError, match="REVISION_CONFLICT"):
        import_wiki_frontmatter(source, record, expected_revision=2,
                                expected_preimage=payload_digest(source))
    with pytest.raises(RecordError, match="REVISION_CONFLICT"):
        import_wiki_frontmatter(source, record, expected_revision=1, expected_preimage="0" * 64)
    assert record == before


def test_typed_links_store_ids_and_refuse_slug_guessing_or_ambiguous_resolution():
    text = "[Task](record:Xy9Za8) and [[record:Qr7St6]]"
    assert extract_record_links(text) == ("Xy9Za8", "Qr7St6")
    assert validate_record_links(text, lambda value: value) == ("Xy9Za8", "Qr7St6")
    with pytest.raises(RecordError, match="DOCUMENT_LINK_AMBIGUOUS"):
        validate_record_links("[Task](record:mutable-slug)")
    with pytest.raises(RecordError, match="DOCUMENT_LINK_AMBIGUOUS"):
        validate_record_links("[Task](record:Xy9Za8)", lambda value: "Other1")


def test_exact_document_updates_are_copy_on_write_and_pin_payload_identity():
    record = wiki(text="same same")
    before = deepcopy(record)
    with pytest.raises(RecordError, match="EXACT_MATCH_AMBIGUOUS"):
        exact_update_document(record, path="/payload/text", expected="same", replacement="x",
                              expected_revision=1, mode="substring")
    with pytest.raises(RecordError, match="REVISION_CONFLICT"):
        exact_update_document(record, path="/title", expected=record["title"], replacement="new",
                              expected_revision=2)
    with pytest.raises(RecordError, match="REVISION_CONFLICT"):
        exact_update_document(record, path="/title", expected=record["title"], replacement="new",
                              expected_revision=1, expected_record_digest="0" * 64)
    with pytest.raises(RecordError, match="REVISION_CONFLICT"):
        exact_update_document(record, path="/payload/text", expected="same same", replacement="new",
                              expected_revision=1, expected_payload_digest="0" * 64, mode="payload")
    assert record == before

    updated, event = exact_update_document(
        record, path="/payload/text", expected="same same", replacement="Δ\n",
        expected_revision=1, mode="payload", timestamp="2026-08-28T01:00:00Z")
    assert updated["revision"] == 2
    assert updated["payload"]["sha256"] == hashlib.sha256("Δ\n".encode()).hexdigest()
    assert event["payload_sha256"] == updated["payload"]["sha256"]
    renamed, _ = exact_update_document(
        updated, path="/slug", expected=updated["slug"], replacement="mutable-name",
        expected_revision=2, timestamp="2026-08-28T02:00:00Z")
    assert renamed["payload"]["sha256"] == updated["payload"]["sha256"]
    assert renamed["revision"] == 3


def test_workflow_yaml_is_deterministic_validated_and_never_executed(tmp_path):
    marker = tmp_path / "executed"
    source = ("schema_version: v1\nworkflow_id: safe\nsteps:\n"
              "  - id: one\n    run: 'touch %s'\n" % marker)
    record = workflow(source)
    parsed = parse_workflow_yaml(record["payload"]["text"])
    assert parse_workflow_yaml(emit_workflow_yaml(parsed)) == parsed
    assert normalize_workflow_yaml(source) == normalize_workflow_yaml(normalize_workflow_yaml(source))
    assert not marker.exists()


def test_workflow_rejects_unsupported_media_schema_invalid_shape_and_unsafe_yaml():
    with pytest.raises(RecordError, match="MEDIA_TYPE_UNSUPPORTED"):
        workflow(media_type="text/yaml")
    with pytest.raises(RecordError, match="SCHEMA_UNSUPPORTED"):
        workflow(schema_ref="https://example.test/workflow/v2")
    with pytest.raises(RecordError, match="WORKFLOW_SCHEMA_INVALID"):
        workflow("schema_version: v1\nworkflow_id: bad\nsteps: nope\n")
    unsafe = [
        "schema_version: v1\nworkflow_id: bad\nsteps: &s []\ncopy: *s\n",
        "schema_version: v1\nworkflow_id: bad\nsteps: !!python/object:evil {}\n",
        "schema_version: v1\nworkflow_id: first\nworkflow_id: second\nsteps: []\n",
        "schema_version: v1\nworkflow_id: bad\nsteps: []\nwhen: 2026-08-28\n",
    ]
    for source in unsafe:
        with pytest.raises(RecordError):
            workflow(source)


def test_custom_metadata_must_be_namespaced_and_is_not_a_profile_index_default():
    with pytest.raises(RecordError, match="CUSTOM_METADATA_INVALID"):
        wiki(custom_metadata={"rank": 7})
    with pytest.raises(RecordError, match="CUSTOM_METADATA_INVALID"):
        wiki(custom_metadata={"yylo.private": {"run": "no"}})
    profile = default_profile_registry().get("wiki")
    assert all(not field.startswith("custom_metadata") for field in profile.indexed_fields)
