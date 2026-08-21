#!/usr/bin/env python3
"""
Configuration management for task system.
"""

import json
import os
import re
from pathlib import Path
from typing import Optional, Tuple, Dict, Any, List
from copy import deepcopy

from .identity import migrate_user_home


class ConfigError(Exception):
    """Configuration-related errors."""
    pass


class Config:
    """Configuration manager for task system."""

    # Default configuration (fallback)
    DEFAULT_CONFIG = {
        "version": "1.0",
        "status_workflow": {
            "enabled": True,
            "values": ["backlog", "todo", "in_progress", "done", "archive"],
            "default": "backlog",
            "transitions": {
                "backlog": ["todo", "archive"],
                "todo": ["in_progress", "backlog", "archive"],
                "in_progress": ["done", "todo", "archive"],
                "done": ["archive"],
                "archive": []
            },
            "enforce_transitions": False,
            "allow_any_to_archive": True
        },
        "feature_tags": {
            "enabled": True,
            "allowed_tags": None,
            "max_tags_per_task": 20,
            "validation_pattern": "^[a-zA-Z0-9_-]{1,50}$",
            "case_sensitive": False,
            "auto_create": True
        },
        "storage": {
            "base_path": ".juno_task/tasks",
            "file_pattern": "*/*.md",
            "default_file": "",
            "ledger_segment_bytes": 5242880
        },
        "search": {
            "default_limit": 5,
            "use_ripgrep": True,
            "ripgrep_path": None,
            "case_sensitive": False
        },
        "output": {
            "default_format": "ndjson",
            "pretty_print": False,
            "color": True,
            "timestamp_format": "iso8601",
            "redaction_patterns": []
        },
        "project_root": {
            "auto_detect": True,
            "root_markers": [".git", ".juno-root", "package.json", "pyproject.toml", "Cargo.toml"],
            "max_depth": 10,
            "non_root_behavior": "warn",  # "warn", "error", "allow"
            "enable_prevention": True
        },
        "help_text": {
            "status": "Available statuses: backlog, todo, in_progress, done, archive",
            "tags": "Tags can be any alphanumeric string with underscores/hyphens (max 50 chars, max 20 per task)",
            "workflow": "Workflow: backlog → todo → in_progress → done → archive",
            "general": "YYLO Ledger shell-based task manager"
        },
        "error_messages": {
            "invalid_status": "Error: Invalid status '{status}'. Allowed: {allowed_values}",
            "invalid_transition": "Error: Cannot transition from '{from_status}' to '{to_status}'. Allowed: {allowed_transitions}",
            "invalid_tag": "Error: Tag '{tag}' not allowed. {constraint}",
            "task_not_found": "Error: Task '{task_id}' not found"
        }
    }

    def __init__(self, config_path: Optional[str] = None, auto_create: bool = True):
        """
        Initialize configuration.

        Args:
            config_path: Path to config.json (default: search for .juno_task/tasks/config.json)
            auto_create: Automatically create default config if not found
        """
        if config_path is None:
            config_path = self._find_config()

        self.config_path = config_path
        self.config = self._load_config(auto_create=auto_create)

        # Validate configuration
        is_valid, error = self.validate()
        if not is_valid:
            raise ConfigError(f"Invalid configuration: {error}")

    def _find_config(self) -> str:
        """
        Find config file by searching up directory tree.

        Priority:
        1. JUNO_TASK_ROOT environment variable (if set, always use it)
        2. Upward directory search from CWD
        3. Default: CWD/.juno_task/tasks/config.json

        Returns:
            Path to config file
        """
        # Check JUNO_TASK_ROOT env var first (highest priority)
        env_root = os.environ.get('JUNO_TASK_ROOT')
        if env_root:
            return str(Path(env_root) / ".juno_task" / "tasks" / "config.json")

        current = Path.cwd()

        # Search up to root
        while current != current.parent:
            config_file = current / ".juno_task" / "tasks" / "config.json"
            if config_file.exists():
                return str(config_file)
            current = current.parent

        # Not found, return default location
        return str(Path.cwd() / ".juno_task" / "tasks" / "config.json")

    def _load_config(self, auto_create: bool = True) -> Dict[str, Any]:
        """
        Load configuration from file or use defaults.

        Configuration loading priority:
        1. Default configuration (base)
        2. Global configuration (~/.yylo-ledger/config.json) - overrides defaults
        3. Local configuration (.juno_task/tasks/config.json) - overrides global

        Args:
            auto_create: Create default config if file doesn't exist

        Returns:
            Configuration dictionary
        """
        # Start with default configuration
        config = deepcopy(self.DEFAULT_CONFIG)

        # Load and merge global configuration
        global_config = self.load_global_config()
        if global_config:
            config = self._deep_merge(config, global_config)

        # Load and merge local configuration
        if not os.path.exists(self.config_path):
            if auto_create:
                self._create_default_config()
            return config

        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                local_config = json.load(f)

            # Deep merge local config (highest priority)
            config = self._deep_merge(config, local_config)
            return config

        except json.JSONDecodeError as e:
            raise ConfigError(f"Invalid JSON in config file: {e}")
        except Exception as e:
            raise ConfigError(f"Error loading config: {e}")

    def _deep_merge(self, base: Dict, override: Dict) -> Dict:
        """
        Deep merge two dictionaries.

        Args:
            base: Base dictionary
            override: Override dictionary

        Returns:
            Merged dictionary
        """
        result = base.copy()

        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = value

        return result

    def _create_default_config(self):
        """Create default configuration file."""
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)

        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump(self.DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)

    def validate(self) -> Tuple[bool, Optional[str]]:
        """
        Validate configuration.

        Returns:
            (is_valid, error_message)
        """
        # Check version
        if 'version' not in self.config:
            return False, "Missing 'version' field"

        # Validate version format
        version_pattern = re.compile(r'^\d+\.\d+(\.\d+)?$')
        if not version_pattern.match(self.config['version']):
            return False, f"Invalid version format: {self.config['version']}"

        # Check required sections
        required_sections = ['status_workflow', 'feature_tags', 'storage']
        for section in required_sections:
            if section not in self.config:
                return False, f"Missing required section: {section}"

        # Validate status_workflow
        workflow = self.config['status_workflow']
        if 'values' not in workflow or not isinstance(workflow['values'], list):
            return False, "status_workflow.values must be a list"

        if not workflow['values']:
            return False, "status_workflow.values cannot be empty"

        if 'default' not in workflow:
            return False, "status_workflow.default is required"

        if workflow['default'] not in workflow['values']:
            return False, f"Default status '{workflow['default']}' not in values"

        # Validate transitions (if enforce_transitions is true)
        if workflow.get('enforce_transitions', False):
            if 'transitions' not in workflow:
                return False, "transitions required when enforce_transitions is true"

            transitions = workflow['transitions']

            # Check all statuses have transitions defined
            for status in workflow['values']:
                if status not in transitions:
                    return False, f"Missing transitions for status: {status}"

                # Validate transition targets exist
                for next_status in transitions[status]:
                    if next_status not in workflow['values']:
                        return False, f"Invalid transition: {status} -> {next_status} (target status not in values)"

        # Validate storage
        storage = self.config['storage']
        required_storage = ['base_path', 'file_pattern', 'default_file']
        for field in required_storage:
            if field not in storage:
                return False, f"storage.{field} is required"

        # Validate feature_tags
        tags_config = self.config['feature_tags']
        if 'validation_pattern' in tags_config:
            try:
                re.compile(tags_config['validation_pattern'])
            except re.error as e:
                return False, f"Invalid validation_pattern: {e}"

        return True, None

    def save(self):
        """Save current configuration to file."""
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)

        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump(self.config, f, indent=2, ensure_ascii=False)

    # Convenience accessors
    @property
    def status_values(self) -> List[str]:
        """Get list of allowed status values."""
        return self.config['status_workflow']['values']

    @property
    def default_status(self) -> str:
        """Get default status for new tasks."""
        return self.config['status_workflow']['default']

    @property
    def enforce_transitions(self) -> bool:
        """Check if status transitions are strictly enforced."""
        return self.config['status_workflow'].get('enforce_transitions', False)

    @property
    def transitions(self) -> Dict[str, List[str]]:
        """Get status transition map."""
        return self.config['status_workflow'].get('transitions', {})

    @property
    def allow_any_to_archive(self) -> bool:
        """Check if any status can transition to archive."""
        return self.config['status_workflow'].get('allow_any_to_archive', True)

    @property
    def allowed_tags(self) -> Optional[List[str]]:
        """Get whitelist of allowed tags (None = any allowed)."""
        return self.config['feature_tags'].get('allowed_tags')

    @property
    def max_tags_per_task(self) -> int:
        """Get maximum tags per task."""
        return self.config['feature_tags'].get('max_tags_per_task', 20)

    @property
    def tag_validation_pattern(self) -> str:
        """Get regex pattern for tag validation."""
        return self.config['feature_tags'].get('validation_pattern', '^[a-zA-Z0-9_-]{1,50}$')

    @property
    def storage_base_path(self) -> str:
        """Get base path for task storage.

        Resolution priority for relative base_path:
        1. JUNO_TASK_ROOT env var (highest priority)
        2. Config file's parent-of-parent directory (config lives at root/.juno_task/tasks/config.json)
        3. Relative to CWD (fallback)
        """
        base = self.config['storage']['base_path']
        if os.path.isabs(base):
            return base
        env_root = os.environ.get('JUNO_TASK_ROOT')
        if env_root:
            return os.path.join(os.path.abspath(env_root), base)
        # Resolve relative to config file's project root (go up from .juno_task/tasks/config.json)
        if self.config_path and os.path.exists(self.config_path):
            config_dir = os.path.dirname(os.path.abspath(self.config_path))  # .../tasks/
            project_root = os.path.dirname(os.path.dirname(config_dir))      # .../
            return os.path.join(project_root, base)
        return base

    @property
    def storage_file_pattern(self) -> str:
        """Get glob pattern for task files."""
        return self.config['storage']['file_pattern']

    @property
    def default_file(self) -> str:
        """Get default filename for new tasks."""
        return self.config['storage']['default_file']

    @property
    def default_limit(self) -> int:
        """Get default search result limit."""
        return self.config['search'].get('default_limit', 5)

    @property
    def use_ripgrep(self) -> bool:
        """Check if ripgrep should be used for searching."""
        return self.config['search'].get('use_ripgrep', True)

    @property
    def default_output_format(self) -> str:
        """Get default output format."""
        return self.config['output'].get('default_format', 'ndjson')

    def get_help_text(self, section: str) -> str:
        """
        Get help text for specific section.

        Args:
            section: Section name (status, tags, workflow, general)

        Returns:
            Help text string
        """
        return self.config.get('help_text', {}).get(section, "")

    def get_error_message(self, error_type: str, **kwargs) -> str:
        """
        Get formatted error message.

        Args:
            error_type: Type of error (invalid_status, invalid_transition, etc.)
            **kwargs: Format arguments

        Returns:
            Formatted error message
        """
        template = self.config.get('error_messages', {}).get(error_type, f"Error: {error_type}")

        try:
            return template.format(**kwargs)
        except KeyError:
            return template

    def generate_help_output(self) -> str:
        """
        Generate LLM-optimized help text based on current configuration.

        Returns:
            Complete help text string
        """
        lines = []

        # General help
        general = self.get_help_text('general')
        if general:
            lines.append(general)
            lines.append("")

        # Status help
        lines.append("Status Values:")
        lines.append(f"  {', '.join(self.status_values)}")
        lines.append(f"  Default: {self.default_status}")

        status_help = self.get_help_text('status')
        if status_help:
            lines.append(f"  {status_help}")
        lines.append("")

        # Workflow help
        if self.enforce_transitions:
            lines.append("Workflow Transitions (STRICT MODE):")
            for from_status, to_statuses in self.transitions.items():
                if to_statuses:
                    lines.append(f"  {from_status} → {', '.join(to_statuses)}")
                else:
                    lines.append(f"  {from_status} → (terminal state)")
        else:
            lines.append("Workflow: Flexible (any status transition allowed)")

        workflow_help = self.get_help_text('workflow')
        if workflow_help:
            lines.append(f"  {workflow_help}")
        lines.append("")

        # Tag help
        lines.append("Feature Tags:")
        if self.allowed_tags:
            lines.append(f"  Allowed: {', '.join(self.allowed_tags)}")
        else:
            lines.append(f"  Pattern: {self.tag_validation_pattern}")
        lines.append(f"  Max per task: {self.max_tags_per_task}")

        tags_help = self.get_help_text('tags')
        if tags_help:
            lines.append(f"  {tags_help}")
        lines.append("")

        return "\n".join(lines)

    # Project Root Detection Methods

    def find_project_root(self, start_path: Optional[str] = None) -> Optional[str]:
        """
        Find project root by searching for root markers.

        Args:
            start_path: Starting directory (default: current working directory)

        Returns:
            Path to project root or None if not found
        """
        if not self.config['project_root']['auto_detect']:
            return None

        current = Path(start_path) if start_path else Path.cwd()
        current = current.resolve()
        markers = self.config['project_root']['root_markers']
        max_depth = self.config['project_root']['max_depth']

        # Search up directory tree
        depth = 0
        while current != current.parent and depth < max_depth:
            for marker in markers:
                marker_path = current / marker
                if marker_path.exists():
                    return str(current)
            current = current.parent
            depth += 1

        return None

    def find_existing_juno_task_root(self, start_path: Optional[str] = None) -> Optional[str]:
        """
        Find existing .juno_task directory by searching upward.

        Args:
            start_path: Starting directory (default: current working directory)

        Returns:
            Path containing .juno_task directory or None if not found
        """
        current = Path(start_path) if start_path else Path.cwd()
        current = current.resolve()
        max_depth = self.config['project_root']['max_depth']

        # Search up directory tree
        depth = 0
        while current != current.parent and depth < max_depth:
            juno_task_path = current / ".juno_task"
            if juno_task_path.exists() and juno_task_path.is_dir():
                return str(current)
            current = current.parent
            depth += 1

        return None

    def get_recommended_root(self, start_path: Optional[str] = None) -> Optional[str]:
        """
        Get recommended root directory based on detection rules.

        Priority:
        1. Environment variable JUNO_TASK_ROOT
        2. Project root markers (git, etc.) - highest priority for prevention
        3. Existing .juno_task directory (upward search)

        Args:
            start_path: Starting directory (default: current working directory)

        Returns:
            Recommended root path or None
        """
        # Check environment variable first (highest priority)
        env_root = os.environ.get('JUNO_TASK_ROOT')
        if env_root and os.path.exists(env_root):
            return env_root

        # Find project root markers (prioritize over existing .juno_task)
        project_root = self.find_project_root(start_path)
        if project_root:
            return project_root

        # Fall back to existing .juno_task directory
        existing_root = self.find_existing_juno_task_root(start_path)
        if existing_root:
            return existing_root

        return None

    def validate_current_location(self, current_path: Optional[str] = None) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Validate current location against project root detection rules.

        Args:
            current_path: Current directory to validate (default: cwd)

        Returns:
            Tuple of (is_valid, warning_message, recommended_path)
        """
        if not self.config['project_root']['enable_prevention']:
            return True, None, None

        # Check environment variable override first
        env_root = os.environ.get('JUNO_TASK_ROOT')
        if env_root:
            # Environment variable explicitly allows this location
            return True, None, None

        current = str(Path(current_path).resolve()) if current_path else str(Path.cwd().resolve())
        recommended = self.get_recommended_root(current)

        if not recommended:
            # No recommended root found, allow operation
            return True, None, None

        recommended = str(Path(recommended).resolve())

        if current == recommended:
            # Current location is the recommended root
            return True, None, None

        behavior = self.config['project_root']['non_root_behavior']

        if behavior == 'allow':
            return True, None, None
        elif behavior == 'warn':
            warning = (
                f"Warning: Running from '{current}' but recommended root is '{recommended}'. "
                f"This may create scattered .juno_task directories. "
                f"Consider running from '{recommended}' instead."
            )
            return True, warning, recommended
        elif behavior == 'error':
            error = (
                f"Error: Must run from project root '{recommended}', not '{current}'. "
                f"Set JUNO_TASK_ROOT environment variable to override."
            )
            return False, error, recommended
        else:
            return True, None, None

    def get_global_config_path(self) -> str:
        """
        Get path to global configuration file.

        Returns:
            Path to ~/.yylo-ledger/config.json
        """
        return str(migrate_user_home() / "config.json")

    def load_global_config(self) -> Dict[str, Any]:
        """
        Load global configuration if it exists.

        Returns:
            Global configuration dictionary or empty dict
        """
        global_config_path = self.get_global_config_path()
        if os.path.exists(global_config_path):
            try:
                with open(global_config_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                return {}
        return {}

    def save_global_config(self, config: Dict[str, Any]):
        """
        Save global configuration.

        Args:
            config: Configuration to save
        """
        global_config_path = self.get_global_config_path()
        os.makedirs(os.path.dirname(global_config_path), exist_ok=True)

        with open(global_config_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)

    def to_dict(self) -> Dict[str, Any]:
        """Return configuration as dictionary."""
        return self.config.copy()


def init_config(path: str = ".juno_task/tasks/config.json", force: bool = False):
    """
    Initialize default configuration file.

    Args:
        path: Path to create config file
        force: Overwrite existing file
    """
    if os.path.exists(path) and not force:
        print(f"Config already exists: {path}")
        print("Use --force to overwrite")
        return

    os.makedirs(os.path.dirname(path), exist_ok=True)

    with open(path, 'w', encoding='utf-8') as f:
        json.dump(Config.DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)

    print(f"Created config file: {path}")
