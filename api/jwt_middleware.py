from urllib.parse import parse_qs
from channels.db import database_sync_to_async

@database_sync_to_async
def get_user_from_token(token: str):
    # ALL Django imports must be lazy to avoid AppRegistryNotReady
    from django.contrib.auth.models import AnonymousUser
    from rest_framework_simplejwt.authentication import JWTAuthentication

    try:
        # Initialize JWT Authentication
        jwt_auth = JWTAuthentication()
        validated_token = jwt_auth.get_validated_token(token)
        user = jwt_auth.get_user(validated_token)
        return user if user else AnonymousUser()
    except Exception:
        return AnonymousUser()

class JWTAuthMiddleware:
    """
    Custom JWT Authentication middleware for Channels.

    Token resolution order:
      1. `Sec-WebSocket-Protocol` subprotocols of the form
         ['auth.token', '<jwt>'] -- preferred; keeps the token out of URLs,
         access logs, and proxy buffers.
      2. `?token=...` querystring -- legacy path, kept for backward compat
         with older clients still on the URL-token scheme.
    """
    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        from django.contrib.auth.models import AnonymousUser  # lazy import

        token = None

        # 1) Subprotocol auth: client offers ['auth.token', '<jwt>'].
        subprotocols = scope.get("subprotocols") or []
        if "auth.token" in subprotocols:
            idx = subprotocols.index("auth.token")
            if idx + 1 < len(subprotocols):
                candidate = subprotocols[idx + 1]
                if candidate:
                    token = candidate.strip()

        # 2) Legacy ?token=... querystring fallback.
        if not token:
            query_params = parse_qs(scope.get("query_string", b"").decode())
            qs_token = query_params.get("token", [None])[0]
            if qs_token:
                token = qs_token.strip()

        if token:
            scope["user"] = await get_user_from_token(token)
        else:
            scope["user"] = AnonymousUser()

        # Pass the scope, receive, and send to the next middleware or consumer
        return await self.inner(scope, receive, send)
