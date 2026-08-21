"""Safe, round-trip Markdown/YAML task codec."""
from __future__ import annotations

import io
import json
import math
from datetime import date, datetime
from typing import Any, Mapping, MutableMapping

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.error import YAMLError


BODY_START = "<!-- juno:body:start -->"
BODY_END = "<!-- juno:body:end -->"
RESPONSE_START = "<!-- juno:response:start -->"
RESPONSE_END = "<!-- juno:response:end -->"
MARKERS = (BODY_START, BODY_END, RESPONSE_START, RESPONSE_END)

# Public task serialization order. Parsers remain order-independent; this only
# makes emitted JSON/YAML predictable and keeps high-value identity/content
# fields ahead of implementation metadata and extensions.
TASK_FIELD_ORDER = (
    "id", "status", "body", "created_date", "last_modified",
    "commit_hash", "agent_response", "feature_tags", "related_tasks",
    "blocked_by", "schema_version", "fields",
)


def order_task_fields(record: Mapping[str, Any]) -> MutableMapping[str, Any]:
    """Return a mapping with known task fields first and extensions afterward."""
    if isinstance(record, CommentedMap):
        # CommentedMap.copy() retains ruamel's per-key comment attachments.
        ordered = record.copy()
        for key in reversed(TASK_FIELD_ORDER):
            if key in ordered:
                ordered.move_to_end(key, last=False)
        return ordered
    ordered = {}
    for key in TASK_FIELD_ORDER:
        if key in record:
            ordered[key] = record[key]
    for key, value in record.items():
        if key not in ordered:
            ordered[key] = value
    return ordered


class TaskFormatError(ValueError):
    pass


def _json_compatible(value: Any, path: str = "$") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise TaskFormatError(f"non-finite YAML number at {path} is not JSON-compatible")
    if value is None or isinstance(value, (str, bool, int, float, date, datetime)):
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _json_compatible(item, f"{path}[{index}]")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TaskFormatError(f"YAML map key at {path} must be a string")
            _json_compatible(item, f"{path}.{key}")
        return
    raise TaskFormatError(f"unsupported YAML value at {path}: {type(value).__name__}")


class MarkdownTaskCodec:
    """Round-trip metadata comments while keeping Markdown boundaries unambiguous."""

    @staticmethod
    def _yaml() -> YAML:
        # ruamel parser/emitter instances carry mutable scanner/composer state
        # and are not safe to share across different-task worker threads.
        yaml = YAML(typ="rt")
        yaml.preserve_quotes = True
        yaml.allow_duplicate_keys = False
        yaml.default_flow_style = False
        yaml.width = 4096
        return yaml

    @staticmethod
    def _split(text: str):
        if "\r" in text:
            raise TaskFormatError("task files must use LF line endings")
        if not text.startswith("---\n"):
            raise TaskFormatError("missing opening YAML front matter delimiter")
        end = text.find("\n---\n", 4)
        if end < 0:
            raise TaskFormatError("missing closing YAML front matter delimiter")
        yaml_text = text[4:end]
        markdown = text[end + 5:]
        for marker in MARKERS:
            if markdown.count(marker) != 1:
                raise TaskFormatError(f"boundary marker must occur exactly once: {marker}")
        positions = [markdown.index(marker) for marker in MARKERS]
        if positions != sorted(positions):
            raise TaskFormatError("body/response boundary markers are nested or reversed")
        body_segment = markdown[positions[0] + len(BODY_START):positions[1]]
        response_segment = markdown[positions[2] + len(RESPONSE_START):positions[3]]
        if not (body_segment.startswith("\n") and body_segment.endswith("\n")):
            raise TaskFormatError("body marker boundaries require structural newlines")
        if not (response_segment.startswith("\n") and response_segment.endswith("\n")):
            raise TaskFormatError("response marker boundaries require structural newlines")
        # Remove exactly the codec-owned separator newlines; user newlines remain exact.
        body = body_segment[1:-1]
        response = response_segment[1:-1]
        return yaml_text, body, response

    def loads(self, text: str) -> MutableMapping[str, Any]:
        yaml_text, body, response = self._split(text)
        # Let ruamel distinguish tags from quoted scalar content. Any custom-tag
        # object it constructs is rejected below as non-JSON-compatible.
        try:
            metadata = self._yaml().load(yaml_text) or CommentedMap()
        except YAMLError as exc:
            raise TaskFormatError(f"invalid YAML front matter: {exc}") from exc
        if not isinstance(metadata, MutableMapping):
            raise TaskFormatError("YAML front matter must be a map")
        try:
            _json_compatible(metadata)
        except RecursionError as exc:
            raise TaskFormatError("recursive YAML aliases are not JSON-compatible") from exc
        if metadata.get("schema_version") != 1:
            raise TaskFormatError("unsupported or missing schema_version")
        if not isinstance(metadata.get("id"), str):
            raise TaskFormatError("id must be a string")
        for key in ("created_date", "last_modified"):
            value = metadata.get(key)
            try:
                parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except (TypeError, ValueError) as exc:
                raise TaskFormatError(f"{key} must be ISO-8601") from exc
            if parsed.tzinfo is None:
                raise TaskFormatError(f"{key} must be timezone-aware")
        metadata["body"] = body
        metadata["agent_response"] = response
        return order_task_fields(metadata)

    def dumps(self, record: Mapping[str, Any]) -> str:
        # Work on an ordered copy: callers may hold ruamel nodes used for direct
        # edit reconciliation, and serialization must not mutate their key order.
        complete = record.copy() if isinstance(record, CommentedMap) else dict(record)
        complete.setdefault("schema_version", 1)
        body = str(complete.pop("body", ""))
        response = str(complete.pop("agent_response", ""))
        metadata = order_task_fields(complete)
        _json_compatible(metadata)
        stream = io.StringIO()
        self._yaml().dump(metadata, stream)
        yaml_text = stream.getvalue().rstrip("\n")
        return (
            f"---\n{yaml_text}\n---\n\n{BODY_START}\n{body}\n{BODY_END}\n\n"
            f"{RESPONSE_START}\n{response}\n{RESPONSE_END}\n"
        )


def plain_value(value: Any) -> Any:
    """Convert round-trip YAML values to deterministic JSON-compatible values."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): plain_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [plain_value(v) for v in value]
    return value


def normalized_bytes(record: Mapping[str, Any]) -> bytes:
    return json.dumps(plain_value(record), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
