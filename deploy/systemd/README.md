# Scheduled jobs

## Trash retention sweep

`purge_expired_trash` permanently deletes trashed pages/posts older than 90 days
(soft delete keeps rows + files until then; this is what actually reclaims them).
It's idempotent and safe to run any time — `--dry-run` reports without deleting.

### Option A — systemd timer (recommended on a Linux host)

```bash
sudo cp purge-expired-trash.{service,timer} /etc/systemd/system/
# edit purge-expired-trash.service: User / WorkingDirectory / EnvironmentFile /
# the venv python path to match your deployment
sudo systemctl daemon-reload
sudo systemctl enable --now purge-expired-trash.timer

sudo systemctl list-timers purge-expired-trash.timer   # next run
sudo systemctl start purge-expired-trash.service        # run once now
journalctl -u purge-expired-trash.service               # logs
```

### Option B — cron

```cron
# daily at 03:30
30 3 * * *  cd /srv/here/backend && /srv/here/backend/venv/bin/python manage.py purge_expired_trash >> /var/log/here/purge_trash.log 2>&1
```

### Option C — Celery Beat (if you'd rather schedule it with the app)

The stack already runs Celery; instead of a system timer you can add a beat
entry that calls the command from a tiny task:

```python
# tasks.py
from celery import shared_task
from django.core.management import call_command

@shared_task
def purge_expired_trash_task():
    call_command("purge_expired_trash")
```

```python
# settings.py
from celery.schedules import crontab
CELERY_BEAT_SCHEDULE = {
    "purge-expired-trash": {
        "task": "api.tasks.purge_expired_trash_task",
        "schedule": crontab(hour=3, minute=30),
    },
}
```

(Requires a running `celery beat` process.)

### Windows / local dev

systemd/cron don't apply — just run it by hand when you want to test it:

```
python manage.py purge_expired_trash --dry-run
```
