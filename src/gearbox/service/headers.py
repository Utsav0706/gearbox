"""Response-header helpers shared by both endpoints."""

from __future__ import annotations

REASON_HEADER_CHARS = 200


def header_safe(text: str, limit: int = REASON_HEADER_CHARS) -> str:
    """HTTP header values must be plain printable ASCII on one line. The router's reason
    can be anything (v2's comes from a model), so non-ASCII becomes '?', line breaks and
    control characters become spaces, and it is cut to `limit` characters."""
    ascii_text = text.encode("ascii", "replace").decode("ascii")
    one_line = "".join(c if c.isprintable() else " " for c in ascii_text)
    return " ".join(one_line.split())[:limit]
