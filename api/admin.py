from django.contrib import admin
from django.utils import timezone
from .models import Activity, Media,PageInvite, MessageReaction, ProfileVisit, ConversationHidden, PagePoster, MutedPage, PageReport, UserReport, Memory, MutedUser, BlockedUser, PostReport, PostLike, SavedPost, CommentLike, Post, PostMedia, PostMediaTag, Comment, CommentMention, Conversation, Message, Follow, Device, FollowRequest, Notification, UserProfile, Page, PageFollow, PageFollowRequest

admin.site.register(Activity)
admin.site.register(Media)
admin.site.register(Follow)
admin.site.register(Device)
admin.site.register(FollowRequest)
admin.site.register(Notification)
admin.site.register(UserProfile)
admin.site.register(Page)
admin.site.register(PageInvite)
admin.site.register(Memory)
admin.site.register(PageFollow)
admin.site.register(PageFollowRequest)
admin.site.register(Conversation)
admin.site.register(Message)
admin.site.register(MessageReaction)
admin.site.register(Comment)
admin.site.register(CommentMention)
admin.site.register(Post)
admin.site.register(PostMedia)
admin.site.register(PostMediaTag)
admin.site.register(PostLike)
admin.site.register(SavedPost)
admin.site.register(CommentLike)
admin.site.register(MutedUser)
admin.site.register(BlockedUser)
admin.site.register(MutedPage)


# ---------------------------------------------------------------------------
# Moderation queue (audit H4). The three *Report models share a ReportTriage
# base (status / handled_by / resolved_at); these admin classes turn the raw
# tables into a workable queue — filter to the unhandled ones, see who's on a
# row, and resolve in bulk — instead of the bare append-only list they were.
# ---------------------------------------------------------------------------
class _BaseReportAdmin(admin.ModelAdmin):
    list_filter = ("status", "reason", "created_at")
    search_fields = ("reporter__username", "details")
    date_hierarchy = "created_at"
    ordering = ("status", "-created_at")  # open rows first, newest within
    readonly_fields = ("reporter", "reason", "details", "created_at")
    actions = ("mark_reviewing", "mark_actioned", "mark_dismissed")

    @admin.action(description="Mark selected as Reviewing")
    def mark_reviewing(self, request, queryset):
        updated = queryset.update(status="reviewing")
        self.message_user(request, f"{updated} report(s) marked Reviewing.")

    @admin.action(description="Mark selected as Actioned (resolved)")
    def mark_actioned(self, request, queryset):
        updated = queryset.update(
            status="actioned", handled_by=request.user, resolved_at=timezone.now(),
        )
        self.message_user(request, f"{updated} report(s) marked Actioned.")

    @admin.action(description="Mark selected as Dismissed (resolved)")
    def mark_dismissed(self, request, queryset):
        updated = queryset.update(
            status="dismissed", handled_by=request.user, resolved_at=timezone.now(),
        )
        self.message_user(request, f"{updated} report(s) Dismissed.")


@admin.register(PostReport)
class PostReportAdmin(_BaseReportAdmin):
    list_display = ("id", "status", "reason", "reporter", "post", "handled_by", "created_at", "resolved_at")
    list_select_related = ("reporter", "handled_by", "post")


@admin.register(UserReport)
class UserReportAdmin(_BaseReportAdmin):
    list_display = ("id", "status", "reason", "reporter", "reported_user", "handled_by", "created_at", "resolved_at")
    list_select_related = ("reporter", "handled_by", "reported_user")


@admin.register(PageReport)
class PageReportAdmin(_BaseReportAdmin):
    list_display = ("id", "status", "reason", "reporter", "page", "handled_by", "created_at", "resolved_at")
    list_select_related = ("reporter", "handled_by", "page")
admin.site.register(PagePoster)
admin.site.register(ConversationHidden)
admin.site.register(ProfileVisit)
