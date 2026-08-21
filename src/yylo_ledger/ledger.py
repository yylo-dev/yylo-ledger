"""Append-only, hash-chained, segmented per-task ledgers."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

MAX_SEGMENT_BYTES = 5 * 1024 * 1024


class LedgerError(ValueError):
    pass


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _hash_event(event: Mapping[str, Any]) -> str:
    payload = {key: value for key, value in event.items() if key != "event_sha256"}
    return hashlib.sha256(_canonical(payload)).hexdigest()


def changed_paths(before: Mapping[str, Any], after: Mapping[str, Any]) -> List[str]:
    keys = sorted(set(before) | set(after))
    return [f"/{key}" for key in keys if before.get(key) != after.get(key)]


class TaskLedger:
    def __init__(self, root: Path, max_segment_bytes: int = MAX_SEGMENT_BYTES):
        self.root = Path(root)
        self.max_segment_bytes = max_segment_bytes

    def directory(self, task_id: str) -> Path:
        return self.root / task_id[:2].lower() / task_id

    def segments(self, task_id: str) -> List[Path]:
        directory = self.directory(task_id)
        paths = sorted(directory.glob("[0-9][0-9][0-9][0-9][0-9][0-9].ndjson"))
        expected = [f"{i:06d}.ndjson" for i in range(1, len(paths) + 1)]
        if [p.name for p in paths] != expected:
            raise LedgerError(f"non-contiguous ledger segments for {task_id}")
        return paths

    def read(self, task_id: str, verify: bool = True) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        previous = None
        for path in self.segments(task_id):
            with path.open(encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, 1):
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise LedgerError(f"invalid ledger JSON {path}:{line_number}: {exc}") from exc
                    if verify:
                        if event.get("previous_event_sha256") != previous:
                            raise LedgerError(f"ledger chain discontinuity {path}:{line_number}")
                        if event.get("event_sha256") != _hash_event(event):
                            raise LedgerError(f"ledger event hash mismatch {path}:{line_number}")
                    previous = event.get("event_sha256")
                    events.append(event)
        return events

    def latest(self, task_id: str) -> Optional[Dict[str, Any]]:
        events = self.read(task_id)
        return events[-1] if events else None

    def prepare(self, task_id: str, operation: str, source: str, before_hash: Optional[str],
                after_hash: str, before: Mapping[str, Any], after: Mapping[str, Any],
                include_snapshot: bool = False) -> Dict[str, Any]:
        """Build and size-check the exact event before canonical state is replaced."""
        latest = self.latest(task_id)
        paths = changed_paths(before, after)
        changes = []
        for path in paths:
            key = path[1:]
            change = {"op": "replace" if key in before and key in after else ("add" if key in after else "remove"), "path": path}
            # A creation snapshot already contains every value. Repeating those
            # values in `changes` can turn a valid <5 MiB task into a >5 MiB
            # ledger blob without adding history information.
            if key in after and not include_snapshot:
                change["value"] = after[key]
            changes.append(change)
        event: Dict[str, Any] = {
            "event_id": str(uuid.uuid4()),
            "task_id": task_id,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "operation": operation,
            "source": source,
            "before_sha256": before_hash,
            "after_sha256": after_hash,
            "previous_event_sha256": latest.get("event_sha256") if latest else None,
            "changed_paths": paths,
            "changes": changes,
        }
        if include_snapshot:
            event["snapshot"] = dict(after)
        event["event_sha256"] = _hash_event(event)
        if len(_canonical(event)) + 1 > self.max_segment_bytes:
            raise LedgerError(
                f"ledger event for {task_id} exceeds {self.max_segment_bytes} bytes; "
                "reduce the changed task content before retrying"
            )
        return event

    def append_prepared(self, event: Mapping[str, Any]) -> Dict[str, Any]:
        """Append an event returned by prepare, refusing a stale chain tip."""
        task_id = str(event["task_id"])
        latest = self.latest(task_id)
        expected_previous = latest.get("event_sha256") if latest else None
        if event.get("previous_event_sha256") != expected_previous:
            raise LedgerError(f"prepared ledger event for {task_id} has a stale chain tip")
        if event.get("event_sha256") != _hash_event(event):
            raise LedgerError(f"prepared ledger event hash mismatch for {task_id}")
        encoded = _canonical(event) + b"\n"
        if len(encoded) > self.max_segment_bytes:
            raise LedgerError(f"ledger event for {task_id} exceeds {self.max_segment_bytes} bytes")
        directory = self.directory(task_id)
        directory.mkdir(parents=True, exist_ok=True)
        segments = self.segments(task_id)
        target = segments[-1] if segments else directory / "000001.ndjson"
        if target.exists() and target.stat().st_size and target.stat().st_size + len(encoded) > self.max_segment_bytes:
            target = directory / f"{len(segments) + 1:06d}.ndjson"
        created = not target.exists()
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            # os.write is allowed to complete partially (notably under fault
            # injection, signals, or unusual filesystems).  A truncated NDJSON
            # event would poison the append-only chain, so drain the complete
            # buffer before acknowledging the mutation.
            view = memoryview(encoded)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("ledger append made no forward progress")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        if created:
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return dict(event)

    def append(self, task_id: str, operation: str, source: str, before_hash: Optional[str],
               after_hash: str, before: Mapping[str, Any], after: Mapping[str, Any],
               include_snapshot: bool = False) -> Dict[str, Any]:
        event = self.prepare(task_id, operation, source, before_hash, after_hash,
                             before, after, include_snapshot)
        return self.append_prepared(event)
