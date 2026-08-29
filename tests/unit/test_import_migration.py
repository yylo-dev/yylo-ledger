import os
import re
import subprocess
import sys
from pathlib import Path

import yylo_ledger


def test_canonical_library_api_is_public():
    source = (Path(__file__).resolve().parents[2] / "src" / "yylo_ledger" / "__init__.py").read_text(
        encoding="utf-8"
    )
    version = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', source, re.MULTILINE)
    assert yylo_ledger.Task is not None
    assert version is not None and yylo_ledger.__version__ == version.group(1)


def test_deprecated_import_shares_runtime_and_emits_action():
    result = subprocess.run(
        [sys.executable, "-W", "always", "-c", "import kanban,yylo_ledger; assert kanban.Task is yylo_ledger.Task"],
        text=True,
        capture_output=True,
        check=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
    )
    assert "deprecated; migrate to 'yylo_ledger'" in result.stderr
