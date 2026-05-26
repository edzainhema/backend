# Server-side overlay fonts

When a user uploads a video with text overlays, the server bakes the text into
the video via FFmpeg's `drawtext` filter. `drawtext` needs an actual `.ttf`
file on the server's filesystem — it can't use a font name the way the mobile
app can. To keep the baked-in text identical to what the user saw in the
editor, this directory needs to contain the **same 21 `.ttf` files** the
mobile app bundles in `frontend/assets/fonts/`.

If a TTF is missing from this folder the server falls back to a DejaVu /
Liberation system font for that overlay only — so a partial set still works,
the overlay just won't render in the exact selected face for the missing
files.

## Files this folder expects

These names exactly match the PostScript names referenced by
`resolve_overlay_font_path` in `backend/api/views.py`. They're the same
filenames you already downloaded into `frontend/assets/fonts/`.

```
Caveat-Regular.ttf            Caveat-SemiBold.ttf            Caveat-Bold.ttf
Oswald-Regular.ttf            Oswald-SemiBold.ttf            Oswald-Bold.ttf
Fredoka-Regular.ttf           Fredoka-Medium.ttf             Fredoka-Bold.ttf
Montserrat-Regular.ttf        Montserrat-SemiBold.ttf        Montserrat-ExtraBold.ttf
PlayfairDisplay-Regular.ttf   PlayfairDisplay-SemiBold.ttf   PlayfairDisplay-ExtraBold.ttf
JetBrainsMono-Regular.ttf     JetBrainsMono-SemiBold.ttf     JetBrainsMono-ExtraBold.ttf
Nunito-Regular.ttf            Nunito-SemiBold.ttf            Nunito-ExtraBold.ttf
```

## Easiest way to populate it

The mobile app already has all 21 files in `frontend/assets/fonts/`. Just copy
them over:

```bash
# from project root
cp frontend/assets/fonts/*.ttf backend/fonts/
```

Or on Windows PowerShell:

```powershell
Copy-Item -Path frontend\assets\fonts\*.ttf -Destination backend\fonts\
```

Don't copy `README.md` or `download-fonts.ps1` — only the `.ttf` files.

## Deployment

When deploying the server, make sure this folder ships with the rest of the
code (it's not in `.gitignore`-style exclusions by default — check your
deploy script if videos start coming back with the wrong font in production).
The lookup uses `BASE_DIR / 'fonts'`, so wherever your Django app's `BASE_DIR`
is at runtime, this folder must sit next to it.

## Adding more fonts later

When you add a new font to `frontend/utils/textWeight.ts`'s `BUNDLED_FONTS`,
do all three of:

1. Add the same family → weight → PostScript-name rows to `_BUNDLED_FONTS` in
   `backend/api/views.py`.
2. Drop the new `.ttf` files in this folder.
3. Update the list in this README.
