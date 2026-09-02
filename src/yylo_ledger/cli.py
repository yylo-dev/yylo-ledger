#!/usr/bin/env python3
"""
YYLO Ledger CLI - Complete Implementation
Git-native task manager optimized for shell and LLM usage.
"""

import sys
import os
import re
import argparse
import base64
import hashlib
import json
import time
from typing import List, Optional, Dict, Any, Tuple, Set
from pathlib import Path
from datetime import timedelta

from .config import Config, ConfigError
from .models import Task, ValidationError, parse_related_task_ids, parse_blocked_by_ids, validate_task_ids
from .storage import TaskStorage
from .search import TaskSearch, SearchFilters, OPEN_STATUSES
from .validators import TaskValidator
from .codec import order_task_fields
from .merge import TaskMerger
from .graph import DependencyGraph
from .project_registry import (
    ProjectRegistry, RegistryError, load_access_policy, require_enabled,
    route_to_project, source_project_root,
)
from .archive import (DEFAULT_HARD_MAX_BYTES, DEFAULT_MAX_RECORDS,
                      DEFAULT_TARGET_BYTES, archive_doctor, create_archive, plan_archive)
from .record_cli import RecordCLI, TYPED_GROUPS, add_record_parsers
from .migration_cli import MigrationCLI, add_migration_parser
from . import __version__


CONSOLE_COMMAND_NAMES = (
    'yylo-ledger',
    # Bounded migration launchers retained only through the 0.1 RC window.
    'juno-ledger', 'ledger-juno', 'jl',
    'juno-kanban', 'juno-feedback', 'kanban-juno',
)


# Exit codes
class ExitCode:
    """Standard exit codes."""
    SUCCESS = 0
    GENERAL_ERROR = 1
    INVALID_USAGE = 2
    CONFIG_ERROR = 3
    IO_ERROR = 4
    VALIDATION_ERROR = 5


class OutputFormatter:
    """Format output in different formats."""

    @staticmethod
    def format_tasks(tasks: List[Dict[str, Any]], output_format: str, pretty: bool = False) -> str:
        """
        Format task list for output.

        Args:
            tasks: List of task dictionaries
            output_format: Format type (ndjson, json, xml, table)
            pretty: Pretty print output

        Returns:
            Formatted string
        """
        if not tasks:
            return ""

        if output_format == 'pretty':
            return OutputFormatter._format_pretty_tasks(tasks)

        if output_format == 'ndjson':
            return '\n'.join(json.dumps(task, ensure_ascii=False) for task in tasks)

        elif output_format == 'json':
            indent = 2 if pretty else None
            return json.dumps(tasks, ensure_ascii=False, indent=indent)

        elif output_format == 'xml':
            lines = ['<?xml version="1.0" encoding="UTF-8"?>']
            lines.append('<tasks>')
            for task in tasks:
                lines.append('  <task>')
                for key, value in task.items():
                    if value is None:
                        lines.append(f'    <{key} />')
                    elif isinstance(value, list):
                        lines.append(f'    <{key}>')
                        for item in value:
                            lines.append(f'      <item>{OutputFormatter._escape_xml(str(item))}</item>')
                        lines.append(f'    </{key}>')
                    else:
                        lines.append(f'    <{key}>{OutputFormatter._escape_xml(str(value))}</{key}>')
                lines.append('  </task>')
            lines.append('</tasks>')
            return '\n'.join(lines)

        elif output_format == 'table':
            if not tasks:
                return ""

            # Simple table format
            lines = []
            lines.append(f"{'ID':<8} {'Status':<12} {'Body':<40} {'Tags':<15} {'Related':<15} {'Blocked By':<15}")
            lines.append("-" * 110)

            for task in tasks:
                task_id = task.get('id', '')[:8]
                status = task.get('status', '')[:12]
                body = task.get('body', '')[:37] + ("..." if len(task.get('body', '')) > 37 else "")
                tags = ', '.join(task.get('feature_tags', []) or [])[:12] + ("..." if len(', '.join(task.get('feature_tags', []) or [])) > 12 else "")
                related = ', '.join(task.get('related_tasks', []) or [])[:12] + ("..." if len(', '.join(task.get('related_tasks', []) or [])) > 12 else "")
                blocked = ', '.join(task.get('blocked_by', []) or [])[:12] + ("..." if len(', '.join(task.get('blocked_by', []) or [])) > 12 else "")

                lines.append(f"{task_id:<8} {status:<12} {body:<40} {tags:<15} {related:<15} {blocked:<15}")

            return '\n'.join(lines)

        else:
            return json.dumps(tasks, ensure_ascii=False)

    @staticmethod
    def _format_pretty_tasks(tasks: List[Dict[str, Any]]) -> str:
        """Render tasks as human-readable field blocks."""
        blocks = []
        for task in tasks:
            lines = []
            for key, value in task.items():
                if isinstance(value, (dict, list)):
                    rendered = json.dumps(value, ensure_ascii=False, indent=2)
                elif value is None:
                    rendered = ""
                else:
                    rendered = str(value)

                if '\n' in rendered:
                    lines.append(f"{key}:")
                    lines.extend(f"  {line}" if line else "" for line in rendered.splitlines())
                else:
                    lines.append(f"{key}: {rendered}")
            blocks.append('\n'.join(lines))
        return '\n\n---\n\n'.join(blocks)

    @staticmethod
    def _escape_xml(text: str) -> str:
        """Escape XML special characters."""
        return (text.replace('&', '&amp;')
                    .replace('<', '&lt;')
                    .replace('>', '&gt;')
                    .replace('"', '&quot;')
                    .replace("'", '&#39;'))


class TaskCLI:
    """Main CLI application."""

    VERSION = __version__

    def __init__(self):
        """Initialize CLI."""
        self.config = None
        self.storage = None
        self.search = None
        self.parser = self._create_parser()

    def _get_command_name(self) -> str:
        """
        Detect the YYLO Ledger command name being used.

        Returns:
            The command name as it appears in sys.argv[0]
        """
        import os
        import sys

        # Get the command name from sys.argv[0]
        command_path = sys.argv[0] if sys.argv else 'yylo-ledger'
        command_name = os.path.basename(command_path)

        # Handle different scenarios
        if command_name in CONSOLE_COMMAND_NAMES:
            return command_name
        elif command_name.endswith('.py'):
            # Direct Python execution uses the canonical public identity.
            return 'yylo-ledger'
        else:
            # Development wrappers and embedded callers use the canonical identity.
            return 'yylo-ledger'

    def _create_parser(self) -> argparse.ArgumentParser:
        """
        Create argument parser with all commands and options.

        Returns:
            Configured ArgumentParser
        """
        # Detect the YYLO Ledger command name being used.
        command_name = self._get_command_name()

        # Main parser
        parser = argparse.ArgumentParser(
            prog=command_name,
            description='YYLO Ledger task manager. Use without arguments for full command reference.',
            epilog=f'Use "{command_name} COMMAND --help" for more information on a specific command',
            formatter_class=argparse.RawDescriptionHelpFormatter,
            allow_abbrev=False,
        )

        # Global options
        parser.add_argument(
            '-c', '--config',
            metavar='PATH',
            help='Config file path (default: .juno_task/tasks/config.json)'
        )
        parser.add_argument(
            '-f', '--format',
            choices=['ndjson', 'json', 'xml', 'table'],
            metavar='FORMAT',
            help='Output format: ndjson, json, xml, table'
        )
        parser.add_argument(
            '-p', '--pretty',
            action='store_true',
            help='Pretty print output; for get/list/search/ready without -f, render human-readable multiline fields'
        )
        parser.add_argument(
            '--raw',
            action='store_true',
            help='Output compact/raw format for machine processing'
        )
        parser.add_argument(
            '-v', '--verbose',
            action='store_true',
            help='Verbose output (show debug info)'
        )
        parser.add_argument(
            '--project',
            metavar='ALIAS',
            help='Route this command through an allowed registered project wrapper'
        )
        parser.add_argument(
            '--version',
            action='version',
            version=f'yylo-ledger {self.VERSION}'
        )

        # Subcommands
        subparsers = parser.add_subparsers(
            dest='command',
            help='Available commands',
            metavar='COMMAND'
        )

        # PROJECT registry management (does not initialize local task storage)
        project_parser = subparsers.add_parser(
            'project',
            help='Manage the opt-in cross-project registry',
            description='Add, list, show, remove, or inspect the opt-in user project registry',
        )
        project_subparsers = project_parser.add_subparsers(
            dest='project_command', required=True, metavar='ACTION'
        )
        project_add = project_subparsers.add_parser('add', help='Register an initialized Juno project')
        project_add.add_argument('alias')
        project_add.add_argument('--path', required=True)
        project_add.add_argument('--replace', action='store_true')
        project_subparsers.add_parser('list', help='List registered projects')
        project_show = project_subparsers.add_parser('show', help='Show one registered project')
        project_show.add_argument('alias')
        project_remove = project_subparsers.add_parser('remove', help='Remove one registered project')
        project_remove.add_argument('alias')
        project_subparsers.add_parser('status', help='Show registry enablement without exposing disabled paths')

        # Native Record v2 groups. Flat commands below remain the explicit
        # legacy task compatibility surface; they are not silently reinterpreted.
        add_record_parsers(subparsers)
        add_migration_parser(subparsers)

        host_parser = subparsers.add_parser(
            'host', help='Serve bounded read-only Record projections', allow_abbrev=False)
        host_parser.add_argument('--host', default='127.0.0.1',
                                 help='Explicit bind host (default: 127.0.0.1)')
        host_parser.add_argument('--port', type=int, default=8765,
                                 help='Explicit bind port, or 0 for an ephemeral port (default: 8765)')
        host_parser.add_argument('--access-policy', choices=['local', 'private'], default='local',
                                 help='local requires loopback; private permits an explicit non-loopback bind')
        host_parser.add_argument('--allow-redirect-host', action='append', default=[], metavar='HOST',
                                 help='Approve HTTPS Artifact redirects to exactly this hostname (repeatable)')
        host_parser.add_argument('--max-output-bytes', type=int, default=1024 * 1024)
        host_parser.add_argument('--max-range-bytes', type=int, default=256 * 1024)

        # CREATE command
        create_parser = subparsers.add_parser(
            'create',
            help='Create a new task',
            description='Create a new task with specified body and optional metadata'
        )
        # Support both positional and --body flag for flexibility
        create_parser.add_argument('body', nargs='?', help='Task description/body (positional)')
        create_parser.add_argument('--body', dest='body_flag', help='Task description/body (--body flag)')
        create_parser.add_argument('--body-file', dest='body_file', help='Read task body from UTF-8 file, or - for stdin')
        create_parser.add_argument('--title', dest='title', help='Task title (prepended to body as "title:{title}\\n\\n{body}")')
        create_parser.add_argument('--status', help='Initial status (default: from config)')
        create_parser.add_argument('--tags', nargs='*', help='Feature tags (letters/numbers/underscore/hyphen only, space-separated: --tags backend fix_auth OR comma-separated: --tags backend,fix_auth)')
        create_parser.add_argument('--commit', help='Git commit hash')
        create_parser.add_argument('--field', action='append', default=[], metavar='KEY=JSON', help='Set a custom field (repeatable)')
        create_parser.add_argument('--receipt-file', help='Write the complete structured mutation receipt to PATH, or - for stderr')
        create_parser.add_argument('--related-tasks', nargs='*', dest='related_tasks', help='Related task IDs (space-separated: --related-tasks ABC123 DEF456 OR comma-separated: --related-tasks ABC123,DEF456). Also auto-parsed from body using [task_id]...[/task_id], [task_id]...[/], ##ID, or ## ID1 ID2 ## formats. Missing-but-valid IDs are stored as forward references with a warning.')
        create_parser.add_argument('--blocked-by', nargs='*', dest='blocked_by', help='Task IDs that must be completed before this task (space-separated: --blocked-by ABC123 DEF456 OR comma-separated: --blocked-by ABC123,DEF456). Also auto-parsed from body using [blocked_by]...[/], [block_by]...[/], [block]...[/], [parent_task]...[/] formats')
        create_parser.add_argument(
            '--reject-duplicates',
            '--no-duplicate',
            '--discard-duplicates',
            dest='reject_duplicates',
            action='store_true',
            help='Reject creation when an open task (backlog/todo/in_progress) already has the exact same body'
        )

        # SEARCH command
        search_parser = subparsers.add_parser(
            'search',
            help='Search for tasks',
            description='Search for tasks using various filters'
        )
        search_parser.add_argument('--id', help='Filter by task ID')
        search_parser.add_argument('--status', nargs='*', help='Filter by status (space-separated: --status todo done OR comma-separated: --status todo,done)')
        search_parser.add_argument('--tag', nargs='*', help='Filter by tag (space-separated: --tag backend urgent OR comma-separated: --tag backend,urgent)')
        search_parser.add_argument('--exclude', nargs='*', help='Exclude tasks with these tags (space-separated: --exclude deprecated archived OR comma-separated: --exclude deprecated,archived)')
        search_parser.add_argument('--commit', help='Filter by commit hash')
        search_parser.add_argument('--body', help='Search in task body')
        search_parser.add_argument('--response', help='Search in agent response')
        search_parser.add_argument('--open', action='store_true', help='Show only open tasks (no agent response)')
        search_parser.add_argument('--recent', action='store_true', help='Sort by most recent')
        search_parser.add_argument('--limit', type=int, help='Max number of results (default: from config)')
        search_parser.add_argument('--offset', type=int, default=0, help='Number of sorted matches to skip')
        search_parser.add_argument('--cursor', help='Opaque cursor returned by a previous page')
        search_parser.add_argument('--show-cursor', action='store_true', help='Opt in to emitting an opaque next-page cursor (use --offset for context-efficient pagination)')
        search_parser.add_argument('--field', action='append', default=[], metavar='KEY=VALUE')
        search_parser.add_argument('--field-exists', action='append', default=[], metavar='KEY')
        search_parser.add_argument('--field-before', action='append', default=[], metavar='KEY=VALUE')
        search_parser.add_argument('--field-after', action='append', default=[], metavar='KEY=VALUE')
        search_parser.add_argument('--overdue', action='store_true')
        search_parser.add_argument('--projection', choices=['metadata', 'summary', 'full'], default='summary')
        search_parser.add_argument('--fields', help='Comma-separated output fields (id is always retained)')
        search_parser.add_argument('--full', action='store_true', help='Audited alias for --projection full')
        search_parser.add_argument('--sort', choices=['asc', 'desc'], default='desc', help='Sort order by last_modified (asc: oldest first, desc: newest first)')
        search_parser.add_argument('-p', '--pretty', action='store_true', help='Render human-readable multiline body/agent_response fields unless -f/--format is set')
        search_parser.add_argument('-f', '--format', dest='search_format', choices=['ndjson', 'json', 'xml', 'table'], metavar='FORMAT', help='Output format: ndjson, json, xml, table. JSON format includes tasks array and summary object.')

        # GET command
        get_parser = subparsers.add_parser(
            'get',
            help='Get one or more tasks by ID',
            description='Retrieve one or more tasks by their IDs'
        )
        get_parser.add_argument('ids', nargs='*', help='Task ID(s) (positional, supports multiple)')
        get_parser.add_argument('-ID', '--id', dest='id_flag', help='Task ID via flag (--ID or --id)')
        get_parser.add_argument('--compact', action='store_true', help='Show related tasks as IDs only, without embedding their full task bodies')
        get_parser.add_argument('-p', '--pretty', action='store_true', help='Render human-readable multiline body/agent_response fields unless -f/--format is set')

        # SHOW command (alias for get)
        show_parser = subparsers.add_parser(
            'show',
            help='Get one or more tasks by ID (alias for get)',
            description='Retrieve one or more tasks by their IDs (alias for get)'
        )
        show_parser.add_argument('ids', nargs='*', help='Task ID(s) (positional, supports multiple)')
        show_parser.add_argument('-ID', '--id', dest='id_flag', help='Task ID via flag (--ID or --id)')
        show_parser.add_argument('--compact', action='store_true', help='Show related tasks as IDs only, without embedding their full task bodies')
        show_parser.add_argument('-p', '--pretty', action='store_true', help='Render human-readable multiline body/agent_response fields unless -f/--format is set')

        # UPDATE command
        update_parser = subparsers.add_parser(
            'update',
            help='Update a task',
            description='Update task fields (status, agent_response, commit_hash)'
        )
        update_parser.add_argument('id', nargs='?', help='Task ID (positional)')
        update_parser.add_argument('-ID', '--id', dest='id_flag', help='Task ID via flag (--ID or --id)')
        update_parser.add_argument('--status', help='New status')
        update_parser.add_argument('--body', help='Task body')
        update_parser.add_argument('--body-file', dest='body_file', help='Read task body from UTF-8 file, or - for stdin')
        update_parser.add_argument('--response', help='Agent response')
        update_parser.add_argument('--response-file', dest='response_file', help='Read agent response from UTF-8 file, or - for stdin')
        update_parser.add_argument('--commit', help='Git commit hash')
        update_parser.add_argument('--field', action='append', default=[], metavar='KEY=JSON', help='Set a custom field (repeatable)')
        update_parser.add_argument('--expected-revision', help='Fail if the normalized task revision changed')
        update_parser.add_argument('--receipt-file', help='Write the complete structured mutation receipt to PATH, or - for stderr')
        update_parser.add_argument('--tags', nargs='*', help='Feature tags (letters/numbers/underscore/hyphen only, space-separated: --tags backend fix_auth OR comma-separated: --tags backend,fix_auth, replaces existing)')
        update_parser.add_argument('--blocked-by', nargs='*', dest='blocked_by', help='Task IDs that must be completed before this task (space-separated: --blocked-by ABC123 DEF456 OR comma-separated: --blocked-by ABC123,DEF456). Replaces existing blocked_by. Cycle detection applied.')

        # ARCHIVE command (replaces delete/remove)
        archive_parser = subparsers.add_parser(
            'archive',
            help='Archive a task',
            description='Archive a task by setting its status to archive'
        )
        archive_parser.add_argument('id', nargs='?', help='Task ID to archive (positional)')
        archive_parser.add_argument('-ID', '--id', dest='id_flag', help='Task ID via flag (--ID or --id)')
        archive_parser.add_argument('--receipt-file', help='Write the complete structured mutation receipt to PATH, or - for stderr')

        archive_pack_parser = subparsers.add_parser(
            'archive-pack', help='Plan or manage immutable cold archive packs',
            allow_abbrev=False)
        archive_pack_subparsers = archive_pack_parser.add_subparsers(
            dest='archive_pack_command', required=True, metavar='ACTION')
        archive_plan_parser = archive_pack_subparsers.add_parser(
            'plan', help='Create a revision-bound cold archive plan', allow_abbrev=False)
        archive_plan_parser.add_argument('--status', default='done,archive')
        archive_plan_parser.add_argument('--older-than', default='90d')
        archive_plan_parser.add_argument('--max-tasks', type=int, default=DEFAULT_MAX_RECORDS)
        archive_plan_parser.add_argument('--target-bytes', type=int, default=DEFAULT_TARGET_BYTES)
        archive_plan_parser.add_argument('--hard-max-bytes', type=int, default=DEFAULT_HARD_MAX_BYTES)
        archive_plan_parser.add_argument('--report', required=True)
        archive_create_parser = archive_pack_subparsers.add_parser(
            'create', help='Activate a sealed cold archive plan', allow_abbrev=False)
        archive_create_parser.add_argument('--plan', required=True)
        archive_create_parser.add_argument('--report', required=True)
        archive_pack_subparsers.add_parser(
            'doctor', help='Verify every immutable cold archive artifact', allow_abbrev=False)

        archive_search_parser = subparsers.add_parser(
            'archive-search', help='Explicit bounded search of immutable cold tasks',
            allow_abbrev=False)
        archive_search_parser.add_argument('--id')
        archive_search_parser.add_argument('--status', nargs='*')
        archive_search_parser.add_argument('--tag', nargs='*')
        archive_search_parser.add_argument('--before', help='Only tasks modified before this ISO date/time')
        archive_search_parser.add_argument('--limit', type=int, default=20)
        archive_search_parser.add_argument('--offset', type=int, default=0)
        archive_search_parser.add_argument('--cursor', help='Opaque cursor returned by a previous archive page')
        archive_search_parser.add_argument('--projection', choices=['metadata', 'summary', 'full'], default='summary')
        archive_search_parser.add_argument('--fields')
        archive_search_parser.add_argument('--full', action='store_true')
        archive_search_parser.add_argument('--sort', choices=['asc', 'desc'], default='desc')
        archive_search_parser.add_argument('-p', '--pretty', action='store_true')
        archive_search_parser.add_argument('-f', '--format', dest='search_format',
                                           choices=['ndjson', 'json', 'xml', 'table'])

        # MARK command (new workflow command)
        mark_parser = subparsers.add_parser(
            'mark',
            help='Mark a task with status and response',
            description='Mark a task with new status and required agent response'
        )
        mark_parser.add_argument('status', help='New status to mark task as')
        mark_parser.add_argument('id_positional', nargs='?', help='Task ID (positional, after status)')
        mark_parser.add_argument('-ID', '--id', dest='id_flag', help='Task ID via flag (--ID or --id)')
        mark_parser.add_argument('--response', help='Agent response')
        mark_parser.add_argument('--response-file', dest='response_file', help='Read agent response from UTF-8 file, or - for stdin')
        mark_parser.add_argument('--commit', help='Git commit hash (recommended)')
        mark_parser.add_argument('--receipt-file', help='Write the complete structured mutation receipt to PATH, or - for stderr')

        umbrella_parser = subparsers.add_parser(
            'umbrella-finalize',
            help='Atomically complete a receipt-admitted umbrella and its owned children')
        umbrella_parser.add_argument('id', help='Umbrella task ID')
        umbrella_parser.add_argument('--admission-receipt', required=True,
                                     help='Sealed umbrella-admission receipt with per-child ownership and revisions')
        umbrella_parser.add_argument('--evidence-receipt', required=True,
                                     help='Sealed machine-pass evidence receipt')
        umbrella_parser.add_argument('--commit', required=True, help='Commit shared by all finalization transitions')
        umbrella_parser.add_argument('--receipt-file', required=True,
                                     help='Write the sealed finalization receipt to PATH, or - for stdout')

        # LIST command (alias for search)
        list_parser = subparsers.add_parser(
            'list',
            help='List tasks (alias for search)',
            description='List tasks with optional filters'
        )
        list_parser.add_argument('--status', nargs='*', help='Filter by status (space-separated: --status todo done OR comma-separated: --status todo,done)')
        list_parser.add_argument('--tag', nargs='*', help='Filter by tag (space-separated: --tag backend urgent OR comma-separated: --tag backend,urgent)')
        list_parser.add_argument('--exclude', nargs='*', help='Exclude tasks with these tags (space-separated: --exclude deprecated archived OR comma-separated: --exclude deprecated,archived)')
        list_parser.add_argument('--open', action='store_true', help='Show only open tasks')
        list_parser.add_argument('--recent', action='store_true', help='Sort by most recent')
        list_parser.add_argument('--limit', type=int, help='Max number of results')
        list_parser.add_argument('--offset', type=int, default=0, help='Number of sorted results to skip before applying --limit (default: 0)')
        list_parser.add_argument('--cursor', help='Opaque cursor returned by a previous page')
        list_parser.add_argument('--show-cursor', action='store_true', help='Opt in to emitting an opaque next-page cursor (use --offset for context-efficient pagination)')
        list_parser.add_argument('--projection', choices=['metadata', 'summary', 'full'], default='summary')
        list_parser.add_argument('--fields', help='Comma-separated output fields (id is always retained)')
        list_parser.add_argument('--full', action='store_true', help='Audited alias for --projection full')
        list_parser.add_argument('--sort', choices=['asc', 'desc'], default='desc', help='Sort order by last_modified (asc: oldest first, desc: newest first). Default status priority is open->closed; with --status filters, provided status order is preserved.')
        list_parser.add_argument('-p', '--pretty', action='store_true', help='Render human-readable multiline body/agent_response fields unless -f/--format is set')
        list_parser.add_argument('-f', '--format', dest='list_format', choices=['ndjson', 'json', 'xml', 'table'], metavar='FORMAT', help='Output format: ndjson, json, xml, table. JSON format includes tasks array and summary object.')

        # TAGS command (aggregate tag usage counts)
        tags_parser = subparsers.add_parser(
            'tags',
            help='Show tag usage counts',
            description='Aggregate feature tag counts across tasks (optionally filtered by status)'
        )
        tags_parser.add_argument('--status', nargs='*', help='Filter source tasks by status before counting tags (space-separated: --status todo in_progress OR comma-separated: --status todo,in_progress)')
        tags_parser.add_argument('-f', '--format', dest='tags_format', choices=['ndjson', 'json', 'xml', 'table'], metavar='FORMAT', help='Output format: ndjson, json, xml, table. Table uses markdown layout for readability.')

        # DEPS command (dependency query/management)
        # Uses first positional to distinguish: deps TASK_ID (show) vs deps add/remove
        # action_or_id is optional to support flag-only syntax: deps --ID X --blocked-by Y (defaults to "add")
        deps_parser = subparsers.add_parser(
            'deps',
            help='Show or manage dependencies for a task',
            description='Show dependency info: deps TASK_ID. Add: deps add -ID TASK_ID --blocked-by IDS. Remove: deps remove -ID TASK_ID --blocked-by IDS. Shorthand: deps --ID TASK_ID --blocked-by IDS (defaults to add)'
        )
        deps_parser.add_argument('action_or_id', nargs='?', default=None, help='Task ID to show info, or "add"/"remove" action')
        deps_parser.add_argument('-ID', '--id', dest='task_id', help='Task ID (for add/remove actions, or show when used alone)')
        deps_parser.add_argument('--blocked-by', nargs='*', dest='blocked_by', help='Blocker task ID(s) (for add/remove)')

        # READY command (list tasks ready to work on)
        ready_parser = subparsers.add_parser(
            'ready',
            help='List tasks that are ready to work on (all blockers resolved)',
            description='Show tasks with status in [backlog, todo, in_progress] whose blockers are all done/archive (use --status to filter and preserve status order, and --sort asc|desc to order by last_modified within each status)'
        )
        ready_parser.add_argument('--tag', nargs='*', help='Filter by tag')
        ready_parser.add_argument('--status', nargs='*', help='Filter by status and preserve provided status order (space/comma separated)')
        ready_parser.add_argument('--limit', type=int, help='Max number of results')
        ready_parser.add_argument('--offset', type=int, default=0, help='Number of sorted ready results to skip before applying --limit (default: 0)')
        ready_parser.add_argument('--cursor', help='Opaque cursor returned by a previous page')
        ready_parser.add_argument('--show-cursor', action='store_true', help='Opt in to emitting an opaque next-page cursor (use --offset for context-efficient pagination)')
        ready_parser.add_argument('--projection', choices=['metadata', 'summary', 'full'], default='summary')
        ready_parser.add_argument('--fields', help='Comma-separated output fields (id is always retained)')
        ready_parser.add_argument('--full', action='store_true', help='Audited alias for --projection full')
        ready_parser.add_argument('--sort', choices=['asc', 'desc'], default='desc', help='Sort order by last_modified (asc: oldest first, desc: newest first)')
        ready_parser.add_argument('--raw', action='store_true', dest='ready_raw', help='Compact output for scripting')
        ready_parser.add_argument('-p', '--pretty', action='store_true', help='Render human-readable multiline body/agent_response fields unless -f/--format is set')
        ready_parser.add_argument('-f', '--format', dest='ready_format', choices=['ndjson', 'json', 'xml', 'table'], metavar='FORMAT', help='Output format')

        # ORDER command (topological sort of open tasks)
        order_parser = subparsers.add_parser(
            'order',
            help='Show execution order (topological sort of open tasks)',
            description='Display tasks in dependency order (topological sort)'
        )
        order_parser.add_argument('--scores', action='store_true', help='Include priority scores')
        order_parser.add_argument('--projection', choices=['metadata', 'summary', 'full'], default='summary')
        order_parser.add_argument('--fields', help='Comma-separated output fields (id is always retained)')
        order_parser.add_argument('--full', action='store_true', help='Audited alias for --projection full')
        order_parser.add_argument('-f', '--format', dest='order_format', choices=['ndjson', 'json', 'xml', 'table'], metavar='FORMAT', help='Output format')

        # MERGE command (new workflow command for combining task files)
        merge_parser = subparsers.add_parser(
            'merge',
            help='Merge multiple .juno_task directories',
            description='Merge tasks from multiple .juno_task directories into a target location'
        )
        merge_parser.add_argument(
            'sources',
            nargs='*',
            help='Source .juno_task directory paths to merge'
        )
        merge_parser.add_argument(
            '--into',
            required=True,
            metavar='TARGET',
            help='Target .juno_task directory path'
        )
        merge_parser.add_argument(
            '--strategy',
            choices=['keep-newer', 'keep-both'],
            default='keep-newer',
            help='Conflict resolution strategy (default: keep-newer)'
        )
        merge_parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Preview merge without making changes'
        )
        merge_parser.add_argument(
            '--plan-file',
            help='Write the sealed dry-run plan to this path'
        )
        merge_parser.add_argument(
            '--apply-plan',
            help='Apply this previously reviewed sealed plan'
        )
        merge_parser.add_argument(
            '--receipt-file',
            help='Write the exact apply receipt to this path'
        )
        merge_parser.add_argument(
            '--find-all',
            action='store_true',
            help='Auto-discover all .juno_task directories under current directory'
        )

        history_parser = subparsers.add_parser('history', help='Show opt-in per-task ledger history')
        history_parser.add_argument('id')
        history_parser.add_argument('--limit', type=int)
        history_parser.add_argument('--include-content', action='store_true')

        reconcile_parser = subparsers.add_parser('reconcile', help='Record direct task-file edits')
        reconcile_parser.add_argument('--check', action='store_true')

        subparsers.add_parser('doctor', help='Verify task markers, paths, hashes, and ledgers')
        cache_parser = subparsers.add_parser('cache', help='Manage disposable query cache')
        cache_parser.add_argument('action', choices=['rebuild'])

        convert_parser = subparsers.add_parser('convert', help='One-time validated NDJSON import')
        convert_parser.add_argument('source')
        convert_parser.add_argument('--dry-run', action='store_true')
        convert_parser.add_argument('--report', required=True, help='Durable machine conversion receipt')
        convert_parser.add_argument('--pre-cutover-tag')
        convert_parser.add_argument('--backup-path')
        convert_parser.add_argument('--legacy-package', help='Exact legacy wheel retained for rollback')
        convert_parser.add_argument('--new-package-version')
        convert_parser.add_argument('--benchmark-receipt')

        compatibility_parser = subparsers.add_parser('compatibility', help='Manage receipt-driven compatibility window')
        compatibility_parser.add_argument('action', choices=['accept', 'lift'])
        compatibility_parser.add_argument('--acceptance-receipt')
        compatibility_parser.add_argument('--evidence', action='append', default=[], metavar='GATE=PATH')
        compatibility_parser.add_argument('--report')

        export_parser = subparsers.add_parser('export-legacy', help='Lossless rollback NDJSON export')
        export_parser.add_argument('destination')
        export_parser.add_argument('--report')

        rollback_parser = subparsers.add_parser('rollback', help='Execute verified storage rollback')
        rollback_parser.add_argument('mode', choices=['immediate', 'post-write'])
        rollback_parser.add_argument('--conversion-receipt')
        rollback_parser.add_argument('--legacy-wheel')
        rollback_parser.add_argument('--legacy-runtime-dir')
        rollback_parser.add_argument('--archive')
        rollback_parser.add_argument('--report', required=True)

        # COMPLETION command (generate shell completion scripts)
        completion_parser = subparsers.add_parser(
            'completion',
            help='Generate shell completion script',
            description='Generate shell completion script for bash, zsh, or fish'
        )
        completion_parser.add_argument(
            'shell',
            nargs='?',
            choices=['bash', 'zsh', 'fish'],
            default='bash',
            help='Target shell (default: bash)'
        )

        # Internal completion candidate provider (used by generated scripts)
        complete_parser = subparsers.add_parser(
            '__complete',
            help=argparse.SUPPRESS,
            description=argparse.SUPPRESS
        )
        complete_parser.add_argument('--index', type=int, required=True, help=argparse.SUPPRESS)
        complete_parser.add_argument('words', nargs='*', help=argparse.SUPPRESS)

        # argparse enables long-option prefix matching independently on every
        # subparser.  Write-capable commands must reject misspelled or removed
        # controls rather than silently selecting a dangerous near-match.
        for command_parser in subparsers.choices.values():
            command_parser.allow_abbrev = False
        return parser

    @staticmethod
    def _extract_project_route(args: List[str]) -> Tuple[Optional[str], List[str]]:
        """Extract one global --project option without touching forwarded argv."""
        alias = None
        forwarded: List[str] = []
        index = 0
        while index < len(args):
            argument = args[index]
            lowered = argument.lower()
            if lowered == '--project':
                if alias is not None:
                    raise RegistryError('--project may be specified only once')
                if index + 1 >= len(args) or args[index + 1].startswith('-'):
                    raise RegistryError('--project requires an alias')
                alias = args[index + 1]
                index += 2
                continue
            if lowered.startswith('--project='):
                if alias is not None:
                    raise RegistryError('--project may be specified only once')
                alias = argument.split('=', 1)[1]
                if not alias:
                    raise RegistryError('--project requires an alias')
                index += 1
                continue
            forwarded.append(argument)
            index += 1
        return alias, forwarded

    def cmd_project(self, args) -> int:
        root = source_project_root()
        policy = load_access_policy(root)
        if args.project_command == 'status':
            payload = {'enabled': policy.enabled, 'source': policy.source}
            if policy.enabled:
                payload['allowedProjects'] = sorted(policy.allowed_projects)
            print(json.dumps(payload, sort_keys=True))
            return ExitCode.SUCCESS

        require_enabled(root)
        registry = ProjectRegistry()
        if args.project_command == 'add':
            entry = registry.add(args.alias, Path(args.path), replace=args.replace)
            payload = {'alias': args.alias, **entry}
        elif args.project_command == 'list':
            payload = [
                {'alias': alias, **entry}
                for alias, entry in registry.list().items()
            ]
        elif args.project_command == 'show':
            payload = {'alias': args.alias, **registry.get(args.alias)}
        elif args.project_command == 'remove':
            payload = {'alias': args.alias, **registry.remove(args.alias)}
        else:  # argparse requires one of the actions above.
            raise RegistryError(f'unknown project action: {args.project_command}')
        print(json.dumps(payload, sort_keys=True))
        return ExitCode.SUCCESS

    def _normalize_arguments(self, args: List[str]) -> List[str]:
        """
        Normalize command line arguments for case-insensitive handling.

        This method preprocesses arguments to handle case variations like:
        - -ID, -Id, -id, -iD -> -ID (preserve original short form)
        - --ID, --Id, --iD -> --id (normalize to lowercase)
        - -C, -c -> -c (normalize short flags to lowercase)
        - --STATUS, --Status -> --status (normalize long flags to lowercase)

        Args:
            args: Original command line arguments

        Returns:
            Normalized arguments with consistent case
        """
        normalized = []

        for arg in args:
            if arg.startswith('--'):
                # Long form arguments: normalize to lowercase
                if '=' in arg:
                    # Handle --arg=value format
                    flag, value = arg.split('=', 1)
                    normalized.append(f"{flag.lower()}={value}")
                else:
                    # Handle --arg format
                    normalized.append(arg.lower())
            elif arg.startswith('-') and len(arg) > 1:
                # Short form arguments: handle special cases
                if arg.upper() == '-ID':
                    # Special case: -ID variations should map to -ID (preserve uppercase for compatibility)
                    normalized.append('-ID')
                elif len(arg) == 2:
                    # Single character flags: normalize to lowercase
                    normalized.append(f"-{arg[1:].lower()}")
                else:
                    # Multi-character short flags: handle as-is
                    normalized.append(arg)
            else:
                # Not an argument flag, preserve as-is
                normalized.append(arg)

        return normalized

    def _init_components(self, config_path: Optional[str] = None):
        """Initialize configuration, storage, and search components.

        Programmatic/public command dispatch may execute several warm commands in
        one process. Reuse components only while the explicit config file identity
        is unchanged; ordinary one-command entrypoint behavior is unaffected.
        """
        try:
            config_identity = None
            if config_path:
                path = Path(config_path).resolve()
                stat = path.stat()
                config_identity = (str(path), stat.st_size, stat.st_mtime_ns,
                                   hashlib.sha256(path.read_bytes()).hexdigest())
            if (config_identity is not None
                    and getattr(self, '_component_config_identity', None) == config_identity
                    and getattr(self, 'storage', None) is not None):
                return
            self.config = Config(config_path)
            self.storage = TaskStorage(self.config)
            self.search = TaskSearch(self.config, self.storage)
            self._component_config_identity = config_identity
        except ConfigError as e:
            print(f"Configuration error: {e}", file=sys.stderr)
            sys.exit(ExitCode.CONFIG_ERROR)
        except Exception as e:
            print(f"Initialization error: {e}", file=sys.stderr)
            sys.exit(ExitCode.GENERAL_ERROR)

    def _validate_project_root(self) -> bool:
        """
        Validate current location against project root detection rules.

        Returns:
            True if validation passes, False if it fails
        """
        try:
            is_valid, message, recommended_path = self.config.validate_current_location()

            if message:
                if is_valid:
                    # Warning case
                    print(message, file=sys.stderr)
                    print(f"TIP: You can set JUNO_TASK_ROOT={recommended_path} to override this behavior", file=sys.stderr)
                else:
                    # Error case
                    print(message, file=sys.stderr)
                    return False

            return is_valid

        except Exception as e:
            # If validation fails, log but don't block operation
            if self.config and hasattr(self.config, 'config') and \
               self.config.config.get('project_root', {}).get('enable_prevention', True):
                print(f"Warning: Project root validation error: {e}", file=sys.stderr)
            return True

    def _get_command_specific_format(self, args: argparse.Namespace) -> Optional[str]:
        """Return command-local format option when provided (e.g., list/search --format)."""
        for attr in ('list_format', 'search_format', 'ready_format', 'order_format', 'tags_format'):
            value = getattr(args, attr, None)
            if value:
                return value
        return None

    def _get_output_format(self, args: argparse.Namespace) -> str:
        """Get output format from args or config.

        Priority order:
        1. Command-specific format (e.g., list/search --format)
        2. Global format (-f/--format before command)
        3. Config default
        4. Fallback to 'ndjson'
        """
        command_format = self._get_command_specific_format(args)
        if command_format:
            return command_format

        # Check for global format (-f/--format before command)
        if args.format:
            return args.format
        elif self.config:
            return self.config.default_output_format
        else:
            return 'ndjson'

    def _truncate_task_bodies(self, tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Bound and redact broad body/response output before every renderer."""
        # Get truncation limit from environment variable
        truncate_limit = int(os.environ.get('YYLO_LEDGER_LIST_BODY_TRUNCATE_CHARS', '1200'))

        def redact(value: Any) -> Any:
            if isinstance(value, dict):
                return {key: redact(item) for key, item in value.items()}
            if isinstance(value, list):
                return [redact(item) for item in value]
            if not isinstance(value, str):
                return value
            value = re.sub(r'(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b', '[REDACTED_EMAIL]', value)
            value = re.sub(r'(?i)\b(?:api[_-]?key|token|password)\s*[:=]\s*[^\s]+', '[REDACTED_CREDENTIAL]', value)
            patterns = self.config.to_dict().get('output', {}).get('redaction_patterns', []) if self.config else []
            for pattern in patterns:
                value = re.sub(pattern, '[REDACTED_PROJECT]', value)
            return value

        truncated_tasks = []
        for task in tasks:
            task_copy = redact(task)
            for key in ('body', 'agent_response'):
                text = str(task_copy.get(key, '') or '')
                if len(text) > truncate_limit:
                    text = text[:truncate_limit] + f"[Truncated full size: {len(text)} characters, use get command to read the full {key}]"
                task_copy[key] = text
            truncated_tasks.append(task_copy)

        return truncated_tasks

    def _project_broad(self, tasks: List[Dict[str, Any]], args: argparse.Namespace) -> List[Dict[str, Any]]:
        projection = 'full' if getattr(args, 'full', False) else getattr(args, 'projection', 'summary')
        if projection == 'full':
            print('AUDIT: full broad task export requested', file=sys.stderr)
            projected = [dict(task) for task in tasks]
        elif projection == 'metadata':
            allowed = {'id', 'status', 'created_date', 'last_modified', 'commit_hash',
                       'feature_tags', 'related_tasks', 'blocked_by', 'fields'}
            redacted = self._truncate_task_bodies(tasks)
            projected = [{key: value for key, value in task.items() if key in allowed} for task in redacted]
        else:
            projected = self._truncate_task_bodies(tasks)
        selected = getattr(args, 'fields', None)
        if selected:
            allowed = {'id', *(part.strip() for part in selected.split(',') if part.strip())}
            unknown = allowed - set().union(*(task.keys() for task in projected))
            if unknown:
                raise ValueError('unknown projected fields: ' + ', '.join(sorted(unknown)))
            projected = [{key: value for key, value in task.items() if key in allowed} for task in projected]
        return projected

    @staticmethod
    def _parse_key_values(values: Optional[List[str]], parse_json: bool = False) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for value in values or []:
            if '=' not in value:
                raise ValueError(f"expected KEY=VALUE, got: {value}")
            key, raw = value.split('=', 1)
            if not key:
                raise ValueError("custom field key cannot be empty")
            if parse_json:
                try:
                    result[key] = json.loads(raw)
                except json.JSONDecodeError:
                    result[key] = raw
            else:
                result[key] = raw
        return result

    def _parse_comma_separated_values(self, values: Optional[List[str]]) -> Optional[List[str]]:
        """Parse comma-separated values from argument lists.

        Supports multiple syntaxes:
        - Space-separated: --status todo done
        - Comma-separated: --status todo,done
        - Mixed: --status todo,done archive

        Args:
            values: List of argument values from argparse (nargs='*')

        Returns:
            Flattened list of individual values, or None if input is None/empty
        """
        if not values:  # None or empty list
            return None

        result = []
        for value in values:
            # Split on commas, strip whitespace, filter out empty strings
            parts = [part.strip() for part in value.split(',') if part.strip()]
            result.extend(parts)

        return result if result else None

    def _looks_like_id_list(self, value: str) -> bool:
        """Return True when a string appears to be a task-id list (space/comma separated)."""
        parts = [part for part in re.split(r'[\s,]+', value.strip()) if part]
        if not parts:
            return False
        return all(re.fullmatch(r'[A-Za-z0-9]{6}', part) for part in parts)

    def _looks_like_tag_list(self, value: str) -> bool:
        """Return True when a string appears to be a tag list (space/comma separated)."""
        parts = [part for part in re.split(r'[\s,]+', value.strip()) if part]
        if not parts:
            return False
        return all(re.fullmatch(r'[A-Za-z0-9_-]+', part) for part in parts)

    def _recover_create_body_from_list_flags(self, args: argparse.Namespace) -> Optional[str]:
        """Recover trailing positional body swallowed by nargs='*' create flags.

        argparse greedily consumes trailing non-flag tokens for list-like options
        (`--related-tasks`, `--blocked-by`, `--tags`). If users put the body at
        the end, this can leave `args.body` empty and incorrectly trigger
        "Task body is required". We recover only when the swallowed value clearly
        looks like prose (contains whitespace and is not an ID/tag list).
        """
        candidate_sources = (
            ('related_tasks', self._looks_like_id_list),
            ('blocked_by', self._looks_like_id_list),
            ('tags', self._looks_like_tag_list),
        )

        for field_name, list_shape_check in candidate_sources:
            values = getattr(args, field_name, None)
            if not values:
                continue

            candidate = values[-1]
            if not isinstance(candidate, str):
                continue

            # Only recover body-like prose, not compact IDs/tags.
            has_whitespace = any(char.isspace() for char in candidate)
            if not has_whitespace:
                continue
            if list_shape_check(candidate):
                continue

            # Mutate args in place so downstream parsing of tags/ids remains correct.
            values.pop()
            if len(values) == 0:
                setattr(args, field_name, None)

            return candidate

        return None

    def _pretty_was_requested(self, args: argparse.Namespace) -> bool:
        """Return True only when the user supplied --pretty/-p explicitly."""
        return bool(getattr(args, '_pretty_requested', False))

    @staticmethod
    def _decode_pretty_text(value: str) -> str:
        """Decode common escaped whitespace sequences for human output."""
        return (value
                .replace('\\r\\n', '\n')
                .replace('\\n', '\n')
                .replace('\\r', '\r')
                .replace('\\t', '\t'))

    def _humanize_pretty_fields(self, value: Any) -> Any:
        """Recursively decode multiline task text fields while preserving data shape."""
        if isinstance(value, dict):
            return {key: self._humanize_pretty_fields(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._humanize_pretty_fields(item) for item in value]
        if isinstance(value, str):
            return self._decode_pretty_text(value)
        return value

    def _resolve_output_style(self, args: argparse.Namespace) -> Tuple[str, bool]:
        """Resolve effective output format and pretty-print style.

        Keeps jq-style default behavior consistent across task and aggregate
        commands: default to pretty JSON when no explicit format is provided.
        Explicit --pretty on task-list commands switches the default renderer to
        human-readable blocks so escaped body/agent_response newlines are visible.
        """
        output_format = self._get_output_format(args)

        # Check if an explicit format was specified (global or command-specific)
        explicit_format = args.format or self._get_command_specific_format(args)
        pretty_requested = self._pretty_was_requested(args)

        # New jq-style formatting logic (v1.4.0)
        if hasattr(args, 'raw') and args.raw:
            # --raw flag: use compact output (old behavior)
            pretty = False
        elif explicit_format:
            # Explicit format specified: use existing pretty logic
            pretty = args.pretty
        elif pretty_requested and getattr(args, 'command', None) in {'get', 'show', 'list', 'search', 'ready'}:
            output_format = 'pretty'
            pretty = True
        else:
            # Default: pretty-printed JSON (new jq-style behavior)
            output_format = 'json'
            pretty = True

        return output_format, pretty

    def _format_output(self, tasks: List[Dict[str, Any]], args: argparse.Namespace) -> str:
        """Format task-list output based on format and options."""
        output_format, pretty = self._resolve_output_style(args)
        if self._pretty_was_requested(args):
            tasks = self._humanize_pretty_fields(tasks)
        tasks = [dict(order_task_fields(task)) for task in tasks]
        return OutputFormatter.format_tasks(tasks, output_format, pretty)

    def _build_tag_counts(self, tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Build deterministic tag-count aggregates from task list."""
        counts: Dict[str, int] = {}
        for task in tasks:
            for tag in (task.get('feature_tags') or []):
                counts[tag] = counts.get(tag, 0) + 1

        sorted_tags = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        return [{'tag': tag, 'count': count} for tag, count in sorted_tags]

    def _format_tag_output(self, tags: List[Dict[str, Any]], args: argparse.Namespace) -> str:
        """Format tag-count aggregates across supported output formats."""
        if not tags:
            return ""

        output_format, pretty = self._resolve_output_style(args)

        if output_format == 'ndjson':
            return '\n'.join(json.dumps(item, ensure_ascii=False) for item in tags)

        if output_format == 'json':
            indent = 2 if pretty else None
            return json.dumps(tags, ensure_ascii=False, indent=indent)

        if output_format == 'xml':
            lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<tags>']
            for item in tags:
                lines.append('  <tag>')
                lines.append(f"    <name>{OutputFormatter._escape_xml(str(item.get('tag', '')))}</name>")
                lines.append(f"    <count>{item.get('count', 0)}</count>")
                lines.append('  </tag>')
            lines.append('</tags>')
            return '\n'.join(lines)

        if output_format == 'table':
            lines = ['| Tag | Count |', '| --- | ---: |']
            for item in tags:
                lines.append(f"| {item.get('tag', '')} | {item.get('count', 0)} |")
            return '\n'.join(lines)

        return json.dumps(tags, ensure_ascii=False)

    def _load_all_tasks_for_graph(self) -> List[Dict[str, Any]]:
        """Load all tasks as dicts for building a DependencyGraph."""
        all_tasks = []
        for filepath in self.storage.get_files():
            for task in self.storage.read_tasks(filepath):
                all_tasks.append(task)
        return all_tasks

    def _find_open_duplicate_by_body(self, body: str) -> Optional[Dict[str, Any]]:
        """Return first open task whose body exactly matches the candidate body."""
        for task in self.storage.read_all_tasks():
            if task.get('status') in OPEN_STATUSES and task.get('body') == body:
                return task
        return None

    def _read_text_file_argument(self, path: str, field_name: str) -> Tuple[Optional[str], Optional[int]]:
        """Read exact UTF-8 text from a file argument or stdin marker.

        Returns (text, None) on success, or (None, exit_code) after printing a clear
        error. Keeping this helper shared makes file-input behavior a single source
        of truth for create/update/mark body and response fields.
        """
        if path == '-':
            try:
                return sys.stdin.read(), None
            except Exception as e:
                print(f"Error reading {field_name} from stdin: {e}", file=sys.stderr)
                return None, ExitCode.IO_ERROR

        try:
            return Path(path).read_text(encoding='utf-8'), None
        except FileNotFoundError:
            print(f"Error reading {field_name} file: file not found: {path}", file=sys.stderr)
            return None, ExitCode.IO_ERROR
        except IsADirectoryError:
            print(f"Error reading {field_name} file: path is a directory: {path}", file=sys.stderr)
            return None, ExitCode.IO_ERROR
        except PermissionError:
            print(f"Error reading {field_name} file: permission denied: {path}", file=sys.stderr)
            return None, ExitCode.IO_ERROR
        except UnicodeDecodeError as e:
            print(f"Error reading {field_name} file as UTF-8: {e}", file=sys.stderr)
            return None, ExitCode.IO_ERROR
        except OSError as e:
            print(f"Error reading {field_name} file: {e}", file=sys.stderr)
            return None, ExitCode.IO_ERROR

    @staticmethod
    def _has_text_value(value: Optional[str]) -> bool:
        """Return True when a CLI text field was explicitly provided with content."""
        return value is not None and value != ''

    @staticmethod
    def _inline_body_shell_sensitive_reason(value: str) -> Optional[str]:
        """Return why an inline create body should be supplied via file/stdin instead.

        Shell expansion happens before Python receives argv, so this guard cannot
        stop unquoted command substitutions that the shell already executed. It
        does reject dangerous literals that survive parsing and points users to
        the file/stdin path, which is the reliable single source of truth for
        rich markdown bodies.
        """
        if '$(' in value:
            return 'command substitution literal "$()"'
        if '`' in value:
            return 'backtick literal "`"'
        if '\n' in value or '\r' in value:
            return 'multiline inline body'
        if '<<' in value:
            return 'here-document-like literal "<<"'
        return None

    def _reject_shell_sensitive_inline_create_body(self, body: Optional[str]) -> Optional[int]:
        """Reject shell-sensitive inline create bodies; return an exit code on error."""
        if body is None:
            return None
        reason = self._inline_body_shell_sensitive_reason(body)
        if not reason:
            return None
        print(
            "Error: refusing shell-sensitive inline task body "
            f"({reason}). Use --body-file PATH or --body-file - for rich markdown so "
            "your shell cannot expand backticks, $(), heredocs, or multiline content before yylo-ledger sees it.",
            file=sys.stderr,
        )
        return ExitCode.INVALID_USAGE

    def _emit_mutation_receipt(self, args: argparse.Namespace, receipt) -> None:
        destination = getattr(args, 'receipt_file', None)
        if not destination:
            return
        payload = json.dumps(receipt.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
        if destination == '-':
            print(payload, end='', file=sys.stderr)
        else:
            path = Path(destination)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            temporary.write_text(payload, encoding='utf-8')
            os.replace(temporary, path)

    def cmd_create(self, args: argparse.Namespace) -> int:
        """Handle create command."""
        try:
            # Validate project root before creating tasks
            if not self._validate_project_root():
                return ExitCode.GENERAL_ERROR
            # Get body from either positional argument, --body flag, or --body-file.
            body = args.body_flag if args.body_flag is not None else args.body
            body_file = getattr(args, 'body_file', None)

            # Recover trailing prose body swallowed by nargs='*' list flags.
            if not body and not body_file:
                recovered_body = self._recover_create_body_from_list_flags(args)
                if recovered_body:
                    body = recovered_body

            if body_file and self._has_text_value(body):
                print("Error: --body-file cannot be used together with positional body or --body", file=sys.stderr)
                return ExitCode.INVALID_USAGE

            if body_file:
                body, error_code = self._read_text_file_argument(body_file, 'body')
                if error_code is not None:
                    return error_code
            else:
                # Multiline content read from stdin did not travel through shell
                # argument parsing, so it is safe even though the legacy stdin
                # shortcut is represented as a positional body after pre-reading.
                if not getattr(args, '_body_from_stdin', False):
                    error_code = self._reject_shell_sensitive_inline_create_body(body)
                    if error_code is not None:
                        return error_code

            # Handle --title: merge into body as "title:{title}\n\n{body}"
            title = getattr(args, 'title', None)
            if title:
                if body:
                    body = f"title:{title}\n\n{body}"
                else:
                    # --title provided without body: use title as the body
                    body = f"title:{title}"

            if not body:
                print("Error: Task body is required. Use either 'task create \"body text\"', 'task create --body \"body text\"', 'task create --body-file path.md', or 'task create --title \"title\"'", file=sys.stderr)
                return ExitCode.INVALID_USAGE

            if getattr(args, 'reject_duplicates', False):
                duplicate_task = self._find_open_duplicate_by_body(body)
                if duplicate_task:
                    print(
                        f"Duplicate task body matches open task {duplicate_task.get('id')} "
                        f"(status: {duplicate_task.get('status')})."
                    )
                    return ExitCode.VALIDATION_ERROR

            # Prepare task data
            task_data = {
                'body': body,
            }

            if args.status:
                task_data['status'] = args.status
            elif self.config:
                task_data['status'] = self.config.default_status

            if args.tags:
                # Parse comma-separated tags (Issue 26)
                parsed_tags = self._parse_comma_separated_values(args.tags)
                task_data['feature_tags'] = parsed_tags

            if args.commit:
                task_data['commit_hash'] = args.commit
            if getattr(args, 'field', None):
                task_data['fields'] = self._parse_key_values(args.field, parse_json=True)

            # Handle related tasks - combine explicit arg and parsed from body
            all_related_ids = []

            # Parse related task IDs from body using [task_id]...[/] format
            parsed_from_body = parse_related_task_ids(body)
            all_related_ids.extend(parsed_from_body)

            # Add explicitly provided related tasks (via --related-tasks arg)
            if hasattr(args, 'related_tasks') and args.related_tasks:
                explicit_ids = self._parse_comma_separated_values(args.related_tasks)
                if explicit_ids:
                    for task_id in explicit_ids:
                        if task_id not in all_related_ids:
                            all_related_ids.append(task_id)

            # Validate related task IDs.
            # Keep valid-format IDs as informational forward references even when
            # they are not yet present in storage (parity with blocked_by behavior).
            if all_related_ids:
                valid_format_ids = []
                invalid_format_ids = []

                for related_id in all_related_ids:
                    is_valid_id, _ = TaskValidator.validate_id(related_id)
                    if is_valid_id:
                        valid_format_ids.append(related_id)
                    else:
                        invalid_format_ids.append(related_id)

                for invalid_id in invalid_format_ids:
                    print(f"Warning: {invalid_id} has invalid task ID format and was ignored.", file=sys.stderr)

                if valid_format_ids:
                    _, missing_ids = validate_task_ids(valid_format_ids, self.storage)
                    for missing_id in missing_ids:
                        print(f"Warning: {missing_id} wasn't found on the kanban (stored as forward reference).", file=sys.stderr)

                    task_data['related_tasks'] = valid_format_ids

            # Handle blocked_by - combine explicit arg and parsed from body
            all_blocked_ids = []

            # Parse blocked_by IDs from body using synonym tags
            parsed_blocked = parse_blocked_by_ids(body)
            all_blocked_ids.extend(parsed_blocked)

            # Add explicitly provided blocked_by (via --blocked-by arg)
            if hasattr(args, 'blocked_by') and args.blocked_by:
                explicit_blocked = self._parse_comma_separated_values(args.blocked_by)
                if explicit_blocked:
                    for bid in explicit_blocked:
                        if bid not in all_blocked_ids:
                            all_blocked_ids.append(bid)

            # Validate and apply blocked_by
            if all_blocked_ids:
                # Split into existing and forward-reference IDs (not yet in kanban)
                valid_blocked, invalid_blocked = validate_task_ids(all_blocked_ids, self.storage)

                for invalid_id in invalid_blocked:
                    print(f"Warning: {invalid_id} wasn't found on the kanban (stored as forward reference).", file=sys.stderr)

                if valid_blocked:
                    # Cycle detection: build graph from all existing tasks + proposed new task
                    # The new task doesn't exist yet, so we simulate it
                    all_tasks = self._load_all_tasks_for_graph()
                    # Add a placeholder for the new task
                    new_task_placeholder = {'id': '__new__', 'status': task_data.get('status', 'backlog'), 'blocked_by': valid_blocked}
                    all_tasks.append(new_task_placeholder)

                    graph = DependencyGraph(all_tasks)

                    # Check for cycles: for each existing blocker, would adding this edge create a cycle?
                    for blocker_id in valid_blocked:
                        cycle = graph.detect_cycle(blocker_id, '__new__')
                        if cycle:
                            # Replace placeholder ID in cycle path for clarity
                            cycle_path = ' → '.join(c if c != '__new__' else 'NEW_TASK' for c in cycle)
                            print(f"Error: Dependency cycle detected: {cycle_path}", file=sys.stderr)
                            return ExitCode.VALIDATION_ERROR

                # Store ALL IDs including forward references to not-yet-existing tasks
                task_data['blocked_by'] = all_blocked_ids

            # Create task
            task = self.storage.create_task(**task_data)
            self._emit_mutation_receipt(args, self.storage.last_receipt)

            # Output created task
            output = self._format_output([task.to_dict()], args)
            print(output)

            if args.verbose:
                print(f"Task {task.id} created successfully", file=sys.stderr)

            return ExitCode.SUCCESS

        except ValidationError as e:
            print(f"Validation error: {e}", file=sys.stderr)
            return ExitCode.VALIDATION_ERROR
        except Exception as e:
            print(f"Error creating task: {e}", file=sys.stderr)
            return ExitCode.GENERAL_ERROR

    def cmd_search(self, args: argparse.Namespace) -> int:
        """Handle search command through the indexed cache plan."""
        try:
            return self._cmd_indexed_collection(args, "search")
            # Handle nargs='*' with comma-separated support (Issue 26)
            status_filter = self._parse_comma_separated_values(args.status)
            if status_filter and len(status_filter) == 1:
                status_filter = status_filter[0]  # Single value - use as string for compatibility
            # else: multiple values - keep as list, or None if empty

            tag_filter = self._parse_comma_separated_values(args.tag)
            if tag_filter and len(tag_filter) == 1:
                tag_filter = tag_filter[0]  # Single value - use as string for compatibility
            # else: multiple values - keep as list, or None if empty

            exclude_tags_filter = self._parse_comma_separated_values(args.exclude)
            if exclude_tags_filter and len(exclude_tags_filter) == 1:
                exclude_tags_filter = exclude_tags_filter[0]  # Single value - use as string for compatibility
            # else: multiple values - keep as list, or None if empty

            # Build shared search criteria
            display_limit = args.limit or (self.config.default_limit if self.config else 5)
            sort_order = getattr(args, 'sort', 'desc')
            filter_kwargs = {
                'id': args.id,
                'status': status_filter,
                'tag': tag_filter,
                'exclude_tags': exclude_tags_filter,
                'commit_hash': args.commit,
                'body_text': args.body,
                'response_text': args.response,
                'open_only': args.open,
                'recent': args.recent,
                'sort_order': sort_order,
            }

            # Fetch full match set for summary stats and limited result set for display.
            # Why: search should mirror list ergonomics where summary reflects the
            # complete matched set while the rendered output respects --limit.
            all_filters = SearchFilters(limit=sys.maxsize, **filter_kwargs)
            all_results = self.search.search(all_filters)
            if any((args.field, args.field_exists, args.field_before, args.field_after, args.overdue)):
                field_matches = self.storage.query_fields(
                    field_equals=self._parse_key_values(args.field),
                    field_exists=args.field_exists,
                    field_before=self._parse_key_values(args.field_before),
                    field_after=self._parse_key_values(args.field_after),
                    overdue=args.overdue,
                )
                allowed_ids = {task['id'] for task in field_matches}
                all_results = [task for task in all_results if task.get('id') in allowed_ids]

            # Check if any results found
            if not all_results:
                print("No results found")
                return ExitCode.SUCCESS

            offset = self._validate_pagination_args(args)
            if offset is None:
                return ExitCode.INVALID_USAGE
            display_results = self._paginate_results(all_results, offset, display_limit)

            # Projection/redaction happens before every renderer.
            projected_results = self._project_broad(display_results, args)

            # Output results
            output = self._format_output(projected_results, args)
            print(output)

            # Show summary/count context (parity with list command)
            effective_offset = next((i for i, task in enumerate(all_results)
                                     if display_results and task.get('id') == display_results[0].get('id')), offset)
            self._set_next_cursor(args, effective_offset, display_results, len(all_results))
            self._show_summary_stats(all_results, args, displayed_count=len(display_results))

            if args.verbose:
                print(f"Showing {len(display_results)} of {len(all_results)} tasks", file=sys.stderr)

            return ExitCode.SUCCESS

        except Exception as e:
            print(f"Error searching tasks: {e}", file=sys.stderr)
            return ExitCode.GENERAL_ERROR

    @staticmethod
    def _emit_phase_timing(phase: str, started: float, **details):
        if os.environ.get('YYLO_LEDGER_DIAGNOSTICS', '').lower() not in ('1', 'true', 'yes'):
            return
        suffix = ' '.join(f'{key}={value}' for key, value in details.items())
        print(f'DIAGNOSTIC phase={phase} duration_ms={(time.monotonic() - started) * 1000:.3f}'
              f'{" " + suffix if suffix else ""}', file=sys.stderr)

    def cmd_get(self, args: argparse.Namespace) -> int:
        """Handle get/show command - returns one or more tasks with related/dependency details."""
        command_started = time.monotonic()
        try:
            positional_ids = list(getattr(args, 'ids', []) or [])
            flag_id = getattr(args, 'id_flag', None)

            requested_ids: List[str] = []
            if flag_id:
                requested_ids.append(flag_id)
            requested_ids.extend(positional_ids)

            if not requested_ids:
                print("Error: Task ID is required. Use 'get TASK_ID [TASK_ID ...]' or 'get --ID TASK_ID'", file=sys.stderr)
                return ExitCode.INVALID_USAGE

            # De-duplicate while preserving first-seen order
            seen_ids: Set[str] = set()
            ordered_ids: List[str] = []
            for task_id in requested_ids:
                if task_id not in seen_ids:
                    seen_ids.add(task_id)
                    ordered_ids.append(task_id)

            task_lookup: Dict[str, Optional[Dict[str, Any]]] = {}
            missing_ids: List[str] = []
            for task_id in ordered_ids:
                lookup_started = time.monotonic()
                task = self.search.search_by_id(task_id)
                self._emit_phase_timing('exact_task_lookup', lookup_started, task=task_id,
                                        tier='hot_or_verified_cold', found=bool(task))
                task_lookup[task_id] = task
                if not task:
                    missing_ids.append(task_id)

            if missing_ids:
                if len(missing_ids) == 1:
                    print(f"Task not found: {missing_ids[0]}", file=sys.stderr)
                else:
                    print(f"Task(s) not found: {', '.join(missing_ids)}", file=sys.stderr)
                return ExitCode.GENERAL_ERROR

            def get_task_by_id(task_id: str) -> Optional[Dict[str, Any]]:
                if task_id in task_lookup:
                    return task_lookup[task_id]
                task_lookup[task_id] = self.search.search_by_id(task_id)
                return task_lookup[task_id]

            result_tasks: List[Dict[str, Any]] = []
            for task_id in ordered_ids:
                task = task_lookup[task_id]
                if not task:
                    continue

                task_copy = task.copy()

                # If task has related_tasks, fetch and include their details.
                # --compact keeps the cross-reference visible without embedding
                # full related task bodies, which can waste review context.
                related_task_ids = task.get('related_tasks') or []
                if related_task_ids:
                    related_tasks_details = []
                    for related_id in related_task_ids:
                        if getattr(args, 'compact', False):
                            # Compact exact readback is identity-only: do not turn
                            # each reference into another hot/cold/cache lookup.
                            related_tasks_details.append({'id': related_id})
                            continue
                        related_task = get_task_by_id(related_id)
                        if related_task:
                            related_tasks_details.append(related_task)
                        else:
                            print(f"Warning: Related task {related_id} no longer exists in kanban.", file=sys.stderr)

                    if related_tasks_details:
                        task_copy['_related_tasks_details'] = related_tasks_details

                # Canonical identity readback must win over optional derived
                # dependency enrichment. This call never validates/rebuilds the
                # global cache and SQLite lock waits are timeout-bounded.
                enrichment_started = time.monotonic()
                dependency, enrichment_error = self.storage.dependency_info_best_effort(task_id)
                self._emit_phase_timing('dependency_enrichment', enrichment_started, task=task_id,
                                        cache='unavailable' if enrichment_error else 'available')
                if enrichment_error:
                    print(f"Warning [exact_get_enrichment_unavailable] task={task_id} "
                          f"resource={self.storage.cache.path}: {enrichment_error}", file=sys.stderr)
                    dependency = {'blockers': [], 'dependents': [], 'priority_score': 0}
                blockers, dependents = dependency['blockers'], dependency['dependents']
                if blockers or dependents:
                    unmet_blockers = [{'id': bid, 'status': status or 'unknown'}
                                      for bid, status in blockers if status not in ('done', 'archive')]
                    met_blockers = [{'id': bid, 'status': status}
                                    for bid, status in blockers if status in ('done', 'archive')]
                    task_copy['_dependency_info'] = {
                        'is_blocked': bool(unmet_blockers), 'unmet_blockers': unmet_blockers,
                        'met_blockers': met_blockers, 'dependents': dependents,
                        'priority_score': dependency['priority_score']}

                result_tasks.append(task_copy)

            rendering_started = time.monotonic()
            output = self._format_output(result_tasks, args)
            print(output)
            self._emit_phase_timing('response_rendering', rendering_started, tasks=len(result_tasks))
            self._emit_phase_timing('get_total', command_started, tasks=len(result_tasks))
            return ExitCode.SUCCESS

        except Exception as e:
            print(f"Error getting task: {e}", file=sys.stderr)
            return ExitCode.GENERAL_ERROR

    def cmd_update(self, args: argparse.Namespace) -> int:
        """Handle update command."""
        try:
            # Validate project root before updating tasks
            if not self._validate_project_root():
                return ExitCode.GENERAL_ERROR
            # Resolve task_id from --ID/--id flag or positional argument
            task_id = getattr(args, 'id_flag', None) or args.id
            if not task_id:
                print("Error: Task ID is required. Use 'update TASK_ID' or 'update --ID TASK_ID'", file=sys.stderr)
                return ExitCode.INVALID_USAGE
            args.id = task_id
            # Build updates dictionary
            updates = {}
            if args.status:
                updates['status'] = args.status

            body_file = getattr(args, 'body_file', None)
            if body_file and args.body is not None:
                print("Error: --body-file cannot be used together with --body", file=sys.stderr)
                return ExitCode.INVALID_USAGE
            if body_file:
                body, error_code = self._read_text_file_argument(body_file, 'body')
                if error_code is not None:
                    return error_code
                updates['body'] = body
            elif args.body is not None:
                updates['body'] = args.body

            response_file = getattr(args, 'response_file', None)
            if response_file and args.response is not None:
                print("Error: --response-file cannot be used together with --response", file=sys.stderr)
                return ExitCode.INVALID_USAGE
            if response_file:
                response, error_code = self._read_text_file_argument(response_file, 'response')
                if error_code is not None:
                    return error_code
                updates['agent_response'] = response
            elif args.response is not None:
                updates['agent_response'] = args.response

            if args.commit:
                updates['commit_hash'] = args.commit
            if getattr(args, 'field', None):
                current_task = self.storage.find_task(args.id) or {}
                fields = dict(current_task.get('fields') or {})
                fields.update(self._parse_key_values(args.field, parse_json=True))
                updates['fields'] = fields
            if args.tags is not None:  # Allow empty list
                # Parse comma-separated tags (Issue 26)
                parsed_tags = self._parse_comma_separated_values(args.tags)
                updates['feature_tags'] = parsed_tags if parsed_tags is not None else []

            # Handle --blocked-by with cycle detection
            blocked_by_provided = hasattr(args, 'blocked_by') and args.blocked_by is not None
            if blocked_by_provided:
                parsed_blocked = self._parse_comma_separated_values(args.blocked_by)
                blocked_ids = parsed_blocked if parsed_blocked else []

                if blocked_ids:
                    # Self-dependency check
                    if args.id in blocked_ids:
                        print(f"Error: Task cannot be blocked by itself: {args.id}", file=sys.stderr)
                        return ExitCode.VALIDATION_ERROR

                    # Split into existing and forward-reference IDs (not yet in kanban)
                    valid_blocked, invalid_blocked = validate_task_ids(blocked_ids, self.storage)
                    for invalid_id in invalid_blocked:
                        print(f"Warning: {invalid_id} wasn't found on the kanban (stored as forward reference).", file=sys.stderr)

                    if valid_blocked:
                        # Cycle checks are recursive indexed dependency queries;
                        # mutation commands never materialize the whole board.
                        for blocker_id in valid_blocked:
                            if self.storage.dependency_would_cycle(args.id, blocker_id):
                                print(f"Error: Dependency cycle detected: {args.id} → {blocker_id} → {args.id}", file=sys.stderr)
                                return ExitCode.VALIDATION_ERROR

                    # Store ALL IDs including forward references to not-yet-existing tasks
                    updates['blocked_by'] = blocked_ids
                else:
                    # Empty --blocked-by clears dependencies
                    updates['blocked_by'] = []

            if not updates:
                print("No updates specified", file=sys.stderr)
                return ExitCode.INVALID_USAGE

            # Perform update
            success = self.storage.update_task(
                args.id, updates, expected_revision=getattr(args, 'expected_revision', None)
            )

            if success:
                self._emit_mutation_receipt(args, success)
                if args.verbose:
                    print(f"Task {args.id} updated successfully", file=sys.stderr)

                # Show updated task
                task = self.search.search_by_id(args.id)
                if task:
                    output = self._format_output([task], args)
                    print(output)

                return ExitCode.SUCCESS
            else:
                print(f"Task not found: {args.id}", file=sys.stderr)
                return ExitCode.GENERAL_ERROR

        except ValidationError as e:
            print(f"Validation error: {e}", file=sys.stderr)
            return ExitCode.VALIDATION_ERROR
        except Exception as e:
            print(f"Error updating task: {e}", file=sys.stderr)
            return ExitCode.GENERAL_ERROR

    def _cmd_indexed_collection(self, args: argparse.Namespace, command: str) -> int:
        """Shared SQL-backed list/search/ready implementation.

        Summaries are SQL aggregates and only the requested page is JSON-decoded.
        """
        status = self._parse_comma_separated_values(getattr(args, 'status', None))
        tags = self._parse_comma_separated_values(getattr(args, 'tag', None))
        excluded = self._parse_comma_separated_values(getattr(args, 'exclude', None))
        filters = {'status': status, 'tag': tags, 'exclude_tags': excluded,
                   'open_only': getattr(args, 'open', False)}
        if command == 'search':
            filters.update({'id': getattr(args, 'id', None), 'commit_hash': getattr(args, 'commit', None),
                            'body_text': getattr(args, 'body', None),
                            'response_text': getattr(args, 'response', None),
                            'field_equals': self._parse_key_values(getattr(args, 'field', None)),
                            'field_exists': getattr(args, 'field_exists', None) or [],
                            'field_before': self._parse_key_values(getattr(args, 'field_before', None)),
                            'field_after': self._parse_key_values(getattr(args, 'field_after', None))})
            filters['field_before'], filters['field_after'] = self.storage.normalize_field_ranges(
                filters['field_before'], filters['field_after'])
            definitions = self.config.to_dict().get('custom_fields', {})
            if getattr(args, 'overdue', False):
                from datetime import datetime, timezone
                if definitions.get('due_date', {}).get('type') != 'date':
                    raise ValueError('--overdue requires configured date field: due_date')
                filters['overdue'] = str(datetime.now(timezone.utc).date())
        offset = self._validate_pagination_args(args)
        if offset is None:
            return ExitCode.INVALID_USAGE
        limit = getattr(args, 'limit', None)
        if limit is None:
            limit = self.config.default_limit if self.config else 5
        query_limit = limit + 1 if limit else None
        sort_order = getattr(args, 'sort', 'desc')
        result = self.storage.query_collection(
            filters=filters, limit=query_limit, offset=offset,
            last_key=self._cursor_last_key, sort_order=sort_order,
            prioritized=command == 'list', status_sequence=status if command in ('list', 'ready') else None,
            ready=command == 'ready')
        tasks, keys = result['tasks'], result['keys']
        has_more = bool(limit and len(tasks) > limit)
        if has_more:
            tasks, keys = tasks[:limit], keys[:limit]
        if not tasks:
            print('No ready tasks found' if command == 'ready' else
                  ('No tasks found' if command == 'list' and not any((status, tags, excluded, filters.get('open_only'))) else 'No results found'))
            return ExitCode.SUCCESS
        if command == 'ready':
            if getattr(args, 'ready_format', None): args.format = args.ready_format
            if getattr(args, 'ready_raw', False): args.raw = True
        self._page_keys = keys
        print(self._format_output(self._project_broad(tasks, args), args))
        self._set_next_cursor(args, offset, tasks, result['total'], has_more=has_more)
        self._show_indexed_summary(result['total'], result['status_counts'], args, len(tasks))
        if getattr(args, 'verbose', False):
            print(f"Showing {len(tasks)} of {result['total']} tasks", file=sys.stderr)
        return ExitCode.SUCCESS

    def _show_indexed_summary(self, total: int, status_counts: Dict[str, int],
                              args: argparse.Namespace, displayed: int):
        statuses = self.config.status_values if self.config and hasattr(self.config, 'status_values') else ['backlog','todo','in_progress','done','archive']
        if self._get_output_format(args) == 'json':
            summary = {'total_tasks': total, 'displayed_tasks': displayed,
                       'status_counts': {s: status_counts.get(s, 0) for s in statuses},
                       'help': 'Use --limit N to show more/fewer results'}
            if getattr(args, 'show_cursor', False):
                summary['next_cursor'] = getattr(self, '_next_cursor', None)
            print(json.dumps({'summary': summary}, indent=2 if args.pretty else None))
            return
        print('\nSUMMARY:', file=sys.stderr)
        print(f'Displayed: {displayed} of {total} total tasks' if displayed != total else f'Total tasks: {total}', file=sys.stderr)
        print('Status breakdown:', file=sys.stderr)
        for status in statuses:
            print(f'  {status}: {status_counts.get(status, 0)}', file=sys.stderr)
        for status, count in status_counts.items():
            if status not in statuses: print(f'  {status}: {count}', file=sys.stderr)
        if getattr(self, '_next_cursor', None): print(f'Next cursor: {self._next_cursor}', file=sys.stderr)
        print('\nTIP: Use --limit N to show more/fewer results', file=sys.stderr)

    def _collection_revision(self) -> str:
        # A cursor is bound to the exact freshness-validated cache snapshot.
        self.storage._ensure_query_cache()
        revision = self.storage.cache.revision()
        if revision is None:
            list(self.storage.read_all_tasks())
            revision = self.storage.cache.revision()
        return revision or hashlib.sha256(b"canonical-empty").hexdigest()

    @staticmethod
    def _pagination_identity(args: argparse.Namespace) -> str:
        excluded = {
            'cursor', 'offset', 'limit', 'format', 'pretty', 'raw', 'verbose',
            'list_format', 'search_format', 'ready_format', 'ready_raw', 'show_cursor',
            'projection', 'full', 'fields', '_pretty_requested',
        }
        query = {key: value for key, value in vars(args).items()
                 if key not in excluded and value not in (None, False, [], {})}
        encoded = json.dumps(query, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(encoded.encode()).hexdigest()

    def _set_next_cursor(self, args: argparse.Namespace, offset: int,
                         displayed_tasks: List[Dict[str, Any]], total: int,
                         has_more: Optional[bool] = None):
        self._next_cursor = None
        if not getattr(args, 'show_cursor', False):
            return
        more = offset + len(displayed_tasks) < total if has_more is None else has_more
        if more and displayed_tasks:
            key = getattr(self, '_page_keys', None)
            last_key = list(key[-1]) if key else [0, str(displayed_tasks[-1].get('last_modified', '')), str(displayed_tasks[-1].get('id', ''))]
            core = {'v': 2, 'last': last_key, 'revision': self._collection_revision(),
                    'query': self._pagination_identity(args)}
            canonical = json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            payload = dict(core, integrity=self.storage.cache.sign_cursor(canonical))
            self._next_cursor = base64.urlsafe_b64encode(
                json.dumps(payload, separators=(',', ':')).encode()).decode().rstrip('=')

    def _validate_pagination_args(self, args: argparse.Namespace) -> Optional[int]:
        """Validate offset/cursor and reject cursors after collection mutation."""
        offset = getattr(args, 'offset', 0) or 0
        self._cursor_last_id = None
        self._cursor_last_key = None
        cursor = getattr(args, 'cursor', None)
        if cursor:
            if offset:
                print("Error: --cursor cannot be combined with non-zero --offset", file=sys.stderr)
                return None
            try:
                padded = cursor + '=' * (-len(cursor) % 4)
                payload = json.loads(base64.urlsafe_b64decode(padded).decode())
                integrity = payload.pop('integrity')
                canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
                if not isinstance(integrity, str) or not self.storage.cache.verify_cursor(canonical, integrity):
                    raise ValueError("cursor integrity")
                if payload.get('v') != 2:
                    raise ValueError("cursor version")
                if payload.get('revision') != self._collection_revision():
                    print("Error: cursor collection revision is stale; restart pagination", file=sys.stderr)
                    return None
                if payload.get('query') != self._pagination_identity(args):
                    print("Error: cursor does not belong to this collection query", file=sys.stderr)
                    return None
                last = payload.get('last')
                if not isinstance(last, list) or len(last) != 3 or not isinstance(last[0], int):
                    raise ValueError("cursor key")
                self._cursor_last_key = (last[0], str(last[1]), str(last[2]))
                self._cursor_last_id = str(last[2])
                offset = 0
            except Exception:
                print("Error: invalid opaque cursor", file=sys.stderr)
                return None
        if offset < 0:
            print("Error: --offset must be greater than or equal to 0", file=sys.stderr)
            return None
        limit = getattr(args, 'limit', None)
        if limit is not None and limit < 0:
            print("Error: --limit must be greater than or equal to 0", file=sys.stderr)
            return None
        return offset

    def _paginate_results(self, results: List[Dict[str, Any]], offset: int,
                          limit: Optional[int]) -> List[Dict[str, Any]]:
        """Apply offset compatibility or cache-revision keyset pagination."""
        if self._cursor_last_id is not None:
            positions = [index for index, task in enumerate(results)
                         if str(task.get('id')) == self._cursor_last_id]
            if not positions:
                raise ValueError("cursor key is absent from collection snapshot")
            offset = positions[0] + 1
        if limit is None or limit == 0:
            return results[offset:]
        return results[offset:offset + limit]

    def cmd_list(self, args: argparse.Namespace) -> int:
        """Handle list through the indexed cache plan."""
        try:
            return self._cmd_indexed_collection(args, "list")
            # Create filters from user arguments with comma-separated support (Issue 26)
            # Parse comma-separated values and handle nargs='*' which returns empty list when no args provided
            status_filter = self._parse_comma_separated_values(getattr(args, 'status', None))
            if status_filter and len(status_filter) == 1:
                status_filter = status_filter[0]  # Single value - use as string for compatibility
            # else: multiple values - keep as list, or None if empty

            tag_filter = self._parse_comma_separated_values(getattr(args, 'tag', None))
            if tag_filter and len(tag_filter) == 1:
                tag_filter = tag_filter[0]  # Single value - use as string for compatibility
            # else: multiple values - keep as list, or None if empty

            exclude_tags_filter = self._parse_comma_separated_values(getattr(args, 'exclude', None))
            if exclude_tags_filter and len(exclude_tags_filter) == 1:
                exclude_tags_filter = exclude_tags_filter[0]  # Single value - use as string for compatibility
            # else: multiple values - keep as list, or None if empty

            user_filters = SearchFilters(
                id=None,
                status=status_filter,
                tag=tag_filter,
                exclude_tags=exclude_tags_filter,
                commit_hash=None,
                body_text=None,
                open_only=getattr(args, 'open', False),
                recent=getattr(args, 'recent', False),
                limit=sys.maxsize  # Complete set: summaries must not truncate at scale.
            )

            # First get all tasks matching filters for summary statistics
            # IMPORTANT: Use Python backend directly to ensure consistent results
            # with search_prioritized_list (which also uses Python backend).
            # This fixes bug where ripgrep optimization could read from different
            # files or apply different filtering, causing summary stats mismatch.
            # See Issue 31: Summary stats showed wrong counts due to backend mismatch.
            all_tasks = self.search.python_search.search_all(user_filters)

            # Check if any tasks exist
            if not all_tasks:
                if user_filters.status or user_filters.tag or user_filters.exclude_tags or user_filters.open_only:
                    print("No results found")
                else:
                    print("No tasks found")
                return ExitCode.SUCCESS

            # Now get sorted set for display, then apply shared offset/limit pagination.
            # Why: pagination must happen after the same filter/status/sort ordering users see,
            # otherwise page boundaries drift when --status/--sort/--tag filters are combined.
            offset = self._validate_pagination_args(args)
            if offset is None:
                return ExitCode.INVALID_USAGE
            display_limit = args.limit if args.limit is not None else (self.config.default_limit if self.config else 5)

            # Create display filters with the same criteria and a large internal limit so
            # offset is applied to the final ordered set rather than before sorting.
            display_filters = SearchFilters(
                id=None,
                status=status_filter,
                tag=tag_filter,
                exclude_tags=exclude_tags_filter,
                commit_hash=None,
                body_text=None,
                open_only=getattr(args, 'open', False),
                recent=getattr(args, 'recent', False),
                limit=sys.maxsize
            )

            # Get sort order from args (default is 'desc' - newest first)
            sort_order = getattr(args, 'sort', 'desc')
            ordered_tasks = self.search.search_prioritized_list(
                sys.maxsize,
                display_filters,
                sort_order,
                status_sequence=status_filter,
            )
            display_tasks = self._paginate_results(ordered_tasks, offset, display_limit)

            # Projection/redaction happens before every renderer.
            projected_tasks = self._project_broad(display_tasks, args)

            # Output task results (limited set)
            output = self._format_output(projected_tasks, args)
            print(output)

            # Show summary statistics based on all tasks
            effective_offset = next((i for i, task in enumerate(ordered_tasks)
                                     if display_tasks and task.get('id') == display_tasks[0].get('id')), offset)
            self._set_next_cursor(args, effective_offset, display_tasks, len(ordered_tasks))
            self._show_summary_stats(all_tasks, args, displayed_count=len(display_tasks))

            if args.verbose:
                print(f"Showing {len(display_tasks)} of {len(all_tasks)} tasks", file=sys.stderr)

            return ExitCode.SUCCESS

        except Exception as e:
            print(f"Error listing tasks: {e}", file=sys.stderr)
            return ExitCode.GENERAL_ERROR

    def cmd_tags(self, args: argparse.Namespace) -> int:
        """Handle tags command - aggregate feature-tag counts."""
        try:
            status_filter = self._parse_comma_separated_values(getattr(args, 'status', None))
            if status_filter and len(status_filter) == 1:
                status_filter = status_filter[0]

            filters = SearchFilters(
                status=status_filter,
                limit=10000,
                sort_order='desc',
            )

            # Use python backend directly for deterministic aggregate source data.
            tasks = self.search.python_search.search_all(filters)
            tag_counts = self._build_tag_counts(tasks)

            if not tag_counts:
                print("No tags found")
                return ExitCode.SUCCESS

            output = self._format_tag_output(tag_counts, args)
            print(output)
            return ExitCode.SUCCESS

        except Exception as e:
            print(f"Error listing tags: {e}", file=sys.stderr)
            return ExitCode.GENERAL_ERROR

    def _show_summary_stats(self, tasks: List[Dict[str, Any]], args: argparse.Namespace, displayed_count: Optional[int] = None) -> None:
        """Show summary statistics of tasks by status."""
        # Count tasks by status
        status_counts = {}
        for task in tasks:
            status = task.get('status', 'unknown')
            status_counts[status] = status_counts.get(status, 0) + 1

        # Get all possible statuses from config (to show zeros)
        all_statuses = ['backlog', 'todo', 'in_progress', 'done', 'archive']
        if self.config and hasattr(self.config, 'status_values'):
            all_statuses = self.config.status_values

        # Determine effective output format (global or command-specific)
        effective_format = self._get_output_format(args)

        # Show summary (respect JSON output format)
        if effective_format == 'json':
            # Add helper text to JSON format
            help_text = "Use --limit N to show more/fewer results"
            summary = {
                "summary": {
                    "total_tasks": len(tasks),
                    "displayed_tasks": displayed_count if displayed_count is not None else len(tasks),
                    "status_counts": {status: status_counts.get(status, 0) for status in all_statuses},
                    "help": help_text,
                    "next_cursor": getattr(self, '_next_cursor', None)
                }
            }
            print(json.dumps(summary, indent=2 if args.pretty else None))
        else:
            print("\nSUMMARY:", file=sys.stderr)
            if displayed_count is not None and displayed_count != len(tasks):
                print(f"Displayed: {displayed_count} of {len(tasks)} total tasks", file=sys.stderr)
            else:
                print(f"Total tasks: {len(tasks)}", file=sys.stderr)
            print("Status breakdown:", file=sys.stderr)
            for status in all_statuses:
                count = status_counts.get(status, 0)
                print(f"  {status}: {count}", file=sys.stderr)

            # Show any unknown statuses
            for status, count in status_counts.items():
                if status not in all_statuses:
                    print(f"  {status}: {count}", file=sys.stderr)

            if getattr(self, '_next_cursor', None):
                print(f"Next cursor: {self._next_cursor}", file=sys.stderr)
            # Add helper text to stderr output
            print("", file=sys.stderr)  # Empty line for readability
            print("TIP: Use --limit N to show more/fewer results", file=sys.stderr)

    def cmd_archive_pack(self, args: argparse.Namespace) -> int:
        if args.archive_pack_command == 'doctor':
            failures = archive_doctor(self.storage.juno_root)
            print(json.dumps({'ok': not failures, 'failures': failures}, ensure_ascii=False))
            return ExitCode.SUCCESS if not failures else ExitCode.VALIDATION_ERROR
        if args.archive_pack_command == 'create':
            plan_path = self.storage._external_path(
                Path(args.plan), self.storage.project_root, 'archive plan report')
            report = self.storage._external_path(
                Path(args.report), self.storage.project_root, 'archive create report')
            if not plan_path.is_file():
                raise ValueError('archive plan report does not exist')
            try:
                plan = json.loads(plan_path.read_text(encoding='utf-8'))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError('archive plan report is invalid') from exc
            receipt = create_archive(self.storage, plan, report, __version__)
            print(json.dumps({'report': str(report),
                              'receipt_sha256': receipt['receipt_sha256'],
                              'archive_commit': receipt['archive_commit'],
                              'selected_tasks': len(receipt['selected_ids'])},
                             ensure_ascii=False))
            return ExitCode.SUCCESS
        if args.archive_pack_command != 'plan':
            raise ValueError('unsupported archive-pack action')
        match = re.fullmatch(r'([1-9][0-9]*)d', args.older_than)
        if not match:
            raise ValueError('--older-than must use positive whole days, for example 90d')
        statuses = [item for item in args.status.split(',') if item]
        report = self.storage._external_path(Path(args.report), self.storage.project_root,
                                             'archive plan report')
        plan = plan_archive(
            self.storage, statuses=statuses,
            older_than=timedelta(days=int(match.group(1))),
            max_tasks=args.max_tasks, target_bytes=args.target_bytes,
            hard_max_bytes=args.hard_max_bytes)
        report.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(plan, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':')) + '\n'
        with report.open('x', encoding='utf-8', newline='\n') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        print(json.dumps({'report': str(report), 'plan_sha256': plan['plan_sha256'],
                          'selected_tasks': len(plan['selected_ids']),
                          'batches': len(plan['batches'])}, ensure_ascii=False))
        return ExitCode.SUCCESS

    def cmd_archive_search(self, args: argparse.Namespace) -> int:
        """Explicit cold-only query with shared projection and redaction semantics."""
        try:
            if args.limit is not None and (args.limit < 0 or args.limit > 1000):
                raise ValueError('--limit must be between 0 and 1000')
            offset = self._validate_pagination_args(args)
            if offset is None:
                return ExitCode.INVALID_USAGE
            statuses = self._parse_comma_separated_values(args.status)
            tags = self._parse_comma_separated_values(args.tag)
            before = args.before
            if before:
                from datetime import datetime, timezone
                parsed = datetime.fromisoformat(before.replace('Z', '+00:00'))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                before = parsed.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
            query_limit = args.limit + 1 if args.limit else None
            result = self.storage.archive_search(
                task_id=args.id, statuses=statuses, tags=tags, before=before,
                limit=query_limit, offset=offset, sort_order=args.sort,
                last_key=self._cursor_last_key)
            raw_tasks, keys = result['tasks'], result['keys']
            has_more = bool(args.limit and len(raw_tasks) > args.limit)
            if has_more:
                raw_tasks, keys = raw_tasks[:args.limit], keys[:args.limit]
            self._page_keys = keys
            tasks = self._project_broad(raw_tasks, args)
            if not tasks:
                print('No archived results found')
                return ExitCode.SUCCESS
            print(self._format_output(tasks, args))
            self._set_next_cursor(args, offset, raw_tasks, result['total'], has_more=has_more)
            if self._get_output_format(args) == 'json':
                print(json.dumps({'summary': {'total_tasks': result['total'],
                      'displayed_tasks': len(tasks), 'offset': offset,
                      'next_cursor': getattr(self, '_next_cursor', None)}},
                      indent=2 if args.pretty else None))
            else:
                print(f"Archived: displayed {len(tasks)} of {result['total']}", file=sys.stderr)
            return ExitCode.SUCCESS
        except Exception as exc:
            print(f"Error searching archived tasks: {exc}", file=sys.stderr)
            return ExitCode.GENERAL_ERROR

    def cmd_archive(self, args: argparse.Namespace) -> int:
        """Handle archive command."""
        try:
            # Validate project root before archiving tasks
            if not self._validate_project_root():
                return ExitCode.GENERAL_ERROR
            # Resolve task_id from --ID/--id flag or positional argument
            task_id = getattr(args, 'id_flag', None) or args.id
            if not task_id:
                print("Error: Task ID is required. Use 'archive TASK_ID' or 'archive --ID TASK_ID'", file=sys.stderr)
                return ExitCode.INVALID_USAGE
            args.id = task_id
            # Exact lookup distinguishes immutable archived IDs from missing IDs.
            task = self.storage.find_task_exact(args.id)
            if not task:
                print(f"Error: Task {args.id} not found", file=sys.stderr)
                return ExitCode.VALIDATION_ERROR

            # The legacy task command remains a status mutation. Immediate verified
            # cold archival is exposed by the typed `record task archive` command.
            updated = self.storage.update_task(args.id, {'status': 'archive'}, operation='archive')
            if updated:
                self._emit_mutation_receipt(args, updated)
                print(f"Task {args.id} archived successfully")
                if args.verbose:
                    print(f"Task {args.id} status set to archive", file=sys.stderr)
                return ExitCode.SUCCESS

            print(f"Error: Failed to archive task {args.id}", file=sys.stderr)
            return ExitCode.GENERAL_ERROR

        except Exception as e:
            print(f"Error archiving task: {e}", file=sys.stderr)
            return ExitCode.GENERAL_ERROR

    def cmd_mark(self, args: argparse.Namespace) -> int:
        """Handle mark command with required response."""
        try:
            # Validate project root before marking tasks
            if not self._validate_project_root():
                return ExitCode.GENERAL_ERROR
            # Resolve task_id from --ID/--id flag or positional argument
            task_id = getattr(args, 'id_flag', None) or getattr(args, 'id_positional', None)
            if not task_id:
                print("Error: Task ID is required. Use 'mark STATUS --id TASK_ID' or 'mark STATUS TASK_ID'", file=sys.stderr)
                return ExitCode.INVALID_USAGE
            args.id = task_id

            response_file = getattr(args, 'response_file', None)
            if response_file and args.response is not None:
                print("Error: --response-file cannot be used together with --response", file=sys.stderr)
                return ExitCode.INVALID_USAGE

            if response_file:
                response, error_code = self._read_text_file_argument(response_file, 'response')
                if error_code is not None:
                    return error_code
            elif args.response is not None:
                response = args.response
            else:
                cn = self._get_command_name()
                print("Error: Agent response is required. Use --response TEXT or --response-file path.md", file=sys.stderr)
                print("\nMark usage:", file=sys.stderr)
                print(f"  {cn} mark STATUS TASK_ID --response \"message\"", file=sys.stderr)
                print(f"  {cn} mark STATUS --id TASK_ID --response-file response.md", file=sys.stderr)
                return ExitCode.INVALID_USAGE

            # Check if task exists after validating required response arguments so
            # usage mistakes report INVALID_USAGE instead of being masked by a
            # missing/nonexistent task ID.
            task = self.storage.find_task_exact(args.id)
            if not task:
                print(f"Error: Task {args.id} not found", file=sys.stderr)
                return ExitCode.VALIDATION_ERROR

            # Prepare update data
            update_data = {
                'status': args.status,
                'agent_response': response
            }

            # Add commit hash if provided, otherwise remind user
            if args.commit:
                update_data['commit_hash'] = args.commit
            else:
                print("Commit Hash is empty, if you have committed something, please give commit hash as well", file=sys.stderr)

            # Update the task
            updated = self.storage.update_task(args.id, update_data, operation='mark')
            if updated:
                self._emit_mutation_receipt(args, updated)
                print(f"Task {args.id} marked as {args.status}")
                if args.verbose:
                    print(f"Task {args.id} updated with response and status", file=sys.stderr)
                return ExitCode.SUCCESS
            else:
                print(f"Error: Failed to mark task {args.id}", file=sys.stderr)
                return ExitCode.GENERAL_ERROR

        except Exception as e:
            print(f"Error marking task: {e}", file=sys.stderr)
            return ExitCode.GENERAL_ERROR

    def cmd_umbrella_finalize(self, args: argparse.Namespace) -> int:
        """Apply the explicit ownership/evidence-bound multi-task finalizer."""
        try:
            if not self._validate_project_root():
                return ExitCode.GENERAL_ERROR
            receipt = self.storage.finalize_umbrella(
                Path(args.admission_receipt), Path(args.evidence_receipt), args.commit,
                expected_umbrella_id=args.id)
            payload = json.dumps(receipt, ensure_ascii=False, sort_keys=True) + '\n'
            if args.receipt_file == '-':
                print(payload, end='')
            else:
                path = Path(args.receipt_file)
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
                temporary.write_text(payload, encoding='utf-8')
                os.replace(temporary, path)
            print(f"Umbrella {args.id} finalized with {len(receipt['child_ids'])} admitted children",
                  file=sys.stderr)
            return ExitCode.SUCCESS
        except Exception as exc:
            print(f"Error finalizing umbrella: {exc}", file=sys.stderr)
            return ExitCode.GENERAL_ERROR

    def cmd_merge(self, args: argparse.Namespace) -> int:
        """Handle merge command."""
        try:
            # Initialize merger
            merger = TaskMerger(self.config)

            # Determine source paths
            if args.find_all:
                # Auto-discover .juno_task directories
                current_dir = str(Path.cwd())
                source_paths = merger.find_juno_task_directories(current_dir)
                if not source_paths:
                    print("No .juno_task directories found under current directory", file=sys.stderr)
                    return ExitCode.GENERAL_ERROR

                print(f"Found {len(source_paths)} .juno_task directories:")
                for path in source_paths:
                    print(f"  - {path}")
                print()
            else:
                # Use explicitly provided sources
                if not args.sources:
                    print("Error: No source paths provided. Use source paths or --find-all", file=sys.stderr)
                    return ExitCode.INVALID_USAGE

                source_paths = args.sources

            # Validate source paths
            for source_path in source_paths:
                if not os.path.exists(source_path):
                    print(f"Error: Source path does not exist: {source_path}", file=sys.stderr)
                    return ExitCode.IO_ERROR

                tasks_dir = os.path.join(source_path, 'tasks')
                if not os.path.exists(tasks_dir):
                    print(f"Error: Source path is not a valid .juno_task directory: {source_path}", file=sys.stderr)
                    return ExitCode.IO_ERROR

            # Validate target path
            target_path = args.into
            if os.path.exists(target_path) and not os.path.isdir(target_path):
                print(f"Error: Target path exists but is not a directory: {target_path}", file=sys.stderr)
                return ExitCode.IO_ERROR

            # Perform merge
            print(f"Merging {len(source_paths)} source(s) into {target_path}")
            print(f"Strategy: {args.strategy}")
            if args.dry_run:
                print("DRY RUN - No files will be modified")
            print()

            result = merger.merge_files(
                source_paths=source_paths,
                target_path=target_path,
                strategy=args.strategy,
                dry_run=args.dry_run,
                plan_file=args.plan_file,
                apply_plan=args.apply_plan,
                receipt_file=args.receipt_file,
            )

            if result['success']:
                stats = result['statistics']
                print("MERGE RESULTS:")
                print(f"  Total sources processed: {stats['total_sources']}")
                print(f"  Conflicts found: {stats['conflicts_found']}")
                print(f"  Conflicts resolved: {stats['conflicts_resolved']}")
                print(f"  New tasks added: {stats['tasks_added']}")
                print(f"  Existing tasks kept: {stats['tasks_kept']}")
                print(f"  Final task count: {stats['final_task_count']}")

                if result['conflicts']:
                    print("\nCONFLICTS RESOLVED:")
                    for conflict in result['conflicts']:
                        print(f"  - {conflict}")

                if args.dry_run:
                    print(f"  Plan: {result['plan']['path']}")
                    print(f"  Plan SHA-256: {result['plan']['sha256']}")
                    print(f"  Planned changed paths: {len(result['plan']['changed_paths'])}")
                    print("\nDRY RUN COMPLETE - No files were modified")
                    print("Review the plan, then apply with --apply-plan and --receipt-file")
                else:
                    print(f"  Receipt: {result['receipt']['path']}")
                    print(f"  Receipt SHA-256: {result['receipt']['sha256']}")
                    print(f"\nMerge completed successfully!")
                    print(f"Merged tasks are now available in: {target_path}")

                return ExitCode.SUCCESS
            else:
                print("Merge failed", file=sys.stderr)
                return ExitCode.GENERAL_ERROR

        except Exception as e:
            print(f"Error during merge: {e}", file=sys.stderr)
            return ExitCode.GENERAL_ERROR

    def cmd_deps(self, args: argparse.Namespace) -> int:
        """Handle deps command — show, add, or remove dependencies."""
        try:
            action_or_id = args.action_or_id

            # Infer action when action_or_id is omitted (flag-only syntax)
            # e.g. deps --ID X --blocked-by Y  → defaults to "add"
            # e.g. deps --ID X                 → defaults to "show"
            if action_or_id is None:
                if args.task_id and args.blocked_by:
                    action_or_id = 'add'
                elif args.task_id:
                    # --ID provided without --blocked-by → show dependency info
                    action_or_id = args.task_id
                    args.task_id = None  # consumed as the show target
                else:
                    cn = self._get_command_name()
                    print("Error: deps requires a task ID or action.", file=sys.stderr)
                    print(f"\nDeps usage:", file=sys.stderr)
                    print(f"  {cn} deps TASK_ID                                  Show blockers, dependents, priority score", file=sys.stderr)
                    print(f"  {cn} deps add --id TASK_ID --blocked-by ID...      Add dependency (cycle-checked)", file=sys.stderr)
                    print(f"  {cn} deps remove --id TASK_ID --blocked-by ID...   Remove dependency", file=sys.stderr)
                    print(f"  {cn} deps --id TASK_ID --blocked-by ID...          Shorthand for add", file=sys.stderr)
                    return ExitCode.INVALID_USAGE

            if action_or_id == 'add':
                return self._deps_add(args)
            elif action_or_id == 'remove':
                return self._deps_remove(args)
            else:
                # Show dependency info for a task
                task_id = action_or_id

                task = self.search.search_by_id(task_id)
                if not task:
                    print(f"Task not found: {task_id}", file=sys.stderr)
                    return ExitCode.GENERAL_ERROR

                dependency = self.storage.dependency_info(task_id)
                unmet_blockers = [{'id': bid, 'status': status or 'unknown'}
                                  for bid, status in dependency['blockers']
                                  if status not in ('done', 'archive')]
                met_blockers = [{'id': bid, 'status': status}
                                for bid, status in dependency['blockers']
                                if status in ('done', 'archive')]
                info = {
                    'task_id': task_id,
                    'is_blocked': bool(unmet_blockers),
                    'unmet_blockers': unmet_blockers,
                    'met_blockers': met_blockers,
                    'dependents': dependency['dependents'],
                    'priority_score': dependency['priority_score']
                }

                output = self._format_output([info], args)
                print(output)
                return ExitCode.SUCCESS

        except Exception as e:
            print(f"Error querying dependencies: {e}", file=sys.stderr)
            return ExitCode.GENERAL_ERROR

    def _deps_add(self, args: argparse.Namespace) -> int:
        """Add dependencies to a task."""
        try:
            if not self._validate_project_root():
                return ExitCode.GENERAL_ERROR

            task_id = args.task_id
            task = self.search.search_by_id(task_id)
            if not task:
                print(f"Task not found: {task_id}", file=sys.stderr)
                return ExitCode.GENERAL_ERROR

            new_blockers = self._parse_comma_separated_values(args.blocked_by)
            if not new_blockers:
                print("Error: --blocked-by required", file=sys.stderr)
                return ExitCode.INVALID_USAGE

            # Self-dependency check
            if task_id in new_blockers:
                print(f"Error: Task cannot be blocked by itself: {task_id}", file=sys.stderr)
                return ExitCode.VALIDATION_ERROR

            # Split into existing and forward-reference IDs (not yet in kanban)
            valid_ids, invalid_ids = validate_task_ids(new_blockers, self.storage)
            for invalid_id in invalid_ids:
                print(f"Warning: {invalid_id} wasn't found on the kanban (stored as forward reference).", file=sys.stderr)

            # Merge ALL provided IDs (including forward references) with existing blocked_by
            existing = task.get('blocked_by') or []
            merged = list(existing)
            for bid in new_blockers:
                if bid not in merged:
                    merged.append(bid)

            # Cycle detection on existing IDs only, using the indexed graph.
            for blocker_id in valid_ids:
                if self.storage.dependency_would_cycle(task_id, blocker_id):
                    print(f"Error: Dependency cycle detected: {task_id} → {blocker_id} → {task_id}", file=sys.stderr)
                    return ExitCode.VALIDATION_ERROR

            # Save
            self.storage.update_task(task_id, {'blocked_by': merged})
            updated = self.search.search_by_id(task_id)
            output = self._format_output([updated], args)
            print(output)
            return ExitCode.SUCCESS

        except Exception as e:
            print(f"Error adding dependency: {e}", file=sys.stderr)
            return ExitCode.GENERAL_ERROR

    def _deps_remove(self, args: argparse.Namespace) -> int:
        """Remove dependencies from a task."""
        try:
            if not self._validate_project_root():
                return ExitCode.GENERAL_ERROR

            task_id = args.task_id
            task = self.search.search_by_id(task_id)
            if not task:
                print(f"Task not found: {task_id}", file=sys.stderr)
                return ExitCode.GENERAL_ERROR

            to_remove = self._parse_comma_separated_values(args.blocked_by)
            if not to_remove:
                print("Error: --blocked-by required", file=sys.stderr)
                return ExitCode.INVALID_USAGE

            existing = task.get('blocked_by') or []
            updated_blocked = [bid for bid in existing if bid not in to_remove]

            self.storage.update_task(task_id, {'blocked_by': updated_blocked})
            updated = self.search.search_by_id(task_id)
            output = self._format_output([updated], args)
            print(output)
            return ExitCode.SUCCESS

        except Exception as e:
            print(f"Error removing dependency: {e}", file=sys.stderr)
            return ExitCode.GENERAL_ERROR

    def cmd_ready(self, args: argparse.Namespace) -> int:
        """Handle ready through the indexed dependency plan."""
        try:
            return self._cmd_indexed_collection(args, "ready")
            all_tasks = self._load_all_tasks_for_graph()
            graph = DependencyGraph(all_tasks)
            ready_ids = graph.get_ready_tasks()

            # Build result list from ready IDs
            all_ready_results = []
            for task_id in ready_ids:
                task = self.search.search_by_id(task_id)
                if task:
                    all_ready_results.append(task)

            # Filter by tag if specified
            tag_filter = self._parse_comma_separated_values(getattr(args, 'tag', None))
            if tag_filter:
                filtered = []
                for task in all_ready_results:
                    task_tags = task.get('feature_tags') or []
                    if any(t in task_tags for t in tag_filter):
                        filtered.append(task)
                all_ready_results = filtered

            # Optional status filtering for ready set (space/comma separated)
            status_filter = self._parse_comma_separated_values(getattr(args, 'status', None))
            if status_filter:
                allowed_statuses = set(status_filter)
                all_ready_results = [
                    task for task in all_ready_results if task.get('status') in allowed_statuses
                ]

            # Apply shared sort-order contract by last_modified and honor explicit
            # status-order requests when --status is provided.
            sort_order = getattr(args, 'sort', 'desc')
            if status_filter:
                all_ready_results = self.search.sort_tasks_by_status_sequence(
                    all_ready_results,
                    status_filter,
                    sort_order,
                )
            else:
                all_ready_results = self.search.sort_tasks_by_last_modified(all_ready_results, sort_order)

            if not all_ready_results:
                print("No ready tasks found")
                return ExitCode.SUCCESS

            # Apply offset/limit for display output only (summary should reflect full ready set).
            # Why: ready pagination should match list pagination by slicing only after graph
            # readiness filtering and shared sort/status-order rules have produced final order.
            offset = self._validate_pagination_args(args)
            if offset is None:
                return ExitCode.INVALID_USAGE
            display_results = self._paginate_results(all_ready_results, offset, args.limit)

            # Determine output format (command-specific or global)
            ready_fmt = getattr(args, 'ready_format', None)
            if ready_fmt:
                args.format = ready_fmt

            # Handle --raw flag from ready subparser
            if getattr(args, 'ready_raw', False):
                args.raw = True

            output = self._format_output(self._project_broad(display_results, args), args)
            print(output)

            # Show summary/count context (parity with list/search contracts)
            effective_offset = next((i for i, task in enumerate(all_ready_results)
                                     if display_results and task.get('id') == display_results[0].get('id')), offset)
            self._set_next_cursor(args, effective_offset, display_results, len(all_ready_results))
            self._show_summary_stats(all_ready_results, args, displayed_count=len(display_results))
            return ExitCode.SUCCESS

        except Exception as e:
            print(f"Error listing ready tasks: {e}", file=sys.stderr)
            return ExitCode.GENERAL_ERROR

    def cmd_order(self, args: argparse.Namespace) -> int:
        """Handle order command — show execution order via topological sort."""
        try:
            all_tasks = self._load_all_tasks_for_graph()

            # Filter to only open tasks for ordering
            open_tasks = [t for t in all_tasks if t.get('status') in ('backlog', 'todo', 'in_progress')]

            if not open_tasks:
                print("No open tasks to order")
                return ExitCode.SUCCESS

            # Include resolved tasks so the graph can distinguish satisfied
            # historical edges from genuinely missing dependencies. Only open
            # tasks are projected to the user below.
            graph = DependencyGraph(all_tasks)
            score_graph = DependencyGraph(open_tasks)

            try:
                order = graph.topological_sort()
            except ValueError as e:
                print(f"Error: {e}", file=sys.stderr)
                return ExitCode.GENERAL_ERROR

            include_scores = getattr(args, 'scores', False)

            # Build result list in topological order
            results = []
            open_task_ids = {task['id'] for task in open_tasks}
            for task_id in order:
                if task_id not in open_task_ids:
                    continue
                task = self.search.search_by_id(task_id)
                if task:
                    if include_scores:
                        task_copy = task.copy()
                        task_copy['_priority_score'] = score_graph.get_priority_score(task_id)
                        results.append(task_copy)
                    else:
                        results.append(task)

            if not results:
                print("No open tasks to order")
                return ExitCode.SUCCESS

            # Determine output format
            order_fmt = getattr(args, 'order_format', None)
            if order_fmt:
                args.format = order_fmt

            output = self._format_output(self._project_broad(results, args), args)
            print(output)
            return ExitCode.SUCCESS

        except Exception as e:
            print(f"Error computing execution order: {e}", file=sys.stderr)
            return ExitCode.GENERAL_ERROR

    def _get_completion_subparsers_action(self) -> Optional[argparse._SubParsersAction]:
        """Return the argparse subparsers action from the root parser."""
        for action in self.parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                return action
        return None

    def _get_completion_status_values(self) -> List[str]:
        """Get status values for completion without requiring runtime config loading."""
        return list(Config.DEFAULT_CONFIG.get('status_workflow', {}).get('values', [
            'backlog', 'todo', 'in_progress', 'done', 'archive'
        ]))

    def _get_completion_metadata(self) -> Tuple[List[str], List[str], Dict[str, Dict[str, Any]]]:
        """Build completion metadata from argparse parser definitions (single source of truth)."""
        subparsers_action = self._get_completion_subparsers_action()
        if not subparsers_action:
            return [], [], {}

        command_names = sorted(
            [name for name in subparsers_action.choices.keys() if name != '__complete']
        )

        global_options: Set[str] = set()
        for action in self.parser._actions:
            if action.option_strings:
                global_options.update(action.option_strings)

        metadata: Dict[str, Dict[str, Any]] = {}

        for command_name, command_parser in subparsers_action.choices.items():
            if command_name == '__complete':
                continue

            command_options: Set[str] = set()
            option_values: Dict[str, List[str]] = {}
            positional_args: List[str] = []
            nested_actions: Dict[str, Dict[str, Any]] = {}

            for action in command_parser._actions:
                if isinstance(action, argparse._HelpAction):
                    continue

                if action.option_strings:
                    command_options.update(action.option_strings)

                    if action.choices:
                        values = [str(choice) for choice in action.choices]
                        for option in action.option_strings:
                            option_values[option] = values

                    if any(option in ('--status',) for option in action.option_strings):
                        status_values = self._get_completion_status_values()
                        for option in action.option_strings:
                            if option == '--status':
                                option_values[option] = status_values
                else:
                    positional_args.append(action.dest)

                    if action.choices:
                        option_values[action.dest] = [str(choice) for choice in action.choices]
                    if isinstance(action, argparse._SubParsersAction):
                        for nested_name, nested_parser in action.choices.items():
                            nested_options: Set[str] = set()
                            nested_values: Dict[str, List[str]] = {}
                            for nested_action in nested_parser._actions:
                                if nested_action.option_strings:
                                    nested_options.update(nested_action.option_strings)
                                    if nested_action.choices:
                                        values = [str(choice) for choice in nested_action.choices]
                                        for option in nested_action.option_strings:
                                            nested_values[option] = values
                            nested_actions[nested_name] = {
                                'options': sorted(nested_options),
                                'option_values': nested_values,
                            }

            if command_name == 'mark':
                option_values['status'] = self._get_completion_status_values()
            elif command_name == 'deps':
                option_values['action_or_id'] = ['add', 'remove']

            metadata[command_name] = {
                'options': sorted(command_options),
                'option_values': option_values,
                'positionals': positional_args,
                'nested_actions': nested_actions,
            }

        return command_names, sorted(global_options), metadata

    def _filter_completion_candidates(self, candidates: List[str], prefix: str) -> List[str]:
        """Filter and dedupe completion candidates based on prefix."""
        seen = set()
        filtered: List[str] = []

        for candidate in candidates:
            if not candidate:
                continue
            if prefix and not candidate.startswith(prefix):
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            filtered.append(candidate)

        return filtered

    def _suggest_option_values(self, command_name: str, option_token: str, metadata: Dict[str, Dict[str, Any]]) -> List[str]:
        """Suggest values for option tokens when applicable."""
        command_meta = metadata.get(command_name, {})
        option_values = command_meta.get('option_values', {})

        if option_token in option_values:
            return option_values[option_token]

        if option_token in ('-f', '--format'):
            return ['ndjson', 'json', 'xml', 'table']

        return []

    def _compute_completion_candidates(self, words: List[str], cursor_index: int) -> List[str]:
        """Compute completion candidates from parser metadata and cursor state."""
        command_names, global_options, metadata = self._get_completion_metadata()
        if not words:
            return command_names

        safe_index = max(0, min(cursor_index, len(words) - 1))
        current = words[safe_index] if words else ''
        tokens_before = words[1:safe_index]

        selected_command = None
        selected_command_index = None

        for index, token in enumerate(tokens_before, start=1):
            if token in command_names:
                selected_command = token
                selected_command_index = index
                break

        if not selected_command:
            base = global_options if current.startswith('-') else command_names + global_options
            return self._filter_completion_candidates(base, current)

        # command-specific completion
        command_meta = metadata.get(selected_command, {})
        command_options = command_meta.get('options', [])
        command_positionals = command_meta.get('positionals', [])

        previous_token = words[safe_index - 1] if safe_index > 0 else ''

        if previous_token.startswith('-'):
            option_value_candidates = self._suggest_option_values(selected_command, previous_token, metadata)
            if option_value_candidates:
                return self._filter_completion_candidates(option_value_candidates, current)

        command_tokens_before_cursor = words[(selected_command_index + 1):safe_index] if selected_command_index is not None else []
        nested_meta = command_meta.get('nested_actions', {}).get(
            command_tokens_before_cursor[0] if command_tokens_before_cursor else '')
        if nested_meta:
            if previous_token in nested_meta.get('option_values', {}):
                return self._filter_completion_candidates(
                    nested_meta['option_values'][previous_token], current)
            return self._filter_completion_candidates(
                nested_meta.get('options', []) + global_options, current)
        positional_index = len([token for token in command_tokens_before_cursor if token and not token.startswith('-')])

        if selected_command == 'mark' and positional_index == 0 and not current.startswith('-'):
            return self._filter_completion_candidates(self._get_completion_status_values(), current)

        if selected_command == 'deps' and positional_index == 0 and not current.startswith('-'):
            return self._filter_completion_candidates(['add', 'remove'], current)

        if selected_command == 'project' and positional_index == 0 and not current.startswith('-'):
            return self._filter_completion_candidates(['add', 'list', 'show', 'remove', 'status'], current)

        if selected_command == 'completion' and positional_index == 0 and not current.startswith('-'):
            return self._filter_completion_candidates(['bash', 'zsh', 'fish'], current)

        positional_dest = command_positionals[positional_index] if positional_index < len(command_positionals) else None
        if positional_dest:
            positional_values = command_meta.get('option_values', {}).get(positional_dest, [])
            if positional_values and not current.startswith('-'):
                return self._filter_completion_candidates(positional_values, current)

        candidates = command_options + global_options if current.startswith('-') else command_options + global_options
        return self._filter_completion_candidates(candidates, current)

    def _generate_bash_completion_script(self) -> str:
        """Generate bash completion script."""
        command_name = self._get_command_name()
        command_names = ' '.join(CONSOLE_COMMAND_NAMES)

        return f'''# bash completion for {command_name}
_{command_name.replace('-', '_')}_completion() {{
  local cur
  cur="${{COMP_WORDS[COMP_CWORD]}}"

  local suggestions
  suggestions=$(\
    {command_name} __complete --index "$COMP_CWORD" -- "${{COMP_WORDS[@]}}" 2>/dev/null
  )

  COMPREPLY=($(compgen -W "$suggestions" -- "$cur"))
}}

complete -o default -F _{command_name.replace('-', '_')}_completion {command_names}
'''

    def _generate_zsh_completion_script(self) -> str:
        """Generate zsh completion script via bashcompinit for portability."""
        command_name = self._get_command_name()
        command_names = ' '.join(CONSOLE_COMMAND_NAMES)

        return f'''#compdef {command_names}
# zsh completion for {command_name}
autoload -U +X bashcompinit && bashcompinit
_{command_name.replace('-', '_')}_completion() {{
  local cur
  cur="${{COMP_WORDS[COMP_CWORD]}}"

  local suggestions
  suggestions=$(\
    {command_name} __complete --index "$COMP_CWORD" -- "${{COMP_WORDS[@]}}" 2>/dev/null
  )

  COMPREPLY=($(compgen -W "$suggestions" -- "$cur"))
}}

complete -o default -F _{command_name.replace('-', '_')}_completion {command_names}
'''

    def _generate_fish_completion_script(self) -> str:
        """Generate fish completion script."""
        command_name = self._get_command_name()
        completions = '\n'.join(
            f'complete -c {name} -f -a "(__{command_name.replace("-", "_")}_complete)"'
            for name in CONSOLE_COMMAND_NAMES
        )

        return f'''# fish completion for {command_name}
function __{command_name.replace('-', '_')}_complete
    set -l tokens (commandline -opc)
    set -l cur (commandline -ct)
    set -l idx (count $tokens)

    {command_name} __complete --index $idx -- $tokens "$cur" 2>/dev/null
end

{completions}
'''

    def cmd_history(self, args: argparse.Namespace) -> int:
        try:
            events = self.storage.history(args.id, args.include_content, args.limit)
            print(OutputFormatter.format_tasks(events, self._get_output_format(args), args.pretty))
            return ExitCode.SUCCESS
        except Exception as exc:
            print(f"Error reading history: {exc}", file=sys.stderr)
            return ExitCode.GENERAL_ERROR

    def cmd_reconcile(self, args: argparse.Namespace) -> int:
        try:
            changed = self.storage.reconcile(check=args.check)
            print(json.dumps({'changed_task_ids': changed, 'check': args.check}, ensure_ascii=False))
            return ExitCode.VALIDATION_ERROR if args.check and changed else ExitCode.SUCCESS
        except Exception as exc:
            print(f"Error reconciling tasks: {exc}", file=sys.stderr)
            return ExitCode.GENERAL_ERROR

    def cmd_doctor(self, args: argparse.Namespace) -> int:
        failures = self.storage.doctor()
        print(json.dumps({'ok': not failures, 'failures': failures}, ensure_ascii=False))
        return ExitCode.SUCCESS if not failures else ExitCode.VALIDATION_ERROR

    def cmd_cache(self, args: argparse.Namespace) -> int:
        try:
            count = self.storage.rebuild_cache()
            print(json.dumps({'rebuilt_tasks': count, 'path': str(self.storage.cache.path)}))
            return ExitCode.SUCCESS
        except Exception as exc:
            print(f"Error rebuilding cache: {exc}", file=sys.stderr)
            return ExitCode.GENERAL_ERROR

    @staticmethod
    def _write_report(path: Optional[str], report: Dict[str, Any]):
        if path:
            Path(path).write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')

    def cmd_convert(self, args: argparse.Namespace) -> int:
        try:
            report = self.storage.convert_legacy(
                Path(args.source), dry_run=args.dry_run,
                pre_cutover_tag=args.pre_cutover_tag,
                backup_path=Path(args.backup_path) if args.backup_path else None,
                legacy_package=Path(args.legacy_package) if args.legacy_package else None,
                new_package_version=args.new_package_version,
                benchmark_receipt=Path(args.benchmark_receipt) if args.benchmark_receipt else None,
                report_path=Path(args.report),
            )
            print(json.dumps(report, ensure_ascii=False))
            return ExitCode.SUCCESS
        except Exception as exc:
            print(f"Error converting legacy storage: {exc}", file=sys.stderr)
            return ExitCode.VALIDATION_ERROR

    def cmd_compatibility(self, args: argparse.Namespace) -> int:
        try:
            if args.action == 'accept':
                if not args.report or args.acceptance_receipt:
                    raise ValueError('compatibility accept requires --report and does not accept caller-authored receipts')
                evidence = {key: Path(value) for key, value in self._parse_key_values(args.evidence).items()}
                report = self.storage.generate_acceptance_receipt(evidence, Path(args.report))
            else:
                if not args.acceptance_receipt:
                    raise ValueError('compatibility lift requires --acceptance-receipt')
                report = self.storage.lift_compatibility_window(Path(args.acceptance_receipt))
                self._write_report(args.report, report)
            print(json.dumps(report, ensure_ascii=False))
            return ExitCode.SUCCESS
        except Exception as exc:
            print(f"Error lifting compatibility window: {exc}", file=sys.stderr)
            return ExitCode.VALIDATION_ERROR

    def cmd_export_legacy(self, args: argparse.Namespace) -> int:
        try:
            report = self.storage.export_legacy(Path(args.destination))
            self._write_report(args.report, report)
            print(json.dumps(report, ensure_ascii=False))
            return ExitCode.SUCCESS
        except Exception as exc:
            print(f"Error exporting legacy storage: {exc}", file=sys.stderr)
            return ExitCode.VALIDATION_ERROR

    def cmd_rollback(self, args: argparse.Namespace) -> int:
        try:
            if args.mode == 'immediate':
                if not args.conversion_receipt:
                    raise ValueError('immediate rollback requires --conversion-receipt')
                report = self.storage.immediate_rollback(
                    Path(args.conversion_receipt), Path(args.report))
            else:
                if not args.legacy_wheel or not args.legacy_runtime_dir or not args.archive:
                    raise ValueError('post-write rollback requires --legacy-wheel, --legacy-runtime-dir, and --archive')
                report = self.storage.execute_post_write_rollback(
                    Path(args.legacy_wheel), Path(args.legacy_runtime_dir),
                    Path(args.archive), Path(args.report))
            print(json.dumps(report, ensure_ascii=False))
            return ExitCode.SUCCESS
        except Exception as exc:
            print(f"Error executing rollback: {exc}", file=sys.stderr)
            return ExitCode.VALIDATION_ERROR

    def cmd_completion(self, args: argparse.Namespace) -> int:
        """Handle completion command: print shell completion script."""
        shell = getattr(args, 'shell', 'bash')

        if shell == 'bash':
            print(self._generate_bash_completion_script())
        elif shell == 'zsh':
            print(self._generate_zsh_completion_script())
        elif shell == 'fish':
            print(self._generate_fish_completion_script())
        else:
            print(f"Unsupported shell: {shell}", file=sys.stderr)
            return ExitCode.INVALID_USAGE

        return ExitCode.SUCCESS

    def cmd_internal_complete(self, args: argparse.Namespace) -> int:
        """Internal completion candidate command for shell scripts."""
        words = list(getattr(args, 'words', []) or [])
        if not words:
            return ExitCode.SUCCESS

        index = getattr(args, 'index', 0)
        suggestions = self._compute_completion_candidates(words, index)

        if suggestions:
            print('\n'.join(suggestions))

        return ExitCode.SUCCESS

    def show_help(self, args: argparse.Namespace) -> int:
        """Show help text optimized for LLM token efficiency — schema-driven, no fluff."""
        cn = self._get_command_name()

        # Task schema — LLMs need to know field names to construct queries
        print(f"{cn} COMMAND [OPTIONS]")
        print()
        print("TASK SCHEMA:")
        print("  {id, status, body, commit_hash, agent_response, created_date, last_modified, feature_tags[], related_tasks[], blocked_by[]}")
        available_statuses = ', '.join(self.config.status_values) if self.config and hasattr(self.config, 'status_values') else 'backlog, todo, in_progress, done, archive'
        print(f"  Statuses: {available_statuses}")
        print("  IDs: 6-char alphanumeric (auto-generated)")
        print()

        # Command signatures — show exact syntax, required vs optional
        print("COMMANDS:")
        print(f"  {cn} record|task|wiki|workflow|artifact ACTION ...      Native ID-first Record API v2 (no remove)")
        print(f"      ACTION: create|list|search|get|update|history|archive (where profile permits)")
        print(f"  {cn} migration inventory|plan|apply|status|verify ...    Copy legacy files into Records; never delete sources")
        print(f"      Flat task commands below are the versioned legacy v1 compatibility surface")
        print(f"  {cn} --project ALIAS COMMAND ...                       Route through an allowed destination wrapper")
        print(f"  {cn} project add ALIAS --path PATH [--replace]          Register an initialized project")
        print(f"  {cn} project list|show|remove|status                    Manage the opt-in user registry")
        print(f"  {cn} create  BODY | --body TEXT | --body-file PATH|- [--status S] [--tags T...] [--commit H] [--related-tasks ID...] [--blocked-by ID...] [--reject-duplicates|--no-duplicate|--discard-duplicates]")
        print(f"  {cn} get     TASK_ID [TASK_ID ...] | --ID TASK_ID [--compact]")
        print(f"  {cn} update  TASK_ID | --ID TASK_ID [--status S] [--body TEXT|--body-file PATH|-] [--response TEXT|--response-file PATH|-] [--commit H] [--tags T...] [--blocked-by ID...]")
        print(f"  {cn} mark    STATUS TASK_ID | --ID TASK_ID (--response TEXT|--response-file PATH|-) [--commit H]")
        print(f"  {cn} archive TASK_ID | --ID TASK_ID")
        print(f"  {cn} list    [--status S...] [--tag T...] [--exclude T...] [--limit N] [--offset N] [--sort asc|desc] [--open] [--recent] [-f FMT]")
        print(f"  {cn} search  [--id ID] [--status S...] [--tag T...] [--exclude T...] [--body TEXT] [--response TEXT] [--commit H] [--open] [--recent] [--limit N] [--sort asc|desc]")
        print(f"  {cn} tags    [--status S...] [-f FMT]                    Tag counts (supports comma/space status filters)")
        print(f"  {cn} deps    TASK_ID                                      Show blockers, dependents, priority score")
        print(f"  {cn} deps    add --id TASK_ID --blocked-by ID...          Add dependency (cycle-checked)")
        print(f"  {cn} deps    remove --id TASK_ID --blocked-by ID...       Remove dependency")
        print(f"  {cn} ready   [--tag T...] [--status S...] [--limit N] [--offset N] [--sort asc|desc] [-f FMT]  Unblocked tasks (status order follows --status when provided)")
        print(f"  {cn} order   [--scores] [-f FMT]                          Topological sort of open tasks")
        print(f"  {cn} merge   SOURCES --into TARGET --dry-run --plan-file PLAN")
        print(f"  {cn} merge   SOURCES --into TARGET --apply-plan PLAN --receipt-file RECEIPT")
        print(f"  {cn} completion [bash|zsh|fish]                     Print shell completion script")
        print()

        # Global options
        print("GLOBAL OPTIONS:")
        print("  -f FMT    Output: ndjson (default) | json | xml | table")
        print("  --raw     Compact output for scripting")
        print("  -p        Pretty-print json/xml")
        print("  -c PATH   Config file (default: .juno_task/tasks/config.json)")
        print("  --project ALIAS   Route through an enabled and source-allowed registry alias")
        print()

        # Body markup — critical for correct task creation
        print("BODY MARKUP (auto-parsed on create):")
        print("  [task_id]ID1, ID2[/task_id]         -> related_tasks")
        print("  ## ID1 ID2 ##  or  ##ID1            -> related_tasks")
        print("  [blocked_by]ID1, ID2[/blocked_by]   -> blocked_by  (synonyms: block_by, block, parent_task)")
        print()

        # Dependency model — essential for understanding ready/order/deps
        print("DEPENDENCY MODEL:")
        print("  blocked_by: list of task IDs that must complete (done/archive) before this task")
        print("  ready:  tasks with status in [backlog, todo, in_progress] + all blockers resolved (optional --status filter keeps provided status order)")
        print("  order:  topological sort respecting blocked_by — safe parallel execution order")
        print("  deps:   query blockers/dependents/priority-score for any task")
        print()

        # Environment variables
        print("ENVIRONMENT:")
        print("  JUNO_TASK_ROOT=PATH   Pin .juno_task directory to PATH (overrides PWD-based search)")
        print("                        Prevents scattered ndjson files when running from different dirs")
        print("  YYLO_LEDGER_REGISTRY_ENABLED=true|false")
        print("  YYLO_LEDGER_REGISTRY_ALLOWED_PROJECTS=alias,alias")
        print("  YYLO_LEDGER_REGISTRY_PATH=PATH   Override the user registry path")
        print()

        # Stdin support
        print("STDIN: echo \"body\" | " + cn + " create [--tags T...]")
        print("       " + cn + " create --body-file path.md   # preserves shell-sensitive markdown")
        print("       " + cn + " create --body-file -         # stdin path for rich markdown/backticks/$()")
        print("       " + cn + " mark done --id ID --response-file response.md")
        print()

        print(f"Per-command help: {cn} COMMAND --help")
        return ExitCode.SUCCESS

    def _print_error_hints(self, args: List[str]) -> None:
        """Print helpful hints after an argparse error, based on which command was attempted."""
        cn = self._get_command_name()

        # Detect the command from args (first non-flag argument)
        command = None
        for arg in args:
            if not arg.startswith('-'):
                command = arg
                break

        print(file=sys.stderr)

        if command == 'deps':
            print("Deps usage:", file=sys.stderr)
            print(f"  {cn} deps TASK_ID                                  Show blockers, dependents, priority score", file=sys.stderr)
            print(f"  {cn} deps add --id TASK_ID --blocked-by ID...      Add dependency (cycle-checked)", file=sys.stderr)
            print(f"  {cn} deps remove --id TASK_ID --blocked-by ID...   Remove dependency", file=sys.stderr)
            print(f"  {cn} deps --id TASK_ID --blocked-by ID...          Shorthand for add", file=sys.stderr)
        elif command == 'mark':
            print("Mark usage:", file=sys.stderr)
            print(f"  {cn} mark STATUS TASK_ID --response \"message\"", file=sys.stderr)
            print(f"  {cn} mark STATUS --id TASK_ID --response \"message\"", file=sys.stderr)
            print(f"  {cn} mark STATUS --id TASK_ID --response-file response.md", file=sys.stderr)
            print(f"  Example: {cn} mark done ABC123 --response \"task completed\"", file=sys.stderr)
        elif command == 'create':
            print("Create usage:", file=sys.stderr)
            print(f"  {cn} create \"task body\" [--status S] [--tags T...] [--blocked-by ID...] [--reject-duplicates|--no-duplicate|--discard-duplicates]", file=sys.stderr)
            print(f"  {cn} create --title \"title\" --body \"body\" [--status S] [--tags T...]", file=sys.stderr)
            print(f"  {cn} create --body-file body.md [--status S] [--tags T...]", file=sys.stderr)
        elif command == 'update':
            print("Update usage:", file=sys.stderr)
            print(f"  {cn} update TASK_ID [--status S] [--body TEXT|--body-file body.md] [--response TEXT|--response-file response.md] [--commit H] [--tags T...]", file=sys.stderr)
            print(f"  {cn} update --id TASK_ID [--status S] [--body TEXT|--body-file body.md] [--response TEXT|--response-file response.md] [--commit H] [--tags T...]", file=sys.stderr)
        elif command == 'list':
            print("List usage:", file=sys.stderr)
            print(f"  {cn} list [--status S...] [--tag T...] [--limit N] [--sort asc|desc]", file=sys.stderr)
        elif command == 'tags':
            print("Tags usage:", file=sys.stderr)
            print(f"  {cn} tags [--status S...] [--format ndjson|json|xml|table]", file=sys.stderr)
        elif command == 'merge':
            print("Merge usage:", file=sys.stderr)
            print(f"  {cn} merge SOURCE... --into TARGET --dry-run --plan-file PLAN [--strategy keep-newer|keep-both]", file=sys.stderr)
            print(f"  {cn} merge SOURCE... --into TARGET --apply-plan PLAN --receipt-file RECEIPT [--strategy keep-newer|keep-both]", file=sys.stderr)
        else:
            print(f"Run '{cn} --help' to see all commands and usage examples.", file=sys.stderr)
            return

        print(f"\nRun '{cn} {command} --help' for detailed options.", file=sys.stderr)

    def _read_stdin_if_available(self) -> Optional[str]:
        """
        Read from stdin if data is available (piped input).

        This checks if stdin has data without blocking, allowing:
        - echo "task body" | yylo-ledger
        - yylo-ledger << 'EOF'
          task body
          EOF
        - cat file.txt | yylo-ledger

        Returns:
            The stdin content stripped of leading/trailing whitespace,
            or None if stdin is a terminal (interactive) or empty.
        """
        import select

        # Check if stdin is a tty (interactive terminal)
        # If it's a tty, we don't want to wait for input
        if sys.stdin.isatty():
            return None

        # For non-tty stdin (piped input), try to read
        try:
            # On Unix-like systems, use select to check if data is available
            # On Windows, this approach may differ, but sys.stdin.isatty() covers most cases
            if hasattr(select, 'select'):
                # Check if stdin has data ready (with 0 timeout = non-blocking)
                readable, _, _ = select.select([sys.stdin], [], [], 0)
                if not readable:
                    # No data available yet, but stdin is not a tty
                    # This could be a pipe that will have data, so read it
                    pass

            # Read all available stdin content
            stdin_content = sys.stdin.read()

            if stdin_content:
                return stdin_content.strip()
            return None

        except Exception:
            # If anything goes wrong reading stdin, fall back to normal behavior
            return None

    def run(self, args: Optional[List[str]] = None) -> int:
        """
        Run the CLI application.

        Args:
            args: Command line arguments (default: sys.argv[1:])

        Returns:
            Exit code
        """
        try:
            # Check for explicit cross-project routing before parsing or reading
            # stdin. Process replacement leaves stdin bytes untouched for the
            # destination wrapper's command-aware transport contract.
            args_to_parse = list(args if args is not None else sys.argv[1:])
            project_alias, args_to_parse = self._extract_project_route(args_to_parse)
            if project_alias is not None:
                route_to_project(project_alias, args_to_parse, source_project_root())
                raise AssertionError('project route unexpectedly returned')

            # Check for stdin input (piped data). Registry management never reads
            # task stdin or initializes local task storage.
            # This supports: echo "task" | yylo-ledger or yylo-ledger << 'EOF'.
            # Do not pre-read stdin when explicit file-input flags use '-' because
            # create/update/mark must read exact content themselves (without strip()).
            stdin_file_flags = ('--body-file', '--response-file', '--file',
                                '--expect-file', '--value-file', '--old-file',
                                '--new-file', '--front-matter-file')
            file_flag_reads_stdin = '--stdin' in args_to_parse or any(
                any(arg == f'{flag}=-' for flag in stdin_file_flags)
                for arg in args_to_parse
            ) or any(
                arg in stdin_file_flags and idx + 1 < len(args_to_parse) and args_to_parse[idx + 1] == '-'
                for idx, arg in enumerate(args_to_parse)
            )
            non_stdin_command = any(value in args_to_parse for value in ('project', 'host'))
            stdin_body = None if file_flag_reads_stdin or non_stdin_command else self._read_stdin_if_available()
            body_from_implicit_stdin = False

            if stdin_body and not args_to_parse:
                # Stdin has content and no args provided
                # Treat as shortcut: equivalent to yylo-ledger "{stdin content}"
                args_to_parse = ['create', stdin_body]
                body_from_implicit_stdin = True
            elif stdin_body and args_to_parse:
                # Stdin has content AND args provided
                # If args is just flags (starts with -) or a command without body,
                # append stdin as the body
                first_arg = args_to_parse[0] if args_to_parse else ''

                if first_arg == 'create':
                    # create command - check if body is already provided
                    has_body_flag = '--body' in args_to_parse
                    # Check if there's a positional body after 'create'
                    has_positional_body = len(args_to_parse) > 1 and not args_to_parse[1].startswith('-')

                    if not has_body_flag and not has_positional_body:
                        # No body provided, use stdin as body
                        # Insert right after 'create' command (position 1), before any flags
                        args_to_parse = ['create', stdin_body] + args_to_parse[1:]
                        body_from_implicit_stdin = True
                elif first_arg.startswith('-') or first_arg not in ['record', *TYPED_GROUPS, 'host', 'project', 'create', 'search', 'get', 'show', 'update', 'list', 'archive', 'archive-pack', 'archive-search', 'mark', 'umbrella-finalize', 'merge', 'deps', 'ready', 'order', 'tags', 'completion', '__complete']:
                    # No command specified or shortcut syntax with flags
                    # Treat stdin as task body for create
                    if first_arg.startswith('-'):
                        # Flags provided: yylo-ledger --tags backend < task.txt
                        args_to_parse = ['create', stdin_body] + args_to_parse
                    else:
                        # Unknown first arg (could be shortcut body ignored, use stdin)
                        args_to_parse = ['create', stdin_body]
                    body_from_implicit_stdin = True

            # Handle 'help' keyword as equivalent to --help / no-command help display
            if args_to_parse and args_to_parse[0] == 'help':
                if len(args_to_parse) == 1:
                    # yylo-ledger help -> show full help (same as yylo-ledger with no args)
                    args_to_parse = []
                else:
                    # yylo-ledger help <command> -> yylo-ledger <command> --help
                    args_to_parse = args_to_parse[1:] + ['--help']

            if args_to_parse and len(args_to_parse) > 0 and not args_to_parse[0].startswith('-'):
                # Check if first argument is not a known command
                known_commands = ['record', *TYPED_GROUPS, 'migration', 'host', 'project', 'create', 'search', 'get', 'show', 'update', 'list', 'archive', 'archive-pack', 'archive-search', 'mark', 'umbrella-finalize', 'merge', 'deps', 'ready', 'order', 'tags', 'history', 'reconcile', 'doctor', 'cache', 'convert', 'compatibility', 'export-legacy', 'rollback', 'completion', '__complete']
                if args_to_parse[0] not in known_commands:
                    # Treat as shortcut: yylo-ledger "task body" -> yylo-ledger create "task body"
                    args_to_parse = ['create'] + args_to_parse

            # Preprocess arguments for case-insensitive handling
            args_to_parse = self._normalize_arguments(args_to_parse)
            pretty_requested = any(arg == '--pretty' or arg == '-p' for arg in args_to_parse)

            try:
                parsed_args = self.parser.parse_args(args_to_parse)
                parsed_args._pretty_requested = pretty_requested
                parsed_args._body_from_stdin = body_from_implicit_stdin
            except SystemExit as e:
                if e.code == 2:
                    # argparse already printed usage + error to stderr
                    # Add helpful hints based on which command was attempted
                    self._print_error_hints(args_to_parse)
                    return ExitCode.INVALID_USAGE
                raise

            # Handle commands that do not require config/storage initialization
            if parsed_args.command == 'project':
                return self.cmd_project(parsed_args)

            if parsed_args.command == 'completion':
                return self.cmd_completion(parsed_args)

            if parsed_args.command == '__complete':
                return self.cmd_internal_complete(parsed_args)

            if parsed_args.command == 'host':
                # Hosting must remain read-only even when pointed at an
                # uninitialized prefix: do not create config, storage, or cache paths.
                from .hosting import HostPolicy, serve
                try:
                    config = Config(parsed_args.config, auto_create=False)
                    storage = TaskStorage(config, create_directories=False)
                    policy = HostPolicy(
                        access=parsed_args.access_policy,
                        allowed_redirect_hosts=tuple(parsed_args.allow_redirect_host),
                        max_output_bytes=parsed_args.max_output_bytes,
                        max_range_bytes=parsed_args.max_range_bytes,
                    )
                    serve(storage, host=parsed_args.host, port=parsed_args.port, policy=policy)
                    return ExitCode.SUCCESS
                except (ConfigError, OSError, ValueError) as exc:
                    print(f"Hosting error: {exc}", file=sys.stderr)
                    return ExitCode.CONFIG_ERROR

            # Initialize components with config path
            self._init_components(parsed_args.config)

            # Handle commands
            if not parsed_args.command:
                # No command specified - show help
                return self.show_help(parsed_args)

            elif parsed_args.command == 'record' or parsed_args.command in TYPED_GROUPS:
                return RecordCLI(self).run(parsed_args)

            elif parsed_args.command == 'migration':
                return MigrationCLI(self).run(parsed_args)

            elif parsed_args.command == 'create':
                return self.cmd_create(parsed_args)

            elif parsed_args.command == 'search':
                return self.cmd_search(parsed_args)

            elif parsed_args.command == 'get':
                return self.cmd_get(parsed_args)

            elif parsed_args.command == 'show':
                return self.cmd_get(parsed_args)  # Alias for get command

            elif parsed_args.command == 'update':
                return self.cmd_update(parsed_args)

            elif parsed_args.command == 'list':
                return self.cmd_list(parsed_args)

            elif parsed_args.command == 'archive':
                return self.cmd_archive(parsed_args)

            elif parsed_args.command == 'archive-pack':
                return self.cmd_archive_pack(parsed_args)

            elif parsed_args.command == 'archive-search':
                return self.cmd_archive_search(parsed_args)

            elif parsed_args.command == 'mark':
                return self.cmd_mark(parsed_args)
            elif parsed_args.command == 'umbrella-finalize':
                return self.cmd_umbrella_finalize(parsed_args)

            elif parsed_args.command == 'merge':
                return self.cmd_merge(parsed_args)

            elif parsed_args.command == 'deps':
                return self.cmd_deps(parsed_args)

            elif parsed_args.command == 'ready':
                return self.cmd_ready(parsed_args)

            elif parsed_args.command == 'tags':
                return self.cmd_tags(parsed_args)

            elif parsed_args.command == 'order':
                return self.cmd_order(parsed_args)

            elif parsed_args.command == 'history':
                return self.cmd_history(parsed_args)
            elif parsed_args.command == 'reconcile':
                return self.cmd_reconcile(parsed_args)
            elif parsed_args.command == 'doctor':
                return self.cmd_doctor(parsed_args)
            elif parsed_args.command == 'cache':
                return self.cmd_cache(parsed_args)
            elif parsed_args.command == 'convert':
                return self.cmd_convert(parsed_args)
            elif parsed_args.command == 'compatibility':
                return self.cmd_compatibility(parsed_args)
            elif parsed_args.command == 'export-legacy':
                return self.cmd_export_legacy(parsed_args)
            elif parsed_args.command == 'rollback':
                return self.cmd_rollback(parsed_args)

            else:
                print(f"Unknown command: {parsed_args.command}", file=sys.stderr)
                return ExitCode.INVALID_USAGE

        except RegistryError as e:
            print(f"Project registry error: {e}", file=sys.stderr)
            return ExitCode.CONFIG_ERROR

        except KeyboardInterrupt:
            print("\nInterrupted", file=sys.stderr)
            return ExitCode.GENERAL_ERROR

        except BrokenPipeError:
            # Handle broken pipe (e.g., piping to head)
            return ExitCode.SUCCESS

        except Exception as e:
            print(f"Unexpected error: {e}", file=sys.stderr)
            if hasattr(parsed_args, 'verbose') and parsed_args.verbose:
                import traceback
                traceback.print_exc()
            return ExitCode.GENERAL_ERROR


def legacy_main(args: Optional[List[str]] = None) -> int:
    """Bounded launcher migration with one actionable canonical path."""
    invoked = os.path.basename(sys.argv[0])
    print(
        f"{invoked} is deprecated; install yylo-ledger and use yylo-ledger instead.",
        file=sys.stderr,
    )
    return main(args)


def main(args: Optional[List[str]] = None) -> int:
    """
    Main entry point for the CLI.

    Args:
        args: Command line arguments (default: sys.argv[1:])

    Returns:
        Exit code
    """
    cli = TaskCLI()
    return cli.run(args)


if __name__ == '__main__':
    sys.exit(main())
