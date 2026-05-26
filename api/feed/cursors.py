"""Opaque base64/JSON pagination cursor encode/decode and bounded-int coercion."""
from __future__ import annotations

import base64
import json
from typing import Optional

from .constants import MAX_OFFSET

# ---------------------------------------------------------------------------
# Cursor: base64-encoded JSON. Opaque to clients; validated server-side.
# ---------------------------------------------------------------------------

def encode_cursor(payload: dict) -> str:
    """JSON → bytes → urlsafe base64 → ascii string."""
    raw = json.dumps(payload, default=str, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")



def decode_cursor(token: Optional[str]) -> dict:
    """
    Inverse of encode_cursor. Returns an empty dict on missing / malformed
    input so the caller never has to wrap this in try/except. Server-side
    validation (offset bounds, timestamp parsing) happens in the rail code.
    """
    if not token:
        return {}
    try:
        # urlsafe_b64encode strips padding; restore it before decoding.
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            return {}
        return data
    except (ValueError, TypeError, json.JSONDecodeError):
        return {}



def _bounded_int(v, lo: int = 0, hi: int = MAX_OFFSET) -> int:
    """Coerce a cursor field to a bounded non-negative int."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(n, hi))
