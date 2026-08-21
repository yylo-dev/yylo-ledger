import os
import subprocess
import sys
from pathlib import Path

import yylo_ledger


def test_canonical_library_api_is_public():
    assert yylo_ledger.Task is not None
    assert yylo_ledger.__version__ == "0.1.0rc1"


def test_deprecated_import_shares_runtime_and_emits_action():
    result = subprocess.run(
        [sys.executable, "-W", "always", "-c", "import kanban,yylo_ledger; assert kanban.Task is yylo_ledger.Task"],
        text=True,
        capture_output=True,
        check=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
    )
    assert "deprecated; migrate to 'yylo_ledger'" in result.stderr
