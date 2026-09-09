############################################################
#  [*] Scraper API — run history and manual triggers (admin)
#
#  Admin-only control surface for the four scrapers that
#  otherwise run on the cron container's ticks. The trigger
#  routes run the scrape SYNCHRONOUSLY inside the request —
#  the response waits for every page fetch and DB write
#  (tens of seconds to minutes, well past the mobile
#  client's 15 s default timeout). A manual run that
#  overlaps a cron run of the same scraper does not race it:
#  the two-layer run lock (module Lock + open_run's
#  conditional INSERT) hands the loser "skipped": true, and
#  the route turns that into 409. Each scraper opens a
#  scraper_runs row as 'running' and closes it
#  'completed'/'failed' itself; /status reads that table.
#
#  Every handler here is @transaction.non_atomic_requests:
#  the scrapers draw their own transaction boundaries (the
#  run row must be committed while the fetches take their
#  minutes), and one request-wide transaction would also
#  hold the SQLite write lock across the whole scrape.
#
#  Status mapping, the same for all four triggers:
#    200 — the scrape ran
#    409 — a run of that scraper was already going
#    502 — the scrape failed (the scraper's "error" key)
#  The body carries a stable slug plus the run id; the
#  exception text lives in the log and (truncated) in
#  scraper_runs.error_message.
#
#  Result dicts are the scrapers' own, each with "runId":
#  news {"found", "new"[, "error"|"skipped"]}, schedule
#  {"groups_scraped", "lessons_found", "lessons_new",
#  "dropped"[, "error"|"skipped"]}, info {"pages_scraped",
#  "contacts_found", "programs_found"[, "error"|"skipped"]}.
#
#  The triggers pass notify=False: a hand-fired scrape must
#  not push a notification to every device in the faculty,
#  and it walks the same number of listing pages the timer
#  run does.
#
#  Nothing in the mobile app calls any of these; they are
#  reached through Swagger UI or curl.
#
#    GET  /api/scraper/status   — last 20 runs + a per-source
#                                 summary, ?source/?status
#    POST /api/scraper/trigger  — knf (2 pages) + vu (1) news
#    POST /api/scraper/run      — alias of /trigger
#    POST /api/scraper/schedule — tvarkarasciai.vu.lt timetables
#    POST /api/scraper/info     — knf.vu.lt contacts/programs
############################################################


import logging

from django.db import transaction

from knfapp.common.http import json_response
from knfapp.scraper.info_scraper import scrape_faculty_info
from knfapp.scraper.knf_scraper import scrape_knf_news
from knfapp.scraper.models import ScraperRun
from knfapp.scraper.schedule_scraper import scrape_knf_schedule
from knfapp.scraper.vu_scraper import scrape_vu_news
from knfapp.users.auth import require_role

logger = logging.getLogger(__name__)

# What a client is told when a scrape failed. The exception
# text goes to the log and (truncated) into
# scraper_runs.error_message; an admin reads it there, not
# out of an HTTP body
ERROR_SLUG = "scrape_failed"

# The statuses /status accepts as a ?status filter
_RUN_STATUSES = ("running", "completed", "failed")

_RUN_FIELDS = ("id", "source", "status", "articles_found", "articles_new",
               "error_message", "started_at", "finished_at")








############################################################
# scraper_status
############################################################
#
# GET /api/scraper/status
#   ?source=<source>  — only that source's runs
#   ?status=running|completed|failed
#
# {"runs": [...], "sources": [...]} — the 20 newest
# scraper_runs rows, all sources mixed ('knf.vu.lt',
# 'vu.lt', 'tvarkarasciai.vu.lt', 'knf.vu.lt/info'),
# started_at DESC — a datetime column, so the sort is
# chronological on both engines — and rows older than 30
# days are pruned at the end of every run, except each
# source's newest, which is kept whatever its age.
#
# "sources" is one entry per source with its latest run, its
# latest SUCCESS and its latest FAILURE. Twenty mixed rows
# are four news runs' worth, so a scraper failing every 24 h
# for a month would be invisible in the array alone — the
# summary makes "info last succeeded 41 days ago" a fact on
# the page.
#
# The column names are the news scraper's, and the wire
# keys articlesFound/articlesNew keep them; itemsFound/
# itemsNew carry the same two numbers under a name that
# fits every source. finishedAt and error are null while a
# run is 'running', and stay null for a run whose process
# died mid-way until the daily reconcile closes it.
#
# An unknown ?status is ignored rather than refused — the
# route is a dashboard.
#
# Used by:
#   - nothing in the mobile app — Swagger UI / curl
############################################################

@require_role("admin")
def scraper_status(request):
    # STEP 1: the optional filters
    # ============================
    source_filter = (request.GET.get("source") or "").strip()
    status_filter = (request.GET.get("status") or "").strip().lower()
    if status_filter not in _RUN_STATUSES:
        status_filter = ""


    # STEP 2: the newest runs, filtered on demand
    # ===========================================
    base = ScraperRun.objects.order_by("-started_at")
    if source_filter:
        base = base.filter(source=source_filter)
    if status_filter:
        base = base.filter(status=status_filter)

    runs = [_run_row(r) for r in base.values(*_RUN_FIELDS)[:20]]


    # STEP 3: the per-source summary — latest, latest success,
    # latest failure, one small indexed query each
    # ========================================================
    sources = []
    for source in (ScraperRun.objects.order_by("source")
                   .values_list("source", flat=True).distinct()):
        if source_filter and source != source_filter:
            continue

        sources.append({
            "source": source,
            "latest": _latest_run(source),
            "lastSuccess": _latest_run(source, "completed"),
            "lastFailure": _latest_run(source, "failed"),
        })

    return json_response({"runs": runs, "sources": sources}, naive_stamps=True)


def _run_row(r) -> dict:
    return {
        "id": r["id"],
        "source": r["source"],
        "status": r["status"],
        "articlesFound": r["articles_found"],
        "articlesNew": r["articles_new"],
        # Same numbers, source-neutral names — the two
        # above serve the existing consumers
        "itemsFound": r["articles_found"],
        "itemsNew": r["articles_new"],
        "error": r["error_message"],
        # The status wire's stamp shape is NAIVE UTC — the
        # naive_stamps encoder strips the offsets
        "startedAt": r["started_at"],
        "finishedAt": r["finished_at"],
    }


def _latest_run(source: str, status: str = ""):
    base = ScraperRun.objects.filter(source=source)
    if status:
        base = base.filter(status=status)

    row = base.order_by("-started_at").values(*_RUN_FIELDS).first()
    return _run_row(row) if row else None








############################################################
# The triggers
############################################################
#
# POST /api/scraper/trigger (alias /run) — knf 2 pages then
# vu 1 page back to back in the request thread, answering
# {"knf": {...}, "vu": {...}}. POST /api/scraper/schedule —
# the full timetable import with its default rolling window.
# POST /api/scraper/info — contacts/programs/structure. The
# page counts are the cron run's on purpose — a manual
# trigger reaching one page deeper than the schedule would
# make "did the trigger work?" unanswerable — and every
# trigger runs with notify=False.
#
# _public_result turns a raw "error" into the stable slug
# (the counts pass through); _trigger_status maps the
# results onto 502 / 409 / 200, failure winning over
# "already running" — an admin firing a trigger needs to
# hear about the break first.
#
# Used by:
#   - nothing in the mobile app — Swagger UI / curl
############################################################

@transaction.non_atomic_requests
@require_role("admin")
def trigger_scrape(request):
    knf_result = scrape_knf_news(pages=2, notify=False)
    vu_result = scrape_vu_news(pages=1, notify=False)

    body = {
        "knf": _public_result(knf_result, "knf.vu.lt"),
        "vu": _public_result(vu_result, "vu.lt"),
    }

    return json_response(body, status=_trigger_status(knf_result, vu_result))


@transaction.non_atomic_requests
@require_role("admin")
def trigger_schedule_scrape(request):
    result = scrape_knf_schedule(notify=False)

    return json_response(_public_result(result, "tvarkarasciai.vu.lt"),
                         status=_trigger_status(result))


@transaction.non_atomic_requests
@require_role("admin")
def trigger_info_scrape(request):
    result = scrape_faculty_info()

    return json_response(_public_result(result, "knf.vu.lt/info"),
                         status=_trigger_status(result))


def _public_result(result, source: str) -> dict:
    # The counts pass through; a raw exception text becomes the
    # stable slug — the original is logged here and stored
    # (truncated) in scraper_runs.error_message by the scraper
    if not isinstance(result, dict):
        return {"error": ERROR_SLUG, "source": source}

    public = dict(result)

    if public.get("error"):
        logger.warning("%s scrape failed: %s", source, public["error"])
        public["error"] = ERROR_SLUG

    return public


def _trigger_status(*results) -> int:
    # Failure wins over "already running"; a result that is not
    # a dict is a failure too — _public_result has already
    # turned it into an ERROR_SLUG body, and a 200 beside that
    # body would call the run fine
    if any(not isinstance(result, dict) for result in results):
        return 502

    if any(result.get("error") for result in results):
        return 502

    if any(result.get("skipped") for result in results):
        return 409

    return 200
