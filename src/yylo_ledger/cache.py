"""Disposable, freshness-checked SQLite index of canonical current task files."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Set, Tuple

SCHEMA_VERSION = 10


class TaskCache:
    """Derived query state. Public reads validate it against Git/filesystem truth."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists()
        timeout = float(os.environ.get("YYLO_LEDGER_CACHE_TIMEOUT_SECONDS", "0.25"))
        db = sqlite3.connect(self.path, timeout=max(0.0, timeout))
        db.execute("PRAGMA busy_timeout=%d" % max(0, int(timeout * 1000)))
        if existed:
            return db
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
          id TEXT PRIMARY KEY, id_fold TEXT NOT NULL, status TEXT NOT NULL, status_rank INTEGER NOT NULL,
          modified TEXT NOT NULL, commit_hash TEXT, body TEXT NOT NULL, response TEXT NOT NULL, json TEXT NOT NULL,
          source_path TEXT NOT NULL UNIQUE, source_size INTEGER NOT NULL,
          source_mtime_ns INTEGER NOT NULL, source_sha256 TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS task_tags (
          task_id TEXT NOT NULL, tag TEXT NOT NULL, PRIMARY KEY(task_id, tag)
        );
        CREATE TABLE IF NOT EXISTS dependencies (
          task_id TEXT NOT NULL, blocker_id TEXT NOT NULL, PRIMARY KEY(task_id, blocker_id)
        );
        CREATE TABLE IF NOT EXISTS custom_fields (
          task_id TEXT NOT NULL, path TEXT NOT NULL, scalar_type TEXT NOT NULL,
          scalar_text TEXT, normalized_date TEXT, PRIMARY KEY(task_id, path)
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS task_text USING fts5(
          task_id UNINDEXED, body, response,
          tokenize='trigram case_sensitive 0', detail='none'
        );
        CREATE TABLE IF NOT EXISTS status_counts (
          status TEXT PRIMARY KEY, task_count INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS archive_tasks (
          id TEXT PRIMARY KEY, id_fold TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
          terminal_transition_at TEXT NOT NULL, modified TEXT NOT NULL,
          pack_path TEXT NOT NULL, manifest_path TEXT NOT NULL, checksum_path TEXT NOT NULL,
          record_offset INTEGER NOT NULL, record_length INTEGER NOT NULL,
          record_sha256 TEXT NOT NULL, pack_sha256 TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS archive_tags (
          task_id TEXT NOT NULL, tag TEXT NOT NULL, PRIMARY KEY(task_id, tag)
        );
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS custom_fields_lookup ON custom_fields(path, scalar_type, scalar_text, task_id);
        CREATE INDEX IF NOT EXISTS custom_fields_date ON custom_fields(path, normalized_date, task_id);
        CREATE UNIQUE INDEX IF NOT EXISTS tasks_id_fold ON tasks(id_fold);
        CREATE INDEX IF NOT EXISTS tasks_status_modified ON tasks(status, modified, id);
        CREATE INDEX IF NOT EXISTS tasks_rank_modified ON tasks(status_rank, modified, id);
        CREATE INDEX IF NOT EXISTS tasks_rank_modified_desc ON tasks(status_rank ASC, modified DESC, id DESC);
        CREATE INDEX IF NOT EXISTS tasks_modified ON tasks(modified, id);
        CREATE INDEX IF NOT EXISTS tasks_commit ON tasks(commit_hash, modified, id);
        CREATE INDEX IF NOT EXISTS tasks_source_path ON tasks(source_path);
        CREATE INDEX IF NOT EXISTS task_tags_lookup ON task_tags(tag, task_id);
        CREATE INDEX IF NOT EXISTS dependencies_blocker ON dependencies(blocker_id, task_id);
        CREATE INDEX IF NOT EXISTS archive_tasks_status_modified ON archive_tasks(status, modified, id);
        CREATE INDEX IF NOT EXISTS archive_tasks_terminal ON archive_tasks(terminal_transition_at, id);
        CREATE INDEX IF NOT EXISTS archive_tags_lookup ON archive_tags(tag, task_id);
        """)
        return db

    @staticmethod
    def _plain(record: Mapping[str, Any]) -> dict:
        return json.loads(json.dumps(record, default=lambda value: value.isoformat()))

    @staticmethod
    def _head(repository_root: Path) -> Optional[str]:
        try:
            return subprocess.check_output(["git", "-C", str(repository_root), "rev-parse", "HEAD"],
                                           text=True, stderr=subprocess.DEVNULL).strip()
        except (OSError, subprocess.CalledProcessError):
            return None

    @staticmethod
    def archive_tree_identity(repository_root: Path) -> str:
        """Identify the committed archive tree without coupling it to repository HEAD.

        Product or hot-task commits must not invalidate a large cold index. Dirty
        archive paths are checked separately; this tree object detects committed
        archive changes while remaining stable across unrelated commits.
        """
        try:
            return subprocess.check_output(
                ["git", "-C", str(repository_root), "rev-parse", "HEAD:.juno_task/archive"],
                text=True, stderr=subprocess.DEVNULL,
            ).strip()
        except OSError:
            return "outside-git"
        except subprocess.CalledProcessError:
            return "git-empty" if TaskCache._head(repository_root) is not None else "outside-git"

    @staticmethod
    def _revision(rows: Iterable[Tuple[str, str]]) -> str:
        payload = json.dumps(sorted(rows), separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(payload.encode()).hexdigest()

    def _write_record(self, db: sqlite3.Connection, record: Mapping[str, Any], path: Path, digest: str,
                      *, index_text: bool = True):
        stat = path.stat()
        plain = self._plain(record)
        task_id = plain["id"]
        old_status_row = db.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        old_status = old_status_row[0] if old_status_row else None
        new_status = plain.get("status", "backlog")
        db.execute("""INSERT OR REPLACE INTO tasks
            (id,id_fold,status,status_rank,modified,commit_hash,body,response,json,source_path,source_size,source_mtime_ns,source_sha256)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            task_id, task_id.casefold(), new_status,
            0 if new_status in ("backlog", "todo", "in_progress") else (1 if new_status in ("done", "archive") else 2),
            str(plain.get("last_modified", "")),
            plain.get("commit_hash"), plain.get("body", ""), plain.get("agent_response", ""),
            json.dumps(plain, ensure_ascii=False, separators=(",", ":")), str(path), stat.st_size,
            stat.st_mtime_ns, digest,
        ))
        if old_status != new_status:
            if old_status is not None:
                db.execute("UPDATE status_counts SET task_count=task_count-1 WHERE status=?", (old_status,))
                db.execute("DELETE FROM status_counts WHERE status=? AND task_count<=0", (old_status,))
            db.execute("""INSERT INTO status_counts(status,task_count) VALUES (?,1)
                          ON CONFLICT(status) DO UPDATE SET task_count=task_count+1""", (new_status,))
        for table in ("custom_fields", "task_tags", "dependencies"):
            db.execute(f"DELETE FROM {table} WHERE task_id=?", (task_id,))
        if index_text:
            db.execute("DELETE FROM task_text WHERE task_id=?", (task_id,))
        db.executemany("INSERT INTO task_tags VALUES (?,?)", ((task_id, str(tag)) for tag in (plain.get("feature_tags") or [])))
        db.executemany("INSERT INTO dependencies VALUES (?,?)", ((task_id, str(blocker)) for blocker in (plain.get("blocked_by") or [])))
        if index_text:
            db.execute("INSERT INTO task_text(task_id,body,response) VALUES (?,?,?)",
                       (task_id, plain.get("body", ""), plain.get("agent_response", "")))
        rows = []
        def flatten(value, prefix=""):
            if isinstance(value, dict):
                for key, child in value.items():
                    flatten(child, f"{prefix}.{key}" if prefix else str(key))
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    flatten(child, f"{prefix}[{index}]")
            else:
                kind = "null" if value is None else ("boolean" if isinstance(value, bool) else
                       "number" if isinstance(value, (int, float)) else "string")
                text = None if value is None else str(value)
                normalized = text if isinstance(text, str) and len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-" else None
                if normalized and ('T' in normalized or 't' in normalized):
                    try:
                        parsed = datetime.fromisoformat(normalized.replace('Z', '+00:00'))
                        if parsed.tzinfo is None: parsed = parsed.replace(tzinfo=timezone.utc)
                        normalized = parsed.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
                    except ValueError:
                        normalized = None
                rows.append((task_id, prefix, kind, text, normalized))
        flatten(plain.get("fields") or {})
        db.executemany("INSERT INTO custom_fields VALUES (?,?,?,?,?)", rows)

    @staticmethod
    def _directory_signature(tasks_root: Path) -> str:
        rows = [(path.name, path.stat().st_mtime_ns) for path in tasks_root.iterdir() if path.is_dir()]
        return hashlib.sha256(json.dumps(sorted(rows), separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def _index_signature(repository_root: Path) -> str:
        try:
            relative = subprocess.check_output(["git", "-C", str(repository_root), "rev-parse", "--git-path", "index"],
                                               text=True, stderr=subprocess.DEVNULL).strip()
            path = Path(relative) if Path(relative).is_absolute() else repository_root / relative
            stat = path.stat()
            return f"{stat.st_size}:{stat.st_mtime_ns}"
        except (OSError, subprocess.CalledProcessError):
            return "missing"

    @staticmethod
    def _untracked_paths(repository_root: Path, tasks_root: Path):
        try:
            result = subprocess.run(["git", "-C", str(repository_root), "ls-files", "--others",
                                     "--exclude-standard", "-z", "--", str(tasks_root)],
                                    capture_output=True, check=False)
            if result.returncode:
                return []
            return sorted(str(repository_root / p.decode(errors="surrogateescape"))
                          for p in result.stdout.split(b"\0") if p and p.endswith(b".md"))
        except OSError:
            return []

    def _finish_metadata(self, db: sqlite3.Connection, repository_root: Path, config_hash: str,
                         *, rebuild: bool = False, changed_path: Optional[Path] = None):
        # Rebuilds receive a deterministic content revision. Incremental refresh
        # and mutation only need a unique revision to invalidate existing cursors;
        # rescanning every indexed digest would make each write O(board size).
        revision = (self._revision(db.execute("SELECT id, source_sha256 FROM tasks"))
                    if rebuild else uuid.uuid4().hex)
        tasks_root = Path(db.execute("SELECT source_path FROM tasks LIMIT 1").fetchone()[0]).parent.parent if db.execute("SELECT count(*) FROM tasks").fetchone()[0] else repository_root / ".juno_task/tasks"
        old_metadata = self._metadata(db)
        current_directory_signature = self._directory_signature(tasks_root)
        # Listing every untracked path is O(Git index size). A CLI mutation knows
        # its exact path, so update that set directly and deliberately preserve
        # the prior directory signature. The next collection then notices any
        # directory change (including concurrent external additions) and performs
        # one complete reconciliation rather than charging every write.
        if changed_path is not None and not rebuild:
            directory_signature = old_metadata.get("directory_signature", current_directory_signature)
            untracked = set(json.loads(old_metadata.get("untracked_task_paths", "[]")))
            tracked = subprocess.run(
                ["git", "-C", str(repository_root), "ls-files", "--error-unmatch", "--", str(changed_path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ).returncode == 0
            if tracked:
                untracked.discard(str(changed_path))
            elif changed_path.exists():
                untracked.add(str(changed_path))
            else:
                untracked.discard(str(changed_path))
            untracked = sorted(untracked)
        elif rebuild or current_directory_signature != old_metadata.get("directory_signature"):
            directory_signature = current_directory_signature
            untracked = self._untracked_paths(repository_root, tasks_root)
        else:
            directory_signature = current_directory_signature
            untracked = json.loads(old_metadata.get("untracked_task_paths", "[]"))
        values = [("schema_version", str(SCHEMA_VERSION)), ("config_sha256", config_hash),
                  ("repository_head", self._head(repository_root) or "outside-git"),
                  ("cache_revision", revision), ("directory_signature", directory_signature),
                  ("git_index_signature", self._index_signature(repository_root)),
                  ("untracked_task_paths", json.dumps(untracked, separators=(",", ":"))),
                  ("cursor_secret", old_metadata.get("cursor_secret") or secrets.token_hex(32))]
        db.executemany("INSERT OR REPLACE INTO metadata VALUES (?,?)", values)

    def upsert(self, record: Mapping[str, Any], path: Path, digest: str,
               repository_root: Optional[Path] = None, config_hash: str = ""):
        with self._connect() as db:
            old = db.execute("SELECT source_sha256 FROM tasks WHERE id=?", (record["id"],)).fetchone()
            self._write_record(db, record, path, digest)
            if old is None or old[0] != digest:
                self._finish_metadata(db, repository_root or path.parent, config_hash,
                                      changed_path=path)

    def rebuild(self, records: Iterable[Tuple[Mapping[str, Any], Path, str]], repository_root: Path,
                config_hash: str = "", archive_records: Iterable[Mapping[str, Any]] = (),
                archive_inventory: str = ""):
        for path in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
            path.unlink(missing_ok=True)
        with self._connect() as db:
            # This database is disposable and rebuilt from canonical files. Avoid
            # a hundreds-of-MiB WAL/checkpoint during the one bulk transaction.
            db.execute("PRAGMA journal_mode=OFF")
            db.execute("PRAGMA synchronous=OFF")
            db.execute("PRAGMA temp_store=MEMORY")
            # Populate ordinary/cache relation rows first, then feed FTS in one
            # set-based statement. Per-document FTS updates create/merge many
            # transient segments and become super-linear near 140k tasks.
            for record, path, digest in records:
                self._write_record(db, record, path, digest, index_text=False)
            db.execute("INSERT INTO task_text(task_id,body,response) SELECT id,body,response FROM tasks")
            db.execute("INSERT INTO task_text(task_text) VALUES('optimize')")
            for item in archive_records:
                db.execute("""INSERT INTO archive_tasks
                    (id,id_fold,status,terminal_transition_at,modified,pack_path,manifest_path,
                     checksum_path,record_offset,record_length,record_sha256,pack_sha256)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    item["task_id"], item["id_fold"], item["status"],
                    item["terminal_transition_at"], item["last_modified"], item["pack"],
                    item["manifest"], item["checksum"], item["offset"], item["length"],
                    item["record_sha256"], item["pack_sha256"]))
                db.executemany("INSERT INTO archive_tags VALUES (?,?)",
                               ((item["task_id"], str(tag)) for tag in item["feature_tags"]))
            self._finish_metadata(db, repository_root, config_hash, rebuild=True)
            db.executemany("INSERT OR REPLACE INTO metadata VALUES (?,?)", (
                ("archive_inventory_sha256", archive_inventory),
                ("archive_record_count", str(db.execute("SELECT count(*) FROM archive_tasks").fetchone()[0])),
                ("archive_tree_identity", self.archive_tree_identity(repository_root))))
        # journal_mode=OFF is intentionally fast for the private bulk build but
        # persists in the database header. Restore WAL after the transaction so
        # ordinary read-only commands can overlap with later incremental refreshes.
        with self._connect() as db:
            mode = db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise sqlite3.DatabaseError(f"failed to restore WAL journal mode: {mode}")

    def _metadata(self, db: sqlite3.Connection) -> Dict[str, str]:
        return dict(db.execute("SELECT key, value FROM metadata"))

    def ensure_fresh(self, tasks_root: Path, repository_root: Path, config_hash: str,
                     loader: Callable[[Path], Mapping[str, Any]], hasher: Callable[[Mapping[str, Any]], str]) -> bool:
        if not self.path.exists():
            return False
        try:
            with self._connect() as db:
                metadata = self._metadata(db)
                if metadata.get("schema_version") != str(SCHEMA_VERSION) or metadata.get("config_sha256") != config_hash:
                    return False
                cached_head, current_head = metadata.get("repository_head"), self._head(repository_root)
                changed: Set[Path] = set()
                metadata_changed = False
                if current_head is not None:
                    if cached_head not in (current_head, None, "outside-git"):
                        result = subprocess.run(["git", "-C", str(repository_root), "diff", "--name-only", "-z",
                                                 cached_head, current_head, "--", str(tasks_root)], capture_output=True)
                        if result.returncode != 0: return False
                        changed.update(repository_root / p.decode(errors="surrogateescape") for p in result.stdout.split(b"\0") if p)
                    # One fsmonitor-aware status query reports tracked staged and
                    # unstaged paths without traversing untracked task files. This
                    # avoids separate O(index) diff-files/cached-diff commands.
                    status = subprocess.run(
                        ["git", "-C", str(repository_root), "status", "--porcelain=v1",
                         "-z", "-uno", "--", str(tasks_root)], capture_output=True)
                    if status.returncode != 0:
                        return False
                    entries = status.stdout.split(b"\0")
                    index = 0
                    while index < len(entries):
                        entry = entries[index]
                        index += 1
                        if not entry:
                            continue
                        code = entry[:2]
                        raw = entry[3:] if len(entry) >= 4 else b""
                        if raw:
                            changed.add(repository_root / raw.decode(errors="surrogateescape"))
                        if b"R" in code or b"C" in code:
                            if index < len(entries) and entries[index]:
                                changed.add(repository_root / entries[index].decode(errors="surrogateescape"))
                            index += 1
                    index_signature = self._index_signature(repository_root)
                    metadata_changed = index_signature != metadata.get("git_index_signature")
                    # Cache-created/new task files are untracked until the operator
                    # commits them. Hash only that usually-small set on warm reads;
                    # this closes the same-size/same-mtime direct-edit gap without
                    # traversing every tracked task.
                    for raw in json.loads(metadata.get("untracked_task_paths", "[]")):
                        path = Path(raw)
                        if path.exists():
                            row = db.execute("SELECT source_sha256 FROM tasks WHERE source_path=?", (str(path),)).fetchone()
                            try:
                                if row is None or hasher(loader(path)) != row[0]: changed.add(path)
                            except (OSError, ValueError): return False
                        else:
                            changed.add(path)
                    directory_signature = self._directory_signature(tasks_root)
                    if directory_signature != metadata.get("directory_signature"):
                        changed.update(Path(p) for p in self._untracked_paths(repository_root, tasks_root))
                        metadata_changed = True
                else:
                    # Hash every file outside Git. Metadata-only checks can miss same-size/same-mtime edits.
                    disk = {str(path): path for path in tasks_root.glob("*/*.md")}
                    cached = {row[0]: row[1] for row in db.execute("SELECT source_path,source_sha256 FROM tasks")}
                    for key, path in disk.items():
                        try:
                            if key not in cached or hasher(loader(path)) != cached[key]: changed.add(path)
                        except (OSError, ValueError):
                            return False
                    changed.update(Path(key) for key in cached if key not in disk)
                mutated = False
                for path in changed:
                    if path.suffix != ".md" or path.name == "config.json": continue
                    if path.exists():
                        record = loader(path)
                        digest = hasher(record)
                        old = db.execute("SELECT source_sha256 FROM tasks WHERE source_path=?", (str(path),)).fetchone()
                        if old is None or old[0] != digest:
                            self._write_record(db, record, path, digest); mutated = True
                    else:
                        row = db.execute("SELECT id FROM tasks WHERE source_path=?", (str(path),)).fetchone()
                        if row:
                            status_row = db.execute("SELECT status FROM tasks WHERE id=?", (row[0],)).fetchone()
                            for table in ("custom_fields", "task_tags", "dependencies", "task_text"):
                                db.execute(f"DELETE FROM {table} WHERE task_id=?", (row[0],))
                            db.execute("DELETE FROM tasks WHERE source_path=?", (str(path),))
                            if status_row:
                                db.execute("UPDATE status_counts SET task_count=task_count-1 WHERE status=?", status_row)
                                db.execute("DELETE FROM status_counts WHERE status=? AND task_count<=0", status_row)
                            mutated = True
                if mutated or metadata_changed or cached_head != (current_head or "outside-git"):
                    self._finish_metadata(db, repository_root, config_hash)
                return True
        except (OSError, sqlite3.DatabaseError, ValueError, json.JSONDecodeError):
            return False

    def all(self):
        if not self.path.exists(): return None
        try:
            with self._connect() as db:
                if self._metadata(db).get("schema_version") != str(SCHEMA_VERSION): return None
                return [json.loads(row[0]) for row in db.execute("SELECT json FROM tasks")]
        except sqlite3.DatabaseError:
            return None

    def case_collision(self, task_id: str) -> Optional[str]:
        if not self.path.exists(): return None
        with self._connect() as db:
            row = db.execute("SELECT id FROM tasks WHERE id_fold=? AND id<>?", (task_id.casefold(), task_id)).fetchone()
            return row[0] if row else None

    def revision(self) -> Optional[str]:
        if not self.path.exists(): return None
        try:
            with self._connect() as db:
                row = db.execute("SELECT value FROM metadata WHERE key='cache_revision'").fetchone()
                return row[0] if row else None
        except sqlite3.DatabaseError: return None

    def archive_identity(self) -> Optional[str]:
        if not self.path.exists(): return None
        try:
            with self._connect() as db:
                row = db.execute("SELECT value FROM metadata WHERE key='archive_tree_identity'").fetchone()
                return row[0] if row else None
        except sqlite3.DatabaseError: return None

    def archive_index_complete(self) -> bool:
        """Cheaply detect missing/extra logical rows in the disposable index."""
        if not self.path.exists(): return False
        try:
            with self._connect() as db:
                metadata = self._metadata(db)
                expected = int(metadata.get("archive_record_count", "-1"))
                return expected == db.execute("SELECT count(*) FROM archive_tasks").fetchone()[0]
        except (sqlite3.DatabaseError, ValueError):
            return False

    def sign_cursor(self, canonical: str) -> str:
        with self._connect() as db:
            secret = self._metadata(db).get("cursor_secret")
            if not secret: raise ValueError("cache cursor secret missing")
            return hmac.new(bytes.fromhex(secret), canonical.encode(), hashlib.sha256).hexdigest()

    def verify_cursor(self, canonical: str, signature: str) -> bool:
        try: return hmac.compare_digest(self.sign_cursor(canonical), signature)
        except (ValueError, sqlite3.DatabaseError): return False

    @staticmethod
    def _values(value):
        if value is None: return []
        return value if isinstance(value, list) else [value]

    def query(self, *, filters: Mapping[str, Any], limit: Optional[int], offset: int = 0,
              last_key: Optional[Tuple[int, str, str]] = None, sort_order: str = "desc",
              prioritized: bool = False, status_sequence=None, ready: bool = False,
              capture_plan: bool = False):
        """Run one indexed SQL collection plan; decode only the requested page."""
        where, params, joins, join_params = [], [], [], []
        seeded = False

        def indexed_relation(table, condition, values):
            """Use the first selective relation as the driving indexed join."""
            nonlocal seeded
            if not seeded:
                alias = "seed"
                joins.append(f"JOIN {table} {alias} ON {alias}.task_id=t.id AND " + condition.replace("x.", f"{alias}."))
                join_params.extend(values); seeded = True
            else:
                where.append(f"EXISTS (SELECT 1 FROM {table} x WHERE x.task_id=t.id AND {condition})")
                params.extend(values)

        statuses = self._values(filters.get("status"))
        if statuses:
            where.append(f"t.status IN ({','.join('?' * len(statuses))})"); params.extend(statuses)
        if filters.get("id"):
            where.append("t.id=?"); params.append(filters["id"])
        if filters.get("commit_hash"):
            where.append("t.commit_hash=?"); params.append(filters["commit_hash"])
        tags = self._values(filters.get("tag"))
        if tags:
            indexed_relation("task_tags", f"x.tag IN ({','.join('?' * len(tags))})", tags)
        excluded = self._values(filters.get("exclude_tags"))
        if excluded:
            where.append(f"NOT EXISTS (SELECT 1 FROM task_tags x WHERE x.task_id=t.id AND x.tag IN ({','.join('?' * len(excluded))}))"); params.extend(excluded)
        if filters.get("open_only"): where.append("t.response=''")
        text_terms = []
        if filters.get("body_text"): text_terms.append(("body", filters["body_text"]))
        if filters.get("response_text"): text_terms.append(("response", filters["response_text"]))
        for column, term in text_terms:
            term = str(term)
            if len(term) < 3:
                where.append(f"instr(lower(t.{column}), lower(?)) > 0"); params.append(term)
            else:
                # detail=none stores no positions (bounded cold rebuild/memory).
                # Intersect indexed trigrams, then verify exact substring in SQL
                # to remove non-contiguous/cross-order false positives.
                trigrams = dict.fromkeys(term[index:index + 3].lower() for index in range(len(term) - 2))
                fts_query = " AND ".join(f'"{value.replace(chr(34), chr(34) * 2)}"'
                                         for value in trigrams)
                if not seeded:
                    joins.append("JOIN task_text ON task_text.task_id=t.id AND task_text MATCH ?")
                    join_params.append(fts_query); seeded = True
                else:
                    where.append("t.id IN (SELECT task_id FROM task_text WHERE task_text MATCH ?)")
                    params.append(fts_query)
                where.append(f"instr(lower(t.{column}), lower(?)) > 0"); params.append(term)
        for path, value in (filters.get("field_equals") or {}).items():
            indexed_relation("custom_fields", "x.path=? AND x.scalar_text=?", (path, str(value)))
        for path in filters.get("field_exists") or []:
            indexed_relation("custom_fields", "x.path=?", (path,))
        for operator, values in (("<", filters.get("field_before") or {}), (">", filters.get("field_after") or {})):
            for path, value in values.items():
                indexed_relation("custom_fields", f"x.path=? AND x.normalized_date {operator} ?", (path, str(value)))
        if filters.get("overdue"):
            where.append("t.status NOT IN ('done','archive')")
            indexed_relation("custom_fields", "x.path='due_date' AND x.normalized_date < ?", (str(filters["overdue"]),))
        if ready:
            where.extend(["t.status IN ('backlog','todo','in_progress')",
                          """NOT EXISTS (SELECT 1 FROM dependencies d
                          LEFT JOIN tasks b ON b.id=d.blocker_id
                          LEFT JOIN archive_tasks a ON a.id=d.blocker_id
                          WHERE d.task_id=t.id AND
                          (COALESCE(b.status,a.status) IS NULL OR COALESCE(b.status,a.status) NOT IN ('done','archive')))"""])
        if status_sequence:
            cases = " ".join(f"WHEN ? THEN {i}" for i, _ in enumerate(status_sequence))
            rank_sql = f"CASE t.status {cases} ELSE {len(status_sequence)} END"
            rank_params = list(status_sequence)
        elif prioritized:
            rank_sql = "t.status_rank"
            rank_params = []
        else:
            rank_sql, rank_params = "0", []
        predicate = " AND ".join(where) if where else "1"
        from_sql = "tasks t " + " ".join(joins)
        direction = "ASC" if sort_order == "asc" else "DESC"
        with self._connect() as db:
            active_filters = {key for key, value in filters.items() if value}
            if not joins and not ready and active_filters <= {"status"}:
                count_sql = "SELECT status,task_count FROM status_counts"
                count_params = []
                if statuses:
                    count_sql += f" WHERE status IN ({','.join('?' * len(statuses))})"
                    count_params.extend(statuses)
                counts = {row[0]: row[1] for row in db.execute(count_sql, count_params)}
            else:
                counts = {row[0]: row[1] for row in db.execute(
                    f"SELECT t.status,count(*) FROM {from_sql} WHERE {predicate} GROUP BY t.status", join_params + params)}
            total = sum(counts.values())
            key_where, key_params = "", []
            if last_key is not None:
                rank, modified, task_id = last_key
                cmp = ">" if direction == "ASC" else "<"
                key_where = f" WHERE (sort_rank > ? OR (sort_rank=? AND (modified {cmp} ? OR (modified=? AND id {cmp} ?))))"
                key_params = [rank, rank, modified, modified, task_id]
            sql = f"""WITH ranked AS (SELECT t.json,t.id,t.modified,{rank_sql} AS sort_rank FROM {from_sql} WHERE {predicate})
                      SELECT json,sort_rank,modified,id FROM ranked{key_where}
                      ORDER BY sort_rank ASC,modified {direction},id {direction}"""
            query_params = rank_params + join_params + params + key_params
            if limit not in (None, 0): sql += " LIMIT ?"; query_params.append(limit)
            if offset: sql += " OFFSET ?"; query_params.append(offset)
            plan = ([row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql, query_params)]
                    if capture_plan else None)
            rows = db.execute(sql, query_params).fetchall()
            return {"tasks": [json.loads(row[0]) for row in rows], "keys": [(row[1], row[2], row[3]) for row in rows],
                    "total": total, "status_counts": counts, "revision": self._metadata(db).get("cache_revision"),
                    "query_plan": plan}

    def archive_entry(self, task_id: str):
        """Return bounded derived location metadata, never canonical task content."""
        if not self.path.exists():
            return None
        try:
            with self._connect() as db:
                row = db.execute("""SELECT id,status,terminal_transition_at,modified,pack_path,
                    manifest_path,checksum_path,record_offset,record_length,record_sha256,pack_sha256
                    FROM archive_tasks WHERE id_fold=?""", (task_id.casefold(),)).fetchone()
                if not row:
                    return None
                keys = ("task_id", "status", "terminal_transition_at", "last_modified", "pack",
                        "manifest", "checksum", "offset", "length", "record_sha256", "pack_sha256")
                return dict(zip(keys, row))
        except sqlite3.DatabaseError:
            return None

    def archive_case_collision(self, task_id: str) -> Optional[str]:
        entry = self.archive_entry(task_id)
        return entry["task_id"] if entry and entry["task_id"] != task_id else None

    def archive_query(self, *, task_id=None, statuses=None, tags=None, before=None,
                      limit=20, offset=0, sort_order="desc", last_key=None):
        """Query only bounded cold metadata; callers verify/read selected records."""
        where, params = [], []
        if task_id:
            where.append("a.id=?"); params.append(task_id)
        if statuses:
            where.append("a.status IN (%s)" % ",".join("?" * len(statuses))); params.extend(statuses)
        if tags:
            where.append("EXISTS (SELECT 1 FROM archive_tags x WHERE x.task_id=a.id AND x.tag IN (%s))" %
                         ",".join("?" * len(tags))); params.extend(tags)
        if before:
            where.append("a.modified < ?"); params.append(before)
        predicate = " AND ".join(where) if where else "1"
        direction = "ASC" if sort_order == "asc" else "DESC"
        with self._connect() as db:
            total = db.execute("SELECT count(*) FROM archive_tasks a WHERE " + predicate, params).fetchone()[0]
            if last_key is not None:
                _, modified, last_id = last_key
                comparison = ">" if direction == "ASC" else "<"
                where.append(f"(a.modified {comparison} ? OR (a.modified=? AND a.id {comparison} ?))")
                params.extend((modified, modified, last_id))
                predicate = " AND ".join(where)
            sql = """SELECT id,status,terminal_transition_at,modified,pack_path,manifest_path,
                checksum_path,record_offset,record_length,record_sha256,pack_sha256
                FROM archive_tasks a WHERE %s ORDER BY modified %s,id %s""" % (predicate, direction, direction)
            query_params = list(params)
            if limit not in (None, 0):
                sql += " LIMIT ?"; query_params.append(limit)
            if offset:
                sql += " OFFSET ?"; query_params.append(offset)
            keys = ("task_id", "status", "terminal_transition_at", "last_modified", "pack",
                    "manifest", "checksum", "offset", "length", "record_sha256", "pack_sha256")
            rows = list(db.execute(sql, query_params))
            return {"entries": [dict(zip(keys, row)) for row in rows],
                    "keys": [(0, row[3], row[0]) for row in rows], "total": total}

    def dependency_info(self, task_id: str):
        """Indexed exact dependency/dependent lookup with recursive priority score."""
        with self._connect() as db:
            blockers = list(db.execute(
                """SELECT d.blocker_id,COALESCE(b.status,a.status) FROM dependencies d
                LEFT JOIN tasks b ON b.id=d.blocker_id
                LEFT JOIN archive_tasks a ON a.id=d.blocker_id
                WHERE d.task_id=? ORDER BY d.blocker_id""", (task_id,)))
            dependents = [row[0] for row in db.execute(
                "SELECT task_id FROM dependencies WHERE blocker_id=? ORDER BY task_id", (task_id,))]
            score = db.execute("""WITH RECURSIVE downstream(id) AS (
                SELECT task_id FROM dependencies WHERE blocker_id=?
                UNION SELECT d.task_id FROM dependencies d JOIN downstream x ON d.blocker_id=x.id)
                SELECT count(*) FROM downstream""", (task_id,)).fetchone()[0]
            return {"blockers": blockers, "dependents": dependents, "priority_score": score}

    def dependency_would_cycle(self, task_id: str, blocker_id: str) -> bool:
        """Return whether adding task_id -> blocker_id closes a dependency cycle."""
        if task_id == blocker_id:
            return True
        with self._connect() as db:
            row = db.execute("""WITH RECURSIVE blockers(id) AS (
                SELECT blocker_id FROM dependencies WHERE task_id=?
                UNION SELECT d.blocker_id FROM dependencies d JOIN blockers b ON d.task_id=b.id)
                SELECT 1 FROM blockers WHERE id=? LIMIT 1""", (blocker_id, task_id)).fetchone()
            return row is not None

    def explain_query_plan(self, **kwargs):
        """Return SQLite's plan for the exact collection query."""
        return self.query(capture_plan=True, **kwargs)["query_plan"]

    def list_page(self, limit: int = 20):
        return self.query(filters={}, limit=limit, prioritized=False)["tasks"]

    def indexed_search(self, text: str, limit: int = 20):
        return self.query(filters={"body_text": text}, limit=limit)["tasks"]
