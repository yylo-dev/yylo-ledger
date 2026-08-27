"""Deprecated import bridge for the YYLO Ledger 0.1 RC migration window.

Use :mod:`yylo_ledger`. This module aliases the prior public module graph
without duplicating runtime state or persisted data formats.
"""
from __future__ import annotations

import importlib
import sys
import warnings

warnings.warn(
    "The 'kanban' import is deprecated; migrate to 'yylo_ledger'.",
    DeprecationWarning,
    stacklevel=2,
)

_CANONICAL = importlib.import_module("yylo_ledger")
_MODULES = (
    "archive", "benchmark_git_native", "cache", "codec", "config",
    "graph", "ledger", "merge", "models", "project_registry", "records",
    "search", "storage", "validators",
)
for _name in _MODULES:
    _module = importlib.import_module(f"yylo_ledger.{_name}")
    sys.modules[f"{__name__}.{_name}"] = _module
    globals()[_name] = _module

for _name in getattr(_CANONICAL, "__all__", ()):
    globals()[_name] = getattr(_CANONICAL, _name)
__version__ = _CANONICAL.__version__
__author__ = _CANONICAL.__author__
__all__ = list(getattr(_CANONICAL, "__all__", ()))
