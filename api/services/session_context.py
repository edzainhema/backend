"""
Per-request session-id capture (C3).

The client sends a per-app-launch UUID in the ``X-Session-Id`` header.
``SessionIdMiddleware`` stashes it here for the life of the request, and
``log_activity`` (plus the impression logger) read it via
``get_current_session_id()`` so every Activity row from one visit shares an id
— without threading the value through every view signature.

We use ``asgiref.local.Local`` (the same request-local primitive Django uses
internally) so the value is correctly isolated per request under both WSGI
worker threads and ASGI tasks, and never leaks between concurrent requests.
"""

import re

from asgiref.local import Local

_local = Local()

# Accept only opaque UUID-ish tokens, length-bounded to the column width, so a
# hostile or buggy client can't stuff arbitrary / oversized data into the
# indexed session_id column.
_SESSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def sanitize_session_id(raw) -> str:
    """Return a safe session id, or "" if the input isn't a valid token."""
    if not isinstance(raw, str):
        return ""
    raw = raw.strip()
    return raw if _SESSION_RE.match(raw) else ""


def set_current_session_id(value: str) -> None:
    _local.session_id = value or ""


def clear_current_session_id() -> None:
    try:
        del _local.session_id
    except AttributeError:
        pass


def get_current_session_id() -> str:
    return getattr(_local, "session_id", "")
