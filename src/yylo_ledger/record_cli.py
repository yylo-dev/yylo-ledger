"""Typed native Record command groups for :mod:`yylo_ledger.cli`.

This is deliberately an adapter over the canonical stores.  It owns argument
transport and rendering only; Record validation, search, and archival remain in
their profile engines.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import secrets
import string
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from xml.etree.ElementTree import Element, SubElement, tostring

from .archive import iter_archive_envelopes
from .artifacts import ARTIFACT_PROFILES, ArtifactStore
from .documents import DocumentStore
from .frontmatter import emit_wiki_frontmatter, import_wiki_frontmatter
from .profiles import WORKFLOW_SCHEMA_V1
from .record_search import (IndexedRecord, RecordSearchIndex, RecordSearchPolicy,
                            RecordSearchQuery)
from .records import RECORD_ID_RE, RecordError, payload_digest, task_record_projection, value_digest
from .workflow_yaml import normalize_workflow_yaml

KINDS = ("task", "document", "artifact")
TYPED_GROUPS = ("task", "wiki", "workflow", "artifact")
ACTIONS = ("create", "list", "search", "get", "update", "history", "archive")
FORMATS = ("ndjson", "json", "xml", "table")


def _add_search(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--scope", choices=("hot", "archive", "all"), default="hot")
    parser.add_argument("--id", action="append", dest="ids", default=[])
    parser.add_argument("--slug")
    parser.add_argument("--kind", action="append", choices=KINDS, default=[])
    parser.add_argument("--profile", action="append", default=[])
    parser.add_argument("--namespace", action="append", default=[])
    parser.add_argument("--lifecycle", action="append", default=[])
    parser.add_argument("--media-type", action="append", default=[])
    parser.add_argument("--text")
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--relation-type")
    parser.add_argument("--relation-id")
    parser.add_argument("--digest")
    parser.add_argument("--backend")
    parser.add_argument("--size-min", type=int)
    parser.add_argument("--size-max", type=int)
    parser.add_argument("--created-after")
    parser.add_argument("--created-before")
    parser.add_argument("--modified-after")
    parser.add_argument("--modified-before")
    parser.add_argument("--provenance", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--git-role")
    parser.add_argument("--git-head")
    parser.add_argument("--git-ref")
    parser.add_argument("--git-dirty", choices=("true", "false"))
    parser.add_argument("--repository-id")
    parser.add_argument("--custom", action="append", default=[], metavar="PATH=JSON")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--cursor")
    parser.add_argument("--projection", choices=("metadata", "summary", "full"), default="summary")
    parser.add_argument("--fields", help="comma-separated fields; id is always retained")
    parser.add_argument("--sort", choices=("asc", "desc"), default="desc")
    parser.add_argument("--max-output-bytes", type=int)
    parser.add_argument("-f", "--format", dest="record_format", choices=FORMATS, default="ndjson")


def _add_create(parser: argparse.ArgumentParser, group: str) -> None:
    parser.add_argument("--id", dest="record_id")
    parser.add_argument("--title")
    parser.add_argument("--slug")
    parser.add_argument("--namespace", default="default")
    parser.add_argument("--alias", action="append", default=[])
    if group == "record":
        parser.add_argument("--kind", choices=KINDS, required=True)
        parser.add_argument("--profile")
    if group in ("record", "wiki", "workflow", "artifact"):
        parser.add_argument("--file", help="payload file, or - for stdin")
        parser.add_argument("--stdin", action="store_true", help="read payload from stdin (same as --file -)")
    if group in ("record", "artifact"):
        parser.add_argument("--mode", choices=("inline", "local", "external", "link"))
        if group == "artifact":
            parser.add_argument("--profile", dest="artifact_profile")
        parser.add_argument("--media-type", default="application/octet-stream")
        parser.add_argument("--uri")
        parser.add_argument("--digest")
        parser.add_argument("--size", type=int)
        parser.add_argument("--provenance", action="append", default=[], metavar="KEY=VALUE")
        parser.add_argument("--retention-file", help="JSON retention manifest")
        parser.add_argument("--predecessor-id")
    if group == "task":
        parser.add_argument("body", nargs="?")
    if group in ("record", "task"):
        parser.add_argument("--body-file")
        parser.add_argument("--status")
        parser.add_argument("--tags", nargs="*")


def _add_update(parser: argparse.ArgumentParser, group: str) -> None:
    parser.add_argument("identity")
    parser.add_argument("--expected-revision", type=int, required=True)
    parser.add_argument("--path")
    parser.add_argument("--expect-file")
    parser.add_argument("--value-file")
    parser.add_argument("--old-file")
    parser.add_argument("--new-file")
    parser.add_argument("--expected-record-digest")
    parser.add_argument("--expected-payload-digest")
    if group in ("record", "wiki"):
        parser.add_argument("--front-matter-file")
        parser.add_argument("--expected-preimage")
    if group in ("record", "artifact"):
        parser.add_argument("--mode", choices=("inline", "local", "external", "link"))
        parser.add_argument("--media-type")
        parser.add_argument("--uri")
        parser.add_argument("--digest")
        parser.add_argument("--size", type=int)
        parser.add_argument("--successor-id")


def add_record_parsers(subparsers: argparse._SubParsersAction) -> None:
    """Install generic and typed v2 groups. No group defines ``remove``."""
    for group in ("record", *TYPED_GROUPS):
        parser = subparsers.add_parser(
            group, allow_abbrev=False,
            help=("Native Record API v2" if group == "record" else f"Typed {group} Record API v2"),
            description="ID-first native Record commands (v2; legacy flat task commands remain compatible)")
        actions = parser.add_subparsers(dest="record_action", required=True, metavar="ACTION")
        create = actions.add_parser("create", allow_abbrev=False)
        _add_create(create, group)
        for action in ("list", "search"):
            child = actions.add_parser(action, allow_abbrev=False)
            _add_search(child)
        get = actions.add_parser("get", allow_abbrev=False)
        get.add_argument("identity")
        get.add_argument("--revision", type=int)
        get.add_argument("--source", "--raw", dest="source", action="store_true", help="emit exact Markdown/YAML payload bytes")
        get.add_argument("--front-matter", action="store_true", help="emit canonical wiki front matter")
        get.add_argument("--rendered", action="store_true", help="emit inert, HTML-escaped wiki rendering")
        get.add_argument("--validated", action="store_true", help="emit normalized validated workflow YAML")
        get.add_argument("-f", "--format", dest="record_format", choices=FORMATS, default="json")
        update = actions.add_parser("update", allow_abbrev=False)
        _add_update(update, group)
        history = actions.add_parser("history", allow_abbrev=False)
        history.add_argument("identity")
        history.add_argument("-f", "--format", dest="record_format", choices=FORMATS, default="ndjson")
        archive = actions.add_parser("archive", allow_abbrev=False)
        archive.add_argument("identity")
        archive.add_argument("--expected-revision", type=int, required=True)
        archive.add_argument("--receipt-file")


def _read_text(path: Optional[str], *, required: bool = True) -> Optional[str]:
    if path is None:
        if required:
            raise RecordError("INPUT_REQUIRED", "a file or '-' stdin transport is required")
        return None
    if path == "-":
        return sys.stdin.read()
    try:
        return Path(path).read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise RecordError("INPUT_UTF8_INVALID", "input file is not UTF-8") from exc


def _read_bytes(path: Optional[str]) -> Optional[bytes]:
    if path is None:
        return None
    return sys.stdin.buffer.read() if path == "-" else Path(path).read_bytes()


def _pairs(values: Iterable[str], *, parse_json: bool = False) -> dict[str, Any]:
    result = {}
    for item in values:
        if "=" not in item:
            raise RecordError("INPUT_INVALID", "expected KEY=VALUE")
        key, value = item.split("=", 1)
        if not key:
            raise RecordError("INPUT_INVALID", "key cannot be empty")
        if parse_json:
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise RecordError("INPUT_INVALID", f"{key} value must be JSON") from exc
        result[key] = value
    return result


def _render_markdown_safe(text: str) -> str:
    """Render a deliberately small inert Markdown subset with no active HTML."""
    lines, output, paragraph = text.splitlines(), [], []
    in_code = False
    def flush() -> None:
        if paragraph:
            output.append("<p>" + "<br>".join(paragraph) + "</p>")
            paragraph.clear()
    for raw in lines:
        if raw.startswith("```"):
            flush()
            output.append("</code></pre>" if in_code else "<pre><code>")
            in_code = not in_code
            continue
        escaped = html.escape(raw, quote=True)
        if in_code:
            output.append(escaped + "\n")
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", raw)
        if heading:
            flush(); level = len(heading.group(1))
            output.append(f"<h{level}>{html.escape(heading.group(2), quote=True)}</h{level}>")
        elif not raw:
            flush()
        else:
            paragraph.append(escaped)
    flush()
    if in_code:
        output.append("</code></pre>")
    return "\n".join(output) + "\n"


def _new_id() -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        value = "".join(secrets.choice(alphabet) for _ in range(6))
        if RECORD_ID_RE.fullmatch(value):
            return value


def _identity(record: Mapping[str, Any], supplied: Optional[str] = None) -> dict[str, Any]:
    value = dict(record)
    value["id"] = record["id"]
    value["slug"] = record["slug"]
    if supplied and supplied != record["id"]:
        value["resolved_from"] = supplied
    return value


def _with_receipt(record: Mapping[str, Any], operation: str,
                  supplied: Optional[str] = None) -> dict[str, Any]:
    value = _identity(record, supplied)
    receipt = {"operation": operation, "id": record["id"], "slug": record["slug"],
               "revision": record.get("revision"), "record_sha256": value_digest(record)}
    if supplied and supplied != record["id"]:
        receipt["resolved_from"] = supplied
    value["receipt"] = receipt
    return value


def _format(values: list[Mapping[str, Any]], fmt: str, *, single: bool = False,
            page: Optional[Mapping[str, Any]] = None) -> str:
    payload: Any = values[0] if single and values else values
    if fmt == "json":
        if page is not None:
            payload = {"records": values, **page}
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if fmt == "ndjson":
        lines = [json.dumps(item, ensure_ascii=False, sort_keys=True) for item in values]
        if page is not None:
            lines.append(json.dumps({"type": "page", **page}, sort_keys=True))
        return "\n".join(lines)
    if fmt == "xml":
        root = Element("record" if single else "records")
        targets = [root] if single else [SubElement(root, "record") for _ in values]
        for target, value in zip(targets, values):
            for key, item in value.items():
                node = SubElement(target, key)
                node.text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False, sort_keys=True)
        if page:
            node = SubElement(root, "page"); node.text = json.dumps(page, sort_keys=True)
        return tostring(root, encoding="unicode")
    lines = ["ID      KIND      PROFILE       TIER     REV  SLUG", "-" * 78]
    for item in values:
        lines.append(f"{item.get('id',''):<7} {item.get('kind',''):<9} {str(item.get('profile') or ''):<13} "
                     f"{item.get('tier',''):<8} {str(item.get('revision','')):<4} {item.get('slug','')}")
    return "\n".join(lines)


class RecordCLI:
    def __init__(self, task_cli: Any):
        self.cli = task_cli
        self.tasks = task_cli.storage
        self.root = self.tasks.juno_root
        self.documents = DocumentStore(self.root)
        self.artifacts = ArtifactStore(self.root)

    @staticmethod
    def _type(group: str) -> tuple[Optional[str], Optional[str]]:
        return {"record": (None, None), "task": ("task", None), "wiki": ("document", "wiki"),
                "workflow": ("document", "workflow"), "artifact": ("artifact", None)}[group]

    def _resolve(self, supplied: str, group: str = "record") -> tuple[str, dict[str, Any]]:
        kind, profile = self._type(group)
        matches = []
        getters = (("task", self.tasks.resolve_record_id, self.tasks.get_record),
                   ("document", self.documents.resolve, self.documents.get),
                   ("artifact", self.artifacts._resolve_archive_id, self.artifacts.get))
        for candidate_kind, resolver, getter in getters:
            if kind and candidate_kind != kind:
                continue
            try:
                record_id = resolver(supplied)
                record = getter(record_id)
                if profile and record.get("profile") != profile:
                    continue
                matches.append((record_id, record))
            except RecordError as exc:
                if exc.code not in ("RECORD_NOT_FOUND",):
                    raise
        unique = {item[0]: item for item in matches}
        if not unique:
            raise RecordError("RECORD_NOT_FOUND", f"no {group} Record matches {supplied!r}")
        if len(unique) != 1:
            raise RecordError("RECORD_IDENTITY_AMBIGUOUS", "identity resolves to more than one Record")
        return next(iter(unique.values()))

    def _sources(self) -> list[IndexedRecord]:
        result: list[IndexedRecord] = []
        cold_ids = set()
        for envelope in iter_archive_envelopes(self.root):
            record = envelope["task"]
            cold_ids.add(record["id"])
            result.append(IndexedRecord(record, tier="archive", locator=record["id"]))
        for path in sorted(self.tasks.tasks_root.glob("*/*.md")):
            task = self.tasks._read_path(path)
            result.append(IndexedRecord(task_record_projection(task), locator=task["id"]))
        for store in (self.documents, self.artifacts):
            for directory in sorted(store.records_root.glob("*/*")):
                paths = sorted(directory.glob("*.json"))
                if paths and directory.name not in cold_ids:
                    record = json.loads(paths[-1].read_text(encoding="utf-8"))
                    result.append(IndexedRecord(record, locator=record["id"]))
        return result

    def _canonical(self, tier: str, locator: str, record_id: str) -> Mapping[str, Any]:
        _, record = self._resolve(record_id)
        value = dict(record)
        value["tier"] = tier
        return value

    def _search(self, args: argparse.Namespace, group: str) -> int:
        kind, fixed_profile = self._type(group)
        custom = _pairs(args.custom, parse_json=True)
        configured_paths = self.cli.config.to_dict().get("record_search", {}).get(
            "custom_metadata_paths", [])
        policy = RecordSearchPolicy(custom_metadata_paths=frozenset(configured_paths),
                                    max_output_bytes=1024 * 1024)
        index = RecordSearchIndex(self.root / "cache" / "records-v2.sqlite3", policy=policy)
        index.rebuild(self._sources())
        fields = tuple(part.strip() for part in (args.fields or "").split(",") if part.strip())
        page = index.search(RecordSearchQuery(
            scope=args.scope, ids=args.ids, slug=args.slug,
            kinds=(kind,) if kind else tuple(args.kind), profiles=(fixed_profile,) if fixed_profile else tuple(args.profile),
            namespaces=tuple(args.namespace), lifecycles=tuple(args.lifecycle), media_types=tuple(args.media_type),
            text=args.text, tags=tuple(args.tag), relation_type=args.relation_type, relation_id=args.relation_id,
            digest=args.digest, backend=args.backend, size_min=args.size_min, size_max=args.size_max,
            created_after=args.created_after, created_before=args.created_before,
            modified_after=args.modified_after, modified_before=args.modified_before,
            provenance=_pairs(args.provenance), creation_git={key: value for key, value in {
                "role": args.git_role, "head_sha": args.git_head, "ref": args.git_ref,
                "worktree_dirty": None if args.git_dirty is None else args.git_dirty == "true",
                "repository_id": args.repository_id}.items() if value is not None},
            custom_equals=custom, limit=args.limit, cursor=args.cursor,
            projection=args.projection, fields=fields, sort_order=args.sort,
            output_byte_budget=args.max_output_bytes), self._canonical)
        metadata = {"next_cursor": page.next_cursor, "index_revision": page.index_revision,
                    "candidates_examined": page.candidates_examined, "output_bytes": page.output_bytes}
        print(_format(page.records, args.record_format, page=metadata))
        return 0

    def _create(self, args: argparse.Namespace, group: str) -> int:
        if group == "task" or (group == "record" and args.kind == "task"):
            # The flat task API is the explicit v1 compatibility implementation.
            body = (getattr(args, "body", None)
                    or _read_text(getattr(args, "body_file", None), required=False)
                    or (_read_text("-" if args.stdin else args.file, required=False) if group == "record" else None))
            if not body:
                raise RecordError("INPUT_REQUIRED", "task body requires a positional value or --body-file")
            task = self.tasks.create_task(body=body,
                status=getattr(args, "status", None) or self.cli.config.default_status,
                feature_tags=getattr(args, "tags", None) or [])
            record = task_record_projection(task.to_dict())
        elif group in ("wiki", "workflow") or (group == "record" and args.kind == "document"):
            profile = group if group != "record" else args.profile
            if profile not in ("wiki", "workflow"):
                raise RecordError("PROFILE_UNSUPPORTED", "Document create requires --profile wiki|workflow")
            if not args.title:
                raise RecordError("INPUT_REQUIRED", "Document create requires --title")
            text = _read_text("-" if args.stdin else args.file)
            record = self.documents.create(record_id=args.record_id or _new_id(), title=args.title,
                profile=profile, media_type="text/markdown" if profile == "wiki" else "application/yaml",
                text=text, namespace=args.namespace, slug=args.slug, aliases=args.alias,
                schema_ref=None if profile == "wiki" else WORKFLOW_SCHEMA_V1)
        else:
            profile = getattr(args, "artifact_profile", None) or getattr(args, "profile", None)
            if profile not in ARTIFACT_PROFILES:
                raise RecordError("ARTIFACT_PROFILE_INVALID", "Artifact create requires --profile stdout|model-output|report|receipt")
            mode = args.mode
            if not args.title:
                raise RecordError("INPUT_REQUIRED", "Artifact create requires --title")
            if not mode:
                raise RecordError("ARTIFACT_MODE_INVALID", "Artifact create requires an explicit --mode")
            content = _read_bytes("-" if args.stdin else args.file)
            retention = json.loads(Path(args.retention_file).read_text()) if args.retention_file else None
            kwargs = dict(record_id=args.record_id or _new_id(), title=args.title, profile=profile, mode=mode,
                          content=content, media_type=args.media_type, uri=args.uri, digest=args.digest, size=args.size,
                          provenance=_pairs(args.provenance), retention=retention, predecessor_id=args.predecessor_id)
            record = self.artifacts.create(**kwargs)
        print(_format([_with_receipt(record, "create")], "json", single=True))
        return 0

    def _get(self, args: argparse.Namespace, group: str) -> int:
        record_id, record = self._resolve(args.identity, group)
        if args.revision:
            if record["kind"] == "document": record = self.documents.get(record_id, args.revision)
            elif record["kind"] == "artifact": record = self.artifacts.get(record_id, args.revision)
            else: raise RecordError("REVISION_UNSUPPORTED", "legacy task snapshots use history")
        if args.source or args.front_matter or args.rendered or args.validated:
            if record["kind"] != "document":
                raise RecordError("RENDER_UNSUPPORTED", "source rendering is available only for wiki/workflow Documents")
            if args.front_matter:
                output = emit_wiki_frontmatter(record)
            elif args.rendered:
                if record["profile"] != "wiki": raise RecordError("PROFILE_KIND_MISMATCH", "--rendered requires wiki")
                output = _render_markdown_safe(record["payload"]["text"])
            elif args.validated:
                if record["profile"] != "workflow": raise RecordError("PROFILE_KIND_MISMATCH", "--validated requires workflow")
                output = normalize_workflow_yaml(record["payload"]["text"])
            else:
                output = record["payload"]["text"]
            sys.stdout.write(output)
            return 0
        print(_format([_identity(record, args.identity)], args.record_format, single=True))
        return 0

    def _persist_frontmatter(self, record_id: str, supplied: str, args: argparse.Namespace) -> dict[str, Any]:
        with self.documents._lock(record_id):
            current = self.documents.get(record_id)
            source = _read_text(args.front_matter_file)
            candidate, event = import_wiki_frontmatter(source, current,
                expected_revision=args.expected_revision, expected_preimage=args.expected_preimage or "")
            if event is None:
                return current
            revision = self.documents._directory(record_id) / f"{candidate['revision']:08d}.json"
            history = self.documents._event_directory(record_id) / f"{candidate['revision']:08d}.json"
            try:
                self.documents._atomic_create(revision, candidate)
                self.documents._atomic_create(history, event)
            except BaseException:
                revision.unlink(missing_ok=True); history.unlink(missing_ok=True); raise
            return candidate

    def _update(self, args: argparse.Namespace, group: str) -> int:
        record_id, current = self._resolve(args.identity, group)
        if current["kind"] == "task":
            path = args.path
            old_file, new_file = args.old_file or args.expect_file, args.new_file or args.value_file
            if not path or not old_file or not new_file:
                raise RecordError("EXACT_PREIMAGE_REQUIRED", "task update requires --path plus exact old/new files")
            if path in ("/body", "/agent_response"):
                expected, replacement, mode = _read_text(old_file), _read_text(new_file), "structured"
            else:
                expected, replacement, mode = json.loads(_read_text(old_file)), json.loads(_read_text(new_file)), "structured"
            self.tasks.exact_replace_record(record_id, path=path, expected=expected,
                replacement=replacement, expected_revision=args.expected_revision, mode=mode,
                expected_digest=args.expected_record_digest)
            record = self.tasks.get_record(record_id)
        elif current["kind"] == "artifact":
            manifest_file = args.expect_file or args.old_file
            content_file = args.new_file or args.value_file
            if not manifest_file or not args.mode:
                raise RecordError("EXACT_PREIMAGE_REQUIRED", "Artifact update requires an exact old manifest file and --mode")
            expected_payload = json.loads(_read_text(manifest_file))
            content = _read_bytes(content_file)
            record = self.artifacts.revise(record_id, expected_revision=args.expected_revision,
                expected_payload=expected_payload, successor_id=args.successor_id, mode=args.mode,
                content=content, media_type=args.media_type or current["media_type"], uri=args.uri,
                digest=args.digest, size=args.size)
        elif getattr(args, "front_matter_file", None):
            if not args.expected_preimage:
                raise RecordError("EXACT_PREIMAGE_REQUIRED", "front matter import requires --expected-preimage")
            record = self._persist_frontmatter(record_id, args.identity, args)
        else:
            path = args.path
            old_file, new_file = args.old_file or args.expect_file, args.new_file or args.value_file
            if not path:
                path = "/payload/text" if new_file and (old_file or args.expected_payload_digest) else None
            if not path or not new_file or (not old_file and not args.expected_payload_digest):
                raise RecordError("EXACT_PREIMAGE_REQUIRED", "update requires exact old/new files or a payload digest plus new file")
            payload_mode = path == "/payload/text"
            if payload_mode:
                expected, replacement, mode = (_read_text(old_file) if old_file else None), _read_text(new_file), "payload"
            else:
                expected, replacement, mode = json.loads(_read_text(old_file)), json.loads(_read_text(new_file)), "structured"
            record = self.documents.update(record_id, path=path, expected=expected, replacement=replacement,
                expected_revision=args.expected_revision, expected_record_digest=args.expected_record_digest,
                expected_payload_digest=args.expected_payload_digest, mode=mode)
        print(_format([_with_receipt(record, "update", args.identity)], "json", single=True))
        return 0

    def _history(self, args: argparse.Namespace, group: str) -> int:
        record_id, record = self._resolve(args.identity, group)
        if record["kind"] == "task": events = self.tasks.history(record_id)
        elif record["kind"] == "document": events = self.documents.history(record_id)
        else:
            root = self.artifacts.events_root / record_id[:2].lower() / record_id
            events = [json.loads(path.read_text()) for path in sorted(root.glob("*.json"))]
            if not events: events = self.artifacts.archive_history(record_id)
        values = [{"id": record_id, "slug": record["slug"], **event,
                   **({"resolved_from": args.identity} if args.identity != record_id else {})} for event in events]
        print(_format(values, args.record_format))
        return 0

    def _archive(self, args: argparse.Namespace, group: str) -> int:
        record_id, record = self._resolve(args.identity, group)
        receipt = Path(args.receipt_file) if args.receipt_file else None
        if record["kind"] == "task":
            result = self.tasks.archive_record(record_id, expected_revision=args.expected_revision,
                                               receipt_path=receipt)
        elif record["kind"] == "document":
            result = self.documents.archive(record_id, expected_revision=args.expected_revision,
                                            receipt_path=receipt)
        else:
            result = self.artifacts.archive_record(record_id, expected_revision=args.expected_revision,
                                                   receipt_path=receipt)
        result = {"id": record_id, "slug": record["slug"],
                  **({"resolved_from": args.identity} if args.identity != record_id else {}),
                  "receipt": {"id": record_id, "slug": record["slug"],
                              **({"resolved_from": args.identity} if args.identity != record_id else {}),
                              "archive": result}}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0

    def run(self, args: argparse.Namespace) -> int:
        group, action = args.command, args.record_action
        try:
            if action == "create": return self._create(args, group)
            if action in ("list", "search"): return self._search(args, group)
            if action == "get": return self._get(args, group)
            if action == "update": return self._update(args, group)
            if action == "history": return self._history(args, group)
            if action == "archive": return self._archive(args, group)
            raise RecordError("COMMAND_UNSUPPORTED", "unsupported Record action")
        except RecordError as exc:
            print(json.dumps({"error": {"code": exc.code, "message": exc.message}}, sort_keys=True), file=sys.stderr)
            return 5
        except (OSError, json.JSONDecodeError) as exc:
            print(json.dumps({"error": {"code": "INPUT_INVALID", "message": str(exc)}}, sort_keys=True), file=sys.stderr)
            return 5
