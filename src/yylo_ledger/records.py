"""Native Record envelope and exact compare-and-replace primitives.

This module is intentionally profile-neutral.  Document and Artifact payload
implementations build on it in later delivery waves; the compatibility adapter
projects legacy Tasks without rewriting their canonical bytes.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, MutableMapping, Optional, Sequence, Tuple


RECORD_KINDS = frozenset({"task", "document", "artifact"})
SLUG_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._~-]{0,199})$")
RECORD_ID_RE = re.compile(r"^(?=.*[A-Za-z])(?=.*[0-9])[A-Za-z0-9]{6}$")
RELATION_TYPE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
PROVENANCE_FIELDS = ("actor_type", "actor", "agent", "model", "session_id", "run_id", "invocation_id")
_GIT_HEAD_RE = re.compile(r"^[0-9a-f]{32,}$")


class RecordError(ValueError):
    """Stable machine-readable Record refusal."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RevisionProvenance:
    actor_type: str
    actor: Optional[str] = None
    agent: Optional[str] = None
    model: Optional[str] = None
    session_id: Optional[str] = None
    run_id: Optional[str] = None
    invocation_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.actor_type, str) or not self.actor_type.strip():
            raise RecordError("PROVENANCE_INVALID", "actor_type is required")
        for name in PROVENANCE_FIELDS[1:]:
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise RecordError("PROVENANCE_INVALID", f"{name} must be a string or null")

    def to_dict(self) -> Dict[str, Optional[str]]:
        return {name: getattr(self, name) for name in PROVENANCE_FIELDS}


@dataclass(frozen=True)
class Relation:
    type: str
    record_id: str

    def to_dict(self) -> Dict[str, str]:
        return {"type": self.type, "record_id": self.record_id}


@dataclass
class Record:
    id: str
    slug: str
    kind: str
    title: str
    namespace: str = "default"
    profile: Optional[str] = None
    aliases: list[str] = field(default_factory=list)
    lifecycle: str = "active"
    tier: str = "hot"
    schema_version: int = 2
    media_type: str = "application/octet-stream"
    payload: Dict[str, Any] = field(default_factory=dict)
    created_date: str = ""
    last_modified: str = ""
    revision: int = 1
    relations: list[Relation] = field(default_factory=list)
    system_metadata: Dict[str, Any] = field(default_factory=dict)
    custom_metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_record(self.to_dict())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "slug": self.slug, "aliases": list(self.aliases),
            "kind": self.kind, "profile": self.profile, "title": self.title,
            "namespace": self.namespace, "lifecycle": self.lifecycle, "tier": self.tier,
            "schema_version": self.schema_version, "media_type": self.media_type,
            "payload": dict(self.payload), "created_date": self.created_date,
            "last_modified": self.last_modified, "revision": self.revision,
            "relations": [relation.to_dict() for relation in self.relations],
            "system_metadata": dict(self.system_metadata),
            "custom_metadata": dict(self.custom_metadata),
        }


def default_slug(record_id: str, text: str) -> str:
    short = re.sub(r"[^A-Za-z0-9._~-]+", "-", text.strip().lower()).strip("-._~")
    return f"{record_id}-{short[:80]}" if short else record_id


def _legacy_title(body: str) -> str:
    for line in body.splitlines():
        value = line.strip().lstrip("#").strip()
        if value:
            return value[:200]
    return "Untitled task"


def task_record_projection(task: Mapping[str, Any]) -> Dict[str, Any]:
    """Losslessly project a legacy Task as a native Record envelope.

    Existing task files are not eagerly migrated.  Native fields, when present,
    win; otherwise deterministic compatibility defaults are derived.
    """
    body = str(task.get("body") or "")
    record_id = str(task["id"])
    related = task.get("related_tasks") or []
    blocked = task.get("blocked_by") or []
    relations = task.get("relations")
    if relations is None:
        relations = ([{"type": "related", "record_id": value} for value in related] +
                     [{"type": "blocked_by", "record_id": value} for value in blocked])
    system = dict(task.get("system_metadata") or {})
    if task.get("provenance") and "provenance" not in system:
        system["provenance"] = task["provenance"]
    return {
        **dict(task),
        "id": record_id,
        "slug": task.get("slug") or default_slug(record_id, _legacy_title(body)),
        "aliases": list(task.get("aliases") or []),
        "kind": "task",
        "profile": task.get("profile"),
        "title": task.get("title") or _legacy_title(body),
        "namespace": task.get("namespace") or "default",
        # Task status remains the compatibility source for lifecycle until the
        # archive migration defines a generic lifecycle state machine.
        "lifecycle": task.get("lifecycle") or task.get("status") or "backlog",
        "tier": task.get("tier") or "hot",
        "schema_version": max(2, int(task.get("record_schema_version") or 2)),
        "media_type": task.get("media_type") or "text/markdown",
        "payload": task.get("payload") or {"backend": "inline", "field": "body"},
        "revision": max(1, int(task.get("revision") or 1)),
        "relations": relations,
        "system_metadata": system,
        "custom_metadata": dict(task.get("custom_metadata") or {}),
    }


def validate_record(record: Mapping[str, Any]) -> None:
    required = ("id", "slug", "kind", "title", "namespace", "lifecycle", "tier",
                "schema_version", "media_type", "payload", "created_date", "last_modified",
                "revision", "relations", "system_metadata", "custom_metadata")
    missing = [name for name in required if name not in record]
    if missing:
        raise RecordError("RECORD_INVALID", "missing fields: " + ", ".join(missing))
    if record["kind"] not in RECORD_KINDS:
        raise RecordError("RECORD_INVALID", f"unsupported kind {record['kind']!r}")
    for name in ("id", "title", "namespace", "lifecycle", "tier", "media_type",
                 "created_date", "last_modified"):
        if not isinstance(record[name], str):
            raise RecordError("RECORD_INVALID", f"{name} must be a string")
    if not RECORD_ID_RE.fullmatch(record["id"]):
        raise RecordError("RECORD_INVALID", "id must be an immutable 6-character Record ID")
    if not isinstance(record["slug"], str) or not SLUG_RE.fullmatch(record["slug"]):
        raise RecordError("RECORD_INVALID", "slug must be URL-safe and 1-200 bytes")
    aliases = record.get("aliases") or []
    if not isinstance(aliases, list) or any(not isinstance(v, str) or not SLUG_RE.fullmatch(v) for v in aliases):
        raise RecordError("RECORD_INVALID", "aliases must be URL-safe strings")
    if len(set([record["slug"], *aliases])) != len([record["slug"], *aliases]):
        raise RecordError("RECORD_INVALID", "slug and aliases must be unique")
    if not isinstance(record["revision"], int) or isinstance(record["revision"], bool) or record["revision"] < 1:
        raise RecordError("RECORD_INVALID", "revision must be a positive integer")
    if not isinstance(record["payload"], Mapping):
        raise RecordError("RECORD_INVALID", "payload must be an object")
    if not isinstance(record["relations"], list):
        raise RecordError("RECORD_INVALID", "relations must be a list")
    relation_keys = []
    for relation in record["relations"]:
        if (not isinstance(relation, Mapping) or not isinstance(relation.get("type"), str)
                or not isinstance(relation.get("record_id"), str)):
            raise RecordError("RECORD_INVALID", "relations require type and record_id strings")
        if not RELATION_TYPE_RE.fullmatch(relation["type"]):
            raise RecordError("RECORD_INVALID", "relation type is not a stable typed name")
        if (not RECORD_ID_RE.fullmatch(relation["record_id"])
                or relation["record_id"] == record["slug"]
                or relation["record_id"] in aliases):
            raise RecordError("RECORD_INVALID", "relations store immutable Record IDs, never slugs")
        relation_keys.append((relation["type"], relation["record_id"]))
    if len(set(relation_keys)) != len(relation_keys):
        raise RecordError("RECORD_INVALID", "duplicate typed relation")
    for name in ("system_metadata", "custom_metadata"):
        if not isinstance(record[name], Mapping):
            raise RecordError("RECORD_INVALID", f"{name} must be an object")
    creation = record["system_metadata"].get("creation_context")
    if creation is not None:
        if not isinstance(creation, Mapping) or set(creation) != {"git"}:
            raise RecordError("GIT_CONTEXT_INVALID", "creation context must contain only versioned Git metadata")
        git = creation["git"]
        if (not isinstance(git, Mapping) or set(git) != {"schema_version", "captured_at", "repositories"}
                or git.get("schema_version") != 1 or not isinstance(git.get("captured_at"), str)
                or not isinstance(git.get("repositories"), list)):
            raise RecordError("GIT_CONTEXT_INVALID", "Git creation context has an invalid envelope")
        previous = None
        seen_roles = set()
        for repository in git["repositories"]:
            allowed = {"roles", "repository_id", "head_sha", "ref", "worktree_dirty"}
            if not isinstance(repository, Mapping) or not set(repository).issubset(allowed):
                raise RecordError("GIT_CONTEXT_INVALID", "Git repository observation has unknown fields")
            roles = repository.get("roles")
            if (not isinstance(roles, list) or not roles or roles != sorted(set(roles))
                    or any(role not in {"controller", "project"} for role in roles)
                    or seen_roles.intersection(roles)):
                raise RecordError("GIT_CONTEXT_INVALID", "Git repository roles are invalid or duplicated")
            seen_roles.update(roles)
            sha = repository.get("head_sha")
            if not isinstance(sha, str) or not _GIT_HEAD_RE.fullmatch(sha):
                raise RecordError("GIT_CONTEXT_INVALID", "Git HEAD must be a full lowercase object ID")
            if not isinstance(repository.get("worktree_dirty"), bool):
                raise RecordError("GIT_CONTEXT_INVALID", "Git dirty state must be boolean")
            if "ref" in repository and (not isinstance(repository["ref"], str)
                                         or not repository["ref"].startswith("refs/")
                                         or "\n" in repository["ref"]):
                raise RecordError("GIT_CONTEXT_INVALID", "Git symbolic ref is invalid")
            if "repository_id" in repository and (not isinstance(repository["repository_id"], str)
                                                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,127}",
                                                                        repository["repository_id"])):
                raise RecordError("GIT_CONTEXT_INVALID", "configured repository identity is invalid")
            order = (roles, repository.get("repository_id", ""), sha)
            if previous is not None and order < previous:
                raise RecordError("GIT_CONTEXT_INVALID", "Git repository observations are not deterministic")
            previous = order


def value_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def payload_digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _pointer_tokens(path: str) -> list[str]:
    if path == "":
        return []
    if not path.startswith("/"):
        raise RecordError("EXACT_PATH_INVALID", "structured path must be an RFC 6901 JSON pointer")
    tokens = []
    for part in path[1:].split("/"):
        if re.search(r"~(?:[^01]|$)", part):
            raise RecordError("EXACT_PATH_INVALID", "JSON pointer contains an invalid escape")
        tokens.append(part.replace("~1", "/").replace("~0", "~"))
    return tokens


def _typed_equal(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, Mapping):
        return (left.keys() == right.keys()
                and all(_typed_equal(left[key], right[key]) for key in left))
    if isinstance(left, list):
        return len(left) == len(right) and all(_typed_equal(a, b) for a, b in zip(left, right))
    return left == right


def _resolve_parent(document: Any, path: str) -> Tuple[Any, Any]:
    tokens = _pointer_tokens(path)
    if not tokens:
        raise RecordError("EXACT_PATH_INVALID", "the Record root cannot be replaced")
    current = document
    for token in tokens[:-1]:
        if isinstance(current, MutableMapping) and token in current:
            current = current[token]
        elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
            current = current[int(token)]
        else:
            raise RecordError("EXACT_MATCH_NOT_FOUND", f"path {path!r} does not exist")
    leaf: Any = int(tokens[-1]) if isinstance(current, list) and tokens[-1].isdigit() else tokens[-1]
    return current, leaf


def exact_replace(record: MutableMapping[str, Any], *, path: str, expected: Any,
                  replacement: Any, mode: str = "structured",
                  expected_digest: Optional[str] = None) -> Dict[str, Any]:
    """Apply one exact replacement to an in-memory Record.

    Comparison is type-sensitive and byte-exact.  Callers persist only after this
    function returns, so every refusal is naturally zero-write.
    """
    if mode == "structured":
        parent, leaf = _resolve_parent(record, path)
        try:
            actual = parent[leaf]
        except (KeyError, IndexError, TypeError):
            raise RecordError("EXACT_MATCH_NOT_FOUND", f"path {path!r} does not exist")
        if not _typed_equal(actual, expected):
            raise RecordError("EXACT_MATCH_NOT_FOUND", f"value at {path!r} is not the exact typed preimage")
        before = actual
        parent[leaf] = replacement
    elif mode == "substring":
        parent, leaf = _resolve_parent(record, path)
        try:
            actual = parent[leaf]
        except (KeyError, IndexError, TypeError):
            raise RecordError("EXACT_MATCH_NOT_FOUND", f"path {path!r} does not exist")
        if not isinstance(actual, str) or not isinstance(expected, str) or not isinstance(replacement, str):
            raise RecordError("EXACT_PATH_INVALID", "substring replacement requires UTF-8 strings")
        if expected == "":
            raise RecordError("EXACT_MATCH_AMBIGUOUS", "empty text has no unique match identity")
        starts = []
        offset = 0
        while True:
            offset = actual.find(expected, offset)
            if offset < 0:
                break
            starts.append(offset)
            offset += 1
        count = len(starts)
        if count == 0:
            raise RecordError("EXACT_MATCH_NOT_FOUND", "text preimage does not occur")
        if count > 1:
            raise RecordError("EXACT_MATCH_AMBIGUOUS", f"text preimage occurs {count} times")
        before = actual
        parent[leaf] = actual.replace(expected, replacement, 1)
    elif mode == "payload":
        parent, leaf = _resolve_parent(record, path)
        try:
            actual = parent[leaf]
        except (KeyError, IndexError, TypeError):
            raise RecordError("EXACT_MATCH_NOT_FOUND", f"path {path!r} does not exist")
        if not isinstance(actual, str) or not isinstance(replacement, str):
            raise RecordError("EXACT_PATH_INVALID", "payload replacement requires UTF-8 strings")
        if expected_digest is not None and payload_digest(actual) != expected_digest:
            raise RecordError("REVISION_CONFLICT", "payload digest is stale")
        if expected is not None and (not isinstance(expected, str) or actual != expected):
            raise RecordError("EXACT_MATCH_NOT_FOUND", "whole payload preimage differs")
        before = actual
        parent[leaf] = replacement
    else:
        raise RecordError("EXACT_PATH_INVALID", f"unsupported replacement mode {mode!r}")
    after = parent[leaf]
    return {"path": path, "mode": mode, "before_sha256": value_digest(before),
            "after_sha256": value_digest(after), "expected_sha256": value_digest(expected)}
