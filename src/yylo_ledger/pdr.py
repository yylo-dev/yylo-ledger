"""Validated, explicit task bindings to immutable revisions of revisable PDRs."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from .documents import DocumentStore
from .records import RECORD_ID_RE, RecordError


def validate_pdr_binding(root: Path, candidate: Mapping, previous: Mapping | None = None) -> None:
    """Validate adoption before task writes, not the latest PDR on every status update.

    Revisions are immutable. An unchanged accepted binding remains historical
    truth even after the PDR is updated or archived. No implicit adoption occurs.
    """
    fields = candidate.get("fields") or {}
    old_fields = (previous or {}).get("fields") or {}
    if "pdr_binding" not in fields:
        return
    binding = fields["pdr_binding"]
    if previous is not None and "pdr_binding" in old_fields and binding == old_fields["pdr_binding"]:
        return
    if (not isinstance(binding, Mapping)
            or set(binding) != {"record_id", "revision", "payload_sha256"}
            or not isinstance(binding["record_id"], str)
            or not RECORD_ID_RE.fullmatch(binding["record_id"])
            or type(binding["revision"]) is not int or binding["revision"] < 1):
        raise RecordError("PDR_BINDING_INVALID", "pdr_binding requires an immutable record_id, positive revision and payload_sha256")
    record = DocumentStore(root).get(binding["record_id"], binding["revision"])
    if record["profile"] != "pdr":
        raise RecordError("PDR_PROFILE_REQUIRED", "task requirements must reference a PDR Document")
    if binding["payload_sha256"] != record["payload"]["sha256"]:
        raise RecordError("PDR_DIGEST_MISMATCH", "approved PDR revision digest differs")
