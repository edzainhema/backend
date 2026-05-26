"""
Single source of truth for reading a Post's media *in order*, cache-safely.

The trap this exists to prevent: chaining `.order_by("order")` onto the related
manager — `post.media.all().order_by("order")` — returns a brand-new queryset
that ignores any `prefetch_related("media")` the view set up, so it issues a
fresh `SELECT ... FROM postmedia` for every post in the page (a classic N+1).
The same "prefetch then read ordered" logic used to be reimplemented inline at
half a dozen call sites with subtly different correctness; route them all
through `ordered_media()` so the trap can't reappear.
"""


def ordered_media(post):
    """
    Return ``post``'s media ordered by ``order`` without busting the prefetch
    cache. Two cache-safe paths:

      1. If the view prefetched media with a pre-sorted ``to_attr`` —
         ``Prefetch("media", queryset=PostMedia.objects.order_by("order"),
         to_attr="ordered_media")`` — that list is already on the instance;
         return it as-is.
      2. Otherwise sort the prefetched ``media.all()`` cache in Python (this
         reuses the rows ``prefetch_related("media")`` already loaded; it does
         NOT re-query).

    Always use this instead of ``post.media.all().order_by("order")``.
    """
    prefetched = getattr(post, "ordered_media", None)
    if prefetched is not None:
        return prefetched
    return sorted(post.media.all(), key=lambda m: m.order)
