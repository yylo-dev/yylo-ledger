"""Opt-in user registry and exact process routing for cross-project Kanban commands."""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Mapping, Sequence

from .identity import migrate_user_home

try:  # Unix is the supported runtime for distributed Juno shell wrappers.
    import fcntl
except ImportError:  # pragma: no cover - fail closed on unsupported platforms
    fcntl = None


SCHEMA_VERSION = 1
ALIAS_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
DEFAULT_LOCK_TIMEOUT_SECONDS = 2.0
REGISTRY_ENV = "YYLO_LEDGER_REGISTRY_PATH"
INVOCATION_ROOT_ENV = "YYLO_LEDGER_INVOCATION_ROOT"
HOP_ENV = "YYLO_LEDGER_REGISTRY_HOP"


class RegistryError(ValueError):
    """A fail-closed registry configuration, storage, or routing error."""


@dataclass(frozen=True)
class AccessPolicy:
    enabled: bool
    allowed_projects: frozenset[str]
    source: str


def _validate_alias(alias: object, label: str = "project alias") -> str:
    if not isinstance(alias, str) or not ALIAS_PATTERN.fullmatch(alias):
        raise RegistryError(
            f"invalid {label}: expected 1-64 lowercase letters, digits, '_' or '-'"
        )
    return alias


def _parse_enabled(value: str, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RegistryError(f"invalid {name}: expected true or false")


def _parse_allowed(value: object, label: str) -> frozenset[str]:
    if not isinstance(value, list):
        raise RegistryError(f"invalid {label}: expected an array of project aliases")
    aliases = [_validate_alias(item, label) for item in value]
    if len(set(aliases)) != len(aliases):
        raise RegistryError(f"invalid {label}: duplicate project aliases")
    return frozenset(aliases)


def source_project_root() -> Path:
    raw = os.environ.get(INVOCATION_ROOT_ENV, "").strip()
    root = Path(raw).expanduser() if raw else Path.cwd()
    try:
        root = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RegistryError(f"source project root is unavailable: {root}: {exc}") from exc
    if raw:
        return root
    for candidate in (root, *root.parents):
        if (candidate / ".juno_task" / "config.json").is_file():
            return candidate
    return root


def load_access_policy(source_root: Path) -> AccessPolicy:
    """Load environment-over-project policy; defaults are disabled and deny-all."""
    source_root = source_root.resolve()
    config_path = source_root / ".juno_task" / "config.json"
    enabled = False
    allowed = frozenset()
    source = "default"

    if config_path.exists():
        try:
            document = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RegistryError(f"source project config is malformed: {config_path}: {exc}") from exc
        if not isinstance(document, dict):
            raise RegistryError(f"source project config is malformed: root must be an object")
        section = document.get("kanbanRegistry")
        if section is not None:
            if not isinstance(section, dict):
                raise RegistryError("invalid kanbanRegistry: expected an object")
            unknown = set(section) - {"enabled", "allowedProjects"}
            if unknown:
                raise RegistryError(
                    f"invalid kanbanRegistry: unknown keys: {', '.join(sorted(unknown))}"
                )
            configured_enabled = section.get("enabled", False)
            if not isinstance(configured_enabled, bool):
                raise RegistryError("invalid kanbanRegistry.enabled: expected a boolean")
            enabled = configured_enabled
            allowed = _parse_allowed(
                section.get("allowedProjects", []), "kanbanRegistry.allowedProjects"
            )
            source = "project-config"

    env_enabled = os.environ.get("YYLO_LEDGER_REGISTRY_ENABLED")
    if env_enabled is not None:
        enabled = _parse_enabled(env_enabled, "YYLO_LEDGER_REGISTRY_ENABLED")
        source = "environment"

    env_allowed = os.environ.get("YYLO_LEDGER_REGISTRY_ALLOWED_PROJECTS")
    if env_allowed is not None:
        raw_aliases = env_allowed.split(",") if env_allowed else []
        if any(not item.strip() for item in raw_aliases):
            raise RegistryError(
                "invalid YYLO_LEDGER_REGISTRY_ALLOWED_PROJECTS: empty project alias"
            )
        aliases = [_validate_alias(item.strip(), "environment project alias") for item in raw_aliases]
        if len(set(aliases)) != len(aliases):
            raise RegistryError(
                "invalid YYLO_LEDGER_REGISTRY_ALLOWED_PROJECTS: duplicate project aliases"
            )
        allowed = frozenset(aliases)
        source = "environment"

    return AccessPolicy(enabled=enabled, allowed_projects=allowed, source=source)


def registry_path() -> Path:
    override = os.environ.get(REGISTRY_ENV, "").strip()
    return (
        Path(override).expanduser().resolve()
        if override
        else migrate_user_home() / "projects.json"
    )


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_target(path: Path) -> Path:
    try:
        target = path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RegistryError(f"registered target no longer exists: {path}: {exc}") from exc
    if not target.is_dir() or not (target / ".juno_task").is_dir():
        raise RegistryError(f"target is not an initialized Juno project: {target}")
    wrapper = target / ".juno_task" / "scripts" / "kanban.sh"
    if not wrapper.is_file() or not os.access(wrapper, os.X_OK):
        raise RegistryError(f"target Kanban wrapper is missing or not executable: {wrapper}")
    return target


class ProjectRegistry:
    def __init__(self, path: Path | None = None, lock_timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS):
        self.path = (path or registry_path()).expanduser().resolve()
        self.lock_timeout = lock_timeout

    def _empty(self) -> dict[str, object]:
        return {"schemaVersion": SCHEMA_VERSION, "projects": {}}

    def _load(self) -> dict[str, object]:
        if not self.path.exists():
            return self._empty()
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RegistryError(f"project registry is malformed: {self.path}: {exc}") from exc
        if not isinstance(document, dict) or set(document) != {"schemaVersion", "projects"}:
            raise RegistryError("project registry is malformed: expected schemaVersion and projects")
        if document["schemaVersion"] != SCHEMA_VERSION or not isinstance(document["projects"], dict):
            raise RegistryError(f"project registry is malformed or unsupported: {self.path}")
        for alias, entry in document["projects"].items():
            _validate_alias(alias)
            if not isinstance(entry, dict) or set(entry) != {"path", "createdAt", "updatedAt"}:
                raise RegistryError(f"project registry is malformed: invalid entry for {alias}")
            if not all(isinstance(entry[key], str) and entry[key] for key in entry):
                raise RegistryError(f"project registry is malformed: invalid entry values for {alias}")
            if not Path(entry["path"]).is_absolute():
                raise RegistryError(f"project registry is malformed: path for {alias} is not absolute")
        return document

    @contextmanager
    def _locked(self) -> Iterator[None]:
        if fcntl is None:
            raise RegistryError("project registry locking is unsupported on this platform")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + self.lock_timeout
        try:
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RegistryError(
                            f"timed out waiting for project registry lock: {lock_path}"
                        )
                    time.sleep(0.025)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _save(self, document: Mapping[str, object]) -> None:
        payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def list(self) -> dict[str, dict[str, str]]:
        projects = self._load()["projects"]
        assert isinstance(projects, dict)
        return {alias: dict(projects[alias]) for alias in sorted(projects)}

    def get(self, alias: str) -> dict[str, str]:
        alias = _validate_alias(alias)
        projects = self.list()
        if alias not in projects:
            raise RegistryError(f"project alias is not registered: {alias}")
        return projects[alias]

    def add(self, alias: str, path: Path, replace: bool = False) -> dict[str, str]:
        alias = _validate_alias(alias)
        target = _validate_target(path)
        with self._locked():
            document = self._load()
            projects = document["projects"]
            assert isinstance(projects, dict)
            existing = projects.get(alias)
            if existing and not replace:
                raise RegistryError(
                    f"project alias is already registered: {alias}; use --replace to change it"
                )
            now = _timestamp()
            entry = {
                "path": str(target),
                "createdAt": existing.get("createdAt", now) if isinstance(existing, dict) else now,
                "updatedAt": now,
            }
            projects[alias] = entry
            self._save(document)
            return entry

    def remove(self, alias: str) -> dict[str, str]:
        alias = _validate_alias(alias)
        with self._locked():
            document = self._load()
            projects = document["projects"]
            assert isinstance(projects, dict)
            if alias not in projects:
                raise RegistryError(f"project alias is not registered: {alias}")
            removed = projects.pop(alias)
            self._save(document)
            assert isinstance(removed, dict)
            return dict(removed)


def require_enabled(source_root: Path) -> AccessPolicy:
    policy = load_access_policy(source_root)
    if not policy.enabled:
        raise RegistryError(
            "cross-project Kanban registry is disabled in the source project"
        )
    return policy


def route_to_project(alias: str, forwarded_args: Sequence[str], source_root: Path) -> None:
    """Replace this process with the destination wrapper, preserving stdio exactly."""
    if os.environ.get(HOP_ENV):
        raise RegistryError("cross-project Kanban routing recursion rejected")
    alias = _validate_alias(alias)
    source_root = source_root.resolve(strict=True)
    policy = require_enabled(source_root)
    if alias not in policy.allowed_projects:
        raise RegistryError(f"project alias is not allowed by source project: {alias}")
    entry = ProjectRegistry().get(alias)
    target = _validate_target(Path(entry["path"]))
    if target == source_root:
        raise RegistryError(f"cross-project alias resolves to the source project: {alias}")

    wrapper = (target / ".juno_task" / "scripts" / "kanban.sh").resolve()
    environment = dict(os.environ)
    source_venv = environment.get("VIRTUAL_ENV")
    if source_venv and environment.get("PATH"):
        source_bin = str((Path(source_venv).expanduser() / "bin").resolve())
        environment["PATH"] = os.pathsep.join(
            entry for entry in environment["PATH"].split(os.pathsep)
            if entry and str(Path(entry).expanduser().resolve()) != source_bin
        )
    for key in (
        "JUNO_TASK_ROOT",
        "JUNO_CONTROLLER_BRANCH",
        "JUNO_CONTROLLER_SOURCE",
        "JUNO_WORKSPACE_ROLE",
        "JUNO_WORKSPACE_ENFORCEMENT",
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "PYTHONHOME",
    ):
        environment.pop(key, None)
    environment[HOP_ENV] = "1"
    environment[INVOCATION_ROOT_ENV] = str(target)
    os.chdir(target)
    os.execve(str(wrapper), [str(wrapper), *forwarded_args], environment)
