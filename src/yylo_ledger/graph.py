#!/usr/bin/env python3
"""
Dependency graph engine for task dependency resolution.

Builds a directed acyclic graph (DAG) from tasks' blocked_by fields and provides
queries for topological sorting, cycle detection, ready/blocked task queries,
and priority scoring. All operations use stdlib only (zero external dependencies).

Why: Enables dependency-aware task scheduling for parallel execution. Without this,
parallel_runner dispatches tasks in arbitrary order with no guarantee that prerequisites
are met. The graph engine answers "what can I work on now?" and "in what order should
tasks be completed?" — critical for AI agent orchestration.
"""

from collections import deque
from typing import Dict, List, Optional, Set


# Statuses that mean a blocker is resolved (task is finished)
_RESOLVED_STATUSES = frozenset(('done', 'archive'))

# Statuses that mean a task is actionable (can be worked on)
_ACTIONABLE_STATUSES = frozenset(('backlog', 'todo', 'in_progress'))


class DependencyGraph:
    """
    In-memory directed acyclic graph (DAG) built from task data.

    Each task's blocked_by field defines edges: if task B has blocked_by=[A],
    then there is an edge A -> B (A must complete before B can start).

    All methods operate on task IDs (strings). The graph is immutable after
    construction — to reflect status changes, create a new DependencyGraph.
    """

    def __init__(self, tasks: List[Dict]):
        """
        Build graph from task list.

        Args:
            tasks: List of task dicts, each with at minimum 'id' and 'status'.
                   Optional 'blocked_by' field (list of task IDs or None).
        """
        # Task ID -> task dict (for status lookups)
        self._tasks: Dict[str, Dict] = {}
        # Forward edges: blocker_id -> set of task IDs it blocks
        self._forward: Dict[str, Set[str]] = {}
        # Reverse edges: task_id -> set of blocker IDs (what blocks this task)
        self._reverse: Dict[str, Set[str]] = {}

        for task in tasks:
            task_id = task['id']
            self._tasks[task_id] = task
            if task_id not in self._forward:
                self._forward[task_id] = set()
            if task_id not in self._reverse:
                self._reverse[task_id] = set()

        # Build edges from blocked_by fields
        for task in tasks:
            task_id = task['id']
            blocked_by = task.get('blocked_by') or []
            for blocker_id in blocked_by:
                # Missing forward references remain unresolved dependencies.
                self._forward.setdefault(blocker_id, set()).add(task_id)
                self._reverse[task_id].add(blocker_id)

        # Cache for priority scores (memoized)
        self._priority_cache: Dict[str, int] = {}

    def get_ready_tasks(self, include_statuses: Optional[List[str]] = None) -> List[str]:
        """
        Tasks that can be worked on now.

        A task is ready when:
        1. Its status is in include_statuses (default: backlog, todo, in_progress)
        2. ALL of its blockers exist and are resolved (status done or archive)

        Args:
            include_statuses: Which task statuses to consider. Default: backlog, todo, in_progress.

        Returns:
            List of task IDs that are ready, sorted by ID for deterministic output.
        """
        if include_statuses is None:
            statuses = _ACTIONABLE_STATUSES
        else:
            statuses = frozenset(include_statuses)

        ready = []
        for task_id, task in self._tasks.items():
            if task.get('status') not in statuses:
                continue

            # Check all blockers
            blockers = self._reverse.get(task_id, set())
            all_met = True
            for blocker_id in blockers:
                blocker = self._tasks.get(blocker_id)
                if not blocker or blocker.get('status') not in _RESOLVED_STATUSES:
                    all_met = False
                    break

            if all_met:
                ready.append(task_id)

        return sorted(ready)

    def get_blocked_tasks(self) -> List[str]:
        """
        Tasks that have at least one open (unresolved) blocker.

        Only considers tasks with actionable statuses (backlog, todo, in_progress).

        Returns:
            List of task IDs that are blocked, sorted by ID.
        """
        blocked = []
        for task_id, task in self._tasks.items():
            if task.get('status') not in _ACTIONABLE_STATUSES:
                continue

            blockers = self._reverse.get(task_id, set())
            for blocker_id in blockers:
                blocker = self._tasks.get(blocker_id)
                if not blocker or blocker.get('status') not in _RESOLVED_STATUSES:
                    blocked.append(task_id)
                    break

        return sorted(blocked)

    def get_blockers(self, task_id: str) -> List[str]:
        """
        Direct blockers: what must complete before this task can start.

        Args:
            task_id: The task to query.

        Returns:
            List of blocker task IDs, sorted. Empty if task has no blockers
            or doesn't exist in the graph.
        """
        return sorted(self._reverse.get(task_id, set()))

    def get_dependents(self, task_id: str) -> List[str]:
        """
        Direct dependents: what tasks are blocked by this one.

        Args:
            task_id: The task to query.

        Returns:
            List of dependent task IDs, sorted. Empty if nothing depends on
            this task or it doesn't exist in the graph.
        """
        return sorted(self._forward.get(task_id, set()))

    def topological_sort(self) -> List[str]:
        """
        Execution order respecting all dependencies (Kahn's algorithm, BFS).

        Returns tasks in an order where every task appears after all of its
        blockers. Tasks at the same depth are sorted by ID for determinism.

        Returns:
            List of task IDs in topological order.

        Raises:
            ValueError: If the graph contains a cycle (should not happen if
                        cycle detection is used on writes).
        """
        # Compute in-degree for each node (only counting edges within the graph)
        in_degree: Dict[str, int] = {tid: 0 for tid in self._tasks}
        for task_id, blockers in self._reverse.items():
            if task_id in in_degree:
                in_degree[task_id] = len(blockers)

        # Start with nodes that have no blockers (in-degree 0)
        queue = deque(sorted(tid for tid, deg in in_degree.items() if deg == 0))
        result: List[str] = []

        while queue:
            node = queue.popleft()
            result.append(node)

            # Reduce in-degree for all dependents
            next_batch = []
            for dependent in self._forward.get(node, set()):
                if dependent in in_degree:
                    in_degree[dependent] -= 1
                    if in_degree[dependent] == 0:
                        next_batch.append(dependent)

            # Sort the batch for deterministic ordering within each level
            for dep in sorted(next_batch):
                queue.append(dep)

        if len(result) != len(self._tasks):
            # Some tasks weren't reached — cycle exists
            remaining = set(self._tasks.keys()) - set(result)
            raise ValueError(
                f"Dependency cycle detected. Tasks involved: {sorted(remaining)}"
            )

        return result

    def detect_cycle(self, from_id: str, to_id: str) -> Optional[List[str]]:
        """
        Would adding an edge from_id -> to_id create a cycle?

        This checks: "if to_id becomes blocked_by from_id, is there a cycle?"
        A cycle exists if there's already a path from to_id to from_id
        (because adding from_id -> to_id would close the loop).

        Also detects self-dependency (from_id == to_id).

        Args:
            from_id: The blocker task (would be added to blocked_by).
            to_id: The dependent task (the one that would be blocked).

        Returns:
            The cycle path as a list of task IDs if a cycle would be created,
            or None if the edge is safe. For self-dependency returns [from_id, from_id].
        """
        # Self-dependency
        if from_id == to_id:
            return [from_id, from_id]

        # Check if there's already a path from to_id to from_id
        # If yes, adding from_id -> to_id would create: from_id -> to_id -> ... -> from_id
        path = self._find_path(to_id, from_id)
        if path is not None:
            # Return the full cycle: from_id -> to_id -> ... -> from_id
            return [from_id] + path
        return None

    def _find_path(self, start: str, end: str) -> Optional[List[str]]:
        """
        Find a path from start to end using BFS on forward edges.

        Args:
            start: Starting task ID.
            end: Target task ID.

        Returns:
            List of task IDs from start to end (inclusive), or None if no path exists.
        """
        if start not in self._tasks or end not in self._tasks:
            return None

        # BFS to find path
        queue = deque([(start, [start])])
        visited: Set[str] = {start}

        while queue:
            node, path = queue.popleft()
            if node == end:
                return path

            for neighbor in sorted(self._forward.get(node, set())):
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, path + [neighbor]))

        return None

    def get_priority_score(self, task_id: str) -> int:
        """
        Count of tasks that transitively depend on this one.

        Higher score = more critical to complete (unblocks more downstream work).
        A leaf task (nothing depends on it) has score 0.

        Uses memoized DFS for efficiency.

        Args:
            task_id: The task to score.

        Returns:
            Number of transitive dependents. 0 if task doesn't exist or is a leaf.
        """
        if task_id not in self._tasks:
            return 0

        if task_id in self._priority_cache:
            return self._priority_cache[task_id]

        # DFS to collect all transitive dependents
        visited: Set[str] = set()
        stack = list(self._forward.get(task_id, set()))

        while stack:
            node = stack.pop()
            if node in visited:
                continue
            visited.add(node)
            stack.extend(self._forward.get(node, set()))

        score = len(visited)
        self._priority_cache[task_id] = score
        return score

    def get_critical_path(self) -> List[str]:
        """
        Longest dependency chain in the graph.

        Uses topological sort + dynamic programming to find the longest path.
        In case of ties, prefers the path with lexicographically smaller IDs.

        Returns:
            List of task IDs forming the longest dependency chain, in order.
            Empty list if graph is empty or has no edges.
        """
        if not self._tasks:
            return []

        try:
            order = self.topological_sort()
        except ValueError:
            # Cycle in graph — can't compute critical path
            return []

        # dp[node] = length of longest path ending at node
        dp: Dict[str, int] = {tid: 1 for tid in order}
        # parent[node] = previous node on the longest path
        parent: Dict[str, Optional[str]] = {tid: None for tid in order}

        for node in order:
            for dependent in self._forward.get(node, set()):
                if dp[node] + 1 > dp[dependent]:
                    dp[dependent] = dp[node] + 1
                    parent[dependent] = node

        # Find the endpoint of the longest path
        if not dp:
            return []

        end_node = max(dp, key=lambda tid: (dp[tid], tid))

        # If longest path is 1, there are no edges — no meaningful chain
        if dp[end_node] <= 1:
            return []

        # Reconstruct path
        path: List[str] = []
        node: Optional[str] = end_node
        while node is not None:
            path.append(node)
            node = parent[node]

        path.reverse()
        return path
