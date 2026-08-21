#!/usr/bin/env python3
"""
Search implementation with ripgrep integration and Python fallback.
"""

import subprocess
import shutil
import json
import re
import glob
import os
from typing import List, Dict, Optional, Any, Union
from dataclasses import dataclass

from .config import Config
from .storage import TaskStorage


OPEN_STATUSES = ('backlog', 'todo', 'in_progress')
CLOSED_STATUSES = ('done', 'archive')


def normalize_sort_order(sort_order: Optional[str]) -> str:
    """Normalize sort order to supported values.

    Args:
        sort_order: Requested sort order value.

    Returns:
        'asc' for oldest-first ordering, otherwise 'desc' (newest-first).
    """
    return 'asc' if sort_order == 'asc' else 'desc'


def sort_tasks_by_last_modified(tasks: List[Dict[str, Any]], sort_order: str = 'desc') -> List[Dict[str, Any]]:
    """Sort tasks by last_modified with deterministic ID tie-break.

    Args:
        tasks: Tasks to sort.
        sort_order: Sort direction ('asc' or 'desc').

    Returns:
        New sorted task list.
    """
    normalized_order = normalize_sort_order(sort_order)
    reverse_sort = normalized_order == 'desc'
    return sorted(
        tasks,
        key=lambda task: (task.get('last_modified', ''), task.get('id', '')),
        reverse=reverse_sort,
    )


def sort_tasks_with_status_priority(tasks: List[Dict[str, Any]], sort_order: str = 'desc') -> List[Dict[str, Any]]:
    """Sort tasks with list-command status grouping and shared time ordering.

    Group order is always open -> closed -> other. Within each group, task order
    follows `sort_tasks_by_last_modified` semantics.
    """
    ordered_by_time = sort_tasks_by_last_modified(tasks, sort_order)

    open_tasks = [task for task in ordered_by_time if task.get('status') in OPEN_STATUSES]
    closed_tasks = [task for task in ordered_by_time if task.get('status') in CLOSED_STATUSES]
    other_tasks = [
        task
        for task in ordered_by_time
        if task.get('status') not in OPEN_STATUSES and task.get('status') not in CLOSED_STATUSES
    ]

    return open_tasks + closed_tasks + other_tasks


def normalize_status_sequence(statuses: Optional[Union[str, List[str]]]) -> List[str]:
    """Normalize status filters into a deterministic, de-duplicated sequence.

    Why: list/ready should respect the user's explicit --status order when provided,
    while keeping one shared normalization path for parser and sorting logic.
    """
    if not statuses:
        return []
    if isinstance(statuses, str):
        candidates = [statuses]
    else:
        candidates = list(statuses)

    ordered_unique: List[str] = []
    seen = set()
    for status in candidates:
        if status and status not in seen:
            ordered_unique.append(status)
            seen.add(status)
    return ordered_unique


def sort_tasks_by_status_sequence(
    tasks: List[Dict[str, Any]],
    status_sequence: Optional[Union[str, List[str]]],
    sort_order: str = 'desc',
) -> List[Dict[str, Any]]:
    """Sort tasks by caller-provided status sequence, then by last_modified.

    Tasks whose status is not present in `status_sequence` are appended at the end,
    still respecting shared `last_modified` ordering.
    """
    normalized_statuses = normalize_status_sequence(status_sequence)
    if not normalized_statuses:
        return sort_tasks_by_last_modified(tasks, sort_order)

    ordered_by_time = sort_tasks_by_last_modified(tasks, sort_order)
    by_status = {status: [] for status in normalized_statuses}
    overflow: List[Dict[str, Any]] = []

    for task in ordered_by_time:
        task_status = task.get('status')
        if task_status in by_status:
            by_status[task_status].append(task)
        else:
            overflow.append(task)

    ordered: List[Dict[str, Any]] = []
    for status in normalized_statuses:
        ordered.extend(by_status.get(status, []))
    ordered.extend(overflow)
    return ordered


def check_ripgrep() -> bool:
    """
    Check if ripgrep is installed.

    Returns:
        True if ripgrep is available
    """
    return shutil.which('rg') is not None


def get_ripgrep_version() -> Optional[str]:
    """
    Get ripgrep version.

    Returns:
        Version string or None
    """
    try:
        result = subprocess.run(
            ['rg', '--version'],
            capture_output=True,
            text=True,
            check=True
        )
        # Output: ripgrep 13.0.0
        return result.stdout.split('\n')[0].split()[1]
    except (subprocess.CalledProcessError, FileNotFoundError, IndexError):
        return None


@dataclass
class SearchFilters:
    """Search filter criteria."""
    id: Optional[str] = None
    status: Optional[Union[str, List[str]]] = None  # Support both single value and list
    tag: Optional[Union[str, List[str]]] = None     # Support both single value and list
    exclude_tags: Optional[Union[str, List[str]]] = None  # Support both single value and list for exclusion
    commit_hash: Optional[str] = None
    body_text: Optional[str] = None
    response_text: Optional[str] = None  # Search in agent_response field
    open_only: bool = False
    recent: bool = False
    limit: int = 5
    sort_order: str = 'desc'
    case_sensitive: bool = False


class RipgrepSearch:
    """High-performance search using ripgrep."""

    def __init__(self, base_path: str, file_pattern: str = "*.ndjson"):
        """
        Initialize ripgrep search.

        Args:
            base_path: Base directory for task files
            file_pattern: File pattern to search
        """
        self.base_path = base_path
        self.file_pattern = file_pattern
        self.rg_available = check_ripgrep()

    def _get_files(self) -> List[str]:
        """Get list of files to search."""
        search_pattern = os.path.join(self.base_path, self.file_pattern)
        return sorted(glob.glob(search_pattern, recursive=True))

    def _run_ripgrep(self, pattern: str, limit: int = None, fixed_strings: bool = True) -> List[str]:
        """
        Run ripgrep with common options.

        Args:
            pattern: Search pattern
            limit: Max results (None for no limit - Issue 28: needed for proper sorting)
            fixed_strings: Use literal string matching

        Returns:
            List of matching lines
        """
        if not self.rg_available:
            return []

        files = self._get_files()
        if not files:
            return []

        args = [
            'rg',
            '--no-heading',
            '--no-line-number',
        ]

        # Only add --max-count if limit is specified (Issue 28: avoid pre-sorting limits)
        if limit is not None:
            args.extend(['--max-count', str(limit)])

        if fixed_strings:
            args.append('--fixed-strings')

        args.extend([pattern] + files)

        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                check=False
            )

            if result.returncode == 0 and result.stdout:
                return [line.strip() for line in result.stdout.strip().split('\n') if line.strip()]

            return []

        except subprocess.CalledProcessError:
            return []

    def search_by_id(self, task_id: str) -> Optional[Dict[str, Any]]:
        """
        Search for task by exact ID using ripgrep.

        Args:
            task_id: Task ID to find

        Returns:
            Task dict or None
        """
        # Pattern: "id": "a1b2c3" (with space after colon)
        pattern = f'"id": "{task_id}"'
        lines = self._run_ripgrep(pattern, limit=1)

        if lines:
            try:
                return json.loads(lines[0])
            except json.JSONDecodeError:
                pass
        return None

    def search_by_status(self, status: str, limit: int = 5, sort_order: str = 'desc') -> List[Dict[str, Any]]:
        """
        Search for tasks by status using ripgrep.

        Args:
            status: Status to filter
            limit: Max results
            sort_order: Sort direction for last_modified ('asc' or 'desc')

        Returns:
            List of task dicts
        """
        # Pattern: "status": "in_progress" (with space after colon)
        pattern = f'"status": "{status}"'
        # Issue 28: Get all results first, then sort and limit
        lines = self._run_ripgrep(pattern, limit=None)

        tasks = []
        for line in lines:
            try:
                task = json.loads(line)
                tasks.append(task)
            except json.JSONDecodeError:
                continue

        tasks = sort_tasks_by_last_modified(tasks, sort_order)
        return tasks[:limit]

    def search_by_tag(self, tag: str, limit: int = 5, sort_order: str = 'desc') -> List[Dict[str, Any]]:
        """
        Search for tasks by tag using ripgrep.

        Args:
            tag: Tag to filter
            limit: Max results
            sort_order: Sort direction for last_modified ('asc' or 'desc')

        Returns:
            List of task dicts
        """
        # Pattern: search for tag in feature_tags field (with space after colon)
        pattern = f'"feature_tags": \\[.*"{tag}".*\\]'
        # Issue 28: Get all results first, then sort and limit
        lines = self._run_ripgrep(pattern, limit=None, fixed_strings=False)

        tasks = []
        for line in lines:
            try:
                task = json.loads(line)
                # Verify tag is actually in the list (regex can be imprecise)
                if task.get('feature_tags') and tag in task['feature_tags']:
                    tasks.append(task)
            except json.JSONDecodeError:
                continue

        tasks = sort_tasks_by_last_modified(tasks, sort_order)
        return tasks[:limit]

    def search_by_commit(self, commit_hash: str, limit: int = 5, sort_order: str = 'desc') -> List[Dict[str, Any]]:
        """
        Search for tasks by commit hash using ripgrep.

        Args:
            commit_hash: Commit hash to filter
            limit: Max results
            sort_order: Sort direction for last_modified ('asc' or 'desc')

        Returns:
            List of task dicts
        """
        # Pattern: "commit_hash": "abc123" (with space after colon)
        pattern = f'"commit_hash": "{commit_hash}"'
        # Issue 28: Get all results first, then sort and limit
        lines = self._run_ripgrep(pattern, limit=None)

        tasks = []
        for line in lines:
            try:
                task = json.loads(line)
                tasks.append(task)
            except json.JSONDecodeError:
                continue

        tasks = sort_tasks_by_last_modified(tasks, sort_order)
        return tasks[:limit]

    def search_in_body(self, text: str, limit: int = 5, case_sensitive: bool = False, sort_order: str = 'desc') -> List[Dict[str, Any]]:
        """
        Search for text in task body using ripgrep.

        Args:
            text: Text to search for
            limit: Max results
            case_sensitive: Case sensitive search
            sort_order: Sort direction for last_modified ('asc' or 'desc')

        Returns:
            List of task dicts
        """
        # Search for text in the body field (with space after colon)
        pattern = f'"body": "[^"]*{re.escape(text)}[^"]*"'

        if not self.rg_available:
            return []

        files = self._get_files()
        if not files:
            return []

        args = [
            'rg',
            '--no-heading',
            '--no-line-number',
        ]

        # Issue 28: Don't use --max-count to allow proper sorting

        if not case_sensitive:
            args.append('--ignore-case')

        args.extend([pattern] + files)

        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                check=False
            )

            if result.returncode == 0 and result.stdout:
                tasks = []
                for line in result.stdout.strip().split('\n'):
                    if line.strip():
                        try:
                            task = json.loads(line)
                            tasks.append(task)
                        except json.JSONDecodeError:
                            continue

                tasks = sort_tasks_by_last_modified(tasks, sort_order)
                return tasks[:limit]

            return []

        except subprocess.CalledProcessError:
            return []

    def search_in_response(self, text: str, limit: int = 5, case_sensitive: bool = False, sort_order: str = 'desc') -> List[Dict[str, Any]]:
        """
        Search for text in agent_response field using ripgrep.

        Args:
            text: Text to search for
            limit: Max results
            case_sensitive: Case sensitive search
            sort_order: Sort direction for last_modified ('asc' or 'desc')

        Returns:
            List of task dicts
        """
        # Search for text in the agent_response field (with space after colon)
        pattern = f'"agent_response": "[^"]*{re.escape(text)}[^"]*"'

        if not self.rg_available:
            return []

        files = self._get_files()
        if not files:
            return []

        args = [
            'rg',
            '--no-heading',
            '--no-line-number',
        ]

        # Issue 28: Don't use --max-count to allow proper sorting

        if not case_sensitive:
            args.append('--ignore-case')

        args.extend([pattern] + files)

        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                check=False
            )

            if result.returncode == 0 and result.stdout:
                tasks = []
                for line in result.stdout.strip().split('\n'):
                    if line.strip():
                        try:
                            task = json.loads(line)
                            tasks.append(task)
                        except json.JSONDecodeError:
                            continue

                tasks = sort_tasks_by_last_modified(tasks, sort_order)
                return tasks[:limit]

            return []

        except subprocess.CalledProcessError:
            return []


class PythonSearch:
    """Python-based search fallback."""

    def __init__(self, storage: TaskStorage):
        """
        Initialize Python search.

        Args:
            storage: TaskStorage instance
        """
        self.storage = storage

    def search_all(self, filters: SearchFilters) -> List[Dict[str, Any]]:
        """
        Search using Python with all filters.

        Args:
            filters: Search criteria

        Returns:
            List of task dicts
        """
        tasks = []

        # Sorting is part of query truth.  Stopping after an arbitrary prefix
        # can omit newer matches merely because their paths sort later, and at
        # scale it also corrupts collection summaries.  Read the complete
        # canonical match set; cache-backed fast paths may optimize this only
        # when they preserve identical semantics.
        for task in self.storage.read_all_tasks():
            if self._matches_filters(task, filters):
                tasks.append(task)

        # Apply sorting and final limit
        tasks = sort_tasks_by_last_modified(tasks, filters.sort_order)

        return tasks[:filters.limit]

    def search_all_prioritized(
        self,
        filters: SearchFilters,
        sort_order: str = 'desc',
        status_sequence: Optional[Union[str, List[str]]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Search using Python with list-command sorting semantics.

        Default behavior: open issues (backlog, todo, in_progress) first, then closed issues.
        If an explicit status sequence is provided, respect that status order first and
        apply shared last_modified sorting inside each status bucket.

        Args:
            filters: Search criteria
            sort_order: Sort direction for last_modified - 'asc' (oldest first) or 'desc' (newest first)
            status_sequence: Optional status order from user --status filter.

        Returns:
            List of task dicts with prioritized sorting
        """
        tasks = []

        # Get all tasks that match filters
        for task in self.storage.read_all_tasks():
            if self._matches_filters(task, filters):
                tasks.append(task)

        normalized_status_sequence = normalize_status_sequence(status_sequence)
        if normalized_status_sequence:
            prioritized_tasks = sort_tasks_by_status_sequence(tasks, normalized_status_sequence, sort_order)
        else:
            # Preserve historical default behavior when no explicit status order was requested.
            prioritized_tasks = sort_tasks_with_status_priority(tasks, sort_order)

        return prioritized_tasks[:filters.limit]

    def _matches_filters(self, task: Dict[str, Any], filters: SearchFilters) -> bool:
        """
        Check if task matches all filters.

        Args:
            task: Task dictionary
            filters: Search criteria

        Returns:
            True if task matches all filters
        """
        # ID filter
        if filters.id and task.get('id') != filters.id:
            return False

        # Status filter (supports single value or list for OR logic)
        if filters.status:
            task_status = task.get('status')
            if isinstance(filters.status, list):
                if task_status not in filters.status:
                    return False
            else:
                if task_status != filters.status:
                    return False

        # Tag filter (supports single value or list for OR logic)
        if filters.tag:
            task_tags = task.get('feature_tags', [])
            if not task_tags:
                return False

            if isinstance(filters.tag, list):
                # Check if any of the filter tags match any of the task tags
                if not any(filter_tag in task_tags for filter_tag in filters.tag):
                    return False
            else:
                # Single tag filter
                if filters.tag not in task_tags:
                    return False

        # Tag exclusion filter (supports single value or list for OR logic)
        if filters.exclude_tags:
            task_tags = task.get('feature_tags', [])
            if task_tags:  # Only check if task has tags
                if isinstance(filters.exclude_tags, list):
                    # Exclude if any of the exclude tags match any of the task tags
                    if any(exclude_tag in task_tags for exclude_tag in filters.exclude_tags):
                        return False
                else:
                    # Single exclude tag
                    if filters.exclude_tags in task_tags:
                        return False

        # Commit hash filter
        if filters.commit_hash and task.get('commit_hash') != filters.commit_hash:
            return False

        # Body text filter
        if filters.body_text:
            body = task.get('body', '')
            if filters.case_sensitive:
                if filters.body_text not in body:
                    return False
            else:
                if filters.body_text.lower() not in body.lower():
                    return False

        # Response text filter
        if filters.response_text:
            response = task.get('agent_response', '')
            if filters.case_sensitive:
                if filters.response_text not in response:
                    return False
            else:
                if filters.response_text.lower() not in response.lower():
                    return False

        # Open tasks filter
        if filters.open_only:
            agent_response = task.get('agent_response', '').strip()
            if agent_response:
                return False

        return True


class TaskSearch:
    """Main search interface with automatic backend selection."""

    def __init__(self, config: Optional[Config] = None, storage: Optional[TaskStorage] = None):
        """
        Initialize task search.

        Args:
            config: Configuration object
            storage: TaskStorage instance
        """
        self.config = config or Config()
        self.storage = storage or TaskStorage(self.config)

        # Initialize search backends
        self.ripgrep = RipgrepSearch(
            self.config.storage_base_path,
            self.config.storage_file_pattern
        )
        self.python_search = PythonSearch(self.storage)

        # Markdown records span multiple lines and must be decoded through the safe
        # codec; line-oriented NDJSON ripgrep parsing is intentionally disabled.
        self.ripgrep_available = False

    def search(self, filters: SearchFilters) -> List[Dict[str, Any]]:
        """
        Search tasks using optimal backend.

        Args:
            filters: Search criteria

        Returns:
            List of task dictionaries
        """
        # For single ID lookup, try ripgrep first
        if filters.id and not any([filters.status, filters.tag, filters.exclude_tags, filters.commit_hash,
                                   filters.body_text, filters.response_text, filters.open_only, filters.recent]):
            try:
                result = self.storage.find_task_exact(filters.id)
            except ValueError:
                result = None
            return [result] if result else []

        # For simple single-field searches, try ripgrep (only for string values, not lists)
        # NOTE: Exclude optimizations if exclude_tags is present - need Python filtering
        if self.ripgrep_available and not filters.open_only and not filters.recent and not filters.exclude_tags:
            if filters.status and isinstance(filters.status, str) and not any([filters.id, filters.tag, filters.commit_hash, filters.body_text, filters.response_text]):
                return self.ripgrep.search_by_status(filters.status, filters.limit, filters.sort_order)

            if filters.tag and isinstance(filters.tag, str) and not any([filters.id, filters.status, filters.commit_hash, filters.body_text, filters.response_text]):
                return self.ripgrep.search_by_tag(filters.tag, filters.limit, filters.sort_order)

            if filters.commit_hash and not any([filters.id, filters.status, filters.tag, filters.body_text, filters.response_text]):
                return self.ripgrep.search_by_commit(filters.commit_hash, filters.limit, filters.sort_order)

            if filters.body_text and not any([filters.id, filters.status, filters.tag, filters.commit_hash, filters.response_text]):
                return self.ripgrep.search_in_body(filters.body_text, filters.limit, filters.case_sensitive, filters.sort_order)

            if filters.response_text and not any([filters.id, filters.status, filters.tag, filters.commit_hash, filters.body_text]):
                return self.ripgrep.search_in_response(filters.response_text, filters.limit, filters.case_sensitive, filters.sort_order)

        # Fall back to Python search for complex queries
        return self.python_search.search_all(filters)

    def search_by_id(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Quick ID lookup."""
        filters = SearchFilters(id=task_id, limit=1)
        results = self.search(filters)
        return results[0] if results else None

    def search_by_status(self, status: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Quick status search."""
        filters = SearchFilters(status=status, limit=limit)
        return self.search(filters)

    def search_by_tag(self, tag: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Quick tag search."""
        filters = SearchFilters(tag=tag, limit=limit)
        return self.search(filters)

    def search_open_tasks(self, limit: int = 5) -> List[Dict[str, Any]]:
        """Get open tasks (no agent response)."""
        filters = SearchFilters(open_only=True, limit=limit)
        return self.search(filters)

    def search_recent_tasks(self, limit: int = 5) -> List[Dict[str, Any]]:
        """Get recent tasks."""
        filters = SearchFilters(recent=True, limit=limit)
        return self.search(filters)

    def search_prioritized_list(
        self,
        limit: int = 5,
        filters: Optional[SearchFilters] = None,
        sort_order: str = 'desc',
        status_sequence: Optional[Union[str, List[str]]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Get tasks with list-command sorting semantics.

        Default behavior keeps open->closed grouping. If a status sequence is supplied,
        it becomes the primary status-bucket order.

        Args:
            limit: Maximum number of tasks to return
            filters: Optional search filters to apply
            sort_order: Sort direction for last_modified - 'asc' (oldest first) or 'desc' (newest first)
            status_sequence: Optional user-provided status order.

        Returns:
            List of tasks with prioritized sorting
        """
        if filters is None:
            filters = SearchFilters(limit=limit)
        else:
            # Update the limit in the provided filters
            filters.limit = limit
        return self.python_search.search_all_prioritized(filters, sort_order, status_sequence)

    def sort_tasks_by_last_modified(self, tasks: List[Dict[str, Any]], sort_order: str = 'desc') -> List[Dict[str, Any]]:
        """Sort arbitrary task lists with the shared last_modified ordering contract."""
        return sort_tasks_by_last_modified(tasks, sort_order)

    def sort_tasks_by_status_sequence(
        self,
        tasks: List[Dict[str, Any]],
        status_sequence: Optional[Union[str, List[str]]],
        sort_order: str = 'desc',
    ) -> List[Dict[str, Any]]:
        """Sort arbitrary task lists by explicit status sequence + shared time ordering."""
        return sort_tasks_by_status_sequence(tasks, status_sequence, sort_order)

    def get_info(self) -> Dict[str, Any]:
        """Get search backend information."""
        return {
            'ripgrep_available': self.ripgrep_available,
            'ripgrep_version': get_ripgrep_version() if self.ripgrep_available else None,
            'backend': 'ripgrep + python' if self.ripgrep_available else 'python only',
            'base_path': self.config.storage_base_path,
            'file_pattern': self.config.storage_file_pattern
        }