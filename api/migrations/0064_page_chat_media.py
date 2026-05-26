"""No-op shim. Real page-chat media migration is 0066_page_chat_media."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("api", "0066_page_chat_media")]
    operations = []

# ─── Padding block ──────────────────────────────────────────────────────────
# This file lives in a workspace mount that holds the file at its original
# 2719-byte size: shorter rewrites leave the tail filled with NUL bytes, and
# Python refuses to parse a module that contains any \x00. To dodge that we
# pad the file out past 2719 bytes with plain `#`-comment lines, so wherever
# the truncation lands it lands inside a comment — which is always valid.
#
# The real schema work for page-chat media (adding `media` + `media_type` to
# PageChatMessage, allowing `text` to be blank, and creating the
# PageChatMessageMedia model) lives in 0066_page_chat_media. This file is
# only here because the underlying mount won't allow file deletion. Whoever
# is reading this in a normal git checkout: feel free to `git rm` it; the
# remaining graph (0063 → 0064_userprofile → 0065 → 0066) is linear and
# self-contained without this tail node.
#
# Padding line  1 — nothing to see here, just bytes filling space.
# Padding line  2 — nothing to see here, just bytes filling space.
# Padding line  3 — nothing to see here, just bytes filling space.
# Padding line  4 — nothing to see here, just bytes filling space.
# Padding line  5 — nothing to see here, just bytes filling space.
# Padding line  6 — nothing to see here, just bytes filling space.
# Padding line  7 — nothing to see here, just bytes filling space.
# Padding line  8 — nothing to see here, just bytes filling space.
# Padding line  9 — nothing to see here, just bytes filling space.
# Padding line 10 — nothing to see here, just bytes filling space.
# Padding line 11 — nothing to see here, just bytes filling space.
# Padding line 12 — nothing to see here, just bytes filling space.
# Padding line 13 — nothing to see here, just bytes filling space.
# Padding line 14 — nothing to see here, just bytes filling space.
# Padding line 15 — nothing to see here, just bytes filling space.
# Padding line 16 — nothing to see here, just bytes filling space.
# Padding line 17 — nothing to see here, just bytes filling space.
# Padding line 18 — nothing to see here, just bytes filling space.
# Padding line 19 — nothing to see here, just bytes filling space.
# Padding line 20 — nothing to see here, just bytes filling space.
# Padding line 21 — nothing to see here, just bytes filling space.
# Padding line 22 — nothing to see here, just bytes filling space.
# Padding line 23 — nothing to see here, just bytes filling space.
# Padding line 24 — nothing to see here, just bytes filling space.
# Padding line 25 — nothing to see here, just bytes filling space.
# Padding line 26 — nothing to see here, just bytes filling space.
# Padding line 27 — nothing to see here, just bytes filling space.
# Padding line 28 — nothing to see here, just bytes filling space.
# Padding line 29 — nothing to see here, just bytes filling space.
# Padding line 30 — nothing to see here, just bytes filling space.
# Padding line 31 — nothing to see here, just bytes filling space.
# Padding line 32 — nothing to see here, just bytes filling space.
# Padding line 33 — nothing to see here, just bytes filling space.
# Padding line 34 — nothing to see here, just bytes filling space.
# Padding line 35 — nothing to see here, just bytes filling space.
# end of padding block.
