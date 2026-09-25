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
#
#  The database is the ONE switch: TEST_DATABASE_URL, read
#  with the same django-environ parser settings.py uses for
#  DATABASE_URL. Unset, the suite runs on in-memory SQLite
#  as it always has; set to a postgres:// URL naming a
#  THROWAWAY database it runs the same suite on the engine
#  production uses — deferred foreign keys, strict text
#  (no NUL bytes), collation-aware LIKE, timezone-aware
#  columns, the vendor-split SQL in common/expressions.py.
#  Django creates and drops test_<name> beside the named
#  database, so the URL must never point at the live
#  cluster (settings.py refuses that at import; see its
#  test-run guard). Recipe in django/README.md.
############################################################


import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-secret-key")

from knfapp.settings import *  # noqa: E402,F401,F403
from knfapp.settings import env  # noqa: E402  (the star import hides nothing, this names the dependency)

DATABASES = {
    "default": env.db("TEST_DATABASE_URL", default="sqlite:///:memory:"),
}
DATABASES["default"]["ATOMIC_REQUESTS"] = True

LOGGING = {"version": 1, "disable_existing_loggers": True}
