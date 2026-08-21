#!/usr/bin/env python3
"""
Task file merging operations and conflict resolution.
"""

import os
import json
import shutil
import tempfile
import hashlib
from typing import List, Dict, Any, Optional, Tuple, Set, Iterator
from pathlib import Path
from datetime import datetime
from collections import defaultdict

from .models import Task
from .storage import TaskStorage
from .config import Config
from .codec import MarkdownTaskCodec, normalized_bytes, plain_value
from .ledger import TaskLedger


MERGE_PLAN_SCHEMA = "juno_kanban_merge_plan.v1"
MERGE_RECEIPT_SCHEMA = "juno_kanban_merge_receipt.v1"


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sealed(value: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(value)
    result.pop("content_sha256", None)
    result["content_sha256"] = _sha256_bytes(_canonical_bytes(result))
    return result


def _verify_seal(value: Dict[str, Any], label: str) -> None:
    expected = value.get("content_sha256")
    if not isinstance(expected, str) or _sealed(value)["content_sha256"] != expected:
        raise ValueError(f"{label} content hash mismatch")


def _atomic_json(path: Path, value: Dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class MergeConflict:
    """Represents a task ID conflict during merge."""

    def __init__(self, task_id: str, source_task: Dict[str, Any],
                 target_task: Dict[str, Any], source_path: str, target_path: str):
        self.task_id = task_id
        self.source_task = source_task
        self.target_task = target_task
        self.source_path = source_path
        self.target_path = target_path

    def get_newer_task(self) -> Tuple[Dict[str, Any], str]:
        """Return the task with newer last_modified date."""
        source_modified = self.source_task.get('last_modified', '')
        target_modified = self.target_task.get('last_modified', '')

        if source_modified > target_modified:
            return self.source_task, 'source'
        else:
            return self.target_task, 'target'

    def __str__(self) -> str:
        return f"Conflict for task {self.task_id}: {self.source_path} vs {self.target_path}"


class TaskMerger:
    """Handles merging of multiple task files with conflict resolution."""

    def __init__(self, config: Optional[Config] = None):
        """
        Initialize merger.

        Args:
            config: Configuration object
        """
        self.config = config or Config()

    def find_juno_task_directories(self, root_path: str) -> List[str]:
        """
        Find all .juno_task directories under root_path.

        Args:
            root_path: Root directory to search

        Returns:
            List of .juno_task directory paths
        """
        juno_task_dirs = []

        for root, dirs, files in os.walk(root_path):
            if '.juno_task' in dirs:
                juno_task_path = os.path.join(root, '.juno_task')
                # Check if it has a tasks subdirectory
                tasks_path = os.path.join(juno_task_path, 'tasks')
                if os.path.exists(tasks_path):
                    juno_task_dirs.append(juno_task_path)

        return sorted(juno_task_dirs)

    def collect_tasks_from_sources(self, source_paths: List[str]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, str]]:
        """
        Collect all tasks from source paths.

        Args:
            source_paths: List of .juno_task directory paths

        Returns:
            Tuple of (tasks_dict, task_sources_dict) where:
            - tasks_dict: {task_id: task_data}
            - task_sources_dict: {task_id: source_path}
        """
        all_tasks = {}
        task_sources = {}

        for source_path in source_paths:
            tasks_dir = os.path.join(source_path, 'tasks')
            if not os.path.exists(tasks_dir):
                continue

            # Create temporary config for this source
            # We need to create a config with modified storage path
            source_config_dict = self.config.to_dict().copy()
            source_config_dict['storage']['base_path'] = tasks_dir

            # Create temporary config from modified dict
            temp_config = Config(auto_create=False)
            temp_config.config = source_config_dict

            # Create storage instance for this source
            storage = TaskStorage(temp_config)

            # Read all tasks from this source
            for task_dict in storage.read_all_tasks():
                task_id = task_dict.get('id')
                if not task_id:
                    continue

                if task_id in all_tasks:
                    # We have a duplicate - we'll handle this in conflict resolution
                    pass

                all_tasks[task_id] = task_dict
                task_sources[task_id] = source_path

        return all_tasks, task_sources

    def detect_conflicts(self, source_paths: List[str], target_path: str) -> List[MergeConflict]:
        """
        Detect ID conflicts between source paths and target.

        Args:
            source_paths: List of source .juno_task directories
            target_path: Target .juno_task directory

        Returns:
            List of merge conflicts
        """
        conflicts = []

        # Get existing tasks in target
        target_tasks_dir = os.path.join(target_path, 'tasks')
        if os.path.exists(target_tasks_dir):
            # Create temporary config for target
            target_config_dict = self.config.to_dict().copy()
            target_config_dict['storage']['base_path'] = target_tasks_dir

            target_config = Config(auto_create=False)
            target_config.config = target_config_dict
            target_storage = TaskStorage(target_config)

            target_tasks = {task['id']: task for task in target_storage.read_all_tasks() if 'id' in task}
        else:
            target_tasks = {}

        # Check for conflicts with each source
        for source_path in source_paths:
            source_tasks_dir = os.path.join(source_path, 'tasks')
            if not os.path.exists(source_tasks_dir):
                continue

            # Create temporary config for this source
            source_config_dict = self.config.to_dict().copy()
            source_config_dict['storage']['base_path'] = source_tasks_dir

            source_config = Config(auto_create=False)
            source_config.config = source_config_dict
            source_storage = TaskStorage(source_config)

            for source_task in source_storage.read_all_tasks():
                task_id = source_task.get('id')
                if not task_id:
                    continue

                if task_id in target_tasks:
                    conflict = MergeConflict(
                        task_id=task_id,
                        source_task=source_task,
                        target_task=target_tasks[task_id],
                        source_path=source_path,
                        target_path=target_path
                    )
                    conflicts.append(conflict)

        return conflicts

    def resolve_conflicts_keep_newer(self, conflicts: List[MergeConflict]) -> Dict[str, Dict[str, Any]]:
        """
        Resolve conflicts by keeping the task with newer last_modified.

        Args:
            conflicts: List of conflicts to resolve

        Returns:
            Dict of resolved tasks {task_id: task_data}
        """
        resolved = {}

        for conflict in conflicts:
            newer_task, source = conflict.get_newer_task()
            resolved[conflict.task_id] = newer_task

        return resolved

    def resolve_conflicts_keep_both(self, conflicts: List[MergeConflict]) -> Dict[str, Dict[str, Any]]:
        """
        Resolve conflicts by keeping both tasks (rename source task IDs).

        Args:
            conflicts: List of conflicts to resolve

        Returns:
            Dict of all tasks with renamed IDs {task_id: task_data}
        """
        resolved = {}

        for conflict in conflicts:
            # Keep target task as-is
            resolved[conflict.task_id] = conflict.target_task

            # Create new ID for source task
            new_id = self._generate_new_id(conflict.task_id, resolved.keys())
            source_task_copy = conflict.source_task.copy()
            source_task_copy['id'] = new_id
            source_task_copy['last_modified'] = datetime.now().isoformat()

            resolved[new_id] = source_task_copy

        return resolved

    def _generate_new_id(self, original_id: str, existing_ids: Set[str]) -> str:
        """
        Generate a new unique ID based on original ID.

        Args:
            original_id: Original task ID
            existing_ids: Set of existing IDs to avoid

        Returns:
            New unique ID
        """
        # Try appending _1, _2, etc.
        counter = 1
        while True:
            new_id = f"{original_id}_{counter}"
            if new_id not in existing_ids:
                return new_id
            counter += 1

            # Safety limit
            if counter > 1000:
                # Fallback to generating completely new ID
                return Task.generate_id()

    def _storage(self, juno_root: Path) -> TaskStorage:
        config_dict = self.config.to_dict().copy()
        config_dict['storage'] = dict(config_dict['storage'])
        config_dict['storage']['base_path'] = str(juno_root / 'tasks')
        config = Config(auto_create=False)
        config.config = config_dict
        return TaskStorage(config)

    @staticmethod
    def _task_snapshot(storage: TaskStorage) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
        records: Dict[str, Dict[str, Any]] = {}
        identities: Dict[str, Dict[str, Any]] = {}
        for task in storage.read_all_tasks_canonical():
            task_id = str(task['id'])
            record = plain_value(task)
            path = Path(storage.find_task_file(task_id))
            latest = storage.ledger.latest(task_id)
            normalized_sha = _sha256_bytes(normalized_bytes(record))
            if latest is None or latest.get('after_sha256') != normalized_sha:
                raise ValueError(f"task and ledger head do not agree: {task_id}")
            records[task_id] = record
            identities[task_id] = {
                'task_sha256': _sha256_bytes(path.read_bytes()),
                'normalized_sha256': normalized_sha,
                'ledger_head_sha256': latest['event_sha256'],
            }
        return records, identities

    def _source_snapshot(self, source_paths: List[str]) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
        combined: Dict[str, Dict[str, Any]] = {}
        snapshots = []
        for source_path in source_paths:
            root = Path(source_path).expanduser().resolve()
            storage = self._storage(root)
            records, identities = self._task_snapshot(storage)
            combined.update(records)
            snapshots.append({'path': str(root), 'tasks': identities})
        return combined, snapshots

    @staticmethod
    def _ledger_relative_path(ledger: TaskLedger, task_id: str, event: Dict[str, Any]) -> str:
        encoded_size = len(_canonical_bytes(event)) + 1
        segments = ledger.segments(task_id)
        target = segments[-1] if segments else ledger.directory(task_id) / '000001.ndjson'
        if target.exists() and target.stat().st_size and target.stat().st_size + encoded_size > ledger.max_segment_bytes:
            target = ledger.directory(task_id) / f"{len(segments) + 1:06d}.ndjson"
        return str(target.relative_to(ledger.root.parent))

    def build_plan(self, source_paths: List[str], target_path: str,
                   strategy: str = 'keep-newer') -> Dict[str, Any]:
        if strategy not in {'keep-newer', 'keep-both'}:
            raise ValueError(f"Unsupported strategy: {strategy}")
        target = Path(target_path).expanduser().resolve()
        source_tasks, source_snapshots = self._source_snapshot(source_paths)
        if (target / 'tasks').exists():
            target_storage = self._storage(target)
            target_tasks, target_identities = self._task_snapshot(target_storage)
        else:
            target_tasks, target_identities = {}, {}
            target_storage = None

        final_tasks = dict(target_tasks)
        added: List[str] = []
        updated: List[str] = []
        kept = set(target_tasks)
        conflicts = sorted(set(source_tasks) & set(target_tasks))
        occupied = set(target_tasks) | set(source_tasks)
        for task_id in sorted(source_tasks):
            source_task = source_tasks[task_id]
            if task_id not in target_tasks:
                final_tasks[task_id] = source_task
                added.append(task_id)
                continue
            if strategy == 'keep-both':
                new_id = self._generate_new_id(task_id, occupied)
                occupied.add(new_id)
                duplicate = dict(source_task)
                duplicate['id'] = new_id
                final_tasks[new_id] = duplicate
                added.append(new_id)
                continue
            target_task = target_tasks[task_id]
            if source_task.get('last_modified', '') > target_task.get('last_modified', ''):
                if normalized_bytes(source_task) != normalized_bytes(target_task):
                    final_tasks[task_id] = source_task
                    updated.append(task_id)
                    kept.discard(task_id)

        # Merge activation is a lifecycle mutation too. A reviewed plan may not
        # import or preserve the contradictory done-plus-blocked state.
        for task_id, task in sorted(final_tasks.items()):
            if task.get('status') != 'done':
                continue
            unmet = [blocker_id for blocker_id in (task.get('blocked_by') or [])
                     if final_tasks.get(blocker_id, {}).get('status') not in ('done', 'archive')]
            if unmet:
                raise ValueError(
                    f"merge result cannot complete task {task_id}; unmet blockers: "
                    + ", ".join(sorted(unmet)))

        ledger = target_storage.ledger if target_storage else TaskLedger(target / 'ledger')
        codec = MarkdownTaskCodec()
        mutations = []
        for task_id in sorted(added + updated):
            after = plain_value(final_tasks[task_id])
            before = plain_value(target_tasks.get(task_id, {}))
            before_sha = (_sha256_bytes(normalized_bytes(before)) if before else None)
            after_sha = _sha256_bytes(normalized_bytes(after))
            event = ledger.prepare(
                task_id,
                'create' if task_id in added else 'merge',
                'merge-plan',
                before_sha,
                after_sha,
                before,
                after,
                task_id in added,
            )
            task_rel = f"tasks/{task_id[:2].lower()}/{task_id}.md"
            ledger_rel = self._ledger_relative_path(ledger, task_id, event)
            mutations.append({
                'task_id': task_id,
                'operation': 'create' if task_id in added else 'merge',
                'record': after,
                'before_normalized_sha256': before_sha,
                'after_normalized_sha256': after_sha,
                'task_content_sha256': _sha256_bytes(codec.dumps(after).encode('utf-8')),
                'ledger_event': event,
                'changed_paths': [task_rel, ledger_rel],
            })

        input_identity = {
            'sources': source_snapshots,
            'target': {'path': str(target), 'tasks': target_identities},
            'strategy': strategy,
        }
        planned_paths = sorted({path for mutation in mutations for path in mutation['changed_paths']})
        plan = {
            'schema_version': MERGE_PLAN_SCHEMA,
            'input_identity': input_identity,
            'input_identity_sha256': _sha256_bytes(_canonical_bytes(input_identity)),
            'result': {
                'added_ids': sorted(added),
                'updated_ids': sorted(updated),
                'kept_ids': sorted(kept),
                'conflict_ids': conflicts,
                'final_task_count': len(final_tasks),
                'planned_changed_paths': planned_paths,
                'mutations': mutations,
            },
        }
        return _sealed(plan)

    @staticmethod
    def _path_hashes(root: Path, paths: List[str]) -> Dict[str, Optional[str]]:
        result: Dict[str, Optional[str]] = {}
        for relative in paths:
            path = root / relative
            result[relative] = _sha256_bytes(path.read_bytes()) if path.is_file() else None
        return result

    @staticmethod
    def _merge_fault(point: str) -> None:
        if os.environ.get('YYLO_LEDGER_MERGE_FAULT') == point:
            raise RuntimeError(f"injected merge fault: {point}")

    def apply_plan(self, plan: Dict[str, Any], target_path: str,
                   receipt_path: Optional[Path] = None) -> Dict[str, Any]:
        _verify_seal(plan, 'merge plan')
        if plan.get('schema_version') != MERGE_PLAN_SCHEMA:
            raise ValueError('unsupported merge plan schema')
        target = Path(target_path).expanduser().resolve()
        identity = plan.get('input_identity', {})
        if identity.get('target', {}).get('path') != str(target):
            raise ValueError('merge plan target identity mismatch')
        sources = [item['path'] for item in identity.get('sources', [])]
        current = self.build_plan(sources, str(target), identity.get('strategy', ''))
        if current.get('input_identity_sha256') != plan.get('input_identity_sha256'):
            raise ValueError('merge plan is stale; source, target, task snapshot, or ledger head changed')
        # Events contain the only intentionally volatile plan fields. Everything
        # else must still be derivable from the bound input snapshot.
        for key in ('added_ids', 'updated_ids', 'kept_ids', 'conflict_ids',
                    'final_task_count', 'planned_changed_paths'):
            if current['result'][key] != plan['result'].get(key):
                raise ValueError(f"merge plan result mismatch: {key}")
        current_records = {item['task_id']: item['record'] for item in current['result']['mutations']}
        for mutation in plan['result'].get('mutations', []):
            task_id = mutation.get('task_id')
            if current_records.get(task_id) != mutation.get('record'):
                raise ValueError(f"merge plan changed record mismatch: {task_id}")
            if _sha256_bytes(normalized_bytes(mutation['record'])) != mutation.get('after_normalized_sha256'):
                raise ValueError(f"merge plan record hash mismatch: {task_id}")

        changed_paths = plan['result']['planned_changed_paths']
        before_hashes = self._path_hashes(target, changed_paths)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix='.juno-merge-', dir=target.parent))
        staged = temporary / 'staged'
        backup_tasks = temporary / 'original-tasks'
        backup_ledger = temporary / 'original-ledger'
        staged.mkdir()
        if (target / 'tasks').exists():
            shutil.copytree(target / 'tasks', staged / 'tasks')
        else:
            (staged / 'tasks').mkdir()
        if (target / 'ledger').exists():
            shutil.copytree(target / 'ledger', staged / 'ledger')
        else:
            (staged / 'ledger').mkdir()
        staged_storage = self._storage(staged)
        activated_tasks = activated_ledger = False
        had_tasks = (target / 'tasks').exists()
        had_ledger = (target / 'ledger').exists()
        try:
            for mutation in plan['result'].get('mutations', []):
                task_id = mutation['task_id']
                before = staged_storage.find_task(task_id)
                before_sha = staged_storage.normalized_hash(before) if before else None
                if before_sha != mutation.get('before_normalized_sha256'):
                    raise ValueError(f"staged merge precondition mismatch: {task_id}")
                staged_storage._write_current(mutation['record'])
                staged_storage.ledger.append_prepared(mutation['ledger_event'])
            staged_hashes = self._path_hashes(staged, changed_paths)
            if any(value is None for value in staged_hashes.values()):
                raise ValueError('staged merge did not create every planned path')
            self._merge_fault('before_activation')
            pre_activation = self.build_plan(sources, str(target), identity['strategy'])
            if pre_activation.get('input_identity_sha256') != plan.get('input_identity_sha256'):
                raise ValueError('merge plan became stale before activation')
            target.mkdir(parents=True, exist_ok=True)
            if had_tasks:
                os.replace(target / 'tasks', backup_tasks)
            os.replace(staged / 'tasks', target / 'tasks')
            activated_tasks = True
            self._merge_fault('after_tasks_activation')
            if had_ledger:
                os.replace(target / 'ledger', backup_ledger)
            os.replace(staged / 'ledger', target / 'ledger')
            activated_ledger = True
            self._merge_fault('after_ledger_activation')
            after_hashes = self._path_hashes(target, changed_paths)
            if after_hashes != staged_hashes:
                raise ValueError('activated merge hashes differ from staged plan')
            receipt = _sealed({
                'schema_version': MERGE_RECEIPT_SCHEMA,
                'plan_sha256': plan['content_sha256'],
                'input_identity_sha256': plan['input_identity_sha256'],
                'target': str(target),
                'added_ids': plan['result']['added_ids'],
                'updated_ids': plan['result']['updated_ids'],
                'kept_ids': plan['result']['kept_ids'],
                'changed_paths': changed_paths,
                'before_sha256': before_hashes,
                'after_sha256': after_hashes,
                'ledger_event_ids': {
                    item['task_id']: item['ledger_event']['event_id']
                    for item in plan['result'].get('mutations', [])
                },
            })
            if receipt_path is not None:
                _atomic_json(receipt_path, receipt)
            return receipt
        except Exception:
            if activated_ledger and (target / 'ledger').exists():
                shutil.rmtree(target / 'ledger')
            if backup_ledger.exists():
                os.replace(backup_ledger, target / 'ledger')
            elif activated_ledger and not had_ledger:
                pass
            if activated_tasks and (target / 'tasks').exists():
                shutil.rmtree(target / 'tasks')
            if backup_tasks.exists():
                os.replace(backup_tasks, target / 'tasks')
            raise
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    def merge_files(self, source_paths: List[str], target_path: str,
                   strategy: str = 'keep-newer', dry_run: bool = False,
                   plan_file: Optional[str] = None, apply_plan: Optional[str] = None,
                   receipt_file: Optional[str] = None) -> Dict[str, Any]:
        """Preview or apply one sealed, identity-bound merge plan."""
        if dry_run:
            if not plan_file or apply_plan or receipt_file:
                raise ValueError('dry-run requires --plan-file and cannot use --apply-plan/--receipt-file')
            plan = self.build_plan(source_paths, target_path, strategy)
            _atomic_json(Path(plan_file), plan)
            stats = {
                'total_sources': len(source_paths),
                'conflicts_found': len(plan['result']['conflict_ids']),
                'conflicts_resolved': len(plan['result']['updated_ids']),
                'tasks_added': len(plan['result']['added_ids']),
                'tasks_kept': len(plan['result']['kept_ids']),
                'final_task_count': plan['result']['final_task_count'],
                'strategy_used': strategy,
                'dry_run': True,
            }
            return {'success': True, 'conflicts': plan['result']['conflict_ids'],
                    'statistics': stats, 'plan': {'path': str(Path(plan_file).resolve()),
                    'sha256': plan['content_sha256'],
                    'changed_paths': plan['result']['planned_changed_paths']}}
        if not apply_plan or not receipt_file or plan_file:
            raise ValueError('apply requires --apply-plan and --receipt-file and cannot use --plan-file')
        plan = json.loads(Path(apply_plan).read_text(encoding='utf-8'))
        if source_paths:
            supplied = [str(Path(path).expanduser().resolve()) for path in source_paths]
            planned = [item['path'] for item in plan.get('input_identity', {}).get('sources', [])]
            if supplied != planned:
                raise ValueError('apply source paths differ from the reviewed merge plan')
        if strategy != plan.get('input_identity', {}).get('strategy'):
            raise ValueError('apply strategy differs from the reviewed merge plan')
        receipt = self.apply_plan(plan, target_path, Path(receipt_file))
        stats = {
            'total_sources': len(plan['input_identity']['sources']),
            'conflicts_found': len(plan['result']['conflict_ids']),
            'conflicts_resolved': len(plan['result']['updated_ids']),
            'tasks_added': len(plan['result']['added_ids']),
            'tasks_kept': len(plan['result']['kept_ids']),
            'final_task_count': plan['result']['final_task_count'],
            'strategy_used': strategy,
            'dry_run': False,
        }
        return {'success': True, 'conflicts': plan['result']['conflict_ids'],
                'statistics': stats, 'receipt': {'path': str(Path(receipt_file).resolve()),
                'sha256': receipt['content_sha256'], 'changed_paths': receipt['changed_paths']}}

    def _backup_sources(self, source_paths: List[str]):
        """
        Create backups of source directories before merge.

        Args:
            source_paths: Source paths to backup
        """
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

        for source_path in source_paths:
            backup_path = f"{source_path}.backup.{timestamp}"
            if not os.path.exists(backup_path):
                shutil.copytree(source_path, backup_path)
