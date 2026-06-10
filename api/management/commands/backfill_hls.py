"""
Backfill / self-heal HLS streams for video posts that don't have one.

This is the safety net that makes adaptive HLS reliable for EVERY clip, not
just the ones whose first encode happened to succeed. The synchronous upload
always produces a playable MP4, and a Celery task (process_post_media) packages
the HLS ladder in the background — but that task can fail or be lost: a long
clip times out, the worker restarts mid-encode, ffmpeg hits a transient
resource crunch, or a deploy interrupts it. In all those cases the post keeps
working on its MP4, but `hls_master` is left null and the clip never gets the
fast-start adaptive stream.

This command finds those stragglers (video media with no hls_master) and
re-runs the encode. Run it on a schedule (see deploy/systemd/backfill-hls.*)
so failures heal automatically within a cycle, and run it by hand any time:

    python manage.py backfill_hls                 # enqueue up to --limit
    python manage.py backfill_hls --sync          # encode inline (no worker)
    python manage.py backfill_hls --dry-run       # just report the count

process_post_media is idempotent (it no-ops if hls_master is already set), so
overlapping runs or a re-run after a partial success are safe.
"""
from django.core.management.base import BaseCommand
from django.db.models import Q

from api.models import PostMedia
from api.tasks import process_post_media

# Extensions we treat as video (PostMedia has no media_type column, so we sniff
# the stored filename — matches how the upload path decides what's a video).
VIDEO_EXTS = (".mp4", ".mov", ".webm", ".m4v")

DEFAULT_LIMIT = 200


class Command(BaseCommand):
    help = "Re-encode HLS for video posts whose hls_master is missing (self-heal)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=DEFAULT_LIMIT,
            help=f"Max media rows to process this run (default {DEFAULT_LIMIT}). "
                 "Bounds load per cycle; the next run picks up the rest.",
        )
        parser.add_argument(
            "--sync",
            action="store_true",
            help="Encode inline in this process instead of enqueuing to the "
                 "Celery worker. Useful with no broker, or for a one-off drain.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report how many clips are missing HLS without doing anything.",
        )

    def _candidates(self, limit):
        # Video media with no HLS yet. FileField stores '' (not NULL) when unset,
        # so check both. Oldest first, so a backlog drains in upload order.
        ext_q = Q()
        for ext in VIDEO_EXTS:
            ext_q |= Q(file__iendswith=ext)
        return (
            PostMedia.objects
            .filter(ext_q)
            .filter(Q(hls_master__isnull=True) | Q(hls_master=""))
            .order_by("id")
            .values_list("id", flat=True)[:limit]
        )

    def handle(self, *args, **opts):
        limit = opts["limit"]
        sync = opts["sync"]
        dry_run = opts["dry_run"]

        ids = list(self._candidates(limit))
        if not ids:
            self.stdout.write("No video posts are missing HLS. Nothing to do.")
            return

        if dry_run:
            self.stdout.write(
                f"{len(ids)} video post(s) missing HLS (showing up to --limit "
                f"{limit}). Re-run without --dry-run to encode."
            )
            return

        mode = "inline" if sync else "enqueued to worker"
        self.stdout.write(f"Processing {len(ids)} clip(s) ({mode})...")

        done = 0
        for media_id in ids:
            try:
                if sync:
                    process_post_media(media_id)
                else:
                    process_post_media.delay(media_id)
                done += 1
            except Exception as exc:  # keep going; one bad row mustn't stop the drain
                self.stderr.write(f"  media {media_id}: failed to {mode}: {exc}")

        verb = "encoded" if sync else "enqueued"
        self.stdout.write(self.style.SUCCESS(f"{verb} {done}/{len(ids)} clip(s)."))
