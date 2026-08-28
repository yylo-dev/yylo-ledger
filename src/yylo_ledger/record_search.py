"""Disposable SQLite/FTS discovery for canonical Ledger Records.

The index stores candidate metadata only.  Every returned Record is re-read through
``canonical_reader`` and digest-checked, so SQLite is never authoritative.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence

from .records import RecordError, task_record_projection, validate_record, value_digest

RECORD_SEARCH_SCHEMA = 1
SCOPES = frozenset({"hot", "archive", "all"})
PROJECTIONS = frozenset({"metadata", "summary", "full"})
_PROVENANCE = frozenset({"actor_type", "actor", "agent", "model", "session_id", "run_id", "invocation_id"})
_SAFE_SENSITIVE_FIELDS = frozenset({"id", "kind", "profile", "namespace", "lifecycle", "tier",
                                    "schema_version", "media_type", "created_date", "last_modified", "revision"})
_METADATA_FIELDS = _SAFE_SENSITIVE_FIELDS | frozenset({"slug", "aliases", "title", "relations",
                                                       "system_metadata", "custom_metadata"})
_SECRET_KEY = re.compile(r"(?i)(?:password|passwd|secret|api[_-]?key|access[_-]?token|authorization)")
_SECRET_VALUE = re.compile(
    r"(?i)(?:bearer\s+\S+|-----BEGIN .*PRIVATE KEY-----|gh[oprsu]_[A-Za-z0-9]{30,}|"
    r"(?:password|passwd|secret|api[_-]?key|access[_-]?token)\s*[:=]\s*\S+|"
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b)"
)


def _plain(value: Any) -> Any:
    return json.loads(json.dumps(value, default=lambda item: item.isoformat()))


def _json_path(value: Mapping[str, Any], path: str) -> Any:
    """Resolve an allowlisted dotted path while permitting dotted namespace keys."""
    current: Any = value
    parts = path.split(".")
    offset = 0
    while offset < len(parts):
        if not isinstance(current, Mapping):
            return None
        match = None
        for end in range(len(parts), offset, -1):
            candidate = ".".join(parts[offset:end])
            if candidate in current:
                match = (end, candidate)
                break
        if match is None:
            return None
        offset, key = match
        current = current[key]
    return current


def _values(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple, set, frozenset)) else [value]


def _sensitive(record: Mapping[str, Any]) -> bool:
    system = record.get("system_metadata") or {}
    custom = record.get("custom_metadata") or {}
    return bool(system.get("sensitive") or custom.get("sensitive") or
                str(system.get("classification", "")).casefold() in {"secret", "restricted"})


def _approved_text(record: Mapping[str, Any], maximum: int) -> str:
    if _sensitive(record):
        return ""
    media = str(record.get("media_type") or "").split(";", 1)[0].strip().casefold()
    if not (media.startswith("text/") or media in {"application/json", "application/yaml", "application/x-yaml"}):
        return ""
    if record.get("kind") == "task":
        text = "\n".join((str(record.get("body") or ""), str(record.get("agent_response") or "")))
    else:
        payload = record.get("payload") or {}
        text = payload.get("text")
        if text is None and payload.get("backend") == "inline" and payload.get("encoding") == "base64":
            try:
                text = base64.b64decode(payload.get("data", ""), validate=True).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return ""
        text = text if isinstance(text, str) else ""
    return text[:maximum]


def _provenance(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    system = record.get("system_metadata") or {}
    values: list[Mapping[str, Any]] = []
    direct = system.get("provenance") or record.get("provenance")
    if isinstance(direct, Mapping):
        values.append(direct)
    revisions = system.get("revision_provenance") or []
    if isinstance(revisions, list):
        values.extend(item for item in revisions if isinstance(item, Mapping))
    return values


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: "[REDACTED]" if _SECRET_KEY.search(str(key)) else _redact(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _SECRET_VALUE.sub("[REDACTED]", value)
    return value


@dataclass(frozen=True)
class IndexedRecord:
    """One canonical snapshot and its opaque re-read locator."""
    record: Mapping[str, Any]
    tier: str = "hot"
    locator: str = ""


@dataclass(frozen=True)
class RecordSearchPolicy:
    custom_metadata_paths: frozenset[str] = field(default_factory=frozenset)
    max_indexed_text_bytes: int = 1024 * 1024
    max_candidates: int = 1000
    max_page_size: int = 100
    max_output_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        if min(self.max_indexed_text_bytes, self.max_candidates, self.max_page_size, self.max_output_bytes) <= 0:
            raise RecordError("SEARCH_POLICY_INVALID", "search bounds must be positive")
        for path in self.custom_metadata_paths:
            if not isinstance(path, str) or not path or path.startswith(".") or ".." in path:
                raise RecordError("SEARCH_POLICY_INVALID", "custom metadata allowlist contains an invalid path")


@dataclass(frozen=True)
class RecordSearchQuery:
    scope: str = "hot"
    ids: Sequence[str] = ()
    slug: Optional[str] = None
    kinds: Sequence[str] = ()
    profiles: Sequence[str] = ()
    namespaces: Sequence[str] = ()
    lifecycles: Sequence[str] = ()
    media_types: Sequence[str] = ()
    text: Optional[str] = None
    tags: Sequence[str] = ()
    relation_type: Optional[str] = None
    relation_id: Optional[str] = None
    digest: Optional[str] = None
    backend: Optional[str] = None
    size_min: Optional[int] = None
    size_max: Optional[int] = None
    created_after: Optional[str] = None
    created_before: Optional[str] = None
    modified_after: Optional[str] = None
    modified_before: Optional[str] = None
    provenance: Mapping[str, str] = field(default_factory=dict)
    creation_git: Mapping[str, Any] = field(default_factory=dict)
    custom_equals: Mapping[str, Any] = field(default_factory=dict)
    limit: int = 20
    cursor: Optional[str] = None
    projection: str = "summary"
    fields: Sequence[str] = ()
    sort_order: str = "desc"
    output_byte_budget: Optional[int] = None


@dataclass(frozen=True)
class RecordSearchPage:
    records: list[Dict[str, Any]]
    next_cursor: Optional[str]
    index_revision: str
    candidates_examined: int
    output_bytes: int


CanonicalReader = Callable[[str, str, str], Mapping[str, Any]]


class RecordSearchIndex:
    """Rebuildable unified Record index with canonical verification."""

    def __init__(self, path: Path, *, policy: Optional[RecordSearchPolicy] = None,
                 record_source: Optional[Callable[[], Iterable[IndexedRecord]]] = None):
        self.path = Path(path)
        self.policy = policy or RecordSearchPolicy()
        self.record_source = record_source

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _canonical_record(value: Mapping[str, Any], tier: str) -> Dict[str, Any]:
        record = task_record_projection(value) if "kind" not in value else dict(value)
        record["tier"] = tier
        validate_record(record)
        return _plain(record)

    def rebuild(self, records: Iterable[IndexedRecord]) -> str:
        """Replace the disposable index from canonical hot/cold snapshots."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        cursor_secret = None
        if self.path.exists():
            try:
                with self._connect() as current:
                    cursor_secret = self._metadata(current).get("cursor_secret")
            except (RecordError, sqlite3.DatabaseError):
                pass
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temporary.unlink(missing_ok=True)
        db = sqlite3.connect(temporary)
        try:
            db.executescript("""
            CREATE TABLE records (
              id TEXT PRIMARY KEY, id_fold TEXT NOT NULL UNIQUE, slug TEXT NOT NULL,
              kind TEXT NOT NULL, profile TEXT, namespace TEXT NOT NULL, lifecycle TEXT NOT NULL,
              tier TEXT NOT NULL, media_type TEXT NOT NULL, title TEXT NOT NULL,
              digest TEXT, size INTEGER, backend TEXT, created TEXT NOT NULL, modified TEXT NOT NULL,
              sensitive INTEGER NOT NULL, locator TEXT NOT NULL, canonical_sha256 TEXT NOT NULL
            );
            CREATE TABLE aliases(record_id TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(record_id,value));
            CREATE TABLE tags(record_id TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(record_id,value));
            CREATE TABLE relations(record_id TEXT NOT NULL, type TEXT NOT NULL, target_id TEXT NOT NULL,
                                   PRIMARY KEY(record_id,type,target_id));
            CREATE TABLE provenance(record_id TEXT NOT NULL, name TEXT NOT NULL, value TEXT NOT NULL,
                                    PRIMARY KEY(record_id,name,value));
            CREATE TABLE creation_git(record_id TEXT NOT NULL, role TEXT NOT NULL, head_sha TEXT NOT NULL,
                                      ref TEXT, dirty INTEGER, repository_id TEXT,
                                      PRIMARY KEY(record_id,role,head_sha));
            CREATE TABLE custom_values(record_id TEXT NOT NULL, path TEXT NOT NULL, value_json TEXT NOT NULL,
                                       PRIMARY KEY(record_id,path));
            CREATE VIRTUAL TABLE record_text USING fts5(record_id UNINDEXED,title,text,
                tokenize='trigram case_sensitive 0',detail='none');
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE INDEX records_filter ON records(tier,kind,profile,modified,id);
            CREATE INDEX records_slug ON records(slug);
            CREATE INDEX alias_lookup ON aliases(value,record_id);
            CREATE INDEX tag_lookup ON tags(value,record_id);
            CREATE INDEX relation_lookup ON relations(type,target_id,record_id);
            CREATE INDEX provenance_lookup ON provenance(name,value,record_id);
            CREATE INDEX creation_git_lookup ON creation_git(role,head_sha,record_id);
            CREATE INDEX custom_lookup ON custom_values(path,value_json,record_id);
            """)
            digests: list[tuple[str, str]] = []
            for source in records:
                if source.tier not in {"hot", "archive"}:
                    raise RecordError("SEARCH_SCOPE_INVALID", "indexed tier must be hot or archive")
                record = self._canonical_record(source.record, source.tier)
                record_id = record["id"]
                canonical_sha = value_digest(record)
                payload = record.get("payload") or {}
                digest = payload.get("sha256") or payload.get("digest")
                size = payload.get("size")
                backend = payload.get("backend")
                db.execute("INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    record_id, record_id.casefold(), record["slug"], record["kind"], record.get("profile"),
                    record["namespace"], record["lifecycle"], source.tier, record["media_type"], record["title"],
                    digest, size if isinstance(size, int) and not isinstance(size, bool) else None, backend,
                    record["created_date"], record["last_modified"], int(_sensitive(record)), source.locator,
                    canonical_sha))
                db.executemany("INSERT INTO aliases VALUES (?,?)", ((record_id, str(v)) for v in record.get("aliases") or []))
                tag_values = record.get("tags") or record.get("feature_tags") or []
                db.executemany("INSERT INTO tags VALUES (?,?)", ((record_id, str(v)) for v in tag_values))
                db.executemany("INSERT INTO relations VALUES (?,?,?)", ((record_id, item["type"], item["record_id"])
                               for item in record.get("relations") or []))
                for item in _provenance(record):
                    db.executemany("INSERT OR IGNORE INTO provenance VALUES (?,?,?)",
                                   ((record_id, name, str(item[name])) for name in _PROVENANCE if item.get(name) is not None))
                creation = (record.get("system_metadata") or {}).get("creation_context", {}).get("git", {})
                for repository in creation.get("repositories", []) if isinstance(creation, Mapping) else []:
                    if not isinstance(repository, Mapping) or not repository.get("head_sha"):
                        continue
                    for role in repository.get("roles") or []:
                        db.execute("INSERT OR IGNORE INTO creation_git VALUES (?,?,?,?,?,?)", (
                            record_id, str(role), str(repository["head_sha"]), repository.get("ref"),
                            int(repository["worktree_dirty"]) if isinstance(repository.get("worktree_dirty"), bool) else None,
                            repository.get("repository_id")))
                custom = record.get("custom_metadata") or {}
                for path in self.policy.custom_metadata_paths:
                    value = _json_path(custom, path)
                    if value is not None:
                        db.execute("INSERT INTO custom_values VALUES (?,?,?)",
                                   (record_id, path, json.dumps(value, sort_keys=True, separators=(",", ":"))))
                db.execute("INSERT INTO record_text VALUES (?,?,?)",
                           (record_id, "" if _sensitive(record) else record["title"],
                            _approved_text(record, self.policy.max_indexed_text_bytes)))
                digests.append((record_id, canonical_sha))
            revision = hashlib.sha256(json.dumps(sorted(digests), separators=(",", ":")).encode()).hexdigest()
            db.executemany("INSERT INTO metadata VALUES (?,?)", (("schema_version", str(RECORD_SEARCH_SCHEMA)),
                           ("revision", revision), ("cursor_secret", cursor_secret or secrets.token_hex(32))))
            db.commit()
        except BaseException:
            db.close(); temporary.unlink(missing_ok=True)
            raise
        db.close()
        os.replace(temporary, self.path)
        return revision

    def ensure(self) -> str:
        """Return the current revision, rebuilding missing/corrupt state when configured."""
        try:
            if not self.path.exists():
                raise RecordError("SEARCH_INDEX_MISSING", "disposable index is missing")
            with self._connect() as db:
                return self._metadata(db)["revision"]
        except (RecordError, sqlite3.DatabaseError):
            if self.record_source is None:
                raise RecordError("SEARCH_INDEX_MISSING", "disposable index must be rebuilt from canonical storage")
            return self.rebuild(self.record_source())

    def _metadata(self, db: sqlite3.Connection) -> Dict[str, str]:
        try:
            values = dict(db.execute("SELECT key,value FROM metadata"))
        except sqlite3.DatabaseError as exc:
            raise RecordError("SEARCH_INDEX_INVALID", "disposable index must be rebuilt") from exc
        if values.get("schema_version") != str(RECORD_SEARCH_SCHEMA):
            raise RecordError("SEARCH_INDEX_STALE", "disposable index schema changed; rebuild required")
        return values

    @staticmethod
    def _query_identity(query: RecordSearchQuery) -> str:
        values = dict(query.__dict__)
        values.pop("cursor", None)
        return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":"), default=list).encode()).hexdigest()

    def _cursor(self, metadata: Mapping[str, str], query_hash: str, key: tuple[str, str]) -> str:
        payload = {"v": 1, "revision": metadata["revision"], "query": query_hash, "key": list(key)}
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        signature = hmac.new(bytes.fromhex(metadata["cursor_secret"]), raw, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(raw + signature).decode().rstrip("=")

    def _decode_cursor(self, token: str, metadata: Mapping[str, str], query_hash: str) -> tuple[str, str]:
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            payload_raw, signature = raw[:-32], raw[-32:]
            expected = hmac.new(bytes.fromhex(metadata["cursor_secret"]), payload_raw, hashlib.sha256).digest()
            payload = json.loads(payload_raw)
            if not hmac.compare_digest(signature, expected) or payload.get("v") != 1:
                raise ValueError
            if payload.get("revision") != metadata["revision"]:
                raise RecordError("SEARCH_CURSOR_STALE", "cursor index revision is no longer current")
            if payload.get("query") != query_hash:
                raise RecordError("SEARCH_CURSOR_MISMATCH", "cursor belongs to a different query")
            key = payload["key"]
            if not isinstance(key, list) or len(key) != 2 or not all(isinstance(v, str) for v in key):
                raise ValueError
            return key[0], key[1]
        except RecordError:
            raise
        except Exception as exc:
            raise RecordError("SEARCH_CURSOR_INVALID", "cursor is malformed or untrusted") from exc

    def _validate_query(self, query: RecordSearchQuery) -> None:
        if query.scope not in SCOPES:
            raise RecordError("SEARCH_SCOPE_INVALID", "scope must be hot, archive, or all")
        if query.projection not in PROJECTIONS:
            raise RecordError("SEARCH_PROJECTION_INVALID", "projection must be metadata, summary, or full")
        if query.sort_order not in {"asc", "desc"}:
            raise RecordError("SEARCH_SORT_INVALID", "sort order must be asc or desc")
        if not isinstance(query.limit, int) or isinstance(query.limit, bool) or query.limit < 1 or query.limit > self.policy.max_page_size:
            raise RecordError("SEARCH_LIMIT_INVALID", "page limit exceeds the configured bound")
        unknown = set(query.custom_equals) - self.policy.custom_metadata_paths
        if unknown:
            raise RecordError("SEARCH_FIELD_UNINDEXED", "custom metadata query path is not allowlisted")
        unknown_provenance = set(query.provenance) - _PROVENANCE
        if unknown_provenance:
            raise RecordError("SEARCH_FILTER_INVALID", "unsupported provenance filter")
        if query.output_byte_budget is not None and (not isinstance(query.output_byte_budget, int) or query.output_byte_budget <= 0):
            raise RecordError("SEARCH_BUDGET_INVALID", "output byte budget must be positive")

    def search(self, query: RecordSearchQuery, canonical_reader: CanonicalReader) -> RecordSearchPage:
        self._validate_query(query)
        self.ensure()
        where: list[str] = []
        params: list[Any] = []
        joins: list[str] = []
        if query.scope != "all": where.append("r.tier=?"); params.append(query.scope)
        for column, values in (("id", query.ids), ("kind", query.kinds), ("profile", query.profiles),
                               ("namespace", query.namespaces), ("lifecycle", query.lifecycles),
                               ("media_type", query.media_types)):
            values = _values(values)
            if values:
                where.append(f"r.{column} IN ({','.join('?' * len(values))})"); params.extend(values)
        if query.slug:
            where.append("(r.slug=? OR EXISTS(SELECT 1 FROM aliases a WHERE a.record_id=r.id AND a.value=?))")
            params.extend((query.slug, query.slug))
        if query.tags:
            values = list(query.tags); where.append(f"EXISTS(SELECT 1 FROM tags t WHERE t.record_id=r.id AND t.value IN ({','.join('?' * len(values))}))"); params.extend(values)
        if query.relation_type or query.relation_id:
            clauses = ["x.record_id=r.id"]
            if query.relation_type: clauses.append("x.type=?"); params.append(query.relation_type)
            if query.relation_id: clauses.append("x.target_id=?"); params.append(query.relation_id)
            where.append("EXISTS(SELECT 1 FROM relations x WHERE " + " AND ".join(clauses) + ")")
        for column, value in (("digest", query.digest), ("backend", query.backend)):
            if value is not None: where.append(f"r.{column}=?"); params.append(value)
        for expression, value in (("r.size>=?", query.size_min), ("r.size<=?", query.size_max),
                                  ("r.created>?", query.created_after), ("r.created<?", query.created_before),
                                  ("r.modified>?", query.modified_after), ("r.modified<?", query.modified_before)):
            if value is not None: where.append(expression); params.append(value)
        for name, value in query.provenance.items():
            where.append("EXISTS(SELECT 1 FROM provenance p WHERE p.record_id=r.id AND p.name=? AND p.value=?)")
            params.extend((name, str(value)))
        creation_git_columns = {"role": "role", "head_sha": "head_sha", "ref": "ref",
                                "worktree_dirty": "dirty", "repository_id": "repository_id"}
        for name, value in query.creation_git.items():
            column = creation_git_columns.get(name)
            if column is None:
                raise RecordError("SEARCH_FILTER_INVALID", "unsupported creation Git filter")
            where.append(f"EXISTS(SELECT 1 FROM creation_git g WHERE g.record_id=r.id AND g.{column}=?)")
            params.append(int(value) if name == "worktree_dirty" and isinstance(value, bool) else value)
        for path, value in query.custom_equals.items():
            where.append("EXISTS(SELECT 1 FROM custom_values c WHERE c.record_id=r.id AND c.path=? AND c.value_json=?)")
            params.extend((path, json.dumps(value, sort_keys=True, separators=(",", ":"))))
        if query.text:
            term = str(query.text)
            if len(term) >= 3:
                trigrams = dict.fromkeys(term[index:index + 3].lower() for index in range(len(term) - 2))
                expression = " AND ".join('"' + value.replace('"', '""') + '"' for value in trigrams)
                joins.append("JOIN record_text f ON f.record_id=r.id AND record_text MATCH ?")
                params.insert(0, expression)
                where.append("(instr(lower(r.title),lower(?))>0 OR instr(lower(f.text),lower(?))>0)")
                params.extend((term, term))
            else:
                where.append("(instr(lower(r.title),lower(?))>0 OR EXISTS(SELECT 1 FROM record_text f WHERE f.record_id=r.id AND instr(lower(f.text),lower(?))>0))")
                params.extend((term, term))
        predicate = " AND ".join(where) if where else "1"
        direction = "ASC" if query.sort_order == "asc" else "DESC"
        query_hash = self._query_identity(query)
        with self._connect() as db:
            metadata = self._metadata(db)
            last = self._decode_cursor(query.cursor, metadata, query_hash) if query.cursor else None
            key_predicate = ""
            if last:
                comparator = ">" if direction == "ASC" else "<"
                key_predicate = f" AND (r.modified {comparator} ? OR (r.modified=? AND r.id {comparator} ?))"
                params.extend((last[0], last[0], last[1]))
            candidate_limit = min(self.policy.max_candidates, max(query.limit + 1, query.limit * 4))
            sql = ("SELECT r.* FROM records r " + " ".join(joins) + " WHERE " + predicate + key_predicate +
                   f" ORDER BY r.modified {direction},r.id {direction} LIMIT ?")
            params.append(candidate_limit)
            rows = list(db.execute(sql, params))
        records: list[Dict[str, Any]] = []
        output_bytes = 2
        consumed: Optional[tuple[str, str]] = None
        examined = 0
        budget = min(query.output_byte_budget or self.policy.max_output_bytes, self.policy.max_output_bytes)
        for row in rows:
            if len(records) >= query.limit:
                break
            examined += 1
            candidate_key = (row["modified"], row["id"])
            try:
                canonical = self._canonical_record(canonical_reader(row["tier"], row["locator"], row["id"]), row["tier"])
            except RecordError:
                raise
            except Exception as exc:
                raise RecordError("SEARCH_CANONICAL_UNAVAILABLE", "canonical Record could not be verified") from exc
            if value_digest(canonical) != row["canonical_sha256"]:
                raise RecordError("SEARCH_INDEX_STALE", "candidate differs from canonical storage; rebuild required")
            if row["sensitive"]:
                projected = {key: canonical[key] for key in _SAFE_SENSITIVE_FIELDS if key in canonical}
                projected["sensitive"] = True
            elif query.projection == "full":
                projected = dict(canonical)
            elif query.projection == "metadata":
                projected = {key: canonical[key] for key in _METADATA_FIELDS if key in canonical}
            else:
                projected = dict(canonical)
                payload = projected.get("payload")
                if isinstance(payload, Mapping):
                    projected["payload"] = {key: value for key, value in payload.items() if key not in {"text", "data"}}
                projected.pop("body", None); projected.pop("agent_response", None)
            projected = _redact(projected)
            if query.fields:
                unknown = set(query.fields) - set(projected)
                if unknown:
                    raise RecordError("SEARCH_PROJECTION_INVALID", "requested field is unavailable in the selected projection")
                projected = {key: projected[key] for key in query.fields}
                if "id" not in projected: projected = {"id": canonical["id"], **projected}
            encoded_size = len(json.dumps(projected, ensure_ascii=False, separators=(",", ":")).encode())
            separator = 1 if records else 0
            if output_bytes + separator + encoded_size > budget:
                if not records:
                    raise RecordError("SEARCH_OUTPUT_BUDGET", "one projected Record exceeds the output byte budget")
                break
            records.append(projected); output_bytes += separator + encoded_size
            consumed = candidate_key
        has_more = len(rows) > examined or (examined == candidate_limit and bool(rows))
        next_cursor = self._cursor(metadata, query_hash, consumed) if consumed and has_more else None
        return RecordSearchPage(records, next_cursor, metadata["revision"], examined, output_bytes)
