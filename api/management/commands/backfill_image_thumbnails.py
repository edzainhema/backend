"""Backfill grid/feed thumbnail, placeholder colour, and pixel dimensions for
PostMedia rows created before those columns were populated server-side.

New uploads get all of these at create time. This command repairs legacy rows
in a single pass:

  - IMAGE rows get `thumbnail` (small JPEG), `placeholder_color` (average
    colour), and EXIF-corrected `width`/`height`.
  - VIDEO rows get `width`/`height` derived from their stored thumbnail (the
    first frame, same aspect ratio) — PIL can't measure a video directly, and
    without dimensions the feed renders the video tile square and resizes it
    once the player loads. (Videos have no image thumbnail/placeholder colour.)

Pass --fix-dimensions to RE-COMPUTE dimensions for every row (the one-time
repair for feed cropping/resizing of rows with stale or missing dims); without
it, dimensions are only filled when missing.

Idempotent and crash-tolerant: each row is isolated so one unreadable file logs
and is skipped rather than aborting the run. Safe to re-run and safe to schedule.
"""
import io

from django.core.management.base import BaseCommand
from django.db.models import Q
from PIL import Image as PILImage, ImageOps as PILImageOps

from api.models import PostMedia
from api.services.media import make_image_thumbnail, average_color

# Mirror the grid's video detection (PostGridItem.VIDEO_FILE_RE).
VIDEO_SUFFIXES = (".mp4", ".mov", ".webm")


def _exif_size(file_obj):
    """EXIF-corrected (width, height) of an image file object, or None."""
    with PILImage.open(file_obj) as im:
        im = PILImageOps.exif_transpose(im)
        return im.size


class Command(BaseCommand):
    help = "Backfill PostMedia thumbnail, placeholder_color and dimensions for legacy rows."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit", type=int, default=0,
            help="Process at most N rows (0 = no limit).",
        )
        parser.add_argument(
            "--fix-dimensions", action="store_true",
            help="Re-compute width/height for EVERY row, not just those missing it.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report how many rows WOULD be touched without writing.",
        )

    def handle(self, *args, **opts):
        fix_dims = opts["fix_dimensions"]

        if fix_dims:
            qs = PostMedia.objects.all().order_by("id")
        else:
            qs = PostMedia.objects.filter(
                Q(thumbnail__isnull=True) | Q(thumbnail="")
                | Q(placeholder_color__isnull=True) | Q(placeholder_color="")
                | Q(width__isnull=True) | Q(height__isnull=True)
            ).order_by("id")

        candidates = [m for m in qs.iterator() if m.file]

        limit = opts["limit"]
        if limit and limit > 0:
            candidates = candidates[:limit]

        if opts["dry_run"]:
            imgs = [m for m in candidates if not m.file.name.lower().endswith(VIDEO_SUFFIXES)]
            vids = [m for m in candidates if m.file.name.lower().endswith(VIDEO_SUFFIXES)]
            self.stdout.write(
                f"[dry-run] {len(imgs)} image row(s), {len(vids)} video row(s) to consider"
            )
            return

        thumbs = colors = dims = failed = 0
        for media in candidates:
            is_video = media.file.name.lower().endswith(VIDEO_SUFFIXES)
            try:
                if is_video:
                    # Videos: derive dimensions from the stored thumbnail only.
                    need_dims = fix_dims or media.width is None or media.height is None
                    if need_dims and media.thumbnail and media.thumbnail.name:
                        try:
                            with media.thumbnail.open("rb") as th:
                                w, h = _exif_size(th)
                            if (w, h) != (media.width, media.height):
                                media.width, media.height = w, h
                                media.save(update_fields=["width", "height"])
                                dims += 1
                        except Exception as exc:  # noqa: BLE001
                            self.stderr.write(
                                f"  PostMedia {media.id}: video dim read failed: {exc}"
                            )
                    continue

                # Image rows: thumbnail + colour + dimensions in one read.
                with media.file.open("rb") as fh:
                    data = fh.read()
                update_fields = []

                if fix_dims or media.width is None or media.height is None:
                    try:
                        w, h = _exif_size(io.BytesIO(data))
                        if (w, h) != (media.width, media.height):
                            media.width, media.height = w, h
                            update_fields += ["width", "height"]
                            dims += 1
                    except Exception as exc:  # noqa: BLE001
                        self.stderr.write(
                            f"  PostMedia {media.id}: dimension read failed: {exc}"
                        )

                if not media.placeholder_color:
                    color = average_color(io.BytesIO(data))
                    if color:
                        media.placeholder_color = color
                        update_fields.append("placeholder_color")
                        colors += 1

                if not (media.thumbnail and media.thumbnail.name):
                    thumb = make_image_thumbnail(io.BytesIO(data))
                    if thumb is not None:
                        safe_name = f"post_{media.post_id}_media_{media.order}_thumb.jpg"
                        media.thumbnail.save(safe_name, thumb, save=False)
                        update_fields.append("thumbnail")
                        thumbs += 1
                    else:
                        self.stderr.write(
                            f"  PostMedia {media.id}: could not decode "
                            f"{media.file.name} for thumbnail"
                        )

                if update_fields:
                    media.save(update_fields=update_fields)
            except Exception as exc:  # noqa: BLE001 - per-row isolation
                failed += 1
                self.stderr.write(f"  skip PostMedia {media.id}: {exc}")

        self.stdout.write(
            self.style.SUCCESS(
                f"backfilled {thumbs} thumbnail(s), {colors} colour(s), "
                f"{dims} dimension set(s); {failed} row(s) skipped"
            )
        )
