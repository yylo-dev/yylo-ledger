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

__version__ = "0.2.1rc4"
__author__ = "JUNO AI INC."

# Export main classes for easy importing
from .models import Task
from .records import (
    Record, RecordError, Relation, RevisionProvenance, exact_replace,
    task_record_projection,
)
from .artifacts import ArtifactPolicy, ArtifactStore, validate_retention
from .content_objects import ContentObjectStore
from .record_search import (
    IndexedRecord, RecordSearchIndex, RecordSearchPage, RecordSearchPolicy,
    RecordSearchQuery,
)
from .documents import (
    DocumentStore, create_document, exact_update_document, extract_record_links,
    validate_document, validate_record_links,
)
from .frontmatter import emit_wiki_frontmatter, import_wiki_frontmatter, parse_wiki_frontmatter
from .profiles import (
    ProfileRegistry, RecordProfile, WIKI_PROFILE, WORKFLOW_PROFILE,
    WORKFLOW_SCHEMA_V1, default_profile_registry,
)
from .workflow_yaml import emit_workflow_yaml, normalize_workflow_yaml, parse_workflow_yaml
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
    "Record",
    "RecordError",
    "Relation",
    "RevisionProvenance",
    "exact_replace",
    "task_record_projection",
    "ArtifactPolicy",
    "ArtifactStore",
    "ContentObjectStore",
    "IndexedRecord",
    "RecordSearchIndex",
    "RecordSearchPage",
    "RecordSearchPolicy",
    "RecordSearchQuery",
    "validate_retention",
    "RecordProfile",
    "ProfileRegistry",
    "WIKI_PROFILE",
    "WORKFLOW_PROFILE",
    "WORKFLOW_SCHEMA_V1",
    "default_profile_registry",
    "DocumentStore",
    "create_document",
    "validate_document",
    "exact_update_document",
    "extract_record_links",
    "validate_record_links",
    "parse_wiki_frontmatter",
    "emit_wiki_frontmatter",
    "import_wiki_frontmatter",
    "parse_workflow_yaml",
    "emit_workflow_yaml",
    "normalize_workflow_yaml",
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
