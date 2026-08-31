"""Immutable Artifact profiles and opt-in payload capture.

Artifacts are deliberately independent of process/run collection.  A caller must
provide bytes (or an explicit stream) and optional narrow provenance.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import tempfile
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Dict, Mapping, Optional
from urllib.parse import urlparse

from .content_objects import ContentObjectStore, sha256_bytes
from .git_creation import attach_creation_context, capture_creation_context
from .records import RECORD_ID_RE, RecordError, default_slug, validate_record, value_digest


ARTIFACT_PROFILES = frozenset({"stdout", "model-output", "report", "receipt"})
PAYLOAD_MODES = frozenset({"inline", "local", "external", "link"})
DEFAULT_INLINE_LIMIT = 64 * 1024
DEFAULT_CAPTURE_LIMIT = 64 * 1024 * 1024
_SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    re.compile(rb"gh[oprsu]_[A-Za-z0-9]{30,}"),
    re.compile(rb"(?i)(?:password|passwd|secret|api[_-]?key|access[_-]?token)\s*[:=]\s*[^\s]{8,}"),
    re.compile(rb"(?i)authorization\s*:\s*(?:bearer|basic)\s+[^\s]+"),
)
_PROVENANCE_FIELDS = ("actor", "actor_type", "agent", "model", "session_id", "run_id",
                      "invocation_id", "task_id", "workflow_id")
_MIGRATION_FIELDS = {"schema_version", "plan_sha256", "source_path", "source_sha256"}


@dataclass(frozen=True)
class ArtifactPolicy:
    max_inline_bytes: int = DEFAULT_INLINE_LIMIT
    max_capture_bytes: int = DEFAULT_CAPTURE_LIMIT
    allowed_external_schemes: tuple[str, ...] = ("https", "s3", "gs")
    allowed_link_schemes: tuple[str, ...] = ("https", "http", "s3", "gs")
    reject_secrets: bool = True

    def __post_init__(self) -> None:
        if self.max_inline_bytes < 0 or self.max_capture_bytes <= 0:
            raise RecordError("ARTIFACT_POLICY_INVALID", "payload limits must be positive")


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_media_type(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or "/" not in value or "\n" in value or "\r" in value:
        raise RecordError("ARTIFACT_MEDIA_TYPE_INVALID", "media type must be a non-empty type/subtype")


def _validate_uri(uri: str, schemes: tuple[str, ...]) -> None:
    if not isinstance(uri, str) or not uri:
        raise RecordError("ARTIFACT_URI_INVALID", "URI is required")
    parsed = urlparse(uri)
    if parsed.scheme.lower() not in schemes:
        raise RecordError("ARTIFACT_SCHEME_UNSUPPORTED", f"unsupported URI scheme {parsed.scheme!r}")
    if parsed.username is not None or parsed.password is not None:
        raise RecordError("ARTIFACT_SECRET_REJECTED", "credentials must not be embedded in artifact URIs")
    if parsed.scheme in ("http", "https") and not parsed.netloc:
        raise RecordError("ARTIFACT_URI_INVALID", "network URI requires a host")
    if any(part == ".." for part in parsed.path.split("/")):
        raise RecordError("ARTIFACT_PATH_UNSAFE", "URI path traversal is not allowed")


def _validate_provenance(provenance: Optional[Mapping[str, Any]]) -> Dict[str, str]:
    if provenance is None:
        return {}
    if not isinstance(provenance, Mapping):
        raise RecordError("PROVENANCE_INVALID", "provenance must be an object")
    unknown = set(provenance) - set(_PROVENANCE_FIELDS)
    if unknown:
        raise RecordError("PROVENANCE_INVALID", "unsupported provenance fields: " + ", ".join(sorted(unknown)))
    result: Dict[str, str] = {}
    for key, value in provenance.items():
        if not isinstance(value, str) or not value.strip():
            raise RecordError("PROVENANCE_INVALID", f"{key} must be a non-empty string")
        if key in ("task_id", "workflow_id") and not RECORD_ID_RE.fullmatch(value):
            raise RecordError("PROVENANCE_INVALID", f"{key} must be an immutable Record ID")
        result[key] = value
    return result


def validate_retention(retention: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Validate narrow, portable retention metadata without running deletion."""
    if retention is None:
        return {"class": "standard"}
    if not isinstance(retention, Mapping):
        raise RecordError("ARTIFACT_RETENTION_INVALID", "retention must be an object")
    allowed = {"class", "retain_until", "legal_hold"}
    if set(retention) - allowed:
        raise RecordError("ARTIFACT_RETENTION_INVALID", "retention contains unsupported fields")
    result = dict(retention)
    retention_class = result.get("class", "standard")
    if retention_class not in ("temporary", "standard", "permanent"):
        raise RecordError("ARTIFACT_RETENTION_INVALID", "unsupported retention class")
    result["class"] = retention_class
    if "legal_hold" in result and not isinstance(result["legal_hold"], bool):
        raise RecordError("ARTIFACT_RETENTION_INVALID", "legal_hold must be boolean")
    if retention_class == "permanent" and "retain_until" in result:
        raise RecordError("ARTIFACT_RETENTION_INVALID", "permanent retention has no expiry")
    if "retain_until" in result:
        value = result["retain_until"]
        if not isinstance(value, str):
            raise RecordError("ARTIFACT_RETENTION_INVALID", "retain_until must be an RFC 3339 string")
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RecordError("ARTIFACT_RETENTION_INVALID", "retain_until must be an RFC 3339 string") from exc
    return result


def _validate_migration_metadata(metadata: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """Validate the narrow, Ledger-owned source binding used by migration."""
    if metadata is None:
        return None
    if not isinstance(metadata, Mapping) or set(metadata) != _MIGRATION_FIELDS:
        raise RecordError("MIGRATION_METADATA_INVALID", "migration metadata has an unsupported shape")
    if metadata.get("schema_version") != 1:
        raise RecordError("MIGRATION_METADATA_INVALID", "unsupported migration metadata schema")
    for key in ("plan_sha256", "source_sha256"):
        value = metadata.get(key)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise RecordError("MIGRATION_METADATA_INVALID", f"{key} must be a SHA-256 digest")
    path = metadata.get("source_path")
    if (not isinstance(path, str) or not path or path.startswith("/")
            or ".." in path.split("/") or "\x00" in path):
        raise RecordError("MIGRATION_METADATA_INVALID", "source_path must be a safe relative path")
    return dict(metadata)


class ArtifactStore:
    """Transactional immutable manifests plus a deduplicated local object store."""

    def __init__(self, juno_root: Path, *, policy: Optional[ArtifactPolicy] = None,
                 project_root: Optional[Path] = None,
                 repository_ids: Optional[Mapping[str, str]] = None):
        self.root = Path(juno_root)
        self.juno_root = self.root
        self.controller_root = self.root.parent
        self.project_root = Path(project_root) if project_root is not None else self.controller_root
        self.repository_ids = dict(repository_ids or {})
        self.policy = policy or ArtifactPolicy()
        self.objects = ContentObjectStore(self.root)
        self.records_root = self.root / "artifacts"
        self.events_root = self.root / "artifact-ledger"

    def _mutation_fault(self, point: str) -> None:
        if os.environ.get("YYLO_ARTIFACT_FAULT_POINT") == point:
            raise OSError(f"injected artifact fault: {point}")

    def _record_dir(self, record_id: str) -> Path:
        self._validate_id(record_id)
        return self.records_root / record_id[:2].lower() / record_id

    @staticmethod
    def _validate_id(record_id: str) -> None:
        if not isinstance(record_id, str) or not RECORD_ID_RE.fullmatch(record_id):
            raise RecordError("RECORD_INVALID", "artifact ID must be an immutable 6-character Record ID")

    def _revision_path(self, record_id: str, revision: int) -> Path:
        return self._record_dir(record_id) / f"{revision:08d}.json"

    def _event_path(self, record_id: str, revision: int) -> Path:
        return self.events_root / record_id[:2].lower() / record_id / f"{revision:08d}.json"

    def _lock(self):
        path = self.root / "locks" / "artifact-mutation.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+b")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle

    @staticmethod
    def _atomic_create(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError as exc:
                raise RecordError("ARTIFACT_REVISION_CONFLICT", f"immutable path already exists: {path.name}") from exc
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _check_bytes(self, content: bytes) -> None:
        if not isinstance(content, bytes):
            raise RecordError("ARTIFACT_BYTES_INVALID", "artifact content must be bytes")
        if len(content) > self.policy.max_capture_bytes:
            raise RecordError("ARTIFACT_TOO_LARGE", "artifact exceeds the configured capture limit")
        if self.policy.reject_secrets and any(pattern.search(content) for pattern in _SECRET_PATTERNS):
            raise RecordError("ARTIFACT_SECRET_REJECTED", "artifact matches the credential/secret policy")

    def inline_payload(self, content: bytes, media_type: str) -> Dict[str, Any]:
        self._check_bytes(content)
        _require_media_type(media_type)
        if len(content) > self.policy.max_inline_bytes:
            raise RecordError("ARTIFACT_INLINE_TOO_LARGE", "inline artifact exceeds the configured bound")
        return {"backend": "inline", "encoding": "base64", "data": base64.b64encode(content).decode("ascii"),
                "sha256": sha256_bytes(content), "size": len(content), "media_type": media_type,
                "immutable_bytes": True}

    def external_payload(self, *, uri: str, digest: str, size: int, media_type: str,
                         evidence_bytes: Optional[bytes] = None) -> Dict[str, Any]:
        _validate_uri(uri, self.policy.allowed_external_schemes)
        _require_media_type(media_type)
        ContentObjectStore._validate_digest(digest)
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise RecordError("ARTIFACT_SIZE_INVALID", "external size must be a non-negative integer")
        if evidence_bytes is not None:
            self._check_bytes(evidence_bytes)
            if len(evidence_bytes) != size:
                raise RecordError("ARTIFACT_SIZE_MISMATCH", "external evidence bytes differ from claimed size")
            if sha256_bytes(evidence_bytes) != digest:
                raise RecordError("ARTIFACT_DIGEST_MISMATCH", "external evidence bytes differ from claimed digest")
        return {"backend": "external", "uri": uri, "sha256": digest, "size": size,
                "media_type": media_type, "immutable_bytes": True}

    def link_payload(self, *, uri: str, media_type: str) -> Dict[str, Any]:
        _validate_uri(uri, self.policy.allowed_link_schemes)
        _require_media_type(media_type)
        return {"backend": "link", "uri": uri, "media_type": media_type,
                "immutable_bytes": False}

    def _local_payload(self, content: bytes, media_type: str) -> tuple[Dict[str, Any], bool]:
        self._check_bytes(content)
        _require_media_type(media_type)
        digest, created = self.objects.put(content)
        return ({"backend": "local", "algorithm": "sha256", "sha256": digest,
                 "size": len(content), "media_type": media_type,
                 "path": self.objects.relative_path(digest), "immutable_bytes": True}, created)

    def _record(self, *, record_id: str, title: str, profile: str, payload: Mapping[str, Any],
                revision: int, created_date: str, provenance: Mapping[str, str],
                retention: Mapping[str, Any], predecessor_id: Optional[str] = None,
                migration_metadata: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        now = _timestamp()
        system: Dict[str, Any] = {"artifact": {"payload_mode": payload["backend"],
                                                "durable_evidence": bool(payload["immutable_bytes"]),
                                                "retention": dict(retention)}}
        if provenance:
            system["provenance"] = dict(provenance)
        migration = _validate_migration_metadata(migration_metadata)
        if migration is not None:
            system["migration"] = migration
        relations = []
        if predecessor_id:
            relations.append({"type": "supersedes", "record_id": predecessor_id})
        record = {"id": record_id, "slug": default_slug(record_id, title), "aliases": [],
                  "kind": "artifact", "profile": profile, "title": title, "namespace": "default",
                  "lifecycle": "active", "tier": "hot", "schema_version": 2,
                  "media_type": payload["media_type"], "payload": dict(payload),
                  "created_date": created_date, "last_modified": now, "revision": revision,
                  "relations": relations, "system_metadata": system, "custom_metadata": {}}
        validate_record(record)
        return record

    def create(self, *, record_id: str, title: str, profile: str, mode: str,
               content: Optional[bytes] = None, media_type: str = "application/octet-stream",
               uri: Optional[str] = None, digest: Optional[str] = None, size: Optional[int] = None,
               provenance: Optional[Mapping[str, Any]] = None,
               retention: Optional[Mapping[str, Any]] = None,
               predecessor_id: Optional[str] = None,
               migration_metadata: Optional[Mapping[str, Any]] = None,
               required_git_roles=()) -> Dict[str, Any]:
        self._validate_id(record_id)
        if not isinstance(title, str) or not title.strip():
            raise RecordError("RECORD_INVALID", "artifact title must be a non-empty string")
        if predecessor_id is not None:
            self._validate_id(predecessor_id)
        if profile not in ARTIFACT_PROFILES:
            raise RecordError("ARTIFACT_PROFILE_INVALID", f"unsupported Artifact profile {profile!r}")
        if mode not in PAYLOAD_MODES:
            raise RecordError("ARTIFACT_MODE_INVALID", f"unsupported payload mode {mode!r}")
        provenance_value = _validate_provenance(provenance)
        retention_value = validate_retention(retention)
        created_object = False
        object_digest: Optional[str] = None
        manifest_path = self._revision_path(record_id, 1)
        event_path = self._event_path(record_id, 1)
        manifest_written = event_written = False
        lock = self._lock()
        try:
            if self._record_dir(record_id).exists() or self._cold_record(record_id) is not None:
                raise RecordError("ARTIFACT_ID_CONFLICT", f"Artifact {record_id} already exists")
            if predecessor_id is not None:
                self.get(predecessor_id)
            if mode == "inline":
                if content is None:
                    raise RecordError("ARTIFACT_BYTES_INVALID", "inline mode requires provided bytes")
                payload = self.inline_payload(content, media_type)
            elif mode == "local":
                if content is None:
                    raise RecordError("ARTIFACT_BYTES_INVALID", "local mode requires provided bytes")
                payload, created_object = self._local_payload(content, media_type)
                object_digest = payload["sha256"]
            elif mode == "external":
                if uri is None or digest is None or size is None:
                    raise RecordError("ARTIFACT_EXTERNAL_INCOMPLETE", "external evidence requires URI, digest, size, and media type")
                payload = self.external_payload(uri=uri, digest=digest, size=size, media_type=media_type,
                                                evidence_bytes=content)
            else:
                if uri is None:
                    raise RecordError("ARTIFACT_LINK_INCOMPLETE", "link-only payload requires a URI")
                if content is not None or digest is not None or size is not None:
                    raise RecordError("ARTIFACT_LINK_INVALID", "link-only payload makes no immutable byte claim")
                payload = self.link_payload(uri=uri, media_type=media_type)
            created = _timestamp()
            record = self._record(record_id=record_id, title=title, profile=profile, payload=payload,
                                  revision=1, created_date=created, provenance=provenance_value,
                                  retention=retention_value, predecessor_id=predecessor_id,
                                  migration_metadata=migration_metadata)
            context = capture_creation_context(
                controller_root=self.controller_root, project_root=self.project_root,
                required_roles=required_git_roles, repository_ids=self.repository_ids)
            record = attach_creation_context(record, context)
            validate_record(record)
            self._mutation_fault("after_object")
            encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode() + b"\n"
            event = {"operation": "create", "record_id": record_id, "revision": 1,
                     "record_sha256": sha256_bytes(encoded), "timestamp": created}
            self._atomic_create(manifest_path, encoded)
            manifest_written = True
            self._mutation_fault("after_manifest")
            self._atomic_create(event_path, json.dumps(event, sort_keys=True, separators=(",", ":")).encode() + b"\n")
            event_written = True
            self._mutation_fault("after_event")
            return record
        except BaseException:
            if event_written:
                event_path.unlink(missing_ok=True)
            if manifest_written:
                manifest_path.unlink(missing_ok=True)
            if created_object and object_digest:
                self.objects.remove_if_unreferenced_creation(object_digest)
            raise
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()

    def create_from_stream(self, stream: BinaryIO, **kwargs: Any) -> Dict[str, Any]:
        """Explicit stdin/pipe adapter; it captures no environment or run metadata."""
        content = stream.read(self.policy.max_capture_bytes + 1)
        if not isinstance(content, bytes):
            raise RecordError("ARTIFACT_BYTES_INVALID", "capture stream must be opened in binary mode")
        if len(content) > self.policy.max_capture_bytes:
            raise RecordError("ARTIFACT_TOO_LARGE", "artifact exceeds the configured capture limit")
        return self.create(content=content, **kwargs)

    def create_from_path(self, path: Path, *, allowed_root: Optional[Path] = None,
                         **kwargs: Any) -> Dict[str, Any]:
        """Explicit local-file adapter which refuses links and root escape."""
        source = Path(path)
        if source.is_symlink():
            raise RecordError("ARTIFACT_PATH_UNSAFE", "artifact source must not be a symlink")
        try:
            resolved = source.resolve(strict=True)
        except OSError as exc:
            raise RecordError("ARTIFACT_PATH_UNSAFE", "artifact source is missing or inaccessible") from exc
        if not resolved.is_file():
            raise RecordError("ARTIFACT_PATH_UNSAFE", "artifact source must be a regular file")
        if allowed_root is not None:
            try:
                resolved.relative_to(Path(allowed_root).resolve(strict=True))
            except (OSError, ValueError) as exc:
                raise RecordError("ARTIFACT_PATH_UNSAFE", "artifact source escapes the allowed root") from exc
        with resolved.open("rb") as stream:
            return self.create_from_stream(stream, **kwargs)

    def capture_stdout(self, *, content: bytes, **kwargs: Any) -> Dict[str, Any]:
        return self.create(profile="stdout", content=content, **kwargs)

    def capture_model_output(self, *, content: bytes, **kwargs: Any) -> Dict[str, Any]:
        return self.create(profile="model-output", content=content, **kwargs)

    def _cold_record(self, record_id: str) -> Optional[Dict[str, Any]]:
        from .archive import ArchiveFormatError, iter_archive_envelopes
        matches = [item for item in iter_archive_envelopes(self.root)
                   if item["task"]["id"].casefold() == record_id.casefold()]
        if len(matches) > 1:
            raise ArchiveFormatError("duplicate cold Record ID: %s" % record_id)
        if not matches:
            return None
        record = matches[0]["task"]
        return record if record.get("kind") == "artifact" else None

    def get(self, record_id: str, revision: Optional[int] = None) -> Dict[str, Any]:
        directory = self._record_dir(record_id)
        if revision is None:
            paths = sorted(directory.glob("*.json")) if directory.is_dir() else []
            if not paths:
                cold = self._cold_record(record_id)
                if cold is None:
                    raise RecordError("RECORD_NOT_FOUND", f"Artifact {record_id!r} does not exist")
                self.validate_payload(cold["payload"])
                return cold
            path = paths[-1]
        else:
            if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
                raise RecordError("ARTIFACT_REVISION_INVALID", "revision must be positive")
            path = self._revision_path(record_id, revision)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RecordError("RECORD_NOT_FOUND", f"Artifact revision does not exist") from exc
        validate_record(record)
        self.validate_payload(record["payload"])
        return record

    def _resolve_archive_id(self, id_or_slug: str) -> str:
        matches = []
        for directory in self.records_root.glob("*/*"):
            try:
                record = self.get(directory.name)
            except RecordError:
                continue
            if id_or_slug == record["id"] or id_or_slug == record["slug"] or id_or_slug in record["aliases"]:
                matches.append(record["id"])
        from .archive import iter_archive_envelopes
        for envelope in iter_archive_envelopes(self.root):
            record = envelope["task"]
            if record.get("kind") == "artifact" and (id_or_slug == record["id"] or
                    id_or_slug == record["slug"] or id_or_slug in record.get("aliases", [])):
                matches.append(record["id"])
        matches = sorted(set(matches))
        if not matches:
            raise RecordError("RECORD_NOT_FOUND", f"no Artifact matches {id_or_slug!r}")
        if len(matches) != 1:
            raise RecordError("RECORD_IDENTITY_AMBIGUOUS", "Artifact identity is ambiguous")
        return matches[0]

    @contextmanager
    def _record_archive_lock(self, id_or_slug: str):
        from .archive import _record_archive_owner
        record_id = self._resolve_archive_id(id_or_slug)
        with _record_archive_owner(self.root):
            handle = self._lock()
            try:
                if self._resolve_archive_id(id_or_slug) != record_id:
                    raise RecordError("RECORD_IDENTITY_AMBIGUOUS", "Artifact identity changed while locking")
                if self._cold_record(record_id) is not None:
                    raise RecordError("RECORD_ARCHIVED", "archived Records are immutable")
                yield record_id
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()

    def _record_archive_snapshot(self, record_id: str) -> Dict[str, Any]:
        revisions = sorted(self._record_dir(record_id).glob("*.json"))
        events = sorted((self.events_root / record_id[:2].lower() / record_id).glob("*.json"))
        if not revisions or len(revisions) != len(events):
            from .archive import ArchiveFormatError
            raise ArchiveFormatError("Artifact snapshot/history is incomplete")
        records = [json.loads(path.read_text(encoding="utf-8")) for path in revisions]
        event_values = [json.loads(path.read_text(encoding="utf-8")) for path in events]
        history = []
        for item, event in zip(records, event_values):
            history.extend(({"operation": "snapshot", "record_id": record_id,
                             "revision": item["revision"], "record": item}, event))
        current = records[-1]
        objects = []
        for item in records:
            payload = item["payload"]
            if payload.get("backend") == "local":
                reference = {"path": payload["path"], "sha256": payload["sha256"],
                             "size": payload["size"], "media_type": payload["media_type"]}
                if reference not in objects:
                    objects.append(reference)
        return {"record": current, "history": history,
                "paths": [*revisions, *events], "owned_objects": objects}

    def _record_archive_verify_objects(self, objects) -> None:
        for item in objects:
            if item["path"] != self.objects.relative_path(item["sha256"]):
                raise RecordError("ARTIFACT_PATH_UNSAFE", "local object path is not digest-derived")
            self.objects.verify(item["sha256"], size=item["size"])

    def _record_archive_refresh(self) -> None:
        # Artifact lookup is canonical-file based; no SQLite authority exists.
        return None

    def _git_head(self) -> Optional[str]:
        try:
            return subprocess.check_output(["git", "-C", str(self.root.parent), "rev-parse", "HEAD"],
                                           text=True, stderr=subprocess.DEVNULL).strip()
        except (OSError, subprocess.CalledProcessError):
            return None

    def _config_hash(self) -> str:
        encoded = json.dumps(self.policy.__dict__, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def archive_record(self, id_or_slug: str, *, expected_revision: int,
                       receipt_path: Optional[Path] = None,
                       provenance: Optional[Mapping[str, Any]] = None,
                       fault=None) -> Dict[str, Any]:
        from .archive import archive_record
        return archive_record(self, id_or_slug, expected_revision=expected_revision,
                              receipt_path=receipt_path, provenance=provenance, fault=fault)

    def doctor(self) -> list[Dict[str, str]]:
        """Verify hot/cold manifests, owned bytes, and report GC-only orphans."""
        from .archive import archive_doctor, iter_archive_envelopes
        failures = list(archive_doctor(self.root))
        referenced = set()
        hot_ids = set()
        for directory in self.records_root.glob("*/*"):
            revision_paths = sorted(directory.glob("*.json"))
            if not revision_paths:
                continue
            try:
                record = json.loads(revision_paths[-1].read_text(encoding="utf-8"))
                hot_ids.add(record["id"].casefold())
                for path in revision_paths:
                    revision = json.loads(path.read_text(encoding="utf-8"))
                    self.validate_payload(revision["payload"])
                    if revision["payload"].get("backend") == "local":
                        digest = revision["payload"]["sha256"]
                        self.objects.verify(digest, size=revision["payload"]["size"])
                        referenced.add(digest)
            except Exception as exc:
                failures.append({"path": str(directory), "error": str(exc)})
        try:
            for envelope in iter_archive_envelopes(self.root):
                record = envelope["task"]
                if record.get("kind") != "artifact":
                    continue
                if record["id"].casefold() in hot_ids:
                    raise RecordError("RECORD_TIER_CONFLICT", "Artifact exists in hot and cold tiers")
                for item in envelope.get("owned_objects", []):
                    self.objects.verify(item["sha256"], size=item.get("size"))
                    referenced.add(item["sha256"])
        except Exception as exc:
            failures.append({"path": str(self.root / "archive"), "error": str(exc)})
        object_root = self.root / "objects" / "sha256"
        for path in object_root.glob("*/*"):
            if path.is_symlink() or not path.is_file() or path.name not in referenced:
                failures.append({"path": str(path), "error": "unreferenced or unsafe object; explicit GC review required"})
        return failures

    def garbage_collection_plan(self) -> list[Dict[str, Any]]:
        """Return bounded orphan evidence; this method never deletes objects."""
        referenced = set()
        for directory in self.records_root.glob("*/*"):
            for path in directory.glob("*.json"):
                payload = json.loads(path.read_text(encoding="utf-8"))["payload"]
                if payload.get("backend") == "local":
                    referenced.add(payload["sha256"])
        from .archive import iter_archive_envelopes
        for envelope in iter_archive_envelopes(self.root):
            referenced.update(item["sha256"] for item in envelope.get("owned_objects", []))
        return [{"path": str(path), "sha256": path.name, "size": path.stat().st_size}
                for path in sorted((self.root / "objects" / "sha256").glob("*/*"))
                if path.is_file() and path.name not in referenced]

    def archive_history(self, record_id: str) -> list[Dict[str, Any]]:
        from .archive import iter_archive_envelopes
        matches = [item for item in iter_archive_envelopes(self.root)
                   if item["task"]["id"] == record_id and item["task"].get("kind") == "artifact"]
        if len(matches) != 1:
            raise RecordError("RECORD_NOT_FOUND", "archived Artifact history does not resolve exactly")
        return list(matches[0]["ledger"])

    def validate_payload(self, payload: Mapping[str, Any]) -> None:
        if not isinstance(payload, Mapping) or payload.get("backend") not in PAYLOAD_MODES:
            raise RecordError("ARTIFACT_MANIFEST_INVALID", "unsupported payload descriptor")
        mode = payload["backend"]
        _require_media_type(payload.get("media_type"))
        if mode in ("inline", "local", "external"):
            ContentObjectStore._validate_digest(payload.get("sha256"))
            if not isinstance(payload.get("size"), int) or isinstance(payload.get("size"), bool) or payload["size"] < 0:
                raise RecordError("ARTIFACT_MANIFEST_INVALID", "durable payload requires a non-negative size")
            if payload.get("immutable_bytes") is not True:
                raise RecordError("ARTIFACT_MANIFEST_INVALID", "durable payload must claim immutable bytes")
        elif payload.get("immutable_bytes") is not False or set(payload) & {"sha256", "size", "data"}:
            raise RecordError("ARTIFACT_MANIFEST_INVALID", "link-only payload must make no immutable byte claim")

    def verify(self, record_id: str, *, revision: Optional[int] = None,
               external_resolver: Optional[Callable[[str], bytes]] = None) -> Optional[bytes]:
        record = self.get(record_id, revision)
        payload = record["payload"]
        mode = payload["backend"]
        if mode == "inline":
            try:
                content = base64.b64decode(payload["data"], validate=True)
            except Exception as exc:
                raise RecordError("ARTIFACT_ENCODING_INVALID", "inline payload is not canonical base64") from exc
        elif mode == "local":
            expected_path = self.objects.relative_path(payload["sha256"])
            if payload.get("path") != expected_path:
                raise RecordError("ARTIFACT_PATH_UNSAFE", "local object path is not digest-derived")
            content = self.objects.verify(payload["sha256"], size=payload["size"])
        elif mode == "external":
            if external_resolver is None:
                return None
            content = external_resolver(payload["uri"])
            if not isinstance(content, bytes):
                raise RecordError("ARTIFACT_BYTES_INVALID", "external resolver must return bytes")
        else:
            return None
        if len(content) != payload["size"]:
            raise RecordError("ARTIFACT_SIZE_MISMATCH", "payload bytes differ from manifest size")
        if sha256_bytes(content) != payload["sha256"]:
            raise RecordError("ARTIFACT_DIGEST_MISMATCH", "payload bytes differ from manifest digest")
        return content

    def revise(self, record_id: str, *, expected_revision: int,
               expected_payload: Mapping[str, Any], successor_id: Optional[str] = None,
               **replacement: Any) -> Dict[str, Any]:
        """Create immutable revision bytes after an exact descriptor preimage match.

        Supplying ``successor_id`` creates a distinct successor Record; otherwise a
        new immutable revision is added under the same Record identity.
        """
        current = self.get(record_id)
        if current.get("tier") == "cold" and successor_id is None:
            raise RecordError("RECORD_ARCHIVED", "archived Records are immutable; create a successor")
        if current["revision"] != expected_revision:
            raise RecordError("REVISION_CONFLICT", "artifact revision is stale")
        if value_digest(current["payload"]) != value_digest(expected_payload) or current["payload"] != dict(expected_payload):
            raise RecordError("EXACT_MATCH_NOT_FOUND", "artifact payload descriptor is not the exact preimage")
        target_id = successor_id or record_id
        if successor_id:
            return self.create(record_id=successor_id, title=replacement.pop("title", current["title"]),
                               profile=replacement.pop("profile", current["profile"]),
                               predecessor_id=record_id, **replacement)
        # Build replacement through create's validated payload machinery in a
        # temporary identity, then transact it as the next immutable revision.
        mode = replacement.pop("mode")
        title = replacement.pop("title", current["title"])
        if not isinstance(title, str) or not title.strip():
            raise RecordError("RECORD_INVALID", "artifact title must be a non-empty string")
        profile = replacement.pop("profile", current["profile"])
        if profile not in ARTIFACT_PROFILES:
            raise RecordError("ARTIFACT_PROFILE_INVALID", f"unsupported Artifact profile {profile!r}")
        provenance = _validate_provenance(replacement.pop("provenance", None))
        retention = validate_retention(replacement.pop("retention", current["system_metadata"]["artifact"]["retention"]))
        if replacement:
            allowed = {"content", "media_type", "uri", "digest", "size"}
            if set(replacement) - allowed:
                raise TypeError("unsupported revision arguments")
        # Use a side-effect-free payload builder except local, whose object is
        # rolled back if the revision transaction fails.
        content = replacement.get("content")
        media_type = replacement.get("media_type", current["media_type"])
        created_object = False
        object_digest = None
        if mode == "inline":
            payload = self.inline_payload(content, media_type)
        elif mode == "local":
            payload, created_object = self._local_payload(content, media_type)
            object_digest = payload["sha256"]
        elif mode == "external":
            if replacement.get("uri") is None or replacement.get("digest") is None or replacement.get("size") is None:
                raise RecordError("ARTIFACT_EXTERNAL_INCOMPLETE", "external evidence requires URI, digest, size, and media type")
            payload = self.external_payload(uri=replacement["uri"], digest=replacement["digest"],
                                            size=replacement["size"], media_type=media_type,
                                            evidence_bytes=content)
        elif mode == "link":
            if replacement.get("uri") is None:
                raise RecordError("ARTIFACT_LINK_INCOMPLETE", "link-only payload requires a URI")
            if content is not None or replacement.get("digest") is not None or replacement.get("size") is not None:
                raise RecordError("ARTIFACT_LINK_INVALID", "link-only payload makes no immutable byte claim")
            payload = self.link_payload(uri=replacement["uri"], media_type=media_type)
        else:
            raise RecordError("ARTIFACT_MODE_INVALID", f"unsupported payload mode {mode!r}")
        revision = expected_revision + 1
        record = self._record(record_id=target_id, title=title, profile=profile, payload=payload,
                              revision=revision, created_date=current["created_date"],
                              provenance=provenance, retention=retention)
        creation_context = (current.get("system_metadata") or {}).get("creation_context")
        if creation_context is not None:
            record = attach_creation_context(record, creation_context)
            validate_record(record)
        manifest_path, event_path = self._revision_path(target_id, revision), self._event_path(target_id, revision)
        manifest_written = event_written = False
        lock = self._lock()
        try:
            latest = self.get(record_id)
            if latest["revision"] != expected_revision or latest["payload"] != dict(expected_payload):
                raise RecordError("REVISION_CONFLICT", "artifact changed before revision activation")
            encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode() + b"\n"
            event = {"operation": "revise", "record_id": target_id, "revision": revision,
                     "preimage_sha256": value_digest(expected_payload), "record_sha256": sha256_bytes(encoded),
                     "timestamp": record["last_modified"]}
            self._mutation_fault("after_object")
            self._atomic_create(manifest_path, encoded); manifest_written = True
            self._mutation_fault("after_manifest")
            self._atomic_create(event_path, json.dumps(event, sort_keys=True, separators=(",", ":")).encode() + b"\n"); event_written = True
            self._mutation_fault("after_event")
            return record
        except BaseException:
            if event_written: event_path.unlink(missing_ok=True)
            if manifest_written: manifest_path.unlink(missing_ok=True)
            if created_object and object_digest: self.objects.remove_if_unreferenced_creation(object_digest)
            raise
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN); lock.close()
