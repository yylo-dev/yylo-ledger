"""Deprecated module launcher bridge; use :mod:`yylo_ledger.cli`."""
from yylo_ledger.cli import *  # noqa: F401,F403
from yylo_ledger.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
