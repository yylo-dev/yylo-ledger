"""
YYLO Ledger

A Git-native, shell-friendly task manager. The Python package remains named
``kanban`` for backward compatibility.

Main features:
- One safe Markdown/YAML current-state file per task
- Configurable status workflows
- Feature tag system
- Disposable SQLite query cache and typed custom-field search
- LLM-optimized CLI interface
- Atomic file operations

Author: JUNO AI INC.
License: MIT
"""

from .identity import migrate_environment
migrate_environment()

__version__ = "0.2.0"
__author__ = "JUNO AI INC."

# Export main classes for easy importing
from .models import Task
from .config import Config
from .validators import TaskValidator, ValidationError

# Import other modules when they exist
try:
    from .storage import TaskStorage
except ImportError:
    TaskStorage = None

try:
    from .search import TaskSearch
except ImportError:
    TaskSearch = None

try:
    from .graph import DependencyGraph
except ImportError:
    DependencyGraph = None

try:
    from .cli import main as cli_main
except ImportError:
    cli_main = None

__all__ = [
    "Task",
    "Config",
    "TaskValidator",
    "ValidationError",
]

# Add modules that exist
if TaskStorage:
    __all__.append("TaskStorage")
if TaskSearch:
    __all__.append("TaskSearch")
if DependencyGraph:
    __all__.append("DependencyGraph")
if cli_main:
    __all__.append("cli_main")
