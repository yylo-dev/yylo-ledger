"""Tests for kanban.graph module — DependencyGraph engine.

Why: The dependency graph engine determines task execution order and readiness.
Bugs here could cause agents to start work on tasks whose prerequisites aren't met,
skip critical tasks, or get stuck in infinite loops from undetected cycles. Every
algorithm (topological sort, cycle detection, priority scoring) must be tested against
known graph topologies to ensure correctness.
"""

import pytest
from kanban.graph import DependencyGraph


# ---------------------------------------------------------------------------
# Helpers — reusable task fixtures
# ---------------------------------------------------------------------------

def _task(tid, status='todo', blocked_by=None):
    """Shorthand to create a task dict."""
    return {'id': tid, 'status': status, 'blocked_by': blocked_by}


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

class TestGraphConstruction:
    """DependencyGraph builds adjacency lists correctly from task data."""

    def test_empty_graph(self):
        g = DependencyGraph([])
        assert g.get_ready_tasks() == []
        assert g.get_blocked_tasks() == []
        assert g.topological_sort() == []
        assert g.get_critical_path() == []

    def test_single_task_no_deps(self):
        g = DependencyGraph([_task('A1b2C3')])
        assert g.get_ready_tasks() == ['A1b2C3']
        assert g.get_blocked_tasks() == []
        assert g.get_blockers('A1b2C3') == []
        assert g.get_dependents('A1b2C3') == []

    def test_blocked_by_none_treated_as_no_deps(self):
        g = DependencyGraph([_task('A1b2C3', blocked_by=None)])
        assert g.get_ready_tasks() == ['A1b2C3']

    def test_blocked_by_empty_list_treated_as_no_deps(self):
        g = DependencyGraph([_task('A1b2C3', blocked_by=[])])
        assert g.get_ready_tasks() == ['A1b2C3']

    def test_nonexistent_blocker_remains_unmet(self):
        """A forward reference cannot silently satisfy declared dependency truth."""
        g = DependencyGraph([_task('A1b2C3', blocked_by=['XXXXXX'])])
        assert g.get_ready_tasks() == []
        assert g.get_blocked_tasks() == ['A1b2C3']


# ---------------------------------------------------------------------------
# Ready / Blocked queries
# ---------------------------------------------------------------------------

class TestReadyTasks:
    """get_ready_tasks returns tasks with all blockers resolved."""

    def test_linear_chain_only_root_ready(self):
        tasks = [
            _task('A1b2C3'),
            _task('D4e5F6', blocked_by=['A1b2C3']),
            _task('G7h8I9', blocked_by=['D4e5F6']),
        ]
        g = DependencyGraph(tasks)
        assert g.get_ready_tasks() == ['A1b2C3']

    def test_after_root_done_next_becomes_ready(self):
        tasks = [
            _task('A1b2C3', status='done'),
            _task('D4e5F6', blocked_by=['A1b2C3']),
            _task('G7h8I9', blocked_by=['D4e5F6']),
        ]
        g = DependencyGraph(tasks)
        assert g.get_ready_tasks() == ['D4e5F6']

    def test_after_all_done_leaf_ready(self):
        tasks = [
            _task('A1b2C3', status='done'),
            _task('D4e5F6', status='done', blocked_by=['A1b2C3']),
            _task('G7h8I9', blocked_by=['D4e5F6']),
        ]
        g = DependencyGraph(tasks)
        assert g.get_ready_tasks() == ['G7h8I9']

    def test_done_tasks_not_in_ready(self):
        """Done/archive tasks themselves should not appear as ready."""
        tasks = [
            _task('A1b2C3', status='done'),
            _task('D4e5F6', status='archive'),
        ]
        g = DependencyGraph(tasks)
        assert g.get_ready_tasks() == []

    def test_archive_blocker_treated_as_resolved(self):
        tasks = [
            _task('A1b2C3', status='archive'),
            _task('D4e5F6', blocked_by=['A1b2C3']),
        ]
        g = DependencyGraph(tasks)
        assert g.get_ready_tasks() == ['D4e5F6']

    def test_diamond_both_roots_ready(self):
        """Diamond: A->C, B->C. Both A and B are ready, C is blocked."""
        tasks = [
            _task('Aaaaaa'),
            _task('Bbbbbb'),
            _task('Cccccc', blocked_by=['Aaaaaa', 'Bbbbbb']),
        ]
        g = DependencyGraph(tasks)
        assert g.get_ready_tasks() == ['Aaaaaa', 'Bbbbbb']
        assert g.get_blocked_tasks() == ['Cccccc']

    def test_diamond_one_root_done_still_blocked(self):
        """Diamond: C needs both A and B done."""
        tasks = [
            _task('Aaaaaa', status='done'),
            _task('Bbbbbb'),
            _task('Cccccc', blocked_by=['Aaaaaa', 'Bbbbbb']),
        ]
        g = DependencyGraph(tasks)
        assert g.get_ready_tasks() == ['Bbbbbb']
        assert g.get_blocked_tasks() == ['Cccccc']

    def test_diamond_both_roots_done_c_ready(self):
        tasks = [
            _task('Aaaaaa', status='done'),
            _task('Bbbbbb', status='done'),
            _task('Cccccc', blocked_by=['Aaaaaa', 'Bbbbbb']),
        ]
        g = DependencyGraph(tasks)
        assert g.get_ready_tasks() == ['Cccccc']
        assert g.get_blocked_tasks() == []

    def test_custom_include_statuses(self):
        tasks = [
            _task('Aaaaaa', status='backlog'),
            _task('Bbbbbb', status='todo'),
            _task('Cccccc', status='in_progress'),
        ]
        g = DependencyGraph(tasks)
        assert g.get_ready_tasks(include_statuses=['backlog']) == ['Aaaaaa']
        assert g.get_ready_tasks(include_statuses=['todo', 'in_progress']) == ['Bbbbbb', 'Cccccc']

    def test_in_progress_task_is_ready(self):
        """in_progress tasks with met deps are ready (agents can see what's active)."""
        tasks = [
            _task('Aaaaaa', status='done'),
            _task('Bbbbbb', status='in_progress', blocked_by=['Aaaaaa']),
        ]
        g = DependencyGraph(tasks)
        assert g.get_ready_tasks() == ['Bbbbbb']

    def test_multiple_independent_tasks_all_ready(self):
        tasks = [
            _task('Aaaaaa'),
            _task('Bbbbbb'),
            _task('Cccccc'),
        ]
        g = DependencyGraph(tasks)
        assert g.get_ready_tasks() == ['Aaaaaa', 'Bbbbbb', 'Cccccc']


class TestBlockedTasks:
    """get_blocked_tasks returns tasks with at least one open blocker."""

    def test_no_blocked_in_flat_graph(self):
        tasks = [_task('Aaaaaa'), _task('Bbbbbb')]
        g = DependencyGraph(tasks)
        assert g.get_blocked_tasks() == []

    def test_blocked_with_open_blocker(self):
        tasks = [
            _task('Aaaaaa'),
            _task('Bbbbbb', blocked_by=['Aaaaaa']),
        ]
        g = DependencyGraph(tasks)
        assert g.get_blocked_tasks() == ['Bbbbbb']

    def test_not_blocked_when_blocker_done(self):
        tasks = [
            _task('Aaaaaa', status='done'),
            _task('Bbbbbb', blocked_by=['Aaaaaa']),
        ]
        g = DependencyGraph(tasks)
        assert g.get_blocked_tasks() == []

    def test_done_tasks_not_in_blocked_list(self):
        """A done task with open blockers shouldn't appear in blocked list."""
        tasks = [
            _task('Aaaaaa'),
            _task('Bbbbbb', status='done', blocked_by=['Aaaaaa']),
        ]
        g = DependencyGraph(tasks)
        assert g.get_blocked_tasks() == []


# ---------------------------------------------------------------------------
# Blocker / Dependent lookups
# ---------------------------------------------------------------------------

class TestBlockersDependents:
    """get_blockers and get_dependents return direct relationships."""

    def test_get_blockers_returns_direct_blockers(self):
        tasks = [
            _task('Aaaaaa'),
            _task('Bbbbbb'),
            _task('Cccccc', blocked_by=['Aaaaaa', 'Bbbbbb']),
        ]
        g = DependencyGraph(tasks)
        assert g.get_blockers('Cccccc') == ['Aaaaaa', 'Bbbbbb']

    def test_get_blockers_empty_for_root(self):
        g = DependencyGraph([_task('Aaaaaa')])
        assert g.get_blockers('Aaaaaa') == []

    def test_get_blockers_unknown_task(self):
        g = DependencyGraph([_task('Aaaaaa')])
        assert g.get_blockers('XXXXXX') == []

    def test_get_dependents_returns_direct_dependents(self):
        tasks = [
            _task('Aaaaaa'),
            _task('Bbbbbb', blocked_by=['Aaaaaa']),
            _task('Cccccc', blocked_by=['Aaaaaa']),
        ]
        g = DependencyGraph(tasks)
        assert g.get_dependents('Aaaaaa') == ['Bbbbbb', 'Cccccc']

    def test_get_dependents_empty_for_leaf(self):
        tasks = [
            _task('Aaaaaa'),
            _task('Bbbbbb', blocked_by=['Aaaaaa']),
        ]
        g = DependencyGraph(tasks)
        assert g.get_dependents('Bbbbbb') == []

    def test_get_dependents_unknown_task(self):
        g = DependencyGraph([_task('Aaaaaa')])
        assert g.get_dependents('XXXXXX') == []

    def test_blockers_include_missing_forward_references(self):
        """Dependency projection retains missing blockers as unresolved truth."""
        g = DependencyGraph([_task('Aaaaaa', blocked_by=['XXXXXX'])])
        assert g.get_blockers('Aaaaaa') == ['XXXXXX']


# ---------------------------------------------------------------------------
# Topological sort
# ---------------------------------------------------------------------------

class TestTopologicalSort:
    """topological_sort returns valid execution order."""

    def test_linear_chain(self):
        tasks = [
            _task('A1b2C3'),
            _task('D4e5F6', blocked_by=['A1b2C3']),
            _task('G7h8I9', blocked_by=['D4e5F6']),
        ]
        g = DependencyGraph(tasks)
        order = g.topological_sort()
        assert order.index('A1b2C3') < order.index('D4e5F6') < order.index('G7h8I9')

    def test_diamond(self):
        """Diamond: A->C, B->C. Both A,B before C."""
        tasks = [
            _task('Aaaaaa'),
            _task('Bbbbbb'),
            _task('Cccccc', blocked_by=['Aaaaaa', 'Bbbbbb']),
        ]
        g = DependencyGraph(tasks)
        order = g.topological_sort()
        assert order.index('Aaaaaa') < order.index('Cccccc')
        assert order.index('Bbbbbb') < order.index('Cccccc')

    def test_complex_dag(self):
        """
        A -> B -> D
        A -> C -> D
        """
        tasks = [
            _task('Aaaaaa'),
            _task('Bbbbbb', blocked_by=['Aaaaaa']),
            _task('Cccccc', blocked_by=['Aaaaaa']),
            _task('Dddddd', blocked_by=['Bbbbbb', 'Cccccc']),
        ]
        g = DependencyGraph(tasks)
        order = g.topological_sort()
        assert order[0] == 'Aaaaaa'  # A must be first
        assert order[-1] == 'Dddddd'  # D must be last
        assert order.index('Bbbbbb') < order.index('Dddddd')
        assert order.index('Cccccc') < order.index('Dddddd')

    def test_independent_tasks_sorted_by_id(self):
        """Tasks with no dependencies between them are sorted by ID."""
        tasks = [_task('Cccccc'), _task('Aaaaaa'), _task('Bbbbbb')]
        g = DependencyGraph(tasks)
        order = g.topological_sort()
        assert order == ['Aaaaaa', 'Bbbbbb', 'Cccccc']

    def test_includes_all_statuses(self):
        """Topological sort includes done/archive tasks too (for full ordering)."""
        tasks = [
            _task('Aaaaaa', status='done'),
            _task('Bbbbbb', status='todo', blocked_by=['Aaaaaa']),
        ]
        g = DependencyGraph(tasks)
        order = g.topological_sort()
        assert order == ['Aaaaaa', 'Bbbbbb']

    def test_single_task(self):
        g = DependencyGraph([_task('Aaaaaa')])
        assert g.topological_sort() == ['Aaaaaa']

    @pytest.mark.parametrize('resolved_status', ['done', 'archive'])
    def test_resolved_blocker_does_not_contribute_in_degree(self, resolved_status):
        tasks = [
            _task('Aaaaaa', status=resolved_status),
            _task('Bbbbbb', blocked_by=['Aaaaaa']),
            _task('Cccccc', blocked_by=['Bbbbbb']),
        ]
        order = DependencyGraph(tasks).topological_sort()
        assert order.index('Bbbbbb') < order.index('Cccccc')

    def test_missing_blocker_is_not_reported_as_cycle(self):
        graph = DependencyGraph([_task('Aaaaaa', blocked_by=['XXXXXX'])])
        with pytest.raises(ValueError, match='Unresolved dependencies.*Missing blockers') as error:
            graph.topological_sort()
        assert 'cycle' not in str(error.value).lower()


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------

class TestCycleDetection:
    """detect_cycle catches all cycle types and returns the cycle path."""

    def test_self_dependency(self):
        g = DependencyGraph([_task('Aaaaaa')])
        cycle = g.detect_cycle('Aaaaaa', 'Aaaaaa')
        assert cycle == ['Aaaaaa', 'Aaaaaa']

    def test_direct_cycle_a_b(self):
        """A->B exists. Adding B->A would create A->B->A."""
        tasks = [
            _task('Aaaaaa'),
            _task('Bbbbbb', blocked_by=['Aaaaaa']),
        ]
        g = DependencyGraph(tasks)
        cycle = g.detect_cycle('Bbbbbb', 'Aaaaaa')
        assert cycle is not None
        # Cycle should be: Bbbbbb -> Aaaaaa -> Bbbbbb
        assert cycle[0] == 'Bbbbbb'
        assert cycle[-1] == 'Bbbbbb'

    def test_transitive_cycle(self):
        """A->B->C exists. Adding C->A would create A->B->C->A."""
        tasks = [
            _task('Aaaaaa'),
            _task('Bbbbbb', blocked_by=['Aaaaaa']),
            _task('Cccccc', blocked_by=['Bbbbbb']),
        ]
        g = DependencyGraph(tasks)
        cycle = g.detect_cycle('Cccccc', 'Aaaaaa')
        assert cycle is not None
        assert cycle[0] == 'Cccccc'
        assert cycle[-1] == 'Cccccc'
        assert len(cycle) == 4  # Cccccc -> Aaaaaa -> Bbbbbb -> Cccccc

    def test_no_cycle_when_safe(self):
        """A->B exists. Adding C->A is safe (no cycle)."""
        tasks = [
            _task('Aaaaaa'),
            _task('Bbbbbb', blocked_by=['Aaaaaa']),
            _task('Cccccc'),
        ]
        g = DependencyGraph(tasks)
        assert g.detect_cycle('Cccccc', 'Aaaaaa') is None

    def test_no_cycle_parallel_deps(self):
        """A->C, B->C exists. Adding D->A is safe."""
        tasks = [
            _task('Aaaaaa'),
            _task('Bbbbbb'),
            _task('Cccccc', blocked_by=['Aaaaaa', 'Bbbbbb']),
            _task('Dddddd'),
        ]
        g = DependencyGraph(tasks)
        assert g.detect_cycle('Dddddd', 'Aaaaaa') is None

    def test_cycle_detection_with_nonexistent_from_id(self):
        """If from_id isn't in the graph, no path can exist — safe."""
        g = DependencyGraph([_task('Aaaaaa')])
        assert g.detect_cycle('XXXXXX', 'Aaaaaa') is None

    def test_cycle_detection_with_nonexistent_to_id(self):
        """If to_id isn't in the graph, no path can exist — safe."""
        g = DependencyGraph([_task('Aaaaaa')])
        assert g.detect_cycle('Aaaaaa', 'XXXXXX') is None

    def test_diamond_no_false_cycle(self):
        """Diamond A->C, B->C, A->B. Adding D->A is safe even with complex topology."""
        tasks = [
            _task('Aaaaaa'),
            _task('Bbbbbb', blocked_by=['Aaaaaa']),
            _task('Cccccc', blocked_by=['Aaaaaa', 'Bbbbbb']),
            _task('Dddddd'),
        ]
        g = DependencyGraph(tasks)
        assert g.detect_cycle('Dddddd', 'Aaaaaa') is None

    def test_topological_sort_raises_on_cycle(self):
        """topological_sort raises ValueError if graph has a cycle (shouldn't happen with validation)."""
        # Manually build a graph with a cycle by using blocked_by
        # A blocked_by C, B blocked_by A, C blocked_by B => cycle
        tasks = [
            _task('Aaaaaa', blocked_by=['Cccccc']),
            _task('Bbbbbb', blocked_by=['Aaaaaa']),
            _task('Cccccc', blocked_by=['Bbbbbb']),
        ]
        g = DependencyGraph(tasks)
        with pytest.raises(ValueError, match="cycle"):
            g.topological_sort()


# ---------------------------------------------------------------------------
# Priority score
# ---------------------------------------------------------------------------

class TestPriorityScore:
    """get_priority_score counts transitive dependents."""

    def test_leaf_has_score_zero(self):
        g = DependencyGraph([_task('Aaaaaa')])
        assert g.get_priority_score('Aaaaaa') == 0

    def test_root_blocks_two(self):
        """A -> B, A -> C. A's score = 2."""
        tasks = [
            _task('Aaaaaa'),
            _task('Bbbbbb', blocked_by=['Aaaaaa']),
            _task('Cccccc', blocked_by=['Aaaaaa']),
        ]
        g = DependencyGraph(tasks)
        assert g.get_priority_score('Aaaaaa') == 2
        assert g.get_priority_score('Bbbbbb') == 0
        assert g.get_priority_score('Cccccc') == 0

    def test_linear_chain_scores(self):
        """A -> B -> C. A=2, B=1, C=0."""
        tasks = [
            _task('Aaaaaa'),
            _task('Bbbbbb', blocked_by=['Aaaaaa']),
            _task('Cccccc', blocked_by=['Bbbbbb']),
        ]
        g = DependencyGraph(tasks)
        assert g.get_priority_score('Aaaaaa') == 2
        assert g.get_priority_score('Bbbbbb') == 1
        assert g.get_priority_score('Cccccc') == 0

    def test_diamond_scores(self):
        """A->C, B->C. A=1, B=1, C=0."""
        tasks = [
            _task('Aaaaaa'),
            _task('Bbbbbb'),
            _task('Cccccc', blocked_by=['Aaaaaa', 'Bbbbbb']),
        ]
        g = DependencyGraph(tasks)
        assert g.get_priority_score('Aaaaaa') == 1
        assert g.get_priority_score('Bbbbbb') == 1
        assert g.get_priority_score('Cccccc') == 0

    def test_complex_transitive_score(self):
        """
        A -> B -> D
        A -> C -> D
        A's score = 3 (B, C, D transitively)
        """
        tasks = [
            _task('Aaaaaa'),
            _task('Bbbbbb', blocked_by=['Aaaaaa']),
            _task('Cccccc', blocked_by=['Aaaaaa']),
            _task('Dddddd', blocked_by=['Bbbbbb', 'Cccccc']),
        ]
        g = DependencyGraph(tasks)
        assert g.get_priority_score('Aaaaaa') == 3
        assert g.get_priority_score('Bbbbbb') == 1
        assert g.get_priority_score('Cccccc') == 1
        assert g.get_priority_score('Dddddd') == 0

    def test_unknown_task_score_zero(self):
        g = DependencyGraph([_task('Aaaaaa')])
        assert g.get_priority_score('XXXXXX') == 0

    def test_score_is_cached(self):
        """Calling get_priority_score twice returns same result (cached)."""
        tasks = [
            _task('Aaaaaa'),
            _task('Bbbbbb', blocked_by=['Aaaaaa']),
        ]
        g = DependencyGraph(tasks)
        assert g.get_priority_score('Aaaaaa') == 1
        assert g.get_priority_score('Aaaaaa') == 1  # Hits cache


# ---------------------------------------------------------------------------
# Critical path
# ---------------------------------------------------------------------------

class TestCriticalPath:
    """get_critical_path finds the longest dependency chain."""

    def test_empty_graph(self):
        g = DependencyGraph([])
        assert g.get_critical_path() == []

    def test_single_task_no_edges(self):
        g = DependencyGraph([_task('Aaaaaa')])
        assert g.get_critical_path() == []

    def test_linear_chain_is_critical_path(self):
        tasks = [
            _task('Aaaaaa'),
            _task('Bbbbbb', blocked_by=['Aaaaaa']),
            _task('Cccccc', blocked_by=['Bbbbbb']),
        ]
        g = DependencyGraph(tasks)
        assert g.get_critical_path() == ['Aaaaaa', 'Bbbbbb', 'Cccccc']

    def test_diamond_picks_longest(self):
        """
        A -> B -> D (length 3)
        A -> D      (length 2)
        Critical path: A -> B -> D
        """
        tasks = [
            _task('Aaaaaa'),
            _task('Bbbbbb', blocked_by=['Aaaaaa']),
            _task('Dddddd', blocked_by=['Aaaaaa', 'Bbbbbb']),
        ]
        g = DependencyGraph(tasks)
        path = g.get_critical_path()
        assert path == ['Aaaaaa', 'Bbbbbb', 'Dddddd']

    def test_parallel_chains_picks_longest(self):
        """
        A -> B (length 2)
        C -> D -> E (length 3)
        Critical path: C -> D -> E
        """
        tasks = [
            _task('Aaaaaa'),
            _task('Bbbbbb', blocked_by=['Aaaaaa']),
            _task('Cccccc'),
            _task('Dddddd', blocked_by=['Cccccc']),
            _task('Eeeeee', blocked_by=['Dddddd']),
        ]
        g = DependencyGraph(tasks)
        path = g.get_critical_path()
        assert path == ['Cccccc', 'Dddddd', 'Eeeeee']

    def test_critical_path_with_cycle_returns_empty(self):
        """If graph has a cycle, critical path returns empty (can't compute)."""
        tasks = [
            _task('Aaaaaa', blocked_by=['Cccccc']),
            _task('Bbbbbb', blocked_by=['Aaaaaa']),
            _task('Cccccc', blocked_by=['Bbbbbb']),
        ]
        g = DependencyGraph(tasks)
        assert g.get_critical_path() == []


# ---------------------------------------------------------------------------
# Integration: real-world-ish scenarios
# ---------------------------------------------------------------------------

class TestIntegrationScenarios:
    """End-to-end scenarios combining multiple operations."""

    def test_task_completion_flow(self):
        """Simulate completing tasks and checking readiness."""
        # Phase 1: all todo, A->B->C
        tasks = [
            _task('Aaaaaa'),
            _task('Bbbbbb', blocked_by=['Aaaaaa']),
            _task('Cccccc', blocked_by=['Bbbbbb']),
        ]
        g1 = DependencyGraph(tasks)
        assert g1.get_ready_tasks() == ['Aaaaaa']
        assert g1.get_blocked_tasks() == ['Bbbbbb', 'Cccccc']

        # Phase 2: A done
        tasks[0]['status'] = 'done'
        g2 = DependencyGraph(tasks)
        assert g2.get_ready_tasks() == ['Bbbbbb']
        assert g2.get_blocked_tasks() == ['Cccccc']

        # Phase 3: A done, B done
        tasks[1]['status'] = 'done'
        g3 = DependencyGraph(tasks)
        assert g3.get_ready_tasks() == ['Cccccc']
        assert g3.get_blocked_tasks() == []

        # Phase 4: all done
        tasks[2]['status'] = 'done'
        g4 = DependencyGraph(tasks)
        assert g4.get_ready_tasks() == []
        assert g4.get_blocked_tasks() == []

    def test_large_graph_performance(self):
        """100 tasks in a chain — verify operations complete (no O(n!) blowup)."""
        tasks = [_task(f'T{i:05d}') for i in range(100)]
        for i in range(1, 100):
            tasks[i]['blocked_by'] = [tasks[i-1]['id']]

        g = DependencyGraph(tasks)

        # Only first task is ready
        assert g.get_ready_tasks() == ['T00000']

        # Topological sort should be in order
        order = g.topological_sort()
        assert order == [f'T{i:05d}' for i in range(100)]

        # Priority: first task has highest score
        assert g.get_priority_score('T00000') == 99
        assert g.get_priority_score('T00099') == 0

        # Critical path is the entire chain
        path = g.get_critical_path()
        assert len(path) == 100

    def test_mixed_statuses(self):
        """Graph with tasks in various statuses."""
        tasks = [
            _task('Aaaaaa', status='done'),
            _task('Bbbbbb', status='in_progress', blocked_by=['Aaaaaa']),
            _task('Cccccc', status='todo', blocked_by=['Bbbbbb']),
            _task('Dddddd', status='backlog', blocked_by=['Cccccc']),
            _task('Eeeeee', status='archive'),
        ]
        g = DependencyGraph(tasks)

        # B is ready (A is done), C and D are blocked
        ready = g.get_ready_tasks()
        assert 'Bbbbbb' in ready
        assert 'Cccccc' not in ready
        assert 'Dddddd' not in ready
        assert 'Aaaaaa' not in ready  # done
        assert 'Eeeeee' not in ready  # archive

    def test_wide_fan_out(self):
        """One task blocks many others."""
        tasks = [_task('Root01')]
        for i in range(10):
            tasks.append(_task(f'Fan{i:04d}', blocked_by=['Root01']))

        g = DependencyGraph(tasks)
        assert g.get_ready_tasks() == ['Root01']
        assert len(g.get_blocked_tasks()) == 10
        assert g.get_priority_score('Root01') == 10

    def test_wide_fan_in(self):
        """Many tasks must complete before one can start."""
        tasks = []
        blocker_ids = []
        for i in range(10):
            tid = f'Blk{i:04d}'
            tasks.append(_task(tid))
            blocker_ids.append(tid)
        tasks.append(_task('Target', blocked_by=blocker_ids))

        g = DependencyGraph(tasks)
        ready = g.get_ready_tasks()
        assert 'Target' not in ready
        assert len(ready) == 10

        # Mark all blockers done
        for t in tasks[:-1]:
            t['status'] = 'done'
        g2 = DependencyGraph(tasks)
        assert g2.get_ready_tasks() == ['Target']
