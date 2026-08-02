import os

from celery import Celery

# Must be set before the app is created: the worker is a separate process with
# no Django settings loaded yet.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("bmh")

# namespace="CELERY" means every Celery option is read from a CELERY_-prefixed
# Django setting, so there is one place to configure the project.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Finds tasks.py in every installed app.
app.autodiscover_tasks()
