"""Pure-logic tests for the opaque pagination cursors.

These guard the two cursor implementations (api.utils and api.feed.cursors):
both must round-trip a dict losslessly and degrade to {} on any malformed
input so a stale/garbage client token can never 500 an endpoint.
"""
from django.test import SimpleTestCase

from api.utils import encode_cursor, decode_cursor
from api.feed import cursors as feed_cursors


class UtilsCursorTests(SimpleTestCase):
    def test_round_trip_preserves_payload(self):
        payload = {"created_at": "2024-05-01T12:00:00Z", "id": 42}
        token = encode_cursor(payload)
        self.assertIsInstance(token, str)
        self.assertEqual(decode_cursor(token), payload)

    def test_malformed_input_decodes_to_empty_dict(self):
        self.assertEqual(decode_cursor("not-a-real-token"), {})
        self.assertEqual(decode_cursor(""), {})
        self.assertEqual(decode_cursor(None), {})

    def test_non_dict_json_decodes_to_empty_dict(self):
        import base64, json
        token = base64.urlsafe_b64encode(json.dumps([1, 2, 3]).encode()).decode()
        self.assertEqual(decode_cursor(token), {})


class FeedCursorTests(SimpleTestCase):
    def test_round_trip_preserves_payload(self):
        payload = {"offset": 20, "seed": 12345}
        token = feed_cursors.encode_cursor(payload)
        self.assertIsInstance(token, str)
        self.assertEqual(feed_cursors.decode_cursor(token), payload)

    def test_malformed_input_decodes_to_empty_dict(self):
        self.assertEqual(feed_cursors.decode_cursor("garbage!!!"), {})
        self.assertEqual(feed_cursors.decode_cursor(None), {})

    def test_bounded_int_clamps_and_defaults(self):
        from api.feed.cursors import _bounded_int
        from api.feed.constants import MAX_OFFSET
        self.assertEqual(_bounded_int(5), 5)
        self.assertEqual(_bounded_int(-3), 0)
        self.assertEqual(_bounded_int(MAX_OFFSET + 999), MAX_OFFSET)
        self.assertEqual(_bounded_int("not-an-int"), 0)
