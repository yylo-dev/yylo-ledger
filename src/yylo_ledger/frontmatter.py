"""Canonical wiki Document front-matter import/export projection."""
from __future__ import annotations

import copy
import io
from typing import Any, Dict, Mapping, Tuple

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from .codec import plain_value
from .documents import exact_update_document, validate_document
from .records import RecordError, payload_digest

_FRONT_FIELDS = (
    "id", "slug", "aliases", "kind", "profile", "title", "namespace",
    "lifecycle", "tier", "schema_version", "media_type", "created_date",
    "last_modified", "revision", "relations", "system_metadata", "custom_metadata",
    "content_sha256",
)
_EDITABLE_FIELDS = {"slug", "aliases", "title", "namespace", "relations", "custom_metadata"}


def _yaml() -> YAML:
    yaml = YAML(typ="safe", pure=True)
    yaml.allow_duplicate_keys = False
    yaml.default_flow_style = False
    yaml.width = 4096
    yaml.sort_base_mapping_type_on_output = False
    return yaml


def parse_wiki_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    if not isinstance(text, str) or "\r" in text or not text.startswith("---\n"):
        raise RecordError("FRONT_MATTER_INVALID", "wiki source requires LF-delimited YAML front matter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise RecordError("FRONT_MATTER_INVALID", "closing front matter delimiter is missing")
    yaml_text, body = text[4:end], text[end + 5:]
    try:
        metadata = _yaml().load(yaml_text)
    except (YAMLError, ValueError) as exc:
        raise RecordError("FRONT_MATTER_INVALID", "invalid or duplicate front matter: %s" % exc)
    if not isinstance(metadata, Mapping):
        raise RecordError("FRONT_MATTER_INVALID", "front matter must be a mapping")
    unknown = set(metadata) - set(_FRONT_FIELDS)
    if unknown:
        raise RecordError("FRONT_MATTER_RESERVED", "unknown top-level fields must be namespaced under custom_metadata")
    return dict(plain_value(metadata)), body


def emit_wiki_frontmatter(record: Mapping[str, Any]) -> str:
    validate_document(record)
    if record.get("profile") != "wiki":
        raise RecordError("PROFILE_KIND_MISMATCH", "front matter is only defined for wiki Documents")
    metadata = {name: copy.deepcopy(record[name]) for name in _FRONT_FIELDS
                if name != "content_sha256" and name in record}
    metadata["content_sha256"] = record["payload"]["sha256"]
    stream = io.StringIO()
    _yaml().dump(metadata, stream)
    return "---\n%s---\n%s" % (stream.getvalue(), record["payload"]["text"])


def import_wiki_frontmatter(text: str, canonical: Mapping[str, Any], *,
                            expected_revision: int, expected_preimage: str):
    """Import one exact projection while keeping the Record envelope authoritative.

    The expected preimage is the SHA-256 of the complete previously exported
    source, so stale imports fail before a candidate is produced.
    """
    validate_document(canonical)
    if canonical.get("profile") != "wiki":
        raise RecordError("PROFILE_KIND_MISMATCH", "front matter is only defined for wiki Documents")
    if canonical["revision"] != expected_revision:
        raise RecordError("REVISION_CONFLICT", "expected revision is stale")
    current_source = emit_wiki_frontmatter(canonical)
    if payload_digest(current_source) != expected_preimage:
        raise RecordError("REVISION_CONFLICT", "front matter preimage is stale")
    metadata, body = parse_wiki_frontmatter(text)
    for name in _FRONT_FIELDS:
        if name in _EDITABLE_FIELDS or name == "content_sha256":
            continue
        if metadata.get(name) != canonical.get(name):
            raise RecordError("FRONT_MATTER_RESERVED", "canonical field %s cannot be imported" % name)
    if metadata.get("content_sha256") != payload_digest(body):
        raise RecordError("DOCUMENT_DIGEST_MISMATCH", "front matter content digest does not match body")

    candidate = copy.deepcopy(dict(canonical))
    changes = []
    for name in _EDITABLE_FIELDS:
        if name in metadata and metadata[name] != candidate.get(name):
            changes.append(("/%s" % name, candidate.get(name), metadata[name], "structured"))
    if body != candidate["payload"]["text"]:
        changes.append(("/payload/text", candidate["payload"]["text"], body, "payload"))
    if not changes:
        return candidate, None
    # Apply all exact projections to a private candidate, then expose one logical
    # revision. Intermediate revisions are not canonical or externally visible.
    original_revision = expected_revision
    original_provenance = list(candidate["system_metadata"].get("revision_provenance") or [])
    events = []
    for index, (path, before, after, mode) in enumerate(changes):
        candidate, event = exact_update_document(
            candidate, path=path, expected=before, replacement=after,
            expected_revision=original_revision + index, mode=mode)
        events.append(event["exact_match"])
    if len(changes) > 1:
        candidate["revision"] = original_revision + 1
        # exact_update_document appended provenance per private intermediate;
        # one import is one canonical revision and therefore one provenance row.
        candidate["system_metadata"]["revision_provenance"] = (
            original_provenance + candidate["system_metadata"]["revision_provenance"][-1:])
    validate_document(candidate)
    return candidate, {"operation": "wiki-front-matter-import", "record_id": candidate["id"],
                       "revision": candidate["revision"], "exact_matches": events}
