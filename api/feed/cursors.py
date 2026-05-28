"""Feed pagination helpers. The generic cursor codec now lives in
services.pagination (audit D-BE1); this module re-exports it and adds the
feed-specific bounded-int offset clamp."""
from __future__ import annotations

from ..services.pagination import decode_cursor, encode_cursor
from .constants import MAX_OFFSET

__all__ = ["encode_cursor", "decode_cursor", "_bounded_int"]


def _bounded_int(v, lo: int = 0, hi: int = MAX_OFFSET) -> int:
    """Coerce a cursor field to a bounded non-negative int."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(n, hi))
