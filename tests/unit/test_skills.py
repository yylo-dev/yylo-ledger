import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from yylo_ledger.cli import ExitCode, TaskCLI
from yylo_ledger.skills import (DESTINATIONS, DESTINATION_ROOTS, LEGACY_SKILLS,
                                RECORD_PATH, REPOSITORY, SKILLS, SkillInstallError,
                                _digest, install, resolve_version, status)


def completed(argv, stdout=""):
    return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")


def write_skill(root: Path, name: str, body: str = "canonical"):
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text("---\nname: {}\n---\n{}\n".format(name, body))


class FakeRunner:
    def __init__(self, npx=True, git=True, body="canonical", npx_extra=False,
                 omit=None):
        self.npx = npx
        self.git = git
        self.body = body
        self.npx_extra = npx_extra
        self.omit = omit
        self.calls = []

    def __call__(self, argv, cwd=None):
        self.calls.append((list(argv), Path(cwd) if cwd else None))
        if argv[:3] == ["git", "ls-remote", "--tags"]:
            if "--refs" in argv:
                return completed(argv, "a refs/tags/v1.0.0\nb refs/tags/v2.0.0-rc.1\nc refs/tags/v2.0.0\n")
            return completed(argv, "a refs/tags/v2.0.0\n")
        if argv[0] == "npx":
            if not self.npx:
                raise FileNotFoundError("npx")
            for root in DESTINATION_ROOTS:
                for skill in SKILLS:
                    if skill != self.omit:
                        write_skill(Path(cwd) / root / skill, skill, self.body)
                if self.npx_extra:
                    write_skill(Path(cwd) / root / "unexpected", "unexpected", "extra")
            return completed(argv)
        if argv[:2] == ["git", "clone"]:
            if not self.git:
                raise subprocess.CalledProcessError(1, argv)
            for skill in SKILLS:
                write_skill(Path(argv[-1]) / "skills" / skill, skill, self.body)
            return completed(argv)
        raise AssertionError(argv)


def old_record(project: Path):
    digests = {}
    for root in DESTINATION_ROOTS:
        for skill in LEGACY_SKILLS:
            relative = root / skill
            write_skill(project / relative, skill, "legacy canonical")
            digests[relative.as_posix()] = _digest(project / relative)
    record = {
        "schemaVersion": 1,
        "repository": REPOSITORY,
        "version": "v1.0.0",
        "acquisition": "npx",
        "skills": list(LEGACY_SKILLS),
        "digests": digests,
    }
    path = project / RECORD_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record))
    return path


def test_resolves_latest_stable_and_exact_version():
    runner = FakeRunner()
    assert resolve_version(None, runner) == "v2.0.0"
    assert resolve_version("2.0.0", runner) == "v2.0.0"
    with pytest.raises(SkillInstallError, match="stable"):
        resolve_version("2.0.0-rc.1", runner)


def test_npx_first_installs_exact_canonical_skills_to_all_destinations(tmp_path):
    assert len(SKILLS) == 7
    runner = FakeRunner(body="literal $ARGUMENTS $1 $2")
    result = install(tmp_path, "2.0.0", runner=runner)
    assert result["acquisition"] == "npx"
    assert result["skills"] == list(SKILLS)
    assert [call[0][0] for call in runner.calls][:2] == ["git", "npx"]
    npx = runner.calls[1][0]
    start = npx.index("--skill") + 1
    assert npx[start:start + len(SKILLS)] == list(SKILLS)
    assert all((tmp_path / destination / "SKILL.md").is_file() for destination in DESTINATIONS)
    assert "literal $ARGUMENTS $1 $2" in (tmp_path / DESTINATIONS[0] / "SKILL.md").read_text()
    assert status(tmp_path)["installed"] is True


def test_clone_is_used_only_after_npx_failure(tmp_path):
    runner = FakeRunner(npx=False)
    result = install(tmp_path, "v2.0.0", runner=runner)
    assert result["acquisition"] == "git"
    assert [call[0][0] for call in runner.calls] == ["git", "npx", "git"]


@pytest.mark.parametrize("runner", [FakeRunner(npx_extra=True), FakeRunner(omit=SKILLS[0])])
def test_non_exact_npx_result_is_rejected_and_falls_back(tmp_path, runner):
    result = install(tmp_path, "2.0.0", runner=runner)
    assert result["acquisition"] == "git"
    assert not list(tmp_path.rglob("unexpected"))


def test_total_acquisition_failure_leaves_project_unchanged(tmp_path):
    marker = tmp_path / "unrelated.txt"
    marker.write_text("keep")
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    with pytest.raises(SkillInstallError, match="npx or Git"):
        install(tmp_path, "2.0.0", runner=FakeRunner(npx=False, git=False))
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before
    assert marker.read_text() == "keep"


def test_conflict_is_noop_and_force_is_bounded(tmp_path):
    for destination in DESTINATIONS:
        write_skill(tmp_path / destination, destination.name, "user copy")
    unrelated = tmp_path / ".agents/skills/custom/SKILL.md"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("custom")
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    with pytest.raises(SkillInstallError, match="--force"):
        install(tmp_path, "2.0.0", runner=FakeRunner())
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before

    result = install(tmp_path, "2.0.0", force=True, runner=FakeRunner())
    assert result["changed"] is True
    assert unrelated.read_text() == "custom"
    assert "canonical" in (tmp_path / DESTINATIONS[0] / "SKILL.md").read_text()


def test_recorded_unmodified_legacy_skill_is_retired(tmp_path):
    old_record(tmp_path)
    result = install(tmp_path, "2.0.0", runner=FakeRunner())
    assert result["changed"] is True
    assert "warnings" not in result
    assert all(not (tmp_path / root / skill).exists()
               for root in DESTINATION_ROOTS for skill in LEGACY_SKILLS)
    assert json.loads((tmp_path / RECORD_PATH).read_text())["skills"] == list(SKILLS)


def test_customized_and_unrecorded_legacy_skills_are_preserved_with_warnings(tmp_path):
    old_record(tmp_path)
    customized = tmp_path / DESTINATION_ROOTS[0] / LEGACY_SKILLS[0] / "SKILL.md"
    customized.write_text(customized.read_text() + "custom\n")
    unrecorded = tmp_path / DESTINATION_ROOTS[1] / "unrelated-old"
    write_skill(unrecorded, "unrelated-old")
    result = install(tmp_path, "2.0.0", runner=FakeRunner())
    assert customized.is_file()
    assert unrecorded.is_dir()
    assert len(result["warnings"]) == 1
    assert (DESTINATION_ROOTS[0] / LEGACY_SKILLS[0]).as_posix() in result["warnings"][0]


def test_unrecorded_legacy_skill_is_preserved(tmp_path):
    legacy = tmp_path / DESTINATION_ROOTS[0] / LEGACY_SKILLS[0]
    write_skill(legacy, LEGACY_SKILLS[0])
    result = install(tmp_path, "2.0.0", runner=FakeRunner())
    assert legacy.is_dir()
    assert result["warnings"]


def test_record_failure_rolls_back_installs_retirements_and_record(tmp_path):
    record_path = old_record(tmp_path)
    before_record = record_path.read_bytes()
    original_replace = os.replace

    def failing_replace(source, destination):
        if Path(destination) == record_path:
            raise OSError("record write failed")
        return original_replace(source, destination)

    with patch("yylo_ledger.skills.os.replace", side_effect=failing_replace):
        with pytest.raises(OSError, match="record write failed"):
            install(tmp_path, "2.0.0", runner=FakeRunner())
    assert record_path.read_bytes() == before_record
    assert all((tmp_path / root / skill).is_dir()
               for root in DESTINATION_ROOTS for skill in LEGACY_SKILLS)
    assert all(not (tmp_path / destination).exists() for destination in DESTINATIONS)


def test_symlink_destination_is_rejected_without_mutation(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".agents").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SkillInstallError, match="symbolic link"):
        install(tmp_path, "2.0.0", runner=FakeRunner())
    assert list(outside.iterdir()) == []


def test_status_and_list_are_offline(tmp_path, capsys):
    with patch("yylo_ledger.skills._run", side_effect=AssertionError("network/process forbidden")):
        assert status(tmp_path) == {"installed": False, "skills": list(SKILLS)}
        assert TaskCLI().run(["skills", "status"]) == ExitCode.SUCCESS
        assert TaskCLI().run(["skills", "list"]) == ExitCode.SUCCESS
    output = capsys.readouterr().out
    assert all(skill in output for skill in SKILLS)
