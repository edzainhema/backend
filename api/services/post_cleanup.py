"""Storage cleanup for permanently-deleted posts.

A *trash* (soft delete) only flips a flag; the media files stay in storage so
the post can be restored. Only a PURGE — permanently deleting a trashed post,
emptying it from the trash, or the 3-month retention sweep — actually reclaims
space. This helper removes a post's stored media (originals, thumbnails, and
the whole HLS bundle) and is shared by ``posts/purge`` and ``pages/purge``.
"""
from django.core.files.storage import default_storage


def _delete_storage_file(name):
    if not name:
        return
    try:
        default_storage.delete(name)
    except Exception:
        pass


def purge_post_files(post):
    """Best-effort deletion of a post's stored media from storage — the
    originals, thumbnails, and the HLS bundle (master playlist + variant
    playlists + segments, which all live under the master's prefix). Swallows
    per-file errors so one missing object can't block the purge."""
    for m in post.media.all():
        _delete_storage_file(getattr(m.file, "name", None))
        _delete_storage_file(getattr(m.thumbnail, "name", None))
        hls_name = getattr(m.hls_master, "name", None)
        if hls_name:
            prefix = hls_name.rsplit("/", 1)[0]
            try:
                _dirs, files = default_storage.listdir(prefix)
                for f in files:
                    _delete_storage_file(f"{prefix}/{f}")
            except Exception:
                # Backend can't list the prefix — at least drop the master.
                _delete_storage_file(hls_name)
