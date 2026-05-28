"""Overlay font resolution: bundled-font lookup table, system-font fallbacks,
and the path resolver used when drawing text overlays."""
import os

from django.conf import settings

FONTS_DIR = os.path.join(getattr(settings, 'BASE_DIR', os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'fonts')

# family → weight → PostScript name (== filename without .ttf)
_BUNDLED_FONTS = {
    'Caveat': {
        '400': 'Caveat-Regular', '600': 'Caveat-SemiBold', '800': 'Caveat-Bold',
    },
    'Oswald': {
        '400': 'Oswald-Regular', '600': 'Oswald-SemiBold', '800': 'Oswald-Bold',
    },
    'Fredoka': {
        '400': 'Fredoka-Regular', '600': 'Fredoka-Medium', '800': 'Fredoka-Bold',
    },
    'Montserrat': {
        '400': 'Montserrat-Regular', '600': 'Montserrat-SemiBold', '800': 'Montserrat-ExtraBold',
    },
    'PlayfairDisplay': {
        '400': 'PlayfairDisplay-Regular', '600': 'PlayfairDisplay-SemiBold', '800': 'PlayfairDisplay-ExtraBold',
    },
    'JetBrainsMono': {
        '400': 'JetBrainsMono-Regular', '600': 'JetBrainsMono-SemiBold', '800': 'JetBrainsMono-ExtraBold',
    },
    'Nunito': {
        '400': 'Nunito-Regular', '600': 'Nunito-SemiBold', '800': 'Nunito-ExtraBold',
    },
}

# System-font fallbacks per weight, used for the "Default" / System option
# and whenever a bundled TTF is missing. Try several common Linux paths plus
# Windows for dev. First existing one wins.
_SYSTEM_FONT_CANDIDATES = {
    'bold': [
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
        '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
        'C:/Windows/Fonts/arialbd.ttf',
    ],
    'regular': [
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
        'C:/Windows/Fonts/arial.ttf',
    ],
}


# Magic-byte signatures we accept for "video/*" uploads. We sniff the first
# 32 bytes of the file rather than trusting the client's Content-Type header,
# which is freely spoofable. Covers MP4 / MOV / M4V (ISO base media, "ftyp"
# at offset 4), WebM / MKV (EBML), and AVI (RIFF). 3GP also uses ftyp so it
# falls under the MP4 branch.


def _first_existing(paths):
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None


def resolve_overlay_font_path(font_family, font_weight):
    """
    Return an absolute path to the TTF that should render an overlay with
    the given (font_family, font_weight). Resolves bundled families first;
    falls back to a system font on miss.

    Mirrors `resolveTextWeight` + `resolvePostScriptName` on the client.
    """
    weight = str(font_weight) if font_weight is not None else '600'
    family = font_family or 'System'

    bundled = _BUNDLED_FONTS.get(family)
    if bundled:
        ps_name = bundled.get(weight) or bundled.get('400')
        if ps_name:
            candidate = os.path.join(FONTS_DIR, f'{ps_name}.ttf')
            if os.path.exists(candidate):
                return candidate
            # Bundled face missing on disk — fall through to system font.

    # System / unknown / missing bundled file: pick a system font that
    # roughly matches the requested weight so at least the bold/regular
    # distinction survives.
    bucket = 'bold' if int(weight) >= 700 else 'regular'
    return _first_existing(_SYSTEM_FONT_CANDIDATES[bucket]) \
        or _first_existing(_SYSTEM_FONT_CANDIDATES['regular'])


