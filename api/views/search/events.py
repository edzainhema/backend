"""Nearby-events discovery (`nearby_events`).

Powers the "Events" tab on the search screen. An event is just a `Page` with
`is_event=True` carrying geocoded coordinates (`event_latitude` /
`event_longitude`, set when the owner picks a Google Places suggestion in
LocationModal). This endpoint returns *upcoming* events (today or later) within
a radius of the viewer's last-known location, nearest first.

Location source: the viewer's stored `UserProfile.latitude/longitude` (reported
by the device after the location permission is granted — see
`update_user_location`). Callers may override with explicit `lat`/`lng` query
params (e.g. a fresh device fix) without persisting them. If no location is
available at all we return an empty result set plus `location_available: false`
so the client can show a "turn on location" empty state rather than an error.

Distance is computed with a cheap bounding-box prefilter in the DB followed by
an exact haversine in Python. This deliberately avoids PostGIS / GeoDjango so it
runs identically on the SQLite dev/CI fallback and on production Postgres.
"""

from datetime import date, time
from math import asin, cos, radians, sin, sqrt

from django.db.models import Q
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ...models import Page, PageFollow
from ...services.feed_helpers import get_muted_page_ids

# Earth radius in km (mean), used by the haversine below.
_EARTH_RADIUS_KM = 6371.0

# Default / max search radius. The client may request a smaller radius; we clamp
# to MAX so a crafted request can't ask us to scan the whole table.
_DEFAULT_RADIUS_KM = 50.0
_MAX_RADIUS_KM = 200.0

# Roughly how many km one degree of latitude spans. Longitude degrees shrink
# with latitude (multiplied by cos(lat)); both are used to size the bounding box.
_KM_PER_DEG_LAT = 111.0


def _haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance between two lat/lng points, in kilometres."""
    rlat1, rlon1, rlat2, rlon2 = map(radians, (lat1, lon1, lat2, lon2))
    dlat = rlat2 - rlat1
    dlon = rlon2 - rlon1
    a = sin(dlat / 2) ** 2 + cos(rlat1) * cos(rlat2) * sin(dlon / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * asin(sqrt(a))


def _read_float(params, key):
    """Parse a float query param, returning None when absent or malformed."""
    raw = params.get(key)
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def nearby_events(request):
    user = request.user
    params = request.query_params

    # ── Resolve the viewer's location ─────────────────────────────────────
    # Explicit lat/lng override (a fresh device fix) wins; otherwise fall back
    # to the last location the device reported into the profile.
    lat = _read_float(params, "lat")
    lng = _read_float(params, "lng")
    if lat is None or lng is None:
        profile = getattr(user, "userprofile", None)
        lat = getattr(profile, "latitude", None) if profile else None
        lng = getattr(profile, "longitude", None) if profile else None

    if lat is None or lng is None:
        # No location to rank against — let the client prompt for permission.
        return Response({
            "results": [],
            "has_more": False,
            "next_offset": None,
            "location_available": False,
        })

    # ── Radius + pagination params ────────────────────────────────────────
    radius_km = _read_float(params, "radius_km") or _DEFAULT_RADIUS_KM
    radius_km = max(1.0, min(radius_km, _MAX_RADIUS_KM))

    try:
        limit = int(params.get("limit", 20))
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 50))

    try:
        offset = int(params.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0
    offset = max(0, offset)

    # ── Bounding-box prefilter (index-friendly, GIS-free) ─────────────────
    # Convert the radius into degree deltas so the DB can drop everything
    # outside a coarse lat/lng box before we do exact distance math in Python.
    # cos(lat) shrinks longitude span toward the poles; guard the degenerate
    # case near ±90° so we never divide by ~0.
    lat_delta = radius_km / _KM_PER_DEG_LAT
    cos_lat = max(cos(radians(lat)), 0.01)
    lng_delta = radius_km / (_KM_PER_DEG_LAT * cos_lat)

    followed_page_ids = PageFollow.objects.filter(user=user).values_list(
        "page_id", flat=True
    )
    muted_page_ids = get_muted_page_ids(user)

    qs = (
        Page.objects.filter(
            is_event=True,
            event_date__gte=date.today(),
            event_latitude__isnull=False,
            event_longitude__isnull=False,
            event_latitude__gte=lat - lat_delta,
            event_latitude__lte=lat + lat_delta,
            event_longitude__gte=lng - lng_delta,
            event_longitude__lte=lng + lng_delta,
        )
        .exclude(id__in=muted_page_ids)
        # Hide private / super-private events the viewer can't access, mirroring
        # the visibility rule in search_pages / combined search.
        .exclude(
            (Q(is_private=True) | Q(is_super_private=True))
            & ~Q(owner=user)
            & ~Q(id__in=followed_page_ids)
        )
        .select_related("owner")
    )

    # ── Exact distance, radius cut, sort ──────────────────────────────────
    followed_ids_set = set(followed_page_ids)
    scored = []
    for page in qs:
        dist = _haversine_km(
            lat, lng, page.event_latitude, page.event_longitude
        )
        if dist <= radius_km:
            scored.append((dist, page))

    # Nearest first; soonest date as the tiebreak so same-venue events are
    # chronologically ordered, then id for a stable cursor.
    scored.sort(
        key=lambda dp: (
            dp[0],
            dp[1].event_date,
            dp[1].event_time or time.min,
            dp[1].id,
        )
    )

    window = scored[offset : offset + limit + 1]
    has_more = len(window) > limit
    window = window[:limit]

    results = [
        {
            "id": page.id,
            "name": page.name,
            "avatar": request.build_absolute_uri(page.avatar.url)
            if page.avatar
            else None,
            "owner": page.owner.username,
            "is_private": page.is_private,
            "event_date": page.event_date.isoformat() if page.event_date else None,
            "event_time": page.event_time.strftime("%H:%M")
            if page.event_time
            else None,
            "event_location": page.event_location,
            "event_address": page.event_address,
            "event_latitude": page.event_latitude,
            "event_longitude": page.event_longitude,
            "distance_km": round(dist, 1),
            "relationship": (
                "owner"
                if page.owner_id == user.id
                else ("following" if page.id in followed_ids_set else "none")
            ),
        }
        for dist, page in window
    ]

    return Response({
        "results": results,
        "has_more": has_more,
        "next_offset": (offset + limit) if has_more else None,
        "location_available": True,
    })
