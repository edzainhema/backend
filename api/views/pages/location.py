"""Page location: Google Places (New) autocomplete/details proxy + persistence.

The mobile client never talks to Google directly — that would leak the
server-side API key into the binary. Instead LocationModal hits these three
endpoints:

  GET  /pages/location/autocomplete/?input=...&session_token=...
       -> predictions for the type-ahead list.
  GET  /pages/location/details/?place_id=...&session_token=...
       -> formatted address + lat/lng for the chosen prediction.
  POST /pages/location/set/  {page_id, location, latitude, longitude, place_id}
       -> atomically persist all four fields on the owner's page.

autocomplete/details proxy to the **Places API (New)** using
settings.GOOGLE_PLACES_API_KEY. (The legacy Places API was frozen in March 2025
and can't be enabled by new projects, so we use the current product:
  - POST https://places.googleapis.com/v1/places:autocomplete
  - GET  https://places.googleapis.com/v1/places/{place_id}
The key is sent in the X-Goog-Api-Key header; Place Details requires a
X-Goog-FieldMask header naming the fields we want back.)

If the key is unset the proxy endpoints return 503 and the client falls back to
plain free-text entry.

A `session_token` (a client-generated UUID) should be passed to both the
autocomplete and the matching details call: Google bills the autocomplete
keystrokes + the final details lookup as one "session," which is much cheaper
than billing each keystroke individually.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ...models import Page

_AUTOCOMPLETE_URL = "https://places.googleapis.com/v1/places:autocomplete"
_DETAILS_URL = "https://places.googleapis.com/v1/places/"  # + place_id
# Only the fields we actually use — a field mask is mandatory for Place Details
# (New) and keeps the response (and cost tier) small.
_DETAILS_FIELD_MASK = "id,formattedAddress,location,displayName"
_HTTP_TIMEOUT = 8  # seconds


def _request_json(url, *, method="GET", headers=None, body=None):
    """Make an HTTP request and return (status_code, parsed_json | None).

    - On a 2xx with a JSON body: (status, dict).
    - On an HTTP error (4xx/5xx): (status_code, None).
    - On a transport/parse failure: (None, None).
    """
    try:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url, data=data, headers=headers or {}, method=method
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return None, None


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def places_autocomplete(request):
    """Type-ahead suggestions for a partial location string.

    Returns: {"predictions": [{place_id, description, main_text, secondary_text}]}
    """
    api_key = getattr(settings, "GOOGLE_PLACES_API_KEY", "")
    if not api_key:
        return Response({"error": "Location search is not configured"}, status=503)

    query = (request.query_params.get("input") or "").strip()
    if len(query) < 2:
        # Too short to be worth a billed request; the UI shows nothing.
        return Response({"predictions": []})

    body = {"input": query}
    session_token = request.query_params.get("session_token")
    if session_token:
        body["sessionToken"] = session_token

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
    }

    status, data = _request_json(
        _AUTOCOMPLETE_URL, method="POST", headers=headers, body=body
    )
    if data is None:
        return Response({"error": "Upstream location service unavailable"}, status=502)

    predictions = []
    for suggestion in data.get("suggestions", []):
        pp = suggestion.get("placePrediction")
        if not pp:
            # Skip query predictions (no place_id to resolve).
            continue
        fmt = pp.get("structuredFormat") or {}
        main_text = (fmt.get("mainText") or {}).get("text", "")
        secondary_text = (fmt.get("secondaryText") or {}).get("text", "")
        description = (pp.get("text") or {}).get("text", "") or main_text
        predictions.append({
            "place_id": pp.get("placeId", ""),
            "description": description,
            "main_text": main_text or description,
            "secondary_text": secondary_text,
        })

    return Response({"predictions": predictions})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def place_details(request):
    """Resolve a place_id to a place name + formatted address + coordinates.

    Returns: {"name", "address", "latitude", "longitude", "place_id"}
    where `name` is the short display name (e.g. "Toothy Moose Cabaret") and
    `address` is the full formatted address.
    """
    api_key = getattr(settings, "GOOGLE_PLACES_API_KEY", "")
    if not api_key:
        return Response({"error": "Location search is not configured"}, status=503)

    place_id = (request.query_params.get("place_id") or "").strip()
    if not place_id:
        return Response({"error": "place_id is required"}, status=400)

    url = _DETAILS_URL + urllib.parse.quote(place_id)
    session_token = request.query_params.get("session_token")
    if session_token:
        url += "?" + urllib.parse.urlencode({"sessionToken": session_token})

    headers = {
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": _DETAILS_FIELD_MASK,
    }

    status, data = _request_json(url, method="GET", headers=headers)
    if data is None:
        # None status => transport failure; an HTTP error code => bad place_id.
        return Response(
            {"error": "Place lookup failed"},
            status=502 if status is None else 404,
        )

    loc = data.get("location") or {}
    name = (data.get("displayName") or {}).get("text", "")
    address = data.get("formattedAddress") or ""

    return Response({
        # Fall back to the address if a place somehow has no display name.
        "name": name or address,
        "address": address,
        "latitude": loc.get("latitude"),
        "longitude": loc.get("longitude"),
        "place_id": data.get("id") or place_id,
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def set_page_location(request):
    """Atomically persist a page's event location name + address + coordinates.

    Body: {page_id, location, address?, latitude?, longitude?, place_id?}

    `location` is the short display name shown everywhere (kept in the existing
    event_location field). `address` is the full formatted address revealed on
    tap. address/latitude/longitude/place_id are optional — they're present
    when the user picked a Google suggestion, and omitted / null for free-text
    entry, in which case the structured fields are cleared.
    """
    page_id = request.data.get("page_id")
    page = get_object_or_404(Page, id=page_id)

    if page.owner != request.user:
        return Response({"error": "Not allowed"}, status=403)

    def _clean_str(field_name, max_len):
        """Validate an optional string field. Returns (value, error_response).

        Empty/None -> ""; oversized or wrong-type -> error response.
        """
        raw = request.data.get(field_name)
        if raw in (None, ""):
            return "", None
        if not isinstance(raw, str):
            return None, Response(
                {"error": f"{field_name} must be a string"}, status=400,
            )
        cleaned = raw.strip()
        if len(cleaned) > max_len:
            return None, Response(
                {"error": f"{field_name} must be {max_len} characters or fewer"},
                status=400,
            )
        return cleaned, None

    # --- location (display name) + address ---
    location, err = _clean_str("location", 200)
    if err:
        return err
    address, err = _clean_str("address", 255)
    if err:
        return err

    # --- coordinates (optional; both must be present and valid together) ---
    def _coord(name, lo, hi):
        val = request.data.get(name)
        if val in (None, ""):
            return None, None
        try:
            num = float(val)
        except (TypeError, ValueError):
            return None, Response(
                {"error": f"{name} must be a number"}, status=400,
            )
        if not (lo <= num <= hi):
            return None, Response(
                {"error": f"{name} out of range"}, status=400,
            )
        return num, None

    latitude, err = _coord("latitude", -90.0, 90.0)
    if err:
        return err
    longitude, err = _coord("longitude", -180.0, 180.0)
    if err:
        return err
    # Coordinates only make sense as a pair; if one is missing, drop both.
    if latitude is None or longitude is None:
        latitude = longitude = None

    # --- place_id (optional) ---
    raw_place_id = request.data.get("place_id")
    if raw_place_id in (None, ""):
        place_id = ""
    elif isinstance(raw_place_id, str):
        place_id = raw_place_id.strip()[:255]
    else:
        return Response({"error": "place_id must be a string"}, status=400)

    page.event_location = location
    page.event_address = address
    page.event_latitude = latitude
    page.event_longitude = longitude
    page.event_place_id = place_id
    page.save(update_fields=[
        "event_location", "event_address",
        "event_latitude", "event_longitude", "event_place_id",
    ])

    return Response({
        "status": "ok",
        "event_location": location,
        "event_address": address,
        "event_latitude": latitude,
        "event_longitude": longitude,
        "event_place_id": place_id,
    })
