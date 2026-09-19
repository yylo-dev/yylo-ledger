"""Revision-pinned, preservation-first publication of package-owned wiki Records.

The caller authenticates its release artifact. These APIs verify byte identity,
not signatures or permission to activate a controller generation. Publication
stages revisions; only a complete receipt may be used as a generation binding.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import string
from contextlib import ExitStack
from pathlib import PurePosixPath
from typing import Any, Mapping

from .artifacts import _SECRET_PATTERNS
from .documents import (DocumentStore, _timestamp, create_document,
                        validate_document, validate_record_links)
from .git_creation import attach_creation_context, capture_creation_context
from .records import RECORD_ID_RE, RecordError, RevisionProvenance, value_digest

MANIFEST_SCHEMA = "yylo_package_wiki_manifest.v1"
PLAN_SCHEMA = "yylo_package_wiki_plan.v1"
RECEIPT_SCHEMA = "yylo_package_wiki_receipt.v1"
OWNER_KEY = "package_wiki"
_HEX = re.compile(r"[0-9a-f]{64}")


def _fail(code: str, message: str) -> None:
    raise RecordError("PACKAGE_WIKI_" + code, message)


def package_record_id(package: str, key: str) -> str:
    """Stable six-character Record identity; collisions are refused, never adopted."""
    alphabet = string.ascii_letters + string.digits
    for nonce in range(128):
        seed = f"{package}\0{key}\0{nonce}".encode()
        number = int.from_bytes(hashlib.sha256(seed).digest(), "big")
        result = ""
        for _ in range(6):
            number, remainder = divmod(number, len(alphabet))
            result += alphabet[remainder]
        if RECORD_ID_RE.fullmatch(result):
            return result
    _fail("IDENTITY_CONFLICT", "could not derive a valid package Record ID")


def validate_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(manifest, dict) or set(manifest) != {
            "schema_version", "package", "version", "artifact_sha256", "entries"}:
        _fail("MANIFEST_INVALID", "manifest fields do not match the v1 schema")
    if manifest["schema_version"] != MANIFEST_SCHEMA:
        _fail("MANIFEST_INVALID", "unsupported manifest schema")
    for name in ("package", "version"):
        value = manifest[name]
        if not isinstance(value, str) or not value.strip() or len(value) > 200 or "\0" in value:
            _fail("MANIFEST_INVALID", f"invalid {name}")
    if not isinstance(manifest["artifact_sha256"], str) or not _HEX.fullmatch(manifest["artifact_sha256"]):
        _fail("MANIFEST_INVALID", "artifact_sha256 must identify the authenticated release")
    entries = manifest["entries"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= 512:
        _fail("MANIFEST_INVALID", "expected 1..512 explicit wiki entries")
    seen, total = set(), 0
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"key", "title", "text", "sha256"}:
            _fail("MANIFEST_INVALID", "invalid entry fields")
        key = entry["key"]
        if (not isinstance(key, str) or not key or len(key) > 200 or "\\" in key
                or "\0" in key or PurePosixPath(key).is_absolute()
                or any(part in ("", ".", "..") for part in key.split("/"))
                or not key.endswith(".md")):
            _fail("MANIFEST_INVALID", "wiki keys must be safe relative Markdown paths")
        identity = package_record_id(manifest["package"], key)
        if identity.casefold() in seen:
            _fail("IDENTITY_CONFLICT", "duplicate entry or stable-ID collision")
        seen.add(identity.casefold())
        if not isinstance(entry["title"], str) or not entry["title"].strip():
            _fail("MANIFEST_INVALID", "entry title is required")
        if not isinstance(entry["text"], str):
            _fail("MANIFEST_INVALID", "entry text must be UTF-8 text")
        data = entry["text"].encode("utf-8")
        total += len(data)
        if len(data) > 1024 * 1024 or total > 8 * 1024 * 1024:
            _fail("MANIFEST_INVALID", "publication exceeds bounded payload limits")
        if any(pattern.search(data) for pattern in _SECRET_PATTERNS):
            _fail("SECRET_REJECTED", "release guidance matches the credential/secret policy")
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            _fail("DIGEST_MISMATCH", f"source digest mismatch: {key}")
        # Canonical validation includes LF, profile and typed-link syntax checks.
        create_document(record_id=identity, title=entry["title"], profile="wiki",
                        media_type="text/markdown", text=entry["text"])
    result = copy.deepcopy(manifest)
    result["entries"].sort(key=lambda row: row["key"])
    return result


def _managed_digest(record: Mapping[str, Any]) -> str:
    return value_digest({key: record[key] for key in (
        "id", "slug", "aliases", "kind", "profile", "title", "namespace",
        "lifecycle", "tier", "schema_version", "media_type", "payload",
        "relations", "custom_metadata")})


class PackageWikiStore(DocumentStore):
    """Canonical Document engine extension; never writes controller selectors."""

    def _current(self, record_id: str) -> dict[str, Any] | None:
        try:
            return self.get(record_id)
        except RecordError as exc:
            if exc.code != "RECORD_NOT_FOUND":
                raise
            return None

    def _owned(self, current: dict[str, Any], package: str, key: str) -> dict[str, Any]:
        owner = current["system_metadata"].get(OWNER_KEY)
        if not isinstance(owner, dict) or (owner.get("package"), owner.get("key")) != (package, key):
            _fail("OWNERSHIP_CONFLICT", f"refusing to adopt project Record {current['id']}")
        if (current["profile"] != "wiki" or current["tier"] != "hot"
                or current["revision"] != owner.get("revision")
                or _managed_digest(current) != owner.get("managed_sha256")):
            _fail("LOCAL_CONFLICT", f"preserve modified or archived Record {current['id']}")
        return owner

    def plan(self, manifest: Mapping[str, Any]) -> dict[str, Any]:
        manifest = validate_manifest(manifest)
        rows = []
        for entry in manifest["entries"]:
            identity = package_record_id(manifest["package"], entry["key"])
            current = self._current(identity)
            if current is not None:
                self._owned(current, manifest["package"], entry["key"])
            rows.append({"id": identity, "revision": current["revision"] if current else 0,
                         "record_sha256": value_digest(current) if current else None})
        body = {"schema_version": PLAN_SCHEMA, "manifest_sha256": value_digest(manifest),
                "records": rows}
        return {**body, "plan_sha256": value_digest(body)}

    def publish(self, manifest: Mapping[str, Any], plan: Mapping[str, Any], *, after_record=None) -> dict[str, Any]:
        """Stage a fully preflighted bundle, with CAS and resumable per-record writes.

        A crash may leave a prefix of immutable revisions. Retrying this EXACT plan
        recognizes only exact resulting revisions; unrelated edits remain conflicts.
        No rollback deletes evidence. A generation rollback selects an old receipt.
        """
        manifest = validate_manifest(manifest)
        digest = value_digest(manifest)
        if not isinstance(plan, dict) or set(plan) != {"schema_version", "manifest_sha256", "records", "plan_sha256"}:
            _fail("PLAN_INVALID", "invalid publication plan")
        body = {key: value for key, value in plan.items() if key != "plan_sha256"}
        if (plan["schema_version"] != PLAN_SCHEMA or plan["manifest_sha256"] != digest
                or plan["plan_sha256"] != value_digest(body)):
            _fail("PLAN_INVALID", "plan does not bind the exact manifest")
        rows = plan["records"]
        identities = [package_record_id(manifest["package"], item["key"]) for item in manifest["entries"]]
        if (not isinstance(rows, list) or len(rows) != len(identities)
                or any(not isinstance(row, dict) or set(row) != {"id", "revision", "record_sha256"}
                       or type(row["revision"]) is not int or row["revision"] < 0
                       or (row["record_sha256"] is not None and
                           (not isinstance(row["record_sha256"], str) or not _HEX.fullmatch(row["record_sha256"])))
                       or (row["revision"] == 0) != (row["record_sha256"] is None) for row in rows)
                or [row["id"] for row in rows] != identities):
            _fail("PLAN_INVALID", "plan entries are malformed or incomplete")
        with ExitStack() as locks:
            # Store lock filenames are case-folded; order by the actual lock
            # identity to avoid AB/ab inversions between overlapping bundles.
            for identity in sorted({identity.casefold() for identity in identities}):
                locks.enter_context(self._lock(identity))
            candidates = []
            for entry, expected in zip(manifest["entries"], rows):
                current = self._current(expected["id"])
                owner = self._owned(current, manifest["package"], entry["key"]) if current else None
                if owner and owner["manifest_sha256"] == digest:
                    # Either a replayed commit of this plan, or an already-published
                    # identical bundle observed when the plan was made.
                    if current["revision"] not in (expected["revision"], expected["revision"] + 1):
                        _fail("REVISION_CONFLICT", expected["id"])
                    candidates.append((current, False))
                    continue
                if ((current["revision"] if current else 0) != expected["revision"]
                        or (value_digest(current) if current else None) != expected["record_sha256"]):
                    _fail("REVISION_CONFLICT", expected["id"])
                if (owner and owner["version"] == manifest["version"]
                        and owner["artifact_sha256"] == manifest["artifact_sha256"]):
                    _fail("RELEASE_CONFLICT", "same package version/artifact has different manifest bytes")
                if current is not None and not self._check_publication_event(current):
                    _fail("HISTORY_INCOMPLETE", "resume the prior exact publication before upgrading")
                candidate = self._candidate(manifest, entry, current, digest)
                candidates.append((candidate, True))
            # Validate typed links against this whole bundle or existing Documents.
            available = dict(zip(identities, (record for record, _ in candidates)))
            def resolve(identity):
                return available.get(identity) or self._current(identity)
            for record, _ in candidates:
                validate_record_links(record["payload"]["text"], resolve)
            # Reject corrupt existing events before writing any bundle member.
            for record, changed in candidates:
                if not changed:
                    self._check_publication_event(record)
            for record, changed in candidates:
                if changed:
                    self._commit_publication(record)
                else:
                    # A hard exit between revision and event publication is a
                    # recoverable prefix only for this exact immutable revision.
                    path = self._event_directory(record["id"]) / f"{record['revision']:08d}.json"
                    if not path.exists():
                        self._atomic_create(path, self._publication_event(record))
                if after_record is not None:
                    after_record(record)
            receipt = {"schema_version": RECEIPT_SCHEMA, "manifest_sha256": digest,
                       "package": manifest["package"], "version": manifest["version"],
                       "artifact_sha256": manifest["artifact_sha256"],
                       "records": [{"key": entry["key"], "id": record["id"],
                                    "revision": record["revision"], "record_sha256": value_digest(record),
                                    "payload_sha256": record["payload"]["sha256"]}
                                   for entry, (record, _) in zip(manifest["entries"], candidates)]}
            return {**receipt, "receipt_sha256": value_digest(receipt)}

    def generation_binding(self, manifest, plan):
        """Derive timestamp-independent pins from exact historical CAS preimages."""
        manifest = validate_manifest(manifest)
        if (not isinstance(plan, dict) or set(plan) != {"schema_version", "manifest_sha256", "records", "plan_sha256"}
                or not isinstance(plan.get("records"), list)
                or any(not isinstance(row, dict) or set(row) != {"id", "revision", "record_sha256"}
                       for row in plan["records"])):
            _fail("PLAN_INVALID", "invalid publication plan")
        body = {key: value for key, value in plan.items() if key != "plan_sha256"}
        if (plan.get("schema_version") != PLAN_SCHEMA or plan.get("manifest_sha256") != value_digest(manifest)
                or plan.get("plan_sha256") != value_digest(body)
                or len(plan.get("records", [])) != len(manifest["entries"])):
            _fail("PLAN_INVALID", "plan does not bind the exact manifest")
        pins = []
        for entry, row in zip(manifest["entries"], plan["records"]):
            identity = package_record_id(manifest["package"], entry["key"])
            if row.get("id") != identity or type(row.get("revision")) is not int or row["revision"] < 0:
                _fail("PLAN_INVALID", "invalid historical revision pin")
            revision = row["revision"] + 1
            if row["revision"]:
                old = self.get(identity, row["revision"])
                if value_digest(old) != row["record_sha256"]:
                    _fail("PLAN_INVALID", "historical preimage differs")
                owner = self._owned(old, manifest["package"], entry["key"])
                if owner["manifest_sha256"] == plan["manifest_sha256"]:
                    revision = row["revision"]
            elif row.get("record_sha256") is not None:
                _fail("PLAN_INVALID", "new Record has a preimage digest")
            pins.append({"key": entry["key"], "id": identity, "revision": revision,
                         "payload_sha256": entry["sha256"]})
        return {"schema_version": "yylo_package_wiki_binding.v1", "package": manifest["package"],
                "version": manifest["version"], "artifact_sha256": manifest["artifact_sha256"],
                "manifest_sha256": plan["manifest_sha256"], "records": pins}

    def verify_generation_binding(self, binding):
        if (not isinstance(binding, dict) or set(binding) != {"schema_version", "package", "version",
                "artifact_sha256", "manifest_sha256", "records"}
                or binding["schema_version"] != "yylo_package_wiki_binding.v1"
                or not isinstance(binding["records"], list) or not 1 <= len(binding["records"]) <= 512):
            _fail("BINDING_INVALID", "invalid generation wiki binding")
        seen = set()
        for pin in binding["records"]:
            if (not isinstance(pin, dict) or set(pin) != {"key", "id", "revision", "payload_sha256"}
                    or not isinstance(pin["key"], str) or not isinstance(binding["package"], str)
                    or type(pin["revision"]) is not int or pin["revision"] < 1
                    or pin["id"] != package_record_id(binding["package"], pin["key"])
                    or pin["key"] in seen):
                _fail("BINDING_INVALID", "invalid generation wiki pin")
            seen.add(pin["key"])
            record = self.get(pin["id"], pin["revision"])
            owner = self._owned(record, binding["package"], pin["key"])
            if (record["payload"]["sha256"] != pin["payload_sha256"] or any(
                    owner.get(name) != binding[name] for name in
                    ("package", "version", "artifact_sha256", "manifest_sha256"))):
                _fail("BINDING_INVALID", "generation wiki differs from release binding")
        return binding

    def read_binding(self, receipt: Mapping[str, Any], key: str) -> dict[str, Any]:
        """Read an exact generation-pinned revision, never falling back to latest."""
        fields = {"schema_version", "manifest_sha256", "package", "version",
                  "artifact_sha256", "records", "receipt_sha256"}
        if not isinstance(receipt, dict) or set(receipt) != fields:
            _fail("BINDING_INVALID", "invalid package publication receipt")
        body = {name: value for name, value in receipt.items() if name != "receipt_sha256"}
        if (receipt["schema_version"] != RECEIPT_SCHEMA
                or receipt["receipt_sha256"] != value_digest(body)
                or not isinstance(receipt["records"], list)):
            _fail("BINDING_INVALID", "package receipt seal or schema mismatch")
        matches = [row for row in receipt["records"] if isinstance(row, dict) and row.get("key") == key]
        if len(matches) != 1:
            _fail("BINDING_INVALID", "package key is absent or ambiguous")
        pin = matches[0]
        if (set(pin) != {"key", "id", "revision", "record_sha256", "payload_sha256"}
                or type(pin["revision"]) is not int or pin["revision"] < 1
                or not isinstance(receipt["package"], str)
                or pin["id"] != package_record_id(receipt["package"], key)):
            _fail("BINDING_INVALID", "invalid package revision pin")
        record = self.get(pin["id"], pin["revision"])
        owner = self._owned(record, receipt["package"], key)
        if (value_digest(record) != pin["record_sha256"]
                or record["payload"]["sha256"] != pin["payload_sha256"]
                or any(owner.get(name) != receipt[name] for name in
                       ("package", "version", "artifact_sha256", "manifest_sha256"))):
            _fail("BINDING_INVALID", "pinned revision differs from publication receipt")
        return record

    def _candidate(self, manifest, entry, current, digest):
        actor = RevisionProvenance(actor_type="package", actor=manifest["package"],
                                   run_id=digest)
        if current is None:
            record = create_document(record_id=package_record_id(manifest["package"], entry["key"]),
                title=entry["title"], profile="wiki", media_type="text/markdown", text=entry["text"],
                namespace="package:" + manifest["package"], provenance=actor)
            record = attach_creation_context(record, capture_creation_context(
                controller_root=self.controller_root, project_root=self.project_root,
                repository_ids=self.repository_ids))
        else:
            record = copy.deepcopy(current)
            record["title"] = entry["title"]
            record["payload"].update(text=entry["text"], sha256=entry["sha256"])
            record["revision"] += 1
            record["last_modified"] = _timestamp()
            record["system_metadata"]["revision_provenance"].append(actor.to_dict())
        record["system_metadata"][OWNER_KEY] = {
            "package": manifest["package"], "key": entry["key"], "version": manifest["version"],
            "artifact_sha256": manifest["artifact_sha256"], "manifest_sha256": digest,
            "revision": record["revision"], "managed_sha256": _managed_digest(record)}
        validate_document(record, registry=self.registry)
        return record

    @staticmethod
    def _publication_event(record):
        return {"operation": "package-wiki-publish", "record_id": record["id"],
                "revision": record["revision"], "record_sha256": value_digest(record),
                "publication": record["system_metadata"][OWNER_KEY]}

    def _check_publication_event(self, record):
        path = self._event_directory(record["id"]) / f"{record['revision']:08d}.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return False
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RecordError("PACKAGE_WIKI_HISTORY_CONFLICT", "preserve malformed publication event") from exc
        if value != self._publication_event(record):
            _fail("HISTORY_CONFLICT", "preserve conflicting publication event")
        return True

    def _commit_publication(self, record):
        identity, revision = record["id"], record["revision"]
        event = self._publication_event(record)
        written = []
        try:
            for path, value in ((self._directory(identity) / f"{revision:08d}.json", record),
                                (self._event_directory(identity) / f"{revision:08d}.json", event)):
                self._atomic_create(path, value)
                written.append(path)
        except BaseException:
            for path in written:
                path.unlink(missing_ok=True)
            raise
