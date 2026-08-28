"""Safe content-addressed objects used by immutable Artifact records."""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Optional

from .records import RecordError


SHA256_HEX_LENGTH = 64


def sha256_bytes(content: bytes) -> str:
    if not isinstance(content, bytes):
        raise RecordError("ARTIFACT_BYTES_INVALID", "artifact content must be bytes")
    return hashlib.sha256(content).hexdigest()


class ContentObjectStore:
    """A local SHA-256 store which never follows links or replaces an object."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def relative_path(self, digest: str) -> str:
        self._validate_digest(digest)
        return f"objects/sha256/{digest[:2]}/{digest}"

    def path(self, digest: str) -> Path:
        return self.root / self.relative_path(digest)

    @staticmethod
    def _validate_digest(digest: str) -> None:
        if (not isinstance(digest, str) or len(digest) != SHA256_HEX_LENGTH
                or any(char not in "0123456789abcdef" for char in digest)):
            raise RecordError("ARTIFACT_DIGEST_INVALID", "SHA-256 digest must be 64 lowercase hex characters")

    def _assert_safe_path(self, path: Path) -> None:
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise RecordError("ARTIFACT_PATH_UNSAFE", "object path escapes the object root") from exc
        current = self.root
        for part in path.relative_to(self.root).parts:
            current = current / part
            if current.is_symlink():
                raise RecordError("ARTIFACT_PATH_UNSAFE", "object path contains a symlink")

    def verify(self, digest: str, *, size: Optional[int] = None) -> bytes:
        path = self.path(digest)
        self._assert_safe_path(path)
        if not path.is_file():
            raise RecordError("ARTIFACT_OBJECT_MISSING", f"content object {digest} is missing")
        content = path.read_bytes()
        if size is not None and len(content) != size:
            raise RecordError("ARTIFACT_SIZE_MISMATCH", "content object size differs from its manifest")
        if sha256_bytes(content) != digest:
            raise RecordError("ARTIFACT_DIGEST_MISMATCH", "content object digest differs from its path")
        return content

    def put(self, content: bytes, *, claimed_digest: Optional[str] = None,
            claimed_size: Optional[int] = None) -> tuple[str, bool]:
        """Store bytes, returning ``(digest, created)``; identical bytes deduplicate."""
        digest = sha256_bytes(content)
        if claimed_digest is not None:
            self._validate_digest(claimed_digest)
            if claimed_digest != digest:
                raise RecordError("ARTIFACT_DIGEST_MISMATCH", "provided bytes do not match the claimed digest")
        if claimed_size is not None and claimed_size != len(content):
            raise RecordError("ARTIFACT_SIZE_MISMATCH", "provided bytes do not match the claimed size")
        path = self.path(digest)
        self._assert_safe_path(path)
        if path.exists() or path.is_symlink():
            self.verify(digest, size=len(content))
            return digest, False
        path.parent.mkdir(parents=True, exist_ok=True)
        self._assert_safe_path(path)
        fd, temporary = tempfile.mkstemp(prefix=".object-", dir=str(path.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            # A concurrent writer may win. Never replace its bytes silently.
            try:
                os.link(temporary, path)
                created = True
            except FileExistsError:
                self.verify(digest, size=len(content))
                created = False
            return digest, created
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def remove_if_unreferenced_creation(self, digest: str) -> None:
        """Rollback a just-created object. Callers must hold their mutation lock."""
        path = self.path(digest)
        self._assert_safe_path(path)
        try:
            path.unlink()
        except FileNotFoundError:
            return
