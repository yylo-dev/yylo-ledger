"""Tests for CLI error message hints.

Why: When users (especially coding agents) mistype commands or use wrong formats,
the CLI should provide helpful suggestions instead of just raw argparse errors.
These tests verify that error hints are shown for common mistakes, exit code 2 is
returned, and the hints include correct usage examples for the specific command.
"""

import io
import json
import os
import sys
import tempfile
import pytest
from copy import deepcopy
from unittest.mock import patch

from kanban.cli import TaskCLI, ExitCode
from kanban.config import Config
from kanban.storage import TaskStorage
from kanban.models import Task


@pytest.fixture
def kanban_env(tmp_path):
    """Set up a kanban environment with config for CLI testing."""
    tasks_dir = tmp_path / ".juno_task" / "tasks"
    tasks_dir.mkdir(parents=True)

    config_data = deepcopy(Config.DEFAULT_CONFIG)
    config_data["storage"]["base_path"] = str(tasks_dir)

    config_path = str(tasks_dir / "config.json")
    with open(config_path, "w") as f:
        json.dump(config_data, f)

    config = Config(config_path=config_path)
    storage = TaskStorage(config)

    # Set JUNO_TASK_ROOT so CLI finds the config
    old_root = os.environ.get("JUNO_TASK_ROOT")
    os.environ["JUNO_TASK_ROOT"] = str(tmp_path)

    yield config_path, tmp_path, storage

    if old_root is not None:
        os.environ["JUNO_TASK_ROOT"] = old_root
    else:
        os.environ.pop("JUNO_TASK_ROOT", None)


class TestDepsErrorHints:
    """Test that deps command errors include helpful usage hints.

    Why: Coding agents commonly try `deps add TASK_ID --blocked-by ID` (missing --id flag)
    which fails with an unhelpful argparse error. The hints guide them to the correct format.
    """

    def test_deps_add_missing_id_flag_shows_hint(self, kanban_env):
        """deps add TASK_ID --blocked-by X (missing --id) should show deps usage hints."""
        cli = TaskCLI()
        stderr = io.StringIO()
        with patch('sys.stderr', stderr):
            result = cli.run(['deps', 'add', 'ABC123', '--blocked-by', 'DEF456'])

        assert result == ExitCode.INVALID_USAGE
        err = stderr.getvalue()
        assert 'deps add --id TASK_ID --blocked-by ID' in err
        assert 'deps remove --id TASK_ID --blocked-by ID' in err

    def test_deps_no_args_shows_hint(self, kanban_env):
        """deps with no arguments should show deps usage hints."""
        cli = TaskCLI()
        stderr = io.StringIO()
        with patch('sys.stderr', stderr):
            result = cli.run(['deps'])

        assert result == ExitCode.INVALID_USAGE
        err = stderr.getvalue()
        assert 'Deps usage:' in err
        assert 'deps add --id TASK_ID --blocked-by ID' in err

    def test_deps_error_exit_code_is_2(self, kanban_env):
        """All deps parse errors should return exit code 2 (INVALID_USAGE)."""
        cli = TaskCLI()
        with patch('sys.stderr', io.StringIO()):
            result = cli.run(['deps', 'add', 'TASKID', '--blocked-by', 'X'])
        assert result == ExitCode.INVALID_USAGE
        assert result == 2

    def test_deps_show_hint_includes_show_format(self, kanban_env):
        """Deps hints should include the show format (deps TASK_ID)."""
        cli = TaskCLI()
        stderr = io.StringIO()
        with patch('sys.stderr', stderr):
            cli.run(['deps'])

        err = stderr.getvalue()
        assert 'deps TASK_ID' in err
        assert 'Show blockers' in err


class TestMarkErrorHints:
    """Test that mark command errors include helpful hints.

    Why: mark requires status, --id, and --response. Missing any shows raw argparse
    errors without examples. The hints show the correct format with an example.
    """

    def test_mark_no_args_shows_hint(self, kanban_env):
        """mark with no args should show mark usage hint with example."""
        cli = TaskCLI()
        stderr = io.StringIO()
        with patch('sys.stderr', stderr):
            result = cli.run(['mark'])

        assert result == ExitCode.INVALID_USAGE
        err = stderr.getvalue()
        assert 'Mark usage:' in err
        assert 'mark done ABC123 --response' in err

    def test_mark_missing_response_shows_hint(self, kanban_env):
        """mark done --id X (missing --response) should show hint."""
        cli = TaskCLI()
        stderr = io.StringIO()
        with patch('sys.stderr', stderr):
            result = cli.run(['mark', 'done', '--id', 'ABC123'])

        assert result == ExitCode.INVALID_USAGE
        err = stderr.getvalue()
        assert 'Mark usage:' in err


class TestCreateErrorHints:
    """Test that create command errors include helpful hints."""

    def test_create_invalid_flag_shows_hint(self, kanban_env):
        """create with unknown flag should show create usage hint."""
        cli = TaskCLI()
        stderr = io.StringIO()
        with patch('sys.stderr', stderr):
            result = cli.run(['create', '--unknown-flag', 'value'])

        assert result == ExitCode.INVALID_USAGE
        err = stderr.getvalue()
        assert 'Create usage:' in err


class TestListErrorHints:
    """Test that list command errors include helpful hints."""

    def test_list_invalid_flag_shows_hint(self, kanban_env):
        """list with unknown flag should show list usage hint."""
        cli = TaskCLI()
        stderr = io.StringIO()
        with patch('sys.stderr', stderr):
            result = cli.run(['list', '--invalid-option'])

        assert result == ExitCode.INVALID_USAGE
        err = stderr.getvalue()
        assert 'List usage:' in err


class TestSearchOutputContracts:
    """Test search output parity with list command ergonomics.

    Why: Users requested list-like behavior from search, specifically command-level
    `--format` support and visible result-count summaries. These tests lock both
    runtime contracts so future parser/output refactors don't silently regress UX.
    """

    def test_search_supports_command_level_table_format(self, kanban_env):
        """search --format table should render table output and summary stats."""
        config_path, tmp_path, storage = kanban_env
        task = storage.create_task(body="Searchable todo", status="todo")

        cli = TaskCLI()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', stderr):
            result = cli.run(['-c', config_path, 'search', '--status', 'todo', '--format', 'table'])

        assert result == ExitCode.SUCCESS
        out = stdout.getvalue()
        err = stderr.getvalue()
        assert 'ID' in out and 'Status' in out
        assert task.id in out
        assert 'SUMMARY:' in err
        assert 'Total tasks: 1' in err

    def test_search_truncates_body_like_list_and_shows_displayed_summary(self, kanban_env):
        """search should truncate long bodies and show list-like displayed summary with --limit."""
        config_path, tmp_path, storage = kanban_env

        storage.write_task(Task(
            id='St1A11',
            body='A' * 1300,
            status='todo',
            created_date='2026-01-01 10:00:00',
            last_modified='2026-01-01 10:00:00',
        ))
        storage.write_task(Task(
            id='St1A12',
            body='Second todo body',
            status='todo',
            created_date='2026-01-02 10:00:00',
            last_modified='2026-01-02 10:00:00',
        ))
        storage.write_task(Task(
            id='St1A13',
            body='Third todo body',
            status='todo',
            created_date='2026-01-03 10:00:00',
            last_modified='2026-01-03 10:00:00',
        ))

        cli = TaskCLI()

        table_stdout = io.StringIO()
        table_stderr = io.StringIO()
        with patch('sys.stdout', table_stdout), patch('sys.stderr', table_stderr):
            table_result = cli.run(['-c', config_path, 'search', '--status', 'todo', '--format', 'table', '--limit', '2'])

        assert table_result == ExitCode.SUCCESS
        assert 'SUMMARY:' in table_stderr.getvalue()
        assert 'Displayed: 2 of 3 total tasks' in table_stderr.getvalue()

        json_stdout = io.StringIO()
        with patch('sys.stdout', json_stdout), patch('sys.stderr', io.StringIO()):
            json_result = cli.run(['-c', config_path, 'search', '--status', 'todo', '--format', 'json', '--sort', 'asc', '--limit', '2'])

        assert json_result == ExitCode.SUCCESS
        json_lines = [line for line in json_stdout.getvalue().strip().splitlines() if line.strip()]
        assert len(json_lines) == 2
        results_payload = json.loads(json_lines[0])
        assert any('[Truncated full size: 1300 characters, use get command to read the full body]' in task['body'] for task in results_payload)

    def test_search_json_format_emits_results_and_summary_documents(self, kanban_env):
        """search --format json should emit results array followed by summary object."""
        config_path, tmp_path, storage = kanban_env

        storage.write_task(Task(
            id='Sj1A11',
            body='Search JSON result 1',
            status='todo',
            created_date='2026-01-01 10:00:00',
            last_modified='2026-01-01 10:00:00',
        ))
        storage.write_task(Task(
            id='Sj1A12',
            body='Search JSON result 2',
            status='todo',
            created_date='2026-01-02 10:00:00',
            last_modified='2026-01-02 10:00:00',
        ))
        storage.write_task(Task(
            id='Sj1A13',
            body='Search JSON result 3',
            status='todo',
            created_date='2026-01-03 10:00:00',
            last_modified='2026-01-03 10:00:00',
        ))

        cli = TaskCLI()
        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run(['-c', config_path, 'search', '--status', 'todo', '--format', 'json', '--limit', '2'])

        assert result == ExitCode.SUCCESS
        lines = [line for line in stdout.getvalue().strip().splitlines() if line.strip()]
        assert len(lines) == 2

        results_payload = json.loads(lines[0])
        summary_payload = json.loads(lines[1])

        assert isinstance(results_payload, list)
        assert len(results_payload) == 2
        assert summary_payload['summary']['total_tasks'] == 3
        assert summary_payload['summary']['displayed_tasks'] == 2

    def test_search_supports_sort_order_asc(self, kanban_env):
        """search --sort asc should return oldest tasks first by last_modified."""
        config_path, tmp_path, storage = kanban_env

        older = Task(
            id='Aa1Bb2',
            body='Older todo task',
            status='todo',
            created_date='2026-01-01 10:00:00',
            last_modified='2026-01-01 10:00:00',
        )
        newer = Task(
            id='Cc3Dd4',
            body='Newer todo task',
            status='todo',
            created_date='2026-01-02 10:00:00',
            last_modified='2026-01-02 10:00:00',
        )
        storage.write_task(older)
        storage.write_task(newer)

        cli = TaskCLI()
        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run(['-c', config_path, 'search', '--status', 'todo', '--sort', 'asc', '--format', 'json', '--limit', '2'])

        assert result == ExitCode.SUCCESS
        lines = [line for line in stdout.getvalue().strip().splitlines() if line.strip()]
        assert len(lines) == 2

        results_payload = json.loads(lines[0])
        assert [task['id'] for task in results_payload] == ['Aa1Bb2', 'Cc3Dd4']


class TestTagsCommandContracts:
    """Test tag-count command output contracts.

    Why: `tags` is an aggregation surface used for planning/triage. We need
    deterministic counts, status filtering parity, and stable format output so
    scripts and operators can rely on one source of truth.
    """

    def test_tags_json_format_aggregates_counts_with_status_filter(self, kanban_env):
        """tags --status todo,in_progress --format json should emit filtered tag counts."""
        config_path, tmp_path, storage = kanban_env

        storage.create_task(body="T1", status="todo", feature_tags=["backend", "urgent"])
        storage.create_task(body="T2", status="in_progress", feature_tags=["backend"])
        storage.create_task(body="T3", status="done", feature_tags=["backend", "qa"])
        storage.create_task(body="T4", status="todo", feature_tags=None)

        cli = TaskCLI()
        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run(['-c', config_path, 'tags', '--status', 'todo,in_progress', '--format', 'json'])

        assert result == ExitCode.SUCCESS
        payload = json.loads(stdout.getvalue().strip())
        assert payload == [
            {'tag': 'backend', 'count': 2},
            {'tag': 'urgent', 'count': 1},
        ]

    def test_tags_table_format_outputs_markdown_table(self, kanban_env):
        """tags --format table should render a markdown table with tag counts."""
        config_path, tmp_path, storage = kanban_env

        storage.create_task(body="T1", status="todo", feature_tags=["backend", "urgent"])
        storage.create_task(body="T2", status="todo", feature_tags=["backend"])

        cli = TaskCLI()
        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run(['-c', config_path, 'tags', '--status', 'todo', '--format', 'table'])

        assert result == ExitCode.SUCCESS
        out = stdout.getvalue()
        assert '| Tag | Count |' in out
        assert '| --- | ---: |' in out
        assert '| backend | 2 |' in out
        assert '| urgent | 1 |' in out


class TestReadyOutputContracts:
    """Test ready output sorting + summary ergonomics.

    Why: Operators prioritize unblocked work by last modification time and need
    list-like summary counters from `ready` to quickly see available work by
    status without running extra commands.
    """

    def test_ready_supports_sort_order_asc(self, kanban_env):
        """ready --sort asc should return oldest ready tasks first by last_modified."""
        config_path, tmp_path, storage = kanban_env

        older = Task(
            id='Ra1Dy2',
            body='Older ready task',
            status='todo',
            created_date='2026-01-01 10:00:00',
            last_modified='2026-01-01 10:00:00',
        )
        newer = Task(
            id='Re3Ad4',
            body='Newer ready task',
            status='todo',
            created_date='2026-01-02 10:00:00',
            last_modified='2026-01-02 10:00:00',
        )
        storage.write_task(older)
        storage.write_task(newer)

        cli = TaskCLI()
        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run(['-c', config_path, 'ready', '--sort', 'asc', '--format', 'json', '--limit', '2'])

        assert result == ExitCode.SUCCESS

        lines = [line for line in stdout.getvalue().strip().splitlines() if line.strip()]
        assert len(lines) == 2

        results_payload = json.loads(lines[0])
        summary_payload = json.loads(lines[1])
        assert [task['id'] for task in results_payload] == ['Ra1Dy2', 'Re3Ad4']
        assert summary_payload['summary']['total_tasks'] == 2
        assert summary_payload['summary']['displayed_tasks'] == 2
        assert summary_payload['summary']['status_counts']['todo'] == 2

    def test_ready_table_format_emits_summary_stats(self, kanban_env):
        """ready --format table should include list-like summary counters on stderr."""
        config_path, tmp_path, storage = kanban_env

        task_backlog = Task(
            id='Rtb1A1',
            body='Backlog ready task',
            status='backlog',
            created_date='2026-01-01 09:00:00',
            last_modified='2026-01-01 09:00:00',
        )
        task_todo = Task(
            id='Rtb1A2',
            body='Todo ready task',
            status='todo',
            created_date='2026-01-01 10:00:00',
            last_modified='2026-01-01 10:00:00',
        )
        task_in_progress = Task(
            id='Rtb1A3',
            body='In progress ready task',
            status='in_progress',
            created_date='2026-01-01 11:00:00',
            last_modified='2026-01-01 11:00:00',
        )
        storage.write_task(task_backlog)
        storage.write_task(task_todo)
        storage.write_task(task_in_progress)

        cli = TaskCLI()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', stderr):
            result = cli.run(['-c', config_path, 'ready', '--format', 'table', '--limit', '2'])

        assert result == ExitCode.SUCCESS
        out = stdout.getvalue()
        err = stderr.getvalue()

        assert 'ID' in out and 'Status' in out
        assert 'SUMMARY:' in err
        assert 'Displayed: 2 of 3 total tasks' in err
        assert 'backlog: 1' in err
        assert 'todo: 1' in err
        assert 'in_progress: 1' in err


class TestStatusOrderContracts:
    """Status-order contracts for list/ready when --status is explicit.

    Why: When users provide a status filter sequence, output should preserve that
    status order while still applying --sort by last_modified inside each status.
    """

    @staticmethod
    def _first_json_document(stdout_value: str):
        lines = [line for line in stdout_value.strip().splitlines() if line.strip()]
        assert lines, "Expected at least one JSON document in stdout"
        return json.loads(lines[0])

    def test_list_preserves_status_filter_order(self, kanban_env):
        """list --status backlog,in_progress should emit backlog tasks before in_progress."""
        config_path, tmp_path, storage = kanban_env

        storage.write_task(Task(
            id='LsA111',
            body='Backlog older',
            status='backlog',
            created_date='2026-01-01 10:00:00',
            last_modified='2026-01-01 10:00:00',
        ))
        storage.write_task(Task(
            id='LsA112',
            body='Backlog newer',
            status='backlog',
            created_date='2026-01-02 10:00:00',
            last_modified='2026-01-02 10:00:00',
        ))
        storage.write_task(Task(
            id='LsA113',
            body='In progress newer',
            status='in_progress',
            created_date='2026-01-03 10:00:00',
            last_modified='2026-01-03 10:00:00',
        ))
        storage.write_task(Task(
            id='LsA114',
            body='In progress older',
            status='in_progress',
            created_date='2026-01-01 09:00:00',
            last_modified='2026-01-01 09:00:00',
        ))

        cli = TaskCLI()
        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run([
                '-c', config_path,
                'list',
                '--status', 'backlog,in_progress',
                '--sort', 'desc',
                '--format', 'json',
                '--limit', '10',
            ])

        assert result == ExitCode.SUCCESS
        payload = self._first_json_document(stdout.getvalue())
        assert [task['id'] for task in payload] == ['LsA112', 'LsA111', 'LsA113', 'LsA114']

    def test_ready_preserves_status_filter_order(self, kanban_env):
        """ready --status backlog,in_progress should emit backlog before in_progress."""
        config_path, tmp_path, storage = kanban_env

        storage.write_task(Task(
            id='RdA111',
            body='Backlog ready older',
            status='backlog',
            created_date='2026-01-01 10:00:00',
            last_modified='2026-01-01 10:00:00',
        ))
        storage.write_task(Task(
            id='RdA112',
            body='Backlog ready newer',
            status='backlog',
            created_date='2026-01-02 10:00:00',
            last_modified='2026-01-02 10:00:00',
        ))
        storage.write_task(Task(
            id='RdA113',
            body='In progress ready newer',
            status='in_progress',
            created_date='2026-01-03 10:00:00',
            last_modified='2026-01-03 10:00:00',
        ))
        storage.write_task(Task(
            id='RdA114',
            body='In progress ready older',
            status='in_progress',
            created_date='2026-01-01 09:00:00',
            last_modified='2026-01-01 09:00:00',
        ))

        cli = TaskCLI()
        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run([
                '-c', config_path,
                'ready',
                '--status', 'backlog,in_progress',
                '--sort', 'desc',
                '--format', 'json',
                '--limit', '10',
            ])

        assert result == ExitCode.SUCCESS
        payload = self._first_json_document(stdout.getvalue())
        assert [task['id'] for task in payload] == ['RdA112', 'RdA111', 'RdA113', 'RdA114']


class TestOffsetPaginationContracts:
    """Offset pagination contracts for list/ready.

    Why: Agents commonly page through kanban results with --limit. --offset must be
    applied after filtering and sorting so every page is stable and no task is
    skipped or duplicated when combined with status-order and ready filters.
    """

    @staticmethod
    def _first_json_document(stdout_value: str):
        lines = [line for line in stdout_value.strip().splitlines() if line.strip()]
        assert lines, "Expected at least one JSON document in stdout"
        return json.loads(lines[0])

    def test_list_offset_applies_after_status_order_and_sort(self, kanban_env):
        """list --offset should skip the final ordered results before applying --limit."""
        config_path, tmp_path, storage = kanban_env

        storage.write_task(Task(
            id='LoF001',
            body='Backlog newest',
            status='backlog',
            created_date='2026-01-03 10:00:00',
            last_modified='2026-01-03 10:00:00',
        ))
        storage.write_task(Task(
            id='LoF002',
            body='Backlog middle',
            status='backlog',
            created_date='2026-01-02 10:00:00',
            last_modified='2026-01-02 10:00:00',
        ))
        storage.write_task(Task(
            id='LoF003',
            body='Todo newest',
            status='todo',
            created_date='2026-01-04 10:00:00',
            last_modified='2026-01-04 10:00:00',
        ))

        cli = TaskCLI()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', stderr):
            result = cli.run([
                '-c', config_path,
                'list',
                '--status', 'backlog,todo',
                '--sort', 'desc',
                '--offset', '1',
                '--limit', '2',
                '--format', 'json',
            ])

        assert result == ExitCode.SUCCESS
        payload = self._first_json_document(stdout.getvalue())
        assert [task['id'] for task in payload] == ['LoF002', 'LoF003']
        summary_payload = json.loads(stdout.getvalue().strip().splitlines()[1])
        assert summary_payload['summary']['total_tasks'] == 3
        assert summary_payload['summary']['displayed_tasks'] == 2

    def test_ready_offset_applies_after_readiness_and_sort(self, kanban_env):
        """ready --offset should page the sorted ready set, not all open tasks."""
        config_path, tmp_path, storage = kanban_env

        blocker = Task(
            id='RoF000',
            body='Unresolved blocker',
            status='todo',
            blocked_by=['FwD999'],
            created_date='2026-01-01 09:00:00',
            last_modified='2026-01-01 09:00:00',
        )
        storage.write_task(blocker)
        storage.write_task(Task(
            id='RoF001',
            body='Ready oldest',
            status='todo',
            feature_tags=['page'],
            created_date='2026-01-01 10:00:00',
            last_modified='2026-01-01 10:00:00',
        ))
        storage.write_task(Task(
            id='RoF002',
            body='Ready middle',
            status='todo',
            feature_tags=['page'],
            created_date='2026-01-02 10:00:00',
            last_modified='2026-01-02 10:00:00',
        ))
        storage.write_task(Task(
            id='RoF003',
            body='Blocked task should not affect ready offset',
            status='todo',
            blocked_by=['RoF000'],
            created_date='2026-01-03 10:00:00',
            last_modified='2026-01-03 10:00:00',
        ))
        storage.write_task(Task(
            id='RoF004',
            body='Ready newest',
            status='todo',
            feature_tags=['page'],
            created_date='2026-01-04 10:00:00',
            last_modified='2026-01-04 10:00:00',
        ))

        cli = TaskCLI()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', stderr):
            result = cli.run([
                '-c', config_path,
                'ready',
                '--sort', 'asc',
                '--tag', 'page',
                '--offset', '1',
                '--limit', '2',
                '--format', 'json',
            ])

        assert result == ExitCode.SUCCESS
        payload = self._first_json_document(stdout.getvalue())
        assert [task['id'] for task in payload] == ['RoF002', 'RoF004']
        summary_payload = json.loads(stdout.getvalue().strip().splitlines()[1])
        assert summary_payload['summary']['total_tasks'] == 3
        assert summary_payload['summary']['displayed_tasks'] == 2

    def test_negative_offset_is_rejected(self, kanban_env):
        """Negative offsets are invalid to keep pagination deterministic."""
        config_path, tmp_path, storage = kanban_env
        storage.write_task(Task(id='NoF001', body='Task', status='todo'))

        cli = TaskCLI()
        stderr = io.StringIO()
        with patch('sys.stdout', io.StringIO()), patch('sys.stderr', stderr):
            result = cli.run(['-c', config_path, 'list', '--offset', '-1'])

        assert result == ExitCode.INVALID_USAGE
        assert '--offset must be greater than or equal to 0' in stderr.getvalue()


class TestSortParityContracts:
    """Sort-order parity contracts across commands.

    Why: `--sort` should mean the same thing across list/search/ready so users and
    agents can switch commands without re-learning ordering semantics.
    """

    @staticmethod
    def _first_json_document(stdout_value: str):
        lines = [line for line in stdout_value.strip().splitlines() if line.strip()]
        assert lines, "Expected at least one JSON document in stdout"
        return json.loads(lines[0])

    def test_sort_tie_breaker_asc_is_consistent_across_list_search_ready(self, kanban_env):
        """For equal timestamps, asc ordering should be deterministic and command-consistent."""
        config_path, tmp_path, storage = kanban_env

        same_timestamp = '2026-01-01 10:00:00'
        first = Task(
            id='Sa1Rt1',
            body='Sort parity task A',
            status='todo',
            created_date=same_timestamp,
            last_modified=same_timestamp,
        )
        second = Task(
            id='Sa1Rt2',
            body='Sort parity task B',
            status='todo',
            created_date=same_timestamp,
            last_modified=same_timestamp,
        )
        storage.write_task(first)
        storage.write_task(second)

        cli = TaskCLI()

        search_stdout = io.StringIO()
        with patch('sys.stdout', search_stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run(['-c', config_path, 'search', '--status', 'todo', '--sort', 'asc', '--format', 'json', '--limit', '2'])
        assert result == ExitCode.SUCCESS
        search_ids = [task['id'] for task in self._first_json_document(search_stdout.getvalue())]

        list_stdout = io.StringIO()
        with patch('sys.stdout', list_stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run(['-c', config_path, 'list', '--status', 'todo', '--sort', 'asc', '--format', 'json', '--limit', '2'])
        assert result == ExitCode.SUCCESS
        list_ids = [task['id'] for task in self._first_json_document(list_stdout.getvalue())]

        ready_stdout = io.StringIO()
        with patch('sys.stdout', ready_stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run(['-c', config_path, 'ready', '--sort', 'asc', '--format', 'json', '--limit', '2'])
        assert result == ExitCode.SUCCESS
        ready_ids = [task['id'] for task in self._first_json_document(ready_stdout.getvalue())]

        assert search_ids == ['Sa1Rt1', 'Sa1Rt2']
        assert list_ids == search_ids
        assert ready_ids == search_ids

    def test_sort_tie_breaker_desc_is_consistent_across_list_search_ready(self, kanban_env):
        """For equal timestamps, desc ordering should also stay command-consistent."""
        config_path, tmp_path, storage = kanban_env

        same_timestamp = '2026-01-01 10:00:00'
        first = Task(
            id='Sd1Rt1',
            body='Sort parity task C',
            status='todo',
            created_date=same_timestamp,
            last_modified=same_timestamp,
        )
        second = Task(
            id='Sd1Rt2',
            body='Sort parity task D',
            status='todo',
            created_date=same_timestamp,
            last_modified=same_timestamp,
        )
        storage.write_task(first)
        storage.write_task(second)

        cli = TaskCLI()

        search_stdout = io.StringIO()
        with patch('sys.stdout', search_stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run(['-c', config_path, 'search', '--status', 'todo', '--sort', 'desc', '--format', 'json', '--limit', '2'])
        assert result == ExitCode.SUCCESS
        search_ids = [task['id'] for task in self._first_json_document(search_stdout.getvalue())]

        list_stdout = io.StringIO()
        with patch('sys.stdout', list_stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run(['-c', config_path, 'list', '--status', 'todo', '--sort', 'desc', '--format', 'json', '--limit', '2'])
        assert result == ExitCode.SUCCESS
        list_ids = [task['id'] for task in self._first_json_document(list_stdout.getvalue())]

        ready_stdout = io.StringIO()
        with patch('sys.stdout', ready_stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run(['-c', config_path, 'ready', '--sort', 'desc', '--format', 'json', '--limit', '2'])
        assert result == ExitCode.SUCCESS
        ready_ids = [task['id'] for task in self._first_json_document(ready_stdout.getvalue())]

        assert search_ids == ['Sd1Rt2', 'Sd1Rt1']
        assert list_ids == search_ids
        assert ready_ids == search_ids


class TestUpdateErrorHints:
    """Test that update command errors include helpful hints."""

    def test_update_no_args_shows_hint(self, kanban_env):
        """update with no args should show error about missing Task ID."""
        cli = TaskCLI()
        stderr = io.StringIO()
        with patch('sys.stderr', stderr):
            result = cli.run(['update'])

        assert result == ExitCode.INVALID_USAGE
        err = stderr.getvalue()
        assert 'Task ID is required' in err


class TestGenericErrorHints:
    """Test that unknown commands show generic help hints."""

    def test_unknown_flag_at_top_level_shows_hint(self, kanban_env):
        """Top-level unknown flags should show general help hint."""
        cli = TaskCLI()
        stderr = io.StringIO()
        with patch('sys.stderr', stderr):
            result = cli.run(['--nonexistent-flag'])

        assert result == ExitCode.INVALID_USAGE
        err = stderr.getvalue()
        assert '--help' in err


class TestDepsAddFormatWorks:
    """Verify that the correct deps add format actually works.

    Why: The deps add --id X --blocked-by Y Z format is the requested feature.
    These tests confirm it works with space-separated and comma-separated IDs.
    """

    def test_deps_add_with_id_flag_and_space_separated_blockers(self, kanban_env):
        """deps add --id TASK --blocked-by A B should work (space-separated)."""
        config_path, tmp_path, storage = kanban_env
        cli = TaskCLI()

        # Create tasks via storage (reliable)
        a = storage.create_task(body="Blocker A", status="todo")
        b = storage.create_task(body="Blocker B", status="todo")
        dep = storage.create_task(body="Dependent task", status="backlog")

        # Add both blockers in one command (space-separated)
        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run(['-c', config_path, 'deps', 'add', '--id', dep.id, '--blocked-by', a.id, b.id])

        assert result == ExitCode.SUCCESS
        # Output is a JSON array (pretty-printed)
        output = json.loads(stdout.getvalue().strip())[0]
        assert a.id in output.get('blocked_by', [])
        assert b.id in output.get('blocked_by', [])

    def test_deps_add_with_id_flag_and_comma_separated_blockers(self, kanban_env):
        """deps add --id TASK --blocked-by A,B should work (comma-separated)."""
        config_path, tmp_path, storage = kanban_env
        cli = TaskCLI()

        a = storage.create_task(body="Blocker A", status="todo")
        b = storage.create_task(body="Blocker B", status="todo")
        dep = storage.create_task(body="Dependent task", status="backlog")

        # Add both blockers in one command (comma-separated)
        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run(['-c', config_path, 'deps', 'add', '--id', dep.id, '--blocked-by', f'{a.id},{b.id}'])

        assert result == ExitCode.SUCCESS
        output = json.loads(stdout.getvalue().strip())[0]
        assert a.id in output.get('blocked_by', [])
        assert b.id in output.get('blocked_by', [])


class TestGetIdFlagFormats:
    """Test that 'get' command supports both positional and --ID flag formats.

    Why: Agents and scripts often use flag-style syntax (get --ID {id}) which is
    consistent with mark/deps commands. Both forms must return the same task data.
    """

    def test_get_positional_id_works(self, kanban_env):
        """get TASK_ID (positional) should return the task."""
        config_path, tmp_path, storage = kanban_env
        task = storage.create_task(body="Test task for get", status="todo")

        cli = TaskCLI()
        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run(['-c', config_path, 'get', task.id])

        assert result == ExitCode.SUCCESS
        output = json.loads(stdout.getvalue().strip())
        assert isinstance(output, list)
        assert output[0]['id'] == task.id

    def test_get_with_id_flag_lowercase_works(self, kanban_env):
        """get --id TASK_ID (--id flag, lowercase) should return the same task."""
        config_path, tmp_path, storage = kanban_env
        task = storage.create_task(body="Test task for get --id", status="backlog")

        cli = TaskCLI()
        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run(['-c', config_path, 'get', '--id', task.id])

        assert result == ExitCode.SUCCESS
        output = json.loads(stdout.getvalue().strip())
        assert isinstance(output, list)
        assert output[0]['id'] == task.id

    def test_get_with_ID_flag_uppercase_works(self, kanban_env):
        """get --ID TASK_ID (--ID flag, uppercase after normalization) should return the task."""
        config_path, tmp_path, storage = kanban_env
        task = storage.create_task(body="Test task for get --ID", status="in_progress")

        cli = TaskCLI()
        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            # --ID gets normalized to --id by _normalize_arguments
            result = cli.run(['-c', config_path, 'get', '--ID', task.id])

        assert result == ExitCode.SUCCESS
        output = json.loads(stdout.getvalue().strip())
        assert isinstance(output, list)
        assert output[0]['id'] == task.id

    def test_get_positional_and_flag_return_same_task(self, kanban_env):
        """Both 'get TASK_ID' and 'get --ID TASK_ID' return identical task data."""
        config_path, tmp_path, storage = kanban_env
        task = storage.create_task(body="Equivalence test", status="done")

        cli = TaskCLI()

        stdout1 = io.StringIO()
        with patch('sys.stdout', stdout1), patch('sys.stderr', io.StringIO()):
            cli.run(['-c', config_path, 'get', task.id])

        stdout2 = io.StringIO()
        with patch('sys.stdout', stdout2), patch('sys.stderr', io.StringIO()):
            cli.run(['-c', config_path, 'get', '--ID', task.id])

        result1 = json.loads(stdout1.getvalue().strip())
        result2 = json.loads(stdout2.getvalue().strip())
        # Both should return the same task id
        assert result1[0]['id'] == result2[0]['id'] == task.id

    def test_get_multiple_positional_ids_returns_requested_order(self, kanban_env):
        """get ID1 ID2 ID3 should return all requested tasks in the same order."""
        config_path, tmp_path, storage = kanban_env
        task_a = storage.create_task(body="Task A", status="todo")
        task_b = storage.create_task(body="Task B", status="backlog")
        task_c = storage.create_task(body="Task C", status="in_progress")

        cli = TaskCLI()
        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run(['-c', config_path, '-f', 'json', 'get', task_b.id, task_a.id, task_c.id])

        assert result == ExitCode.SUCCESS
        output = json.loads(stdout.getvalue().strip())
        assert [task['id'] for task in output] == [task_b.id, task_a.id, task_c.id]

    def test_show_multiple_positional_ids_returns_valid_json_array(self, kanban_env):
        """show ID1 ID2 (alias for get) should emit valid JSON array when -f json is requested."""
        config_path, tmp_path, storage = kanban_env
        task_1 = storage.create_task(body="Show Task 1", status="todo")
        task_2 = storage.create_task(body="Show Task 2", status="todo")

        cli = TaskCLI()
        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run(['-c', config_path, '-f', 'json', 'show', task_1.id, task_2.id])

        assert result == ExitCode.SUCCESS
        parsed = json.loads(stdout.getvalue().strip())
        assert isinstance(parsed, list)
        assert [task['id'] for task in parsed] == [task_1.id, task_2.id]

    def test_get_includes_full_related_task_details_by_default(self, kanban_env):
        """get TASK_ID should keep existing full related-task expansion unless compact is requested."""
        config_path, tmp_path, storage = kanban_env
        related = storage.create_task(body="Related task body should be present", status="todo")
        parent = storage.create_task(body="Parent task", status="backlog", related_tasks=[related.id])

        cli = TaskCLI()
        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run(['-c', config_path, '-f', 'json', 'get', parent.id])

        assert result == ExitCode.SUCCESS
        output = json.loads(stdout.getvalue().strip())
        assert output[0]['related_tasks'] == [related.id]
        assert output[0]['_related_tasks_details'][0]['id'] == related.id
        assert output[0]['_related_tasks_details'][0]['body'] == "Related task body should be present"

    def test_get_compact_related_task_details_are_id_only(self, kanban_env):
        """get --compact should avoid embedding full related task bodies."""
        config_path, tmp_path, storage = kanban_env
        related = storage.create_task(body="Verbose related task body should be omitted", status="todo")
        parent = storage.create_task(body="Parent task", status="backlog", related_tasks=[related.id])

        cli = TaskCLI()
        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run(['-c', config_path, '-f', 'json', 'get', parent.id, '--compact'])

        assert result == ExitCode.SUCCESS
        output = json.loads(stdout.getvalue().strip())
        assert output[0]['related_tasks'] == [related.id]
        assert output[0]['_related_tasks_details'] == [{'id': related.id}]
        assert 'Verbose related task body should be omitted' not in stdout.getvalue()

    def test_show_compact_related_task_details_are_id_only(self, kanban_env):
        """show --compact should match get --compact because show is a get alias."""
        config_path, tmp_path, storage = kanban_env
        related = storage.create_task(body="Show alias related body should be omitted", status="todo")
        parent = storage.create_task(body="Parent task", status="backlog", related_tasks=[related.id])

        cli = TaskCLI()
        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run(['-c', config_path, '-f', 'json', 'show', parent.id, '--compact'])

        assert result == ExitCode.SUCCESS
        output = json.loads(stdout.getvalue().strip())
        assert output[0]['_related_tasks_details'] == [{'id': related.id}]

    def test_get_multiple_ids_with_missing_task_returns_error(self, kanban_env):
        """get ID1 MISSING should fail with an explicit not-found error."""
        config_path, tmp_path, storage = kanban_env
        task = storage.create_task(body="Existing task", status="todo")

        cli = TaskCLI()
        stderr = io.StringIO()
        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', stderr):
            result = cli.run(['-c', config_path, '-f', 'json', 'get', task.id, 'ZZZZZZ'])

        assert result == ExitCode.GENERAL_ERROR
        assert 'Task not found: ZZZZZZ' in stderr.getvalue()
        assert stdout.getvalue().strip() == ''

    def test_get_no_id_returns_error(self, kanban_env):
        """get with no ID (no positional, no flag) should return INVALID_USAGE."""
        config_path, tmp_path, storage = kanban_env

        cli = TaskCLI()
        stderr = io.StringIO()
        with patch('sys.stdout', io.StringIO()), patch('sys.stderr', stderr):
            result = cli.run(['-c', config_path, 'get'])

        assert result == ExitCode.INVALID_USAGE
        assert 'Task ID is required' in stderr.getvalue()

    def test_get_nonexistent_id_with_flag_returns_error(self, kanban_env):
        """get --ID NONEXISTENT should return GENERAL_ERROR."""
        config_path, tmp_path, storage = kanban_env

        cli = TaskCLI()
        stderr = io.StringIO()
        with patch('sys.stdout', io.StringIO()), patch('sys.stderr', stderr):
            result = cli.run(['-c', config_path, 'get', '--ID', 'ZZZZZZ'])

        assert result == ExitCode.GENERAL_ERROR
        assert 'Task not found' in stderr.getvalue()

    def test_show_with_id_flag_works(self, kanban_env):
        """show --ID TASK_ID (show is alias for get) should also support the flag."""
        config_path, tmp_path, storage = kanban_env
        task = storage.create_task(body="Test task for show --ID", status="todo")

        cli = TaskCLI()
        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run(['-c', config_path, 'show', '--ID', task.id])

        assert result == ExitCode.SUCCESS
        output = json.loads(stdout.getvalue().strip())
        assert output[0]['id'] == task.id


class TestDepsFlagOnlySyntax:
    """Test that deps command supports flag-only syntax (no positional action_or_id).

    Why: Coding agents commonly call `deps --ID TASK_ID --blocked-by BLOCKER_ID`
    without the "add" positional. This should default to "add" behavior rather
    than failing with an argparse error (V6dmDO).
    """

    def test_deps_flag_only_add_defaults_to_add(self, kanban_env):
        """deps --ID TASK --blocked-by BLOCKER should work as shorthand for 'deps add'."""
        config_path, tmp_path, storage = kanban_env
        cli = TaskCLI()

        blocker = storage.create_task(body="Blocker task", status="todo")
        dep = storage.create_task(body="Dependent task", status="backlog")

        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run(['-c', config_path, 'deps', '--id', dep.id, '--blocked-by', blocker.id])

        assert result == ExitCode.SUCCESS
        output = json.loads(stdout.getvalue().strip())[0]
        assert blocker.id in output.get('blocked_by', [])

    def test_deps_flag_only_with_uppercase_ID(self, kanban_env):
        """deps --ID TASK --blocked-by BLOCKER (uppercase --ID) should work."""
        config_path, tmp_path, storage = kanban_env
        cli = TaskCLI()

        blocker = storage.create_task(body="Blocker", status="todo")
        dep = storage.create_task(body="Dependent", status="backlog")

        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run(['-c', config_path, 'deps', '--ID', dep.id, '--blocked-by', blocker.id])

        assert result == ExitCode.SUCCESS
        output = json.loads(stdout.getvalue().strip())[0]
        assert blocker.id in output.get('blocked_by', [])

    def test_deps_flag_only_multiple_blockers(self, kanban_env):
        """deps --ID TASK --blocked-by A B should add multiple blockers."""
        config_path, tmp_path, storage = kanban_env
        cli = TaskCLI()

        a = storage.create_task(body="Blocker A", status="todo")
        b = storage.create_task(body="Blocker B", status="todo")
        dep = storage.create_task(body="Dependent", status="backlog")

        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run(['-c', config_path, 'deps', '--id', dep.id, '--blocked-by', a.id, b.id])

        assert result == ExitCode.SUCCESS
        output = json.loads(stdout.getvalue().strip())[0]
        assert a.id in output.get('blocked_by', [])
        assert b.id in output.get('blocked_by', [])

    def test_deps_flag_only_comma_separated_blockers(self, kanban_env):
        """deps --ID TASK --blocked-by A,B should add comma-separated blockers."""
        config_path, tmp_path, storage = kanban_env
        cli = TaskCLI()

        a = storage.create_task(body="Blocker A", status="todo")
        b = storage.create_task(body="Blocker B", status="todo")
        dep = storage.create_task(body="Dependent", status="backlog")

        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run(['-c', config_path, 'deps', '--id', dep.id, '--blocked-by', f'{a.id},{b.id}'])

        assert result == ExitCode.SUCCESS
        output = json.loads(stdout.getvalue().strip())[0]
        assert a.id in output.get('blocked_by', [])
        assert b.id in output.get('blocked_by', [])

    def test_deps_flag_only_id_without_blocked_by_shows_info(self, kanban_env):
        """deps --ID TASK (no --blocked-by) should show dependency info."""
        config_path, tmp_path, storage = kanban_env
        cli = TaskCLI()

        task = storage.create_task(body="Some task", status="todo")

        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run(['-c', config_path, 'deps', '--id', task.id])

        assert result == ExitCode.SUCCESS
        output = json.loads(stdout.getvalue().strip())[0]
        assert output['task_id'] == task.id
        assert 'is_blocked' in output

    def test_deps_no_args_at_all_shows_error(self, kanban_env):
        """deps with no positional and no flags should show usage error."""
        config_path, tmp_path, storage = kanban_env
        cli = TaskCLI()

        stderr = io.StringIO()
        with patch('sys.stdout', io.StringIO()), patch('sys.stderr', stderr):
            result = cli.run(['-c', config_path, 'deps'])

        assert result == ExitCode.INVALID_USAGE

    def test_deps_error_hint_includes_shorthand(self, kanban_env):
        """Deps error hints should include the shorthand format."""
        cli = TaskCLI()
        stderr = io.StringIO()
        with patch('sys.stderr', stderr):
            cli.run(['deps', '--unknown-flag'])

        err = stderr.getvalue()
        assert 'Shorthand for add' in err

    def test_deps_positional_add_still_works(self, kanban_env):
        """Original syntax deps add --id TASK --blocked-by BLOCKER should still work."""
        config_path, tmp_path, storage = kanban_env
        cli = TaskCLI()

        blocker = storage.create_task(body="Blocker", status="todo")
        dep = storage.create_task(body="Dependent", status="backlog")

        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run(['-c', config_path, 'deps', 'add', '--id', dep.id, '--blocked-by', blocker.id])

        assert result == ExitCode.SUCCESS
        output = json.loads(stdout.getvalue().strip())[0]
        assert blocker.id in output.get('blocked_by', [])

    def test_deps_positional_show_still_works(self, kanban_env):
        """Original syntax deps TASK_ID should still work for show."""
        config_path, tmp_path, storage = kanban_env
        cli = TaskCLI()

        task = storage.create_task(body="Task to show deps", status="todo")

        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run(['-c', config_path, 'deps', task.id])

        assert result == ExitCode.SUCCESS
        output = json.loads(stdout.getvalue().strip())[0]
        assert output['task_id'] == task.id

    def test_deps_positional_remove_still_works(self, kanban_env):
        """Original syntax deps remove --id TASK --blocked-by BLOCKER should still work."""
        config_path, tmp_path, storage = kanban_env
        cli = TaskCLI()

        blocker = storage.create_task(body="Blocker", status="todo")
        dep = storage.create_task(body="Dependent", status="backlog", blocked_by=[blocker.id])

        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run(['-c', config_path, 'deps', 'remove', '--id', dep.id, '--blocked-by', blocker.id])

        assert result == ExitCode.SUCCESS
        output = json.loads(stdout.getvalue().strip())[0]
        assert blocker.id not in (output.get('blocked_by') or [])


class TestCreateTitleFlag:
    """Test that create command supports --title flag.

    Why: Coding agents commonly call `create --title "Title" --body "Body"` which
    fails because --title is not recognized. The --title flag prepends to body
    as "title:{title}\\n\\n{body}" (hy7DZR).
    """

    def test_create_with_title_and_body_merges(self, kanban_env):
        """create --title 'My Title' --body 'My body' should merge into body."""
        config_path, tmp_path, storage = kanban_env
        cli = TaskCLI()

        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run(['-c', config_path, 'create', '--title', 'My Title', '--body', 'My body text'])

        assert result == ExitCode.SUCCESS
        output = json.loads(stdout.getvalue().strip())[0]
        assert output['body'] == 'title:My Title\n\nMy body text'

    def test_create_with_title_only(self, kanban_env):
        """create --title 'My Title' (no body) should use title as body."""
        config_path, tmp_path, storage = kanban_env
        cli = TaskCLI()

        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run(['-c', config_path, 'create', '--title', 'Stand-alone Title'])

        assert result == ExitCode.SUCCESS
        output = json.loads(stdout.getvalue().strip())[0]
        assert output['body'] == 'title:Stand-alone Title'

    def test_create_with_title_and_positional_body(self, kanban_env):
        """create --title 'Title' 'positional body' should merge title into positional body."""
        config_path, tmp_path, storage = kanban_env
        cli = TaskCLI()

        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run(['-c', config_path, 'create', '--title', 'My Title', 'Positional body'])

        assert result == ExitCode.SUCCESS
        output = json.loads(stdout.getvalue().strip())[0]
        assert output['body'] == 'title:My Title\n\nPositional body'

    def test_create_with_title_and_tags(self, kanban_env):
        """create --title 'Title' --body 'Body' --tags tag1 should work with other flags."""
        config_path, tmp_path, storage = kanban_env
        cli = TaskCLI()

        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run(['-c', config_path, 'create', '--title', 'Tagged Task', '--body', 'Body', '--tags', 'backend', 'urgent'])

        assert result == ExitCode.SUCCESS
        output = json.loads(stdout.getvalue().strip())[0]
        assert output['body'] == 'title:Tagged Task\n\nBody'
        assert 'backend' in output.get('feature_tags', [])
        assert 'urgent' in output.get('feature_tags', [])

    def test_create_without_title_still_works(self, kanban_env):
        """create 'body' (no --title) should work as before."""
        config_path, tmp_path, storage = kanban_env
        cli = TaskCLI()

        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run(['-c', config_path, 'create', 'Simple body'])

        assert result == ExitCode.SUCCESS
        output = json.loads(stdout.getvalue().strip())[0]
        assert output['body'] == 'Simple body'

    def test_create_with_title_and_blocked_by(self, kanban_env):
        """create --title 'Title' --body 'Body' --blocked-by ID should work."""
        config_path, tmp_path, storage = kanban_env
        cli = TaskCLI()

        blocker = storage.create_task(body="Blocker", status="todo")

        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run(['-c', config_path, 'create', '--title', 'Blocked Task', '--body', 'Body text', '--blocked-by', blocker.id])

        assert result == ExitCode.SUCCESS
        output = json.loads(stdout.getvalue().strip())[0]
        assert output['body'] == 'title:Blocked Task\n\nBody text'
        assert blocker.id in output.get('blocked_by', [])

    def test_create_parses_related_tasks_from_hash_markup(self, kanban_env):
        """Body references using ## markers should populate related_tasks."""
        config_path, tmp_path, storage = kanban_env
        cli = TaskCLI()

        related = storage.create_task(body="Reference task", status="todo")

        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run([
                '-c',
                config_path,
                'create',
                '--body',
                f"Follow-up ## {related.id}",
            ])

        assert result == ExitCode.SUCCESS
        output = json.loads(stdout.getvalue().strip())[0]
        assert related.id in (output.get('related_tasks') or [])

    def test_create_keeps_forward_related_reference_from_hash_markup(self, kanban_env):
        """Valid related IDs should be retained even when they do not exist yet."""
        config_path, tmp_path, storage = kanban_env
        cli = TaskCLI()
        missing_related_id = "yee3R5"

        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', stderr):
            result = cli.run([
                '-c',
                config_path,
                'create',
                '--body',
                f"Follow-up ## {missing_related_id}",
            ])

        assert result == ExitCode.SUCCESS
        output = json.loads(stdout.getvalue().strip())[0]
        assert missing_related_id in (output.get('related_tasks') or [])
        assert f"{missing_related_id} wasn't found on the kanban (stored as forward reference)." in stderr.getvalue()

    def test_create_ignores_invalid_related_task_id_format(self, kanban_env):
        """Malformed related IDs should not be persisted into related_tasks."""
        config_path, tmp_path, storage = kanban_env
        cli = TaskCLI()

        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', stderr):
            result = cli.run([
                '-c',
                config_path,
                'create',
                '--body',
                'Follow-up task',
                '--related-tasks',
                'BAD',
            ])

        assert result == ExitCode.SUCCESS
        output = json.loads(stdout.getvalue().strip())[0]
        assert output.get('related_tasks') is None
        assert 'BAD has invalid task ID format and was ignored.' in stderr.getvalue()


class TestCreateRejectDuplicates:
    """Test optional duplicate-body rejection during create.

    Why: automation loops can produce repeated bug reports. This flag gives callers
    deterministic protection against duplicate open issues while preserving default
    behavior for workflows that intentionally allow repeated tasks.
    """

    @pytest.mark.parametrize('reject_flag', ['--reject-duplicates', '--no-duplicate', '--discard-duplicates'])
    def test_create_reject_duplicates_blocks_exact_open_body_match(self, kanban_env, reject_flag):
        config_path, tmp_path, storage = kanban_env
        cli = TaskCLI()

        existing = storage.create_task(body="Duplicate me", status="todo")

        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', stderr):
            result = cli.run(['-c', config_path, 'create', reject_flag, 'Duplicate me'])

        assert result == ExitCode.VALIDATION_ERROR
        assert stderr.getvalue().strip() == ''
        out = stdout.getvalue()
        assert existing.id in out
        assert 'Duplicate task body matches open task' in out

    @pytest.mark.parametrize('reject_flag', ['--reject-duplicates', '--no-duplicate', '--discard-duplicates'])
    def test_create_reject_duplicates_ignores_closed_status_matches(self, kanban_env, reject_flag):
        config_path, tmp_path, storage = kanban_env
        cli = TaskCLI()

        storage.create_task(body="Reusable after done", status="done")

        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run(['-c', config_path, 'create', reject_flag, 'Reusable after done'])

        assert result == ExitCode.SUCCESS
        output = json.loads(stdout.getvalue().strip())[0]
        assert output['body'] == 'Reusable after done'

    def test_create_allows_duplicates_without_flag(self, kanban_env):
        config_path, tmp_path, storage = kanban_env
        cli = TaskCLI()

        storage.create_task(body="Allowed duplicate", status="backlog")

        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run(['-c', config_path, 'create', 'Allowed duplicate'])

        assert result == ExitCode.SUCCESS
        output = json.loads(stdout.getvalue().strip())[0]
        assert output['body'] == 'Allowed duplicate'

    def test_help_text_mentions_duplicate_rejection_aliases(self, kanban_env):
        config_path, _, _ = kanban_env
        cli = TaskCLI()

        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run(['-c', config_path])

        assert result == ExitCode.SUCCESS
        help_text = stdout.getvalue()
        assert '--reject-duplicates|--no-duplicate|--discard-duplicates' in help_text


class TestCreateTrailingBodyAfterFlags:
    """Ensure create supports trailing quoted positional body after list-like flags.

    Why: Agents frequently compose commands like:
    create --status ... --tags ... --related-tasks ... "long body"
    With argparse nargs='*', the trailing body was swallowed by --related-tasks,
    causing a false "Task body is required" failure.
    """

    def test_create_accepts_trailing_body_after_related_tasks_and_tags(self, kanban_env):
        """create with body last should work without requiring --body flag."""
        config_path, tmp_path, storage = kanban_env
        cli = TaskCLI()

        related = storage.create_task(body="Related task", status="todo")
        body = (
            "[task_id]"
            + related.id
            + "[/task_id] Add contract-first tests for listing write queue architecture. "
            + "Cover schema versioning, dedupe keys, and parity assertions."
        )

        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', stderr):
            result = cli.run([
                '-c',
                config_path,
                'create',
                '--status',
                'todo',
                '--tags',
                'IF_Backend',
                'listing-db',
                'queue',
                'tdd',
                '--related-tasks',
                related.id,
                body,
            ])

        assert result == ExitCode.SUCCESS
        output = json.loads(stdout.getvalue().strip())[0]
        assert output['body'] == body
        assert output['status'] == 'todo'
        assert set(output.get('feature_tags', [])) == {'IF_Backend', 'listing-db', 'queue', 'tdd'}
        assert related.id in (output.get('related_tasks') or [])

    def test_create_accepts_trailing_body_after_blocked_by(self, kanban_env):
        """Trailing quoted body should also recover when --blocked-by consumes nargs='*'."""
        config_path, tmp_path, storage = kanban_env
        cli = TaskCLI()

        blocker = storage.create_task(body="Blocker", status="todo")
        body = "Implement queue worker cancellation and lock behavior parity checks"

        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = cli.run([
                '-c',
                config_path,
                'create',
                '--status',
                'todo',
                '--blocked-by',
                blocker.id,
                body,
            ])

        assert result == ExitCode.SUCCESS
        output = json.loads(stdout.getvalue().strip())[0]
        assert output['body'] == body
        assert blocker.id in (output.get('blocked_by') or [])

    def test_create_without_body_still_errors_for_id_only_lists(self, kanban_env):
        """Recovery should not misclassify pure ID lists as task body."""
        config_path, tmp_path, storage = kanban_env
        cli = TaskCLI()

        blocker = storage.create_task(body="Blocker", status="todo")

        stderr = io.StringIO()
        with patch('sys.stdout', io.StringIO()), patch('sys.stderr', stderr):
            result = cli.run([
                '-c',
                config_path,
                'create',
                '--blocked-by',
                blocker.id,
            ])

        assert result == ExitCode.INVALID_USAGE
        assert 'Task body is required' in stderr.getvalue()


class TestFileInputContracts:
    """Lock file-backed body/response input for shell-sensitive markdown.

    Why: agents create kanban context with code fences, ASCII diagrams, quotes,
    `$VARIABLES`, and inline backticks. Reading these fields from files/stdin avoids
    shell interpolation before juno-kanban receives the content, so task context
    remains a reliable source of truth for later agents.
    """

    SENSITIVE_MARKDOWN = (
        '''Leading space preserved
```text
agent builds markdown task body
        |
        v
./.juno_task/scripts/kanban.sh create --body "markdown with ``` fences, $VARIABLES, `cmds`"
```

ASCII:
+----------+      +-------------+
|  agent   | ---> | juno-kanban |
+----------+      +-------------+

Quotes: "double", 'single'
Inline command: `echo "$VARIABLES"`
Trailing space follows:'''
        + '   \n'
    )

    def test_create_body_file_preserves_shell_sensitive_markdown_exactly(self, kanban_env):
        """create --body-file should store exact rich markdown without shell quoting risk."""
        config_path, tmp_path, storage = kanban_env
        body_path = tmp_path / "body.md"
        body_path.write_text(self.SENSITIVE_MARKDOWN, encoding="utf-8")

        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = TaskCLI().run(['-c', config_path, 'create', '--body-file', str(body_path), '--status', 'todo'])

        assert result == ExitCode.SUCCESS
        output = json.loads(stdout.getvalue().strip())[0]
        assert output['body'] == self.SENSITIVE_MARKDOWN
        assert output['status'] == 'todo'

    def test_create_body_file_stdin_preserves_content_exactly(self, kanban_env):
        """create --body-file - should read stdin in the command handler without strip()."""
        config_path, tmp_path, storage = kanban_env
        stdin = io.StringIO(self.SENSITIVE_MARKDOWN)

        stdout = io.StringIO()
        with patch('sys.stdin', stdin), patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = TaskCLI().run(['-c', config_path, 'create', '--body-file', '-'])

        assert result == ExitCode.SUCCESS
        output = json.loads(stdout.getvalue().strip())[0]
        assert output['body'] == self.SENSITIVE_MARKDOWN

    @pytest.mark.parametrize('args', [
        ['create'],
        [],
    ])
    def test_implicit_stdin_create_allows_multiline_body(self, kanban_env, args, monkeypatch):
        """Heredoc/pipe bodies are safe input, not shell-sensitive inline argv."""
        config_path, tmp_path, storage = kanban_env
        body = 'line one\nline two\n'
        monkeypatch.chdir(tmp_path)

        stdout = io.StringIO()
        with tempfile.TemporaryFile(mode='w+', encoding='utf-8') as stdin:
            stdin.write(body)
            stdin.seek(0)
            with patch('sys.stdin', stdin), patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
                result = TaskCLI().run(args)

        assert result == ExitCode.SUCCESS
        output = json.loads(stdout.getvalue().strip())[0]
        assert output['body'] == body.strip()

    @pytest.mark.parametrize('body, reason', [
        ('run $(danger)', 'command substitution'),
        ('run `danger`', 'backtick'),
        ('line one\nline two', 'multiline inline body'),
        ('cat <<EOF', 'here-document'),
    ])
    def test_create_rejects_shell_sensitive_inline_body_literals(self, kanban_env, body, reason):
        """Inline create bodies with shell-sensitive markdown should fail toward file/stdin input.

        Why: a CLI can only reject literals that survive shell parsing; unquoted
        substitutions execute before Python receives argv. The regression still
        matters because quoted/literal dangerous markdown should not be accepted
        through the fragile inline path when --body-file can preserve exact bytes.
        """
        config_path, tmp_path, storage = kanban_env

        stderr = io.StringIO()
        with patch('sys.stdout', io.StringIO()), patch('sys.stderr', stderr):
            result = TaskCLI().run(['-c', config_path, 'create', '--body', body])

        assert result == ExitCode.INVALID_USAGE
        err = stderr.getvalue()
        assert 'refusing shell-sensitive inline task body' in err
        assert reason in err
        assert '--body-file PATH' in err
        assert '--body-file -' in err

    def test_create_allows_plain_inline_body(self, kanban_env):
        """Plain single-line inline create remains supported for low-risk bodies."""
        config_path, tmp_path, storage = kanban_env

        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = TaskCLI().run(['-c', config_path, 'create', '--body', 'plain task body'])

        assert result == ExitCode.SUCCESS
        output = json.loads(stdout.getvalue().strip())[0]
        assert output['body'] == 'plain task body'

    def test_create_help_guides_rich_markdown_to_body_file(self):
        """Help should explain why --body-file is the safe path for rich markdown."""
        stdout = io.StringIO()
        with patch('sys.stdout', stdout):
            result = TaskCLI().run([])

        assert result == ExitCode.SUCCESS
        out = stdout.getvalue()
        assert 'create --body-file path.md' in out
        assert 'preserves shell-sensitive markdown' in out

    def test_create_title_merges_with_body_file_like_inline_body(self, kanban_env):
        """--title should keep existing prepend semantics when the body comes from a file."""
        config_path, tmp_path, storage = kanban_env
        body_path = tmp_path / "body.md"
        body_path.write_text("details with `inline`", encoding="utf-8")

        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = TaskCLI().run(['-c', config_path, 'create', '--title', 'File title', '--body-file', str(body_path)])

        assert result == ExitCode.SUCCESS
        output = json.loads(stdout.getvalue().strip())[0]
        assert output['body'] == 'title:File title\n\ndetails with `inline`'

    def test_update_body_and_response_file_preserve_content_exactly(self, kanban_env):
        """update should support file-backed body and response through the same update path."""
        config_path, tmp_path, storage = kanban_env
        task = storage.create_task(body="old", status="todo")
        body_path = tmp_path / "updated-body.md"
        response_path = tmp_path / "response.md"
        body_path.write_text(self.SENSITIVE_MARKDOWN, encoding="utf-8")
        response = "Response with `$VALUE`, `cmd`, quotes, and\nmultiple lines.\n"
        response_path.write_text(response, encoding="utf-8")

        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = TaskCLI().run([
                '-c', config_path, 'update', task.id,
                '--body-file', str(body_path),
                '--response-file', str(response_path),
            ])

        assert result == ExitCode.SUCCESS
        output = json.loads(stdout.getvalue().strip())[0]
        assert output['body'] == self.SENSITIVE_MARKDOWN
        assert output['agent_response'] == response

    def test_mark_response_file_preserves_content_exactly(self, kanban_env):
        """mark --response-file should update response/status while preserving markdown."""
        config_path, tmp_path, storage = kanban_env
        task = storage.create_task(body="task", status="todo")
        response_path = tmp_path / "done.md"
        response_path.write_text(self.SENSITIVE_MARKDOWN, encoding="utf-8")

        with patch('sys.stdout', io.StringIO()), patch('sys.stderr', io.StringIO()):
            result = TaskCLI().run(['-c', config_path, 'mark', 'done', '--id', task.id, '--response-file', str(response_path)])

        assert result == ExitCode.SUCCESS
        updated = storage.find_task(task.id)
        assert updated['status'] == 'done'
        assert updated['agent_response'] == self.SENSITIVE_MARKDOWN

    @pytest.mark.parametrize('args, expected', [
        (['create', 'inline body', '--body-file', 'body.md'], '--body-file cannot be used together'),
        (['update', 'TASKID', '--body', 'inline', '--body-file', 'body.md'], '--body-file cannot be used together'),
        (['update', 'TASKID', '--response', 'inline', '--response-file', 'response.md'], '--response-file cannot be used together'),
        (['mark', 'done', '--id', 'TASKID', '--response', 'inline', '--response-file', 'response.md'], '--response-file cannot be used together'),
    ])
    def test_file_input_rejects_ambiguous_inline_and_file_values(self, kanban_env, args, expected):
        """Ambiguous inline+file input should fail clearly instead of choosing precedence."""
        config_path, tmp_path, storage = kanban_env
        task = storage.create_task(body="task", status="todo")
        body_path = tmp_path / "body.md"
        response_path = tmp_path / "response.md"
        body_path.write_text("body", encoding="utf-8")
        response_path.write_text("response", encoding="utf-8")
        resolved_args = [
            str(body_path) if arg == 'body.md' else str(response_path) if arg == 'response.md' else task.id if arg == 'TASKID' else arg
            for arg in args
        ]

        stderr = io.StringIO()
        with patch('sys.stdout', io.StringIO()), patch('sys.stderr', stderr):
            result = TaskCLI().run(['-c', config_path] + resolved_args)

        assert result == ExitCode.INVALID_USAGE
        assert expected in stderr.getvalue()

    def test_missing_body_file_returns_clear_io_error(self, kanban_env):
        """Missing file inputs should return IO_ERROR with the unreadable path."""
        config_path, tmp_path, storage = kanban_env
        missing_path = tmp_path / "missing.md"

        stderr = io.StringIO()
        with patch('sys.stdout', io.StringIO()), patch('sys.stderr', stderr):
            result = TaskCLI().run(['-c', config_path, 'create', '--body-file', str(missing_path)])

        assert result == ExitCode.IO_ERROR
        assert 'file not found' in stderr.getvalue()
        assert str(missing_path) in stderr.getvalue()


class TestOrderDependencyClassification:
    """The user-visible order command distinguishes satisfied and broken edges."""

    def test_order_omits_resolved_blocker_and_orders_open_dependents(self, kanban_env):
        config_path, _, storage = kanban_env
        storage.create_task(id='Aaaa01', body='resolved', status='done')
        storage.create_task(
            id='Bbbb02', body='first open', status='todo', blocked_by=['Aaaa01']
        )
        storage.create_task(
            id='Cccc03', body='second open', status='todo', blocked_by=['Bbbb02']
        )

        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()):
            result = TaskCLI().run(['-c', config_path, '--raw', 'order', '-f', 'json'])

        assert result == ExitCode.SUCCESS
        assert [task['id'] for task in json.loads(stdout.getvalue())] == ['Bbbb02', 'Cccc03']

    def test_order_reports_missing_blocker_without_calling_it_a_cycle(self, kanban_env):
        config_path, _, storage = kanban_env
        storage.create_task(
            id='Aaaa01', body='broken dependency', status='todo', blocked_by=['Xxxx99']
        )

        stderr = io.StringIO()
        with patch('sys.stdout', io.StringIO()), patch('sys.stderr', stderr):
            result = TaskCLI().run(['-c', config_path, 'order'])

        assert result == ExitCode.GENERAL_ERROR
        assert 'Missing blockers' in stderr.getvalue()
        assert 'cycle' not in stderr.getvalue().lower()
