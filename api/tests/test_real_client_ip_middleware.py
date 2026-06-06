"""Unit tests for `RealClientIPMiddleware` (B4-2 follow-up).

DRF's `AnonRateThrottle` (and any other code that reads
`request.META['REMOTE_ADDR']`) needs to see the real CLIENT IP, not
nginx's loopback / VPC address. The middleware promotes the first
entry of `X-Forwarded-For` into `REMOTE_ADDR` when present. Tests
exercise the contract directly via a `RequestFactory`-built request
piped through the middleware -- no DB, no auth, no HTTP layer.
"""
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase

from api.middleware import RealClientIPMiddleware


class RealClientIPMiddlewareTests(SimpleTestCase):
    """B4: REMOTE_ADDR must reflect the real client IP behind nginx."""

    def _process(self, **meta):
        """Run a request through the middleware and capture whatever
        REMOTE_ADDR the downstream view ends up seeing."""
        rf = RequestFactory()
        request = rf.get("/", **meta)
        captured = {}

        def fake_view(req):
            captured["remote_addr"] = req.META.get("REMOTE_ADDR")
            return HttpResponse()

        middleware = RealClientIPMiddleware(fake_view)
        middleware(request)
        return captured["remote_addr"]

    def test_xff_single_hop_promotes_to_remote_addr(self):
        # Client -> nginx -> us. Nginx forwarded the real client IP.
        addr = self._process(
            HTTP_X_FORWARDED_FOR="198.51.100.42",
            REMOTE_ADDR="10.0.0.1",  # nginx's address (what we'd see without the middleware)
        )
        self.assertEqual(addr, "198.51.100.42")

    def test_xff_multi_hop_takes_first_entry(self):
        # Client -> CDN -> LB -> nginx -> us. XFF accumulates
        # left-to-right; the leftmost entry is the original client.
        addr = self._process(
            HTTP_X_FORWARDED_FOR="198.51.100.42, 203.0.113.7, 10.0.0.2",
            REMOTE_ADDR="10.0.0.1",
        )
        self.assertEqual(addr, "198.51.100.42")

    def test_xff_with_whitespace_is_trimmed(self):
        # Some proxies (and some Postman setups) emit `entry, entry`
        # with a leading space after the comma. Make sure we strip.
        addr = self._process(
            HTTP_X_FORWARDED_FOR=" 198.51.100.42 , 10.0.0.2",
            REMOTE_ADDR="10.0.0.1",
        )
        self.assertEqual(addr, "198.51.100.42")

    def test_no_xff_leaves_remote_addr_unchanged(self):
        # Local dev / direct connection (no proxy). The middleware
        # must NOT touch REMOTE_ADDR -- otherwise we'd nuke a
        # legitimate value (or, worse, set it to '' / None and break
        # downstream readers).
        addr = self._process(REMOTE_ADDR="10.0.0.1")
        self.assertEqual(addr, "10.0.0.1")

    def test_empty_xff_leaves_remote_addr_unchanged(self):
        # An XFF header that exists but is blank (some misconfigured
        # proxies do this) must NOT replace REMOTE_ADDR with ''.
        addr = self._process(
            HTTP_X_FORWARDED_FOR="",
            REMOTE_ADDR="10.0.0.1",
        )
        self.assertEqual(addr, "10.0.0.1")
