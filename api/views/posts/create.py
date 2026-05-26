"""Post creation: upload + media processing (create_post) and media-dimension capture."""
import logging

import json
import mimetypes
import re


from PIL import Image
from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ...models import BlockedUser, Follow, Notification, Page, PagePoster, Post, PostMedia
from ...services.media_processing import IMAGE_MAX_BYTES, VIDEO_MAX_BYTES, process_media_image, process_media_video, verify_uploaded_media
from ...utils import push_to_user, sync_post_hashtags

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

        # Pixel dimensions, read from the exact bytes we're about to store
        # (best-effort; videos and unreadable images come back (None, None)).
        width, height = _image_dimensions(processed_file)

        results.append({
            "processed_file": processed_file,
            "is_video": is_video,
            "baked_thumbnail": baked_thumbnail,
            # Client-extracted raw first frame, used as the thumbnail when the
            # server didn't bake one (unedited video, or extraction failed).
            "client_thumbnail": (
                request.FILES.get(f'thumbnail_{idx}') if is_video else None
            ),
            "width": width,
            "height": height,
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
    """Validate each uploaded file (and any video thumbnail) up front:
    type, size cap, and magic-byte content check. Returns an error
    ``Response`` on the first bad file, or None if all files pass."""
    # Cap thumbnails much tighter than full images. A thumbnail is a single
    # still frame the client extracts client-side from the video; 5 MB is
    # generous for any reasonable JPEG. Without this cap, the thumbnail
    # slot acts as an arbitrary-file-upload primitive (the original code
    # had no size/magic-byte check at all on thumbnails — finding #3 in
    # UPLOAD_BUG_AUDIT.md).
    THUMBNAIL_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
    # ── Validate file size, type, and content up front ────────────────
    # The client's Content-Type header is freely spoofable, so we use it
    # only to pick a verifier (image vs video) and then confirm the bytes
    # actually match — Pillow.verify() for images, magic-byte sniff for
    # videos. We also enforce per-file size caps before any expensive
    # processing, so a malicious or buggy client can't DoS the server
    # with a huge or bomb-style file.
    for idx, f in enumerate(files):
        client_ct = (f.content_type or '').lower()
        guessed_ct = (mimetypes.guess_type(f.name or '')[0] or '').lower()
        is_image_ct = client_ct.startswith('image/')
        is_video_ct = client_ct.startswith('video/')
        if not (is_image_ct or is_video_ct):
            return Response(
                {'error': f'Unsupported file type: {client_ct or "unknown"}'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Best-effort sanity check against the filename. If the extension
        # is unknown (no guess) we let it pass — some camera URIs lack one.
        if guessed_ct and not guessed_ct.startswith(client_ct.split('/')[0]):
            return Response(
                {'error': f'Content-type does not match filename: {f.name}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Per-file size cap. f.size is set by Django's upload handler from
        # the multipart length, so this rejects before we spool the whole
        # file to disk or run FFmpeg / Pillow on it.
        size_cap = IMAGE_MAX_BYTES if is_image_ct else VIDEO_MAX_BYTES
        if f.size is not None and f.size > size_cap:
            limit_mb = size_cap // (1024 * 1024)
            kind_label = 'Image' if is_image_ct else 'Video'
            return Response(
                {'error': f'{kind_label} exceeds the {limit_mb} MB limit.'},
                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )

        # Magic-byte check — confirms the file's actual content matches
        # its claimed type. Also catches Pillow decompression bombs via
        # the MAX_IMAGE_PIXELS guard set at module load.
        try:
            verify_uploaded_media(
                f,
                claimed_kind='image' if is_image_ct else 'video',
            )
        except ValueError as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Thumbnail validation. The client extracts a still frame for each
        # uploaded video and posts it under `thumbnail_{idx}`. The original
        # code wrote this file to disk with NO size cap, NO magic-byte
        # check, AND the client-controlled filename — a textbook arbitrary
        # file upload primitive. We validate it here, before the atomic
        # transaction opens, so a bad thumbnail 400s without leaving half
        # a Post + half its media rows behind.
        if is_video_ct:
            thumb = request.FILES.get(f'thumbnail_{idx}')
            if thumb is not None:
                if thumb.size is not None and thumb.size > THUMBNAIL_MAX_BYTES:
                    limit_mb = THUMBNAIL_MAX_BYTES // (1024 * 1024)
                    return Response(
                        {'error': f'Thumbnail exceeds the {limit_mb} MB limit.'},
                        status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    )
                try:
                    verify_uploaded_media(thumb, claimed_kind='image')
                except ValueError as e:
                    return Response(
                        {'error': f'Invalid thumbnail: {e}'},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
    return None


def _notify_post_mentions(request, post, description):
    """Notify users @mentioned in a post description (best-effort;
    respects block relationships)."""
    # --------------------------------------------------
    # 🏷️ @MENTIONS in description (Mentions in posts)
    # Done outside the atomic block — notifications are best-effort and
    # we don't want a flaky push to fail the whole upload.
    # --------------------------------------------------
    mentioned_usernames = set(
        re.findall(r"@([A-Za-z0-9_]{1,30})", description or "")
    )
    if mentioned_usernames:
        # .distinct() dedupes the same user mentioned multiple times in one
        # description; case-insensitive lookup so @Alice and @alice match.
        mentioned_users = User.objects.filter(
            username__iregex=r'^(' + '|'.join(re.escape(u) for u in mentioned_usernames) + ')$'
        ).exclude(id=request.user.id).distinct()

        for u in mentioned_users:
            # 🚫 BLOCK CHECK
            if BlockedUser.objects.between(request.user, u).exists():
                continue

            Notification.objects.create(
                recipient=u,
                actor=request.user,
                notification_type="mention",
                media=post,
            )
            push_to_user(
                u,
                title="You were mentioned",
                body=f"{request.user.username} mentioned you in a post",
                extra_data={
                    "type": "mention",
                    "post_id": post.id,
                    "actor_id": request.user.id,
                },
            )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
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
                )
                # Record the storage path so the rollback handler can clean
                # it up if a later step in the loop fails. pm.file.name is
                # the storage-relative key (e.g. "post_media/abc.jpg"),
                # which is the form default_storage.delete() expects.
                if pm.file and pm.file.name:
                    written_file_paths.append(pm.file.name)

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
                if item["is_video"]:
                    thumbnail = item["baked_thumbnail"] or item["client_thumbnail"]
                    if thumbnail:
                        safe_name = f'post_{post.id}_media_{idx}_thumb.jpg'
                        pm.thumbnail.save(safe_name, thumbnail, save=True)
                        if pm.thumbnail and pm.thumbnail.name:
                            written_file_paths.append(pm.thumbnail.name)

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

    _notify_post_mentions(request, post, description)

    # Invalidate suggested feed caches for all followers so the new post
    # surfaces in their feed immediately rather than waiting for the 5-min TTL.
    follower_ids = list(
        Follow.objects.filter(following=request.user).values_list("follower_id", flat=True)
    )
    if follower_ids:
        cache.delete_many([f"suggested_feed_scores:{fid}" for fid in follower_ids])

    return Response(
        {
            'message': 'Post created successfully',
            'post': {
                'id': post.id,
                'description': post.description,
                'location': post.location,
                'media': created_media,
            },
        },
        status=status.HTTP_201_CREATED,
    )
