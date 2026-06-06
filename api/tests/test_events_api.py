"""Tests for the nearby-events discovery endpoint (`/events/nearby/`).

Covers the contract the search screen's Events tab relies on: auth-gating, the
"no stored location" empty state, the upcoming-only + radius-cap filtering, and
nearest-first ordering with a `distance_km` on each row.

Users are created via the ORM and authenticated with a JWT minted directly
(`RefreshToken.for_user`) rather than through `/auth/register/`, so these tests
don't consume the B4 register throttle (5/min) budget shared across the suite.
"""
from datetime import date, timedelta

from django.contrib.auth.models import User
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from api.models import Page, UserProfile


# Halifax-ish anchor for the viewer.
VIEWER_LAT = 44.6488
VIEWER_LNG = -63.5752


class NearbyEventsTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="viewer", password="x", email="viewer@example.com"
        )
        self.access = str(RefreshToken.for_user(self.user).access_token)

        # Page owner (events are Pages with is_event=True).
        self.owner = User.objects.create_user(
            username="organizer", password="x"
        )

        self.today = date.today()

    def _auth(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.access}")

    def _set_viewer_location(self, lat=VIEWER_LAT, lng=VIEWER_LNG):
        profile, _ = UserProfile.objects.get_or_create(user=self.user)
        profile.latitude = lat
        profile.longitude = lng
        profile.save()

    def _make_event(self, name, lat, lng, event_date):
        return Page.objects.create(
            owner=self.owner,
            name=name,
            is_event=True,
            event_date=event_date,
            event_latitude=lat,
            event_longitude=lng,
        )

    # ── contract ─────────────────────────────────────────────────────────
    def test_requires_auth(self):
        resp = self.client.get("/events/nearby/")
        self.assertEqual(resp.status_code, 401)

    def test_no_location_returns_empty_with_flag(self):
        # Viewer has no stored lat/lng → location_available False, no results.
        self._auth()
        self._make_event("Close", VIEWER_LAT, VIEWER_LNG, self.today)
        resp = self.client.get("/events/nearby/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(resp.data["location_available"])
        self.assertEqual(resp.data["results"], [])

    # ── filtering + ordering ─────────────────────────────────────────────
    def test_filters_and_orders(self):
        self._auth()
        self._set_viewer_location()

        # ~1.5 km away, upcoming → included.
        near = self._make_event(
            "Near & Soon", 44.66, -63.58, self.today + timedelta(days=2)
        )
        # ~5 km away, upcoming → included, but farther than `near`.
        mid = self._make_event(
            "Mid & Soon", 44.69, -63.60, self.today + timedelta(days=1)
        )
        # Montreal (~800 km) → outside the 50 km default radius → excluded.
        self._make_event("Far Away", 45.5017, -73.5673, self.today + timedelta(days=3))
        # Nearby but in the past → excluded (upcoming-only).
        self._make_event("Near but Past", 44.65, -63.57, self.today - timedelta(days=1))
        # Nearby, upcoming, but no coordinates → excluded.
        Page.objects.create(
            owner=self.owner,
            name="No Coords",
            is_event=True,
            event_date=self.today + timedelta(days=1),
        )
        # Nearby + upcoming but not flagged as an event → excluded.
        Page.objects.create(
            owner=self.owner,
            name="Not An Event",
            is_event=False,
            event_date=self.today + timedelta(days=1),
            event_latitude=44.65,
            event_longitude=-63.57,
        )

        resp = self.client.get("/events/nearby/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.data["location_available"])

        names = [r["name"] for r in resp.data["results"]]
        self.assertEqual(names, ["Near & Soon", "Mid & Soon"])  # nearest first

        first = resp.data["results"][0]
        self.assertEqual(first["id"], near.id)
        self.assertIn("distance_km", first)
        self.assertLess(first["distance_km"], resp.data["results"][1]["distance_km"])
        self.assertEqual(first["event_date"], (self.today + timedelta(days=2)).isoformat())
        # mid is the second, farther row.
        self.assertEqual(resp.data["results"][1]["id"], mid.id)

    def test_explicit_coords_override_profile(self):
        # Viewer has NO stored profile location, but passes an explicit device
        # fix as lat/lng — the endpoint should rank against that instead of
        # reporting location_available False.
        self._auth()
        near = self._make_event(
            "Near The Fix", 44.66, -63.58, self.today + timedelta(days=1)
        )
        resp = self.client.get(
            f"/events/nearby/?lat={VIEWER_LAT}&lng={VIEWER_LNG}"
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.data["location_available"])
        ids = [r["id"] for r in resp.data["results"]]
        self.assertIn(near.id, ids)

    def test_radius_param_narrows_results(self):
        self._auth()
        self._set_viewer_location()
        self._make_event("Within 3km", 44.66, -63.58, self.today + timedelta(days=1))
        self._make_event("About 8km", 44.72, -63.60, self.today + timedelta(days=1))

        resp = self.client.get("/events/nearby/?radius_km=3")
        self.assertEqual(resp.status_code, 200, resp.content)
        names = [r["name"] for r in resp.data["results"]]
        self.assertIn("Within 3km", names)
        self.assertNotIn("About 8km", names)
