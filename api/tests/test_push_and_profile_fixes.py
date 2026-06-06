"""Tests for audit L7 (push-task redelivery dedup) and L10 (affinity profile
reuse in the activity rail)."""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from rest_framework.test import APITestCase

from api.tasks import (
    dispatch_push,
    _push_already_sent,
    _mark_push_sent,
)
from api.feed.rails.activity import _rail_activity


class PushTaskIdempotencyTests(APITestCase):
    """L7: a redelivered push task (same Celery task id) must not resend."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="u", password="x")

    # ---- the dedup primitives ---------------------------------------------

    def test_mark_then_already_sent(self):
        self.assertFalse(_push_already_sent("task-1"))
        _mark_push_sent("task-1")
        self.assertTrue(_push_already_sent("task-1"))

    def test_distinct_task_ids_are_independent(self):
        _mark_push_sent("task-1")
        self.assertFalse(_push_already_sent("task-2"))

    def test_none_task_id_never_dedups(self):
        # EAGER mode can hand a None id; it must never short-circuit or error.
        self.assertFalse(_push_already_sent(None))
        _mark_push_sent(None)
        self.assertFalse(_push_already_sent(None))

    # ---- end-to-end task behaviour (apply() lets us fix the task id) -------

    @patch("api.services.push._send_push_to_user")
    def test_redelivery_with_same_id_sends_once(self, mock_send):
        dispatch_push.apply(
            args=[self.user.id, "t", "b", None], task_id="fixed-1",
        )
        # Simulate the broker redelivering the SAME task.
        dispatch_push.apply(
            args=[self.user.id, "t", "b", None], task_id="fixed-1",
        )
        self.assertEqual(mock_send.call_count, 1)

    @patch("api.services.push._send_push_to_user")
    def test_distinct_tasks_both_send(self, mock_send):
        dispatch_push.apply(args=[self.user.id, "t", "b", None], task_id="a")
        dispatch_push.apply(args=[self.user.id, "t", "b", None], task_id="b")
        self.assertEqual(mock_send.call_count, 2)


_FAKE_PROFILE = {"n_events": 0, "author": {}, "hashtag": {}, "keyword": {}}


class ActivityProfileReuseTests(APITestCase):
    """L10: the activity rail reuses a passed-in profile instead of re-fetching;
    it falls back to its own fetch only when none (or an empty dict) is given.
    n_events=0 makes the rail return [] right after the profile step, so these
    tests need no candidate/context setup."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="u", password="x")

    @patch("api.feed.rails.activity._build_activity_profile")
    def test_reuses_passed_profile(self, mock_build):
        result = _rail_activity(
            None, self.user, {}, offset=0, limit=5,
            exclude_ids=set(), profile=_FAKE_PROFILE,
        )
        self.assertEqual(result, [])
        mock_build.assert_not_called()

    @patch("api.feed.rails.activity._build_activity_profile")
    def test_fetches_when_profile_is_none(self, mock_build):
        mock_build.return_value = dict(_FAKE_PROFILE)
        _rail_activity(
            None, self.user, {}, offset=0, limit=5,
            exclude_ids=set(), profile=None,
        )
        mock_build.assert_called_once()

    @patch("api.feed.rails.activity._build_activity_profile")
    def test_refetches_on_empty_dict(self, mock_build):
        # compose passes {} when its own profile build failed — rail must
        # recover by fetching, not crash on profile["n_events"].
        mock_build.return_value = dict(_FAKE_PROFILE)
        _rail_activity(
            None, self.user, {}, offset=0, limit=5,
            exclude_ids=set(), profile={},
        )
        mock_build.assert_called_once()
