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
        from .notification_cache import _unread_count_cache_key

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
