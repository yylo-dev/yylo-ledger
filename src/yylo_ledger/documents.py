"""Editable, versioned Document Records built on the native Record envelope."""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
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


class DocumentStore:
    """Canonical immutable revision files for wiki/workflow Records."""

    def __init__(self, juno_root: Path, *, registry: Optional[ProfileRegistry] = None):
        self.root = Path(juno_root)
        self.juno_root = self.root
        self.registry = registry or default_profile_registry()
        self.records_root = self.root / "documents"
        self.events_root = self.root / "document-ledger"

    def _directory(self, record_id: str) -> Path:
        return self.records_root / record_id[:2].lower() / record_id

    def _event_directory(self, record_id: str) -> Path:
        return self.events_root / record_id[:2].lower() / record_id

    @staticmethod
    def _atomic_create(path: Path, value: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode() + b"\n"
        fd, temporary = tempfile.mkstemp(prefix=".%s." % path.name, dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded); handle.flush(); os.fsync(handle.fileno())
            os.link(temporary, path)
        except FileExistsError as exc:
            raise RecordError("REVISION_CONFLICT", "immutable Document revision already exists") from exc
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _cold_envelope(self, record_id: str) -> Optional[Dict[str, Any]]:
        from .archive import ArchiveFormatError, iter_archive_envelopes
        matches = [item for item in iter_archive_envelopes(self.root)
                   if item["task"]["id"].casefold() == record_id.casefold()
                   and item["task"].get("kind") == "document"]
        if len(matches) > 1:
            raise ArchiveFormatError("duplicate cold Document ID")
        return matches[0] if matches else None

    def create(self, **kwargs: Any) -> Dict[str, Any]:
        record = create_document(registry=self.registry, **kwargs)
        record_id = record["id"]
        with self._lock(record_id):
            if self._directory(record_id).exists() or self._cold_envelope(record_id):
                raise RecordError("DOCUMENT_ID_CONFLICT", "Document ID already exists")
            event = {"operation": "create", "record_id": record_id, "revision": 1,
                     "record_sha256": value_digest(record), "timestamp": record["created_date"]}
            revision = self._directory(record_id) / "00000001.json"
            history = self._event_directory(record_id) / "00000001.json"
            try:
                self._atomic_create(revision, record)
                self._atomic_create(history, event)
            except BaseException:
                revision.unlink(missing_ok=True); history.unlink(missing_ok=True)
                raise
        return record

    def get(self, id_or_slug: str, revision: Optional[int] = None) -> Dict[str, Any]:
        record_id = self.resolve(id_or_slug)
        paths = sorted(self._directory(record_id).glob("*.json"))
        if paths:
            path = paths[-1] if revision is None else self._directory(record_id) / f"{revision:08d}.json"
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError as exc:
                raise RecordError("RECORD_NOT_FOUND", "Document revision does not exist") from exc
        else:
            envelope = self._cold_envelope(record_id)
            if envelope is None or revision is not None:
                raise RecordError("RECORD_NOT_FOUND", "Document does not exist")
            value = envelope["task"]
        validate_document(value, registry=self.registry)
        return value

    def resolve(self, id_or_slug: str) -> str:
        matches = []
        for directory in self.records_root.glob("*/*"):
            paths = sorted(directory.glob("*.json"))
            if paths:
                value = json.loads(paths[-1].read_text(encoding="utf-8"))
                if id_or_slug in [value["id"], value["slug"], *value["aliases"]]:
                    matches.append(value["id"])
        from .archive import iter_archive_envelopes
        for envelope in iter_archive_envelopes(self.root):
            value = envelope["task"]
            if value.get("kind") == "document" and id_or_slug in [value["id"], value["slug"], *value.get("aliases", [])]:
                matches.append(value["id"])
        matches = sorted(set(matches))
        if not matches:
            raise RecordError("RECORD_NOT_FOUND", "Document identity does not exist")
        if len(matches) != 1:
            raise RecordError("RECORD_IDENTITY_AMBIGUOUS", "Document identity is ambiguous")
        return matches[0]

    @contextmanager
    def _lock(self, record_id: str):
        path = self.root / "locks" / "documents" / (record_id.casefold() + ".lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def update(self, id_or_slug: str, **kwargs: Any) -> Dict[str, Any]:
        record_id = self.resolve(id_or_slug)
        with self._lock(record_id):
            current = self.get(record_id)
            if current["tier"] == "cold":
                raise RecordError("RECORD_ARCHIVED", "archived Documents are immutable")
            candidate, event = exact_update_document(current, registry=self.registry, **kwargs)
            revision = self._directory(record_id) / f"{candidate['revision']:08d}.json"
            history = self._event_directory(record_id) / f"{candidate['revision']:08d}.json"
            try:
                self._atomic_create(revision, candidate)
                self._atomic_create(history, event)
            except BaseException:
                revision.unlink(missing_ok=True); history.unlink(missing_ok=True)
                raise
            return candidate

    @contextmanager
    def _record_archive_lock(self, id_or_slug: str):
        from .archive import _record_archive_owner
        record_id = self.resolve(id_or_slug)
        with _record_archive_owner(self.root), self._lock(record_id):
            if self.resolve(id_or_slug) != record_id:
                raise RecordError("RECORD_IDENTITY_AMBIGUOUS", "Document identity changed while locking")
            if self._cold_envelope(record_id):
                raise RecordError("RECORD_ARCHIVED", "archived Documents are immutable")
            yield record_id

    def _record_archive_snapshot(self, record_id: str) -> Dict[str, Any]:
        revisions = sorted(self._directory(record_id).glob("*.json"))
        events = sorted(self._event_directory(record_id).glob("*.json"))
        if not revisions or len(revisions) != len(events):
            from .archive import ArchiveFormatError
            raise ArchiveFormatError("Document snapshot/history is incomplete")
        snapshots = [json.loads(path.read_text(encoding="utf-8")) for path in revisions]
        event_values = [json.loads(path.read_text(encoding="utf-8")) for path in events]
        history = []
        for item, event in zip(snapshots, event_values):
            history.extend(({"operation": "snapshot", "record_id": record_id,
                             "revision": item["revision"], "record": item}, event))
        return {"record": snapshots[-1], "history": history,
                "paths": [*revisions, *events], "owned_objects": []}

    def _record_archive_verify_objects(self, objects) -> None:
        if objects:
            raise RecordError("DOCUMENT_INVALID", "Documents cannot own content objects")

    def _record_archive_refresh(self) -> None:
        return None

    def _git_head(self) -> Optional[str]:
        try:
            return subprocess.check_output(["git", "-C", str(self.root.parent), "rev-parse", "HEAD"],
                                           text=True, stderr=subprocess.DEVNULL).strip()
        except (OSError, subprocess.CalledProcessError):
            return None

    def _config_hash(self) -> str:
        return hashlib.sha256(b"document-store-v1").hexdigest()

    def archive(self, id_or_slug: str, *, expected_revision: int,
                receipt_path: Optional[Path] = None,
                provenance: Optional[Mapping[str, Any]] = None, fault=None) -> Dict[str, Any]:
        from .archive import archive_record
        return archive_record(self, id_or_slug, expected_revision=expected_revision,
                              receipt_path=receipt_path, provenance=provenance, fault=fault)

    def history(self, id_or_slug: str) -> list[Dict[str, Any]]:
        record_id = self.resolve(id_or_slug)
        events = sorted(self._event_directory(record_id).glob("*.json"))
        if events:
            return [json.loads(path.read_text(encoding="utf-8")) for path in events]
        envelope = self._cold_envelope(record_id)
        if envelope is None:
            raise RecordError("RECORD_NOT_FOUND", "Document history does not exist")
        return list(envelope["ledger"])
