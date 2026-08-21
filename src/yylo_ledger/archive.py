"""Deterministic, immutable cold-archive pack codec.

The NDJSON pack is canonical.  Manifests and checksum sidecars are derived and
may be rebuilt without reading hot task storage.
"""
from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .codec import normalized_bytes, plain_value
from .ledger import LedgerError

ARCHIVE_SCHEMA = 1
DEFAULT_TARGET_BYTES = 25 * 1024 * 1024
DEFAULT_HARD_MAX_BYTES = 45 * 1024 * 1024
DEFAULT_MAX_RECORDS = 1000
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class ArchiveFormatError(ValueError):
    """Raised when archive bytes or their derived metadata fail verification."""


@dataclass(frozen=True)
class PackArtifact:
    pack: Path
    manifest: Path
    checksum: Path
    pack_sha256: str
    manifest_sha256: str
    record_count: int
    size_bytes: int
    oversized_record: bool


@dataclass(frozen=True)
class ArchiveCandidate:
    task_id: str
    terminal_transition_at: str
    task_sha256: str
    ledger_sha256: str
    ledger_tip_sha256: str
    estimated_bytes: int


def _canonical(value: Any) -> bytes:
    return json.dumps(plain_value(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _parse_utc(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ArchiveFormatError("%s must be an ISO-8601 timestamp" % field) from exc
    if parsed.tzinfo is None:
        raise ArchiveFormatError("%s must be timezone-aware" % field)
    return parsed.astimezone(timezone.utc)


def _utc_text(value: str, field: str) -> str:
    return _parse_utc(value, field).isoformat().replace("+00:00", "Z")


def _event_hash(event: Mapping[str, Any]) -> str:
    return _sha(_canonical({key: value for key, value in event.items()
                            if key != "event_sha256"}))


def _verify_ledger(task_id: str, task_sha256: str,
                   ledger: Sequence[Mapping[str, Any]]) -> None:
    if not ledger:
        raise ArchiveFormatError("archive ledger for %s is empty" % task_id)
    previous = None
    previous_after = None
    for index, event in enumerate(ledger):
        if not isinstance(event, Mapping):
            raise ArchiveFormatError("archive ledger event %d is not an object" % index)
        if event.get("task_id") != task_id:
            raise ArchiveFormatError("archive ledger event task ID mismatch for %s" % task_id)
        if event.get("previous_event_sha256") != previous:
            raise ArchiveFormatError("archive ledger chain discontinuity for %s" % task_id)
        event_sha = event.get("event_sha256")
        if event_sha != _event_hash(event):
            raise ArchiveFormatError("archive ledger event hash mismatch for %s" % task_id)
        if event.get("before_sha256") != previous_after:
            raise ArchiveFormatError("archive ledger state hash discontinuity for %s" % task_id)
        if index == 0:
            if event.get("operation") != "create" or "snapshot" not in event:
                raise ArchiveFormatError("archive ledger for %s lacks its creation snapshot" % task_id)
            if _sha(_canonical(event["snapshot"])) != event.get("after_sha256"):
                raise ArchiveFormatError("archive creation snapshot hash mismatch for %s" % task_id)
        previous = event_sha
        previous_after = event.get("after_sha256")
    if ledger[-1].get("after_sha256") != task_sha256:
        raise ArchiveFormatError("archive ledger/current-state mismatch for %s" % task_id)


def make_envelope(task: Mapping[str, Any], ledger: Sequence[Mapping[str, Any]],
                  archived_at: str, terminal_transition_at: str,
                  source_revision: str) -> Dict[str, Any]:
    """Create and verify one lossless archive envelope.

    Hashes are over normalized semantic JSON.  ``record_sha256`` covers every
    envelope field except itself, avoiding a self-referential digest.
    """
    task_value = plain_value(task)
    ledger_value = plain_value(list(ledger))
    if not isinstance(task_value, dict) or not isinstance(task_value.get("id"), str):
        raise ArchiveFormatError("archive task must be an object with a string ID")
    task_id = task_value["id"]
    archived_at = _utc_text(archived_at, "archived_at")
    terminal_transition_at = _utc_text(terminal_transition_at, "terminal_transition_at")
    if _parse_utc(archived_at, "archived_at") < _parse_utc(
            terminal_transition_at, "terminal_transition_at"):
        raise ArchiveFormatError("archived_at cannot precede terminal_transition_at")
    if (not isinstance(source_revision, str)
            or not source_revision.startswith("sha256:")
            or not _HEX64.match(source_revision[7:])):
        raise ArchiveFormatError("source_revision must be a sha256 digest")
    task_hash = _sha(_canonical(task_value))
    _verify_ledger(task_id, task_hash, ledger_value)
    envelope: Dict[str, Any] = {
        "archive_schema": ARCHIVE_SCHEMA,
        "task": task_value,
        "ledger": ledger_value,
        "archived_at": archived_at,
        "terminal_transition_at": terminal_transition_at,
        "source_revision": source_revision,
        "task_sha256": task_hash,
        "ledger_sha256": _sha(_canonical(ledger_value)),
    }
    envelope["record_sha256"] = _sha(_canonical(envelope))
    return envelope


def encode_envelope(envelope: Mapping[str, Any]) -> bytes:
    """Return one canonical NDJSON record, including its sole LF terminator."""
    verify_envelope(envelope)
    return _canonical(envelope) + b"\n"


def verify_envelope(envelope: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(envelope, Mapping) or envelope.get("archive_schema") != ARCHIVE_SCHEMA:
        raise ArchiveFormatError("unsupported archive record schema")
    task = envelope.get("task")
    ledger = envelope.get("ledger")
    if not isinstance(task, Mapping) or not isinstance(task.get("id"), str):
        raise ArchiveFormatError("archive record has no task ID")
    if not isinstance(ledger, list):
        raise ArchiveFormatError("archive record ledger must be a list")
    for field in ("archived_at", "terminal_transition_at"):
        if envelope.get(field) != _utc_text(envelope.get(field), field):
            raise ArchiveFormatError("%s must use canonical UTC form" % field)
    if _parse_utc(envelope["archived_at"], "archived_at") < _parse_utc(
            envelope["terminal_transition_at"], "terminal_transition_at"):
        raise ArchiveFormatError("archived_at cannot precede terminal_transition_at")
    source_revision = envelope.get("source_revision")
    if (not isinstance(source_revision, str)
            or not source_revision.startswith("sha256:")
            or not _HEX64.match(source_revision[7:])):
        raise ArchiveFormatError("source_revision must be a sha256 digest")
    task_hash = _sha(_canonical(task))
    ledger_hash = _sha(_canonical(ledger))
    if envelope.get("task_sha256") != task_hash:
        raise ArchiveFormatError("archive task hash mismatch for %s" % task["id"])
    if envelope.get("ledger_sha256") != ledger_hash:
        raise ArchiveFormatError("archive ledger hash mismatch for %s" % task["id"])
    supplied = envelope.get("record_sha256")
    payload = {key: value for key, value in envelope.items() if key != "record_sha256"}
    if not isinstance(supplied, str) or supplied != _sha(_canonical(payload)):
        raise ArchiveFormatError("archive record hash mismatch for %s" % task["id"])
    _verify_ledger(str(task["id"]), task_hash, ledger)
    return dict(envelope)


def decode_envelope(raw: bytes) -> Dict[str, Any]:
    if not raw.endswith(b"\n") or raw.endswith(b"\n\n") or b"\r" in raw:
        raise ArchiveFormatError("archive record must have exactly one LF terminator")
    try:
        value = json.loads(raw[:-1].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArchiveFormatError("invalid archive record JSON: %s" % exc) from exc
    return verify_envelope(value)


def _ordered_encoded(envelopes: Iterable[Mapping[str, Any]]) -> List[Tuple[str, bytes]]:
    records = []
    seen = set()
    for envelope in envelopes:
        verified = verify_envelope(envelope)
        task_id = verified["task"]["id"]
        folded = task_id.casefold()
        if folded in seen:
            raise ArchiveFormatError("duplicate or case-insensitive archive task ID: %s" % task_id)
        seen.add(folded)
        records.append((task_id, _canonical(verified) + b"\n"))
    records.sort(key=lambda item: item[0].encode("utf-8"))
    return records


def split_records(envelopes: Iterable[Mapping[str, Any]],
                  target_bytes: int = DEFAULT_TARGET_BYTES,
                  hard_max_bytes: int = DEFAULT_HARD_MAX_BYTES,
                  max_records: int = DEFAULT_MAX_RECORDS) -> List[List[Tuple[str, bytes]]]:
    """Deterministically split records; an oversized record is always isolated."""
    if target_bytes <= 0 or hard_max_bytes < target_bytes or max_records <= 0 or max_records > 1000:
        raise ValueError("invalid archive pack limits")
    batches: List[List[Tuple[str, bytes]]] = []
    current: List[Tuple[str, bytes]] = []
    current_size = 0
    for item in _ordered_encoded(envelopes):
        size = len(item[1])
        if size > hard_max_bytes:
            if current:
                batches.append(current)
                current, current_size = [], 0
            batches.append([item])
            continue
        if current and (len(current) >= max_records or
                        current_size + size > target_bytes or
                        current_size + size > hard_max_bytes):
            batches.append(current)
            current, current_size = [], 0
        current.append(item)
        current_size += size
    if current:
        batches.append(current)
    return batches


def _manifest_payload(pack_name: str, pack_hash: str, pack_size: int,
                      entries: List[Dict[str, Any]], source_head: str,
                      config_sha256: str, creator_version: str,
                      hard_max_bytes: int) -> Dict[str, Any]:
    terminal = [entry["terminal_transition_at"] for entry in entries]
    oversized = [{"task_id": entry["task_id"], "size_bytes": entry["length"],
                  "hard_max_bytes": hard_max_bytes}
                 for entry in entries if entry["length"] > hard_max_bytes]
    return {
        "archive_schema": ARCHIVE_SCHEMA,
        "pack": pack_name,
        "pack_sha256": pack_hash,
        "pack_size_bytes": pack_size,
        "record_count": len(entries),
        "records": entries,
        "minimum_terminal_transition_at": min(terminal) if terminal else None,
        "maximum_terminal_transition_at": max(terminal) if terminal else None,
        "oversized_records": oversized,
        "source_head": source_head,
        "config_sha256": config_sha256,
        "creator_version": creator_version,
        "hard_max_bytes": hard_max_bytes,
    }


def rebuild_manifest(pack: Path, source_head: str, config_sha256: str,
                     creator_version: str,
                     hard_max_bytes: int = DEFAULT_HARD_MAX_BYTES) -> Dict[str, Any]:
    """Stream and independently derive all index fields from canonical pack bytes."""
    pack = Path(pack)
    digest = hashlib.sha256()
    entries: List[Dict[str, Any]] = []
    offset = 0
    previous_id: Optional[bytes] = None
    with pack.open("rb") as handle:
        while True:
            raw = handle.readline()
            if not raw:
                break
            digest.update(raw)
            envelope = decode_envelope(raw)
            task_id = envelope["task"]["id"]
            encoded_id = task_id.encode("utf-8")
            if previous_id is not None and encoded_id <= previous_id:
                raise ArchiveFormatError("archive pack task IDs are not strictly ordered")
            previous_id = encoded_id
            entries.append({
                "task_id": task_id,
                "offset": offset,
                "length": len(raw),
                "record_sha256": envelope["record_sha256"],
                "terminal_transition_at": envelope["terminal_transition_at"],
            })
            offset += len(raw)
    if not entries:
        raise ArchiveFormatError("archive pack is empty")
    pack_hash = digest.hexdigest()
    expected_name = "pack-"
    if not pack.name.startswith(expected_name) or not pack.name.endswith("-%s.ndjson" % pack_hash):
        raise ArchiveFormatError("archive pack filename/content hash mismatch")
    return _manifest_payload(pack.name, pack_hash, offset, entries, source_head,
                             config_sha256, creator_version, hard_max_bytes)


def manifest_bytes(manifest: Mapping[str, Any]) -> bytes:
    return _canonical(manifest) + b"\n"


def verify_manifest(pack: Path, manifest: Mapping[str, Any]) -> Dict[str, Any]:
    required = ("source_head", "config_sha256", "creator_version")
    if any(key not in manifest for key in required):
        raise ArchiveFormatError("archive manifest lacks provenance")
    rebuilt = rebuild_manifest(pack, str(manifest["source_head"]),
                               str(manifest["config_sha256"]),
                               str(manifest["creator_version"]),
                               int(manifest.get("hard_max_bytes", DEFAULT_HARD_MAX_BYTES)))
    if dict(manifest) != rebuilt:
        raise ArchiveFormatError("archive manifest does not match canonical pack")
    return rebuilt


def verify_archive_id_inventory(pack: Path, manifest_path: Path,
                                checksum_path: Path) -> Dict[str, Any]:
    """Verify the compact canonical ID inventory without decoding pack records.

    The immutable manifest is the archive's canonical offset/ID inventory and its
    sidecar binds the complete manifest bytes to the content-addressed pack name.
    This structural proof is sufficient to establish that an ID is absent while
    exact hits and doctor still verify the selected/all canonical pack bytes.
    """
    pack, manifest_path, checksum_path = map(Path, (pack, manifest_path, checksum_path))
    try:
        manifest_raw = manifest_path.read_bytes()
        manifest = json.loads(manifest_raw.decode("utf-8"))
        checksum_raw = checksum_path.read_bytes()
        pack_size = pack.stat().st_size
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArchiveFormatError("archive sidecar cannot be read: %s" % exc) from exc
    expected_checksum = ("%s  %s\n%s  %s\n" % (
        manifest.get("pack_sha256"), pack.name, _sha(manifest_raw),
        manifest_path.name)).encode("ascii")
    if checksum_raw != expected_checksum:
        raise ArchiveFormatError("archive checksum sidecar mismatch")
    required = ("archive_schema", "pack", "pack_sha256", "pack_size_bytes",
                "record_count", "records", "source_head", "config_sha256",
                "creator_version")
    if any(key not in manifest for key in required):
        raise ArchiveFormatError("archive manifest lacks provenance or inventory")
    pack_hash = manifest["pack_sha256"]
    if (manifest["archive_schema"] != ARCHIVE_SCHEMA or manifest["pack"] != pack.name
            or not isinstance(pack_hash, str) or len(pack_hash) != 64
            or not pack.name.endswith("-%s.ndjson" % pack_hash)
            or manifest["pack_size_bytes"] != pack_size):
        raise ArchiveFormatError("archive manifest identity does not match pack")
    records = manifest["records"]
    if not isinstance(records, list) or not records or manifest["record_count"] != len(records):
        raise ArchiveFormatError("archive manifest has invalid record inventory")
    expected_offset = 0
    previous_id: Optional[bytes] = None
    for item in records:
        if not isinstance(item, Mapping):
            raise ArchiveFormatError("archive manifest has invalid record inventory")
        task_id = item.get("task_id")
        encoded_id = task_id.encode("utf-8") if isinstance(task_id, str) else b""
        length = item.get("length")
        record_hash = item.get("record_sha256")
        if (not encoded_id or previous_id is not None and encoded_id <= previous_id
                or item.get("offset") != expected_offset
                or not isinstance(length, int) or length <= 0
                or not isinstance(record_hash, str) or len(record_hash) != 64
                or not item.get("terminal_transition_at")):
            raise ArchiveFormatError("archive manifest has invalid record inventory")
        previous_id = encoded_id
        expected_offset += length
    if expected_offset != pack_size:
        raise ArchiveFormatError("archive manifest inventory size does not match pack")
    return manifest


def verify_archive_artifact(pack: Path, manifest_path: Path,
                            checksum_path: Path) -> Dict[str, Any]:
    """Verify the hash-bound sidecars and all pack-derived manifest fields."""
    manifest = verify_archive_id_inventory(pack, manifest_path, checksum_path)
    return verify_manifest(Path(pack), manifest)


def verify_archive_pack_hash(pack: Path, manifest: Mapping[str, Any]) -> None:
    """Bind complete immutable pack bytes without decoding every envelope.

    Exact lookup separately verifies the selected envelope. Full semantic scans
    remain the contract for doctor and cache rebuild, while this content hash
    catches any uncoordinated pack corruption on the latency-sensitive read path.
    """
    digest = hashlib.sha256()
    try:
        with Path(pack).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ArchiveFormatError("archive pack cannot be read: %s" % exc) from exc
    if not hmac.compare_digest(digest.hexdigest(), str(manifest.get("pack_sha256", ""))):
        raise ArchiveFormatError("archive pack filename/content hash mismatch")


def read_record(pack: Path, entry: Mapping[str, Any]) -> Dict[str, Any]:
    """Read one exact record using a verified manifest offset; never scan the pack."""
    offset, length = int(entry["offset"]), int(entry["length"])
    if offset < 0 or length <= 0:
        raise ArchiveFormatError("invalid archive record offset or length")
    with Path(pack).open("rb") as handle:
        handle.seek(offset)
        raw = handle.read(length)
    if len(raw) != length:
        raise ArchiveFormatError("truncated archive record")
    envelope = decode_envelope(raw)
    if envelope["task"]["id"] != entry.get("task_id") or envelope["record_sha256"] != entry.get("record_sha256"):
        raise ArchiveFormatError("archive record does not match manifest entry")
    return envelope


def _terminal_transition(task: Mapping[str, Any],
                         ledger: Sequence[Mapping[str, Any]]) -> datetime:
    """Return the latest proved transition into the current terminal status."""
    task_id = str(task.get("id", ""))
    current = task.get("status")
    if current not in ("done", "archive"):
        raise ArchiveFormatError("task %s is not terminal" % task_id)
    status = None
    latest = None
    for index, event in enumerate(ledger):
        before = status
        if index == 0:
            snapshot = event.get("snapshot")
            if event.get("operation") != "create" or not isinstance(snapshot, Mapping):
                raise ArchiveFormatError("ledger for %s lacks creation status evidence" % task_id)
            status = snapshot.get("status")
        for change in (() if index == 0 else (event.get("changes") or [])):
            if not isinstance(change, Mapping) or change.get("path") != "/status":
                continue
            if change.get("op") == "remove" or "value" not in change:
                raise ArchiveFormatError("ledger for %s has ambiguous status evidence" % task_id)
            status = change["value"]
        if status == current and before != current:
            latest = _parse_utc(event.get("timestamp"), "ledger timestamp")
    if status != current or latest is None:
        raise ArchiveFormatError("ledger for %s does not prove current terminal status" % task_id)
    return latest


def _plan_hash(plan: Mapping[str, Any]) -> str:
    return _sha(_canonical({key: value for key, value in plan.items()
                            if key != "plan_sha256"}))


def _reservation_snapshot(juno_root: Path) -> Tuple[set, List[Dict[str, str]], str]:
    """Read explicit active reservation receipts without inventing ownership."""
    paths = set((Path(juno_root) / "reservations").glob("**/*.json"))
    paths.update((Path(juno_root) / "workflows").glob("**/reservation.json"))
    paths.update((Path(juno_root) / "workflows").glob("**/reservations.json"))
    reserved = set()
    receipts = []
    for path in sorted(paths):
        raw = path.read_bytes()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ArchiveFormatError("invalid reservation receipt: %s" % path) from exc
        documents = payload if isinstance(payload, list) else [payload]
        for document in documents:
            if not isinstance(document, Mapping):
                raise ArchiveFormatError("invalid reservation receipt: %s" % path)
            state = str(document.get("status", "active")).lower()
            if document.get("active") is False or state in ("released", "complete", "completed", "inactive"):
                continue
            ids = (document.get("task_ids") or document.get("reserved_task_ids")
                   or document.get("tasks") or ([document["task_id"]] if document.get("task_id") else []))
            if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
                raise ArchiveFormatError("invalid reservation task IDs: %s" % path)
            reserved.update(ids)
        receipts.append({"path": str(path.relative_to(juno_root)),
                         "sha256": _sha(raw)})
    return reserved, receipts, _sha(_canonical(receipts))


def _archive_artifact_sets(juno_root: Path) -> List[Tuple[Path, Path, Path]]:
    """Enumerate canonical packs plus sidecars so incomplete sets stay visible."""
    archive_root = Path(juno_root) / "archive"
    artifacts: Dict[Tuple[str, str], Dict[str, Path]] = {}
    for kind, suffix in (("pack", ".ndjson"),
                         ("manifest", ".manifest.json"),
                         ("checksum", ".sha256")):
        for path in archive_root.glob("*/*/pack-*" + suffix):
            stem = path.name[:-len(suffix)]
            artifacts.setdefault((str(path.parent), stem), {})[kind] = path
    result = []
    for (parent_text, stem), present in sorted(artifacts.items()):
        parent = Path(parent_text)
        expected = {
            "pack": parent / (stem + ".ndjson"),
            "manifest": parent / (stem + ".manifest.json"),
            "checksum": parent / (stem + ".sha256"),
        }
        missing = [kind for kind in ("pack", "manifest", "checksum")
                   if kind not in present]
        if missing:
            raise ArchiveFormatError(
                "incomplete archive artifact triplet for %s: missing %s; "
                "restore the sealed artifact from Git or rebuild derived sidecars "
                "from verified canonical pack provenance" %
                (parent / stem, ", ".join(missing)))
        result.append((expected["pack"], expected["manifest"], expected["checksum"]))
    return result


def scan_archive_id_inventory(juno_root: Path) -> Dict[str, str]:
    """Return the complete case-folded cold ID inventory from sealed manifests.

    This deliberately does not decode/hash every pack record. It is the bounded
    absence-proof path used after a disposable SQLite miss; exact hits continue
    through full selected-artifact verification.
    """
    seen: Dict[str, str] = {}
    for pack, manifest_path, checksum in _archive_artifact_sets(juno_root):
        manifest = verify_archive_id_inventory(pack, manifest_path, checksum)
        for item in manifest["records"]:
            task_id = item["task_id"]
            folded = task_id.casefold()
            if folded in seen:
                raise ArchiveFormatError(
                    "duplicate case-insensitive task ID in cold archive: %s conflicts with %s" %
                    (task_id, seen[folded]))
            seen[folded] = task_id
    return seen


def scan_archive_index(juno_root: Path) -> Tuple[List[Dict[str, Any]], str]:
    """Verify canonical packs and derive the complete disposable cold index."""
    entries: List[Dict[str, Any]] = []
    seen: Dict[str, str] = {}
    for pack, manifest_path, checksum in _archive_artifact_sets(juno_root):
        manifest = verify_archive_artifact(pack, manifest_path, checksum)
        for item in manifest["records"]:
            task_id = item["task_id"]
            folded = task_id.casefold()
            if folded in seen:
                raise ArchiveFormatError(
                    "duplicate case-insensitive task ID in cold archive: %s conflicts with %s" %
                    (task_id, seen[folded]))
            seen[folded] = task_id
            envelope = read_record(pack, item)
            task = envelope["task"]
            status = task.get("status")
            if status not in ("done", "archive"):
                raise ArchiveFormatError("cold archive task is not terminal: %s" % task_id)
            entries.append({
                "task_id": task_id, "id_fold": folded, "status": status,
                "terminal_transition_at": envelope["terminal_transition_at"],
                "last_modified": str(task.get("last_modified", "")),
                "feature_tags": list(task.get("feature_tags") or []),
                "pack": str(pack), "manifest": str(manifest_path),
                "checksum": str(checksum), "offset": int(item["offset"]),
                "length": int(item["length"]),
                "record_sha256": item["record_sha256"],
                "pack_sha256": manifest["pack_sha256"],
            })
    entries.sort(key=lambda item: item["task_id"].encode("utf-8"))
    inventory = [{key: item[key] for key in (
        "task_id", "record_sha256", "pack_sha256", "offset", "length")}
        for item in entries]
    return entries, _sha(_canonical(inventory))


def iter_archive_envelopes(juno_root: Path) -> Iterable[Dict[str, Any]]:
    """Yield every verified cold envelope while verifying each pack only once."""
    for pack, manifest_path, checksum in _archive_artifact_sets(juno_root):
        manifest = verify_archive_artifact(pack, manifest_path, checksum)
        for item in manifest["records"]:
            yield read_record(pack, item)


def read_indexed_archive_record(entry: Mapping[str, Any]) -> Dict[str, Any]:
    """Hash the complete pack, then semantically verify only the selected row.

    The sealed manifest/checksum proves the canonical inventory, the complete
    pack digest catches byte corruption, and ``read_record`` validates all task,
    ledger, and envelope hashes for the returned identity. Doctor/cache rebuild
    intentionally retain the more expensive every-record semantic verification.
    """
    pack = Path(str(entry["pack"]))
    manifest_path = Path(str(entry["manifest"]))
    checksum = Path(str(entry["checksum"]))
    manifest = verify_archive_id_inventory(pack, manifest_path, checksum)
    verify_archive_pack_hash(pack, manifest)
    matches = [item for item in manifest["records"]
               if item["task_id"].casefold() == str(entry["task_id"]).casefold()]
    if len(matches) != 1:
        raise ArchiveFormatError("derived archive index does not match canonical pack")
    canonical = matches[0]
    for key in ("offset", "length", "record_sha256"):
        if canonical[key] != entry[key]:
            raise ArchiveFormatError("derived archive index does not match canonical pack")
    return read_record(pack, canonical)


def _cold_id_snapshot(juno_root: Path) -> Tuple[set, str]:
    entries, inventory = scan_archive_index(juno_root)
    return {item["id_fold"] for item in entries}, inventory


def _ordered_batches(candidates: Sequence[ArchiveCandidate], target_bytes: int,
                     hard_max_bytes: int, max_tasks: int) -> List[List[ArchiveCandidate]]:
    if (target_bytes <= 0 or hard_max_bytes < target_bytes or
            max_tasks <= 0 or max_tasks > DEFAULT_MAX_RECORDS):
        raise ValueError("invalid archive plan limits")
    batches = []
    current = []
    size = 0
    for candidate in candidates:
        if candidate.estimated_bytes > hard_max_bytes:
            if current:
                batches.append(current)
                current, size = [], 0
            batches.append([candidate])
            continue
        if current and (len(current) >= max_tasks or size + candidate.estimated_bytes > target_bytes
                        or size + candidate.estimated_bytes > hard_max_bytes):
            batches.append(current)
            current, size = [], 0
        current.append(candidate)
        size += candidate.estimated_bytes
    if current:
        batches.append(current)
    return batches


def plan_archive(storage: Any, *, statuses: Sequence[str] = ("done", "archive"),
                 older_than: timedelta = timedelta(days=90),
                 max_tasks: int = DEFAULT_MAX_RECORDS,
                 target_bytes: int = DEFAULT_TARGET_BYTES,
                 hard_max_bytes: int = DEFAULT_HARD_MAX_BYTES,
                 now: Optional[datetime] = None) -> Dict[str, Any]:
    """Build a sealed, read-only plan bound to Git/config/task/ledger truth."""
    if set(statuses) - {"done", "archive"} or not statuses:
        raise ValueError("archive plan status must be done and/or archive")
    if older_than <= timedelta(0):
        raise ValueError("older-than must be positive")
    if max_tasks <= 0 or max_tasks > DEFAULT_MAX_RECORDS:
        raise ValueError("max-tasks must be between 1 and 1000")
    if target_bytes <= 0 or hard_max_bytes < target_bytes:
        raise ValueError("invalid archive plan byte limits")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    created_at = now.isoformat().replace("+00:00", "Z")
    source_head = storage._git_head()
    if not source_head:
        raise ArchiveFormatError("archive planning requires a Git HEAD")
    config_sha256 = storage._config_hash()
    records = []
    rejected = []
    folded = {}
    for path in sorted(storage.tasks_root.glob("*/*.md")):
        try:
            record = storage._read_path(path)
            records.append(record)
            folded.setdefault(str(record["id"]).casefold(), []).append(str(record["id"]))
        except Exception as exc:
            rejected.append({"task_id": path.stem, "reason": "corrupt_task", "detail": str(exc)})
    reserved, reservation_receipts, reservations_sha256 = _reservation_snapshot(storage.juno_root)
    cold_ids, archive_inventory_sha256 = _cold_id_snapshot(storage.juno_root)
    status_by_id = {str(item["id"]): item.get("status") for item in records}
    candidates = []
    for task in records:
        task_id = str(task["id"])
        reason = None
        detail = None
        if task.get("status") not in statuses:
            reason = "status"
        elif len(folded[task_id.casefold()]) != 1 or task_id.casefold() in cold_ids:
            reason = "case_insensitive_duplicate"
        elif task_id in reserved:
            reason = "reserved"
        else:
            blockers = task.get("blocked_by") or []
            unsafe = [item for item in blockers if status_by_id.get(item) not in ("done", "archive")]
            if unsafe:
                reason, detail = "active_dependency", ",".join(sorted(map(str, unsafe)))
        if reason:
            rejected.append({"task_id": task_id, "reason": reason, **({"detail": detail} if detail else {})})
            continue
        try:
            ledger = storage.ledger.read(task_id)
            task_sha = storage.normalized_hash(task)
            if not ledger or ledger[-1].get("after_sha256") != task_sha:
                raise ArchiveFormatError("ledger/current-state mismatch")
            # Reuse the archive verifier for the complete chain/state contract.
            _verify_ledger(task_id, task_sha, ledger)
            terminal = _terminal_transition(task, ledger)
            if terminal > now - older_than:
                rejected.append({"task_id": task_id, "reason": "too_young"})
                continue
            ledger_sha = _sha(_canonical(plain_value(ledger)))
            envelope = make_envelope(task, ledger, created_at,
                                     terminal.isoformat().replace("+00:00", "Z"),
                                     "sha256:" + task_sha)
            candidates.append(ArchiveCandidate(
                task_id, terminal.isoformat().replace("+00:00", "Z"), task_sha,
                ledger_sha, str(ledger[-1]["event_sha256"]), len(encode_envelope(envelope))))
        except (ArchiveFormatError, LedgerError, OSError, ValueError) as exc:
            rejected.append({"task_id": task_id, "reason": "missing_or_corrupt_ledger",
                             "detail": str(exc)})
    candidates.sort(key=lambda item: (_parse_utc(item.terminal_transition_at,
                                                 "terminal_transition_at"),
                                      item.task_id.encode("utf-8")))
    selected = candidates[:max_tasks]
    batches = _ordered_batches(selected, target_bytes, hard_max_bytes, max_tasks)
    policy = {"statuses": sorted(set(statuses)), "older_than_seconds": int(older_than.total_seconds()),
              "max_tasks": max_tasks, "target_bytes": target_bytes,
              "hard_max_bytes": hard_max_bytes}
    plan = {
        "archive_plan_schema": 1,
        "created_at": created_at,
        "source_head": source_head,
        "config_sha256": config_sha256,
        "policy": policy,
        "policy_sha256": _sha(_canonical(policy)),
        "reservations": reservation_receipts,
        "reservations_sha256": reservations_sha256,
        "archive_inventory_sha256": archive_inventory_sha256,
        "selected_ids": [item.task_id for item in selected],
        "selected": [dict(item.__dict__) for item in selected],
        "batches": [{"task_ids": [item.task_id for item in batch],
                     "estimated_bytes": sum(item.estimated_bytes for item in batch),
                     "oversized_record": len(batch) == 1 and batch[0].estimated_bytes > hard_max_bytes}
                    for batch in batches],
        "rejected": sorted(rejected, key=lambda item: (item["task_id"].casefold(), item["task_id"])),
    }
    plan["plan_sha256"] = _plan_hash(plan)
    return plan


def verify_archive_plan(storage: Any, plan: Mapping[str, Any]) -> Dict[str, Any]:
    """Fail closed when a sealed plan or any bound source fact is stale."""
    if plan.get("archive_plan_schema") != 1 or plan.get("plan_sha256") != _plan_hash(plan):
        raise ArchiveFormatError("archive plan hash mismatch")
    if plan.get("source_head") != storage._git_head():
        raise ArchiveFormatError("archive plan source HEAD is stale")
    if plan.get("config_sha256") != storage._config_hash():
        raise ArchiveFormatError("archive plan config is stale")
    _, receipts, reservation_hash = _reservation_snapshot(storage.juno_root)
    if plan.get("reservations") != receipts or plan.get("reservations_sha256") != reservation_hash:
        raise ArchiveFormatError("archive plan reservations are stale")
    _, archive_inventory_hash = _cold_id_snapshot(storage.juno_root)
    if plan.get("archive_inventory_sha256") != archive_inventory_hash:
        raise ArchiveFormatError("archive plan cold archive inventory is stale")
    selected = plan.get("selected")
    if not isinstance(selected, list) or plan.get("selected_ids") != [item.get("task_id") for item in selected]:
        raise ArchiveFormatError("archive plan selection is malformed")
    for item in selected:
        task_id = item.get("task_id")
        task = storage.find_task(task_id)
        if task is None or storage.normalized_hash(task) != item.get("task_sha256"):
            raise ArchiveFormatError("archive plan task revision is stale: %s" % task_id)
        try:
            ledger = storage.ledger.read(task_id)
        except (LedgerError, OSError) as exc:
            raise ArchiveFormatError("archive plan ledger is stale: %s" % task_id) from exc
        if (_sha(_canonical(plain_value(ledger))) != item.get("ledger_sha256")
                or not ledger or ledger[-1].get("event_sha256") != item.get("ledger_tip_sha256")):
            raise ArchiveFormatError("archive plan ledger is stale: %s" % task_id)
    return dict(plan)


def write_archive_packs(archive_root: Path, envelopes: Iterable[Mapping[str, Any]],
                        source_head: str, config_sha256: str, creator_version: str,
                        created_at: str, target_bytes: int = DEFAULT_TARGET_BYTES,
                        hard_max_bytes: int = DEFAULT_HARD_MAX_BYTES,
                        max_records: int = DEFAULT_MAX_RECORDS) -> List[PackArtifact]:
    """Write sealed content-addressed packs and derived sidecars.

    Existing output is refused rather than replaced or appended.
    """
    created = _parse_utc(created_at, "created_at")
    batches = split_records(envelopes, target_bytes, hard_max_bytes, max_records)
    if not batches:
        return []
    destination = Path(archive_root) / created.strftime("%Y") / created.strftime("%m")
    destination.mkdir(parents=True, exist_ok=True)
    stamp = created.strftime("%Y%m%dT%H%M%SZ")
    artifacts: List[PackArtifact] = []
    for batch in batches:
        pack_digest = hashlib.sha256()
        pack_size = 0
        for _, raw in batch:
            pack_digest.update(raw)
            pack_size += len(raw)
        pack_hash = pack_digest.hexdigest()
        stem = "pack-%s-%s" % (stamp, pack_hash)
        pack = destination / (stem + ".ndjson")
        manifest_path = destination / (stem + ".manifest.json")
        checksum_path = destination / (stem + ".sha256")
        if any(path.exists() for path in (pack, manifest_path, checksum_path)):
            raise FileExistsError("immutable archive output already exists: %s" % stem)
        # Exclusive writes make accidental replacement impossible even with a race.
        try:
            fd = os.open(pack, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
            try:
                for _, raw in batch:
                    view = memoryview(raw)
                    while view:
                        written = os.write(fd, view)
                        if written <= 0:
                            raise OSError("archive pack write made no forward progress")
                        view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
            manifest = rebuild_manifest(pack, source_head, config_sha256,
                                        creator_version, hard_max_bytes)
            encoded_manifest = manifest_bytes(manifest)
            manifest_hash = _sha(encoded_manifest)
            manifest_path.write_bytes(encoded_manifest)
            checksum = ("%s  %s\n%s  %s\n" %
                        (pack_hash, pack.name, manifest_hash, manifest_path.name)).encode("ascii")
            checksum_path.write_bytes(checksum)
            # Verify from independently read bytes before sealing derived files.
            independently_verified = verify_archive_artifact(
                pack, manifest_path, checksum_path)
            if independently_verified["pack_sha256"] != pack_hash or _sha(manifest_path.read_bytes()) != manifest_hash:
                raise ArchiveFormatError("archive sidecar verification failed")
            manifest_path.chmod(0o444)
            checksum_path.chmod(0o444)
        except Exception:
            for path in (checksum_path, manifest_path, pack):
                try:
                    path.chmod(0o644)
                    path.unlink()
                except FileNotFoundError:
                    pass
            raise
        artifacts.append(PackArtifact(pack, manifest_path, checksum_path, pack_hash,
                                      manifest_hash, len(batch), pack_size,
                                      len(batch) == 1 and len(batch[0][1]) > hard_max_bytes))
    return artifacts


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], check=check,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=".%s." % path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _receipt_hash(value: Mapping[str, Any]) -> str:
    return _sha(_canonical({key: item for key, item in value.items()
                            if key not in ("receipt_sha256", "recovery_sha256")}))


def _archive_paths(storage: Any, plan: Mapping[str, Any]) -> List[Path]:
    paths: List[Path] = []
    for task_id in plan["selected_ids"]:
        task_path = storage.task_path(task_id)
        if not task_path.exists():
            raise ArchiveFormatError("selected hot task disappeared: %s" % task_id)
        paths.append(task_path)
        segments = storage.ledger.segments(task_id)
        if not segments:
            raise ArchiveFormatError("selected ledger disappeared: %s" % task_id)
        paths.extend(segments)
    return paths


def _relative(root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError as exc:
        raise ArchiveFormatError("archive path escapes repository: %s" % path) from exc


def _assert_selected_worktrees_clean(root: Path, selected_paths: Sequence[str]) -> None:
    """Reject selected-path edits in any linked worktree; unrelated edits are allowed."""
    listing = _git(root, "worktree", "list", "--porcelain").stdout.splitlines()
    worktrees = [Path(line[9:]) for line in listing if line.startswith("worktree ")]
    current = root.resolve()
    for worktree in worktrees:
        if worktree.resolve() == current or not worktree.exists():
            continue
        result = _git(worktree, "status", "--porcelain=v1", "--", *selected_paths,
                      check=False)
        if result.returncode or result.stdout.strip():
            raise ArchiveFormatError("selected archive paths changed in linked worktree: %s" % worktree)


@contextmanager
def _archive_owner(root: Path):
    common = Path(_git(root, "rev-parse", "--path-format=absolute",
                       "--git-common-dir").stdout.strip())
    lock = common.resolve() / "juno-archive-pack.lock"
    with lock.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ArchiveFormatError("another archive-pack owner is active") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def archive_doctor(juno_root: Path) -> List[Dict[str, str]]:
    """Verify every immutable pack and reject incomplete sets or duplicate IDs."""
    failures: List[Dict[str, str]] = []
    seen = set()
    try:
        artifacts = _archive_artifact_sets(juno_root)
    except Exception as exc:
        return [{"path": str(Path(juno_root) / "archive"), "error": str(exc)}]
    for pack, manifest_path, checksum in artifacts:
        try:
            manifest = verify_archive_artifact(pack, manifest_path, checksum)
            for entry in manifest["records"]:
                folded = entry["task_id"].casefold()
                if folded in seen:
                    raise ArchiveFormatError("duplicate cold archive task ID: %s" % entry["task_id"])
                seen.add(folded)
        except Exception as exc:
            failures.append({"path": str(pack), "error": str(exc)})
    return failures


def create_archive(storage: Any, plan: Mapping[str, Any], report_path: Path,
                   creator_version: str, *,
                   fault: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Execute one path-owned hot-to-cold Git transaction.

    All faults before the commit restore exact hot bytes and index state.  Any
    fault after the commit intentionally retains ``ARCHIVE_PACK_FREEZE.json``
    so recovery can distinguish a committed transition from an uncommitted one.
    """
    root = Path(storage.project_root).resolve()
    report_path = Path(report_path).resolve()
    try:
        report_path.relative_to(root)
    except ValueError:
        pass
    else:
        raise ArchiveFormatError("archive create report must be outside the repository")
    freeze_path = storage.juno_root / "ARCHIVE_PACK_FREEZE.json"
    emit = fault or (lambda boundary: None)
    with _archive_owner(root):
        if freeze_path.exists():
            raise ArchiveFormatError("archive recovery freeze already exists")
        status = _git(root, "status", "--porcelain=v1", "--untracked-files=all").stdout
        if status.strip():
            raise ArchiveFormatError("archive create requires a clean worktree and index")
        verified = verify_archive_plan(storage, plan)
        if not verified["selected_ids"]:
            raise ArchiveFormatError("archive plan selects no tasks")
        hot_paths = _archive_paths(storage, verified)
        hot_rel = [_relative(root, path) for path in hot_paths]
        _assert_selected_worktrees_clean(root, hot_rel)
        parent = _git(root, "rev-parse", "HEAD").stdout.strip()
        git_common = Path(_git(root, "rev-parse", "--path-format=absolute",
                               "--git-common-dir").stdout.strip())
        stage = Path(tempfile.mkdtemp(prefix="juno-archive-stage-", dir=git_common))
        backups = {path: (path.read_bytes(), path.stat().st_mode) for path in hot_paths}
        backup_modes: Dict[str, int] = {}
        for path, (raw, mode) in backups.items():
            relative = _relative(root, path)
            backup = stage / "hot" / relative
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_bytes(raw)
            backup_modes[relative] = mode
        freeze: Dict[str, Any] = {
            "archive_recovery_schema": 1, "operation": "archive-pack-create",
            "phase": "frozen", "plan_sha256": verified["plan_sha256"],
            "source_head": parent, "selected_ids": verified["selected_ids"],
            "report": str(report_path), "stage": str(stage),
            "hot_paths": hot_rel, "hot_modes": backup_modes, "archive_paths": [],
        }
        freeze["recovery_sha256"] = _receipt_hash(freeze)
        _write_json_atomic(freeze_path, freeze)
        committed = False
        activated: List[Path] = []
        artifacts: List[PackArtifact] = []
        archive_rel: List[str] = []
        try:
            emit("after_freeze")
            emit("before_staging")
            archived_at = verified["created_at"]
            selected_by_id = {item["task_id"]: item for item in verified["selected"]}
            for batch in verified["batches"]:
                envelopes = []
                for task_id in batch["task_ids"]:
                    task = storage.find_task(task_id)
                    ledger = storage.ledger.read(task_id)
                    item = selected_by_id[task_id]
                    envelopes.append(make_envelope(
                        task, ledger, archived_at, item["terminal_transition_at"],
                        "sha256:" + item["task_sha256"]))
                artifacts.extend(write_archive_packs(
                    stage / "archive", envelopes, parent, verified["config_sha256"],
                    creator_version, archived_at,
                    target_bytes=verified["policy"]["target_bytes"],
                    hard_max_bytes=verified["policy"]["hard_max_bytes"],
                    max_records=verified["policy"]["max_tasks"]))
            emit("after_staging")
            emit("before_verification")
            for artifact in artifacts:
                verify_archive_artifact(artifact.pack, artifact.manifest, artifact.checksum)
            emit("after_verification")
            verify_archive_plan(storage, verified)
            _assert_selected_worktrees_clean(root, hot_rel)
            emit("before_deletion")
            copies: List[Tuple[Path, Path]] = []
            for artifact in artifacts:
                relative = artifact.pack.relative_to(stage / "archive")
                destination_dir = storage.juno_root / "archive" / relative.parent
                for source in (artifact.pack, artifact.manifest, artifact.checksum):
                    destination = destination_dir / source.name
                    if destination.exists():
                        raise ArchiveFormatError("immutable archive destination exists: %s" % destination)
                    copies.append((source, destination))
                    archive_rel.append(_relative(root, destination))
            freeze.update({"phase": "activating", "archive_paths": archive_rel})
            freeze["recovery_sha256"] = _receipt_hash(freeze)
            _write_json_atomic(freeze_path, freeze)
            for source, destination in copies:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                destination.chmod(source.stat().st_mode)
                activated.append(destination)
            for path in hot_paths:
                path.unlink()
            emit("after_deletion")
            _git(root, "add", "--", *archive_rel)
            _git(root, "rm", "--", *hot_rel)
            staged = set(filter(None, _git(root, "diff", "--cached", "--name-only").stdout.splitlines()))
            expected = set(archive_rel + hot_rel)
            if staged != expected:
                raise ArchiveFormatError("archive staged paths are not exactly transaction-owned")
            emit("before_commit")
            message = "Archive %d terminal Kanban tasks [%s]" % (
                len(verified["selected_ids"]), verified["plan_sha256"][:12])
            _git(root, "commit", "-m", message, "--", *sorted(expected))
            committed = True
            commit = _git(root, "rev-parse", "HEAD").stdout.strip()
            freeze.update({"phase": "committed", "commit": commit,
                           "archive_paths": archive_rel,
                           "pack_sha256": [item.pack_sha256 for item in artifacts],
                           "oversized_records": [item.pack.name for item in artifacts
                                                 if item.oversized_record]})
            freeze["recovery_sha256"] = _receipt_hash(freeze)
            _write_json_atomic(freeze_path, freeze)
            emit("after_commit")
            emit("before_cache_rebuild")
            storage.rebuild_cache()
            emit("after_cache_rebuild")
            emit("before_doctors")
            archive_failures = archive_doctor(storage.juno_root)
            hot_failures = storage.doctor()
            if archive_failures or hot_failures:
                raise ArchiveFormatError("post-commit doctors failed: %s" %
                                         (archive_failures + hot_failures))
            emit("after_doctors")
            receipt: Dict[str, Any] = {
                "archive_create_schema": 1, "plan_sha256": verified["plan_sha256"],
                "source_parent": parent, "archive_commit": commit,
                "selected_ids": verified["selected_ids"],
                "pack_sha256": [item.pack_sha256 for item in artifacts],
                "oversized_records": [item.pack.name for item in artifacts if item.oversized_record],
                "revert": "git revert %s" % commit,
                "archive_doctor_failures": [], "global_doctor_failures": [],
            }
            receipt["receipt_sha256"] = _receipt_hash(receipt)
            _write_json_atomic(report_path, receipt)
            freeze.update({"phase": "verified", "receipt_sha256": receipt["receipt_sha256"]})
            freeze["recovery_sha256"] = _receipt_hash(freeze)
            _write_json_atomic(freeze_path, freeze)
            emit("before_freeze_cleanup")
            freeze_path.unlink()
            emit("after_freeze_cleanup")
            return receipt
        except Exception:
            if not committed:
                _git(root, "reset", "--quiet", "HEAD", "--",
                     *(hot_rel + archive_rel), check=False)
                for path, (raw, mode) in backups.items():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(raw)
                    path.chmod(mode)
                for path in reversed(activated):
                    try:
                        path.chmod(0o644)
                        path.unlink()
                    except FileNotFoundError:
                        pass
                freeze_path.unlink(missing_ok=True)
            # Post-commit failures deliberately leave the freeze as recovery truth.
            raise
        finally:
            shutil.rmtree(stage, ignore_errors=True)


def recover_archive(storage: Any, report_path: Optional[Path] = None) -> Dict[str, Any]:
    """Finish a machine-recorded post-commit archive transaction.

    Recovery never guesses across the commit boundary: the recorded commit must
    be the exact current HEAD and its parent must be the sealed plan source.
    """
    root = Path(storage.project_root).resolve()
    freeze_path = storage.juno_root / "ARCHIVE_PACK_FREEZE.json"
    with _archive_owner(root):
        try:
            freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArchiveFormatError("archive recovery freeze cannot be read") from exc
        supplied = freeze.get("recovery_sha256")
        if supplied != _receipt_hash(freeze):
            raise ArchiveFormatError("archive recovery freeze hash mismatch")
        phase = freeze.get("phase")
        if phase in ("frozen", "activating"):
            if _git(root, "rev-parse", "HEAD").stdout.strip() != freeze.get("source_head"):
                raise ArchiveFormatError("pre-commit archive recovery HEAD moved")
            hot_rel = list(freeze.get("hot_paths") or [])
            archive_rel = list(freeze.get("archive_paths") or [])
            if any(not isinstance(item, str) or item.startswith("/") or ".." in Path(item).parts
                   for item in hot_rel + archive_rel):
                raise ArchiveFormatError("archive recovery contains unsafe paths")
            stage = Path(str(freeze.get("stage", ""))).resolve()
            git_common = Path(_git(root, "rev-parse", "--path-format=absolute",
                                   "--git-common-dir").stdout.strip()).resolve()
            if stage.parent != git_common or not stage.name.startswith("juno-archive-stage-"):
                raise ArchiveFormatError("archive recovery staging path is unsafe")
            _git(root, "reset", "--quiet", "HEAD", "--", *(hot_rel + archive_rel), check=False)
            for relative in hot_rel:
                backup = stage / "hot" / relative
                if not backup.is_file():
                    raise ArchiveFormatError("archive recovery hot backup is missing: %s" % relative)
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(backup, destination)
                destination.chmod(int((freeze.get("hot_modes") or {})[relative]))
            for relative in archive_rel:
                destination = root / relative
                try:
                    destination.chmod(0o644)
                    destination.unlink()
                except FileNotFoundError:
                    pass
            if _git(root, "status", "--porcelain=v1", "--", *hot_rel).stdout.strip():
                raise ArchiveFormatError("pre-commit archive recovery did not restore source tree")
            shutil.rmtree(stage)
            freeze_path.unlink()
            return {"archive_recovery_schema": 1, "result": "rolled_back",
                    "source_head": freeze["source_head"],
                    "plan_sha256": freeze["plan_sha256"]}
        if phase not in ("committed", "verified") or not freeze.get("commit"):
            raise ArchiveFormatError("archive recovery phase is ambiguous")
        commit = str(freeze["commit"])
        if _git(root, "rev-parse", "HEAD").stdout.strip() != commit:
            raise ArchiveFormatError("archive recovery HEAD does not match recorded commit")
        parent = _git(root, "rev-parse", commit + "^").stdout.strip()
        if parent != freeze.get("source_head"):
            raise ArchiveFormatError("archive recovery commit parent mismatch")
        archive_failures = archive_doctor(storage.juno_root)
        storage.rebuild_cache()
        hot_failures = storage.doctor()
        if archive_failures or hot_failures:
            raise ArchiveFormatError("archive recovery doctors failed: %s" %
                                     (archive_failures + hot_failures))
        output = Path(report_path or freeze["report"]).resolve()
        try:
            output.relative_to(root)
        except ValueError:
            pass
        else:
            raise ArchiveFormatError("archive recovery report must be outside repository")
        receipt: Dict[str, Any] = {
            "archive_create_schema": 1,
            "plan_sha256": freeze["plan_sha256"],
            "source_parent": parent,
            "archive_commit": commit,
            "selected_ids": freeze["selected_ids"],
            "pack_sha256": freeze.get("pack_sha256", []),
            "oversized_records": freeze.get("oversized_records", []),
            "revert": "git revert %s" % commit,
            "archive_doctor_failures": [], "global_doctor_failures": [],
        }
        receipt["receipt_sha256"] = _receipt_hash(receipt)
        if output.exists():
            existing = json.loads(output.read_text(encoding="utf-8"))
            if existing != receipt:
                raise ArchiveFormatError("archive recovery receipt conflicts with existing report")
        else:
            _write_json_atomic(output, receipt)
        freeze_path.unlink()
        return receipt
