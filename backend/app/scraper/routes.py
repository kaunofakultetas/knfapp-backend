############################################################
#  [*] Scraper — run history and manual triggers (admin)
#
#  Admin-only control surface for the four scrapers that
#  otherwise run on the APScheduler timers in scheduler.py.
#  The trigger routes run the scrape SYNCHRONOUSLY inside
#  the request — the response waits for every page fetch
#  and DB write (tens of seconds to minutes, well past the
#  mobile client's 15 s default timeout). A manual run that
#  overlaps a timer run of the same scraper does not race
#  it: every scraper holds its own module-level lock, a
#  trigger that finds it taken comes back "skipped": true,
#  and the route turns that into 409. Each scraper opens a
#  scraper_runs row as 'running' and closes it
#  'completed'/'failed' itself; /status reads that table.
#
#  Status mapping, the same for all four triggers:
#    200 — the scrape ran
#    409 — a run of that scraper was already going
#    502 — the scrape failed (the scraper's "error" key)
#  A failing scrape used to answer 200 with the exception
#  text in the body; now the body carries a stable slug plus
#  the run id, and the exception stays in the log.
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
#  Nothing in the mobile app calls any of these (admin.ts
#  only reads scrapedArticles from /api/admin/stats); they
#  are reached through Swagger UI (swagger/swagger.yaml
#  documents all four paths) or curl.
#
#    GET  /api/scraper/status   — last 20 runs + a per-source
#                                 summary, ?source/?status
#    POST /api/scraper/trigger  — knf (2 pages) + vu (1) news
#    POST /api/scraper/run      — alias of /trigger
#    POST /api/scraper/schedule — tvarkarasciai.vu.lt timetables
#    POST /api/scraper/info     — knf.vu.lt contacts/programs
############################################################


import logging

from flask import Blueprint, jsonify, request

from app.auth.routes import require_role
from app.database import get_db
from app.scraper.knf_scraper import scrape_knf_news
from app.scraper.schedule_scraper import scrape_knf_schedule
from app.scraper.info_scraper import scrape_faculty_info
from app.scraper.vu_scraper import scrape_vu_news

logger = logging.getLogger(__name__)

scraper_bp = Blueprint("scraper", __name__)

# What a client is told when a scrape failed. The exception
# text goes to the log and (truncated) into
# scraper_runs.error_message; an admin reads it there, not
# out of an HTTP body
ERROR_SLUG = "scrape_failed"

# The statuses /status accepts as a ?status filter
_RUN_STATUSES = ("running", "completed", "failed")








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
# started_at DESC. started_at is ISO-8601 UTC text stamped
# by the scraper (the SQLite datetime('now') DEFAULT is a
# never-firing backstop), so the string sort IS
# chronological, and rows older than 30 days are pruned at
# the end of every run — except each source's newest, which
# is kept whatever its age.
#
# "sources" is the block this endpoint was missing: one
# entry per source with its latest run, its latest SUCCESS
# and its latest FAILURE. Twenty mixed rows are four news
# runs' worth, so a scraper that has been failing every 24 h
# for a month was invisible — the summary makes "info last
# succeeded 41 days ago" a fact on the page.
#
# The column names are the news scraper's, and the wire
# keys articlesFound/articlesNew keep them; itemsFound/
# itemsNew carry the same two numbers under a name that
# fits every source. Per source:
#   - knf.vu.lt / vu.lt — articles considered / rows
#     inserted
#   - tvarkarasciai.vu.lt — lessons scraped / lessons that
#     were not already stored
#   - knf.vu.lt/info — contacts + programs found / SECTIONS
#     whose stored blob actually changed
#
# finishedAt and error are null while a run is 'running',
# and stay null for a run whose process died mid-way (the
# row is never reconciled, so it shows 'running' forever).
# An info run that completed with one section broken is
# 'completed' WITH an error naming that section. Error text
# is html-escaped on the way out by create_app's
# after_request.
#
# Used by:
#   - nothing calls this at the moment (documented in
#     swagger/swagger.yaml, no mobile caller)
############################################################

@scraper_bp.route("/status", methods=["GET"])
@require_role("admin")
def scraper_status():
    # STEP 1: the optional filters — an unknown status is
    # ignored rather than refused, the route is a dashboard
    # =====================================================
    source_filter = (request.args.get("source") or "").strip()
    status_filter = (request.args.get("status") or "").strip().lower()
    if status_filter not in _RUN_STATUSES:
        status_filter = ""

    db = get_db()
    try:
        # STEP 2: the newest runs, filtered on demand — the
        # array shape is unchanged, only what fills it
        # =================================================
        sql = "SELECT * FROM scraper_runs"
        params = []
        clauses = []
        if source_filter:
            clauses.append("source = ?")
            params.append(source_filter)
        if status_filter:
            clauses.append("status = ?")
            params.append(status_filter)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY started_at DESC LIMIT 20"

        runs = [_run_row(r) for r in db.execute(sql, params).fetchall()]


        # STEP 3: the per-source summary — latest, latest
        # success, latest failure, one small indexed query each
        # =====================================================
        sources = []
        for row in db.execute("SELECT DISTINCT source FROM scraper_runs ORDER BY source").fetchall():
            source = row["source"]
            if source_filter and source != source_filter:
                continue

            sources.append({
                "source": source,
                "latest": _latest_run(db, source),
                "lastSuccess": _latest_run(db, source, "completed"),
                "lastFailure": _latest_run(db, source, "failed"),
            })

        return jsonify({"runs": runs, "sources": sources})
    finally:
        db.close()








############################################################
# _run_row
############################################################
#
# One scraper_runs row in wire shape. The column names are
# the news scraper's and the articlesFound/articlesNew keys
# keep them; itemsFound/itemsNew carry the same two numbers
# under a name that fits every source.
#
# Used by:
#   - scraper_status (above) — the runs array
#   - _latest_run (below) — every summary entry
############################################################

def _run_row(r) -> dict:
    return {
        "id": r["id"],
        "source": r["source"],
        "status": r["status"],
        "articlesFound": r["articles_found"],
        "articlesNew": r["articles_new"],
        # Same numbers, source-neutral names — the two
        # above stay for the existing consumers
        "itemsFound": r["articles_found"],
        "itemsNew": r["articles_new"],
        "error": r["error_message"],
        "startedAt": r["started_at"],
        "finishedAt": r["finished_at"],
    }








############################################################
# _latest_run
############################################################
#
# The newest run of one source, optionally in one status, or
# None when there is none. Rides migration v36's
# scraper_runs(source, started_at DESC) index, so the three
# calls per source stay three index seeks.
#
# Used by:
#   - scraper_status (above) — latest / lastSuccess /
#     lastFailure per source
############################################################

def _latest_run(db, source: str, status: str = ""):
    sql = "SELECT * FROM scraper_runs WHERE source = ?"
    params = [source]
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY started_at DESC LIMIT 1"

    row = db.execute(sql, params).fetchone()

    return _run_row(row) if row else None








############################################################
# trigger_scrape
############################################################
#
# POST /api/scraper/trigger
# POST /api/scraper/run
#
# Runs scrape_knf_news(pages=2) then scrape_vu_news(pages=1)
# back to back in the request thread and answers
# {"knf": {...}, "vu": {...}}:
#   200 — both ran
#   409 — one of them was already running (its own lock)
#   502 — one of them failed
# The page counts are the timer run's on purpose — a manual
# trigger reaching one page deeper than the schedule made
# "did the trigger work?" unanswerable — and both run with
# notify=False, so a hand-fired scrape never pushes to every
# device. /run and /trigger are the same function.
#
# Used by:
#   - nothing calls this at the moment (documented in
#     swagger/swagger.yaml, no mobile caller)
############################################################

@scraper_bp.route("/trigger", methods=["POST"])
@scraper_bp.route("/run", methods=["POST"])
@require_role("admin")
def trigger_scrape():
    knf_result = scrape_knf_news(pages=2, notify=False)
    vu_result = scrape_vu_news(pages=1, notify=False)

    body = {
        "knf": _public_result(knf_result, "knf.vu.lt"),
        "vu": _public_result(vu_result, "vu.lt"),
    }

    return jsonify(body), _trigger_status(knf_result, vu_result)








############################################################
# trigger_schedule_scrape
############################################################
#
# POST /api/scraper/schedule
#
# scrape_knf_schedule() with its default rolling window
# ([today - 2 weeks, today + 20 weeks]) for every group that
# tvarkarasciai.vu.lt lists — by far the slowest of the four
# scrapers, and it runs with notify=False. 200 when it ran,
# 409 when the 6 h job is still going, 502 when it failed:
# the scraper now carries an "error" key on both failure
# branches, so a failed import is no longer reported as an
# empty semester.
#
# Used by:
#   - nothing calls this at the moment (documented in
#     swagger/swagger.yaml, no mobile caller)
############################################################

@scraper_bp.route("/schedule", methods=["POST"])
@require_role("admin")
def trigger_schedule_scrape():
    result = scrape_knf_schedule(notify=False)

    return jsonify(_public_result(result, "tvarkarasciai.vu.lt")), _trigger_status(result)








############################################################
# trigger_info_scrape
############################################################
#
# POST /api/scraper/info
#
# scrape_faculty_info() — knf.vu.lt contacts, study programs
# and department structure into faculty_info (served by
# /api/info). 200 when it ran, 409 when the 24 h job is
# still going, 502 when it failed — the 500 it used to
# answer claimed the fault was ours when the source site was
# down. The body is unescaped on a failure (after_request
# skips statuses >= 400), which is the other reason the raw
# exception text no longer goes into it.
#
# Used by:
#   - nothing calls this at the moment (documented in
#     swagger/swagger.yaml, no mobile caller)
############################################################

@scraper_bp.route("/info", methods=["POST"])
@require_role("admin")
def trigger_info_scrape():
    result = scrape_faculty_info()

    return jsonify(_public_result(result, "knf.vu.lt/info")), _trigger_status(result)








############################################################
# _public_result
############################################################
#
# A scraper's result dict as the body a client may see: the
# counts pass through, and an "error" carrying the raw
# exception text becomes the stable ERROR_SLUG. The original
# text is logged here and stored (truncated) in
# scraper_runs.error_message by the scraper itself, so an
# admin loses nothing — a stack-trace fragment in an HTTP
# response is what they lose.
#
# Used by:
#   - trigger_scrape, trigger_schedule_scrape,
#     trigger_info_scrape (above)
############################################################

def _public_result(result, source: str) -> dict:
    if not isinstance(result, dict):
        return {"error": ERROR_SLUG, "source": source}

    public = dict(result)

    if public.get("error"):
        logger.warning("%s scrape failed: %s", source, public["error"])
        public["error"] = ERROR_SLUG

    return public








############################################################
# _trigger_status
############################################################
#
# The status code for one or more scraper results: 502 when
# any of them failed, 409 when any stepped aside because
# that scraper's lock was held, 200 otherwise. Failure wins
# over "already running" — an admin firing a trigger needs
# to hear about the break first. These routes are admin-only
# with no mobile caller, so the non-200 answers are safe to
# introduce.
#
# A result that is not a dict is a failure too: _public_result
# has already turned it into an ERROR_SLUG body, and the 200
# beside that body told an admin the run was fine.
#
# Used by:
#   - trigger_scrape, trigger_schedule_scrape,
#     trigger_info_scrape (above)
############################################################

def _trigger_status(*results) -> int:
    if any(not isinstance(result, dict) for result in results):
        return 502

    if any(result.get("error") for result in results):
        return 502

    if any(result.get("skipped") for result in results):
        return 409

    return 200
