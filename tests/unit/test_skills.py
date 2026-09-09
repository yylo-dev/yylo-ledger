import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from yylo_ledger.cli import ExitCode, TaskCLI
from yylo_ledger.skills import (DESTINATIONS, SkillInstallError, install,
                                resolve_version, status)


def completed(argv, stdout=""):
    return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")


def write_skill(root: Path, body: str = "canonical"):
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text("---\nname: kanban-workflow\n---\n" + body + "\n")


class FakeRunner:
    def __init__(self, npx=True, git=True, body="canonical", npx_extra=False):
        self.npx = npx
        self.git = git
        self.body = body
        self.npx_extra = npx_extra
        self.calls = []

    def __call__(self, argv, cwd=None):
        self.calls.append((list(argv), Path(cwd) if cwd else None))
        if argv[:3] == ["git", "ls-remote", "--tags"]:
            if "--refs" in argv:
                return completed(argv, "a refs/tags/v1.0.0\nb refs/tags/v2.0.0-rc.1\nc refs/tags/v1.2.0\n")
            return completed(argv, "a refs/tags/v1.0.0\n")
        if argv[0] == "npx":
            if not self.npx:
                raise FileNotFoundError("npx")
            for destination in DESTINATIONS:
                write_skill(Path(cwd) / destination, self.body)
                if self.npx_extra:
                    write_skill(Path(cwd) / destination.parent / "unexpected", "extra")
            return completed(argv)
        if argv[:2] == ["git", "clone"]:
            if not self.git:
                raise subprocess.CalledProcessError(1, argv)
            write_skill(Path(argv[-1]) / "skills/kanban-workflow", self.body)
            return completed(argv)
        raise AssertionError(argv)


def test_resolves_latest_stable_and_exact_version():
    runner = FakeRunner()
    assert resolve_version(None, runner) == "v1.2.0"
    assert resolve_version("1.0.0", runner) == "v1.0.0"
    with pytest.raises(SkillInstallError, match="stable"):
        resolve_version("1.0.0-rc.1", runner)


def test_npx_first_installs_only_kanban_to_all_destinations(tmp_path):
    runner = FakeRunner()
    result = install(tmp_path, "1.0.0", runner=runner)
    assert result["acquisition"] == "npx"
    assert [call[0][0] for call in runner.calls][:2] == ["git", "npx"]
    assert all((tmp_path / destination / "SKILL.md").is_file() for destination in DESTINATIONS)
    assert not list(tmp_path.rglob("plan-kanban-tasks"))
    assert status(tmp_path)["installed"] is True


def test_clone_is_used_only_after_npx_failure(tmp_path):
    runner = FakeRunner(npx=False)
    result = install(tmp_path, "v1.0.0", runner=runner)
    assert result["acquisition"] == "git"
    assert [call[0][0] for call in runner.calls] == ["git", "npx", "git"]


def test_non_targeted_npx_result_is_rejected_and_falls_back(tmp_path):
    result = install(tmp_path, "1.0.0", runner=FakeRunner(npx_extra=True))
    assert result["acquisition"] == "git"
    assert not list(tmp_path.rglob("unexpected"))


def test_total_acquisition_failure_leaves_project_unchanged(tmp_path):
    marker = tmp_path / "unrelated.txt"
    marker.write_text("keep")
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    with pytest.raises(SkillInstallError, match="npx or Git"):
        install(tmp_path, "1.0.0", runner=FakeRunner(npx=False, git=False))
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before
    assert marker.read_text() == "keep"


def test_conflict_is_noop_and_force_is_bounded(tmp_path):
    for destination in DESTINATIONS:
        write_skill(tmp_path / destination, "user copy")
    unrelated = tmp_path / ".agents/skills/custom/SKILL.md"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("custom")
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    with pytest.raises(SkillInstallError, match="--force"):
        install(tmp_path, "1.0.0", runner=FakeRunner())
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before

    result = install(tmp_path, "1.0.0", force=True, runner=FakeRunner())
    assert result["changed"] is True
    assert unrelated.read_text() == "custom"
    assert "canonical" in (tmp_path / DESTINATIONS[0] / "SKILL.md").read_text()


def test_symlink_destination_is_rejected_without_mutation(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".agents").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SkillInstallError, match="symbolic link"):
        install(tmp_path, "1.0.0", runner=FakeRunner())
    assert list(outside.iterdir()) == []


def test_status_and_list_are_offline(tmp_path, capsys):
    with patch("yylo_ledger.skills._run", side_effect=AssertionError("network/process forbidden")):
        assert status(tmp_path) == {"installed": False, "skill": "kanban-workflow"}
        assert TaskCLI().run(["skills", "status"]) == ExitCode.SUCCESS
        assert TaskCLI().run(["skills", "list"]) == ExitCode.SUCCESS
    output = capsys.readouterr().out
    assert "kanban-workflow" in output
