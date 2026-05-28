"""Opaque base64 / JSON pagination cursors -- the single canonical codec.

A cursor is an opaque, URL-safe base64-encoded JSON object carrying the
sort-key values of the last row on the previous page. Keyset pagination
compares against those values instead of OFFSET, so it stays fast at any
depth and never skips/duplicates rows when items are inserted or removed
between requests (e.g. the followers screen's "remove" action).

Both helpers are deliberately defensive: a malformed / legacy / garbage
cursor decodes to {} ("start from the beginning") rather than raising, so a
stale client token can never 500 an endpoint.

Consolidated here from the former duplicate copies in api/utils.py and
api/feed/cursors.py (audit D-BE1). decode_cursor restores base64 padding, so
it transparently decodes both padded (legacy utils-style) and unpadded tokens.
"""
from __future__ import annotations

import base64
import json
from typing import Optional


def encode_cursor(payload: dict) -> str:
    """JSON -> bytes -> urlsafe base64 (padding stripped) -> ascii string."""
    raw = json.dumps(payload, default=str, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_cursor(token: Optional[str]) -> dict:
    """Inverse of encode_cursor. Returns {} on missing / malformed input."""
    if not token:
        return {}
    try:
        # urlsafe_b64encode strips padding; restore it before decoding. A
        # properly-padded legacy token has len % 4 == 0, so this adds nothing
        # and decodes it unchanged.
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            return {}
        return data
    except (ValueError, TypeError, json.JSONDecodeError):
        return {}
