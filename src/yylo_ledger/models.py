#!/usr/bin/env python3
"""
Task model and operations.
"""

import json
import random
import re
import string
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple, Set

from .validators import TaskValidator, ValidationError
from .codec import order_task_fields


def _is_valid_reference_task_id(task_id: str) -> bool:
    """Validate extracted reference IDs using the same task ID rules as task objects."""
    is_valid, _ = TaskValidator.validate_id(task_id)
    return is_valid


def _extract_task_ids_from_text(text: str) -> List[str]:
    """
    Extract valid task IDs from free-form text.

    Supports optional braces around IDs (e.g. {Ab3cD4}) and ignores non-ID prose.
    """
    if not text:
        return []

    pattern = r'(?<![A-Za-z0-9])\{?([A-Za-z0-9]{6})\}?(?![A-Za-z0-9])'
    extracted: List[str] = []

    for match in re.finditer(pattern, text):
        candidate = match.group(1)
        if _is_valid_reference_task_id(candidate):
            extracted.append(candidate)

    return extracted


def _extract_hash_reference_segments(body: str) -> List[Tuple[int, str]]:
    """
    Extract ``##``-based related-task reference segments with source positions.

    Supported forms:
    - inline open marker (line-scoped): ``## Ab3cD4`` / ``##Ab3cD4``
    - explicit same-line block: ``## Ab3cD4 Xy1zW2 ##``

    Markers are treated as reference markers only when they are exactly ``##``
    (not part of ``###`` markdown heading syntax).
    """
    if not body:
        return []

    marker_pattern = re.compile(r'(?<!#)##(?!#)')
    marker_matches = list(marker_pattern.finditer(body))

    segments: List[Tuple[int, str]] = []
    i = 0

    while i < len(marker_matches):
        marker_start = marker_matches[i].start()
        content_start = marker_start + 2

        line_end = body.find('\n', content_start)
        if line_end == -1:
            line_end = len(body)

        next_marker = marker_matches[i + 1] if (i + 1) < len(marker_matches) else None

        if next_marker and next_marker.start() <= line_end:
            content_end = next_marker.start()
            i += 2  # consume opening + closing marker
        else:
            content_end = line_end
            i += 1  # only opening marker, parse to end-of-line

        segments.append((marker_start, body[content_start:content_end]))

    return segments


def parse_related_task_ids(body: str) -> List[str]:
    """
    Parse related task IDs from body text.

    Supported markup:
    - ``[task_id]ABC123[/task_id]`` / ``[task_id]ABC123[/]``
    - ``[task_id]ABC123, DEF456[/task_id]``
    - ``## ABC123`` / ``##ABC123``
    - ``## ABC123 DEF456 ##``

    IDs are deduplicated while preserving discovery order across both syntaxes.
    """
    if not body:
        return []

    tagged_pattern = r'\[task_id\](.*?)\[/(?:task_id)?\]'

    segments: List[Tuple[int, str]] = []

    for match in re.finditer(tagged_pattern, body, re.IGNORECASE | re.DOTALL):
        segments.append((match.start(), match.group(1)))

    segments.extend(_extract_hash_reference_segments(body))
    segments.sort(key=lambda item: item[0])

    found_ids: List[str] = []
    seen: Set[str] = set()

    for _, content in segments:
        for task_id in _extract_task_ids_from_text(content):
            if task_id not in seen:
                found_ids.append(task_id)
                seen.add(task_id)

    return found_ids


def parse_blocked_by_ids(body: str) -> List[str]:
    """
    Parse blocked-by task IDs from body text using multiple synonym tags.

    Supported tags (all case-insensitive, all map to blocked_by field):
    - [blocked_by]ABC123[/blocked_by] or [blocked_by]ABC123[/]
    - [block_by]ABC123[/block_by] or [block_by]ABC123[/]
    - [block]ABC123[/block] or [block]ABC123[/]
    - [parent_task]ABC123[/parent_task] or [parent_task]ABC123[/]

    Multiple IDs can be comma or space separated within tags.
    Multiple tags in the same body are merged and deduplicated.

    Args:
        body: Task body text to parse

    Returns:
        List of unique task IDs found, preserving discovery order
    """
    if not body:
        return []

    # Pattern to match any of the 4 synonym tags with flexible close form
    # Tags: blocked_by, block_by, block, parent_task
    pattern = r'\[(?:blocked_by|block_by|block|parent_task)\](.*?)\[/(?:blocked_by|block_by|block|parent_task)?\]'

    found_ids: List[str] = []
    seen: Set[str] = set()

    matches = re.findall(pattern, body, re.IGNORECASE | re.DOTALL)

    for match in matches:
        # Split by comma or whitespace to get individual IDs
        content = match.replace(',', ' ')
        potential_ids = content.split()

        for potential_id in potential_ids:
            task_id = potential_id.strip()
            if not task_id:
                continue
            if task_id not in seen:
                found_ids.append(task_id)
                seen.add(task_id)

    return found_ids


def validate_task_ids(task_ids: List[str], storage) -> Tuple[List[str], List[str]]:
    """
    Validate that task IDs exist in the kanban.

    Args:
        task_ids: List of task IDs to validate
        storage: TaskStorage instance to check task existence

    Returns:
        Tuple of (valid_ids, invalid_ids)
    """
    valid_ids = []
    invalid_ids = []

    for task_id in task_ids:
        finder = getattr(storage, 'find_task_exact', storage.find_task)
        task = finder(task_id)
        if task:
            valid_ids.append(task_id)
        else:
            invalid_ids.append(task_id)

    return valid_ids, invalid_ids


class Task:
    """Represents a kanban task."""

    def __init__(self,
                 id: Optional[str] = None,
                 status: str = "backlog",
                 body: str = "",
                 commit_hash: Optional[str] = None,
                 agent_response: str = "",
                 created_date: Optional[str] = None,
                 last_modified: Optional[str] = None,
                 feature_tags: Optional[List[str]] = None,
                 related_tasks: Optional[List[str]] = None,
                 blocked_by: Optional[List[str]] = None,
                 fields: Optional[Dict[str, Any]] = None,
                 validate: bool = True,
                 config: Optional[dict] = None,
                 **unknown_fields):
        """
        Initialize a task.

        Args:
            id: Task ID (auto-generated if not provided)
            status: Task status
            body: Task description
            commit_hash: Git commit hash
            agent_response: AI agent response
            created_date: Creation timestamp (auto-generated if not provided)
            last_modified: Last modification timestamp (auto-generated if not provided)
            feature_tags: List of feature tags
            related_tasks: List of related task IDs (parsed from body or explicit)
            blocked_by: List of task IDs that must be completed before this task
            validate: Whether to validate task data
            config: Configuration for validation
        """
        self.id = id or self._generate_id()
        self.status = status
        self.body = body
        self.commit_hash = commit_hash
        self.agent_response = agent_response
        self.created_date = created_date or self._get_timestamp()
        self.last_modified = last_modified or self._get_timestamp()
        self.feature_tags = feature_tags
        self.related_tasks = related_tasks
        self.blocked_by = blocked_by
        self.fields = fields or {}
        # Unknown top-level values are retained losslessly for forward compatibility.
        self.unknown_fields = dict(unknown_fields)

        # Validate task if requested
        if validate:
            self._validate(config)

    def _validate(self, config: Optional[dict] = None):
        """Validate task data."""
        is_valid, error = TaskValidator.validate_task(self.to_dict(), config)
        if not is_valid:
            raise ValidationError(error)

    @staticmethod
    def _generate_id() -> str:
        """
        Generate a unique 6-character alphanumeric task ID.
        Ensures mix of letters and numbers (not only numeric).

        Returns:
            6-character ID with mix of letters and numbers
        """
        chars = string.ascii_letters + string.digits
        letters = string.ascii_letters
        digits = string.digits

        # Ensure at least 1 letter and 1 number
        task_id = [
            random.choice(letters),
            random.choice(digits),
            random.choice(chars),
            random.choice(chars),
            random.choice(chars),
            random.choice(chars)
        ]

        # Shuffle to randomize positions
        random.shuffle(task_id)
        return ''.join(task_id)

    @staticmethod
    def _get_timestamp() -> str:
        """
        Get current timestamp without timezone and milliseconds.

        Returns:
            Timestamp in YYYY-MM-DD HH:MM:SS format
        """
        return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

    def to_dict(self) -> Dict[str, Any]:
        """Convert task to a lossless dictionary."""
        result = dict(self.unknown_fields)
        result.update({
            "id": self.id,
            "status": self.status,
            "body": self.body,
            "commit_hash": self.commit_hash,
            "agent_response": self.agent_response,
            "created_date": self.created_date,
            "last_modified": self.last_modified,
            "feature_tags": self.feature_tags,
            "related_tasks": self.related_tasks,
            "blocked_by": self.blocked_by,
        })
        if self.fields:
            result["fields"] = self.fields
        return dict(order_task_fields(result))

    def to_ndjson(self) -> str:
        """
        Convert task to NDJSON format (single line JSON).
        Uses ensure_ascii=False to handle Unicode properly.

        Returns:
            NDJSON string
        """
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: Dict[str, Any], validate: bool = True, config: Optional[dict] = None) -> 'Task':
        """
        Create task from dictionary.

        Args:
            data: Task dictionary
            validate: Whether to validate task data
            config: Configuration for validation

        Returns:
            Task instance
        """
        core = {'id', 'status', 'body', 'commit_hash', 'agent_response', 'created_date',
                'last_modified', 'feature_tags', 'related_tasks', 'blocked_by', 'fields'}
        return cls(
            id=data.get('id'),
            status=data.get('status', 'backlog'),
            body=data.get('body', ''),
            commit_hash=data.get('commit_hash'),
            agent_response=data.get('agent_response', ''),
            created_date=data.get('created_date'),
            last_modified=data.get('last_modified'),
            feature_tags=data.get('feature_tags'),
            related_tasks=data.get('related_tasks'),
            blocked_by=data.get('blocked_by'),
            fields=data.get('fields') or {},
            validate=validate,
            config=config,
            **{key: value for key, value in data.items() if key not in core}
        )

    @classmethod
    def from_ndjson(cls, line: str, validate: bool = True, config: Optional[dict] = None) -> 'Task':
        """
        Create task from NDJSON line.

        Args:
            line: NDJSON line
            validate: Whether to validate task data
            config: Configuration for validation

        Returns:
            Task instance

        Raises:
            json.JSONDecodeError: If line is not valid JSON
            ValidationError: If task data is invalid
        """
        data = json.loads(line.strip())
        return cls.from_dict(data, validate=validate, config=config)

    def update(self, config: Optional[dict] = None, **kwargs):
        """
        Update task fields.

        Args:
            config: Configuration for validation
            **kwargs: Fields to update (status, agent_response, commit_hash, feature_tags, body, related_tasks)

        Raises:
            ValidationError: If updated data is invalid
        """
        allowed_updates = ['status', 'agent_response', 'commit_hash', 'feature_tags', 'body', 'related_tasks', 'blocked_by', 'fields']

        # Validate status transition if changing status
        if 'status' in kwargs and config:
            workflow = config.get('status_workflow', {})
            if workflow.get('enforce_transitions', False):
                transitions = workflow.get('transitions', {})
                is_valid, error = TaskValidator.validate_status_transition(
                    self.status, kwargs['status'], transitions, True
                )
                if not is_valid:
                    raise ValidationError(error)

        # Update fields
        for key, value in kwargs.items():
            if key in allowed_updates:
                setattr(self, key, value)

        # Update last_modified timestamp
        self.last_modified = self._get_timestamp()

        # Validate updated task
        self._validate(config)

    def is_open(self) -> bool:
        """
        Check if task is open (has no agent_response).

        Returns:
            True if task is open (no agent response)
        """
        return not self.agent_response or self.agent_response.strip() == ""

    def has_tag(self, tag: str) -> bool:
        """
        Check if task has a specific tag.

        Args:
            tag: Tag to check for

        Returns:
            True if task has the tag
        """
        return self.feature_tags is not None and tag in self.feature_tags

    def add_tag(self, tag: str, config: Optional[dict] = None):
        """
        Add a tag to the task.

        Args:
            tag: Tag to add
            config: Configuration for validation

        Raises:
            ValidationError: If tag is invalid
        """
        if self.feature_tags is None:
            self.feature_tags = []

        if tag not in self.feature_tags:
            self.feature_tags.append(tag)
            self.last_modified = self._get_timestamp()

            # Validate tags
            max_tags = 20
            allowed_tags = None
            pattern = None

            if config:
                tag_config = config.get('feature_tags', {})
                max_tags = tag_config.get('max_tags_per_task', 20)
                allowed_tags = tag_config.get('allowed_tags')
                pattern_str = tag_config.get('validation_pattern')
                if pattern_str:
                    import re
                    pattern = re.compile(pattern_str)

            is_valid, error = TaskValidator.validate_tags(
                self.feature_tags, max_tags, allowed_tags, pattern
            )
            if not is_valid:
                # Remove the tag we just added
                self.feature_tags.remove(tag)
                raise ValidationError(error)

    def remove_tag(self, tag: str):
        """
        Remove a tag from the task.

        Args:
            tag: Tag to remove
        """
        if self.feature_tags and tag in self.feature_tags:
            self.feature_tags.remove(tag)
            self.last_modified = self._get_timestamp()

    def age_days(self) -> int:
        """
        Get task age in days.

        Returns:
            Number of days since task creation
        """
        created = datetime.fromisoformat(self.created_date.replace('Z', '+00:00'))
        now = datetime.now().astimezone()
        return (now - created).days

    def __repr__(self) -> str:
        """String representation of task."""
        body_preview = self.body[:50] + "..." if len(self.body) > 50 else self.body
        return f"Task(id={self.id}, status={self.status}, body='{body_preview}')"

    def __str__(self) -> str:
        """Human-readable string representation."""
        tags_str = f", tags={self.feature_tags}" if self.feature_tags else ""
        return f"[{self.id}] {self.status}: {self.body[:100]}{tags_str}"

    def __eq__(self, other) -> bool:
        """Check equality based on task ID."""
        if not isinstance(other, Task):
            return False
        return self.id == other.id

    def __hash__(self) -> int:
        """Hash based on task ID."""
        return hash(self.id)