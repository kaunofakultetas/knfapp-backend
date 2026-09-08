############################################################
#  [*] Test settings — the production settings, in memory
#
#  Imports the real settings.py so every regression test
#  runs under the production configuration (middleware,
#  APPEND_SLASH, ATOMIC_REQUESTS, no-contrib app list), and
#  swaps only what a throwaway test run must not depend on:
#  the secret gets a harmless default, the database becomes
#  in-memory SQLite, and request logging is silenced
#  because the suite pins many 4xx responses on purpose.
############################################################


import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-secret-key")

from knfapp.settings import *  # noqa: E402,F401,F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
        "ATOMIC_REQUESTS": True,
    }
}

LOGGING = {"version": 1, "disable_existing_loggers": True}
