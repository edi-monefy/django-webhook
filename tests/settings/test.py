# pylint: disable=wildcard-import,unused-wildcard-import
from .base import *

DATABASES = {
    "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": "db.sqlite3"},
    # A secondary database used to verify dispatch defers to the commit of the
    # database an instance was actually written to.
    "secondary": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": "db_secondary.sqlite3",
    },
}

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_TASK_STORE_EAGER_RESULT = True
