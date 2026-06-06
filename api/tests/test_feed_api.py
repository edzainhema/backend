"""Smoke tests for feed composition (R3).

Validates the /feed/ contract: auth-gated, and returns the documented
{"results": [...], "following_count": N} shape.
"""
from django.core.cache import cache
from rest_framework.test import APITestCase


class HomeFeedTests(APITestCase):
    def setUp(self):
        # Clear DRF throttle counters so the B4 register cap (5/min) isn't
        # tripped by accumulated /auth/register/ calls from sibling tests.
        # (Matches the convention used across test_auth_api / test_message_*.)
        cache.clear()
        reg = self.client.post("/auth/register/", {
            "username": "erin",
            "password": "feed-pass-123",
            "identifier_type": "email",
            "identifier": "erin@example.com",
        }, format="json")
        self.assertEqual(reg.status_code, 200, reg.content)
        self.access = reg.data["access"]

    def test_feed_requires_auth(self):
        resp = self.client.get("/feed/")
        self.assertEqual(resp.status_code, 401)

    def test_feed_returns_documented_shape(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.access}")
        resp = self.client.get("/feed/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn("results", resp.data)
        self.assertIsInstance(resp.data["results"], list)
        self.assertIn("following_count", resp.data)
