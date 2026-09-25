############################################################
#  [*] Django settings — the whole configuration in one file
#
#  Everything environment-specific comes in through env
#  variables (compose supplies them; the defaults below fit
#  a bare runserver):
#
#    DJANGO_DEBUG      — "True" enables debug mode
#    DJANGO_SECRET_KEY — signing key (Django internals only;
#                        API auth is bearer tokens, not
#                        cookies, so no session security
#                        rides on it)
#    DATABASE_URL      — sqlite:///... by default; the
#                        scheme picks the engine, and the
#                        codebase is dual-engine by
#                        construction (portable SQL + the
#                        vendor-split expressions in
#                        common/expressions.py + the psycopg
#                        driver in the image), so
#                        postgres://... is a real env-only
#                        switch
#    TRUSTED_PROXY_HOPS — how many rightmost X-Forwarded-For
#                        entries are the deployment's own
#                        proxies (default 1 — see Hosting)
#
#  Deliberately NO admin site, NO django.contrib.auth and
#  NO sessions: the API authenticates with opaque bearer
#  tokens against its own users/sessions tables
#  (knfapp/users/auth.py), data administration happens
#  through the app's own /api/admin routes and DbGate, and
#  a cookie never carries anything.
############################################################


import os
import sys
from pathlib import Path

import environ
from django.core.exceptions import ImproperlyConfigured


ROOT_DIR = Path(__file__).resolve(strict=True).parent.parent
env = environ.Env()








############################################################
# General
############################################################
#
# Aware UTC everywhere (USE_TZ), and TIME_ZONE pinned to UTC
# on purpose: it is the timezone Django assumes for any NAIVE
# datetime that reaches the ORM, and every naive stamp in
# this system MEANS UTC — Vilnius here would silently shift
# them by the local offset. Nothing renders local time; the
# clients localise. knfapp/common/timestamps.py is the one
# place that knows the wire stamp policy.
############################################################

DEBUG = env.bool("DJANGO_DEBUG", False)
SECRET_KEY = env("DJANGO_SECRET_KEY", default="dev-only-secret-key" if env.bool("DJANGO_DEBUG", False) else environ.Env.NOTSET)
TIME_ZONE = "UTC"
LANGUAGE_CODE = "en-us"
USE_I18N = False
USE_TZ = True








############################################################
# Hosting
############################################################
#
# The service only ever runs behind the Caddy endpoint,
# which forwards whatever Host the client sent. Every host
# is accepted because nothing derives facts from it: no
# absolute URLs, no redirects, no host-dependent cookies.
#
# TRUSTED_PROXY_HOPS — how many RIGHTMOST X-Forwarded-For
# entries are this deployment's own infrastructure. The
# topology is two proxies deep: the host's TLS terminator
# (the outer Caddy) stamps the client's address and hands
# the request to this stack's ingress (endpoint/Caddyfile),
# whose `trusted_proxies private_ranges` keeps that chain
# and APPENDS its own peer, the outer proxy. Django then
# reads "<client>, <outer proxy>": hop 1 from the right is
# the outer proxy, the client sits one further left — so
# the default is 1. This is per-deployment configuration,
# not a constant: a stack put behind a third proxy sets 2,
# a bare runserver with no proxy at all can set 0. The
# value only means something because the ingress refuses
# to trust a chain a PUBLIC peer sent — that half lives in
# the Caddyfile, not here. knfapp/common/http.py
# client_ip() is the one reader.
############################################################

ALLOWED_HOSTS = ["*"]
TRUSTED_PROXY_HOPS = env.int("TRUSTED_PROXY_HOPS", default=1)








############################################################
# Database
############################################################
#
# SQLite in a volume by default; a postgres DATABASE_URL
# switches engines with no code change. ATOMIC_REQUESTS
# wraps every request in one transaction: register's
# invitation-code burn, user INSERT and session mint commit
# together or not at all.
############################################################

DATABASES = {
    "default": env.db("DATABASE_URL", default=f"sqlite:///{ROOT_DIR}/data/knfapp.sqlite3"),
}
DATABASES["default"]["ATOMIC_REQUESTS"] = True

# SQLite under one gthread worker with many threads: WAL lets
# readers run beside the single writer, busy_timeout makes a
# briefly-locked write wait instead of raising "database is
# locked", and synchronous=NORMAL is the WAL-safe durability
# point. Scoped to the sqlite engine — a postgres DATABASE_URL
# ignores all three.
#
# ACCEPTED RISK, on the record: transactions open DEFERRED.
# A view that reads then writes while a sibling process (the
# cron scrapers share this file) commits in between gets
# SQLITE_BUSY_SNAPSHOT — instantly, the busy handler never
# fires — and answers 500; the client's retry lands on a
# fresh snapshot and succeeds. transaction_mode=IMMEDIATE
# would instead take the write lock for EVERY request, reads
# included (ATOMIC_REQUESTS wraps them all), and serialise
# the whole 24-thread server behind it — strictly worse.
# Chat and the scraper triggers are non-atomic and run their
# own BEGIN IMMEDIATE where a read-then-write genuinely
# contends.
if DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3":
    DATABASES["default"].setdefault("OPTIONS", {})["init_command"] = (
        "PRAGMA journal_mode=WAL;"
        "PRAGMA busy_timeout=5000;"
        "PRAGMA synchronous=NORMAL;"
    )

DEFAULT_AUTO_FIELD = "django.db.models.AutoField"


############################################################
# Test-run guard — never build test_<name> on the live cluster
############################################################
#
# `manage.py test` creates test_<NAME> beside the configured
# database and drops it when the run ends — a run killed
# half-way leaves it behind. The live cluster once collected
# eighteen orphaned test_knfapp_* databases from
# `docker exec knfapp-django python3 manage.py test` runs
# that forgot --settings=knfapp.tests.settings and so ran
# against DATABASE_URL. THE RULE: a test run may target
# SQLite, or a database whose NAME differs from the
# production database's — anything else is refused here,
# at import, before the runner opens a connection.
#
# knfapp.tests.settings is the sanctioned entry: it swaps
# DATABASES for TEST_DATABASE_URL (in-memory SQLite unless
# set), so when Django reports it as the settings module in
# charge (--settings sets DJANGO_SETTINGS_MODULE before this
# file loads) the guard judges THAT target — which is how
# the PostgreSQL pass (TEST_DATABASE_URL naming a throwaway
# database, README "Tests") passes while the same URL
# naming the live database is still refused.
# KNFAPP_ALLOW_TEST_ON_PROD_DB=1 is the operator override.
############################################################

def _refuse_test_on_production_db():
    # STEP 1: only a test run is judged, and only unless
    # the operator has said otherwise
    # ===============================================
    if "test" not in sys.argv or env.bool("KNFAPP_ALLOW_TEST_ON_PROD_DB", False):
        return


    # STEP 2: what the runner will actually open — the test
    # settings' own choice when they are in charge, else
    # DATABASE_URL itself
    # =====================================================
    production = DATABASES["default"]
    if os.environ.get("DJANGO_SETTINGS_MODULE") == "knfapp.tests.settings":
        target = env.db("TEST_DATABASE_URL", default="sqlite:///:memory:")
    else:
        target = production


    # STEP 3: SQLite is always fine; a different database
    # name is fine; the production name is not
    # ===================================================
    if target["ENGINE"] == "django.db.backends.sqlite3":
        return
    if target.get("NAME") != production.get("NAME"):
        return
    raise ImproperlyConfigured(
        f"Refusing to run the test suite against the production database "
        f"{target.get('NAME')!r} ({target['ENGINE']}): Django would create "
        f"test_{target.get('NAME')} on the live cluster. Run with "
        f"--settings=knfapp.tests.settings (in-memory SQLite), or point "
        f"TEST_DATABASE_URL at a throwaway database — see django/README.md. "
        f"KNFAPP_ALLOW_TEST_ON_PROD_DB=1 overrides this guard."
    )

_refuse_test_on_production_db()








############################################################
# URLs
############################################################
#
# All /api/* routes live in one file (knfapp/urls.py), the
# table of contents of the whole API. APPEND_SLASH is off:
# the paths carry no trailing slashes and redirecting a
# POST would break the clients.
############################################################

ROOT_URLCONF = "knfapp.urls"
WSGI_APPLICATION = "knfapp.wsgi.application"
APPEND_SLASH = False








############################################################
# Uploads
############################################################
#
# Where the stored files live (a volume in compose; a local
# data/ dir for a bare runserver) and the multipart ceiling
# — a video plus its envelope, mirroring the Caddy
# /api/uploads body limit, so oversized bodies die at the
# edges and never spool here. The per-kind size caps live
# with the byte gates (knfapp/uploads/gates.py).
############################################################

UPLOAD_DIR = env("UPLOAD_DIR", default=str(ROOT_DIR / "data" / "uploads"))
MEMES_DIR = env("MEMES_DIR", default=str(ROOT_DIR / "data" / "memes"))
DATA_UPLOAD_MAX_MEMORY_SIZE = 52 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 52 * 1024 * 1024








############################################################
# AI assistant
############################################################
#
# The faculty AI gateway (ai.knf.vu.lt, OpenAI wire format)
# — Django only ever calls its EMBEDDINGS endpoint; chat
# completions belong to the assistant container. The model
# and dimensions pin what every support_chunks row stores,
# and ASSISTANT_INTERNAL_SECRET gates the /internal/
# assistant routes the container calls (empty = the routes
# refuse everything, failing closed).
############################################################

AI_GATEWAY_URL = env("AI_GATEWAY_URL", default="https://ai.knf.vu.lt/v1")
AI_GATEWAY_KEY = env("AI_GATEWAY_KEY", default="")
AI_EMBED_MODEL = env("AI_EMBED_MODEL", default="text-embedding-3-small")
AI_EMBED_DIMENSIONS = env.int("AI_EMBED_DIMENSIONS", default=1536)
ASSISTANT_INTERNAL_SECRET = env("ASSISTANT_INTERNAL_SECRET", default="")








############################################################
# Apps
############################################################
#
# Only the project's own apps — no contrib at all (which is
# what frees the 'admin' label for knfapp.admin). Each app
# owns its models and its api/ views.
############################################################

INSTALLED_APPS = [
    "knfapp.users.apps.UsersConfig",
    "knfapp.notifications.apps.NotificationsConfig",
    "knfapp.uploads.apps.UploadsConfig",
    "knfapp.social.apps.SocialConfig",
    "knfapp.news.apps.NewsConfig",
    "knfapp.schedule.apps.ScheduleConfig",
    "knfapp.info.apps.InfoConfig",
    "knfapp.memes.apps.MemesConfig",
    "knfapp.admin.apps.AdminConfig",
    "knfapp.scraper.apps.ScraperConfig",
    "knfapp.chat.apps.ChatConfig",
    "knfapp.wayfind.apps.WayfindConfig",
    "knfapp.ops.apps.OpsConfig",
    "knfapp.assistant.apps.AssistantConfig",
]








############################################################
# Middleware
############################################################
#
# CommonMiddleware only. No CSRF (bearer tokens, no cookie
# auth — there is nothing for a cross-site form to ride),
# no sessions, no auth middleware — knfapp/users/auth.py
# decorators resolve the bearer per route.
############################################################

MIDDLEWARE = [
    "django.middleware.common.CommonMiddleware",
]








############################################################
# Templates
############################################################
#
# Only Django's debug error pages use these — the service
# itself is JSON-only.
############################################################

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
            ],
        },
    }
]








############################################################
# Logging
############################################################
#
# Everything to the console — that is what
# `docker logs knfapp-django` will show. INFO and above by
# default; the handler allows DEBUG so one logger-level
# change suffices when digging.
############################################################

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "%(levelname)s %(asctime)s %(module)s "
            "%(process)d %(thread)d %(message)s"
        }
    },
    "handlers": {
        "console": {
            "level": "DEBUG",
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        }
    },
    "root": {"level": "INFO", "handlers": ["console"]},
}
