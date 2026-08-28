"""Read-only HTTP projections for canonical YYLO Ledger Records.

The host deliberately reads canonical files and verified archive packs directly.
It never consults or rebuilds disposable query caches and it has no mutation or
remote-fetch path.
"""
from __future__ import annotations

import base64
import email.utils
import hashlib
import html
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple
from urllib.parse import unquote, urlparse

from .archive import ArchiveFormatError, iter_archive_envelopes
from .artifacts import ArtifactStore
from .documents import DocumentStore, validate_document
from .records import RecordError, task_record_projection, validate_record, value_digest

_RECORD_JSON = "application/vnd.yylo.record+json"
_JSON_TYPES = {_RECORD_JSON, "application/json"}
_YAML_TYPES = {"application/yaml", "application/x-yaml", "text/yaml"}
_MEDIA_TYPE_RE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
_SENSITIVE_CLASSIFICATIONS = {"secret", "restricted"}
_ACTIVE_MEDIA_TYPES = {
    "text/html", "application/xhtml+xml", "image/svg+xml",
    "application/javascript", "text/javascript",
}


class HostingError(Exception):
    """Stable refusal which is safe to serialize to an HTTP client."""

    def __init__(self, status: int, code: str, message: str, record_id: Optional[str] = None):
        super().__init__(message)
        self.status, self.code, self.message, self.record_id = status, code, message, record_id


@dataclass(frozen=True)
class HostPolicy:
    access: str = "local"
    allowed_redirect_hosts: Tuple[str, ...] = ()
    max_output_bytes: int = 1024 * 1024
    max_range_bytes: int = 256 * 1024

    def __post_init__(self) -> None:
        if self.access not in ("local", "private"):
            raise ValueError("access policy must be local or private")
        if self.max_output_bytes <= 0 or self.max_range_bytes <= 0:
            raise ValueError("hosting output bounds must be positive")
        for host in self.allowed_redirect_hosts:
            if not host or "/" in host or "@" in host or ":" in host:
                raise ValueError("redirect allowlist entries must be hostnames")

    def validate_bind(self, host: str) -> None:
        if self.access == "local" and host.casefold() not in {
            "127.0.0.1", "::1", "localhost"
        }:
            raise ValueError("local access policy requires a loopback host")


@dataclass(frozen=True)
class HostedResponse:
    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""


@dataclass(frozen=True)
class ResolvedRecord:
    record: Mapping[str, Any]
    cold_envelope: Optional[Mapping[str, Any]] = None


class CanonicalRecordReader:
    """Cache-free canonical reader shared by every hosted route."""

    def __init__(self, storage: Any):
        self.storage = storage
        self.root = Path(storage.juno_root)
        self.documents = DocumentStore(self.root)
        self.artifacts = ArtifactStore(self.root)

    def _reject_symlinks(self) -> None:
        # These are the only trees from which hosting serves bytes.  Rejecting a
        # link anywhere in them avoids both leaf and parent-directory escape.
        for root in (
            self.storage.tasks_root, self.root / "ledger", self.root / "documents",
            self.root / "document-ledger", self.root / "artifacts",
            self.root / "artifact-ledger", self.root / "objects", self.root / "archive",
        ):
            if root.is_symlink():
                raise HostingError(409, "PATH_UNSAFE", "canonical storage contains a symlink")
            if not root.exists():
                continue
            for current, directories, files in os.walk(str(root), followlinks=False):
                base = Path(current)
                if any((base / name).is_symlink() for name in (*directories, *files)):
                    raise HostingError(409, "PATH_UNSAFE", "canonical storage contains a symlink")

    def _validate_artifact(self, value: Mapping[str, Any]) -> None:
        validate_record(value)
        self.artifacts.validate_payload(value["payload"])
        payload = value["payload"]
        media_type = payload.get("media_type")
        if not isinstance(media_type, str) or not _MEDIA_TYPE_RE.fullmatch(media_type):
            raise RecordError("ARTIFACT_MEDIA_TYPE_INVALID", "Artifact media type is invalid")
        mode = payload["backend"]
        if mode == "inline":
            if payload.get("encoding") != "base64":
                raise RecordError("ARTIFACT_ENCODING_INVALID", "inline Artifact encoding is invalid")
            try:
                content = base64.b64decode(payload.get("data", ""), validate=True)
            except (ValueError, TypeError) as exc:
                raise RecordError("ARTIFACT_ENCODING_INVALID", "inline Artifact encoding is invalid") from exc
            if len(content) != payload["size"] or hashlib.sha256(content).hexdigest() != payload["sha256"]:
                raise RecordError("ARTIFACT_DIGEST_MISMATCH", "inline Artifact bytes do not match the manifest")
        elif mode == "local":
            if payload.get("path") != self.artifacts.objects.relative_path(payload["sha256"]):
                raise RecordError("ARTIFACT_PATH_UNSAFE", "local Artifact path is not digest-derived")
            self.artifacts.objects.verify(payload["sha256"], size=payload["size"])
        else:
            uri = payload.get("uri")
            parsed = urlparse(uri if isinstance(uri, str) else "")
            schemes = {"https", "s3", "gs"} if mode == "external" else {"https", "http", "s3", "gs"}
            if (parsed.scheme.casefold() not in schemes or parsed.username is not None
                    or parsed.password is not None or any(part == ".." for part in parsed.path.split("/"))
                    or (parsed.scheme in ("http", "https") and not parsed.hostname)):
                raise RecordError("ARTIFACT_URI_INVALID", "Artifact URI is invalid")

    def _hot(self) -> Iterable[ResolvedRecord]:
        for path in sorted(self.storage.tasks_root.glob("*/*.md")):
            record = task_record_projection(self.storage._read_path(path))
            validate_record(record)
            yield ResolvedRecord(record)
        for store, validator in (
            (self.documents, validate_document),
            (self.artifacts, self._validate_artifact),
        ):
            for directory in sorted(store.records_root.glob("*/*")):
                revisions = sorted(directory.glob("*.json"))
                if not revisions:
                    continue
                try:
                    value = json.loads(revisions[-1].read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise HostingError(409, "RECORD_INTEGRITY_FAILED", "canonical Record cannot be verified") from exc
                try:
                    validator(value)
                except (RecordError, ValueError, TypeError) as exc:
                    raise HostingError(409, "RECORD_INTEGRITY_FAILED", "canonical Record cannot be verified") from exc
                yield ResolvedRecord(value)

    def records(self) -> Sequence[ResolvedRecord]:
        self._reject_symlinks()
        values = list(self._hot())
        try:
            for envelope in iter_archive_envelopes(self.root):
                record = envelope["task"]
                if record.get("kind") == "document":
                    validate_document(record)
                elif record.get("kind") == "artifact":
                    self._validate_artifact(record)
                else:
                    validate_record(record)
                values.append(ResolvedRecord(record, envelope))
        except (ArchiveFormatError, RecordError, OSError, ValueError, TypeError) as exc:
            raise HostingError(409, "COLD_RECORD_INTEGRITY_FAILED", "cold Record integrity verification failed") from exc
        ids: Dict[str, int] = {}
        for item in values:
            record_id = str(item.record.get("id", "")).casefold()
            ids[record_id] = ids.get(record_id, 0) + 1
        if any(count != 1 for count in ids.values()):
            raise HostingError(409, "RECORD_TIER_CONFLICT", "Record identity exists in more than one canonical tier")
        return values

    @staticmethod
    def _matches_route(record: Mapping[str, Any], route: str) -> bool:
        return (
            route == "record"
            or (route == "wiki" and record.get("kind") == "document" and record.get("profile") == "wiki")
            or (route == "workflow" and record.get("kind") == "document" and record.get("profile") == "workflow")
            or (route == "artifact" and record.get("kind") == "artifact")
        )

    def resolve(self, identity: str, route: str) -> ResolvedRecord:
        matches = []
        for item in self.records():
            value = item.record
            identities = [value.get("id"), value.get("slug"), *(value.get("aliases") or [])]
            if identity in identities and self._matches_route(value, route):
                matches.append(item)
        if not matches:
            raise HostingError(404, "RECORD_NOT_FOUND", "Record does not exist")
        if len(matches) != 1:
            raise HostingError(409, "RECORD_IDENTITY_AMBIGUOUS", "Record identity is ambiguous")
        return matches[0]

    def history(self, resolved: ResolvedRecord) -> Sequence[Mapping[str, Any]]:
        record = resolved.record
        if resolved.cold_envelope is not None:
            return list(resolved.cold_envelope.get("ledger") or [])
        record_id, kind = record["id"], record["kind"]
        if kind == "task":
            return list(self.storage.ledger.read(record_id))
        root = (self.documents.events_root if kind == "document" else self.artifacts.events_root)
        directory = root / record_id[:2].lower() / record_id
        events = []
        for path in sorted(directory.glob("*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise HostingError(409, "HISTORY_INTEGRITY_FAILED", "Record history cannot be verified", record_id) from exc
            if not isinstance(value, Mapping):
                raise HostingError(409, "HISTORY_INTEGRITY_FAILED", "Record history cannot be verified", record_id)
            events.append(value)
        return events


def _sensitive(record: Mapping[str, Any]) -> bool:
    system = record.get("system_metadata") or {}
    custom = record.get("custom_metadata") or {}
    return bool(system.get("sensitive") or custom.get("sensitive") or
                str(system.get("classification", "")).casefold() in _SENSITIVE_CLASSIFICATIONS)


def _accept(headers: Mapping[str, str]) -> Sequence[str]:
    raw = headers.get("accept", "*/*")
    result = []
    for item in raw.split(","):
        parts = [part.strip() for part in item.split(";")]
        media = parts[0].casefold()
        if media != "*/*" and not _MEDIA_TYPE_RE.fullmatch(media):
            raise HostingError(400, "ACCEPT_INVALID", "Accept header is malformed")
        quality = 1.0
        for parameter in parts[1:]:
            if parameter.startswith("q="):
                try:
                    quality = float(parameter[2:])
                except ValueError as exc:
                    raise HostingError(400, "ACCEPT_INVALID", "Accept header is malformed") from exc
                if not 0 <= quality <= 1:
                    raise HostingError(400, "ACCEPT_INVALID", "Accept header is malformed")
        if quality > 0:
            result.append(media)
    return result


def _safe_markdown(source: str) -> str:
    """Render a small inert Markdown subset; source HTML is always escaped."""
    output, paragraph = [], []
    in_code = False

    def flush() -> None:
        if paragraph:
            output.append("<p>" + "<br>".join(paragraph) + "</p>")
            paragraph.clear()

    for raw in source.splitlines():
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
            flush()
            level = len(heading.group(1))
            output.append(f"<h{level}>{html.escape(heading.group(2), quote=True)}</h{level}>")
        elif raw:
            paragraph.append(escaped)
        else:
            flush()
    flush()
    if in_code:
        output.append("</code></pre>")
    return "\n".join(output)


def _json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _last_modified(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return email.utils.format_datetime(parsed.astimezone(timezone.utc), usegmt=True)


class HostingApplication:
    """Pure request dispatcher used by the network adapter and focused tests."""

    def __init__(self, storage: Any, policy: Optional[HostPolicy] = None):
        self.reader = CanonicalRecordReader(storage)
        self.policy = policy or HostPolicy()

    def _error(self, error: HostingError) -> HostedResponse:
        value: Dict[str, Any] = {"error": {"code": error.code, "message": error.message}}
        if error.record_id:
            value["error"]["id"] = error.record_id
        return HostedResponse(error.status, {
            "Content-Type": "application/problem+json; charset=utf-8",
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        }, _json(value))

    def _bounded(self, body: bytes, record_id: str) -> None:
        if len(body) > self.policy.max_output_bytes:
            raise HostingError(413, "OUTPUT_BOUND_EXCEEDED", "hosted output exceeds the configured bound", record_id)

    @staticmethod
    def _source(record: Mapping[str, Any]) -> Tuple[str, str]:
        if record["kind"] == "task":
            return str(record.get("body") or ""), "text/markdown"
        payload = record.get("payload") or {}
        if record["kind"] == "document" and isinstance(payload.get("text"), str):
            return payload["text"], ("text/markdown" if record.get("profile") == "wiki" else "application/yaml")
        raise HostingError(406, "REPRESENTATION_UNAVAILABLE", "raw text is unavailable", record["id"])

    def _base_headers(self, record: Mapping[str, Any], etag: str) -> Dict[str, str]:
        headers = {
            "ETag": f'"{etag}"', "Cache-Control": "private, max-age=0, must-revalidate",
            "Link": f'</record/{record["id"]}>; rel="canonical"',
            "Content-Location": f'/record/{record["id"]}',
            "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
        }
        modified = _last_modified(record.get("last_modified"))
        if modified:
            headers["Last-Modified"] = modified
        return headers

    def _record_response(self, record: Mapping[str, Any], accepts: Sequence[str]) -> HostedResponse:
        etag = value_digest(record)
        headers = self._base_headers(record, etag)
        if "text/html" in accepts:
            source, _ = self._source(record)
            title = html.escape(str(record.get("title") or record["id"]), quote=True)
            rendered = _safe_markdown(source)
            body = ("<!doctype html><html><head><meta charset=\"utf-8\">"
                    f'<link rel="canonical" href="/record/{record["id"]}"><title>{title}</title>'
                    f"</head><body><main>{rendered}</main></body></html>\n").encode("utf-8")
            headers.update({"Content-Type": "text/html; charset=utf-8",
                            "Content-Security-Policy": "default-src 'none'; style-src 'none'; img-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"})
        elif any(media in accepts for media in ("text/markdown", "text/plain", *_YAML_TYPES)):
            source, media = self._source(record)
            requested = next((item for item in accepts if item in _YAML_TYPES or item == "text/markdown"), media)
            if record.get("profile") == "workflow" and requested not in _YAML_TYPES and requested != "text/plain":
                raise HostingError(406, "REPRESENTATION_UNAVAILABLE", "requested representation is unavailable", record["id"])
            body = source.encode("utf-8")
            headers["Content-Type"] = f"{requested if requested != 'text/plain' else 'text/plain'}; charset=utf-8"
        elif "*/*" in accepts or any(media in accepts for media in _JSON_TYPES):
            body = _json(record)
            headers["Content-Type"] = f"{_RECORD_JSON}; charset=utf-8"
        else:
            raise HostingError(406, "REPRESENTATION_UNAVAILABLE", "requested representation is unavailable", record["id"])
        self._bounded(body, record["id"])
        return HostedResponse(200, headers, body)

    def _artifact_bytes(self, record: Mapping[str, Any]) -> Optional[bytes]:
        payload = record["payload"]
        mode = payload["backend"]
        if mode == "inline":
            try:
                content = base64.b64decode(payload.get("data", ""), validate=True)
            except (ValueError, TypeError) as exc:
                raise HostingError(409, "ARTIFACT_ENCODING_INVALID", "Artifact bytes cannot be verified", record["id"]) from exc
            if hashlib.sha256(content).hexdigest() != payload["sha256"] or len(content) != payload["size"]:
                raise HostingError(409, "ARTIFACT_INTEGRITY_FAILED", "Artifact bytes cannot be verified", record["id"])
            return content
        if mode == "local":
            try:
                return self.reader.artifacts.verify(record["id"])
            except RecordError as exc:
                raise HostingError(409, "ARTIFACT_INTEGRITY_FAILED", "Artifact bytes cannot be verified", record["id"]) from exc
        return None

    def _redirect(self, record: Mapping[str, Any]) -> HostedResponse:
        uri = record["payload"].get("uri")
        parsed = urlparse(uri or "")
        allowed_hosts = {host.casefold() for host in self.policy.allowed_redirect_hosts}
        decoded_path = unquote(parsed.path)
        if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
                or parsed.hostname is None or parsed.hostname.casefold() not in allowed_hosts
                or any(ord(char) < 32 for char in (uri or "")) or "\\" in (uri or "")
                or any(part == ".." for part in decoded_path.split("/"))):
            raise HostingError(403, "REDIRECT_NOT_APPROVED", "external redirect is not approved", record["id"])
        return HostedResponse(307, {
            **self._base_headers(record, value_digest(record)), "Location": uri,
            "Content-Type": "application/problem+json; charset=utf-8",
        }, _json({"redirect": {"id": record["id"]}}))

    def _artifact_response(self, record: Mapping[str, Any], action: str,
                           accepts: Sequence[str], headers_in: Mapping[str, str]) -> HostedResponse:
        if action == "manifest" or (action == "" and ("*/*" in accepts or any(value in accepts for value in _JSON_TYPES))):
            return self._record_response(record, (_RECORD_JSON,))
        if action == "download" and record["payload"]["backend"] in ("external", "link"):
            return self._redirect(record)
        content = self._artifact_bytes(record)
        if content is None:
            raise HostingError(409, "ARTIFACT_CONTENT_UNAVAILABLE", "Artifact content is not stored locally", record["id"])
        media_type = str(record["payload"].get("media_type", ""))
        if not _MEDIA_TYPE_RE.fullmatch(media_type):
            raise HostingError(409, "ARTIFACT_MEDIA_TYPE_INVALID", "Artifact media type is invalid", record["id"])
        if action != "download" and "*/*" not in accepts and media_type.casefold() not in accepts:
            raise HostingError(406, "REPRESENTATION_UNAVAILABLE", "requested representation is unavailable", record["id"])
        status, content_range = 200, None
        range_value = headers_in.get("range")
        if range_value:
            match = _RANGE_RE.fullmatch(range_value.strip())
            if match is None or not match.group(1):
                raise HostingError(416, "RANGE_INVALID", "only one bounded byte range is supported", record["id"])
            start = int(match.group(1)); end = int(match.group(2)) if match.group(2) else min(len(content) - 1, start + self.policy.max_range_bytes - 1)
            if start >= len(content) or end < start or end - start + 1 > self.policy.max_range_bytes:
                raise HostingError(416, "RANGE_INVALID", "requested byte range is invalid or unbounded", record["id"])
            content_range = f"bytes {start}-{end}/{len(content)}"
            content, status = content[start:end + 1], 206
        self._bounded(content, record["id"])
        response_headers = self._base_headers(record, record["payload"].get("sha256") or value_digest(record))
        active_media = media_type.casefold() in _ACTIVE_MEDIA_TYPES
        response_headers.update({
            "Content-Type": "application/octet-stream" if active_media else media_type,
            "Accept-Ranges": "bytes",
        })
        if content_range:
            response_headers["Content-Range"] = content_range
        if action == "download" or active_media:
            response_headers["Content-Disposition"] = f'attachment; filename="{record["id"]}"'
        return HostedResponse(status, response_headers, content)

    def dispatch(self, method: str, target: str, headers: Optional[Mapping[str, str]] = None) -> HostedResponse:
        headers_in = {str(key).casefold(): str(value) for key, value in (headers or {}).items()}
        try:
            if method not in ("GET", "HEAD"):
                raise HostingError(405, "METHOD_NOT_ALLOWED", "host is read-only")
            parsed = urlparse(target)
            if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
                raise HostingError(400, "ROUTE_INVALID", "route must not contain a query, fragment, or authority")
            if re.search(r"%(?![0-9A-Fa-f]{2})", parsed.path):
                raise HostingError(400, "ROUTE_INVALID", "route encoding is invalid")
            try:
                decoded = unquote(parsed.path, errors="strict")
            except UnicodeDecodeError as exc:
                raise HostingError(400, "ROUTE_INVALID", "route encoding is invalid") from exc
            if "\x00" in decoded or "\\" in decoded or any(part in ("", ".", "..") for part in decoded.split("/")[1:]):
                raise HostingError(400, "ROUTE_INVALID", "route is unsafe")
            parts = decoded.strip("/").split("/")
            if len(parts) < 2 or parts[0] not in ("record", "wiki", "workflow", "artifact"):
                raise HostingError(404, "ROUTE_NOT_FOUND", "route does not exist")
            route, identity = parts[0], parts[1]
            action = parts[2] if len(parts) == 3 else ""
            if len(parts) > 3 or action not in ("", "history", "manifest", "content", "download"):
                raise HostingError(404, "ROUTE_NOT_FOUND", "route does not exist")
            if route != "record" and action == "history":
                raise HostingError(404, "ROUTE_NOT_FOUND", "route does not exist")
            if route != "artifact" and action in ("manifest", "content", "download"):
                raise HostingError(404, "ROUTE_NOT_FOUND", "route does not exist")
            resolved = self.reader.resolve(identity, route)
            record = resolved.record
            if _sensitive(record):
                raise HostingError(403, "RECORD_RESTRICTED", "Record is restricted by hosting policy", record["id"])
            accepts = _accept(headers_in)
            if action == "history":
                body = _json({"id": record["id"], "history": self.reader.history(resolved)})
                self._bounded(body, record["id"])
                response = HostedResponse(200, {
                    **self._base_headers(record, hashlib.sha256(body).hexdigest()),
                    "Content-Type": "application/json; charset=utf-8",
                }, body)
            elif route == "artifact":
                response = self._artifact_response(record, action, accepts, headers_in)
            else:
                response = self._record_response(record, accepts)
            if headers_in.get("if-none-match") == response.headers.get("ETag") and response.status == 200:
                response = HostedResponse(304, response.headers, b"")
            if method == "HEAD":
                response = HostedResponse(response.status, response.headers, b"")
            return HostedResponse(response.status, {**response.headers, "Content-Length": str(len(response.body))}, response.body)
        except HostingError as exc:
            response = self._error(exc)
            if method == "HEAD":
                response = HostedResponse(response.status, response.headers, b"")
            return HostedResponse(response.status, {**response.headers, "Content-Length": str(len(response.body))}, response.body)
        except (RecordError, ArchiveFormatError, OSError, ValueError, TypeError) as exc:
            response = self._error(HostingError(409, "RECORD_INTEGRITY_FAILED", "canonical Record cannot be verified"))
            return HostedResponse(response.status, {**response.headers, "Content-Length": str(len(response.body))}, response.body)


class LedgerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: Tuple[str, int], application: HostingApplication):
        self.application = application
        super().__init__(address, LedgerRequestHandler)


class LedgerRequestHandler(BaseHTTPRequestHandler):
    server_version = "YYLOLedgerReadOnly/1"

    def _serve(self) -> None:
        response = self.server.application.dispatch(self.command, self.path, dict(self.headers.items()))  # type: ignore[attr-defined]
        self.send_response(response.status)
        for name, value in response.headers.items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD" and response.body:
            self.wfile.write(response.body)

    do_GET = _serve
    do_HEAD = _serve
    do_POST = _serve
    do_PUT = _serve
    do_PATCH = _serve
    do_DELETE = _serve

    def log_message(self, format: str, *args: Any) -> None:
        # Access logs contain only method/status, never identity or query text.
        print("yylo-ledger host: %s %s" % (self.command, args[1] if len(args) > 1 else ""), flush=True)


def serve(storage: Any, *, host: str = "127.0.0.1", port: int = 8765,
          policy: Optional[HostPolicy] = None) -> None:
    active_policy = policy or HostPolicy()
    active_policy.validate_bind(host)
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    server = LedgerHTTPServer((host, port), HostingApplication(storage, active_policy))
    address, actual_port = server.server_address[:2]
    print(json.dumps({"access_policy": active_policy.access, "host": address,
                      "port": actual_port, "read_only": True}, sort_keys=True), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
