"""Declarative native Record profiles.

Profiles are data, not plugins: registering one never imports or invokes executable
workflow behavior.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Iterable, Mapping, Optional, Tuple

from .records import RecordError

SchemaValidator = Callable[[object], None]


@dataclass(frozen=True)
class RecordProfile:
    name: str
    native_kind: str
    media_types: Tuple[str, ...]
    schema_ref: Optional[str]
    renderer: str
    indexed_fields: Tuple[str, ...]
    mutable: bool
    allowed_operations: Tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.name or self.native_kind not in {"task", "document", "artifact"}:
            raise RecordError("PROFILE_INVALID", "profile name and native kind are required")
        if not self.media_types or any(not isinstance(value, str) or not value for value in self.media_types):
            raise RecordError("PROFILE_INVALID", "at least one accepted media type is required")
        if not self.renderer:
            raise RecordError("PROFILE_INVALID", "renderer identity is required")
        permitted = {"create", "get", "list", "search", "update", "history", "archive", "render"}
        if not set(self.allowed_operations).issubset(permitted):
            raise RecordError("PROFILE_INVALID", "profile declares an unsupported operation")
        if not self.mutable and "update" in self.allowed_operations:
            raise RecordError("PROFILE_INVALID", "immutable profiles cannot allow update")


class ProfileRegistry:
    """A closed registry of declarations and optional pure schema validators."""

    def __init__(self, profiles: Iterable[RecordProfile] = ()) -> None:
        self._profiles: Dict[str, RecordProfile] = {}
        self._schemas: Dict[str, SchemaValidator] = {}
        for profile in profiles:
            self.register(profile)

    def register(self, profile: RecordProfile) -> None:
        if profile.name in self._profiles:
            raise RecordError("PROFILE_DUPLICATE", "profile %r is already registered" % profile.name)
        self._profiles[profile.name] = profile

    def register_schema(self, schema_ref: str, validator: SchemaValidator) -> None:
        if not schema_ref or schema_ref in self._schemas:
            raise RecordError("SCHEMA_DUPLICATE", "schema reference is empty or already registered")
        self._schemas[schema_ref] = validator

    def get(self, name: str) -> RecordProfile:
        try:
            return self._profiles[name]
        except KeyError:
            raise RecordError("PROFILE_UNSUPPORTED", "unsupported profile %r" % name)

    def validate(self, *, profile: str, kind: str, media_type: str,
                 schema_ref: Optional[str], payload: object = None) -> RecordProfile:
        declaration = self.get(profile)
        if declaration.native_kind != kind:
            raise RecordError("PROFILE_KIND_MISMATCH", "profile does not belong to kind %r" % kind)
        if media_type not in declaration.media_types:
            raise RecordError("MEDIA_TYPE_UNSUPPORTED", "media type %r is not accepted" % media_type)
        if declaration.schema_ref != schema_ref:
            raise RecordError("SCHEMA_UNSUPPORTED", "schema reference %r is not accepted" % schema_ref)
        if schema_ref is not None:
            validator = self._schemas.get(schema_ref)
            if validator is None:
                raise RecordError("SCHEMA_UNSUPPORTED", "schema %r has no registered validator" % schema_ref)
            validator(payload)
        return declaration

    def declarations(self) -> Mapping[str, RecordProfile]:
        return dict(self._profiles)


WIKI_PROFILE = RecordProfile(
    name="wiki", native_kind="document", media_types=("text/markdown",),
    schema_ref=None, renderer="markdown-safe-v1",
    indexed_fields=("title", "namespace", "slug", "aliases", "relations"),
    mutable=True,
    allowed_operations=("create", "get", "list", "search", "update", "history", "archive", "render"),
)
WORKFLOW_SCHEMA_V1 = "https://yylo.dev/schemas/workflow/v1"
WORKFLOW_PROFILE = RecordProfile(
    name="workflow", native_kind="document",
    media_types=("application/yaml", "application/x-yaml"),
    schema_ref=WORKFLOW_SCHEMA_V1, renderer="yaml-source-v1",
    indexed_fields=("title", "namespace", "slug", "aliases", "relations", "payload.sha256"),
    mutable=True,
    allowed_operations=("create", "get", "list", "search", "update", "history", "archive", "render"),
)


def default_profile_registry() -> ProfileRegistry:
    # Imported lazily to keep the profile declarations independent of codecs.
    from .workflow_yaml import validate_workflow_v1
    registry = ProfileRegistry((WIKI_PROFILE, WORKFLOW_PROFILE))
    registry.register_schema(WORKFLOW_SCHEMA_V1, validate_workflow_v1)
    return registry
