"""Bounded identity migration helpers for YYLO Ledger 0.1 RC."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

LEGACY_ENV_PREFIX = "JUNO_KANBAN_"
CANONICAL_ENV_PREFIX = "YYLO_LEDGER_"


def migrate_environment(environment=None):
    """Map deprecated environment inputs; explicit canonical values win."""
    environment = os.environ if environment is None else environment
    for name, value in list(environment.items()):
        if name.startswith(LEGACY_ENV_PREFIX):
            canonical = CANONICAL_ENV_PREFIX + name[len(LEGACY_ENV_PREFIX):]
            environment.setdefault(canonical, value)


def migrate_user_home(home=None):
    """Copy legacy user configuration once, preserving it for rollback."""
    home = Path.home() if home is None else Path(home)
    legacy = home / ".juno-kanban"
    canonical = home / ".yylo-ledger"
    if canonical.exists() or not legacy.exists():
        return canonical
    temporary = Path(tempfile.mkdtemp(prefix=".yylo-ledger-migrate-", dir=str(home)))
    try:
        shutil.copytree(legacy, temporary / "state", copy_function=shutil.copy2)
        try:
            (temporary / "state").replace(canonical)
        except FileExistsError:
            pass
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    return canonical
