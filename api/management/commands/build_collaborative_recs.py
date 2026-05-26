"""
Nightly collaborative-filtering job — "people like you also engage with X".

Run from cron / Celery beat once a day (low-traffic hours):

    python manage.py build_collaborative_recs

Why this exists
---------------
Every live feed rail is content-based: it can only recommend an author the
viewer already has some connection to (follows, is near, shares hashtags
with). None of them can surface a *fresh* author the viewer has never touched
but whom users with near-identical taste love. That's exactly what
collaborative filtering does. See ACTIVITY_AND_FEED_AUDIT.md item B3.

Algorithm (classic user-based CF, computed offline)
---------------------------------------------------
1. Build a user -> author engagement vector from the Activity stream over the
   last WINDOW_DAYS (positive signals only; weights mirror the live ranker).
2. Invert it to author -> {users who engage with that author}, so we only
   ever compare users who actually overlap (no O(U^2) all-pairs scan).
3. For each user, gather candidate neighbours (users sharing >= MIN_SHARED
   authors), score them by cosine similarity of the two engagement vectors,
   and keep the top K_NEIGHBOURS.
4. Aggregate the neighbours' authors, weighted by neighbour similarity,
   skipping authors the user already engages with. Keep the top
   N_RECS_PER_USER.
5. Replace the RecommendedAuthor table in one transaction.

Everything is bounded by caps so the job stays tractable as a single-process
overnight task. At very large scale you'd swap step 3 for an approximate-
nearest-neighbour index, but the table contract (RecommendedAuthor) and the
rail that reads it would not change.
"""

import math
import time
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from datetime import timedelta

from api.models import Activity, Follow, Post, RecommendedAuthor


# Positive engagement weights — how strongly each action expresses taste for
# the engaged post's author. Negative / neutral actions are intentionally
# excluded: CF taste vectors work best on positive co-engagement, and the
# feed rail itself already applies block / mute / not-interested exclusions.
ACTION_WEIGHTS = {
    "post_save":     5.0,
    "post_share":    4.0,
    "post_comment":  3.0,
    "post_like":     3.0,
    "reel_rewatch":  4.0,
    "reel_complete": 3.0,
    "user_visit":    2.0,
    "post_view":     1.0,   # only counted when it cleared the dwell gate
}

WINDOW_DAYS = 45            # engagement lookback
MIN_USER_AUTHORS = 3        # users with thinner taste vectors are skipped
TOP_AUTHORS_PER_USER = 80   # cap each user's vector length
MAX_USERS_PER_AUTHOR = 400  # cap the fan-out from a hugely popular author
MIN_SHARED_AUTHORS = 2      # neighbours must overlap on at least this many
K_NEIGHBOURS = 50           # top similar users kept per user
N_RECS_PER_USER = 100       # recommendations stored per user
WRITE_BATCH = 5000


class Command(BaseCommand):
    help = "Compute per-user collaborative-filtering author recommendations."

    def add_arguments(self, parser):
        parser.add_argument(
            "--window-days", type=int, default=WINDOW_DAYS,
            help="Engagement lookback window.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Compute and report, but don't write to the DB.",
        )

    def handle(self, *args, **opts):
        t0 = time.time()
        window_days = opts["window_days"]
        dry_run = opts["dry_run"]

        user_authors = self._build_engagement_matrix(window_days)
        self.stdout.write(f"engagement matrix: {len(user_authors)} users")

        author_users = self._invert(user_authors)
        self.stdout.write(f"inverted index: {len(author_users)} authors")

        recs = self._recommend(user_authors, author_users)
        total = sum(len(v) for v in recs.values())
        self.stdout.write(f"recommendations: {total} rows for {len(recs)} users")

        if dry_run:
            self.stdout.write(self.style.WARNING("dry-run: not writing"))
        else:
            self._write(recs)
            self.stdout.write(self.style.SUCCESS("written"))

        self.stdout.write(f"done in {time.time() - t0:.1f}s")

    # ── Step 1: user -> author engagement vector ─────────────────────────
    def _build_engagement_matrix(self, window_days):
        since = timezone.now() - timedelta(days=window_days)
        acts = (
            Activity.objects
            .filter(action_type__in=ACTION_WEIGHTS.keys(), created_at__gte=since)
            .values_list("user_id", "action_type", "post_id", "target_user_id", "duration_seconds")
        )

        # Resolve post_id -> author in one batch.
        post_ids = {pid for _, _, pid, _, _ in acts if pid is not None}
        post_author = {}
        if post_ids:
            post_author = dict(
                Post.objects.filter(id__in=post_ids).values_list("id", "user_id")
            )

        user_authors: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
        for uid, action, pid, tuid, dur in acts:
            # Same dwell gate the live ranker uses for post_view.
            if action == "post_view" and (dur or 0) < 8:
                continue
            author = None
            if pid is not None:
                author = post_author.get(pid)
            elif tuid is not None:    # user_visit
                author = tuid
            if author is None or author == uid:
                continue              # skip self-authored / unresolved
            user_authors[uid][author] += ACTION_WEIGHTS[action]

        # Drop thin vectors and cap each to its strongest authors.
        trimmed: dict[int, dict[int, float]] = {}
        for uid, authors in user_authors.items():
            if len(authors) < MIN_USER_AUTHORS:
                continue
            if len(authors) > TOP_AUTHORS_PER_USER:
                top = sorted(authors.items(), key=lambda kv: kv[1], reverse=True)[:TOP_AUTHORS_PER_USER]
                authors = dict(top)
            trimmed[uid] = authors
        return trimmed

    # ── Step 2: author -> users (bounded inverted index) ─────────────────
    def _invert(self, user_authors):
        author_users: dict[int, list[int]] = defaultdict(list)
        for uid, authors in user_authors.items():
            for a in authors:
                author_users[a].append(uid)
        # Cap fan-out from very popular authors so they don't make every user
        # look similar to everyone. Keep the most engaged users for that author.
        for a, users in author_users.items():
            if len(users) > MAX_USERS_PER_AUTHOR:
                users.sort(key=lambda u: user_authors[u].get(a, 0.0), reverse=True)
                author_users[a] = users[:MAX_USERS_PER_AUTHOR]
        return author_users

    # ── Steps 3 & 4: neighbours -> recommended authors ───────────────────
    def _recommend(self, user_authors, author_users):
        # Precompute vector norms for cosine.
        norms = {
            uid: math.sqrt(sum(w * w for w in authors.values()))
            for uid, authors in user_authors.items()
        }
        recs: dict[int, list[tuple[int, float]]] = {}

        for uid, authors in user_authors.items():
            u_norm = norms[uid]
            if u_norm == 0:
                continue

            # Candidate neighbours + how many authors they share with u.
            shared_count: dict[int, int] = defaultdict(int)
            for a in authors:
                for v in author_users.get(a, ()):
                    if v != uid:
                        shared_count[v] += 1

            # Cosine similarity for neighbours clearing the overlap floor.
            sims = []
            for v, shared in shared_count.items():
                if shared < MIN_SHARED_AUTHORS:
                    continue
                v_authors = user_authors[v]
                dot = sum(w * v_authors.get(a, 0.0) for a, w in authors.items())
                if dot <= 0:
                    continue
                sim = dot / (u_norm * norms[v])
                if sim > 0:
                    sims.append((v, sim))

            if not sims:
                continue
            sims.sort(key=lambda x: x[1], reverse=True)
            sims = sims[:K_NEIGHBOURS]

            # Aggregate neighbours' authors, weighted by similarity, skipping
            # authors u already engages with (the goal is FRESH authors).
            own = set(authors.keys())
            rec_scores: dict[int, float] = defaultdict(float)
            for v, sim in sims:
                for a, w in user_authors[v].items():
                    if a in own or a == uid:
                        continue
                    rec_scores[a] += sim * w

            if not rec_scores:
                continue
            top = sorted(rec_scores.items(), key=lambda kv: kv[1], reverse=True)[:N_RECS_PER_USER]
            recs[uid] = top
        return recs

    # ── Step 5: replace the table ────────────────────────────────────────
    def _write(self, recs):
        rows = [
            RecommendedAuthor(user_id=uid, author_id=aid, score=score)
            for uid, pairs in recs.items()
            for aid, score in pairs
        ]
        with transaction.atomic():
            # Full replace. Runs in the overnight window; the rail tolerates a
            # brief empty read (it just falls back to other rails). A two-table
            # swap would remove even that, at the cost of more machinery.
            RecommendedAuthor.objects.all().delete()
            for i in range(0, len(rows), WRITE_BATCH):
                RecommendedAuthor.objects.bulk_create(
                    rows[i:i + WRITE_BATCH], ignore_conflicts=True
                )
