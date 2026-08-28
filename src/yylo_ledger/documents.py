"""Editable, versioned Document Records built on the native Record envelope."""
from __future__ import annotations

import copy
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

from .profiles import ProfileRegistry, default_profile_registry
from .records import (RecordError, RevisionProvenance, default_slug, exact_replace,
                      payload_digest, validate_record, value_digest)
from .workflow_yaml import parse_workflow_yaml

_CUSTOM_NAMESPACE = re.compile(r"^(?!yylo(?:\.|$))[a-zA-Z0-9][a-zA-Z0-9_-]*(?:\.[a-zA-Z0-9][a-zA-Z0-9_-]*)+$")
_MARKDOWN_LINK = re.compile(r"(?:\[[^\]]*\]\(record:|\[\[record:)([A-Za-z0-9]{6})(?:\)|\]\])")


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_custom_metadata(metadata: object) -> None:
    if not isinstance(metadata, Mapping):
        raise RecordError("CUSTOM_METADATA_INVALID", "custom metadata must be a mapping")
    for namespace in metadata:
        if not isinstance(namespace, str) or not _CUSTOM_NAMESPACE.fullmatch(namespace):
            raise RecordError("CUSTOM_METADATA_INVALID", "custom metadata keys must be non-reserved namespaces")


def extract_record_links(markdown: str) -> Tuple[str, ...]:
    """Return typed immutable Record IDs in source order, without guessing slugs."""
    if not isinstance(markdown, str):
        raise RecordError("DOCUMENT_PAYLOAD_INVALID", "Markdown payload must be text")
    return tuple(match.group(1) for match in _MARKDOWN_LINK.finditer(markdown))


def validate_record_links(markdown: str, resolver: Optional[Callable[[str], object]] = None) -> Tuple[str, ...]:
    # Link-like record targets not carrying an exact six-character ID fail
    # instead of being interpreted as mutable slugs.
    for target in re.findall(r"(?:\[[^\]]*\]\(record:|\[\[record:)([^\])]+)", markdown):
        if not re.fullmatch(r"[A-Za-z0-9]{6}", target):
            raise RecordError("DOCUMENT_LINK_AMBIGUOUS", "typed links must contain one immutable Record ID")
    links = extract_record_links(markdown)
    if resolver is not None:
        for record_id in links:
            resolved = resolver(record_id)
            if resolved is None:
                raise RecordError("DOCUMENT_LINK_NOT_FOUND", "linked Record %s does not exist" % record_id)
            if isinstance(resolved, str) and resolved != record_id:
                raise RecordError("DOCUMENT_LINK_AMBIGUOUS", "link did not resolve to its exact ID")
    return links


def _profile_payload(profile: str, text: str) -> Tuple[object, Optional[str]]:
    if not isinstance(text, str):
        raise RecordError("DOCUMENT_PAYLOAD_INVALID", "Document payload must be Unicode text")
    if "\r" in text:
        raise RecordError("DOCUMENT_PAYLOAD_INVALID", "Document payload must use LF line endings")
    if profile == "wiki":
        validate_record_links(text)
        return text, None
    if profile == "workflow":
        return parse_workflow_yaml(text), None
    raise RecordError("PROFILE_UNSUPPORTED", "unsupported Document profile %r" % profile)


def create_document(*, record_id: str, title: str, profile: str, media_type: str,
                    text: str, namespace: str = "default", slug: Optional[str] = None,
                    aliases: Sequence[str] = (), schema_ref: Optional[str] = None,
                    relations: Sequence[Mapping[str, str]] = (),
                    system_metadata: Optional[Mapping[str, Any]] = None,
                    custom_metadata: Optional[Mapping[str, Any]] = None,
                    provenance: Optional[RevisionProvenance] = None,
                    registry: Optional[ProfileRegistry] = None,
                    timestamp: Optional[str] = None) -> Dict[str, Any]:
    registry = registry or default_profile_registry()
    parsed, _ = _profile_payload(profile, text)
    registry.validate(profile=profile, kind="document", media_type=media_type,
                      schema_ref=schema_ref, payload=parsed)
    custom = copy.deepcopy(dict(custom_metadata or {}))
    _validate_custom_metadata(custom)
    now = timestamp or _timestamp()
    actor = provenance or RevisionProvenance(actor_type="human")
    system = copy.deepcopy(dict(system_metadata or {}))
    # Revision provenance is canonical system metadata; imported custom/front
    # matter data cannot place it there through this API.
    if "revision_provenance" in system:
        raise RecordError("SYSTEM_METADATA_RESERVED", "revision provenance is Ledger-owned")
    system["revision_provenance"] = [actor.to_dict()]
    payload = {"backend": "inline", "text": text, "sha256": payload_digest(text),
               "schema_ref": schema_ref}
    record = {
        "id": record_id, "slug": slug or default_slug(record_id, title),
        "aliases": list(aliases), "kind": "document", "profile": profile,
        "title": title, "namespace": namespace, "lifecycle": "active", "tier": "hot",
        "schema_version": 2, "media_type": media_type, "payload": payload,
        "created_date": now, "last_modified": now, "revision": 1,
        "relations": [dict(item) for item in relations], "system_metadata": system,
        "custom_metadata": custom,
    }
    validate_document(record, registry=registry)
    return record


def validate_document(record: Mapping[str, Any], *, registry: Optional[ProfileRegistry] = None,
                      link_resolver: Optional[Callable[[str], object]] = None) -> None:
    validate_record(record)
    if record["kind"] != "document" or not isinstance(record.get("profile"), str):
        raise RecordError("DOCUMENT_INVALID", "Document kind and profile are required")
    payload = record.get("payload")
    if not isinstance(payload, Mapping) or payload.get("backend") != "inline" or not isinstance(payload.get("text"), str):
        raise RecordError("DOCUMENT_PAYLOAD_INVALID", "Document requires an inline text payload")
    text = payload["text"]
    if payload.get("sha256") != payload_digest(text):
        raise RecordError("DOCUMENT_DIGEST_MISMATCH", "payload digest does not match UTF-8 bytes")
    _validate_custom_metadata(record.get("custom_metadata"))
    if record["profile"] == "wiki":
        parsed = text
        validate_record_links(text, link_resolver)
    elif record["profile"] == "workflow":
        parsed = parse_workflow_yaml(text)
    else:
        parsed = text
    (registry or default_profile_registry()).validate(
        profile=record["profile"], kind="document", media_type=record["media_type"],
        schema_ref=payload.get("schema_ref"), payload=parsed)


def exact_update_document(record: Mapping[str, Any], *, path: str, expected: Any,
                          replacement: Any, expected_revision: int,
                          expected_record_digest: Optional[str] = None,
                          expected_payload_digest: Optional[str] = None,
                          mode: str = "structured",
                          provenance: Optional[RevisionProvenance] = None,
                          registry: Optional[ProfileRegistry] = None,
                          timestamp: Optional[str] = None) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return a validated next revision and event; never mutate the input mapping."""
    validate_document(record, registry=registry)
    if record["revision"] != expected_revision:
        raise RecordError("REVISION_CONFLICT", "expected revision is stale")
    if expected_record_digest is not None and value_digest(record) != expected_record_digest:
        raise RecordError("REVISION_CONFLICT", "Record digest is stale")
    if (expected_payload_digest is not None
            and record["payload"]["sha256"] != expected_payload_digest):
        raise RecordError("REVISION_CONFLICT", "Document payload digest is stale")
    if path in {"/id", "/kind", "/created_date", "/system_metadata"} or path.startswith("/system_metadata/"):
        raise RecordError("IMMUTABLE_FIELD", "identity and canonical system metadata are immutable")
    candidate = copy.deepcopy(dict(record))
    old_slug = candidate["slug"]
    payload_mode = mode
    verified_payload_digest = None
    if path == "/payload/text" and mode == "payload":
        verified_payload_digest = expected_payload_digest
    match = exact_replace(candidate, path=path, expected=expected, replacement=replacement,
                          mode=payload_mode, expected_digest=verified_payload_digest)
    if path == "/slug" and candidate["slug"] != old_slug and old_slug not in candidate["aliases"]:
        candidate["aliases"].append(old_slug)
    if path == "/payload/text" or path.startswith("/payload/text"):
        candidate["payload"]["sha256"] = payload_digest(candidate["payload"]["text"])
    candidate["revision"] = expected_revision + 1
    candidate["last_modified"] = timestamp or _timestamp()
    actor = provenance or RevisionProvenance(actor_type="human")
    provenance_log = list(candidate["system_metadata"].get("revision_provenance") or [])
    provenance_log.append(actor.to_dict())
    candidate["system_metadata"]["revision_provenance"] = provenance_log
    validate_document(candidate, registry=registry)
    event = {"operation": "document-exact-update", "record_id": candidate["id"],
             "revision": candidate["revision"], "exact_match": match,
             "payload_sha256": candidate["payload"]["sha256"],
             "provenance": actor.to_dict()}
    return candidate, event
