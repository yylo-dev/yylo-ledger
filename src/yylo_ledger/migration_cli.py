"""CLI adapter for preservation-first native Record migration."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

from .artifacts import ArtifactStore
from .documents import DocumentStore
from .migration import (INVENTORY_SCHEMA, RecordMigration, _read_json, inventory,
                        make_plan, status_summary, write_inventory, write_plan)
from .records import RecordError


def add_migration_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "migration", allow_abbrev=False,
        help="Inventory, plan, copy, resume, and verify legacy files as native Records",
        description="Preservation-first native Record migration; this command never deletes source files")
    actions = parser.add_subparsers(dest="migration_action", required=True, metavar="ACTION")
    inv = actions.add_parser("inventory", allow_abbrev=False)
    inv.add_argument("--source-root", required=True)
    inv.add_argument("--declarations", help="JSON array of explicit source declarations")
    inv.add_argument("--wiki-root", action="append", default=[])
    inv.add_argument("--workflow-root", action="append", default=[])
    inv.add_argument("--output", required=True, help="Fresh external inventory receipt")
    plan = actions.add_parser("plan", allow_abbrev=False)
    plan.add_argument("--source-root", required=True)
    plan.add_argument("--inventory", required=True)
    plan.add_argument("--output", required=True, help="Fresh external immutable plan")
    apply = actions.add_parser("apply", allow_abbrev=False)
    apply.add_argument("--source-root", required=True)
    apply.add_argument("--plan", required=True)
    apply.add_argument("--status-file", required=True)
    apply.add_argument("--id", action="append", default=[])
    apply.add_argument("--all", action="store_true")
    apply.add_argument("--continue-on-error", action="store_true")
    status = actions.add_parser("status", allow_abbrev=False)
    status.add_argument("--source-root", required=True)
    status.add_argument("--plan", required=True)
    status.add_argument("--status-file", required=True)
    verify = actions.add_parser("verify", allow_abbrev=False)
    verify.add_argument("--source-root", required=True)
    verify.add_argument("--plan", required=True)
    verify.add_argument("--status-file", required=True)
    verify.add_argument("--id", action="append", default=[])


def _declarations(path: Optional[str]) -> list[dict[str, Any]]:
    if path is None:
        return []
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecordError("MIGRATION_DECLARATION_INVALID", f"cannot read declarations: {exc}") from exc
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise RecordError("MIGRATION_DECLARATION_INVALID", "declarations must be a JSON array of objects")
    return value


class MigrationCLI:
    def __init__(self, task_cli: Any):
        self.cli = task_cli
        self.storage = task_cli.storage

    def _migration(self, source_root: str) -> RecordMigration:
        return RecordMigration(
            source_root=Path(source_root), juno_root=self.storage.juno_root,
            project_root=self.storage.git_project_root,
            repository_ids=self.storage.git_repository_ids)

    def run(self, args: argparse.Namespace) -> int:
        try:
            action = args.migration_action
            if action == "inventory":
                value = inventory(Path(args.source_root), _declarations(args.declarations),
                                  wiki_roots=args.wiki_root, workflow_roots=args.workflow_root)
                write_inventory(Path(args.output), value, Path(args.source_root))
                output = {"inventory": str(Path(args.output).resolve()),
                          "inventory_sha256": value["inventory_sha256"], **value["summary"],
                          "source_preserved": True}
            elif action == "plan":
                source = _read_json(Path(args.inventory), INVENTORY_SCHEMA)
                documents = DocumentStore(
                    self.storage.juno_root, project_root=self.storage.git_project_root,
                    repository_ids=self.storage.git_repository_ids)
                artifacts = ArtifactStore(
                    self.storage.juno_root, project_root=self.storage.git_project_root,
                    repository_ids=self.storage.git_repository_ids)
                value = make_plan(source, destination_root=self.storage.juno_root.parent,
                                  documents=documents, artifacts=artifacts,
                                  source_root=Path(args.source_root))
                write_plan(Path(args.output), value, Path(args.source_root))
                output = {"plan": str(Path(args.output).resolve()), "plan_sha256": value["plan_sha256"],
                          **value["summary"], "source_preserved": True}
            else:
                migration = self._migration(args.source_root)
                plan = migration.load_plan(Path(args.plan))
                if action == "apply":
                    output = migration.apply(plan, Path(args.status_file), record_ids=args.id,
                                             all_items=args.all,
                                             continue_on_error=args.continue_on_error)
                elif action == "status":
                    status = migration.status(plan, Path(args.status_file), create=False)
                    output = {"summary": status_summary(status), "items": status["items"]}
                elif action == "verify":
                    output = migration.verify(plan, Path(args.status_file), record_ids=args.id)
                else:
                    raise RecordError("COMMAND_UNSUPPORTED", "unsupported migration action")
            print(json.dumps(output, ensure_ascii=False, sort_keys=True))
            return 0
        except RecordError as exc:
            print(json.dumps({"error": {"code": exc.code, "message": exc.message}}, sort_keys=True), file=sys.stderr)
            return 5
        except (OSError, json.JSONDecodeError) as exc:
            print(json.dumps({"error": {"code": "INPUT_INVALID", "message": str(exc)}}, sort_keys=True), file=sys.stderr)
            return 5
