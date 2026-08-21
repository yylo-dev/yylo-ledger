#!/usr/bin/env python3
"""
Validation logic for tasks.
"""

import re
from datetime import datetime
from typing import Optional, Tuple, List


class TaskValidator:
    """Validates task data."""

    # Regex patterns
    ID_PATTERN = re.compile(r'^[a-zA-Z0-9]{6}$')
    COMMIT_HASH_PATTERN = re.compile(r'^[a-f0-9]{7,40}$')
    TAG_PATTERN = re.compile(r'^[a-zA-Z0-9_-]{1,50}$')

    @staticmethod
    def validate_id(task_id: str) -> Tuple[bool, Optional[str]]:
        """
        Validate task ID.

        Args:
            task_id: Task ID to validate

        Returns:
            (is_valid, error_message)
        """
        if not isinstance(task_id, str):
            return False, "ID must be a string"

        if len(task_id) != 6:
            return False, "ID must be exactly 6 characters"

        if not task_id.isalnum():
            return False, "ID must be alphanumeric"

        if task_id.isdigit():
            return False, "ID cannot be only numeric"

        if task_id.isalpha():
            return False, "ID cannot be only alphabetic"

        return True, None

    @staticmethod
    def validate_status(status: str, allowed_values: List[str]) -> Tuple[bool, Optional[str]]:
        """
        Validate task status.

        Args:
            status: Status to validate
            allowed_values: List of allowed status values

        Returns:
            (is_valid, error_message)
        """
        if not isinstance(status, str):
            return False, "Status must be a string"

        if status not in allowed_values:
            return False, f"Invalid status: '{status}'. Allowed: {', '.join(allowed_values)}"

        return True, None

    @staticmethod
    def validate_status_transition(from_status: str,
                                   to_status: str,
                                   transitions: dict,
                                   enforce: bool) -> Tuple[bool, Optional[str]]:
        """
        Validate status transition.

        Args:
            from_status: Current status
            to_status: Target status
            transitions: Allowed transitions map
            enforce: Whether to enforce transitions

        Returns:
            (is_valid, error_message)
        """
        if not enforce:
            return True, None

        allowed = transitions.get(from_status, [])
        if to_status not in allowed:
            return False, f"Cannot transition from '{from_status}' to '{to_status}'. Allowed: {', '.join(allowed)}"

        return True, None

    @staticmethod
    def validate_commit_hash(commit_hash: Optional[str]) -> Tuple[bool, Optional[str]]:
        """
        Validate git commit hash.

        Args:
            commit_hash: Commit hash to validate

        Returns:
            (is_valid, error_message)
        """
        if commit_hash is None:
            return True, None

        if not isinstance(commit_hash, str):
            return False, "Commit hash must be a string or null"

        if not TaskValidator.COMMIT_HASH_PATTERN.match(commit_hash):
            return False, "Commit hash must be 7-40 hexadecimal characters"

        return True, None

    @staticmethod
    def validate_timestamp(timestamp: str) -> Tuple[bool, Optional[str]]:
        """
        Validate ISO 8601 timestamp.

        Args:
            timestamp: Timestamp to validate

        Returns:
            (is_valid, error_message)
        """
        try:
            # Parse ISO 8601 format
            datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            return True, None
        except (ValueError, AttributeError) as e:
            return False, f"Invalid timestamp format: {e}"

    @staticmethod
    def validate_tags(tags: Optional[List[str]],
                     max_tags: int = 20,
                     allowed_tags: Optional[List[str]] = None,
                     pattern: Optional[re.Pattern] = None) -> Tuple[bool, Optional[str]]:
        """
        Validate feature tags.

        Args:
            tags: Tags to validate
            max_tags: Maximum number of tags
            allowed_tags: Whitelist of allowed tags (None = any)
            pattern: Regex pattern for tag validation

        Returns:
            (is_valid, error_message)
        """
        if tags is None:
            return True, None

        if not isinstance(tags, list):
            return False, "feature_tags must be a list or null"

        if len(tags) > max_tags:
            return False, f"Too many tags. Max: {max_tags}, got: {len(tags)}"

        # Check uniqueness
        if len(set(tags)) != len(tags):
            return False, "Duplicate tags not allowed"

        # Validate each tag
        pattern = pattern or TaskValidator.TAG_PATTERN

        for tag in tags:
            if not isinstance(tag, str):
                return False, f"Tag must be string, got: {type(tag)}"

            if not pattern.match(tag):
                # Generate helpful error message with examples and suggestions
                invalid_chars = []
                if ' ' in tag:
                    invalid_chars.append("spaces")
                if ',' in tag:
                    invalid_chars.append("commas")
                if any(c in tag for c in '!@#$%^&*()+=[]{}|\\;:"\'<>?,./'):
                    invalid_chars.append("special characters")

                error_msg = f"Invalid tag format: '{tag}'\n\n"
                error_msg += "Tags can only contain letters, numbers, underscores (_), and hyphens (-).\n"

                if invalid_chars:
                    error_msg += f"Found: {', '.join(invalid_chars)} (not allowed)\n\n"

                error_msg += "Correct format examples:\n"
                error_msg += "  --tags backend urgent fix-auth\n"
                error_msg += "  --tags frontend_v1 initial feature\n\n"

                # Smart suggestions
                suggested = tag.replace(' ', '_').replace(',', '_')
                suggested = ''.join(c for c in suggested if c.isalnum() or c in '_-')
                if suggested and suggested != tag:
                    error_msg += f"Did you mean: '{suggested}'?"

                return False, error_msg

            # Check whitelist
            if allowed_tags is not None and tag not in allowed_tags:
                return False, f"Tag '{tag}' not in allowed list"

        return True, None

    @staticmethod
    def validate_blocked_by(blocked_by: Optional[List[str]]) -> Tuple[bool, Optional[str]]:
        """
        Validate blocked_by field (list of task IDs that must complete before this task).

        Args:
            blocked_by: List of blocking task IDs, or None

        Returns:
            (is_valid, error_message)
        """
        if blocked_by is None:
            return True, None

        if not isinstance(blocked_by, list):
            return False, "blocked_by must be a list or null"

        seen = set()
        for item in blocked_by:
            if not isinstance(item, str):
                return False, f"blocked_by entries must be strings, got: {type(item).__name__}"

            if not TaskValidator.ID_PATTERN.match(item):
                return False, f"Invalid task ID in blocked_by: '{item}'. Must be 6 alphanumeric characters"

            if item in seen:
                return False, f"Duplicate task ID in blocked_by: '{item}'"
            seen.add(item)

        return True, None

    @staticmethod
    def validate_body(body: str, max_length: int = 1048576) -> Tuple[bool, Optional[str]]:
        """
        Validate task body.

        Args:
            body: Body text to validate
            max_length: Maximum length in bytes

        Returns:
            (is_valid, error_message)
        """
        if not isinstance(body, str):
            return False, "Body must be a string"

        if len(body.encode('utf-8')) > max_length:
            return False, f"Body too large. Max: {max_length} bytes"

        return True, None

    @staticmethod
    def validate_task(task_dict: dict,
                     config: Optional[dict] = None) -> Tuple[bool, Optional[str]]:
        """
        Validate complete task.

        Args:
            task_dict: Task dictionary
            config: Configuration (optional)

        Returns:
            (is_valid, error_message)
        """
        # Check required fields
        required = ['id', 'status', 'body', 'created_date', 'last_modified']
        for field in required:
            if field not in task_dict:
                return False, f"Missing required field: {field}"

        # Validate ID
        is_valid, error = TaskValidator.validate_id(task_dict['id'])
        if not is_valid:
            return False, f"Invalid ID: {error}"

        # Validate status
        allowed_statuses = ['backlog', 'todo', 'in_progress', 'done', 'archive']
        if config:
            allowed_statuses = config.get('status_workflow', {}).get('values', allowed_statuses)

        is_valid, error = TaskValidator.validate_status(task_dict['status'], allowed_statuses)
        if not is_valid:
            return False, error

        # Validate body
        is_valid, error = TaskValidator.validate_body(task_dict['body'])
        if not is_valid:
            return False, error

        # Validate commit_hash
        if 'commit_hash' in task_dict:
            is_valid, error = TaskValidator.validate_commit_hash(task_dict['commit_hash'])
            if not is_valid:
                return False, error

        # Validate timestamps
        for field in ['created_date', 'last_modified']:
            is_valid, error = TaskValidator.validate_timestamp(task_dict[field])
            if not is_valid:
                return False, f"{field}: {error}"

        # Validate tags
        if 'feature_tags' in task_dict:
            max_tags = 20
            allowed_tags = None
            pattern = None

            if config:
                tag_config = config.get('feature_tags', {})
                max_tags = tag_config.get('max_tags_per_task', 20)
                allowed_tags = tag_config.get('allowed_tags')
                pattern_str = tag_config.get('validation_pattern')
                if pattern_str:
                    pattern = re.compile(pattern_str)

            is_valid, error = TaskValidator.validate_tags(
                task_dict['feature_tags'],
                max_tags=max_tags,
                allowed_tags=allowed_tags,
                pattern=pattern
            )
            if not is_valid:
                return False, error

        # Validate blocked_by
        if 'blocked_by' in task_dict:
            is_valid, error = TaskValidator.validate_blocked_by(task_dict['blocked_by'])
            if not is_valid:
                return False, error

        return True, None


class ValidationError(Exception):
    """Raised when validation fails."""
    pass