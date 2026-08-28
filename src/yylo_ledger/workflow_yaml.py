"""Safe deterministic YAML handling for hosted workflow Documents.

This module only parses, validates and emits data.  It deliberately has no
subprocess, import hook, shell, runner, or callback execution surface.
"""
from __future__ import annotations

import io
import math
from datetime import date, datetime
from typing import Any, Mapping

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from ruamel.yaml.tokens import AliasToken, AnchorToken, TagToken

from .records import RecordError


def _yaml() -> YAML:
    parser = YAML(typ="safe", pure=True)
    parser.allow_duplicate_keys = False
    parser.default_flow_style = False
    parser.width = 4096
    parser.sort_base_mapping_type_on_output = False
    return parser


def _validate_json_data(value: Any, path: str = "$") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RecordError("WORKFLOW_YAML_UNSAFE", "non-finite number at %s" % path)
        return
    if isinstance(value, (date, datetime)):
        raise RecordError("WORKFLOW_YAML_UNSAFE", "implicit date/time scalar at %s must be quoted" % path)
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_data(item, "%s[%d]" % (path, index))
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise RecordError("WORKFLOW_YAML_UNSAFE", "mapping key at %s must be a string" % path)
            _validate_json_data(item, "%s.%s" % (path, key))
        return
    raise RecordError("WORKFLOW_YAML_UNSAFE", "unsupported YAML value at %s" % path)


def parse_workflow_yaml(text: str) -> Any:
    if not isinstance(text, str) or "\r" in text:
        raise RecordError("WORKFLOW_YAML_INVALID", "workflow YAML must be UTF-8 text with LF line endings")
    parser = _yaml()
    try:
        # Anchors/aliases can create recursive or surprising shared structures;
        # explicit tags can construct language-specific values. Neither belongs
        # in the portable Document data model.
        for token in parser.scan(text):
            if isinstance(token, (AliasToken, AnchorToken, TagToken)):
                raise RecordError("WORKFLOW_YAML_UNSAFE", "YAML aliases, anchors and explicit tags are forbidden")
        value = parser.load(text)
    except RecordError:
        raise
    except (YAMLError, ValueError) as exc:
        raise RecordError("WORKFLOW_YAML_INVALID", "invalid or duplicate-key YAML: %s" % exc)
    try:
        _validate_json_data(value)
    except RecursionError:
        raise RecordError("WORKFLOW_YAML_UNSAFE", "recursive YAML is forbidden")
    return value


def validate_workflow_v1(value: object) -> None:
    if not isinstance(value, Mapping):
        raise RecordError("WORKFLOW_SCHEMA_INVALID", "workflow v1 root must be a mapping")
    if value.get("schema_version") != "v1":
        raise RecordError("WORKFLOW_SCHEMA_INVALID", "workflow v1 requires schema_version: v1")
    if not isinstance(value.get("workflow_id"), str) or not value.get("workflow_id"):
        raise RecordError("WORKFLOW_SCHEMA_INVALID", "workflow v1 requires workflow_id")
    steps = value.get("steps")
    if not isinstance(steps, list):
        raise RecordError("WORKFLOW_SCHEMA_INVALID", "workflow v1 requires a steps list")
    seen = set()
    for index, step in enumerate(steps):
        if not isinstance(step, Mapping):
            raise RecordError("WORKFLOW_SCHEMA_INVALID", "step %d must be a mapping" % index)
        step_id = step.get("id")
        if not isinstance(step_id, str) or not step_id or step_id in seen:
            raise RecordError("WORKFLOW_SCHEMA_INVALID", "workflow step IDs must be non-empty and unique")
        seen.add(step_id)


def emit_workflow_yaml(value: object) -> str:
    _validate_json_data(value)
    stream = io.StringIO()
    _yaml().dump(value, stream)
    return stream.getvalue()


def normalize_workflow_yaml(text: str) -> str:
    return emit_workflow_yaml(parse_workflow_yaml(text))
