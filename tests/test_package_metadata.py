"""Regression coverage for release package identity and dependency metadata."""
from __future__ import annotations

import configparser
import email
import io
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
from packaging.requirements import Requirement

pytest.importorskip("wheel", reason="wheel is required for the package release gate")


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_VERSION = "0.2.0"
EXPECTED_CONSOLE_SCRIPTS = {
    "yylo-ledger": "yylo_ledger.cli:main",
    "juno-ledger": "yylo_ledger.cli:legacy_main",
    "ledger-juno": "yylo_ledger.cli:legacy_main",
    "jl": "yylo_ledger.cli:legacy_main",
    "juno-kanban": "yylo_ledger.cli:legacy_main",
    "juno-feedback": "yylo_ledger.cli:legacy_main",
    "kanban-juno": "yylo_ledger.cli:legacy_main",
}


def wheel_requirements(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        metadata_name = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
        metadata = email.message_from_bytes(archive.read(metadata_name))
    return sorted(metadata.get_all("Requires-Dist", []))


def wheel_version(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        metadata_name = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
        metadata = email.message_from_bytes(archive.read(metadata_name))
    return metadata["Version"]


def wheel_console_scripts(path: Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as archive:
        entry_points_name = next(
            name for name in archive.namelist() if name.endswith(".dist-info/entry_points.txt")
        )
        parser = configparser.ConfigParser()
        parser.read_file(io.StringIO(archive.read(entry_points_name).decode("utf-8")))
    return dict(parser["console_scripts"])


def build_wheel(source: Path, destination: Path) -> Path:
    subprocess.run(
        [sys.executable, "setup.py", "-q", "bdist_wheel", "--dist-dir", str(destination)],
        cwd=source, check=True, stdin=subprocess.DEVNULL, capture_output=True, text=True,
    )
    return next(destination.glob("yylo_ledger-*.whl"))


def test_direct_and_sdist_derived_wheels_keep_runtime_dependency(tmp_path: Path) -> None:
    source = tmp_path / "source"
    shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns(".git", "build", "dist", "*.egg-info", "__pycache__"))
    direct = build_wheel(source, tmp_path / "direct")

    subprocess.run(
        [sys.executable, "setup.py", "-q", "sdist", "--dist-dir", str(tmp_path / "sdist")],
        cwd=source, check=True, stdin=subprocess.DEVNULL, capture_output=True, text=True,
    )
    archive = next((tmp_path / "sdist").glob("yylo_ledger-*.tar.gz"))
    extracted = tmp_path / "extracted"
    with tarfile.open(archive) as bundle:
        bundle.extractall(extracted)
    derived = build_wheel(next(extracted.iterdir()), tmp_path / "derived")

    direct_requirements = wheel_requirements(direct)
    derived_requirements = wheel_requirements(derived)
    assert direct_requirements == derived_requirements
    assert len(direct_requirements) == 1
    requirement = Requirement(direct_requirements[0])
    assert requirement.name == "ruamel.yaml"
    assert str(requirement.specifier) == "<0.19,>=0.18.6"
    assert wheel_console_scripts(direct) == EXPECTED_CONSOLE_SCRIPTS
    assert wheel_console_scripts(derived) == EXPECTED_CONSOLE_SCRIPTS
    assert wheel_version(direct) == EXPECTED_VERSION
    assert wheel_version(derived) == EXPECTED_VERSION
