#!/usr/bin/env python3
"""Provision or verify this checkout's hash-locked, isolated Python test tools."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "requirements-test.lock"
ENV = ROOT / ".venv"
STAMP = ENV / ".requirements-test.sha256"
TIMEOUT = 600


class HydrationError(RuntimeError):
    pass


def run(args: list[str], deadline: float, *, capture: bool = False) -> str:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise HydrationError("test dependency hydration exceeded 600s")
    # Neither an active controller virtualenv nor Python/Pip configuration is
    # dependency authority for this checkout. Never echo inherited credentials.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PYTHON", "PIP_")) and key != "VIRTUAL_ENV"}
    env["PIP_CONFIG_FILE"] = os.devnull
    result = subprocess.run(args, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                            text=True, capture_output=capture, timeout=remaining)
    if result.returncode:
        raise HydrationError(f"test dependency command failed (exit {result.returncode})")
    return result.stdout if capture else ""


def identity() -> str:
    if sys.implementation.name != "cpython" or sys.version_info[:2] not in ((3, 12), (3, 13)):
        raise HydrationError("test hydration requires CPython 3.12 or 3.13 (runtime support is unchanged)")
    if os.name != "posix":
        raise HydrationError("test hydration currently supports POSIX hosts")
    if not LOCK.is_file() or LOCK.is_symlink():
        raise HydrationError("requirements-test.lock is missing or symbolic")
    if ENV.is_symlink() or (ENV / "pyvenv.cfg").is_symlink() or STAMP.is_symlink():
        raise HydrationError("test environment or lock stamp is symbolic; preserve it for owner review")
    return hashlib.sha256(LOCK.read_bytes()).hexdigest()


def verify(deadline: float, digest: str, *, stamp: bool = True) -> None:
    python = ENV / "bin/python"
    config = ENV / "pyvenv.cfg"
    if not python.is_file() or not config.is_file() or config.is_symlink():
        raise HydrationError("task-local .venv is missing")
    if stamp and (not STAMP.is_file() or STAMP.read_text().strip() != digest):
        raise HydrationError("task-local test dependencies are missing or stale for requirements-test.lock")
    # Validate actual versions, markers, isolation and installation locations,
    # not merely a successful historical stamp or an ambient import.
    result = run([str(python), "-I", "-c", '''
import importlib.metadata as metadata
import json, sys
from pathlib import Path
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
root = Path.cwd()
expected = root / '.venv'
assert Path(sys.prefix).resolve() == expected.resolve(), 'wrong test environment'
assert sys.prefix != sys.base_prefix, 'not a virtual environment'
config = (expected / 'pyvenv.cfg').read_text().lower()
assert 'include-system-site-packages = false' in config, 'shared site packages refused'
expected_names = set()
for line in (root / 'requirements-test.lock').read_text().splitlines():
    line = line.strip().removesuffix('\\\\').strip()
    if not line or line.startswith(('#', '--hash=')):
        continue
    requirement = Requirement(line)
    if requirement.marker and not requirement.marker.evaluate():
        continue
    expected_names.add(canonicalize_name(requirement.name))
    distribution = metadata.distribution(requirement.name)
    assert distribution.version in requirement.specifier, requirement.name + ' version drift'
    assert Path(distribution.locate_file('')).resolve().is_relative_to(expected.resolve()), requirement.name + ' borrowed installation'
assert {canonicalize_name(d.metadata['Name']) for d in metadata.distributions()} == expected_names, 'unlocked test packages present; preserve environment for owner review'
print(json.dumps(list(sys.version_info[:2])))
'''], deadline, capture=True)
    if json.loads(result) != list(sys.version_info[:2]):
        raise HydrationError("test environment interpreter differs from hydration interpreter")
    run([str(python), "-I", "-m", "pip", "--isolated", "--disable-pip-version-check", "check"], deadline, capture=True)


def hydrate(deadline: float, digest: str) -> None:
    try:
        verify(deadline, digest)
        return
    except HydrationError:
        pass
    # Never clear/recreate an existing environment, follow an external venv,
    # or retain a successful stamp after failed installation.
    if ENV.exists():
        if not (ENV / "pyvenv.cfg").is_file():
            raise HydrationError("existing .venv is not a virtualenv; preserve it for owner review")
        config = (ENV / "pyvenv.cfg").read_text().lower()
        if "include-system-site-packages = false" not in config:
            raise HydrationError("existing .venv shares site packages; preserve it for owner review")
    else:
        run([sys.executable, "-I", "-m", "venv", str(ENV)], deadline)
    python = str(ENV / "bin/python")
    actual_prefix = run([python, "-I", "-c",
                         "import sys; print(sys.prefix); print(sys.base_prefix)"], deadline, capture=True).splitlines()
    if len(actual_prefix) != 2 or Path(actual_prefix[0]).resolve() != ENV or actual_prefix[0] == actual_prefix[1]:
        raise HydrationError("existing .venv interpreter is not task-local; preserve it for owner review")
    STAMP.unlink(missing_ok=True)
    run([python, "-I", "-m", "pip", "--isolated", "--disable-pip-version-check", "install",
         "--index-url", "https://pypi.org/simple", "--only-binary=:all:",
         "--require-hashes", "-r", str(LOCK)], deadline)
    verify(deadline, digest, stamp=False)
    if identity() != digest:
        raise HydrationError("requirements-test.lock changed during hydration")
    temporary = STAMP.with_name(f"{STAMP.name}.{os.getpid()}.tmp")
    temporary.write_text(digest + "\n")
    temporary.replace(STAMP)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify only; never provision or repair")
    args = parser.parse_args()
    started = time.monotonic()
    try:
        digest = identity()
        if args.check:
            verify(started + TIMEOUT, digest)
        else:
            hydrate(started + TIMEOUT, digest)
        print(f"[ledger-test-hydration] OK lock={digest} duration={time.monotonic() - started:.1f}s")
        return 0
    except (HydrationError, OSError, subprocess.TimeoutExpired, ValueError) as exc:
        print(f"[ledger-test-hydration] FAILED {exc}; duration={time.monotonic() - started:.1f}s", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
