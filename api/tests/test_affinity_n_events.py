"""Regression test for audit L8: n_events (the taste-maturity counter) counts
only rows that actually contribute a taste signal — not impressions, tab
switches, or sub-threshold glances.

`n_events` gates the activity rail's cold-start ramp and flips compute_slot_layout
between the cold and mature discovery orderings at ACTIVITY_COLD_START_EVENTS.
Before the fix it was incremented for EVERY Activity row, so a passive scroller
crossed the maturity threshold on `post_impression` rows (logged by the feed just
for showing them posts) while their affinity stayed empty. Now a row counts only
once it survives every gate and credits affinity — keeping "we know your taste"
in step with what the profile genuinely learned.
"""
from django.contrib.auth.models import User
from rest_framework.test import APITestCase

from api.models import Activity
from api.feed.affinity import _compute_affinity_profile


class AffinityNEventsTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="scroller", password="x")

    def _act(self, action_type, **kw):
        return Activity.objects.create(
            user=self.user, action_type=action_type, **kw
        )

    def test_impressions_alone_do_not_mature_the_profile(self):
        """The headline case: a passive scroller racks up impressions only.
        Previously n_events would hit 30 (→ 'mature'); now it's 0."""
        for _ in range(30):
            self._act("post_impression")
        profile = _compute_affinity_profile(self.user)
        self.assertEqual(profile["n_events"], 0)

    def test_counts_only_contributing_rows(self):
        # Non-contributing noise: impressions, tab switches, a sub-8s glance.
        for _ in range(4):
            self._act("post_impression")
        for _ in range(2):
            self._act("tab_view", duration_seconds=5)
        self._act("post_view", duration_seconds=2)        # too short to count

        # Genuine taste signals that survive their gates.
        for _ in range(3):
            self._act("post_like")
        self._act("post_view", duration_seconds=12)        # qualifying dwell
        self._act("post_save")

        profile = _compute_affinity_profile(self.user)
        # 3 likes + 1 qualifying view + 1 save = 5; the 7 noise rows are ignored.
        self.assertEqual(profile["n_events"], 5)

    def test_short_view_does_not_count_but_long_view_does(self):
        self._act("post_view", duration_seconds=3)         # below 8s → ignored
        profile = _compute_affinity_profile(self.user)
        self.assertEqual(profile["n_events"], 0)

        self._act("post_view", duration_seconds=20)        # clears 8s → counts
        profile = _compute_affinity_profile(self.user)
        self.assertEqual(profile["n_events"], 1)

    def test_deliberate_negative_actions_still_count(self):
        """unlike / not_interested are real (negative) taste signals — the user
        actively told the app something — so they should still count toward
        maturity."""
        self._act("post_unlike")
        self._act("not_interested")
        profile = _compute_affinity_profile(self.user)
        self.assertEqual(profile["n_events"], 2)
