"""Engagement count subqueries (NQ-1): one correlated COUNT per relation
instead of stacking Count(rel, distinct=True), which would LEFT JOIN all
three and produce an O(likes * comments * saves) cartesian product."""
from django.db.models import Count, IntegerField, OuterRef, Subquery
from django.db.models.functions import Coalesce

from ...models import Comment, PostLike, SavedPost


def _post_relation_count(model, outer):
    return Coalesce(
        Subquery(
            model.objects
            .filter(post=OuterRef(outer))
            .order_by()
            .values("post")
            .annotate(_c=Count("*"))
            .values("_c"),
            output_field=IntegerField(),
        ),
        0,
    )


def likes_count_subquery(outer="pk"):
    return _post_relation_count(PostLike, outer)


def comments_count_subquery(outer="pk"):
    return _post_relation_count(Comment, outer)


def saves_count_subquery(outer="pk"):
    return _post_relation_count(SavedPost, outer)


