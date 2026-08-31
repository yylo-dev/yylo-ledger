"""Preservation-first migration of legacy files into native Records.

The migration contract deliberately separates three truths:

* an immutable inventory of source bytes;
* an immutable, destination-bound plan with deterministic Record IDs; and
* a mutable status receipt updated atomically before and after each item.

No operation removes or rewrites a source file.  Callers must enumerate artifact
files explicitly; only wiki/workflow roots have bounded extension-based scans.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from . import __version__
from .artifacts import ARTIFACT_PROFILES, PAYLOAD_MODES, ArtifactStore
from .documents import DocumentStore
from .profiles import WORKFLOW_SCHEMA_V1
from .records import RECORD_ID_RE, RecordError, value_digest

INVENTORY_SCHEMA = "yylo_ledger_record_migration_inventory.v1"
PLAN_SCHEMA = "yylo_ledger_record_migration_plan.v1"
STATUS_SCHEMA = "yylo_ledger_record_migration_status.v1"
_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_DOCUMENT_EXTENSIONS = {"wiki": (".md",), "workflow": (".yaml", ".yml")}
_FORBIDDEN_ROOTS = (
    ".git", ".juno_task/runtime", ".juno_task/logs", ".juno_task/cache",
    ".juno_task/locks", ".juno_task/objects", ".juno_task/documents",
    ".juno_task/document-ledger", ".juno_task/artifacts", ".juno_task/artifact-ledger",
)
_SECRET_NAMES = re.compile(r"(^|/)(?:\.env(?:\..*)?|id_(?:rsa|ed25519)|credentials(?:\..*)?)$", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _seal(value: Mapping[str, Any], field: str) -> dict[str, Any]:
    result = dict(value)
    result[field] = _digest(result)
    return result


def _verify_seal(value: Mapping[str, Any], field: str, code: str) -> None:
    supplied = value.get(field)
    unsigned = dict(value)
    unsigned.pop(field, None)
    if not isinstance(supplied, str) or supplied != _digest(unsigned):
        raise RecordError(code, f"{field} does not match the canonical payload")


def _atomic_json(path: Path, value: Mapping[str, Any], *, fresh: bool = False) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if fresh and path.exists():
        raise RecordError("MIGRATION_RECEIPT_EXISTS", f"refusing to replace existing receipt: {path}")
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if fresh:
            try:
                os.link(temporary, path)
            except FileExistsError as exc:
                raise RecordError("MIGRATION_RECEIPT_EXISTS", f"refusing to replace existing receipt: {path}") from exc
        else:
            os.replace(temporary, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _write_status(path: Path, value: dict[str, Any], *, fresh: bool = False) -> None:
    value.pop("status_sha256", None)
    value["status_sha256"] = _digest(value)
    _atomic_json(path, value, fresh=fresh)


def _read_json(path: Path, schema: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecordError("MIGRATION_RECEIPT_INVALID", f"cannot read migration receipt {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != schema:
        raise RecordError("MIGRATION_RECEIPT_INVALID", f"receipt does not use {schema}")
    return value


def _outside_source(path: Path, source_root: Path) -> None:
    try:
        Path(path).resolve().relative_to(source_root.resolve())
    except ValueError:
        return
    raise RecordError("MIGRATION_RECEIPT_INSIDE_SOURCE", "migration receipts must be outside the source root")


def _git(root: Path, *argv: str) -> Optional[str]:
    result = subprocess.run(["git", "-C", str(root), *argv], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return result.stdout.strip() if result.returncode == 0 else None


def _source_identity(root: Path) -> dict[str, Any]:
    return {
        "root_sha256": hashlib.sha256(str(root.resolve()).encode()).hexdigest(),
        "git_head": _git(root, "rev-parse", "HEAD"),
        "git_toplevel": bool(_git(root, "rev-parse", "--show-toplevel")),
    }


def _safe_relative(root: Path, supplied: str) -> tuple[str, Path]:
    if not isinstance(supplied, str) or not supplied or "\x00" in supplied:
        raise RecordError("MIGRATION_PATH_UNSAFE", "source path must be a non-empty relative path")
    raw = Path(supplied)
    if raw.is_absolute() or ".." in raw.parts:
        raise RecordError("MIGRATION_PATH_UNSAFE", f"source path escapes the source root: {supplied}")
    relative = raw.as_posix()
    while relative.startswith("./"):
        relative = relative[2:]
    folded = relative.casefold()
    if any(folded == item or folded.startswith(item + "/") for item in _FORBIDDEN_ROOTS):
        raise RecordError("MIGRATION_PATH_EXCLUDED", f"source path is excluded by policy: {relative}")
    if _SECRET_NAMES.search(relative):
        raise RecordError("MIGRATION_SECRET_REJECTED", f"secret-like source path is excluded: {relative}")
    candidate = root / relative
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise RecordError("MIGRATION_PATH_UNSAFE", f"source path is missing or escapes the source root: {relative}") from exc
    cursor = candidate
    while cursor != root:
        if cursor.is_symlink():
            raise RecordError("MIGRATION_PATH_UNSAFE", f"source path contains a symlink: {relative}")
        cursor = cursor.parent
    if not resolved.is_file():
        raise RecordError("MIGRATION_PATH_UNSAFE", f"source path is not a regular file: {relative}")
    return relative, resolved


def _blob_identity(root: Path, relative: str) -> Optional[str]:
    output = _git(root, "ls-files", "-s", "--", relative)
    if not output:
        return None
    fields = output.splitlines()[0].split()
    return fields[1] if len(fields) >= 2 else None


def _file_item(root: Path, declaration: Mapping[str, Any]) -> dict[str, Any]:
    relative, source = _safe_relative(root, str(declaration.get("path") or ""))
    kind = declaration.get("kind")
    if kind not in ("wiki", "workflow", "artifact"):
        raise RecordError("MIGRATION_DECLARATION_INVALID", "kind must be wiki, workflow, or artifact")
    content = source.read_bytes()
    profile = kind
    mode = None
    if kind == "artifact":
        profile = declaration.get("profile")
        mode = declaration.get("mode")
        if profile not in ARTIFACT_PROFILES or mode not in PAYLOAD_MODES:
            raise RecordError("MIGRATION_DECLARATION_INVALID", "Artifact requires a supported profile and explicit mode")
        if mode not in ("inline", "local"):
            raise RecordError("MIGRATION_DECLARATION_INVALID", "file migration copies Artifact bytes only in inline or local mode")
    else:
        if source.suffix.casefold() not in _DOCUMENT_EXTENSIONS[kind]:
            raise RecordError("MIGRATION_DECLARATION_INVALID", f"{kind} source has an unsupported extension")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RecordError("INPUT_UTF8_INVALID", f"Document source is not UTF-8: {relative}") from exc
        if "\r" in text:
            raise RecordError("DOCUMENT_PAYLOAD_INVALID", f"Document source must use LF line endings: {relative}")
    stat = source.stat()
    media_type = declaration.get("media_type") or (
        "text/markdown" if kind == "wiki" else "application/yaml" if kind == "workflow"
        else mimetypes.guess_type(relative)[0] or "application/octet-stream")
    result = {
        "source_path": relative,
        "source_mode": format(stat.st_mode & 0o777, "04o"),
        "source_size": len(content),
        "source_sha256": hashlib.sha256(content).hexdigest(),
        "source_git_blob": _blob_identity(root, relative),
        "kind": "document" if kind != "artifact" else "artifact",
        "profile": profile,
        "payload_mode": "inline" if kind != "artifact" else mode,
        "media_type": media_type,
        "title": declaration.get("title") or source.stem.replace("_", " ").replace("-", " ").strip() or source.name,
        "namespace": declaration.get("namespace") or "migration",
        "retention": declaration.get("retention") or {"class": "standard"},
        "sensitivity": declaration.get("sensitivity") or "normal",
        "relations": declaration.get("relations") or [],
    }
    if kind == "workflow":
        result["schema_ref"] = WORKFLOW_SCHEMA_V1
    return result


def expand_declarations(root: Path, declarations: Sequence[Mapping[str, Any]], *,
                        wiki_roots: Sequence[str] = (), workflow_roots: Sequence[str] = ()) -> list[dict[str, Any]]:
    """Expand explicit declarations and bounded Document roots deterministically."""
    expanded = [dict(item) for item in declarations]
    for kind, roots in (("wiki", wiki_roots), ("workflow", workflow_roots)):
        extensions = _DOCUMENT_EXTENSIONS[kind]
        for supplied in roots:
            raw = Path(supplied)
            if raw.is_absolute() or ".." in raw.parts:
                raise RecordError("MIGRATION_PATH_UNSAFE", "scan roots must be relative")
            base = root / raw
            try:
                resolved = base.resolve(strict=True)
                resolved.relative_to(root.resolve(strict=True))
            except (OSError, ValueError) as exc:
                raise RecordError("MIGRATION_PATH_UNSAFE", f"scan root is missing or unsafe: {supplied}") from exc
            if base.is_symlink() or not resolved.is_dir():
                raise RecordError("MIGRATION_PATH_UNSAFE", f"scan root must be a real directory: {supplied}")
            for path in sorted(resolved.rglob("*")):
                if path.is_symlink():
                    raise RecordError("MIGRATION_PATH_UNSAFE", f"scan encountered symlink: {path}")
                if path.is_file() and path.suffix.casefold() in extensions:
                    expanded.append({"kind": kind, "path": path.relative_to(root.resolve()).as_posix()})
    seen: set[str] = set()
    result = []
    for item in expanded:
        path = str(item.get("path") or "")
        if path in seen:
            raise RecordError("MIGRATION_SOURCE_DUPLICATE", f"source path declared more than once: {path}")
        seen.add(path)
        result.append(item)
    return result


def inventory(source_root: Path, declarations: Sequence[Mapping[str, Any]], *,
              wiki_roots: Sequence[str] = (), workflow_roots: Sequence[str] = ()) -> dict[str, Any]:
    root = Path(source_root).resolve(strict=True)
    expanded = expand_declarations(root, declarations, wiki_roots=wiki_roots,
                                   workflow_roots=workflow_roots)
    items = [_file_item(root, item) for item in expanded]
    items.sort(key=lambda item: item["source_path"])
    return _seal({
        "schema_version": INVENTORY_SCHEMA,
        "created_at": _now(),
        "runtime_version": __version__,
        "source": _source_identity(root),
        "items": items,
        "summary": {
            "total": len(items),
            "documents": sum(item["kind"] == "document" for item in items),
            "artifacts": sum(item["kind"] == "artifact" for item in items),
            "bytes": sum(item["source_size"] for item in items),
        },
    }, "inventory_sha256")


def write_inventory(path: Path, value: Mapping[str, Any], source_root: Path) -> None:
    _outside_source(path, Path(source_root))
    _atomic_json(path, value, fresh=True)


def _base62(raw: bytes) -> str:
    number = int.from_bytes(raw, "big")
    chars = []
    for _ in range(6):
        number, remainder = divmod(number, len(_BASE62))
        chars.append(_BASE62[remainder])
    return "".join(chars)


def _existing(documents: DocumentStore, artifacts: ArtifactStore, record_id: str) -> Optional[dict[str, Any]]:
    for getter in (documents.get, artifacts.get):
        try:
            return getter(record_id)
        except RecordError as exc:
            if exc.code != "RECORD_NOT_FOUND":
                raise
    return None


def _expected_payload(item: Mapping[str, Any]) -> str:
    return str(item["source_sha256"])


def _record_matches(record: Mapping[str, Any], item: Mapping[str, Any]) -> bool:
    if record.get("kind") != item.get("kind") or record.get("profile") != item.get("profile"):
        return False
    payload = record.get("payload") or {}
    if record.get("kind") == "document":
        migration = (record.get("custom_metadata") or {}).get("migration.yylo")
    else:
        migration = (record.get("system_metadata") or {}).get("migration")
    return (payload.get("sha256") == _expected_payload(item)
            and isinstance(migration, Mapping)
            and migration.get("source_path") == item.get("source_path")
            and migration.get("source_sha256") == item.get("source_sha256"))


def make_plan(inventory_value: Mapping[str, Any], *, destination_root: Path,
              documents: DocumentStore, artifacts: ArtifactStore,
              source_root: Optional[Path] = None) -> dict[str, Any]:
    if inventory_value.get("schema_version") != INVENTORY_SCHEMA:
        raise RecordError("MIGRATION_INVENTORY_INVALID", "unsupported inventory schema")
    _verify_seal(inventory_value, "inventory_sha256", "MIGRATION_INVENTORY_TAMPERED")
    if inventory_value.get("runtime_version") != __version__:
        raise RecordError("MIGRATION_RUNTIME_DRIFT", "inventory runtime differs from the executing Ledger")
    if source_root is not None:
        root = Path(source_root).resolve(strict=True)
        if _source_identity(root) != inventory_value.get("source"):
            raise RecordError("MIGRATION_SOURCE_DRIFT", "source root or Git HEAD differs from the inventory")
        for item in inventory_value.get("items") or []:
            relative, path = _safe_relative(root, str(item.get("source_path") or ""))
            content = path.read_bytes()
            if (len(content) != item.get("source_size")
                    or hashlib.sha256(content).hexdigest() != item.get("source_sha256")
                    or _blob_identity(root, relative) != item.get("source_git_blob")):
                raise RecordError("MIGRATION_SOURCE_DRIFT", f"source changed after inventory: {relative}")
    assigned: set[str] = set()
    planned = []
    for source in inventory_value.get("items") or []:
        if not isinstance(source, Mapping):
            raise RecordError("MIGRATION_INVENTORY_INVALID", "inventory item must be an object")
        counter = 0
        while True:
            seed = f"{inventory_value['source']['root_sha256']}\0{source['source_path']}\0{source['kind']}\0{source['profile']}\0{counter}"
            record_id = _base62(hashlib.sha256(seed.encode()).digest())
            if not RECORD_ID_RE.fullmatch(record_id):
                counter += 1
                continue
            existing = _existing(documents, artifacts, record_id)
            if record_id not in assigned and (existing is None or _record_matches(existing, source)):
                break
            counter += 1
        assigned.add(record_id)
        planned.append({**dict(source), "record_id": record_id,
                        "destination_state": "exact_existing" if existing else "absent"})
    plan = {
        "schema_version": PLAN_SCHEMA,
        "created_at": _now(),
        "runtime_version": __version__,
        "inventory_sha256": inventory_value["inventory_sha256"],
        "source": dict(inventory_value["source"]),
        "destination": {
            "root_sha256": hashlib.sha256(str(Path(destination_root).resolve()).encode()).hexdigest(),
        },
        "items": planned,
        "summary": dict(inventory_value["summary"]),
        "deletes_source": False,
    }
    return _seal(plan, "plan_sha256")


def write_plan(path: Path, value: Mapping[str, Any], source_root: Path) -> None:
    _outside_source(path, Path(source_root))
    _atomic_json(path, value, fresh=True)


class RecordMigration:
    """Apply and verify an immutable plan with per-item durable status."""

    def __init__(self, *, source_root: Path, juno_root: Path,
                 project_root: Optional[Path] = None,
                 repository_ids: Optional[Mapping[str, str]] = None):
        self.source_root = Path(source_root).resolve(strict=True)
        self.juno_root = Path(juno_root)
        self.documents = DocumentStore(self.juno_root, project_root=project_root,
                                       repository_ids=repository_ids)
        self.artifacts = ArtifactStore(self.juno_root, project_root=project_root,
                                       repository_ids=repository_ids)

    def load_plan(self, path: Path) -> dict[str, Any]:
        plan = _read_json(path, PLAN_SCHEMA)
        _verify_seal(plan, "plan_sha256", "MIGRATION_PLAN_TAMPERED")
        if plan.get("runtime_version") != __version__:
            raise RecordError("MIGRATION_RUNTIME_DRIFT", "plan runtime differs from the executing Ledger")
        source = _source_identity(self.source_root)
        if source != plan.get("source"):
            raise RecordError("MIGRATION_SOURCE_DRIFT", "source root or Git HEAD differs from the plan")
        destination = hashlib.sha256(str(self.juno_root.parent.resolve()).encode()).hexdigest()
        if plan.get("destination", {}).get("root_sha256") != destination:
            raise RecordError("MIGRATION_DESTINATION_DRIFT", "destination root differs from the plan")
        return plan

    def _new_status(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": STATUS_SCHEMA,
            "plan_sha256": plan["plan_sha256"],
            "created_at": _now(),
            "updated_at": _now(),
            "source_preserved": True,
            "items": {item["record_id"]: {"source_path": item["source_path"], "state": "pending", "attempts": 0}
                      for item in plan["items"]},
        }

    def status(self, plan: Mapping[str, Any], path: Path, *, create: bool = False) -> dict[str, Any]:
        _outside_source(path, self.source_root)
        if not Path(path).exists():
            if not create:
                raise RecordError("MIGRATION_STATUS_MISSING", "migration status receipt does not exist")
            value = self._new_status(plan)
            _write_status(path, value, fresh=True)
            return value
        value = _read_json(path, STATUS_SCHEMA)
        _verify_seal(value, "status_sha256", "MIGRATION_STATUS_TAMPERED")
        if value.get("plan_sha256") != plan.get("plan_sha256"):
            raise RecordError("MIGRATION_STATUS_DRIFT", "status receipt belongs to another plan")
        expected = {item["record_id"] for item in plan["items"]}
        if set(value.get("items") or {}) != expected:
            raise RecordError("MIGRATION_STATUS_DRIFT", "status item set differs from the plan")
        return value

    def _source_bytes(self, item: Mapping[str, Any]) -> bytes:
        relative, path = _safe_relative(self.source_root, str(item["source_path"]))
        content = path.read_bytes()
        if len(content) != item["source_size"] or hashlib.sha256(content).hexdigest() != item["source_sha256"]:
            raise RecordError("MIGRATION_SOURCE_DRIFT", f"source bytes changed after inventory: {relative}")
        if _blob_identity(self.source_root, relative) != item.get("source_git_blob"):
            raise RecordError("MIGRATION_SOURCE_DRIFT", f"source Git blob changed after inventory: {relative}")
        return content

    def _find(self, plan: Mapping[str, Any], record_id: str) -> Mapping[str, Any]:
        matches = [item for item in plan["items"] if item["record_id"] == record_id]
        if len(matches) != 1:
            raise RecordError("MIGRATION_ITEM_UNKNOWN", f"Record ID is not in the plan: {record_id}")
        return matches[0]

    def _get_destination(self, item: Mapping[str, Any]) -> Optional[dict[str, Any]]:
        return _existing(self.documents, self.artifacts, str(item["record_id"]))

    def _create(self, plan: Mapping[str, Any], item: Mapping[str, Any], content: bytes) -> dict[str, Any]:
        migration = {"schema_version": 1, "plan_sha256": plan["plan_sha256"],
                     "source_path": item["source_path"], "source_sha256": item["source_sha256"]}
        if item["kind"] == "document":
            text = content.decode("utf-8")
            return self.documents.create(
                record_id=item["record_id"], title=item["title"], profile=item["profile"],
                media_type=item["media_type"], text=text, namespace=item["namespace"],
                schema_ref=item.get("schema_ref"), relations=item.get("relations") or (),
                custom_metadata={"migration.yylo": migration})
        return self.artifacts.create(
            record_id=item["record_id"], title=item["title"], profile=item["profile"],
            mode=item["payload_mode"], content=content, media_type=item["media_type"],
            retention=item.get("retention"), migration_metadata=migration)

    def _verify_item(self, item: Mapping[str, Any], content: bytes) -> dict[str, Any]:
        record = self._get_destination(item)
        if record is None:
            raise RecordError("MIGRATION_DESTINATION_MISSING", "planned Record does not exist")
        if not _record_matches(record, item):
            raise RecordError("MIGRATION_DESTINATION_DRIFT", "planned Record identity or digest differs")
        if item["kind"] == "document":
            actual = record["payload"]["text"].encode("utf-8")
        else:
            actual = self.artifacts.verify(item["record_id"])
        if actual != content:
            raise RecordError("MIGRATION_BYTE_MISMATCH", "destination bytes differ from source")
        events = self.documents.history(item["record_id"]) if item["kind"] == "document" else [
            json.loads(path.read_text(encoding="utf-8")) for path in sorted(
                (self.artifacts.events_root / item["record_id"][:2].lower() / item["record_id"]).glob("*.json"))]
        if not events:
            raise RecordError("MIGRATION_HISTORY_MISSING", "destination has no creation history")
        return {"revision": record["revision"], "record_sha256": value_digest(record),
                "payload_sha256": item["source_sha256"], "history_events": len(events)}

    def apply_one(self, plan: Mapping[str, Any], status_path: Path, record_id: str) -> dict[str, Any]:
        status = self.status(plan, status_path, create=True)
        item = self._find(plan, record_id)
        entry = status["items"][record_id]
        entry["attempts"] = int(entry.get("attempts", 0)) + 1
        entry["state"] = "copying"
        entry["started_at"] = _now()
        entry.pop("error", None)
        status["updated_at"] = _now()
        _write_status(status_path, status)
        try:
            content = self._source_bytes(item)
            existing = self._get_destination(item)
            if existing is not None and not _record_matches(existing, item):
                raise RecordError("MIGRATION_DESTINATION_DRIFT", "Record ID is occupied by different content")
            record = existing or self._create(plan, item, content)
            receipt = self._verify_item(item, content)
            entry.update({"state": "verified", "completed_at": _now(), "receipt": receipt,
                          "idempotent_reuse": existing is not None, "source_preserved": True})
            status["updated_at"] = _now()
            _write_status(status_path, status)
            return {"record": record, "status": dict(entry)}
        except BaseException as exc:
            entry["state"] = "failed"
            entry["failed_at"] = _now()
            entry["error"] = {"code": exc.code, "message": exc.message} if isinstance(exc, RecordError) else {
                "code": "MIGRATION_APPLY_FAILED", "message": str(exc)}
            status["updated_at"] = _now()
            _write_status(status_path, status)
            raise

    def apply(self, plan: Mapping[str, Any], status_path: Path, *, record_ids: Sequence[str],
              all_items: bool = False, continue_on_error: bool = False) -> dict[str, Any]:
        if all_items == bool(record_ids):
            raise RecordError("MIGRATION_SELECTION_INVALID", "select exactly one or more --id values, or explicit --all")
        selected = [item["record_id"] for item in plan["items"]] if all_items else list(record_ids)
        if len(selected) != len(set(selected)):
            raise RecordError("MIGRATION_SELECTION_INVALID", "duplicate Record IDs are not allowed")
        results, failures = [], []
        for record_id in selected:
            try:
                results.append({"id": record_id, **self.apply_one(plan, status_path, record_id)})
            except BaseException as exc:
                failures.append({"id": record_id, "error": str(exc)})
                if not continue_on_error:
                    raise
        return {"applied": results, "failures": failures, "status": self.status(plan, status_path)}

    def verify(self, plan: Mapping[str, Any], status_path: Path, *, record_ids: Sequence[str] = ()) -> dict[str, Any]:
        status = self.status(plan, status_path, create=False)
        selected = set(record_ids) if record_ids else {item["record_id"] for item in plan["items"]}
        unknown = selected - {item["record_id"] for item in plan["items"]}
        if unknown:
            raise RecordError("MIGRATION_ITEM_UNKNOWN", "verification selection contains an unknown ID")
        verified, failures = [], []
        for item in plan["items"]:
            if item["record_id"] not in selected:
                continue
            try:
                receipt = self._verify_item(item, self._source_bytes(item))
                entry = status["items"][item["record_id"]]
                entry.update({"state": "verified", "verified_at": _now(), "receipt": receipt,
                              "source_preserved": True})
                verified.append(item["record_id"])
            except BaseException as exc:
                failures.append({"id": item["record_id"], "error": str(exc)})
        status["updated_at"] = _now()
        _write_status(status_path, status)
        return {"ok": not failures, "verified": verified, "failures": failures,
                "summary": status_summary(status)}


def status_summary(status: Mapping[str, Any]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for item in (status.get("items") or {}).values():
        state = str(item.get("state") or "unknown")
        counts[state] = counts.get(state, 0) + 1
    return {"plan_sha256": status.get("plan_sha256"), "total": sum(counts.values()),
            "states": counts, "source_preserved": status.get("source_preserved") is True}
