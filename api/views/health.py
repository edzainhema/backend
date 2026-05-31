"""
Lightweight health check endpoint for external monitoring.

Hit at GET /health/ by:
  - UptimeRobot / Better Stack / Pingdom (or similar) every 1-5 min
  - CloudWatch external probes / future load balancer health checks
  - You, when something feels off ("is the site up?")

Designed to be cheap (~5 ms total) and dependency-aware:
  - Verifies Django booted (implicit — if the view runs, Django is alive)
  - Verifies the database connection works (SELECT 1)
  - Verifies the Redis cache is reachable (set + get round-trip)

Returns 200 OK if all checks pass, 503 Service Unavailable if any fail.
The 503 lets uptime monitors and load balancers correctly mark the
instance as down — much better than silently returning 200 while the
DB is gone, which is what would happen without explicit checks.

Public (no auth). Excluded from rate limiting in nginx. Don't add
expensive queries here — the cost is paid on every probe, and an
unhealthy site shouldn't be MORE slow because the health check itself
is slow.
"""
from django.core.cache import cache
from django.db import connection
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response


# Accept both GET and HEAD. HEAD is what most uptime monitors (UptimeRobot,
# AWS ELB health checks, etc.) send by default — same response as GET but
# without the body. Per HTTP spec, any GET endpoint should also handle HEAD.
@api_view(["GET", "HEAD"])
@permission_classes([AllowAny])
def health_check(request):
    """Return 200 + {"status": "ok"} if DB and cache are reachable, else 503."""
    checks = {}
    healthy = True

    # Database: a trivial SELECT 1 — cheaper than any ORM query because
    # it skips the model layer and just verifies the connection works.
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        checks["database"] = "ok"
    except Exception as exc:  # pragma: no cover — defensive
        healthy = False
        checks["database"] = f"error: {exc.__class__.__name__}"

    # Cache (Redis): write + read a sentinel. We use a short TTL (10 s) so
    # the entry expires on its own — no need to clean up. The round-trip
    # catches both connectivity issues and one-way failures (e.g., Redis
    # accepts writes but reads silently fail).
    try:
        cache.set("__healthcheck__", "ok", timeout=10)
        value = cache.get("__healthcheck__")
        if value == "ok":
            checks["cache"] = "ok"
        else:
            healthy = False
            checks["cache"] = f"error: unexpected value {value!r}"
    except Exception as exc:  # pragma: no cover — defensive
        healthy = False
        checks["cache"] = f"error: {exc.__class__.__name__}"

    return Response(
        {"status": "ok" if healthy else "degraded", "checks": checks},
        status=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
    )
