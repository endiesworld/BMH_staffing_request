# Imported here so the Celery app is created whenever Django starts -- this is
# what makes @shared_task in the apps bind to it.
from .celery import app as celery_app

__all__ = ("celery_app",)
