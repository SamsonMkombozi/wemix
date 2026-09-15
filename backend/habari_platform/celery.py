"""
Celery app config. Defaults to CELERY_TASK_ALWAYS_EAGER=True (see
settings.py) whenever no real broker is configured -- tasks then run
inline, synchronously, in the same process that queued them, which is
exactly today's behavior (AI verification/OCR blocking the request that
triggered them). Nothing changes for anyone until CELERY_BROKER_URL is
set to a real Redis instance AND a worker process is actually running;
until then this is a safe no-op wrapper, not a behavior change.
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "habari_platform.settings")

app = Celery("habari_platform")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
