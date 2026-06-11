"""Post creation: upload + media processing (create_post) and media-dimension capture."""
import logging

import json
import mimetypes
import re


from PIL import Image, ImageOps
from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import models, transaction
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ...models import BlockedUser, Follow, Notification, Page, PagePoster, Post, PostMedia, PostMediaTag
from ...serializers import ProfilePostSerializer
from ...services.media import IMAGE_MAX_BYTES, VIDEO_MAX_BYTES, average_color, crop_to_max_portrait_ratio, make_image_thumbnail, process_media_image, process_media_video, validate_uploaded_media_file, verify_uploaded_media
from ...tasks import fan_out_post_notifications, process_post_media
from ...services.push import push_to_user
from ...services.hashtags import sync_post_hashtags
from ...services.throttles import PostCreateRateThrottle

logger = logging.getLogger(__name__)

def _image_dimensions(file_like):
    """
    Read pixel dimensions from an in-memory image object WITHOUT requiring it
    to be written to storage first.

    Captured at upload time so the feed can size each tile before the asset
    finishes loading, eliminating the per-image Image.getSize() round trip the
    client used to make for layout. Best-effort: any failure (videos, corrupt
    files, missing PIL backend for the format) returns ``(None, None)`` and the
    client falls back to its runtime sizing path.

    Reads from the file object directly rather than ``media.file.path`` so it
    can run BEFORE the row is saved — that's what lets the whole media pipeline
    move out of the request's DB transaction (BACKEND_SCALING_AUDIT.md SY-1).
    The read cursor is reset to 0 on the way out so the caller can hand the
    same object straight to ``PostMedia.file`` for saving.
    """
    try:
        file_like.seek(0)
        with Image.open(file_like) as img:
            # Report EXIF-corrected dimensions so the stored width/height match
            # how the image displays. A phone portrait photo is often stored as
            # landscape pixels + an EXIF rotate tag; the feed sizes its tile from
            # these numbers and shows the EXIF-rotated image, so raw (un-rotated)
            # dimensions would give a wrong-shaped box and cover would crop the
            # photo. exif_transpose is a no-op for images with no orientation tag.
            img = ImageOps.exif_transpose(img)
            return img.size
    except Exception:
        # Video or unreadable image — dimensions stay null. The frontend's
        # Image.getSize / video naturalSize paths handle this case.
        return (None, None)
    finally:
        try:
            file_like.seek(0)
        except Exception:
            pass


def _parse_tagged_user_ids(request, idx):
    """Read and sanitise the per-media ``tagged_user_ids_{idx}`` field.

    Frontend sends a JSON array of ints (the user IDs the uploader picked in
    TagUsersModal). We validate it lightly here — JSON-decode, drop anything
    that isn't an int, dedupe — and return a list of ids. Resolution of those
    ids to actual User rows (with block / self-tag filtering) happens later,
    after the post + media rows exist. A missing/malformed field returns ``[]``
    so the caller can treat "no tags" and "couldn't parse tags" identically:
    a tag is a best-effort signal, never a reason to 400 the upload.
    """
    raw = request.data.get(f'tagged_user_ids_{idx}', None)
    if raw in (None, ''):
        return []
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(decoded, list):
        return []
    out = []
    seen = set()
    for v in decoded:
        # Accept ints and numeric strings; reject anything else (booleans,
        # nested objects, NaN-like floats). Coercing through ``int(str)``
        # would silently accept "12.5" -> ValueError; we'd rather drop.
        if isinstance(v, bool):
            continue
        if isinstance(v, int):
            uid = v
        elif isinstance(v, str) and v.isdigit():
            uid = int(v)
        else:
            continue
        if uid <= 0 or uid in seen:
            continue
        seen.add(uid)
        out.append(uid)
    return out


def _process_media_files(request, files):
    """
    Run the slow media pipeline (FFmpeg transcode / Pillow bake) and capture
    pixel dimensions for every uploaded file — all OUTSIDE any database
    transaction (BACKEND_SCALING_AUDIT.md SY-1).

    This work used to run *inside* create_post's ``transaction.atomic()``
    block, so a multi-second-to-multi-minute video transcode held a database
    connection — and, on SQLite, the global write lock — open for its entire
    duration, stalling every other write and risking idle-in-transaction
    timeouts / pool exhaustion on Postgres. Doing it here first means the
    transaction that follows only has to do fast row INSERTs.

    Returns a list of per-file dicts (one per input, in upload order)::

        {
            "processed_file":   <ContentFile|UploadedFile>,  # bytes to store
            "is_video":         <bool>,
            "baked_thumbnail":  <ContentFile|None>,           # server-derived
            "client_thumbnail": <UploadedFile|None>,          # client fallback
            "width":            <int|None>,
            "height":           <int|None>,
        }

    Raises on a processing failure (bad filtergraph, FFmpeg timeout, corrupt
    frame) exactly like the old inline path did — but now nothing has been
    written to the DB or storage yet when it raises, so the caller just returns
    a 500 with no rows or orphan files to undo.
    """
    results = []
    for idx, f in enumerate(files):
        # Per-file editor metadata (filter + overlays). Only videos send it
        # today; images are baked client-side and arrive already composited.
        metadata_str = request.data.get(f'metadata_{idx}', None)
        metadata = {}
        if metadata_str:
            try:
                metadata = json.loads(metadata_str)
            except (json.JSONDecodeError, TypeError):
                metadata = {}

        client_ct = (f.content_type or '').lower()
        is_video = client_ct.startswith('video/')
        is_image = client_ct.startswith('image/')

        has_edits = (
            metadata.get('filter_index', 0) != 0 or
            len(metadata.get('overlays', [])) > 0
        )

        # process_media_* raises on failure; the caller turns that into a 500.
        #
        # baked_thumbnail holds the thumbnail derived server-side from the
        # *processed* video's first frame (so it carries the same filter +
        # overlays as the clip). Only produced for edited videos; everything
        # else leaves it None and falls back to the client-extracted raw frame.
        baked_thumbnail = None
        if is_video and has_edits:
            processed_file, baked_thumbnail = process_media_video(f, metadata)
        elif is_image and has_edits:
            processed_file = process_media_image(f, metadata)
        else:
            processed_file = f

        # ── 9:16 max-height crop (image path) ────────────────────────────────
        # Bake the portrait cap into the stored image: anything taller than 9:16
        # is center-cropped to exactly 9:16; everything else is returned
        # unchanged (crop_to_max_portrait_ratio → None ⇒ keep the original
        # bytes). Done BEFORE dimensions + thumbnail below so width/height and
        # the grid thumbnail all derive from the already-cropped image.
        if is_image:
            cropped_image = crop_to_max_portrait_ratio(processed_file)
            if cropped_image is not None:
                processed_file = cropped_image

        # Pixel dimensions, read from the exact bytes we're about to store
        # (best-effort; videos and unreadable images come back (None, None)).
        width, height = _image_dimensions(processed_file)

        # Client-extracted raw first frame (the unedited-video thumbnail). Read
        # once here so the crop + dims logic below and the result dict agree on
        # the same object.
        client_thumbnail = request.FILES.get(f'thumbnail_{idx}') if is_video else None

        # ── 9:16 max-height crop (video path) ────────────────────────────────
        # PIL can't measure a video, so the DISPLAY dimensions come from the
        # thumbnail — and the thumbnail is also the poster shown under the
        # player. Crop the thumbnail to 9:16 so BOTH the stored dims and the
        # poster are capped; the HLS pass (build_hls_ladder) bakes the identical
        # crop into the streamed renditions, and the progressive MP4 fills the
        # 9:16 box via the player's cover fit. We can't re-encode the (up to
        # 7-minute) MP4 in the request path, so cropping the thumbnail + dims is
        # what makes a tall video render at 9:16 everywhere, immediately.
        # Prefer the baked (filter/overlay-carrying) thumb, else the client's.
        if is_video:
            resolved_thumb = baked_thumbnail or client_thumbnail
            if resolved_thumb is not None:
                cropped_thumb = crop_to_max_portrait_ratio(resolved_thumb)
                if cropped_thumb is not None:
                    resolved_thumb = cropped_thumb
                width, height = _image_dimensions(resolved_thumb)
            # Collapse the two thumbnail sources into one resolved (possibly
            # cropped) object so the save site stores exactly this — not the
            # uncropped original it would otherwise re-pick.
            baked_thumbnail = resolved_thumb
            client_thumbnail = None

        # NOTE: HLS packaging for videos is NOT done here anymore. It's the slow
        # part of the pipeline, so it's deferred to the process_post_media
        # Celery task (enqueued via on_commit after the post is created). The
        # MP4 produced above is immediately playable; hls_master backfills when
        # the task runs. See create_post below.

        # Bake a small JPEG thumbnail for EVERY image (edited or not) so the
        # media grid decodes a sub-1MB bitmap per cell instead of the full-res
        # file — this is what kills the grey-tile flip from bitmap-cache
        # eviction on scroll. Best-effort: a None just leaves `thumbnail` null
        # and the grid falls back to `file` (legacy behaviour). Generated here,
        # outside the DB transaction, alongside the rest of the slow media work.
        # make_image_thumbnail reseeks processed_file to 0 on the way out, so it
        # is still safe to save as PostMedia.file below.
        image_thumbnail = make_image_thumbnail(processed_file) if is_image else None

        # Average colour, used as the tile background so the photo fades in over
        # a matched colour instead of a hard grey box. Seek-safe like the calls
        # above. Images only; videos/None leave it null.
        placeholder_color = average_color(processed_file) if is_image else None

        results.append({
            "processed_file": processed_file,
            "is_video": is_video,
            "image_thumbnail": image_thumbnail,
            "placeholder_color": placeholder_color,
            # For videos this is the single resolved (possibly 9:16-cropped)
            # thumbnail; baked_thumbnail/client_thumbnail were collapsed above.
            "baked_thumbnail": baked_thumbnail,
            # Kept for shape-compatibility with the save site; always None for
            # video now (folded into baked_thumbnail) and None for images.
            "client_thumbnail": client_thumbnail,
            "width": width,
            "height": height,
            # Pre-parsed list of user IDs the uploader tagged in THIS media.
            # Parsed up-front (cheap, no DB) so the atomic-block path below
            # only does row inserts. Resolved to actual User rows AFTER the
            # transaction commits (notification fan-out is best-effort).
            "tagged_user_ids": _parse_tagged_user_ids(request, idx),
        })
    return results



def _parse_upload_location(request):
    """Parse the optional GPS coordinates captured at upload time. Returns
    ``(latitude, longitude, accuracy_m)``; all None unless a well-formed,
    in-range lat/lng pair is present (accuracy independently optional)."""
    upload_latitude = None
    upload_longitude = None
    upload_accuracy_m = None
    lat_raw = request.data.get('upload_latitude')
    lng_raw = request.data.get('upload_longitude')
    if lat_raw not in (None, '') and lng_raw not in (None, ''):
        try:
            lat_val = float(lat_raw)
            lng_val = float(lng_raw)
        except (TypeError, ValueError):
            lat_val = None
            lng_val = None
        if (
            lat_val is not None and lng_val is not None
            and -90.0 <= lat_val <= 90.0
            and -180.0 <= lng_val <= 180.0
        ):
            upload_latitude = lat_val
            upload_longitude = lng_val
            acc_raw = request.data.get('upload_accuracy_m')
            if acc_raw not in (None, ''):
                try:
                    acc_val = float(acc_raw)
                except (TypeError, ValueError):
                    acc_val = None
                if acc_val is not None and acc_val >= 0:
                    upload_accuracy_m = acc_val
    return upload_latitude, upload_longitude, upload_accuracy_m


def _validate_per_file_upload(request, files):
    """Validate each uploaded file (and any video thumbnail) up front via
    the shared `validate_uploaded_media_file` pipeline (audit B5).

    Posts accept image + video; audio attachments aren't a thing here.
    The shared validator handles: header type-check, filename / header
    agreement, per-kind size cap, magic-byte sniff, and (for images)
    Pillow's decompression-bomb guard. Returns an error ``Response`` on
    the first bad file, or None if all files pass.
    """
    # Cap thumbnails below full images, but with enough headroom for a
    # FULL-RESOLUTION frame. The client now extracts the thumbnail at the
    # clip's native resolution (so the thumbnail matches the video instead of
    # the old 512px-downscaled frame), which means a 1080p/4K still can run to
    # several MB — the previous 5 MB cap would have started rejecting those
    # legitimate uploads. 15 MB comfortably covers a high-quality JPEG of any
    # frame we'd produce while still being far tighter than IMAGE_MAX_BYTES.
    # The thumbnail remains fully validated below (magic-byte sniff +
    # decompression-bomb guard), so raising the byte cap doesn't reopen the
    # arbitrary-file-upload hole from finding #3 in UPLOAD_BUG_AUDIT.md — it
    # only allows a larger *image*.
    THUMBNAIL_MAX_BYTES = 15 * 1024 * 1024  # 15 MB
    for idx, f in enumerate(files):
        try:
            kind = validate_uploaded_media_file(
                f,
                allow_kinds=('image', 'video'),
            )
        except ValueError as exc:
            # 413 (Request Entity Too Large) for size violations stays
            # the conventional code; everything else maps to 400.
            code = (
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
                if 'exceeds' in str(exc)
                else status.HTTP_400_BAD_REQUEST
            )
            return Response({'error': str(exc)}, status=code)

        # Thumbnail validation. The client extracts a still frame for each
        # uploaded video and posts it under `thumbnail_{idx}`. The original
        # code wrote this file to disk with NO size cap, NO magic-byte
        # check, AND the client-controlled filename — a textbook arbitrary
        # file upload primitive. We validate it here, before the atomic
        # transaction opens, so a bad thumbnail 400s without leaving half
        # a Post + half its media rows behind.
        if kind == 'video':
            thumb = request.FILES.get(f'thumbnail_{idx}')
            if thumb is not None:
                try:
                    validate_uploaded_media_file(
                        thumb,
                        allow_kinds=('image',),
                        max_bytes_by_kind={'image': THUMBNAIL_MAX_BYTES},
                    )
                except ValueError as exc:
                    code = (
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
                        if 'exceeds' in str(exc)
                        else status.HTTP_400_BAD_REQUEST
                    )
                    return Response(
                        {'error': f'Invalid thumbnail: {exc}'}, status=code,
                    )
    return None


def _notify_post_mentions(actor, post, description):
    """Notify users @mentioned in a post description (best-effort;
    respects block relationships).

    Takes the ``actor`` (the poster) directly rather than ``request`` so it can
    run on a worker — it's invoked from the fan_out_post_notifications Celery
    task, off the upload request thread (see create_post).
    """
    # --------------------------------------------------
    # 🏷️ @MENTIONS in description (Mentions in posts)
    # --------------------------------------------------
    mentioned_usernames = set(
        re.findall(r"@([A-Za-z0-9_]{1,30})", description or "")
    )
    if mentioned_usernames:
        # .distinct() dedupes the same user mentioned multiple times in one
        # description; case-insensitive lookup so @Alice and @alice match.
        mentioned_users = User.objects.filter(
            username__iregex=r'^(' + '|'.join(re.escape(u) for u in mentioned_usernames) + ')$'
        ).exclude(id=actor.id).distinct()

        for u in mentioned_users:
            # 🚫 BLOCK CHECK
            if BlockedUser.objects.between(actor, u).exists():
                continue

            Notification.objects.create(
                recipient=u,
                actor=actor,
                notification_type="mention",
                media=post,
            )
            push_to_user(
                u,
                title="You were mentioned",
                body=f"{actor.username} mentioned you in a post",
                extra_data={
                    "type": "mention",
                    "post_id": post.id,
                    "actor_id": actor.id,
                },
            )


def _notify_post_tags(actor, post):
    """Notify every user who got tagged in any of this post's media.

    Reads back the PostMediaTag rows created inside the atomic block (so we
    pick up exactly the same set the DB persisted — already de-self-tagged
    and block-filtered there) and dedupes across media: if a user is tagged
    in two photos of the same post, we send exactly one notification + one
    push, not two.

    Takes the ``actor`` (the poster) directly rather than ``request`` so it can
    run on a worker — invoked from the fan_out_post_notifications Celery task,
    off the upload request thread (see create_post).
    """
    tagged_users = list(
        User.objects.filter(
            id__in=PostMediaTag.objects
                .filter(media__post=post)
                .values_list("user_id", flat=True)
        ).distinct()
    )
    if not tagged_users:
        return

    for u in tagged_users:
        Notification.objects.create(
            recipient=u,
            actor=actor,
            notification_type="post_tag",
            media=post,
        )
        # M11: pass actor + (optional) page so push_to_user can skip
        # this push when the tagged user has muted the poster or the
        # page the post belongs to. The Notification ROW above still
        # exists (the list view filters muted actors there too); this
        # only suppresses the buzz.
        push_to_user(
            u,
            title="You were tagged",
            body=f"{actor.username} tagged you in a post",
            extra_data={
                "type": "post_tag",
                "post_id": post.id,
                "actor_id": actor.id,
            },
            actor=actor,
            page=post.page,
        )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([PostCreateRateThrottle])
def create_post(request):
    description = request.data.get('description', '').strip()
    page_id = request.data.get('page_id', None)
    location = request.data.get('location', '').strip()[:255]
    files = request.FILES.getlist('files')

    upload_latitude, upload_longitude, upload_accuracy_m = _parse_upload_location(request)
    # ── Basic validation ─────────────────────────────────────────────
    if not files:
        return Response({'error': 'No files provided'}, status=status.HTTP_400_BAD_REQUEST)

    MAX_FILES = 10
    if len(files) > MAX_FILES:
        return Response(
            {'error': f'Too many files (max {MAX_FILES}).'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # ── Resolve and authorize the target Page (if any) ────────────────
    page_obj = None
    if page_id not in (None, ''):
        try:
            page_obj = Page.objects.get(id=int(page_id))
        except (Page.DoesNotExist, ValueError, TypeError):
            return Response({'error': 'Invalid page_id'}, status=status.HTTP_400_BAD_REQUEST)

        is_owner = (page_obj.owner_id == request.user.id)
        is_allowed_poster = PagePoster.objects.filter(
            page=page_obj, user=request.user
        ).exists()
        if not (is_owner or page_obj.anyone_can_post or is_allowed_poster):
            return Response(
                {'error': 'You are not allowed to post to this page.'},
                status=status.HTTP_403_FORBIDDEN,
            )

    validation_error = _validate_per_file_upload(request, files)
    if validation_error:
        return validation_error

    # ── Process media OUTSIDE any transaction (SY-1) ───────────────────
    # The slow FFmpeg transcode / Pillow bake + dimension capture run here,
    # with NO database transaction held. This is the whole point of the
    # SY-1 fix: a video transcode can take seconds to minutes, and it used
    # to run inside the atomic() block below, pinning a DB connection (and
    # the SQLite global write lock) open the entire time. Now the only thing
    # inside the transaction is fast row writes.
    #
    # A processing failure raises here, before any DB row or storage write
    # exists, so there is nothing to roll back or clean up — we just 500.
    try:
        processed = _process_media_files(request, files)
    except Exception as e:
        logger.error(f'[create_post] media processing failed: {e}')
        return Response(
            {'error': 'Failed to create post. Please try again.'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    # ── Resolve taggable users for ALL media in one pair of queries ────────
    # Collect every tagged user ID across all media and resolve them ONCE — a
    # single User lookup + a single BlockedUser lookup — instead of a query
    # pair per tagged media item (a 10-photo, all-tagged post used to fire ~20
    # extra queries inside the transaction). Self-tags and users blocked in
    # either direction are dropped here, leaving `eligible_taggees` (id -> User)
    # so the per-media loop below is just a dict lookup + bulk insert. Read-only,
    # so it runs OUTSIDE the transaction, keeping that block to fast writes.
    all_tagged_ids = {
        uid
        for item in processed
        for uid in (item.get("tagged_user_ids") or [])
        if uid != request.user.id
    }
    eligible_taggees: dict[int, User] = {}
    if all_tagged_ids:
        candidates = {
            u.id: u for u in User.objects.filter(id__in=all_tagged_ids)
        }
        if candidates:
            blocked_ids: set[int] = set()
            block_pairs = BlockedUser.objects.filter(
                models.Q(user=request.user, blocked_user_id__in=candidates.keys())
                | models.Q(user_id__in=candidates.keys(), blocked_user=request.user)
            ).values_list("user_id", "blocked_user_id")
            for a, b in block_pairs:
                blocked_ids.add(a)
                blocked_ids.add(b)
            blocked_ids.discard(request.user.id)
            eligible_taggees = {
                uid: u for uid, u in candidates.items() if uid not in blocked_ids
            }

    # ── Create rows atomically ─────────────────────────────────────────
    # Post + all its PostMedia rows commit together, fully processed, so no
    # feed ever observes a half-built post. Media is already transcoded, so
    # this transaction is sub-second.
    #
    # Caveat the atomic block doesn't cover: `PostMedia.objects.create(
    # file=...)` and `pm.thumbnail.save(...)` each call into the configured
    # storage backend (S3, local disk, etc.) and write the file BEFORE the
    # DB row is committed. If the transaction later rolls back, the DB rows
    # disappear but the storage writes don't — those are orphan files no
    # PostMedia references. To prevent them accumulating, we track every path
    # written during the transaction and delete each one in the except
    # handler below. Cleanup is best-effort: storage failures during cleanup
    # are swallowed so they can't mask the original error.
    written_file_paths: list[str] = []
    created_media = []
    try:
        with transaction.atomic():
            post = Post.objects.create(
                user=request.user,
                page=page_obj,
                description=description,
                location=location,
                upload_latitude=upload_latitude,
                upload_longitude=upload_longitude,
                upload_accuracy_m=upload_accuracy_m,
            )

            # Index hashtags inside the atomic block: if a later media step
            # fails and the transaction rolls back, the hashtag rows roll
            # back with the post. sync_post_hashtags swallows its own
            # errors, so it can't itself trigger the rollback.
            sync_post_hashtags(post)

            for idx, item in enumerate(processed):
                # Dimensions were computed in-memory by _process_media_files,
                # so we set them on the INSERT instead of a follow-up UPDATE.
                pm = PostMedia.objects.create(
                    post=post,
                    file=item["processed_file"],
                    order=idx,
                    width=item["width"],
                    height=item["height"],
                    placeholder_color=item["placeholder_color"],
                )
                # Record the storage path so the rollback handler can clean
                # it up if a later step in the loop fails. pm.file.name is
                # the storage-relative key (e.g. "post_media/abc.jpg"),
                # which is the form default_storage.delete() expects.
                if pm.file and pm.file.name:
                    written_file_paths.append(pm.file.name)

                # Package this video into an adaptive-bitrate HLS ladder on a
                # worker (SY-1). Enqueued via transaction.on_commit so it only
                # runs AFTER this transaction commits — the row exists, and a
                # rolled-back post never enqueues. The MP4 saved above is already
                # playable; hls_master backfills when the task finishes (the app
                # prefers hls and falls back to the MP4 until then). In EAGER
                # mode (no broker) .delay runs inline at commit. `mid=pm.id`
                # binds the current row id (not the loop variable).
                if item["is_video"]:
                    transaction.on_commit(
                        lambda mid=pm.id: process_post_media.delay(mid)
                    )

                # Save the video's thumbnail.
                #
                # Prefer `baked_thumbnail` — the first frame of the *processed*
                # video, which carries the same colour filter and text overlays
                # the user applied in the editor, so the feed thumbnail matches
                # the clip. It only exists for edited videos; for an unedited
                # video (or if server-side extraction failed) we fall back to
                # the client-extracted raw first frame, which is correct in the
                # no-edits case because there's nothing to bake in.
                #
                # Either way we use a server-derived filename so nothing from
                # the client's multipart name can influence what hits storage.
                # Django's FileSystemStorage and most cloud backends already
                # sanitize this further, but server-controlled names give us a
                # clean belt-and-braces guarantee.
                # For images, `image_thumbnail` is the server-downscaled small
                # JPEG baked above (None on a best-effort failure). Same column,
                # same server-derived filename, same rollback-cleanup tracking
                # as the video path — only the source of the thumbnail differs
                # (a shrunk copy of the photo vs the clip's first frame).
                if item["is_video"]:
                    thumbnail = item["baked_thumbnail"] or item["client_thumbnail"]
                else:
                    thumbnail = item.get("image_thumbnail")
                if thumbnail:
                    safe_name = f'post_{post.id}_media_{idx}_thumb.jpg'
                    pm.thumbnail.save(safe_name, thumbnail, save=True)
                    if pm.thumbnail and pm.thumbnail.name:
                        written_file_paths.append(pm.thumbnail.name)

                # Persist who the uploader tagged in this specific media item.
                # We resolve the requested IDs to actual User rows here (one
                # bulk query per media) and filter out:
                #   - the uploader themselves (self-tag is meaningless and
                #     would produce a useless self-notification)
                #   - users that have a Block relationship in either
                #     direction (privacy + harassment-prevention parity with
                #     _notify_post_mentions)
                # We DO NOT 404 / 400 unknown IDs — a tag is best-effort, so
                # we just skip anything that doesn't resolve. The
                # ``unique_together`` on (media, user) makes the create idempotent
                # under client retries.
                # Tag resolution + block filtering already happened once for the
                # whole post (see eligible_taggees above), so this is a pure dict
                # lookup — no per-media User / BlockedUser queries. The
                # ``unique_together`` on (media, user) keeps the create idempotent
                # under client retries.
                requested_ids = item.get("tagged_user_ids") or []
                if requested_ids:
                    tagged_users_for_media = [
                        eligible_taggees[uid]
                        for uid in requested_ids
                        if uid in eligible_taggees
                    ]
                    if tagged_users_for_media:
                        PostMediaTag.objects.bulk_create(
                            [
                                PostMediaTag(media=pm, user=u)
                                for u in tagged_users_for_media
                            ],
                            ignore_conflicts=True,
                        )

                created_media.append({
                    'id': pm.id,
                    'order': pm.order,
                    'file_url': request.build_absolute_uri(pm.file.url),
                    'thumbnail_url': (
                        request.build_absolute_uri(pm.thumbnail.url)
                        if pm.thumbnail else None
                    ),
                })
    except Exception as e:
        # Any error inside the atomic block lands here. The transaction has
        # already rolled back, so the DB has no record of these files. Clean
        # up storage so we don't accumulate orphans. Each delete is wrapped
        # because a single storage failure during cleanup must NOT mask the
        # original error — the user already got nothing useful out of this
        # request, and the 500 we return below is the more important signal
        # than a cleanup hiccup.
        from django.core.files.storage import default_storage
        for path in written_file_paths:
            try:
                default_storage.delete(path)
            except Exception as cleanup_err:
                logger.error(
                    f'[create_post] orphan cleanup failed for {path}: '
                    f'{cleanup_err}'
                )
        logger.error(f'[create_post] failed: {e}')
        return Response(
            {'error': 'Failed to create post. Please try again.'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


    # Fan out @mention + tag notifications on a worker, off the request thread
    # (the per-recipient Notification.create writes, block checks, and push
    # enqueues used to run inline here). The post + its PostMediaTag rows are
    # committed, so the task just reads them. In EAGER mode (no broker) this runs
    # inline, exactly preserving the old timing/behaviour.
    fan_out_post_notifications.delay(post.id, request.user.id, description)

    # Invalidate suggested feed caches for all followers so the new post
    # surfaces in their feed immediately rather than waiting for the 5-min TTL.
    follower_ids = list(
        Follow.objects.filter(following=request.user).values_list("follower_id", flat=True)
    )
    if follower_ids:
        cache.delete_many([f"suggested_feed_scores:{uid}" for uid in follower_ids])

    return Response(
        {'post': ProfilePostSerializer(post, context={'request': request}).data},
        status=status.HTTP_201_CREATED,
    )
