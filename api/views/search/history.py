"""Recent-search history CRUD (`search_history`: GET / POST / DELETE)."""


from django.contrib.auth.models import User
from django.db import transaction
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ...models import Page, SearchHistory
from ...services.activity import log_activity


@api_view(["GET", "POST", "DELETE"])
@permission_classes([IsAuthenticated])
def search_history(request):
    user = request.user

    # ── GET: return last 20 history entries ──────────────────────────────
    if request.method == "GET":
        entries = SearchHistory.objects.filter(user=user).select_related(
            "searched_user__userprofile", "searched_page"
        )[:20]

        results = []
        for e in entries:
            if e.query:
                results.append({"id": e.id, "kind": "query", "query": e.query})

            elif e.searched_user:
                u = e.searched_user
                avatar = None
                if hasattr(u, "userprofile") and u.userprofile.avatar:
                    avatar = request.build_absolute_uri(u.userprofile.avatar.url)
                results.append({
                    "id": e.id,
                    "kind": "user",
                    "user": {
                        "id": u.id,
                        "username": u.username,
                        "avatar": avatar,
                    },
                })

            elif e.searched_page:
                p = e.searched_page
                avatar = None
                if p.avatar:
                    avatar = request.build_absolute_uri(p.avatar.url)
                results.append({
                    "id": e.id,
                    "kind": "page",
                    "page": {
                        "id": p.id,
                        "name": p.name,
                        "is_private": p.is_private,
                        "avatar": avatar,
                    },
                })
            else:
                # Entry has no query, user, or page (e.g. the referenced record
                # was deleted and the FK was set to NULL). Skip silently.
                pass

        return Response(results)

    # ── POST: save a new history entry ───────────────────────────────────
    if request.method == "POST":
        kind = request.data.get("kind")           # "query" | "user" | "page"
        query = request.data.get("query", "").strip()
        target_id = request.data.get("id")        # user_id or page_id

        # Canonical, case-folded form for the analytics / ranking signal so
        # "Vintage Cars" and "vintage cars" collapse to ONE search term
        # (ACTIVITY_AND_FEED_AUDIT.md item A15). The original `query` is kept
        # verbatim on the SearchHistory rows below so the recent-searches UI
        # shows the user exactly what they typed.
        query_norm = query.lower()

        with transaction.atomic():
            if kind == "query":
                if not query:
                    return Response({"error": "query required"}, status=400)

                # Upsert: if the same query already exists, move it to the top
                # by deleting the old one and re-creating it. Dedup is
                # case-INsensitive (iexact) so the recent-searches list doesn't
                # carry "Vintage Cars" and "vintage cars" as two entries; the
                # freshly-created row keeps the latest original casing.
                SearchHistory.objects.filter(user=user, query__iexact=query).delete()
                entry = SearchHistory.objects.create(user=user, query=query)
                log_activity(user, "search_query", query=query_norm)

            elif kind == "user":
                # Coerce + existence-check before INSERT so bad input returns
                # 400/404 instead of crashing the FK constraint with a 500.
                try:
                    target_id = int(target_id)
                except (TypeError, ValueError):
                    return Response({"error": "id required"}, status=400)
                if not User.objects.filter(id=target_id).exists():
                    return Response({"error": "user not found"}, status=404)
                SearchHistory.objects.filter(
                    user=user, searched_user_id=target_id
                ).delete()
                entry = SearchHistory.objects.create(
                    user=user, searched_user_id=target_id
                )
                # Clicking a user from search = search_click
                log_activity(
                    user, "search_click",
                    target_user_id=target_id,
                    query=query_norm,
                    surface="search_results",
                    channel="search",
                )

            elif kind == "page":
                try:
                    target_id = int(target_id)
                except (TypeError, ValueError):
                    return Response({"error": "id required"}, status=400)
                if not Page.objects.filter(id=target_id).exists():
                    return Response({"error": "page not found"}, status=404)
                SearchHistory.objects.filter(
                    user=user, searched_page_id=target_id
                ).delete()
                entry = SearchHistory.objects.create(
                    user=user, searched_page_id=target_id
                )
                # Clicking a page from search = search_click
                log_activity(
                    user, "search_click",
                    page_id=target_id,
                    query=query_norm,
                    surface="search_results",
                    channel="search",
                )

            else:
                return Response({"error": "invalid kind"}, status=400)

            # Keep only the 50 most recent entries per user
            old_ids = list(
                SearchHistory.objects.filter(user=user)
                .values_list("id", flat=True)[50:]
            )
            if old_ids:
                SearchHistory.objects.filter(id__in=old_ids).delete()

        return Response({"id": entry.id}, status=201)

    # ── DELETE: remove a single entry by id ──────────────────────────────
    if request.method == "DELETE":
        try:
            entry_id = int(request.data.get("id"))
        except (TypeError, ValueError):
            return Response({"error": "id required"}, status=400)
        SearchHistory.objects.filter(user=user, id=entry_id).delete()
        return Response(status=204)


