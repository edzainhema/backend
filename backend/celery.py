"""
Celery application for background / deferred work (BACKEND_SCALING_AUDIT.md INF-5).

Why this exists: ranking is offloaded to cron, but per-event fan-out work — push
notifications (SY-2 / WS-3) and media transcoding (SY-1) — has nowhere to go but
the request thread / WebSocket receive loop. This gives that work a home.

Broker: CELERY_BROKER_URL, falling back to REDIS_URL (the same Redis the cache
and channel layer use). Use a dedicated Redis DB index in production
(e.g. redis://host:6379/2) so queue keys don't share space with the cache.

No broker configured? settings.py puts the app in EAGER mode
(CELERY_TASK_ALWAYS_EAGER=True), so `.delay(...)` runs the task inline, in this
process — exactly today's synchronous behaviour. That makes adopting the queue
non-breaking: code can call `.delay(...)` everywhere now, and it only becomes
truly asynchronous once a broker + worker are running.

Tasks live in each app's tasks.py (see api/tasks.py) and are picked up by
autodiscover_tasks(). Run a worker with:  celery -A backend worker -l info
"""
import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")

app = Celery("here")

# Pull every CELERY_* setting from Django settings (namespace="CELERY"),
# e.g. CELERY_TASK_ALWAYS_EAGER -> task_always_eager.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Discover tasks.py in every INSTALLED_APP (api/tasks.py, ...).
app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    """Trivial connectivity check: run `debug_task.delay()` and watch the worker."""
    print(f"[celery] debug_task request: {self.request!r}")
