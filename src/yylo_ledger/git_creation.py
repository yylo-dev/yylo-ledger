"""Bounded, path-free Git identity capture for immutable Record creation metadata."""
from __future__ import annotations

import os
import re
import select
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .records import RecordError

GIT_CREATION_SCHEMA = 1
GIT_ROLES = frozenset({"controller", "project"})
_HEX_OBJECT_ID = re.compile(r"^[0-9a-f]+$")
_REPOSITORY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class _Observation:
    role: str
    repository_key: str
    head_sha: str
    ref: Optional[str]
    worktree_dirty: bool
    repository_id: Optional[str]


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=10,
        check=False,
    )


def _dirty(root: Path) -> Optional[bool]:
    """Read at most one porcelain byte; filenames and status text are discarded."""
    try:
        process = subprocess.Popen(
            ["git", "-C", str(root), "status", "--porcelain=v1", "-z", "--untracked-files=normal"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        assert process.stdout is not None
        deadline = time.monotonic() + 10
        byte = b""
        while process.poll() is None and not byte:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill(); process.wait()
                return None
            readable, _, _ = select.select([process.stdout], [], [], remaining)
            if readable:
                byte = os.read(process.stdout.fileno(), 1)
        if not byte and process.poll() is not None:
            byte = os.read(process.stdout.fileno(), 1)
        if byte and process.poll() is None:
            process.terminate()
        try:
            code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill(); process.wait()
            return None
        return bool(byte) if code in (0, -15) else None
    except OSError:
        return None


def _observe(role: str, root: Path, repository_id: Optional[str]) -> tuple[Optional[_Observation], bool]:
    """Return (observation, unstable); absence/non-Git is not instability."""
    try:
        top = _git(root, "rev-parse", "--show-toplevel")
        common = _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
        first = _git(root, "rev-parse", "--verify", "HEAD")
        object_format = _git(root, "rev-parse", "--show-object-format")
    except (OSError, subprocess.TimeoutExpired):
        return None, False
    if any(item.returncode != 0 for item in (top, common, first, object_format)):
        return None, False  # includes non-Git and unborn HEAD
    sha = first.stdout.strip()
    expected_length = {"sha1": 40, "sha256": 64}.get(object_format.stdout.strip())
    if (not _HEX_OBJECT_ID.fullmatch(sha)
            or (expected_length is not None and len(sha) != expected_length)
            or (expected_length is None and len(sha) < 32)):
        return None, False
    try:
        ref_result = _git(root, "symbolic-ref", "-q", "HEAD")
        ref = ref_result.stdout.strip() if ref_result.returncode == 0 else None
        dirty = _dirty(root)
        if dirty is None:
            return None, False
        second = _git(root, "rev-parse", "--verify", "HEAD")
    except (OSError, subprocess.TimeoutExpired):
        return None, False
    if second.returncode != 0 or second.stdout.strip() != sha:
        return None, True
    # The key is used only for in-memory deduplication and is never serialized.
    repository_key = os.path.realpath(common.stdout.strip())
    return _Observation(role, repository_key, sha, ref, dirty, repository_id), False


def capture_creation_context(*, controller_root: Path, project_root: Optional[Path] = None,
                             required_roles: Sequence[str] = (),
                             repository_ids: Optional[Mapping[str, str]] = None) -> dict[str, Any]:
    """Capture only configured roots, merging identical repository/HEAD observations."""
    required = frozenset(required_roles)
    if not required.issubset(GIT_ROLES):
        raise RecordError("GIT_CONTEXT_REQUIREMENT_INVALID", "required Git roles must be controller or project")
    ids = dict(repository_ids or {})
    for role, value in ids.items():
        if role not in GIT_ROLES or not isinstance(value, str) or not _REPOSITORY_ID.fullmatch(value):
            raise RecordError("GIT_REPOSITORY_ID_INVALID", "repository IDs must be bounded URL-safe configured identities")

    roots = {"controller": Path(controller_root)}
    if project_root is not None:
        roots["project"] = Path(project_root)
    else:
        roots["project"] = Path(controller_root)
    observations: list[_Observation] = []
    unstable_roles = set()
    for role in ("controller", "project"):
        observation, unstable = _observe(role, roots[role], ids.get(role))
        if observation is not None:
            observations.append(observation)
        if unstable:
            unstable_roles.add(role)
    captured_roles = {item.role for item in observations}
    missing = required - captured_roles
    if missing or (required & unstable_roles):
        roles = sorted(missing | (required & unstable_roles))
        raise RecordError("GIT_CONTEXT_REQUIRED", "required Git creation context unavailable or unstable for roles: " + ", ".join(roles))

    grouped: dict[tuple[str, str], list[_Observation]] = {}
    for item in observations:
        grouped.setdefault((item.repository_key, item.head_sha), []).append(item)
    repositories = []
    for (_, sha), items in grouped.items():
        refs = {item.ref for item in items}
        ref = next(iter(refs)) if len(refs) == 1 else None
        dirty = any(item.worktree_dirty for item in items)
        roles = {item.role for item in items}
        configured_ids = {item.repository_id for item in items if item.repository_id is not None}
        if len(configured_ids) > 1:
            raise RecordError("GIT_REPOSITORY_ID_INVALID", "one repository has conflicting configured identities")
        repository_id = next(iter(configured_ids), None)
        entry: dict[str, Any] = {"roles": sorted(roles), "head_sha": sha, "worktree_dirty": dirty}
        if repository_id is not None:
            entry["repository_id"] = repository_id
        if ref is not None:
            entry["ref"] = ref
        repositories.append(entry)
    repositories.sort(key=lambda item: (item["roles"], item.get("repository_id", ""), item["head_sha"]))
    return {"git": {"schema_version": GIT_CREATION_SCHEMA, "captured_at": _timestamp(),
                    "repositories": repositories}}


def attach_creation_context(record: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(record)
    system = dict(value.get("system_metadata") or {})
    if "creation_context" in system:
        raise RecordError("SYSTEM_METADATA_RESERVED", "creation context is Ledger-owned")
    system["creation_context"] = dict(context)
    value["system_metadata"] = system
    return value
