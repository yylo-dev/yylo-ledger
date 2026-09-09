"""Explicit, transactional remote installation for YYLO Ledger skills."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

REPOSITORY = "https://github.com/yylo-dev/yylo-skills.git"
SKILLS = (
    "artifact-yylo",
    "ledger-tasks-yylo",
    "wiki-yylo",
    "workflow-yylo",
)
LEGACY_SKILLS = ("kanban-workflow",)
DESTINATION_ROOTS = (Path(".agents/skills"), Path(".claude/skills"), Path(".pi/skills"))
DESTINATIONS = tuple(root / skill for root in DESTINATION_ROOTS for skill in SKILLS)
RECORD_PATH = Path(".juno_task/skills-install.json")
_STABLE = re.compile(r"^v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


class SkillInstallError(RuntimeError):
    """A fail-closed skill acquisition or installation error."""


def _run(argv: List[str], cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, cwd=str(cwd) if cwd else None, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
        check=True, env=dict(os.environ, CI="1", NO_COLOR="1"),
    )


def _version_key(version: str) -> Tuple[int, int, int]:
    match = _STABLE.fullmatch(version.strip())
    if not match:
        raise SkillInstallError("skill version must be a stable MAJOR.MINOR.PATCH release")
    return tuple(int(value) for value in match.groups())  # type: ignore[return-value]


def resolve_version(requested: Optional[str], runner: Callable = _run) -> str:
    if requested and requested.strip():
        value = requested.strip()
        key = _version_key(value)
        tag = "v{}.{}.{}".format(*key)
        result = runner(["git", "ls-remote", "--tags", REPOSITORY,
                         "refs/tags/{}".format(tag), "refs/tags/{}^{{}}".format(tag)])
        if not result.stdout.strip():
            raise SkillInstallError("skill version does not exist: {}".format(tag))
        return tag

    result = runner(["git", "ls-remote", "--tags", "--refs", REPOSITORY, "refs/tags/v*"])
    versions = []
    for line in result.stdout.splitlines():
        match = re.search(r"refs/tags/(v[^\s]+)$", line)
        if not match:
            continue
        try:
            versions.append((_version_key(match.group(1)), match.group(1)))
        except SkillInstallError:
            continue
    if not versions:
        raise SkillInstallError("no stable yylo-skills release is available")
    return max(versions)[1]


def _walk_files(root: Path) -> Iterable[Tuple[str, Path]]:
    if root.is_symlink() or not root.is_dir():
        raise SkillInstallError("staged skill is not a regular directory")
    for current, directories, files in os.walk(str(root), followlinks=False):
        directories.sort()
        files.sort()
        current_path = Path(current)
        for name in list(directories):
            if (current_path / name).is_symlink():
                raise SkillInstallError("staged skill contains a symbolic link")
        for name in files:
            path = current_path / name
            if path.is_symlink() or not path.is_file():
                raise SkillInstallError("staged skill contains an unsupported entry")
            relative = path.relative_to(root).as_posix()
            if relative.startswith("../") or relative.startswith("/"):
                raise SkillInstallError("staged skill path escapes its root")
            yield relative, path


def _digest(root: Path) -> str:
    digest = hashlib.sha256()
    found = False
    for relative, path in _walk_files(root):
        found = True
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    if not found or not (root / "SKILL.md").is_file():
        raise SkillInstallError("staged skill is missing SKILL.md")
    return digest.hexdigest()


def _validate_stage(stage: Path) -> Dict[Path, str]:
    values = {}
    expected_entries = sorted(SKILLS)
    for root in DESTINATION_ROOTS:
        staged_root = stage / root
        entries = sorted(path.name for path in staged_root.iterdir()) if staged_root.is_dir() else []
        if entries != expected_entries:
            raise SkillInstallError("staged agent destination is not the exact four-skill set")
        for skill in SKILLS:
            relative = root / skill
            values[relative] = _digest(stage / relative)
    for skill in SKILLS:
        skill_values = {values[root / skill] for root in DESTINATION_ROOTS}
        if len(skill_values) != 1:
            raise SkillInstallError("staged {} differs across agent destinations".format(skill))
    return values


def _acquire_npx(stage: Path, version: str, runner: Callable) -> None:
    source = "https://github.com/yylo-dev/yylo-skills/tree/{}".format(version)
    runner(["npx", "--yes", "skills", "add", source, "--skill", *SKILLS,
            "--agent", "codex", "claude-code", "pi", "--copy", "--yes"], cwd=stage)


def _acquire_git(stage: Path, version: str, runner: Callable) -> None:
    repository = stage / "repository"
    runner(["git", "clone", "--depth", "1", "--branch", version,
            "--single-branch", REPOSITORY, str(repository)], cwd=stage)
    for skill in SKILLS:
        source = repository / "skills" / skill
        _digest(source)
        for root in DESTINATION_ROOTS:
            destination = stage / root / skill
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(str(source), str(destination), symlinks=True)


def _safe_destination(project: Path, relative: Path) -> Path:
    project = project.resolve()
    destination = project / relative
    current = project
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            raise SkillInstallError("skill destination traverses a symbolic link: {}".format(relative))
    try:
        destination.relative_to(project)
    except ValueError:
        raise SkillInstallError("skill destination escapes project root")
    return destination


def _load_retirement_record(record_bytes: Optional[bytes]) -> Optional[Dict[str, object]]:
    if not record_bytes:
        return None
    try:
        value = json.loads(record_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("schemaVersion") != 1:
        return None
    if value.get("repository") != REPOSITORY:
        return None
    if not isinstance(value.get("skills"), list) or not isinstance(value.get("digests"), dict):
        return None
    return value


def install(project: Path, requested: Optional[str] = None, force: bool = False,
            runner: Callable = _run) -> Dict[str, object]:
    project = project.resolve()
    version = resolve_version(requested, runner)
    with tempfile.TemporaryDirectory(prefix="yylo-ledger-skills-") as temporary:
        stage = Path(temporary)
        acquisition = "npx"
        try:
            _acquire_npx(stage, version, runner)
            digests = _validate_stage(stage)
        except (OSError, subprocess.SubprocessError, SkillInstallError):
            shutil.rmtree(str(stage), ignore_errors=True)
            stage.mkdir()
            acquisition = "git"
            try:
                _acquire_git(stage, version, runner)
                digests = _validate_stage(stage)
            except (OSError, subprocess.SubprocessError, SkillInstallError) as exc:
                raise SkillInstallError(
                    "unable to acquire YYLO Ledger skills {} with npx or Git: {}".format(version, exc))

        record_path = _safe_destination(project, RECORD_PATH)
        try:
            previous_record_bytes = record_path.read_bytes() if record_path.exists() else None
        except OSError as exc:
            raise SkillInstallError("cannot read local skill install record: {}".format(exc))
        previous_record = _load_retirement_record(previous_record_bytes)

        planned = []
        for relative, expected in digests.items():
            destination = _safe_destination(project, relative)
            if destination.exists():
                actual = _digest(destination)
                if actual == expected:
                    continue
                if not force:
                    raise SkillInstallError("existing skill differs; rerun with --force: {}".format(relative))
            planned.append((relative, destination))

        retirements = []
        warnings = []
        old_skills = previous_record.get("skills", []) if previous_record else []
        old_digests = previous_record.get("digests", {}) if previous_record else {}
        for root in DESTINATION_ROOTS:
            for legacy in LEGACY_SKILLS:
                relative = root / legacy
                destination = _safe_destination(project, relative)
                if not destination.exists():
                    continue
                expected = (old_digests.get(relative.as_posix())
                            if isinstance(old_digests, dict) and legacy in old_skills else None)
                try:
                    current = _digest(destination)
                except (OSError, SkillInstallError):
                    current = None
                if isinstance(expected, str) and current == expected:
                    retirements.append((relative, destination))
                else:
                    warnings.append(
                        "Preserved customized or unrecorded legacy skill at {}".format(relative.as_posix()))

        token = next(tempfile._get_candidate_names())
        prepared = []
        try:
            for relative, destination in planned:
                pending = destination.with_name(".{}.{}.pending".format(destination.name, token))
                destination.parent.mkdir(parents=True, exist_ok=True)
                prepared.append((destination, pending))
                shutil.copytree(str(stage / relative), str(pending), symlinks=True)
                _digest(pending)
        except Exception:
            for _, pending in prepared:
                shutil.rmtree(str(pending), ignore_errors=True)
            raise

        replacements = []
        retired = []
        committed = False
        pending_record = record_path.with_name(".{}.{}.pending".format(record_path.name, token))
        try:
            for destination, pending in prepared:
                backup = None
                if destination.exists():
                    backup = destination.with_name(".{}.{}.backup".format(destination.name, token))
                    os.replace(str(destination), str(backup))
                replacements.append((destination, backup))
                os.replace(str(pending), str(destination))
            for _, destination in retirements:
                backup = destination.with_name(".{}.{}.retired".format(destination.name, token))
                os.replace(str(destination), str(backup))
                retired.append((destination, backup))

            record = {
                "schemaVersion": 1, "repository": REPOSITORY, "version": version,
                "acquisition": acquisition, "skills": list(SKILLS),
                "digests": {relative.as_posix(): digest for relative, digest in digests.items()},
                "installedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
            record_path.parent.mkdir(parents=True, exist_ok=True)
            pending_record.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            os.replace(str(pending_record), str(record_path))
            committed = True
        except Exception:
            for destination, backup in reversed(retired):
                if backup.exists():
                    os.replace(str(backup), str(destination))
            for destination, backup in reversed(replacements):
                shutil.rmtree(str(destination), ignore_errors=True)
                if backup and backup.exists():
                    os.replace(str(backup), str(destination))
            if previous_record_bytes is not None:
                record_path.parent.mkdir(parents=True, exist_ok=True)
                record_path.write_bytes(previous_record_bytes)
            else:
                try:
                    record_path.unlink()
                except FileNotFoundError:
                    pass
            raise
        finally:
            try:
                pending_record.unlink()
            except FileNotFoundError:
                pass
            for _, pending in prepared:
                shutil.rmtree(str(pending), ignore_errors=True)
            if committed:
                for _, backup in replacements:
                    if backup:
                        shutil.rmtree(str(backup), ignore_errors=True)
                for _, backup in retired:
                    shutil.rmtree(str(backup), ignore_errors=True)

        return {
            "changed": bool(planned or retirements), "version": version, "acquisition": acquisition,
            "skills": list(SKILLS), "destinations": [path.as_posix() for path in DESTINATIONS],
            **({"warnings": warnings} if warnings else {}),
        }


def status(project: Path) -> Dict[str, object]:
    project = project.resolve()
    record = _safe_destination(project, RECORD_PATH)
    if not record.is_file():
        return {"installed": False, "skills": list(SKILLS)}
    try:
        value = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SkillInstallError("invalid local skill install record: {}".format(exc))
    installed = all(_safe_destination(project, relative).is_dir() for relative in DESTINATIONS)
    return {"installed": installed, "skills": list(SKILLS), "version": value.get("version"),
            "acquisition": value.get("acquisition"),
            "destinations": [path.as_posix() for path in DESTINATIONS]}
