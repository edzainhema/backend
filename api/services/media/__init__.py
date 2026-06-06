"""Media upload pipeline package (validation, fonts, filters, overlays,
images, video). Public names re-exported so existing
`from ..services.media import X` callers resolve unchanged."""

from .validation import (
    IMAGE_MAX_BYTES,
    VIDEO_MAX_BYTES,
    AUDIO_MAX_BYTES,
    DEFAULT_MAX_BYTES_BY_KIND,
    _sniff_video_signature,
    _sniff_audio_signature,
    verify_uploaded_media,
    validate_image_upload,
    validate_uploaded_media_file,
    safe_image_filename,
)
from .fonts import _first_existing, resolve_overlay_font_path
from .filters import VIDEO_FILTER_CHAINS
from .overlays import _safe_float, _safe_int, _safe_optional_float
from .images import process_media_image
from .video import process_media_video

__all__ = [
    "IMAGE_MAX_BYTES",
    "VIDEO_MAX_BYTES",
    "AUDIO_MAX_BYTES",
    "DEFAULT_MAX_BYTES_BY_KIND",
    "_sniff_video_signature",
    "_sniff_audio_signature",
    "verify_uploaded_media",
    "validate_image_upload",
    "validate_uploaded_media_file",
    "safe_image_filename",
    "_first_existing",
    "resolve_overlay_font_path",
    "VIDEO_FILTER_CHAINS",
    "_safe_float",
    "_safe_int",
    "_safe_optional_float",
    "process_media_image",
    "process_media_video",
]
