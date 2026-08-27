import re
from pathlib import Path

def test_readme_badge_matches_canonical_rc_version():
    root = Path(__file__).resolve().parents[2]
    readme = (root / "README.md").read_text(encoding="utf-8")
    setup = (root / "setup.py").read_text(encoding="utf-8")
    package = (root / "src" / "yylo_ledger" / "__init__.py").read_text(encoding="utf-8")

    badge = re.search(r"shields\.io/badge/version-([^-]+)-blue\.svg", readme)
    version = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', package, re.MULTILINE)
    assert badge is not None and version is not None
    assert badge.group(1) == version.group(1) == "0.2.0"
    assert "src', 'yylo_ledger', '__init__.py" in setup
    assert "pypi.org/project/yylo-ledger/" in readme
