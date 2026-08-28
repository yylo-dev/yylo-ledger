#!/usr/bin/env python3
"""Git-native Markdown task store; canonical state never comes from cache or ledger."""
from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import json
import os
import subprocess
import tempfile
import shutil
import time
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Set

from .cache import TaskCache
from .codec import MarkdownTaskCodec, TaskFormatError, normalized_bytes, plain_value
from .config import Config
from .git_creation import attach_creation_context, capture_creation_context
from .ledger import LedgerError, TaskLedger
from .models import Task
from .records import (RecordError, RevisionProvenance, exact_replace,
                      task_record_projection, validate_record)
from .validators import TaskValidator


class ConflictError(RuntimeError):
    def __init__(self, task_id: str, expected: str, current: str):
        super().__init__(f"stale task revision for {task_id}: expected {expected}, current {current}")
        self.task_id, self.expected, self.current = task_id, expected, current


class UnmetBlockersError(ValueError):
    """A mutation would expose a completed task with unresolved dependencies."""

    def __init__(self, task_id: str, blocker_ids: List[str]):
        blockers = ", ".join(sorted(blocker_ids))
        super().__init__(f"cannot complete task {task_id}; unmet blockers: {blockers}")
        self.task_id = task_id
        self.blocker_ids = sorted(blocker_ids)


class ArchivedTaskError(ValueError):
    def __init__(self, task_id: str):
        super().__init__(
            f"Task {task_id} is in an immutable cold archive and cannot be changed or reopened; "
            "create a new task with related_tasks referencing the archived ID")
        self.task_id = task_id


@dataclass(frozen=True)
class MutationReceipt:
    task_id: str
    operation: str
    before_sha256: Optional[str]
    after_sha256: str
    ledger_event_id: str
    changed_paths: List[str]
    persisted_path: str
    transaction: Optional[Dict[str, Any]] = None

    def __bool__(self):
        return True

    def to_dict(self):
        return dict(self.__dict__)


class TaskStorage:
    """One Markdown file per task, one lock and append-only ledger per task."""

    def __init__(self, config: Optional[Config] = None, *, create_directories: bool = True,
                 git_project_root: Optional[Path] = None):
        self.config = config or Config(auto_create=create_directories)
        self.base_path = os.path.abspath(self.config.storage_base_path)
        self.file_pattern = "*/*.md"
        self.default_file = ""
        self.tasks_root = Path(self.base_path)
        self.juno_root = self.tasks_root.parent
        self.project_root = self.juno_root.parent
        creation_config = self.config.to_dict().get("git_creation_context", {})
        configured_project = (git_project_root or creation_config.get("project_root")
                              or os.environ.get("YYLO_LEDGER_INVOCATION_ROOT"))
        self.git_project_root = Path(configured_project) if configured_project else self.project_root
        self.git_repository_ids = dict(creation_config.get("repository_ids") or {})
        if create_directories:
            self.tasks_root.mkdir(parents=True, exist_ok=True)
        self.codec = MarkdownTaskCodec()
        self.ledger = TaskLedger(self.juno_root / "ledger")
        self.cache = TaskCache(self.juno_root / "cache" / "kanban.sqlite3")
        # One CLI process sees one immutable archive snapshot. Reuse its verified
        # ID inventory for multi-ID exact reads instead of reparsing every sealed
        # manifest for every requested identity.
        self._archive_id_inventory: Optional[Dict[str, str]] = None

    @staticmethod
    def _validate_id(task_id: str):
        valid, error = TaskValidator.validate_id(task_id)
        if not valid or any(char in task_id for char in "/\\."):
            raise ValueError(error or f"invalid task ID: {task_id}")

    def task_path(self, task_id: str) -> Path:
        self._validate_id(task_id)
        # Normalize only the shard directory. On case-insensitive filesystems,
        # mixed-case prefixes otherwise alias to the first directory spelling
        # and make valid task files fail strict path verification.
        return self.tasks_root / task_id[:2].lower() / f"{task_id}.md"

    def get_files(self, pattern: Optional[str] = None) -> List[str]:
        # Runtime intentionally never scans legacy NDJSON.
        return [str(path) for path in sorted(self.tasks_root.glob("*/*.md"))]

    def get_default_filepath(self) -> str:
        return str(self.tasks_root)

    def read_tasks(self, filepath: str, skip_errors: bool = True) -> Iterator[Dict[str, Any]]:
        path = Path(filepath)
        if not path.exists() or path.suffix != ".md":
            return
        try:
            yield self._read_path(path)
        except (OSError, TaskFormatError, ValueError) as exc:
            if not skip_errors:
                raise ValueError(f"Parse error at {path}: {exc}") from exc
            print(f"Warning: Parse error at {path}: {exc}")

    def _read_path(self, path: Path) -> Dict[str, Any]:
        record = self.codec.loads(path.read_text(encoding="utf-8"))
        expected = self.task_path(record["id"])
        if path.stem != record["id"] or path.parent.name != expected.parent.name:
            raise TaskFormatError(f"task id/path mismatch: {path}")
        return record

    def _config_hash(self) -> str:
        payload = json.dumps(plain_value(self.config.to_dict()), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    def read_all_tasks_canonical(self) -> Iterator[Dict[str, Any]]:
        """Explicit source scan used by doctor, cache rebuild, and differential tests."""
        for filepath in self.get_files():
            yield self._read_path(Path(filepath))

    def read_all_tasks_complete(self) -> Iterator[Dict[str, Any]]:
        """Yield complete hot and verified cold canonical task truth."""
        yield from self.read_all_tasks_canonical()
        from .archive import iter_archive_envelopes
        for envelope in iter_archive_envelopes(self.juno_root):
            yield envelope["task"]

    def _write_conversion_task(self, record: Mapping[str, Any]) -> Path:
        """Write one globally prevalidated import row without per-row cache rebuilds."""
        task_id = str(record["id"])
        digest = self.normalized_hash(record)
        event = self.ledger.prepare(task_id, "create", "conversion", None, digest,
                                    {}, plain_value(record), True)
        path = self._write_current(record)
        self.ledger.append_prepared(event)
        return path

    def read_all_tasks(self, file_pattern: Optional[str] = None) -> Iterator[Dict[str, Any]]:
        """Serve collections from a freshness-validated derived cache."""
        with self._cache_refresh_lock():
            fresh = self.cache.ensure_fresh(
                self.tasks_root, self.project_root, self._config_hash(), self._read_path, self.normalized_hash
            )
            if not fresh:
                TaskStorage.rebuild_cache(self, _cache_lock_held=True)
        cached = self.cache.all()
        if cached is None:  # A disposable-cache failure cannot change correctness.
            yield from self.read_all_tasks_canonical()
            return
        yield from cached

    def _archive_inventory(self) -> Dict[str, str]:
        if self._archive_id_inventory is None:
            from .archive import scan_archive_id_inventory
            self._archive_id_inventory = scan_archive_id_inventory(self.juno_root)
        return self._archive_id_inventory

    def _archive_entry_after_canonical_miss_check(self, task_id: str):
        """Resolve a cold entry; prove ordinary misses from sealed ID manifests.

        SQLite remains disposable and cannot prove absence. The checksummed
        canonical manifests can, without rebuilding/decoding every hot task and
        cold pack. If the canonical inventory says the row should exist, rebuild
        once to repair the derived index and then fail closed if it is still absent.
        """
        self._ensure_query_cache()
        entry = self.cache.archive_entry(task_id)
        if entry is not None:
            return entry
        from .archive import ArchiveFormatError
        inventory = self._archive_inventory()
        canonical_id = inventory.get(task_id.casefold())
        if canonical_id is None:
            return None
        self.rebuild_cache()
        entry = self.cache.archive_entry(task_id)
        if entry is None or entry["task_id"] != canonical_id:
            raise ArchiveFormatError(
                "derived archive index could not restore canonical task: %s" % canonical_id)
        return entry

    def resolve_task(self, task_id: str) -> Optional[tuple]:
        """Resolve one canonical tier, taking the bounded direct hot path first.

        A hot identity hit never opens/rebuilds the derived cache and never scans
        cold packs. Global hot/cold duplicate auditing remains a doctor contract;
        a hot miss retains strict verified archive lookup semantics.
        """
        self._validate_id(task_id)
        path = self.task_path(task_id)
        if path.exists():
            hot = self._read_path(path)
            # Preserve fail-closed tier uniqueness without opening SQLite or
            # decoding cold records. Sealed ID manifests are compact canonical
            # metadata, not the disposable query cache or pack payload scan.
            if (self.juno_root / "archive").exists():
                from .archive import ArchiveFormatError
                archived_id = self._archive_inventory().get(task_id.casefold())
                if archived_id == task_id:
                    raise ArchiveFormatError(f"task exists in both hot and cold tiers: {task_id}")
            return "hot", hot, None
        entry = self._archive_entry_after_canonical_miss_check(task_id)
        if entry and entry["task_id"] != task_id:
            entry = None
        if entry is None:
            return None
        from .archive import ArchiveFormatError, read_indexed_archive_record
        try:
            envelope = read_indexed_archive_record(entry)
        except ArchiveFormatError:
            # Distinguish a damaged disposable row from damaged canonical bytes.
            # Rebuild verifies every pack and will itself fail closed on the latter.
            self.rebuild_cache()
            repaired = self.cache.archive_entry(task_id)
            if not repaired or repaired["task_id"] != task_id:
                raise
            envelope = read_indexed_archive_record(repaired)
        return "cold", envelope["task"], envelope

    def find_task(self, task_id: str, file_pattern: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Return hot current state for mutation/planning compatibility."""
        path = self.task_path(task_id)
        return self._read_path(path) if path.exists() else None

    def find_task_exact(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Transparent public exact lookup across the one canonical tier."""
        resolved = self.resolve_task(task_id)
        return resolved[1] if resolved else None

    def find_task_file(self, task_id: str, file_pattern: Optional[str] = None) -> Optional[str]:
        path = self.task_path(task_id)
        return str(path) if path.exists() else None

    def resolve_record_id(self, id_or_slug: str) -> str:
        """Resolve one ID/slug/retained alias to exactly one immutable ID."""
        if not isinstance(id_or_slug, str) or not id_or_slug:
            raise RecordError("RECORD_NOT_FOUND", "record identity is empty")
        valid, _ = TaskValidator.validate_id(id_or_slug)
        if valid and self.find_task_exact(id_or_slug) is not None:
            return id_or_slug
        matches = []
        for task in self.read_all_tasks_complete():
            record = (dict(task) if task.get("kind") in ("document", "artifact")
                      else task_record_projection(task))
            if id_or_slug == record["slug"] or id_or_slug in record["aliases"]:
                matches.append(record["id"])
        matches = sorted(set(matches))
        if not matches:
            raise RecordError("RECORD_NOT_FOUND", f"no Record matches {id_or_slug!r}")
        if len(matches) != 1:
            raise RecordError("RECORD_IDENTITY_AMBIGUOUS", f"identity matches {len(matches)} Records")
        return matches[0]

    def get_record(self, id_or_slug: str) -> Dict[str, Any]:
        record_id = self.resolve_record_id(id_or_slug)
        task = self.find_task_exact(record_id)
        if task is None:
            raise RecordError("RECORD_NOT_FOUND", f"Record {record_id!r} disappeared")
        record = (dict(task) if task.get("kind") in ("document", "artifact")
                  else task_record_projection(task))
        validate_record(record)
        return record

    @staticmethod
    def normalized_hash(record: Mapping[str, Any]) -> str:
        return hashlib.sha256(normalized_bytes(record)).hexdigest()

    def _case_collision(self, task_id: str) -> Optional[str]:
        archived = self._archive_entry_after_canonical_miss_check(task_id)
        if archived:
            return archived["task_id"]
        with self._cache_refresh_lock():
            fresh = self.cache.ensure_fresh(
                self.tasks_root, self.project_root, self._config_hash(), self._read_path, self.normalized_hash
            )
            if fresh:
                return self.cache.case_collision(task_id)
        # Bootstrap/failure fallback remains canonical and correct.
        wanted = task_id.casefold()
        for path in self.tasks_root.glob("*/*.md"):
            existing = path.stem
            if existing.casefold() == wanted and existing != task_id:
                return existing
        return None

    @contextmanager
    def _cache_refresh_lock(self):
        """Serialize disposable-cache refresh owners, never ordinary SQL readers.

        Collection commands can discover stale derived metadata concurrently. A
        dedicated lease prevents those read-induced writers from deleting or
        rebuilding the live SQLite file underneath one another. It is separate
        from canonical per-task mutation locks: once freshness is established,
        normal WAL-backed queries still overlap.
        """
        path = self.juno_root / "locks" / "cache-refresh.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as handle:
            timeout = max(0.0, float(os.environ.get(
                "YYLO_LEDGER_CACHE_REFRESH_TIMEOUT_SECONDS", "5")))
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        handle.seek(0)
                        owner = handle.read().decode("utf-8", errors="replace").strip() or "unknown"
                        raise TimeoutError(
                            f"cache_refresh_wait_timeout: resource={path} "
                            f"waited_seconds={timeout:g} owner={owner}")
                    time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            owner = json.dumps({"pid": os.getpid(), "resource": str(path),
                                "acquired_at": datetime.now(timezone.utc).isoformat()})
            handle.seek(0)
            handle.truncate()
            handle.write(owner.encode("utf-8"))
            handle.flush()
            try:
                yield
            finally:
                handle.seek(0)
                handle.truncate()
                handle.flush()
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def _lock(self, task_id: str):
        # Case-fold-equivalent IDs must share a lock. Otherwise `Ab1Cd2` and
        # `ab1cd2` can both pass collision checks and persist concurrently on a
        # case-sensitive filesystem.
        lock_id = task_id.casefold()
        path = self.juno_root / "locks" / lock_id[:2] / f"{lock_id}.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as handle:
            timeout = max(0.0, float(os.environ.get("YYLO_LEDGER_LOCK_TIMEOUT_SECONDS", "5")))
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        handle.seek(0)
                        owner = handle.read().decode("utf-8", errors="replace").strip() or "unknown"
                        raise TimeoutError(
                            f"lock_wait_timeout: resource={path} waited_seconds={timeout:g} owner={owner}")
                    time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            owner = json.dumps({"pid": os.getpid(), "resource": str(path),
                                "acquired_at": datetime.now(timezone.utc).isoformat()})
            handle.seek(0)
            handle.truncate()
            handle.write(owner.encode("utf-8"))
            handle.flush()
            try:
                yield
            finally:
                handle.seek(0)
                handle.truncate()
                handle.flush()
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _atomic_bytes(path: Path, content: bytes):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    @staticmethod
    def _atomic_write(path: Path, content: str):
        TaskStorage._atomic_bytes(path, content.encode("utf-8"))

    def _verify_registered_controller(self):
        """Reject direct-CLI local fallback when the invocation Git repo is registered."""
        raw = os.environ.get("YYLO_LEDGER_INVOCATION_ROOT", "").strip()
        source = Path(raw).expanduser().resolve() if raw else Path.cwd().resolve()
        top = subprocess.run(["git", "-C", str(source), "rev-parse", "--show-toplevel"],
                             text=True, capture_output=True)
        if top.returncode != 0:
            return
        source_root = Path(top.stdout.strip()).resolve()
        values = {}
        for name in ("path", "branch"):
            result = subprocess.run(
                ["git", "-C", str(source_root), "config", "--local", "--get-all", f"juno.controller.{name}"],
                text=True, capture_output=True)
            values[name] = result.stdout.splitlines() if result.returncode == 0 else []
        if not values["path"] and not values["branch"]:
            return
        if len(values["path"]) != 1 or len(values["branch"]) != 1:
            raise ValueError("canonical controller registration is incomplete or ambiguous; refusing mutation")
        registered = Path(values["path"][0]).expanduser()
        if not registered.is_absolute():
            registered = source_root / registered
        try:
            registered = registered.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"registered canonical controller is unavailable: {exc}") from exc
        expected_ref = values["branch"][0]
        if not expected_ref.startswith("refs/heads/"):
            expected_ref = f"refs/heads/{expected_ref}"
        actual = subprocess.run(["git", "-C", str(registered), "symbolic-ref", "-q", "HEAD"],
                                text=True, capture_output=True)
        if registered != self.project_root.resolve() or actual.returncode != 0 or actual.stdout.strip() != expected_ref:
            raise ValueError(
                f"mutation authority is registered at {registered} on {expected_ref}; local fallback refused")

    @contextmanager
    def _board_lock(self):
        """Serialize exact multi-path plans and recover abandoned activation first."""
        self._verify_registered_controller()
        path = self.juno_root / "locks" / "board-mutation.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                self._recover_transactions_locked()
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _mutation_fault(self, point: str):
        """Stable fault boundary for unit and real-process interruption tests."""
        if os.environ.get("YYLO_LEDGER_CRASH_POINT") == point:
            os._exit(91)
        if os.environ.get("YYLO_LEDGER_FAULT_POINT") == point:
            raise OSError(f"injected mutation fault: {point}")

    @staticmethod
    def _path_state(path: Path) -> Dict[str, Any]:
        if not path.exists():
            return {"exists": False, "sha256": None, "bytes": None}
        content = path.read_bytes()
        return {"exists": True, "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": base64.b64encode(content).decode("ascii")}

    def _git_mutation_identity(self) -> Dict[str, Any]:
        common = subprocess.run(
            ["git", "-C", str(self.project_root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            text=True, capture_output=True)
        head = subprocess.run(["git", "-C", str(self.project_root), "rev-parse", "HEAD"],
                              text=True, capture_output=True)
        ref = subprocess.run(["git", "-C", str(self.project_root), "symbolic-ref", "-q", "HEAD"],
                             text=True, capture_output=True)
        return {"git_common_dir": str(Path(common.stdout.strip()).resolve()) if common.returncode == 0 else None,
                "controller_path": str(self.project_root.resolve()),
                "controller_ref": ref.stdout.strip() if ref.returncode == 0 else None,
                "controller_head": head.stdout.strip() if head.returncode == 0 else None}

    def _verify_controller_binding(self, identity: Mapping[str, Any]):
        raw = os.environ.get("YYLO_LEDGER_CONTROLLER_BINDING", "").strip()
        if not raw:
            return
        try:
            expected = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"malformed canonical controller binding: {exc}") from exc
        for key in ("git_common_dir", "controller_path", "controller_ref", "controller_head"):
            if expected.get(key) != identity.get(key):
                raise ValueError(f"canonical controller binding changed at {key}; refusing mutation")

    def _transaction_paths(self, task_path: Path, event: Mapping[str, Any]) -> tuple[Path, bytes]:
        segments = self.ledger.segments(str(event["task_id"]))
        encoded = json.dumps(event, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8") + b"\n"
        target = segments[-1] if segments else self.ledger.directory(str(event["task_id"])) / "000001.ndjson"
        existing = target.read_bytes() if target.exists() else b""
        if existing and len(existing) + len(encoded) > self.ledger.max_segment_bytes:
            target = self.ledger.directory(str(event["task_id"])) / f"{len(segments) + 1:06d}.ndjson"
            existing = b""
        return target, existing + encoded

    def _apply_transaction(self, *, task_id: str, operation: str, task_path: Path,
                           task_bytes: Optional[bytes], event: Mapping[str, Any]) -> Dict[str, Any]:
        return self._apply_batch_transaction(
            operation=operation,
            mutations=[(task_id, task_path, task_bytes, event)],
        )

    def _apply_batch_transaction(self, *, operation: str,
                                 mutations: List[tuple[str, Path, Optional[bytes], Mapping[str, Any]]]
                                 ) -> Dict[str, Any]:
        """Activate task and ledger after-images as one recoverable board transaction."""
        identity = self._git_mutation_identity()
        self._verify_controller_binding(identity)
        entries = []
        expected_revisions = {}
        for task_id, task_path, task_bytes, event in mutations:
            ledger_path, ledger_bytes = self._transaction_paths(task_path, event)
            expected_revisions[task_id] = event.get("before_sha256")
            for path, after in ((task_path, task_bytes), (ledger_path, ledger_bytes)):
                before = self._path_state(path)
                after_state = {"exists": after is not None,
                               "sha256": hashlib.sha256(after).hexdigest() if after is not None else None,
                               "bytes": base64.b64encode(after).decode("ascii") if after is not None else None}
                entries.append({"path": str(path.resolve()), "before": before, "after": after_state})
        created_at = datetime.now(timezone.utc)
        task_ids = [item[0] for item in mutations]
        plan = {"schema_version": "juno_kanban_mutation.v1", "transaction_id": str(uuid.uuid4()),
                "created_at": created_at.isoformat().replace("+00:00", "Z"),
                "expires_at": (created_at + timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
                "command": operation, "task_id": task_ids[0] if len(task_ids) == 1 else None,
                "task_ids": task_ids, "expected_revisions": expected_revisions,
                "identity": identity, "paths": entries}
        canonical = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
        plan["plan_sha256"] = hashlib.sha256(canonical).hexdigest()
        transaction_dir = self.juno_root / "transactions" / plan["transaction_id"]
        transaction_dir.mkdir(parents=True)
        self._fsync_directory(transaction_dir.parent)
        try:
            # Stage complete after-images and validate the task codec before intent publication.
            for index, entry in enumerate(entries):
                if entry["after"]["exists"]:
                    staged = transaction_dir / f"{index:02d}.stage"
                    staged.write_bytes(base64.b64decode(entry["after"]["bytes"]))
                    with staged.open("rb") as handle:
                        os.fsync(handle.fileno())
                    self._mutation_fault(f"after_stage_{index}")
            for task_id, _, task_bytes, _ in mutations:
                if task_bytes is not None:
                    parsed = self.codec.loads(task_bytes.decode("utf-8"))
                    if parsed.get("id") != task_id:
                        raise TaskFormatError("staged task identity mismatch")
            self._mutation_fault("before_intent")
            self._atomic_write(transaction_dir / "plan.json", json.dumps(plan, sort_keys=True) + "\n")
            self._mutation_fault("after_intent")
            # HEAD/ref/path registration is a lease, not merely receipt metadata.
            if self._git_mutation_identity() != identity:
                raise ValueError("canonical controller identity changed before activation")
            self._verify_controller_binding(identity)
            for index, entry in enumerate(entries):
                self._mutation_fault(f"before_activate_{index}")
                path = Path(entry["path"])
                if entry["after"]["exists"]:
                    staged_bytes = (transaction_dir / f"{index:02d}.stage").read_bytes()
                    if hashlib.sha256(staged_bytes).hexdigest() != entry["after"]["sha256"]:
                        raise IOError("staged transaction image checksum mismatch")
                    self._atomic_bytes(path, staged_bytes)
                elif path.exists():
                    path.unlink()
                self._mutation_fault(f"after_activate_{index}")
            self._mutation_fault("before_complete")
            shutil.rmtree(transaction_dir)
            self._fsync_directory(transaction_dir.parent)
            return {key: plan[key] for key in ("schema_version", "transaction_id", "plan_sha256", "identity")}
        except BaseException:
            # Ordinary errors restore byte-for-byte. SIGKILL leaves the durable intent
            # for deterministic recovery by the next writer and read-only doctor.
            if (transaction_dir / "plan.json").exists():
                self._restore_plan(plan, "before")
            shutil.rmtree(transaction_dir, ignore_errors=True)
            raise

    @staticmethod
    def _fsync_directory(path: Path):
        if not path.exists():
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _restore_plan(self, plan: Mapping[str, Any], side: str):
        for entry in plan["paths"]:
            path, state = Path(entry["path"]), entry[side]
            if state["exists"]:
                content = base64.b64decode(state["bytes"])
                if hashlib.sha256(content).hexdigest() != state["sha256"]:
                    raise IOError("transaction image checksum mismatch")
                self._atomic_bytes(path, content)
            elif path.exists():
                path.unlink()

    def _recover_transactions_locked(self):
        root = self.juno_root / "transactions"
        if not root.exists():
            return
        for directory in sorted(path for path in root.iterdir() if path.is_dir()):
            plan_path = directory / "plan.json"
            if not plan_path.is_file():
                # Pre-intent private staging is never canonical and is safe to discard.
                shutil.rmtree(directory)
                continue
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            supplied = plan.pop("plan_sha256", None)
            digest = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            plan["plan_sha256"] = supplied
            if plan.get("schema_version") != "juno_kanban_mutation.v1" or supplied != digest:
                raise IOError(f"malformed abandoned Kanban transaction: {plan_path}")
            if plan.get("identity") != self._git_mutation_identity():
                raise IOError(f"abandoned transaction controller/ref/HEAD identity is stale: {plan_path}")
            allowed_root = self.juno_root.resolve()
            for entry in plan.get("paths", []):
                try:
                    Path(entry["path"]).resolve().relative_to(allowed_root)
                except (KeyError, ValueError):
                    raise IOError(f"transaction path escapes canonical board: {plan_path}")
            states = []
            for entry in plan["paths"]:
                actual = self._path_state(Path(entry["path"]))
                states.append("after" if actual["exists"] == entry["after"]["exists"] and actual["sha256"] == entry["after"]["sha256"] else
                              "before" if actual["exists"] == entry["before"]["exists"] and actual["sha256"] == entry["before"]["sha256"] else "unknown")
            if "unknown" in states:
                raise IOError(f"transaction paths drifted; refusing recovery: {plan_path}")
            # Complete only if every path reached after; otherwise exact rollback.
            if not all(state == "after" for state in states):
                self._restore_plan(plan, "before")
            shutil.rmtree(directory)
        self._fsync_directory(root)

    def _metadata_record(self, task: Mapping[str, Any]) -> Dict[str, Any]:
        record = dict(task)
        record.setdefault("schema_version", 1)
        record.setdefault("fields", {})
        for key in ("created_date", "last_modified"):
            value = record.get(key)
            if isinstance(value, str) and value and not value.endswith("Z") and not value.endswith(("+00:00", "-00:00")):
                try:
                    parsed = datetime.fromisoformat(value)
                    if parsed.tzinfo is None:
                        record[key] = parsed.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
                except ValueError:
                    pass
        return record

    def _write_current(self, record: Mapping[str, Any]) -> Path:
        path = self.task_path(record["id"])
        self._atomic_write(path, self.codec.dumps(record))
        persisted = self._read_path(path)
        expected = self.normalized_hash(record)
        actual = self.normalized_hash(persisted)
        if expected != actual:
            raise IOError(f"post-write verification failed for {record['id']}: {expected} != {actual}")
        return path

    def _refresh_cache(self, record: Mapping[str, Any], path: Path):
        try:
            self.cache.upsert(plain_value(record), path, self.normalized_hash(record),
                              self.project_root, self._config_hash())
        except Exception:
            # Cache is disposable and never allowed to change mutation truth.
            pass

    def _assert_not_frozen(self):
        for name in ("ROLLBACK_FREEZE.json", "CONVERSION_FREEZE.json",
                     "ARCHIVE_PACK_FREEZE.json"):
            marker = self.juno_root / name
            if marker.exists():
                raise ValueError(f"Kanban mutations are frozen: {marker}")

    def _canonical_blocker_status(self, blocker_id: str) -> Optional[str]:
        """Read blocker truth without treating a missing forward reference as resolved."""
        path = self.task_path(blocker_id)
        if path.exists():
            return self._read_path(path).get("status")
        resolved = self.resolve_task(blocker_id)
        return resolved[1].get("status") if resolved else None

    def _unmet_blockers(self, task: Mapping[str, Any],
                        status_overrides: Optional[Mapping[str, str]] = None) -> List[str]:
        overrides = status_overrides or {}
        unmet = []
        for blocker_id in task.get("blocked_by") or []:
            status = overrides.get(blocker_id)
            if status is None:
                status = self._canonical_blocker_status(blocker_id)
            if status not in ("done", "archive"):
                unmet.append(blocker_id)
        return unmet

    def _assert_completion_invariant(self, task: Mapping[str, Any],
                                     status_overrides: Optional[Mapping[str, str]] = None):
        if task.get("status") == "done":
            unmet = self._unmet_blockers(task, status_overrides)
            if unmet:
                raise UnmetBlockersError(str(task["id"]), unmet)

    def _assert_resolved_blocker_not_reopened(self, task_id: str, new_status: str):
        if new_status in ("done", "archive"):
            return
        blocked_done = []
        for row in self.read_all_tasks_canonical():
            if row.get("status") == "done" and task_id in (row.get("blocked_by") or []):
                blocked_done.append(str(row["id"]))
        if blocked_done:
            raise ValueError(
                f"cannot reopen blocker {task_id}; completed dependents would become blocked: "
                + ", ".join(sorted(blocked_done)))

    def write_task(self, task: Task, filepath: Optional[str] = None, *,
                   required_git_roles=()):
        self._assert_not_frozen()
        if filepath and Path(filepath).suffix == ".ndjson":
            raise ValueError("NDJSON is import-only; runtime writes Markdown task files")
        record = self._metadata_record(task.to_dict())
        task_id = record["id"]
        with self._board_lock(), self._lock(task_id):
            path = self.task_path(task_id)
            collision = self._case_collision(task_id)
            if collision:
                if collision == task_id:
                    raise ArchivedTaskError(task_id)
                raise ValueError(f"case-insensitive task ID collision: {task_id} conflicts with {collision}")
            if path.exists():
                raise ValueError(f"task already exists: {task_id}")
            self._enforce_window({}, {"fields": record.get("fields") or {}})
            self._assert_completion_invariant(record)
            context = capture_creation_context(
                controller_root=self.project_root, project_root=self.git_project_root,
                required_roles=required_git_roles, repository_ids=self.git_repository_ids)
            record = attach_creation_context(record, context)
            digest = self.normalized_hash(record)
            event = self.ledger.prepare(task_id, "create", "cli", None, digest,
                                        {}, plain_value(record), True)
            transaction = self._apply_transaction(task_id=task_id, operation="create",
                                                  task_path=path,
                                                  task_bytes=self.codec.dumps(record).encode("utf-8"),
                                                  event=event)
            self._refresh_cache(record, path)
            self.last_receipt = MutationReceipt(task_id, "create", None, digest, event["event_id"],
                                                event["changed_paths"], str(path), transaction)
        return self.last_receipt

    def create_task(self, *, required_git_roles=(), **kwargs) -> Task:
        task = Task(config=self.config.to_dict(), **kwargs)
        self.write_task(task, required_git_roles=required_git_roles)
        return Task.from_dict(plain_value(self._read_path(self.task_path(task.id))),
                              validate=False, config=self.config.to_dict())

    def _reconcile_locked(self, task_id: str, current: Mapping[str, Any]):
        latest = self.ledger.latest(task_id)
        current_hash = self.normalized_hash(current)
        if latest and latest.get("after_sha256") != current_hash:
            # Canonical state wins after direct edits or interrupted ledger append.
            self.ledger.append(task_id, "reconcile", "external-edit", latest.get("after_sha256"),
                               current_hash, {}, plain_value(current), False)
        elif not latest:
            self.ledger.append(task_id, "recovery", "external-edit", None,
                               current_hash, {}, plain_value(current), True)

    @staticmethod
    def _field_shapes(value: Any, prefix: str = "") -> Set[str]:
        shapes: Set[str] = set()
        if isinstance(value, Mapping):
            for key, child in value.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                shapes.add(path)
                shapes.update(TaskStorage._field_shapes(child, path))
        elif isinstance(value, list):
            path = f"{prefix}[]"
            shapes.add(path)
            for child in value:
                shapes.update(TaskStorage._field_shapes(child, path))
        return shapes

    def _enforce_window(self, before: Mapping[str, Any], updates: Mapping[str, Any]):
        window = self.config.to_dict().get("compatibility_window", {})
        if not window.get("active") or "fields" not in updates:
            return
        old_shapes = self._field_shapes(before.get("fields") or {})
        allowed = set(window.get("allowed_field_shapes") or old_shapes)
        novel = self._field_shapes(updates.get("fields") or {}) - allowed
        if novel:
            raise ValueError("seven-day legacy compatibility window forbids new field shapes: " + ", ".join(sorted(novel)))

    def update_task(self, task_id: str, updates: Dict[str, Any], file_pattern: Optional[str] = None,
                    expected_revision: Optional[str] = None, return_receipt: bool = False,
                    operation: str = "update"):
        self._assert_not_frozen()
        with self._board_lock(), self._lock(task_id):
            path = self.task_path(task_id)
            if not path.exists():
                self._raise_if_archived(task_id)
                return False
            current = self._read_path(path)
            before_plain = plain_value(current)
            before_hash = self.normalized_hash(current)
            if expected_revision is not None and expected_revision != before_hash:
                # CAS refusal precedes reconciliation or any other canonical byte.
                raise ConflictError(task_id, expected_revision, before_hash)
            # Refusal precedes reconciliation so failure has zero task/ledger writes.
            prospective_status = updates.get("status", current.get("status"))
            if current.get("status") in ("done", "archive") and prospective_status not in ("done", "archive"):
                self._assert_resolved_blocker_not_reopened(task_id, prospective_status)
            prospective = dict(plain_value(current))
            prospective.update(plain_value(updates))
            self._assert_completion_invariant(prospective)
            self._reconcile_locked(task_id, current)
            self._enforce_window(current, updates)
            task = Task.from_dict(plain_value(current), validate=False, config=self.config.to_dict())
            task.update(config=self.config.to_dict(), **updates)
            after = self._metadata_record(task.to_dict())
            # Preserve untouched ruamel nodes (including nested custom-field
            # comments/order). Replacing every reconstructed model value would
            # erase human YAML annotations during an unrelated status update.
            for key in {*updates, "last_modified"}:
                current[key] = after[key]
            after_hash = self.normalized_hash(current)
            event = self.ledger.prepare(task_id, operation, "cli", before_hash, after_hash,
                                        before_plain, plain_value(current), False)
            transaction = self._apply_transaction(task_id=task_id, operation=operation,
                                                  task_path=path,
                                                  task_bytes=self.codec.dumps(current).encode("utf-8"),
                                                  event=event)
            self._refresh_cache(current, path)
            receipt = MutationReceipt(task_id, operation, before_hash, after_hash,
                                      event["event_id"], event["changed_paths"], str(path), transaction)
            self.last_receipt = receipt
            return receipt

    def exact_replace_record(self, id_or_slug: str, *, path: str, expected: Any,
                             replacement: Any, expected_revision: int,
                             mode: str = "structured", expected_digest: Optional[str] = None,
                             provenance: Optional[RevisionProvenance] = None) -> MutationReceipt:
        """Resolve to ID, lock/re-read, and atomically replace one exact preimage."""
        self._assert_not_frozen()
        with self._board_lock():
            record_id = self.resolve_record_id(id_or_slug)
            with self._lock(record_id):
                task_path = self.task_path(record_id)
                if not task_path.exists():
                    self._raise_if_archived(record_id)
                    raise RecordError("RECORD_NOT_FOUND", f"hot Record {record_id!r} not found")
                current = self._read_path(task_path)
                before_plain = plain_value(current)
                before_hash = self.normalized_hash(current)
                projected = task_record_projection(before_plain)
                if projected["revision"] != expected_revision:
                    raise RecordError(
                        "REVISION_CONFLICT",
                        f"expected revision {expected_revision}, current {projected['revision']}")
                if expected_digest is not None and mode != "payload" and before_hash != expected_digest:
                    raise RecordError("REVISION_CONFLICT", "Record digest is stale")

                if path == "/system_metadata" or path.startswith("/system_metadata/"):
                    raise RecordError("IMMUTABLE_FIELD", "canonical system metadata is immutable")
                match = exact_replace(projected, path=path, expected=expected,
                                      replacement=replacement, mode=mode,
                                      expected_digest=expected_digest if mode == "payload" else None)
                if projected["id"] != record_id or projected["kind"] != "task":
                    raise RecordError("IMMUTABLE_FIELD", "id and kind are immutable")
                if path == "/slug" and replacement != expected:
                    aliases = list(projected.get("aliases") or [])
                    if expected not in aliases:
                        aliases.append(expected)
                    projected["aliases"] = aliases
                projected["revision"] = expected_revision + 1
                projected["last_modified"] = Task._get_timestamp()
                validate_record(projected)

                # Slug uniqueness is namespace+kind scoped.  The board lock
                # serializes this global check with every canonical mutation.
                identities = {projected["slug"], *projected.get("aliases", [])}
                for other in self.read_all_tasks_canonical():
                    if other["id"] == record_id:
                        continue
                    candidate = task_record_projection(other)
                    candidate_scope = (candidate["kind"], candidate["namespace"])
                    projected_scope = (projected["kind"], projected["namespace"])
                    if candidate_scope != projected_scope:
                        continue
                    if identities.intersection({candidate["slug"], *candidate.get("aliases", [])}):
                        raise RecordError("SLUG_CONFLICT", "slug or alias is already retained")

                # Keep the Task codec at schema v1 while persisting the native
                # envelope under explicit fields.  Legacy fields remain byte- and
                # API-compatible and unknown extensions remain lossless.
                persisted = dict(before_plain)
                for key in ("slug", "aliases", "profile", "title", "namespace", "tier",
                            "media_type", "payload", "revision", "relations",
                            "system_metadata", "custom_metadata", "body", "status",
                            "agent_response", "feature_tags", "related_tasks", "blocked_by"):
                    if key in projected:
                        persisted[key] = projected[key]
                persisted["record_schema_version"] = 2
                persisted["last_modified"] = projected["last_modified"]
                Task.from_dict(persisted, validate=True, config=self.config.to_dict())
                after_hash = self.normalized_hash(persisted)
                actor = provenance or RevisionProvenance(actor_type="human")
                event = self.ledger.prepare(
                    record_id, "exact-replace", "record", before_hash, after_hash,
                    before_plain, persisted, False, provenance=actor.to_dict(),
                    exact_match=match, revision=projected["revision"])
                transaction = self._apply_transaction(
                    task_id=record_id, operation="exact-replace", task_path=task_path,
                    task_bytes=self.codec.dumps(persisted).encode("utf-8"), event=event)
                self._refresh_cache(persisted, task_path)
                receipt = MutationReceipt(record_id, "exact-replace", before_hash, after_hash,
                                          event["event_id"], event["changed_paths"],
                                          str(task_path), transaction)
                self.last_receipt = receipt
                return receipt

    def replace_task_record(self, record: Mapping[str, Any], operation: str = "update") -> MutationReceipt:
        """Losslessly replace one record for merge/import tooling under its task lock."""
        task_id = record["id"]
        with self._board_lock(), self._lock(task_id):
            path = self.task_path(task_id)
            if not path.exists():
                # Canonical tier authority precedes replacement-payload validation:
                # a cold Record remains immutable even when its exported payload
                # contains Ledger-owned creation context or other invalid fields.
                self._raise_if_archived(task_id)
            record = self._metadata_record(plain_value(record))
            self._assert_completion_invariant(record)
            if not path.exists():
                if (record.get("system_metadata") or {}).get("creation_context") is not None:
                    raise RecordError("SYSTEM_METADATA_RESERVED", "creation context cannot be supplied by imports")
                context = capture_creation_context(
                    controller_root=self.project_root, project_root=self.git_project_root,
                    repository_ids=self.git_repository_ids)
                record = attach_creation_context(record, context)
                collision = self._case_collision(task_id)
                if collision:
                    if collision == task_id:
                        raise ArchivedTaskError(task_id)
                    raise ValueError(f"case-insensitive task ID collision: {task_id} conflicts with {collision}")
                before_hash = None
                after_hash = self.normalized_hash(record)
                event = self.ledger.prepare(task_id, "create", "cli", None, after_hash,
                                            {}, plain_value(record), True)
                effective_operation = "create"
            else:
                before = self._read_path(path)
                before_creation = (before.get("system_metadata") or {}).get("creation_context")
                after_creation = (record.get("system_metadata") or {}).get("creation_context")
                if after_creation != before_creation:
                    raise RecordError("IMMUTABLE_FIELD", "creation context cannot be changed by imports")
                if before.get("status") in ("done", "archive") and record.get("status") not in ("done", "archive"):
                    self._assert_resolved_blocker_not_reopened(task_id, str(record.get("status")))
                self._reconcile_locked(task_id, before)
                before_hash = self.normalized_hash(before)
                after_hash = self.normalized_hash(record)
                event = self.ledger.prepare(task_id, operation, "cli", before_hash, after_hash,
                                            plain_value(before), plain_value(record), False)
                effective_operation = operation
            transaction = self._apply_transaction(task_id=task_id, operation=effective_operation,
                                                  task_path=path,
                                                  task_bytes=self.codec.dumps(record).encode("utf-8"),
                                                  event=event)
            self._refresh_cache(record, path)
            receipt = MutationReceipt(task_id, effective_operation, before_hash, after_hash,
                                      event["event_id"], event["changed_paths"], str(path), transaction)
            self.last_receipt = receipt
            return receipt

    def finalize_umbrella(self, admission_receipt: Path, evidence_receipt: Path,
                          commit_hash: str, expected_umbrella_id: Optional[str] = None) -> Dict[str, Any]:
        """Atomically complete one receipt-admitted umbrella and its owned blockers.

        The admission receipt is the ownership boundary. Related tasks are never
        considered; every admitted child must also be an exact declared blocker.
        """
        if not commit_hash or not str(commit_hash).strip():
            raise ValueError("umbrella finalization requires a commit hash")
        admission = self._read_verified_receipt(Path(admission_receipt), "umbrella admission")
        evidence = self._read_verified_receipt(Path(evidence_receipt), "umbrella evidence")
        if admission.get("operation") != "umbrella-admission":
            raise ValueError("umbrella admission receipt has the wrong operation")
        umbrella_id = str(admission.get("umbrella_id") or "")
        self._validate_id(umbrella_id)
        if evidence.get("umbrella_id") != umbrella_id or evidence.get("commit_hash") != str(commit_hash):
            raise ValueError("umbrella evidence receipt is not bound to the umbrella and commit")
        if expected_umbrella_id is not None and umbrella_id != expected_umbrella_id:
            raise ValueError("requested umbrella ID does not match admission receipt")
        child_entries = admission.get("children")
        if not isinstance(child_entries, list) or not child_entries:
            raise ValueError("umbrella admission receipt must contain children")
        child_ids = []
        expected = {umbrella_id: admission.get("umbrella_revision")}
        for entry in child_entries:
            if not isinstance(entry, Mapping):
                raise ValueError("umbrella admission child must be an object")
            child_id = str(entry.get("task_id") or "")
            self._validate_id(child_id)
            if child_id == umbrella_id or child_id in child_ids:
                raise ValueError("umbrella admission contains duplicate or self child")
            if entry.get("owner_id") != umbrella_id or entry.get("admitted") is not True:
                raise ValueError(f"umbrella admission does not prove ownership of {child_id}")
            if not isinstance(entry.get("expected_revision"), str):
                raise ValueError(f"umbrella admission lacks revision for {child_id}")
            child_ids.append(child_id)
            expected[child_id] = entry["expected_revision"]
        if not isinstance(expected[umbrella_id], str):
            raise ValueError("umbrella admission lacks umbrella revision")

        marker = {
            "admission_receipt_sha256": admission["content_sha256"],
            "evidence_receipt_sha256": evidence["content_sha256"],
            "commit_hash": str(commit_hash),
            "umbrella_id": umbrella_id,
        }
        task_ids = sorted([umbrella_id, *child_ids])
        self._assert_not_frozen()
        with self._board_lock():
            with ExitStack() as locks:
                for task_id in task_ids:
                    locks.enter_context(self._lock(task_id))
                current = {}
                for task_id in task_ids:
                    path = self.task_path(task_id)
                    if not path.exists():
                        self._raise_if_archived(task_id)
                        raise ValueError(f"umbrella finalization task not found: {task_id}")
                    current[task_id] = self._read_path(path)

                # A successful apply is an idempotency key, not a request for a
                # second ledger transition.
                if all(row.get("status") == "done" and
                       (row.get("fields") or {}).get("umbrella_finalization") == marker
                       for row in current.values()):
                    event_ids = {task_id: self.ledger.latest(task_id)["event_id"] for task_id in task_ids}
                    return self._seal_receipt({
                        "receipt_version": 2, "operation": "umbrella-finalize", "verdict": "pass",
                        "umbrella_id": umbrella_id, "child_ids": sorted(child_ids),
                        "commit_hash": str(commit_hash), "replayed": True,
                        "ledger_event_ids": event_ids,
                        "admission_receipt_sha256": admission["content_sha256"],
                        "evidence_receipt_sha256": evidence["content_sha256"],
                    })

                declared = current[umbrella_id].get("blocked_by") or []
                if sorted(declared) != sorted(child_ids):
                    raise ValueError("admitted children must exactly match the umbrella declared blockers")
                for task_id, row in current.items():
                    actual = self.normalized_hash(row)
                    if actual != expected[task_id]:
                        raise ConflictError(task_id, str(expected[task_id]), actual)

                overrides = {task_id: "done" for task_id in task_ids}
                after_records = {}
                for task_id, row in current.items():
                    candidate = plain_value(row)
                    candidate["status"] = "done"
                    candidate["commit_hash"] = str(commit_hash)
                    candidate["agent_response"] = (
                        f"Umbrella finalization evidence {evidence['content_sha256']}")
                    fields = dict(candidate.get("fields") or {})
                    fields["umbrella_finalization"] = marker
                    candidate["fields"] = fields
                    task = Task.from_dict(candidate, validate=False, config=self.config.to_dict())
                    task.update(config=self.config.to_dict(), status="done",
                                commit_hash=str(commit_hash), agent_response=candidate["agent_response"],
                                fields=fields)
                    after = self._metadata_record(task.to_dict())
                    self._enforce_window(row, {"fields": fields})
                    self._assert_completion_invariant(after, overrides)
                    after_records[task_id] = after

                mutations = []
                events = {}
                for task_id in task_ids:
                    before = current[task_id]
                    after = after_records[task_id]
                    before_hash = self.normalized_hash(before)
                    after_hash = self.normalized_hash(after)
                    event = self.ledger.prepare(
                        task_id, "umbrella-finalize", "admission-receipt",
                        before_hash, after_hash, plain_value(before), plain_value(after), False)
                    events[task_id] = event
                    mutations.append((task_id, self.task_path(task_id),
                                      self.codec.dumps(after).encode("utf-8"), event))
                transaction = self._apply_batch_transaction(
                    operation="umbrella-finalize", mutations=mutations)
                for task_id in task_ids:
                    self._refresh_cache(after_records[task_id], self.task_path(task_id))
                return self._seal_receipt({
                    "receipt_version": 2, "operation": "umbrella-finalize", "verdict": "pass",
                    "umbrella_id": umbrella_id, "child_ids": sorted(child_ids),
                    "commit_hash": str(commit_hash), "replayed": False,
                    "ledger_event_ids": {task_id: events[task_id]["event_id"] for task_id in task_ids},
                    "admission_receipt_sha256": admission["content_sha256"],
                    "evidence_receipt_sha256": evidence["content_sha256"],
                    "transaction": transaction,
                })

    def delete_task(self, task_id: str, file_pattern: Optional[str] = None) -> bool:
        """Remove current state for the legacy library API; CLI lifecycle uses archive."""
        with self._board_lock(), self._lock(task_id):
            path = self.task_path(task_id)
            if not path.exists():
                self._raise_if_archived(task_id)
                return False
            before = self._read_path(path)
            before_hash = self.normalized_hash(before)
            tombstone = {"id": task_id, "deleted": True}
            after_hash = self.normalized_hash(tombstone)
            event = self.ledger.prepare(task_id, "archive", "cli", before_hash, after_hash,
                                        plain_value(before), tombstone, False)
            self._apply_transaction(task_id=task_id, operation="delete", task_path=path,
                                    task_bytes=None, event=event)
            try:
                if self.cache.path.exists():
                    import sqlite3
                    with sqlite3.connect(self.cache.path) as db:
                        db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
                        db.execute("DELETE FROM custom_fields WHERE task_id=?", (task_id,))
            except Exception:
                pass
            return True

    @contextmanager
    def _record_archive_lock(self, id_or_slug: str):
        """Resolve, globally serialize publication, then re-read under ID lock."""
        from .archive import _record_archive_owner
        record_id = self.resolve_record_id(id_or_slug)
        with _record_archive_owner(self.juno_root), self._lock(record_id):
            if self.resolve_record_id(id_or_slug) != record_id:
                raise RecordError("RECORD_IDENTITY_AMBIGUOUS", "Record identity changed while locking")
            resolved = self.resolve_task(record_id)
            if resolved is None:
                raise RecordError("RECORD_NOT_FOUND", "Record disappeared while locking")
            if resolved[0] == "cold":
                raise ArchivedTaskError(record_id)
            yield record_id

    def _record_archive_snapshot(self, record_id: str) -> Dict[str, Any]:
        task = self.find_task(record_id)
        if task is None:
            self._raise_if_archived(record_id)
            raise RecordError("RECORD_NOT_FOUND", "hot Record does not exist")
        segments = self.ledger.segments(record_id)
        if not segments:
            from .archive import ArchiveFormatError
            raise ArchiveFormatError("hot Record history is missing")
        return {"record": task_record_projection(task), "history": self.ledger.read(record_id),
                "paths": [self.task_path(record_id), *segments], "owned_objects": []}

    def _record_archive_verify_objects(self, objects: List[Mapping[str, Any]]) -> None:
        if objects:
            from .archive import ArchiveFormatError
            raise ArchiveFormatError("Task Record cannot own content objects")

    def _record_archive_refresh(self) -> None:
        self._archive_id_inventory = None
        self.rebuild_cache()

    def archive_record(self, id_or_slug: str, *, expected_revision: int,
                       receipt_path: Optional[Path] = None,
                       provenance: Optional[Mapping[str, Any]] = None,
                       fault=None) -> Dict[str, Any]:
        from .archive import archive_record
        return archive_record(self, id_or_slug, expected_revision=expected_revision,
                              receipt_path=receipt_path, provenance=provenance, fault=fault)

    def _raise_if_archived(self, task_id: str):
        entry = self._archive_entry_after_canonical_miss_check(task_id)
        if entry and entry["task_id"] == task_id:
            raise ArchivedTaskError(task_id)

    def history(self, task_id: str, include_content: bool = False, limit: Optional[int] = None):
        resolved = self.resolve_task(task_id)
        if resolved and resolved[0] == "cold":
            events = list(resolved[2]["ledger"])
        else:
            events = self.ledger.read(task_id)
        if limit is not None:
            events = events[-limit:]
        if include_content:
            return events
        summarized = []
        for event in events:
            summarized.append({key: event.get(key) for key in (
                "event_id", "task_id", "timestamp", "operation", "source", "before_sha256",
                "after_sha256", "previous_event_sha256", "event_sha256", "changed_paths",
                "record_id", "expected_revision", "provenance")})
        return summarized

    def reconcile(self, check: bool = False):
        changed = []
        for filepath in self.get_files():
            path = Path(filepath)
            if check:
                record = self._read_path(path)
                latest = self.ledger.latest(record["id"])
                digest = self.normalized_hash(record)
                if not latest or latest.get("after_sha256") != digest:
                    changed.append(record["id"])
                continue

            # The comparison and append are one task-scoped critical section.
            # Otherwise two concurrent explicit reconciliations can both see
            # the old ledger tip and append duplicate/forked recovery events.
            unlocked_record = self._read_path(path)
            task_id = unlocked_record["id"]
            with self._lock(task_id):
                record = self._read_path(path)
                latest = self.ledger.latest(task_id)
                digest = self.normalized_hash(record)
                if not latest or latest.get("after_sha256") != digest:
                    changed.append(task_id)
                    self._reconcile_locked(task_id, record)
        return changed

    def doctor(self):
        failures = []
        task_ids = set()
        hot_markdown = list(self.tasks_root.glob("*/*.md"))
        legacy_remnants = sorted({*self.tasks_root.glob("*.ndjson"), *self.juno_root.glob("*.ndjson")})
        if hot_markdown and legacy_remnants:
            for remnant in legacy_remnants:
                failures.append({
                    "path": str(remnant),
                    "diagnosis": "mixed_v1_v2_storage",
                    "error": "mixed_v1_v2_storage: legacy NDJSON is ignored by the active V2 runtime; "
                             "preserve it externally or remove it only through an explicitly authorized cleanup",
                })
        for path in sorted(self.tasks_root.glob("*/*.md")):
            try:
                record = self._read_path(path)
                task_ids.add(record["id"])
                segments = self.ledger.segments(record["id"])
                for segment in segments[:-1]:
                    if segment.stat().st_size > self.ledger.max_segment_bytes:
                        failures.append({"path": str(segment), "error": "closed ledger segment exceeds configured limit"})
                events = self.ledger.read(record["id"])
                previous_after = None
                for index, event in enumerate(events):
                    if event.get("task_id") != record["id"]:
                        failures.append({"path": str(segments[0] if segments else path), "error": "ledger event task ID mismatch"})
                    if index == 0 and (event.get("operation") != "create" or "snapshot" not in event):
                        failures.append({"path": str(segments[0] if segments else path), "error": "ledger first event is not a creation snapshot"})
                    if index and event.get("before_sha256") != previous_after:
                        failures.append({"path": str(segments[0] if segments else path), "error": "ledger state hash discontinuity"})
                    previous_after = event.get("after_sha256")
                if not events or events[-1].get("after_sha256") != self.normalized_hash(record):
                    failures.append({"path": str(path), "error": "ledger/current-state mismatch"})
            except Exception as exc:
                failures.append({"path": str(path), "error": str(exc)})

        # Orphan ledgers are integrity failures rather than invisible history.
        ledger_root = self.juno_root / "ledger"
        if ledger_root.exists():
            for directory in sorted(path for path in ledger_root.glob("*/*") if path.is_dir()):
                if directory.parent.name != directory.name[:2].lower():
                    failures.append({"path": str(directory), "error": "ledger task ID/path mismatch"})
                unexpected = [path for path in directory.iterdir()
                              if not (path.is_file() and len(path.name) == 13 and path.name[:6].isdigit()
                                      and path.name.endswith('.ndjson'))]
                for item in unexpected:
                    failures.append({"path": str(item), "error": "unexpected ledger path"})
                if directory.name not in task_ids:
                    failures.append({"path": str(directory), "error": "ledger has no canonical current task"})
        try:
            from .archive import scan_archive_index
            cold, _ = scan_archive_index(self.juno_root)
            hot_folded = {task_id.casefold(): task_id for task_id in task_ids}
            for item in cold:
                if item["id_fold"] in hot_folded:
                    failures.append({"path": item["pack"],
                                     "error": "task exists in both hot and cold tiers: %s" % item["task_id"]})
        except Exception as exc:
            failures.append({"path": str(self.juno_root / "archive"), "error": str(exc)})

        # Diagnostics are intentionally read-only: abandoned transaction intent
        # and pre-contract staging are named for an owner-directed retry/recovery,
        # never merged, deleted, or silently blessed by doctor.
        invocation_raw = os.environ.get("YYLO_LEDGER_INVOCATION_ROOT", "").strip()
        if invocation_raw:
            invocation = Path(invocation_raw).expanduser().resolve()
            if invocation != self.project_root.resolve():
                stale = sorted((invocation / ".juno_task" / "tasks").glob("*/*.md"))
                if stale:
                    failures.append({
                        "path": str(invocation / ".juno_task" / "tasks"),
                        "diagnosis": "legacy_stale_local_board",
                        "canonical_authority": str(self.project_root.resolve()),
                        "error": f"noncanonical checkout contains {len(stale)} task record(s); read-only diagnosis only, no automatic merge or deletion",
                    })

        transactions = self.juno_root / "transactions"
        if transactions.exists():
            for directory in sorted(path for path in transactions.iterdir() if path.is_dir()):
                plan = directory / "plan.json"
                if plan.is_file():
                    diagnosis = "abandoned_canonical_mutation"
                    error = "abandoned canonical mutation intent; next canonical writer will recover deterministically"
                    try:
                        payload = json.loads(plan.read_text(encoding="utf-8"))
                        supplied = payload.pop("plan_sha256", None)
                        actual = hashlib.sha256(json.dumps(
                            payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                        if payload.get("schema_version") != "juno_kanban_mutation.v1" or supplied != actual:
                            diagnosis = "malformed_mutation_intent"
                            error = "malformed mutation intent requires owner inspection; automatic recovery refuses"
                    except (OSError, json.JSONDecodeError):
                        diagnosis = "malformed_mutation_intent"
                        error = "unreadable mutation intent requires owner inspection; automatic recovery refuses"
                else:
                    diagnosis = "legacy_stale_board_artifact"
                    error = "private or pre-contract stale mutation artifact; preserve for owner diagnosis"
                failures.append({"path": str(directory), "diagnosis": diagnosis, "error": error})
        return failures

    def rebuild_cache(self, *, _cache_lock_held: bool = False):
        """Rebuild disposable query state under its dedicated refresh lease."""
        if not _cache_lock_held:
            with self._cache_refresh_lock():
                # Class dispatch deliberately bypasses instance-level test/audit
                # wrappers on the internal re-entry while preserving one public
                # rebuild call for observability.
                return TaskStorage.rebuild_cache(self, _cache_lock_held=True)
        # A rebuild is also the synchronization boundary after archive activation
        # or repair; the next exact operation must derive the new sealed inventory.
        self._archive_id_inventory = None
        records = []
        hot_folds: Dict[str, str] = {}
        for filepath in self.get_files():
            path = Path(filepath)
            record = self._read_path(path)
            folded = record["id"].casefold()
            if folded in hot_folds:
                raise ValueError(f"case-insensitive hot task ID collision: {record['id']}")
            hot_folds[folded] = record["id"]
            records.append((plain_value(record), path, self.normalized_hash(record)))
        from .archive import ArchiveFormatError, scan_archive_index
        archive_records, inventory = scan_archive_index(self.juno_root)
        for item in archive_records:
            if item["id_fold"] in hot_folds:
                raise ArchiveFormatError(
                    f"task exists in both hot and cold tiers: {item['task_id']} conflicts with {hot_folds[item['id_fold']]}")
        self.cache.rebuild(records, self.project_root, self._config_hash(), archive_records, inventory)
        return len(records)

    def _archive_tree_dirty(self) -> bool:
        if self._git_head() is None:
            return (self.juno_root / "archive").exists()
        relative = str((self.juno_root / "archive").relative_to(self.project_root))
        tracked = subprocess.run(
            ["git", "-C", str(self.project_root), "status", "--porcelain=v1", "-uno", "--", relative],
            capture_output=True).stdout
        untracked = subprocess.run(
            ["git", "-C", str(self.project_root), "ls-files", "--others", "--exclude-standard", "--", relative],
            capture_output=True).stdout
        return bool(tracked or untracked)

    def _ensure_query_cache(self):
        # Freshness validation can incrementally update metadata/tasks, so it is
        # a cache writer even though the public command is read-only. Coordinate
        # only that short refresh phase; cache.query() remains outside this lease.
        with self._cache_refresh_lock():
            fresh = self.cache.ensure_fresh(
                self.tasks_root, self.project_root, self._config_hash(), self._read_path, self.normalized_hash
            )
            archive_fresh = (
                self.cache.archive_identity() == self.cache.archive_tree_identity(self.project_root)
                and not self._archive_tree_dirty()
            )
            if not fresh or not archive_fresh:
                TaskStorage.rebuild_cache(self, _cache_lock_held=True)

    def query_collection(self, **kwargs):
        """Freshness-check then execute a cache-indexed collection plan."""
        self._ensure_query_cache()
        return self.cache.query(**kwargs)

    def dependency_info_best_effort(self, task_id: str):
        """Return bounded derived enrichment without rebuilding canonical truth."""
        if not self.cache.path.exists():
            return None, "derived cache is missing"
        try:
            return self.cache.dependency_info(task_id), None
        except Exception as exc:
            return None, f"derived cache is unavailable or busy: {exc}"

    def dependency_info(self, task_id: str):
        self._ensure_query_cache()
        result = self.cache.dependency_info(task_id)
        entry = self.cache.archive_entry(task_id)
        if not entry or entry["task_id"] != task_id:
            return result
        from .archive import read_indexed_archive_record
        task = read_indexed_archive_record(entry)["task"]
        blockers = []
        for blocker_id in task.get("blocked_by") or []:
            hot_path = self.task_path(blocker_id)
            if hot_path.exists():
                status = self._read_path(hot_path).get("status")
            else:
                cold = self.cache.archive_entry(blocker_id)
                status = cold.get("status") if cold and cold["task_id"] == blocker_id else None
            blockers.append((blocker_id, status))
        result["blockers"] = sorted(blockers)
        return result

    def archive_search(self, **kwargs):
        """Read only the requested cold page after cache metadata filtering."""
        self._ensure_query_cache()
        result = self.cache.archive_query(**kwargs)
        from .archive import read_indexed_archive_record
        result["tasks"] = [read_indexed_archive_record(entry)["task"]
                           for entry in result.pop("entries")]
        return result

    def dependency_would_cycle(self, task_id: str, blocker_id: str) -> bool:
        self._ensure_query_cache()
        return self.cache.dependency_would_cycle(task_id, blocker_id)

    def normalize_field_ranges(self, field_before=None, field_after=None):
        """Validate typed range filters and normalize datetime instants for SQL ordering."""
        definitions = self.config.to_dict().get("custom_fields", {})
        normalized = []
        for supplied in (field_before or {}, field_after or {}):
            values = dict(supplied)
            for key, boundary in values.items():
                kind = definitions.get(key, {}).get("type")
                if kind not in ("date", "datetime"):
                    raise ValueError(f"typed range filter requires declared date/datetime field: {key}")
                if kind == "datetime":
                    parsed = datetime.fromisoformat(str(boundary).replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        raise ValueError(f"datetime range boundary must be timezone-aware: {key}")
                    values[key] = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                else:
                    date.fromisoformat(str(boundary))
            normalized.append(values)
        return normalized[0], normalized[1]

    def query_fields(self, field_equals=None, field_exists=None, field_before=None, field_after=None,
                     overdue=False, today: Optional[date] = None):
        field_before, field_after = self.normalize_field_ranges(field_before, field_after)
        filters = {"field_equals": field_equals or {}, "field_exists": field_exists or [],
                   "field_before": field_before, "field_after": field_after,
                   "overdue": str(today or datetime.now(timezone.utc).date()) if overdue else None}
        return self.query_collection(filters=filters, limit=None, sort_order="asc")["tasks"]

    def _git_head(self) -> Optional[str]:
        try:
            return subprocess.check_output(
                ["git", "-C", str(self.project_root), "rev-parse", "HEAD"],
                text=True, stderr=subprocess.DEVNULL,
            ).strip()
        except (OSError, subprocess.CalledProcessError):
            return None

    @staticmethod
    def _implementation_identity() -> Dict[str, Any]:
        # Identify the package that owns the module actually executing this code.
        # Distribution metadata can describe a different, older installation when
        # tests or local CLI runs intentionally execute a source checkout.
        from . import __version__

        module = Path(__file__).resolve()
        return {"package_version": __version__, "storage_module": str(module),
                "storage_module_sha256": hashlib.sha256(module.read_bytes()).hexdigest()}

    @staticmethod
    def _seal_receipt(payload: Mapping[str, Any]) -> Dict[str, Any]:
        sealed = dict(payload)
        sealed.pop("content_sha256", None)
        canonical = json.dumps(sealed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        sealed["content_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
        return sealed

    @staticmethod
    def _wheel_identity(path: Path) -> Dict[str, str]:
        import zipfile
        wheel_path = Path(path).resolve()
        if not wheel_path.is_file() or wheel_path.suffix != ".whl":
            raise ValueError("exact package must be a wheel file")
        with zipfile.ZipFile(wheel_path) as wheel:
            names = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
            if len(names) != 1:
                raise ValueError("wheel has no unique package metadata")
            metadata = wheel.read(names[0]).decode("utf-8")
        package = next((line.split(":", 1)[1].strip() for line in metadata.splitlines()
                        if line.startswith("Name:")), None)
        version = next((line.split(":", 1)[1].strip() for line in metadata.splitlines()
                        if line.startswith("Version:")), None)
        if package not in ("juno-kanban", "juno_kanban") or not version:
            raise ValueError("wheel is not an identified juno-kanban package")
        return {"path": str(wheel_path), "sha256": hashlib.sha256(wheel_path.read_bytes()).hexdigest(),
                "name": package, "version": version}

    @staticmethod
    def _read_verified_receipt(path: Optional[Path], name: str) -> Dict[str, Any]:
        """Read sealed JSON or JSON bound by the benchmark-style SHA-256 sidecar."""
        if path is None:
            raise ValueError(f"missing {name} receipt")
        receipt_path = Path(path)
        raw = receipt_path.read_bytes()
        payload = json.loads(raw)
        file_hash = hashlib.sha256(raw).hexdigest()
        supplied = payload.pop("content_sha256", None)
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        expected = hashlib.sha256(canonical.encode()).hexdigest()
        sealed = isinstance(supplied, str) and hmac.compare_digest(supplied, expected)
        sidecar = Path(str(receipt_path) + ".sha256")
        sidecar_ok = False
        if sidecar.is_file():
            digest = sidecar.read_text(encoding="utf-8").split()[0]
            sidecar_ok = len(digest) == 64 and hmac.compare_digest(digest, file_hash)
        payload["content_sha256"] = supplied or file_hash
        if payload.get("receipt_version", 0) < 2 or not (sealed or sidecar_ok):
            raise ValueError(f"{name} receipt is not content-addressed machine evidence")
        if payload.get("verdict") != "pass" or not payload.get("operation"):
            raise ValueError(f"{name} receipt is not a machine pass")
        now = datetime.now(timezone.utc)
        for key in ("created_at", "started_at", "finished_at", "observed_end"):
            if payload.get(key):
                observed = datetime.fromisoformat(str(payload[key]).replace("Z", "+00:00"))
                if observed.tzinfo is None or observed > now + timedelta(minutes=5):
                    raise ValueError(f"{name} receipt contains an invalid or future {key}")
        payload["receipt_file_sha256"] = file_hash
        return payload

    @staticmethod
    def _external_path(path: Path, project_root: Path, label: str) -> Path:
        resolved = Path(path).resolve()
        try:
            resolved.relative_to(project_root.resolve())
        except ValueError:
            return resolved
        raise ValueError(f"{label} must be outside the repository")

    def _conversion_fault(self, point: str):
        """Named no-op hook used by exhaustive activation fault tests."""
        return None

    def _rollback_fault(self, point: str):
        """Named no-op hook used by exhaustive rollback fault tests."""
        return None

    def _verify_cutover_assets(self, source: Path, source_hash: str, *,
                               pre_cutover_tag: Optional[str], backup_path: Optional[Path],
                               legacy_package: Optional[Path], new_package_version: Optional[str],
                               benchmark_receipt: Optional[Path]) -> Dict[str, Any]:
        head = self._git_head()
        if head is None:
            # Disposable non-Git fixtures still exercise staging; they cannot claim production readiness.
            return {"production_ready": False, "reason": "outside-git test fixture"}
        if not all((pre_cutover_tag, backup_path, legacy_package, new_package_version, benchmark_receipt)):
            raise ValueError("conversion activation requires tag, external backup, exact legacy wheel, new package version, and benchmark")
        legacy_identity = self._wheel_identity(Path(legacy_package))
        legacy_package = Path(legacy_identity["path"])
        implementation = self._implementation_identity()
        if implementation["package_version"] != new_package_version:
            raise ValueError("new package version does not match the executing implementation")
        tag_head = subprocess.check_output(
            ["git", "-C", str(self.project_root), "rev-list", "-n", "1", str(pre_cutover_tag)],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        if tag_head != head:
            raise ValueError(f"pre-cutover tag must resolve to current HEAD {head}, got {tag_head}")
        freeze_path = self.juno_root / "CONVERSION_FREEZE.json"
        freeze = self._read_verified_receipt(freeze_path, "mutation-freeze")
        if (freeze.get("operation") != "conversion-freeze" or freeze.get("source_sha256") != source_hash
                or freeze.get("source_commit") != head or freeze.get("config_sha256") != self._config_hash()):
            raise ValueError("machine conversion freeze does not bind source/commit/config")
        benchmark = self._read_verified_receipt(benchmark_receipt, "benchmark")
        gates = benchmark.get("gates")
        identity = benchmark.get("identity") or {}
        benchmark_driver = Path(__file__).resolve().with_name("benchmark_git_native.py")
        benchmark_driver_sha256 = hashlib.sha256(benchmark_driver.read_bytes()).hexdigest() if benchmark_driver.is_file() else None
        if (benchmark.get("operation") != "installed-cli-benchmark" or benchmark.get("tasks") != 140000
                or not isinstance(gates, dict) or not gates or not all(gates.values())
                or identity.get("package_version") != new_package_version
                or not identity.get("cli_sha256") or identity.get("benchmark_sha256") != benchmark_driver_sha256
                or set((identity.get("measured_commands") or {})) != {"get", "list", "search", "mutation", "cold_rebuild"}):
            raise ValueError("benchmark receipt is not bound 140k installed-CLI evidence")
        backup_path = self._external_path(Path(backup_path), self.project_root, "pre-cutover backup")
        backup_path.mkdir(parents=True, exist_ok=True)
        backup_file = backup_path / f"juno-kanban-precutover-{head[:12]}.tar.gz"
        manifest_file = backup_path / f"juno-kanban-precutover-{head[:12]}.manifest.json"
        import tarfile
        with tarfile.open(backup_file, "w:gz") as archive:
            archive.add(source, arcname=source.name)
            config_path = self.tasks_root / "config.json"
            if config_path.exists():
                archive.add(config_path, arcname="config.json")
        # Rehearse this exact generated backup, not merely an earlier example archive.
        with tempfile.TemporaryDirectory(prefix="juno-restore-rehearsal-") as temporary:
            with tarfile.open(backup_file, "r:gz") as archive:
                for member in archive.getmembers():
                    if Path(member.name).is_absolute() or ".." in Path(member.name).parts:
                        raise ValueError("unsafe path in generated pre-cutover backup")
                    if member.isfile():
                        restored_path = Path(temporary) / member.name
                        restored_path.parent.mkdir(parents=True, exist_ok=True)
                        extracted = archive.extractfile(member)
                        if extracted is None:
                            raise ValueError("generated backup member cannot be restored")
                        restored_path.write_bytes(extracted.read())
            restored_source = Path(temporary) / source.name
            restored_hash = hashlib.sha256(restored_source.read_bytes()).hexdigest()
            if restored_hash != source_hash:
                raise ValueError("generated pre-cutover backup failed restore rehearsal")
        retained_wheel = backup_path / f"legacy-{legacy_identity['version']}-{legacy_identity['sha256'][:12]}.whl"
        shutil.copy2(legacy_package, retained_wheel)
        if hashlib.sha256(retained_wheel.read_bytes()).hexdigest() != legacy_identity["sha256"]:
            raise ValueError("retained legacy package failed checksum verification")
        legacy_identity = dict(legacy_identity, retained_path=str(retained_wheel))
        manifest = {
            "receipt_version": 2, "operation": "pre-cutover-assets", "verdict": "pass",
            "backup": str(backup_file), "backup_sha256": hashlib.sha256(backup_file.read_bytes()).hexdigest(),
            "source_sha256": source_hash, "tag": pre_cutover_tag, "commit": head,
            "legacy_package": legacy_identity,
            "new_package_version": new_package_version,
            "restore_rehearsal": {"operation": "generated-backup-restore-rehearsal",
                                  "generated_backup_restored": True, "restored_source_sha256": restored_hash},
            "benchmark": benchmark, "freeze": freeze,
        }
        manifest = self._seal_receipt(manifest)
        self._atomic_write(manifest_file, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        manifest["manifest"] = str(manifest_file)
        manifest["manifest_sha256"] = hashlib.sha256(manifest_file.read_bytes()).hexdigest()
        manifest["production_ready"] = True
        return manifest

    def convert_legacy(self, source: Path, dry_run: bool = False, *,
                       pre_cutover_tag: Optional[str] = None, backup_path: Optional[Path] = None,
                       legacy_package: Optional[Path] = None, new_package_version: Optional[str] = None,
                       benchmark_receipt: Optional[Path] = None, report_path: Optional[Path] = None):
        source = Path(source).resolve()
        try:
            dirty = subprocess.check_output(
                ["git", "-C", str(self.project_root), "status", "--porcelain", "--",
                 str(self.tasks_root), str(self.juno_root / "ledger"),
                 str(self.juno_root / "archive"), str(source)],
                text=True, stderr=subprocess.DEVNULL,
            ).strip()
            if dirty:
                raise ValueError("conversion refused: uncommitted task-storage changes")
        except subprocess.CalledProcessError:
            pass
        rows = []
        seen, folded = set(), set()
        with source.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                row = json.loads(line)
                task_id = row.get("id")
                self._validate_id(task_id)
                if task_id in seen:
                    raise ValueError(f"duplicate task ID at line {line_number}: {task_id}")
                if task_id.casefold() in folded:
                    raise ValueError(f"case-insensitive task ID collision: {task_id}")
                seen.add(task_id); folded.add(task_id.casefold()); rows.append(row)

        def legacy_semantic(value):
            value = plain_value(value)
            value.pop("schema_version", None)
            if value.get("fields") == {}:
                value.pop("fields", None)
            # Legacy Task records predate dependency/link fields and valid older
            # rows may omit them; v2 materializes the same empty state as null.
            for field in ("blocked_by", "related_tasks"):
                value.setdefault(field, None)
            for key in ("created_date", "last_modified"):
                raw = value.get(key)
                if raw:
                    parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    value[key] = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            return value

        source_hashes = {}
        active_statuses = {"backlog", "todo", "in_progress"}
        terminal_statuses = {"done", "archive"}
        active_rows = []
        terminal_rows = []
        for row in rows:
            staged = self._metadata_record(Task.from_dict(row, validate=False, config=self.config.to_dict()).to_dict())
            decoded = self.codec.loads(self.codec.dumps(staged))
            if normalized_bytes(legacy_semantic(dict(row))) != normalized_bytes(legacy_semantic(dict(decoded))):
                raise ValueError(f"conversion dry-run semantic mismatch: {row['id']}")
            source_hashes[row["id"]] = hashlib.sha256(normalized_bytes(legacy_semantic(dict(row)))).hexdigest()
            if row.get("status") in active_statuses:
                active_rows.append(row)
            elif row.get("status") in terminal_statuses:
                terminal_rows.append(row)
            else:
                raise ValueError(f"conversion cannot tier unknown status for {row['id']}: {row.get('status')}")

        converted_by_id = {str(row["id"]): row for row in rows}
        for row in rows:
            if row.get("status") != "done":
                continue
            unmet = [blocker_id for blocker_id in (row.get("blocked_by") or [])
                     if converted_by_id.get(blocker_id, {}).get("status") not in ("done", "archive")]
            if unmet:
                raise UnmetBlockersError(str(row["id"]), unmet)

        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        freeze_path = self.juno_root / "CONVERSION_FREEZE.json"
        if not dry_run:
            if freeze_path.exists():
                raise ValueError("conversion freeze already exists")
            freeze = self._seal_receipt({"receipt_version": 2, "operation": "conversion-freeze",
                "verdict": "pass", "source_sha256": source_hash, "source_commit": self._git_head(),
                "config_sha256": self._config_hash(), "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")})
            self._atomic_write(freeze_path, json.dumps(freeze, indent=2, sort_keys=True) + "\n")
        window_start = datetime.now(timezone.utc)
        window_id = str(uuid.uuid4())
        allowed_shapes = sorted(set().union(*(self._field_shapes(row.get("fields") or {}) for row in rows)))
        window = {"id": window_id, "active": True,
                  "start": window_start.isoformat().replace("+00:00", "Z"),
                  "end": (window_start + timedelta(days=7)).isoformat().replace("+00:00", "Z"),
                  "lifted_by_acceptance": False, "conversion_source_sha256": source_hash,
                  "allowed_field_shapes": allowed_shapes}
        report = {"receipt_version": 2, "operation": "convert", "source": str(source),
                  "validated": len(rows), "dry_run": dry_run, "semantic_hashes_match": True,
                  "source_sha256": source_hash, "source_task_hashes": source_hashes,
                  "partition": {"hot_statuses": sorted(active_statuses),
                                "cold_statuses": sorted(terminal_statuses),
                                "hot_tasks": len(active_rows), "cold_tasks": len(terminal_rows),
                                "source_tasks": len(rows)},
                  "compatibility_window": window, "activation_steps": []}
        if dry_run:
            report.update({"verdict": "pass", "implementation_commit": self._git_head(),
                           "implementation": self._implementation_identity(),
                           "config_sha256": self._config_hash()})
            report = self._seal_receipt(report)
            if report_path is not None:
                self._atomic_write(Path(report_path), json.dumps(report, indent=2, sort_keys=True) + "\n")
            return report
        try:
            assets = self._verify_cutover_assets(
                source, source_hash, pre_cutover_tag=pre_cutover_tag, backup_path=backup_path,
                legacy_package=legacy_package, new_package_version=new_package_version,
                benchmark_receipt=benchmark_receipt,
            )
            report["pre_cutover_assets"] = assets
            if any(self.tasks_root.glob("*/*.md")):
                raise ValueError("conversion target already contains Markdown tasks")
        except Exception:
            # Asset and target checks run after the machine freeze is persisted,
            # but before activation's broader rollback scope begins. A refused
            # preflight must not strand a board-wide mutation freeze.
            freeze_path.unlink(missing_ok=True)
            raise

        stage_root = self.project_root / f".juno-conversion-{uuid.uuid4().hex}"
        stage_juno = stage_root / ".juno_task"
        stage_tasks = stage_juno / "tasks"
        stage_tasks.mkdir(parents=True)
        stage_config_data = self.config.to_dict()
        stage_config_data["storage"] = dict(stage_config_data["storage"])
        stage_config_data["storage"]["base_path"] = str(stage_tasks)
        stage_config_data["compatibility_window"] = window
        stage_config_path = stage_tasks / "config.json"
        stage_config_path.write_text(json.dumps(stage_config_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        backup_tasks = stage_root / "original-tasks"
        backup_ledger = stage_root / "original-ledger"
        backup_archive = stage_root / "original-archive"
        source_backup = stage_root / source.name
        try:
            source.relative_to(self.tasks_root.resolve())
            source_in_tasks = True
        except ValueError:
            source_in_tasks = False
        activated_tasks = activated_ledger = activated_archive = source_removed = False
        try:
            stage_storage = TaskStorage(Config(str(stage_config_path)))
            (stage_juno / "ledger").mkdir(parents=True, exist_ok=True)
            (stage_juno / "archive").mkdir(parents=True, exist_ok=True)
            for row in active_rows:
                record = stage_storage._metadata_record(
                    Task.from_dict(row, validate=False, config=stage_storage.config.to_dict()).to_dict())
                stage_storage._write_conversion_task(record)
            from . import __version__
            from .archive import make_envelope, write_archive_packs
            cold_records = []
            for row in terminal_rows:
                record = stage_storage._metadata_record(
                    Task.from_dict(row, validate=False, config=stage_storage.config.to_dict()).to_dict())
                digest = stage_storage.normalized_hash(record)
                event = stage_storage.ledger.prepare(
                    str(record["id"]), "create", "conversion", None, digest,
                    {}, plain_value(record), True)
                cold_records.append((record, event))
            archived_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            envelopes = [make_envelope(
                record, [event], archived_at, str(event["timestamp"]),
                "sha256:" + source_hash) for record, event in cold_records]
            canonical_config = plain_value(stage_config_data)
            canonical_config["storage"] = dict(canonical_config["storage"])
            canonical_config["storage"]["base_path"] = self.base_path
            canonical_config_sha256 = hashlib.sha256(json.dumps(
                canonical_config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            artifacts = write_archive_packs(
                stage_juno / "archive", envelopes,
                self._git_head() or source_hash, canonical_config_sha256,
                __version__, archived_at)
            report["cold_archive_packs"] = [
                {"pack": str(item.pack.relative_to(stage_juno)),
                 "pack_sha256": item.pack_sha256,
                 "records": item.record_count, "bytes": item.size_bytes}
                for item in artifacts]
            target_hashes = {record["id"]: hashlib.sha256(normalized_bytes(legacy_semantic(dict(record)))).hexdigest()
                             for record in stage_storage.read_all_tasks_complete()}
            report["target_task_hashes"] = target_hashes
            report["semantic_hashes_match"] = source_hashes == target_hashes
            report["staging_doctor"] = stage_storage.doctor()
            if not report["semantic_hashes_match"] or report["staging_doctor"]:
                raise ValueError("conversion staging validation failed")
            rebuilt = stage_storage.rebuild_cache()
            exact_samples = [rows[0]["id"], rows[-1]["id"]] if rows else []
            report["staging_benchmark"] = {
                "complete_id_set_verified": set(target_hashes) == set(source_hashes),
                "exact_samples_verified": all(stage_storage.find_task_exact(i) for i in exact_samples),
                "cache_rebuild_tasks": rebuilt}
            self._conversion_fault("after_staging_validation")

            # Preserve an in-tree legacy source before the tasks directory is
            # atomically moved; rollback restores backup_tasks, while success
            # disposes that complete legacy tree only after verification.
            if source_in_tasks:
                shutil.copy2(source, source_backup)
            os.replace(self.tasks_root, backup_tasks)
            report["activation_steps"].append("original_tasks_backed_up")
            self._conversion_fault("after_tasks_backup")
            old_ledger = self.juno_root / "ledger"
            old_archive = self.juno_root / "archive"
            if old_ledger.exists():
                os.replace(old_ledger, backup_ledger)
            if old_archive.exists():
                os.replace(old_archive, backup_archive)
            os.replace(stage_tasks, self.tasks_root); activated_tasks = True
            report["activation_steps"].append("tasks_activated")
            self._conversion_fault("after_tasks_activation")
            os.replace(stage_juno / "ledger", old_ledger); activated_ledger = True
            report["activation_steps"].append("ledger_activated")
            self._conversion_fault("after_ledger_activation")
            os.replace(stage_juno / "archive", old_archive); activated_archive = True
            report["activation_steps"].append("archive_activated")
            self._conversion_fault("after_archive_activation")

            self.config = Config(str(self.tasks_root / "config.json"))
            self.config.config["storage"]["base_path"] = self.base_path
            self.config.config["compatibility_window"] = window
            self.config.save()
            report["activation_steps"].append("config_saved")
            self._conversion_fault("after_config_save")
            self.rebuild_cache()
            report["activation_steps"].append("cache_rebuilt")
            self._conversion_fault("after_cache_rebuild")

            if source_in_tasks:
                report["source_disposition"] = "removed_from_active_storage"
            else:
                try:
                    source.relative_to(self.juno_root.resolve())
                    shutil.copy2(source, source_backup)
                    source.unlink(); source_removed = True
                    report["source_disposition"] = "removed_from_active_storage"
                except ValueError:
                    report["source_disposition"] = "external_import_retained"
            report["activation_steps"].append("legacy_source_disposed")
            self._conversion_fault("after_source_disposition")
            report["post_activation_doctor"] = self.doctor()
            if report["post_activation_doctor"]:
                raise ValueError("post-activation doctor failed")
            report["task_tree_sha256"] = hashlib.sha256(
                "".join(sorted(self.normalized_hash(t) for t in self.read_all_tasks_complete())).encode()).hexdigest()
            report["cache_revision"] = self.cache.revision()
            report["status"] = "activated"
            report["verdict"] = "pass"
            report["implementation_commit"] = self._git_head()
            report["implementation"] = self._implementation_identity()
            report["config_sha256"] = self._config_hash()
            if assets.get("production_ready"):
                if report_path is None:
                    raise ValueError("production conversion requires a durable --report path")
                report_path = self._external_path(Path(report_path), self.project_root, "conversion report")
                report_path.parent.mkdir(parents=True, exist_ok=True)
                probe = report_path.with_name(f".{report_path.name}.{uuid.uuid4().hex}.probe")
                self._atomic_write(probe, "conversion receipt write probe\n")
                probe.unlink()
                self._conversion_fault("before_cutover_commit")
                cutover_metadata = self._seal_receipt({
                    "receipt_version": 2, "operation": "cutover-activation", "verdict": "pass",
                    "pre_cutover_tag": pre_cutover_tag, "pre_cutover_commit": report["implementation_commit"],
                    "source_sha256": source_hash, "task_tree_sha256": report["task_tree_sha256"],
                    "config_sha256": report["config_sha256"], "pre_cutover_assets_sha256": assets["content_sha256"],
                })
                self._atomic_write(self.juno_root / "cutover.json",
                                   json.dumps(cutover_metadata, indent=2, sort_keys=True) + "\n")
                freeze_path.unlink(missing_ok=True)
                subprocess.run(["git", "-C", str(self.project_root), "add", "-A", "--", str(self.juno_root)], check=True)
                # SQLite is rebuildable per-worktree state. Legacy boards may
                # still track it despite ignore rules, so cutover removes only
                # its index entry while retaining the rebuilt local cache.
                subprocess.run(
                    ["git", "-C", str(self.project_root), "rm", "--cached", "--ignore-unmatch", "--", str(self.cache.path)],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                committed = subprocess.run(
                    ["git", "-C", str(self.project_root), "commit", "-m", "Activate Git-native Kanban storage"],
                    capture_output=True, text=True)
                if committed.returncode:
                    subprocess.run(["git", "-C", str(self.project_root), "restore", "--staged", "--", str(self.juno_root)],
                                   capture_output=True)
                    raise ValueError(f"cutover commit failed: {committed.stderr.strip()}")
                report["cutover_commit"] = self._git_head()
                report["cutover_parent"] = subprocess.check_output(
                    ["git", "-C", str(self.project_root), "rev-parse", f"{report['cutover_commit']}^"], text=True).strip()
                if report["cutover_parent"] != report["implementation_commit"]:
                    raise ValueError("cutover commit parent changed during activation")
            report = self._seal_receipt(report)
            if report_path is not None:
                self._atomic_write(Path(report_path), json.dumps(report, indent=2, sort_keys=True) + "\n")
            shutil.rmtree(backup_tasks)
            shutil.rmtree(backup_ledger, ignore_errors=True)
            shutil.rmtree(backup_archive, ignore_errors=True)
            freeze_path.unlink(missing_ok=True)
            return report
        except Exception:
            # Deterministically restore every canonical asset regardless of fault point.
            if activated_archive:
                shutil.rmtree(self.juno_root / "archive", ignore_errors=True)
            if backup_archive.exists():
                os.replace(backup_archive, self.juno_root / "archive")
            if activated_ledger:
                shutil.rmtree(self.juno_root / "ledger", ignore_errors=True)
            if backup_ledger.exists():
                os.replace(backup_ledger, self.juno_root / "ledger")
            if activated_tasks:
                shutil.rmtree(self.tasks_root, ignore_errors=True)
            if backup_tasks.exists():
                os.replace(backup_tasks, self.tasks_root)
            if source_removed and source_backup.exists():
                os.replace(source_backup, source)
            try:
                self.config = Config(str(self.tasks_root / "config.json"))
            except Exception:
                pass
            if self.cache.path.exists():
                self.cache.path.unlink()
            raise
        finally:
            shutil.rmtree(stage_root, ignore_errors=True)
            freeze_path.unlink(missing_ok=True)

    def generate_acceptance_receipt(self, evidence: Mapping[str, Path], receipt_path: Path) -> Dict[str, Any]:
        """Generate seven-day acceptance only from content-addressed gate artifacts."""
        required = {"conversion_parity", "mutation_conflicts", "reconciliation", "cache_parity",
                    "worktree_merges", "privacy", "performance", "rollback_rehearsal"}
        if set(evidence) != required:
            raise ValueError("acceptance evidence must contain exactly: " + ", ".join(sorted(required)))
        window = self.config.to_dict().get("compatibility_window", {})
        if not window.get("active"):
            raise ValueError("compatibility window is not active")
        minimum_end = datetime.fromisoformat(str(window["end"]).replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        if now < minimum_end:
            raise ValueError("the seven-day compatibility window has not actually elapsed")
        expected_operations = {gate: f"acceptance-{gate}" for gate in required}
        expected_operations.update({"conversion_parity": "convert", "performance": "installed-cli-benchmark"})
        source_commit = self._git_head()
        config_sha256 = self._config_hash()
        current_state_sha256 = hashlib.sha256(normalized_bytes(sorted(
            (plain_value(row) for row in self.read_all_tasks_complete()), key=lambda row: row["id"]))).hexdigest()
        window_start = datetime.fromisoformat(str(window["start"]).replace("Z", "+00:00"))
        bound = {}
        for gate in sorted(required):
            path = Path(evidence[gate])
            item = self._read_verified_receipt(path, gate)
            if item.get("operation") != expected_operations[gate]:
                raise ValueError(f"{gate} evidence has wrong machine operation")
            if gate == "conversion_parity":
                item_window = (item.get("compatibility_window") or {}).get("id")
                if item_window != window.get("id") or not item.get("semantic_hashes_match"):
                    raise ValueError("conversion evidence does not bind the active window")
            elif gate == "performance":
                identity = item.get("identity") or {}
                if (item.get("tasks") != 140000 or not all((item.get("gates") or {}).values())
                        or identity.get("package_version") != self._implementation_identity()["package_version"]
                        or set((identity.get("measured_commands") or {})) != {"get", "list", "search", "mutation", "cold_rebuild"}):
                    raise ValueError("performance evidence is not a passing installed-CLI 140k report")
            else:
                results = item.get("command_results")
                try:
                    finished = datetime.fromisoformat(str(item.get("finished_at", "")).replace("Z", "+00:00"))
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{gate} evidence is stale, self-declared, or not bound to current truth") from exc
                if (item.get("window_id") != window.get("id")
                        or item.get("generator") != "juno-kanban acceptance-gate"
                        or item.get("source_commit") != source_commit
                        or item.get("config_sha256") != config_sha256
                        or item.get("current_state_sha256") != current_state_sha256
                        or finished < window_start or finished > now
                        or not isinstance(results, list) or not results
                        or any(result.get("exit_code") != 0 or not result.get("argv")
                               or len(str(result.get("stdout_sha256", ""))) != 64 for result in results)):
                    raise ValueError(f"{gate} evidence is stale, self-declared, or not bound to current truth")
            bound[gate] = {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                           "operation": item["operation"], "content_sha256": item["content_sha256"]}
        receipt = self._seal_receipt({"receipt_version": 2, "operation": "seven-day-acceptance",
            "verdict": "pass", "generator": "juno-kanban compatibility accept",
            "implementation": self._implementation_identity(),
            "window_id": window["id"], "observed_end": now.isoformat().replace("+00:00", "Z"),
            "source_commit": source_commit, "config_sha256": config_sha256,
            "current_state_sha256": current_state_sha256,
            "passed_gates": sorted(required), "evidence": bound})
        self._atomic_write(Path(receipt_path), json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        return receipt

    def lift_compatibility_window(self, acceptance_receipt: Path) -> Dict[str, Any]:
        receipt = self._read_verified_receipt(Path(acceptance_receipt), "seven-day acceptance")
        window = self.config.to_dict().get("compatibility_window", {})
        if not window.get("active"):
            raise ValueError("compatibility window is not active")
        if receipt.get("window_id") != window.get("id"):
            raise ValueError("acceptance receipt does not bind the active compatibility window")
        required = {"conversion_parity", "mutation_conflicts", "reconciliation", "cache_parity",
                    "worktree_merges", "privacy", "performance", "rollback_rehearsal"}
        if (receipt.get("operation") != "seven-day-acceptance"
                or receipt.get("generator") != "juno-kanban compatibility accept"
                or set((receipt.get("evidence") or {})) != required
                or receipt.get("source_commit") != self._git_head()
                or receipt.get("config_sha256") != self._config_hash()):
            raise ValueError("acceptance receipt is not machine-generated and bound to current truth")
        passed = set(receipt.get("passed_gates") or [])
        missing = sorted(required - passed)
        if missing:
            raise ValueError("acceptance receipt is missing gates: " + ", ".join(missing))
        for gate, binding in receipt["evidence"].items():
            evidence_path = Path(binding.get("path", ""))
            if (not evidence_path.is_file()
                    or hashlib.sha256(evidence_path.read_bytes()).hexdigest() != binding.get("sha256")):
                raise ValueError(f"acceptance evidence changed or disappeared: {gate}")
        current_state_sha256 = hashlib.sha256(normalized_bytes(sorted(
            (plain_value(row) for row in self.read_all_tasks_complete()), key=lambda row: row["id"]))).hexdigest()
        if receipt.get("current_state_sha256") != current_state_sha256:
            raise ValueError("acceptance receipt binds a stale current-state snapshot")
        observed_end = datetime.fromisoformat(str(receipt.get("observed_end", "")).replace("Z", "+00:00"))
        minimum_end = datetime.fromisoformat(str(window["end"]).replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        if observed_end < minimum_end:
            raise ValueError("acceptance receipt does not cover the complete seven-day window")
        if observed_end > now + timedelta(minutes=5) or now < minimum_end:
            raise ValueError("the seven-day compatibility window has not actually elapsed")
        window.update({"active": False, "lifted_by_acceptance": True,
                       "acceptance_receipt_sha256": hashlib.sha256(Path(acceptance_receipt).read_bytes()).hexdigest(),
                       "lifted_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")})
        self.config.config["compatibility_window"] = window
        self.config.save()
        return {"operation": "lift-compatibility-window", "window": window, "acceptance": receipt}

    def export_legacy(self, destination: Path):
        rows = []
        for record in self.read_all_tasks_complete():
            plain = plain_value(record)
            plain.pop("schema_version", None)
            fields = plain.pop("fields", {})
            if fields:
                raise ValueError("current state contains custom fields not representable by legacy NDJSON")
            rows.append(plain)
        destination = Path(destination)
        # Preserve the exact spacing contract consumed by the legacy runtime's
        # fixed-string ripgrep queries (`"id": "..."`, etc.).
        content = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
        self._atomic_write(destination, content)
        return {"tasks": len(rows), "sha256": hashlib.sha256(content.encode()).hexdigest(), "path": str(destination)}

    def immediate_rollback(self, conversion_receipt: Path, receipt_path: Path) -> Dict[str, Any]:
        """Revert only the machine-recorded cutover before any later mutation."""
        conversion = self._read_verified_receipt(Path(conversion_receipt), "conversion")
        if (conversion.get("operation") != "convert" or conversion.get("dry_run")
                or conversion.get("status") != "activated" or not conversion.get("semantic_hashes_match")):
            raise ValueError("immediate rollback requires a successful activation receipt")
        pre_cutover_tag = (conversion.get("pre_cutover_assets") or {}).get("tag")
        cutover_commit = conversion.get("cutover_commit")
        pre_cutover_commit = conversion.get("cutover_parent")
        if not all((pre_cutover_tag, cutover_commit, pre_cutover_commit)):
            raise ValueError("conversion receipt does not identify the exact cutover")
        head = self._git_head()
        if head != cutover_commit:
            raise ValueError("immediate rollback requires HEAD to equal the receipt cutover commit")
        tag_commit = subprocess.check_output(
            ["git", "-C", str(self.project_root), "rev-list", "-n", "1", pre_cutover_tag],
            text=True, stderr=subprocess.DEVNULL).strip()
        parent = subprocess.check_output(
            ["git", "-C", str(self.project_root), "rev-parse", f"{cutover_commit}^"],
            text=True, stderr=subprocess.DEVNULL).strip()
        if tag_commit != parent or parent != pre_cutover_commit:
            raise ValueError("conversion receipt/tag/cutover ancestry mismatch")
        cutover_meta = self._read_verified_receipt(self.juno_root / "cutover.json", "cutover metadata")
        if (cutover_meta.get("operation") != "cutover-activation"
                or cutover_meta.get("pre_cutover_commit") != parent
                or cutover_meta.get("source_sha256") != conversion.get("source_sha256")
                or cutover_meta.get("task_tree_sha256") != conversion.get("task_tree_sha256")):
            raise ValueError("conversion receipt does not bind committed cutover metadata")
        dirty = subprocess.check_output(
            ["git", "-C", str(self.project_root), "status", "--porcelain"], text=True).strip()
        if dirty:
            raise ValueError("immediate rollback requires a clean unmutated worktree")
        receipt_path = self._external_path(Path(receipt_path), self.project_root, "immediate rollback receipt")
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        self._rollback_fault("before_immediate_revert")
        command = ["git", "revert", "--no-edit", cutover_commit]
        result = subprocess.run(command, cwd=self.project_root, capture_output=True, text=True)
        if result.returncode:
            raise ValueError(f"cutover revert failed: {result.stderr.strip()}")
        after = self._git_head()
        legacy_candidates = list(self.juno_root.glob("*.ndjson")) + list(self.tasks_root.glob("*.ndjson"))
        if not after or not legacy_candidates or list(self.tasks_root.glob("*/*.md")):
            raise ValueError("revert committed but legacy activation verification failed")
        receipt = self._seal_receipt({
            "receipt_version": 2, "operation": "immediate-rollback", "verdict": "pass",
            "conversion_receipt_sha256": hashlib.sha256(Path(conversion_receipt).read_bytes()).hexdigest(),
            "pre_cutover_tag": pre_cutover_tag, "pre_cutover_commit": tag_commit,
            "cutover_commit": cutover_commit, "rollback_commit": after,
            "command": command, "exit_code": result.returncode,
            "legacy_sources": [{"path": str(path.relative_to(self.project_root)),
                                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                               for path in legacy_candidates],
            "implementation": self._implementation_identity(),
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        })
        self._atomic_write(receipt_path, json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        return receipt

    def execute_post_write_rollback(self, legacy_wheel: Path, runtime_dir: Path,
                                    archive: Path, receipt_path: Path) -> Dict[str, Any]:
        """Freeze, export, archive, install exact legacy wheel, activate, commit and verify."""
        import tarfile
        import venv
        legacy_identity = self._wheel_identity(Path(legacy_wheel))
        legacy_wheel = Path(legacy_identity["path"])
        runtime_dir = self._external_path(Path(runtime_dir), self.project_root, "legacy runtime")
        archive = self._external_path(Path(archive), self.project_root, "rollback archive")
        receipt_path = self._external_path(Path(receipt_path), self.project_root, "rollback receipt")
        if archive.exists() or receipt_path.exists():
            raise ValueError("rollback archive and receipt paths must be new and immutable")
        if self._git_head() is None:
            raise ValueError("post-write rollback requires Git")
        if subprocess.check_output(["git", "-C", str(self.project_root), "status", "--porcelain"], text=True).strip():
            raise ValueError("post-write rollback requires a clean worktree")
        source_head = self._git_head()
        current = [plain_value(row) for row in self.read_all_tasks_complete()]
        current_hash = hashlib.sha256(normalized_bytes(sorted(current, key=lambda row: row["id"]))).hexdigest()
        expected_counts = {status: sum(row.get("status") == status for row in current)
                           for status in sorted({row.get("status") for row in current})}
        status_by_id = {row["id"]: row.get("status") for row in current}
        expected_ready = {row["id"] for row in current
                          if row.get("status") in ("backlog", "todo", "in_progress")
                          and all(status_by_id.get(blocker) in ("done", "archive")
                                  for blocker in (row.get("blocked_by") or []))}
        freeze = {"receipt_version": 2, "operation": "rollback-freeze", "source_commit": source_head,
                  "config_sha256": self._config_hash(), "current_state_sha256": current_hash,
                  "tasks": len(current), "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
        freeze["content_sha256"] = hashlib.sha256(json.dumps(freeze, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        freeze_path = self.juno_root / "ROLLBACK_FREEZE.json"
        freeze = self._seal_receipt(freeze)
        self._atomic_write(freeze_path, json.dumps(freeze, indent=2, sort_keys=True) + "\n")
        self._rollback_fault("after_freeze")
        # Build and validate legacy state outside the active repository. Canonical
        # Markdown remains untouched until the exact legacy runtime passes parity.
        staging_dir = Path(tempfile.mkdtemp(prefix="juno-legacy-rollback-"))
        destination = staging_dir / "backlog.ndjson"
        export = self.export_legacy(destination)
        exported_rows = [json.loads(line) for line in destination.read_text(encoding="utf-8").splitlines() if line]
        expected_legacy_rows = []
        for row in current:
            expected = dict(row)
            expected.pop("schema_version", None)
            expected.pop("fields", None)
            expected_legacy_rows.append(expected)
        if normalized_bytes(sorted(exported_rows, key=lambda row: row["id"])) != normalized_bytes(
                sorted(expected_legacy_rows, key=lambda row: row["id"])):
            raise ValueError("generated legacy export failed complete normalized current-state parity")
        self._rollback_fault("after_export")
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive_members = []
        with tarfile.open(archive, "w:gz") as tar:
            for name in ("tasks", "ledger", "archive"):
                path = self.juno_root / name
                if path.exists():
                    for member in sorted(p for p in path.rglob("*") if p.is_file()):
                        archive_members.append({"path": str(member.relative_to(self.juno_root)),
                                                "sha256": hashlib.sha256(member.read_bytes()).hexdigest(),
                                                "bytes": member.stat().st_size})
                    tar.add(path, arcname=name)
        archive_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
        archive.chmod(0o444)
        self._rollback_fault("after_archive")
        wheel_hash = legacy_identity["sha256"]
        package_version = legacy_identity["version"]
        if runtime_dir.exists(): shutil.rmtree(runtime_dir)
        venv.EnvBuilder(with_pip=True, clear=True).create(runtime_dir)
        python = runtime_dir / "bin" / "python"
        install = subprocess.run([str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(legacy_wheel.resolve())],
                                 capture_output=True, text=True)
        if install.returncode:
            raise ValueError(f"exact legacy package install failed: {install.stderr.strip()}")
        legacy_cli = runtime_dir / "bin" / "juno-kanban"
        if not legacy_cli.exists(): raise ValueError("legacy wheel did not install juno-kanban entry point")
        self._rollback_fault("after_legacy_install")
        legacy_config = self.config.to_dict()
        legacy_config["storage"] = dict(legacy_config.get("storage") or {})
        legacy_config["storage"].update({"base_path": str(staging_dir), "file_pattern": "*.ndjson", "default_file": "backlog.ndjson"})
        legacy_config.pop("compatibility_window", None)
        config_path = staging_dir / "config.json"
        self._atomic_write(config_path, json.dumps(legacy_config, indent=2, ensure_ascii=False) + "\n")

        def run_legacy(arguments):
            arguments = list(arguments)
            global_args = []
            if "-f" in arguments:
                index = arguments.index("-f")
                global_args = arguments[index:index + 2]
                del arguments[index:index + 2]
            argv = [str(legacy_cli), "-c", str(config_path), *global_args, *arguments]
            proc = subprocess.run(argv, cwd=self.project_root, stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True)
            if proc.returncode: raise ValueError(f"legacy parity command failed {argv}: {proc.stderr.strip()}")
            return {"argv": argv, "exit_code": proc.returncode,
                    "stdout_sha256": hashlib.sha256(proc.stdout.encode()).hexdigest(), "stdout": proc.stdout}
        first_id = sorted(row["id"] for row in current)[0] if current else None
        commands = [run_legacy(["list", "--limit", str(max(1, len(current))), "-f", "json"])]
        self._rollback_fault("after_legacy_list")
        if first_id:
            commands.append(run_legacy(["get", first_id, "-f", "json"]))
            self._rollback_fault("after_legacy_get")
            commands.append(run_legacy(["search", "--id", first_id, "--limit", "1", "-f", "json"]))
            self._rollback_fault("after_legacy_search")
            commands.append(run_legacy(["ready", "--limit", str(max(1, len(current))), "-f", "json"]))
            self._rollback_fault("after_legacy_ready")
            commands.append(run_legacy(["deps", first_id, "-f", "json"]))
            self._rollback_fault("after_legacy_dependency")
        def decode_stream(text):
            values, index, decoder = [], 0, json.JSONDecoder()
            while index < len(text):
                while index < len(text) and text[index].isspace(): index += 1
                if index >= len(text): break
                value, index = decoder.raw_decode(text, index); values.append(value)
            return values

        # Parse machine output; substring checks are not parity evidence.
        list_values = decode_stream(commands[0].pop("stdout"))
        listed = list_values[0] if list_values and isinstance(list_values[0], list) else []
        summary = next((value.get("summary") for value in list_values if isinstance(value, dict) and "summary" in value), None)
        if {row.get("id") for row in listed} != {row["id"] for row in current}:
            raise ValueError("legacy list parity omitted or added current tasks")
        if not summary or summary.get("total_tasks") != len(current) or any(
                summary.get("status_counts", {}).get(status, 0) != count for status, count in expected_counts.items()):
            raise ValueError("legacy status-summary parity failed")
        parsed_commands = []
        for index, command_result in enumerate(commands[1:], 1):
            values = decode_stream(command_result.pop("stdout"))
            ids = {row.get("id") for value in values for row in (value if isinstance(value, list) else [])
                   if isinstance(row, dict)}
            if index in (1, 2) and first_id not in ids:
                raise ValueError("legacy get/search parity failed")
            if index == 3 and ids != expected_ready:
                raise ValueError("legacy ready/dependency parity failed")
            if index == 4:
                info = next((row for value in values for row in (value if isinstance(value, list) else [])
                             if isinstance(row, dict)), None)
                expected_blockers = set(next(row.get("blocked_by") or [] for row in current if row["id"] == first_id))
                expected_dependents = {row["id"] for row in current if first_id in (row.get("blocked_by") or [])}
                actual_blockers = {row.get("id") for key in ("unmet_blockers", "met_blockers")
                                   for row in (info or {}).get(key, [])}
                if (not info or info.get("task_id") != first_id or actual_blockers != expected_blockers
                        or set(info.get("dependents") or []) != expected_dependents):
                    raise ValueError("legacy dependency parity failed")
            command_result["parsed_sha256"] = hashlib.sha256(normalized_bytes(values)).hexdigest()
            parsed_commands.append(command_result)
        commands = [dict(commands[0], parsed_sha256=hashlib.sha256(normalized_bytes(list_values)).hexdigest()), *parsed_commands]

        def restore_canonical_activation():
            shutil.rmtree(self.tasks_root, ignore_errors=True)
            shutil.rmtree(self.juno_root / "ledger", ignore_errors=True)
            shutil.rmtree(self.juno_root / "archive", ignore_errors=True)
            for generated in (self.juno_root / "legacy-runtime.json", self.juno_root / "kanban-runtime"):
                generated.unlink(missing_ok=True)
            with tarfile.open(archive, "r:gz") as saved:
                for member in saved.getmembers():
                    relative = Path(member.name)
                    if relative.is_absolute() or ".." in relative.parts:
                        raise ValueError("unsafe member in rollback recovery archive")
                    target = self.juno_root / relative
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                    elif member.isfile():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        source = saved.extractfile(member)
                        if source is None:
                            raise ValueError("rollback recovery archive member cannot be read")
                        target.write_bytes(source.read())

        # Only a fully validated staged export is activated. NDJSON, runtime
        # binding, and legacy configuration become visible in one Git commit.
        self._rollback_fault("before_activation")
        for path in self.tasks_root.glob("*/*.md"): path.unlink()
        for path in sorted(self.tasks_root.glob("*"), reverse=True):
            if path.is_dir(): shutil.rmtree(path)
        shutil.rmtree(self.juno_root / "ledger", ignore_errors=True)
        shutil.rmtree(self.juno_root / "archive", ignore_errors=True)
        active_export = self.tasks_root / "backlog.ndjson"
        shutil.copy2(destination, active_export)
        export["path"] = str(active_export)
        legacy_config["storage"]["base_path"] = str(self.tasks_root)
        active_config = self.tasks_root / "config.json"
        self._atomic_write(active_config, json.dumps(legacy_config, indent=2, ensure_ascii=False) + "\n")
        activation = {"operation": "legacy-runtime-activation", "wheel_sha256": wheel_hash,
                      "package_version": package_version, "runtime_entrypoint": str(legacy_cli),
                      "runtime_entrypoint_sha256": hashlib.sha256(legacy_cli.read_bytes()).hexdigest(),
                      "config_sha256": hashlib.sha256(active_config.read_bytes()).hexdigest(),
                      "export_sha256": export["sha256"], "archive_sha256": archive_hash,
                      "source_commit": source_head}
        self._atomic_write(self.juno_root / "legacy-runtime.json", json.dumps(activation, indent=2, sort_keys=True) + "\n")
        runtime_launcher = self.juno_root / "kanban-runtime"
        self._atomic_write(runtime_launcher,
            "#!/bin/sh\nexec " + json.dumps(str(legacy_cli)) + " -c " + json.dumps(str(active_config)) + " \"$@\"\n")
        runtime_launcher.chmod(0o755)
        try:
            self._rollback_fault("before_activation_commit")
            active_probe = subprocess.run([str(runtime_launcher), "-f", "json", "list", "--limit", str(max(1, len(current)))],
                                          cwd=self.project_root, stdin=subprocess.DEVNULL, capture_output=True, text=True)
            if active_probe.returncode or hashlib.sha256(active_probe.stdout.encode()).hexdigest() != commands[0]["stdout_sha256"]:
                raise ValueError("activated legacy runtime does not match staged parity output")
            activation["active_probe_stdout_sha256"] = hashlib.sha256(active_probe.stdout.encode()).hexdigest()
            self._atomic_write(self.juno_root / "legacy-runtime.json", json.dumps(activation, indent=2, sort_keys=True) + "\n")
            freeze_path.unlink()
            subprocess.run(["git", "-C", str(self.project_root), "add", "-A", str(self.juno_root)], check=True)
            commit = subprocess.run(["git", "-C", str(self.project_root), "commit", "-m", "Rollback Kanban to exact legacy runtime"],
                                    capture_output=True, text=True)
            if commit.returncode:
                raise ValueError(f"rollback activation commit failed: {commit.stderr.strip()}")
        except Exception:
            subprocess.run(["git", "-C", str(self.project_root), "restore", "--staged", "--", str(self.juno_root)],
                           capture_output=True)
            restore_canonical_activation()
            raise
        rollback_commit = self._git_head()
        receipt = {"receipt_version": 2, "operation": "post-write-rollback", "verdict": "pass",
                   "source_commit": source_head, "rollback_commit": rollback_commit,
                   "freeze": freeze, "current_state_sha256": current_hash, "export": export,
                   "archive": str(archive), "archive_sha256": archive_hash,
                   "archive_members": archive_members,
                   "legacy_package": {**legacy_identity, "wheel": str(legacy_wheel.resolve()), "wheel_sha256": wheel_hash,
                                      "entrypoint": str(legacy_cli),
                                      "entrypoint_sha256": activation["runtime_entrypoint_sha256"]},
                   "activation": activation, "legacy_parity_commands": commands,
                   "status_counts": expected_counts, "implementation": self._implementation_identity()}
        receipt = self._seal_receipt(receipt)
        self._atomic_write(Path(receipt_path), json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        Path(receipt_path).chmod(0o444)
        shutil.rmtree(staging_dir, ignore_errors=True)
        return receipt

    def count_tasks(self, file_pattern=None): return sum(1 for _ in self.read_all_tasks())
    def get_tasks_by_status(self, status, file_pattern=None): return [t for t in self.read_all_tasks() if t.get("status") == status]
    def get_open_tasks(self, file_pattern=None): return [t for t in self.read_all_tasks() if not str(t.get("agent_response") or "").strip()]
    def get_recent_tasks(self, limit=5, file_pattern=None): return sorted(self.read_all_tasks(), key=lambda t: str(t.get("last_modified", "")), reverse=True)[:limit]
    def get_tasks_with_tag(self, tag, file_pattern=None): return [t for t in self.read_all_tasks() if tag in (t.get("feature_tags") or [])]
    def get_tasks_with_commit(self, commit_hash, file_pattern=None): return [t for t in self.read_all_tasks() if t.get("commit_hash") == commit_hash]
    def get_file_info(self):
        return [{"path": p, "size_bytes": Path(p).stat().st_size, "size_mb": round(Path(p).stat().st_size / 1048576, 2), "task_count": 1, "modified": datetime.fromtimestamp(Path(p).stat().st_mtime, timezone.utc).isoformat()} for p in self.get_files()]

    def __repr__(self): return f"TaskStorage(base_path='{self.base_path}', pattern='{self.file_pattern}')"
