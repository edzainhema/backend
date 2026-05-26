"""Geo math for the nearby rail: haversine distance, bounding box, antimeridian-safe longitude filter, and a coarse geohash for cache bucketing."""
from __future__ import annotations

import math

from django.db.models import Q

def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """
    Great-circle distance in kilometers between two (lat, lng) pairs.

    Returns +inf if any input is None so missing coordinates always rank
    worst rather than throwing.
    """
    if None in (lat1, lng1, lat2, lng2):
        return float("inf")
    r = 6371.0
    lat1r = math.radians(lat1)
    lat2r = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1r) * math.cos(lat2r) * math.sin(dlng / 2) ** 2
    )
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))



def bbox_for_radius(lat: float, lng: float, radius_km: float):
    """
    Return (min_lat, max_lat, min_lng, max_lng) covering a `radius_km`
    circle around `(lat, lng)`. Used as a cheap SQL prefilter before the
    real haversine ranking in Python.

    Longitude span widens near the equator and narrows toward the poles,
    so we scale by cos(lat). Clamps at the poles to avoid division blow-up.
    """
    lat_delta = radius_km / 111.0
    cos_lat = max(0.01, math.cos(math.radians(lat)))
    lng_delta = radius_km / (111.0 * cos_lat)
    return (lat - lat_delta, lat + lat_delta, lng - lng_delta, lng + lng_delta)



def nearby_longitude_q(min_lng: float, max_lng: float):
    """
    Build the longitude half of the nearby bbox prefilter, handling
    antimeridian (±180°) wrap-around.

    bbox_for_radius works in raw degrees and doesn't clamp longitude, so for
    a viewer near the international date line it can return min_lng < -180 or
    max_lng > 180. A naive ``lng >= min_lng AND lng <= max_lng`` then matches
    nothing on the far side of the line — e.g. a viewer at lng 179.9 gets a
    box of [179.6, 180.2], which excludes a post 5 km away at lng -179.95,
    because that post's stored longitude is at the OTHER end of the
    [-180, 180] range. Result: an empty nearby feed near Fiji / NZ / the
    Russian far east / mid-Pacific flights. See ACTIVITY_AND_FEED_AUDIT.md
    item A13.

    Fix: when the box overflows one end, OR in the wrapped segment from the
    other end. The haversine pass in _rail_nearby already computes correct
    great-circle distance across the seam (its sin²(Δlng/2) term is symmetric
    around ±180), so once the prefilter lets the wrapped candidates through,
    the radius check filters them correctly.

    Returns a Q. Latitude is handled separately by the caller — latitude
    doesn't wrap (out-of-[-90,90] bounds simply exclude nothing, which is
    the correct behaviour near the poles).
    """
    # Degenerate: the box spans the whole globe in longitude. Only reachable
    # at extreme latitudes where lng_delta blows up (cos_lat is clamped to
    # 0.01 → up to ~22.5° each side). Don't constrain longitude at all; the
    # haversine pass does the real distance filtering.
    if max_lng - min_lng >= 360.0:
        return Q()

    if max_lng > 180.0:
        # Spills past +180 → wrapped tail starts back at -180.
        return (
            Q(upload_longitude__gte=min_lng, upload_longitude__lte=180.0)
            | Q(upload_longitude__gte=-180.0, upload_longitude__lte=max_lng - 360.0)
        )

    if min_lng < -180.0:
        # Spills past -180 → wrapped head ends at +180.
        return (
            Q(upload_longitude__gte=-180.0, upload_longitude__lte=max_lng)
            | Q(upload_longitude__gte=min_lng + 360.0, upload_longitude__lte=180.0)
        )

    # Common case: no wrap.
    return Q(upload_longitude__gte=min_lng, upload_longitude__lte=max_lng)



def coarse_geohash(lat: float, lng: float, precision: int = 5) -> str:
    """
    Tiny geohash implementation used only as a cache-key bucket — not
    exposed to clients, not authoritative. Precision 5 ≈ ~5 km cells,
    which matches our cache invalidation needs (a viewer who moves 5 km
    gets a fresh nearby score; a viewer who shifts 100 m doesn't trigger
    a re-score).
    """
    if lat is None or lng is None:
        return "none"
    _BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"
    lat_range = [-90.0, 90.0]
    lng_range = [-180.0, 180.0]
    bits = []
    even = True
    while len(bits) < precision * 5:
        if even:
            mid = (lng_range[0] + lng_range[1]) / 2
            if lng > mid:
                bits.append(1)
                lng_range[0] = mid
            else:
                bits.append(0)
                lng_range[1] = mid
        else:
            mid = (lat_range[0] + lat_range[1]) / 2
            if lat > mid:
                bits.append(1)
                lat_range[0] = mid
            else:
                bits.append(0)
                lat_range[1] = mid
        even = not even
    out = []
    for i in range(0, len(bits), 5):
        chunk = bits[i:i + 5]
        n = 0
        for b in chunk:
            n = (n << 1) | b
        out.append(_BASE32[n])
    return "".join(out)
