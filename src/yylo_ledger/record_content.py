"""Bounded universal read projections; never fetch remote payloads."""
import base64
import copy
import hashlib

from .records import RecordError

DEFAULT_CONTENT_BYTES = 64 * 1024
MAX_CONTENT_BYTES = 16 * 1024 * 1024


def readable(media_type):
    media_type = media_type.split(";", 1)[0].strip().lower()
    return media_type.startswith("text/") or media_type in (
        "application/json", "application/yaml", "application/x-yaml", "application/xml")


def project_content(record, artifacts, *, explicit=False, max_bytes=DEFAULT_CONTENT_BYTES):
    """Return a copy and optional exact bytes. Explicit reads may include binaries."""
    if not 1 <= max_bytes <= MAX_CONTENT_BYTES:
        raise RecordError("CONTENT_LIMIT_INVALID", f"byte limit must be 1..{MAX_CONTENT_BYTES}")
    value = copy.deepcopy(record)
    payload = value["payload"]
    backend = payload.get("backend")
    text = record.get("body") if record["kind"] == "task" else payload.get("text")
    content = None
    reason = None
    if isinstance(text, str):
        content = text.encode("utf-8")
        if len(content) > max_bytes:
            content, reason = None, "too_large"
            payload.pop("text", None)
    elif backend not in ("inline", "local"):
        reason = "external_reference"
    elif not explicit and not readable(record["media_type"]):
        reason = "binary"
    elif payload["size"] > max_bytes:
        reason = "too_large"
    elif backend == "local":
        if payload.get("path") != artifacts.objects.relative_path(payload["sha256"]):
            raise RecordError("ARTIFACT_PATH_UNSAFE", "local object path is not digest-derived")
        content = artifacts.objects.verify(payload["sha256"], size=payload["size"], max_bytes=max_bytes)
    else:
        data = payload.get("data", "")
        if len(data) > 4 * ((max_bytes + 2) // 3):
            raise RecordError("CONTENT_TOO_LARGE", "encoded payload exceeds byte limit")
        try:
            content = base64.b64decode(data, validate=True)
        except (ValueError, TypeError) as exc:
            raise RecordError("ARTIFACT_ENCODING_INVALID", "invalid inline base64 payload") from exc
        if len(content) != payload["size"]:
            raise RecordError("ARTIFACT_SIZE_MISMATCH", "payload bytes differ from manifest size")
        if hashlib.sha256(content).hexdigest() != payload["sha256"]:
            raise RecordError("ARTIFACT_DIGEST_MISMATCH", "payload bytes differ from manifest digest")
    # Metadata reads never spill base64 or a partial payload presented as complete.
    payload.pop("data", None)
    if explicit:
        if content is None:
            code = "CONTENT_TOO_LARGE" if reason == "too_large" else "CONTENT_UNAVAILABLE"
            raise RecordError(code, "payload requires a larger bound or explicit external retrieval")
        return value, content
    if content is not None:
        try:
            value["content"] = {"status": "included", "text": content.decode("utf-8"), "size": len(content)}
        except UnicodeDecodeError:
            reason = "non_utf8"
    if reason:
        value["content"] = {"status": "omitted", "reason": reason,
                            "command": f"get {record['id']} --content --max-content-bytes {MAX_CONTENT_BYTES}"}
    return value, None
