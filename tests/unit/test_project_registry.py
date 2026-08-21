import json
import os
from pathlib import Path

import pytest

from kanban.project_registry import (
    RegistryError,
    ProjectRegistry,
    load_access_policy,
    route_to_project,
)


def source_project(tmp_path: Path, registry=None) -> Path:
    root = tmp_path / "source"
    (root / ".juno_task").mkdir(parents=True)
    (root / ".juno_task" / "config.json").write_text(
        json.dumps({"kanbanRegistry": registry or {}}), encoding="utf-8"
    )
    return root


def target_project(tmp_path: Path) -> Path:
    root = tmp_path / "target project"
    wrapper = root / ".juno_task" / "scripts" / "kanban.sh"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    wrapper.chmod(0o755)
    return root


def test_access_is_disabled_and_deny_all_by_default(tmp_path, monkeypatch):
    root = source_project(tmp_path)
    monkeypatch.delenv("YYLO_LEDGER_REGISTRY_ENABLED", raising=False)
    monkeypatch.delenv("YYLO_LEDGER_REGISTRY_ALLOWED_PROJECTS", raising=False)
    policy = load_access_policy(root)
    assert policy.enabled is False
    assert policy.allowed_projects == frozenset()


def test_environment_overrides_config_but_enablement_never_implies_allow_all(tmp_path, monkeypatch):
    root = source_project(tmp_path, {"enabled": False, "allowedProjects": ["from-config"]})
    monkeypatch.setenv("YYLO_LEDGER_REGISTRY_ENABLED", "true")
    policy = load_access_policy(root)
    assert policy.enabled is True
    assert policy.allowed_projects == frozenset({"from-config"})

    monkeypatch.setenv("YYLO_LEDGER_REGISTRY_ALLOWED_PROJECTS", "from-env,second")
    policy = load_access_policy(root)
    assert policy.allowed_projects == frozenset({"from-env", "second"})


def test_invalid_policy_fails_closed(tmp_path, monkeypatch):
    root = source_project(tmp_path, {"enabled": "yes", "allowedProjects": ["ok"]})
    with pytest.raises(RegistryError, match="enabled"):
        load_access_policy(root)
    root = source_project(tmp_path / "valid", {"enabled": False, "allowedProjects": ["ok"]})
    monkeypatch.setenv("YYLO_LEDGER_REGISTRY_ENABLED", "sometimes")
    with pytest.raises(RegistryError, match="YYLO_LEDGER_REGISTRY_ENABLED"):
        load_access_policy(root)


def test_registry_crud_is_atomic_canonical_and_never_silently_replaces(tmp_path):
    registry_path = tmp_path / "private" / "projects.json"
    registry = ProjectRegistry(registry_path)
    first = target_project(tmp_path)
    second = target_project(tmp_path / "other")

    added = registry.add("target-one", first)
    assert added["path"] == str(first.resolve())
    assert registry.get("target-one")["path"] == str(first.resolve())
    assert registry_path.stat().st_mode & 0o777 == 0o600
    assert list(registry.list()) == ["target-one"]

    with pytest.raises(RegistryError, match="already registered"):
        registry.add("target-one", second)
    registry.add("target-one", second, replace=True)
    assert registry.get("target-one")["path"] == str(second.resolve())
    registry.remove("target-one")
    with pytest.raises(RegistryError, match="not registered"):
        registry.get("target-one")


def test_malformed_registry_is_preserved_and_rejected(tmp_path):
    path = tmp_path / "projects.json"
    original = b"{broken"
    path.write_bytes(original)
    with pytest.raises(RegistryError, match="malformed"):
        ProjectRegistry(path).list()
    assert path.read_bytes() == original


def test_route_uses_target_wrapper_exact_args_and_sanitized_environment(tmp_path, monkeypatch):
    source = source_project(tmp_path, {"enabled": True, "allowedProjects": ["target"]})
    target = target_project(tmp_path)
    path = tmp_path / "projects.json"
    ProjectRegistry(path).add("target", target)
    monkeypatch.setenv("YYLO_LEDGER_REGISTRY_PATH", str(path))
    for key in (
        "JUNO_TASK_ROOT", "JUNO_CONTROLLER_BRANCH", "JUNO_CONTROLLER_SOURCE",
        "JUNO_WORKSPACE_ROLE", "JUNO_WORKSPACE_ENFORCEMENT", "VIRTUAL_ENV",
        "PYTHONPATH", "PYTHONHOME",
    ):
        monkeypatch.setenv(key, "source-value")

    seen = {}
    def fake_exec(path_value, argv, env):
        seen.update(path=path_value, argv=argv, env=env, cwd=os.getcwd())
        raise RuntimeError("stop")
    monkeypatch.setattr(os, "execve", fake_exec)

    old_cwd = os.getcwd()
    try:
        with pytest.raises(RuntimeError, match="stop"):
            route_to_project("target", ["create", "exact body"], source)
    finally:
        os.chdir(old_cwd)

    wrapper = str((target / ".juno_task/scripts/kanban.sh").resolve())
    assert seen["path"] == wrapper
    assert seen["argv"] == [wrapper, "create", "exact body"]
    assert seen["cwd"] == str(target.resolve())
    assert seen["env"]["YYLO_LEDGER_REGISTRY_HOP"] == "1"
    for key in (
        "JUNO_TASK_ROOT", "JUNO_CONTROLLER_BRANCH", "JUNO_CONTROLLER_SOURCE",
        "JUNO_WORKSPACE_ROLE", "JUNO_WORKSPACE_ENFORCEMENT", "VIRTUAL_ENV",
        "PYTHONPATH", "PYTHONHOME",
    ):
        assert key not in seen["env"]


def test_route_rejects_disabled_disallowed_missing_and_recursive_access(tmp_path, monkeypatch):
    target = target_project(tmp_path)
    path = tmp_path / "projects.json"
    ProjectRegistry(path).add("target", target)
    monkeypatch.setenv("YYLO_LEDGER_REGISTRY_PATH", str(path))

    disabled = source_project(tmp_path, {"enabled": False, "allowedProjects": ["target"]})
    with pytest.raises(RegistryError, match="disabled"):
        route_to_project("target", ["list"], disabled)

    disallowed = source_project(tmp_path / "other", {"enabled": True, "allowedProjects": []})
    with pytest.raises(RegistryError, match="not allowed"):
        route_to_project("target", ["list"], disallowed)

    allowed = source_project(tmp_path / "allowed", {"enabled": True, "allowedProjects": ["missing"]})
    with pytest.raises(RegistryError, match="not registered"):
        route_to_project("missing", ["list"], allowed)

    monkeypatch.setenv("YYLO_LEDGER_REGISTRY_HOP", "1")
    with pytest.raises(RegistryError, match="recursion"):
        route_to_project("missing", ["list"], allowed)
