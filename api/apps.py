from django.apps import AppConfig


class ApiConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'api'

    def ready(self):
        # Wire up notification cache invalidation. We import inside ready()
        # so model classes are guaranteed to be loaded before we attach
        # signal handlers -- importing models at module top-level here can
        # trigger AppRegistryNotReady on cold start.
        #
        # The cache helper lives at `api.notification_cache` (not inside
        # views/) on purpose: importing from .views.notifications would
        # drag the entire views/__init__.py fan-out into app startup,
        # which is fragile (any syntax error in any view module would
        # then break every Django management command).
        from django.core.cache import cache
        from django.db.models.signals import post_save, post_delete
        from .models import Notification
        from .services.notification_cache import _unread_count_cache_key

        def _invalidate_on_notification_change(sender, instance, **kwargs):
            # Any new notification could change the badge count for the
            # recipient. A notification being read-flipped via .save()
            # also goes through here; .update() is handled by the
            # explicit calls in the mark-read views.
            try:
                cache.delete(_unread_count_cache_key(instance.recipient_id))
            except Exception:
                # Cache failures must never break a notification write.
                pass

        post_save.connect(
            _invalidate_on_notification_change,
            sender=Notification,
            dispatch_uid="invalidate_unread_count_on_notification_save",
        )
        post_delete.connect(
            _invalidate_on_notification_change,
            sender=Notification,
            dispatch_uid="invalidate_unread_count_on_notification_delete",
        )

        # When a user is deleted (admin, a future delete-account endpoint, a data
        # script), tear down their LIVE owned pages first: trash the posts on
        # them so contributors' posts don't orphan into page-less live posts when
        # the page rows cascade-delete with the user (Post.page is SET_NULL).
        # Also cleans up the departing user's own post files. Lives on pre_delete
        # so it always runs, no matter the deletion path.
        from django.db.models.signals import pre_delete
        from django.contrib.auth.models import User as AuthUser

        def _teardown_user_pages_on_delete(sender, instance, **kwargs):
            from .services.page_teardown import teardown_user_owned_pages
            teardown_user_owned_pages(instance)

        pre_delete.connect(
            _teardown_user_pages_on_delete,
            sender=AuthUser,
            dispatch_uid="teardown_user_owned_pages_on_delete",
        )

        # Pin Pillow's decompression-bomb ceiling process-wide at startup. The
        # authoritative value and full rationale live in the upload validator
        # (services/media/validation.py), which also applies it at its own
        # import; we re-apply it here so the cap is guaranteed in force before
        # the first request even if no image-handling module has been imported
        # yet (e.g. a management command or signal that opens an image). Cheap:
        # the validator only pulls in stdlib + PIL, both already loaded.
        from PIL import Image
        from .services.media.validation import IMAGE_MAX_PIXELS
        Image.MAX_IMAGE_PIXELS = IMAGE_MAX_PIXELS
