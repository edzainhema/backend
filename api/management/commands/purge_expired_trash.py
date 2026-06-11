"""Permanently delete trash older than the retention window (default 90 days).

Trash (soft delete) keeps rows + files so they can be restored; this sweep is
what actually reclaims them. Run it on a schedule (cron / systemd timer, same
as backfill-hls):

    python manage.py purge_expired_trash            # purge >90-day trash
    python manage.py purge_expired_trash --days 30  # tighter window
    python manage.py purge_expired_trash --dry-run  # report only, delete nothing

Trashed POSTS and trashed PAGES each age on their own clock (a post's
``trashed_at`` / a page's ``deleted_at``), so they're swept independently:
purging an expired page only detaches its (already-trashed) posts via SET_NULL —
those wait for their own expiry in their owners' trashes.
"""
from datetime import timedelta

from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand
from django.utils import timezone

from api.models import Page, Post
from api.services.post_cleanup import purge_post_files

RETENTION_DAYS = 90


class Command(BaseCommand):
    help = "Permanently delete trashed posts/pages older than the retention window."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days", type=int, default=RETENTION_DAYS,
            help=f"Retention window in days (default {RETENTION_DAYS}).",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be purged without deleting anything.",
        )

    def handle(self, *args, **options):
        days = max(0, options["days"])
        dry = options["dry_run"]
        cutoff = timezone.now() - timedelta(days=days)
        prefix = "[dry-run] " if dry else ""

        # ── Expired trashed posts: drop files, then rows ──────────────────
        expired_posts = Post.all_objects.filter(trashed_at__lt=cutoff)
        post_count = expired_posts.count()
        if post_count and not dry:
            for post in expired_posts.iterator():
                purge_post_files(post)
            expired_posts.delete()

        # ── Expired trashed pages: drop non-default avatar, then row ──────
        expired_pages = Page.all_objects.filter(deleted_at__lt=cutoff)
        page_count = expired_pages.count()
        if page_count and not dry:
            for page in expired_pages.iterator():
                name = getattr(page.avatar, "name", None)
                if name and "default" not in name:
                    try:
                        default_storage.delete(name)
                    except Exception:
                        pass
            expired_pages.delete()

        self.stdout.write(
            f"{prefix}purged {post_count} post(s) and {page_count} page(s) "
            f"older than {days} days from trash"
        )
