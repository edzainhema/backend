"""
Drain buffered feed impressions from Redis into the database (C1).

Feed renders push their impressions onto a Redis list
(api.feed.constants.IMPRESSION_QUEUE_KEY) instead of writing them on the request
path. This command flushes that list — bulk-inserting the post_impression
Activity rows and bumping Post.impression_count — so the writes never touch
the request critical path or spawn a thread/connection per request
(ACTIVITY_AND_FEED_AUDIT.md item C1).

Schedule it frequently (every minute is fine — see
deploy/cron/collaborative-recs.cron). If it falls behind or stops, the buffer
caps at IMPRESSION_QUEUE_MAX and renders self-heal back to synchronous writes,
so impressions are never silently lost.

Best-effort and crash-tolerant: it pulls a chunk off the queue atomically
(LRANGE + LTRIM in a MULTI/EXEC) and writes it; a crash mid-write loses at
most one in-flight chunk of impression analytics, which is acceptable.
"""

import json
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db.models import F

from api.models import Activity, Post
from api.feed.constants import IMPRESSION_QUEUE_KEY


DRAIN_CHUNK = 1000           # buffer entries (renders) pulled per round-trip
MAX_BATCHES_PER_RUN = 2000   # safety cap on work per invocation


class Command(BaseCommand):
    help = "Flush buffered feed impressions from Redis into the DB (C1)."

    def handle(self, *args, **opts):
        try:
            from django_redis import get_redis_connection
            redis = get_redis_connection("default")
        except Exception as exc:
            # No Redis backend → impressions were written synchronously at
            # render time, so there's nothing to drain. Not an error.
            self.stdout.write(f"no redis backend ({exc}); nothing to drain")
            return

        total = 0
        batches = 0
        while batches < MAX_BATCHES_PER_RUN:
            # Atomically read the oldest chunk (tail — lpush prepends) and trim
            # it off in one MULTI/EXEC so concurrent pushes can't be lost.
            pipe = redis.pipeline(transaction=True)
            pipe.lrange(IMPRESSION_QUEUE_KEY, -DRAIN_CHUNK, -1)
            pipe.ltrim(IMPRESSION_QUEUE_KEY, 0, -DRAIN_CHUNK - 1)
            chunk = pipe.execute()[0]
            if not chunk:
                break

            rows = []
            counter = defaultdict(int)
            for payload in chunk:
                try:
                    if isinstance(payload, (bytes, bytearray)):
                        payload = payload.decode("utf-8")
                    data = json.loads(payload)
                except Exception:
                    continue
                uid = data.get("u")
                sid = data.get("s", "")   # session id (C3)
                for it in data.get("items", []):
                    pid = it.get("post_id")
                    if pid is None:
                        continue
                    rows.append(Activity(
                        user_id=uid,
                        action_type="post_impression",
                        post_id=pid,
                        surface="home",
                        session_id=sid,
                        metadata={"rail": it.get("rail"), "slot": it.get("slot")},
                    ))
                    counter[pid] += 1

            if rows:
                Activity.objects.bulk_create(rows, ignore_conflicts=True)
                # Bump impression_count once per post. Group posts by their
                # increment so it's a handful of UPDATEs (mostly the +1 group),
                # not one per post.
                by_inc = defaultdict(list)
                for pid, inc in counter.items():
                    by_inc[inc].append(pid)
                for inc, pids in by_inc.items():
                    Post.objects.filter(id__in=pids).update(
                        impression_count=F("impression_count") + inc
                    )
                total += len(rows)
            batches += 1

        if total:
            self.stdout.write(self.style.SUCCESS(
                f"drained {total} impressions in {batches} chunk(s)"
            ))
