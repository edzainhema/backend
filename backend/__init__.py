# Load the Celery app when Django starts so @shared_task binds to it (the
# standard Celery + Django integration). Guarded so a checkout that hasn't yet
# `pip install`-ed celery still boots Django — the queue is simply unavailable
# until the dependency is present. See BACKEND_SCALING_AUDIT.md INF-5.
try:
    from .celery import app as celery_app
    __all__ = ("celery_app",)
except ImportError:  # celery not installed (e.g. before `pip install`)
    celery_app = None
    __all__ = ()
