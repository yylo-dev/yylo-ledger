"""Immutable Record identity grammar, independent of storage and profiles."""
import re
import secrets
import string

PREFIX_KINDS = {"task": "task", "doc": "document", "artifact": "artifact"}
SUFFIX_PATTERN = r"(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*[0-9])[A-Za-z0-9]{6}"
RECORD_ID_RE = re.compile(r"^(?:(?:task|doc|artifact)_)?" + SUFFIX_PATTERN + r"$")
TASK_ID_RE = re.compile(r"^(?:task_)?" + SUFFIX_PATTERN + r"$")


def identity_kind(identity: str):
    """Return a prefix's kind only for a complete canonical ID (not a slug)."""
    if RECORD_ID_RE.fullmatch(identity) and "_" in identity:
        return PREFIX_KINDS[identity.split("_", 1)[0]]
    return None


def new_id(kind: str) -> str:
    prefix = {value: key for key, value in PREFIX_KINDS.items()}[kind]
    while True:
        suffix = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(6))
        if RECORD_ID_RE.fullmatch(suffix):
            return prefix + "_" + suffix
