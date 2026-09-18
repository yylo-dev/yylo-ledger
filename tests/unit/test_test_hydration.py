"""Offline tests for the task-local dependency preparation boundary."""
import importlib.util
from pathlib import Path
import subprocess
import time

import pytest

SPEC = importlib.util.spec_from_file_location(
    "ledger_test_hydration", Path(__file__).parents[2] / "scripts/hydrate_tests.py")
hydration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hydration)


@pytest.fixture
def project(tmp_path, monkeypatch):
    env = tmp_path / ".venv"
    lock = tmp_path / "requirements-test.lock"
    lock.write_text("pytest==8.3.5\n")
    for name, value in {"ROOT": tmp_path, "LOCK": lock, "ENV": env,
                        "STAMP": env / ".requirements-test.sha256"}.items():
        monkeypatch.setattr(hydration, name, value)
    return tmp_path


def test_missing_environment_check_never_creates_it(project):
    with pytest.raises(hydration.HydrationError, match="missing"):
        hydration.verify(time.monotonic() + 10, hydration.identity())
    assert not hydration.ENV.exists()


@pytest.mark.parametrize("name", ["ENV", "LOCK", "STAMP"])
def test_symbolic_inputs_are_refused(project, name):
    path = getattr(hydration, name)
    if path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(project / "outside")
    with pytest.raises(hydration.HydrationError, match="symbolic"):
        hydration.identity()


def test_non_environment_is_preserved(project):
    hydration.ENV.mkdir()
    marker = hydration.ENV / "owner-bytes"
    marker.write_text("preserve")
    with pytest.raises(hydration.HydrationError, match="not a virtualenv"):
        hydration.hydrate(time.monotonic() + 10, hydration.identity())
    assert marker.read_text() == "preserve"


def test_shared_environment_is_preserved(project):
    hydration.ENV.mkdir()
    config = hydration.ENV / "pyvenv.cfg"
    config.write_text("include-system-site-packages = true\n")
    with pytest.raises(hydration.HydrationError, match="shares site packages"):
        hydration.hydrate(time.monotonic() + 10, hydration.identity())
    assert config.read_text() == "include-system-site-packages = true\n"


def test_successful_probe_does_not_install(project, monkeypatch):
    monkeypatch.setattr(hydration, "verify", lambda *args, **kwargs: None)
    monkeypatch.setattr(hydration, "run", lambda *args, **kwargs: pytest.fail("unexpected install"))
    hydration.hydrate(time.monotonic() + 10, hydration.identity())


def test_failed_install_invalidates_stamp_and_uses_hashed_lock(project, monkeypatch):
    hydration.ENV.mkdir()
    (hydration.ENV / "pyvenv.cfg").write_text("include-system-site-packages = false\n")
    hydration.STAMP.write_text("old-lock\n")
    calls = []

    def run(args, deadline, **kwargs):
        calls.append(args)
        if "sys.base_prefix" in args[-1]:
            return str(hydration.ENV) + "\n/system-python\n"
        assert "--require-hashes" in args
        assert "--only-binary=:all:" in args
        assert str(hydration.LOCK) in args
        raise hydration.HydrationError("injected install failure")

    monkeypatch.setattr(hydration, "run", run)
    with pytest.raises(hydration.HydrationError, match="injected install failure"):
        hydration.hydrate(time.monotonic() + 10, hydration.identity())
    assert len(calls) == 2
    assert not hydration.STAMP.exists()


def test_borrowed_interpreter_is_refused_before_install(project, monkeypatch):
    hydration.ENV.mkdir()
    (hydration.ENV / "pyvenv.cfg").write_text("include-system-site-packages = false\n")
    monkeypatch.setattr(hydration, "run", lambda *args, **kwargs: "/other/venv\n/system-python\n")
    with pytest.raises(hydration.HydrationError, match="not task-local"):
        hydration.hydrate(time.monotonic() + 10, hydration.identity())


def test_expired_budget_never_spawns(project, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("unexpected spawn"))
    with pytest.raises(hydration.HydrationError, match="exceeded"):
        hydration.run(["unused"], time.monotonic() - 1)
