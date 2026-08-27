############################################################
#  [*] Scraper — run history and manual triggers (admin)
#
#  Admin-only control surface for the four scrapers that
#  otherwise run on the APScheduler timers in scheduler.py.
#  The trigger routes run the scrape SYNCHRONOUSLY inside
#  the request — the response waits for every page fetch
#  and DB write (tens of seconds to minutes, well past the
#  mobile client's 15 s default timeout) — and nothing
#  stops a manual run from overlapping a timer run of the
#  same scraper: max_instances=1 only guards the
#  scheduler's own job. Each scraper opens a scraper_runs
#  row as 'running' and closes it 'completed'/'failed'
#  itself; /status is the only reader of that table.
#
#  Result dicts are the scrapers' own: news {"found",
#  "new"[, "error"]}, schedule {"groups_scraped",
#  "lessons_found", "lessons_new"} (never an "error" key),
#  info {"pages_scraped", "contacts_found",
#  "programs_found"[, "error"]}. Only /info turns "error"
#  into a 500 — the others answer 200 for a failed scrape.
#
#  Nothing in the mobile app calls any of these (admin.ts
#  only reads scrapedArticles from /api/admin/stats); they
#  are reached through Swagger UI (swagger/swagger.yaml
#  documents all but /run) or curl.
#
#    GET  /api/scraper/status   — last 20 scraper_runs rows
#    POST /api/scraper/trigger  — knf (3 pages) + vu (1) news
#    POST /api/scraper/run      — alias of /trigger
#    POST /api/scraper/schedule — tvarkarasciai.vu.lt timetables
#    POST /api/scraper/info     — knf.vu.lt contacts/programs
############################################################


from flask import Blueprint, jsonify

from app.auth.routes import require_role
from app.database import get_db
from app.scraper.knf_scraper import scrape_knf_news
from app.scraper.schedule_scraper import scrape_knf_schedule
from app.scraper.info_scraper import scrape_faculty_info
from app.scraper.vu_scraper import scrape_vu_news

scraper_bp = Blueprint("scraper", __name__)








############################################################
# scraper_status
############################################################
#
# GET /api/scraper/status
#
# {"runs": [...]} — the 20 newest scraper_runs rows, all
# sources mixed ('knf.vu.lt', 'vu.lt',
# 'tvarkarasciai.vu.lt', 'knf.vu.lt/info'), started_at
# DESC. started_at is SQLite's datetime('now') text
# ("YYYY-MM-DD HH:MM:SS", UTC), so the string sort IS
# chronological. Column names are the news scraper's: for
# a schedule run articlesFound/articlesNew are lesson
# counts, for an info run both hold contacts + programs.
# finishedAt and error are null while a run is 'running' —
# and stay null for a schedule run that failed on the
# group list (it writes neither), for every schedule
# failure's error (that scraper never records a message),
# and for a run whose process died mid-way (the row is
# never reconciled, so it shows 'running' forever). Error
# text is html-escaped on the way out by create_app's
# after_request.
#
# Used by:
#   - nothing calls this at the moment (documented in
#     swagger/swagger.yaml, no mobile caller)
############################################################

@scraper_bp.route("/status", methods=["GET"])
@require_role("admin")
def scraper_status():
    db = get_db()
    try:
        rows = db.execute(
            "SELECT * FROM scraper_runs ORDER BY started_at DESC LIMIT 20"
        ).fetchall()
        runs = [
            {
                "id": r["id"],
                "source": r["source"],
                "status": r["status"],
                "articlesFound": r["articles_found"],
                "articlesNew": r["articles_new"],
                "error": r["error_message"],
                "startedAt": r["started_at"],
                "finishedAt": r["finished_at"],
            }
            for r in rows
        ]
        return jsonify({"runs": runs})
    finally:
        db.close()








############################################################
# trigger_scrape
############################################################
#
# POST /api/scraper/trigger
# POST /api/scraper/run
#
# Runs scrape_knf_news(pages=3) then scrape_vu_news(pages=1)
# back to back in the request thread and answers 200
# {"knf": {...}, "vu": {...}} whatever happened — a scraper
# that failed reports {"found": 0, "new": 0, "error"} under
# its own key. Three knf pages is one more than the timer
# run's two (5 listing entries per page). New articles fire
# the "news" push channel from inside both scrapers, so a
# manual trigger can notify users. /run and /trigger are
# the same function; swagger documents only /trigger.
#
# Used by:
#   - nothing calls this at the moment (documented in
#     swagger/swagger.yaml, no mobile caller)
############################################################

@scraper_bp.route("/trigger", methods=["POST"])
@scraper_bp.route("/run", methods=["POST"])
@require_role("admin")
def trigger_scrape():
    knf_result = scrape_knf_news(pages=3)
    vu_result = scrape_vu_news(pages=1)
    return jsonify({
        "knf": knf_result,
        "vu": vu_result,
    })








############################################################
# trigger_schedule_scrape
############################################################
#
# POST /api/scraper/schedule
#
# scrape_knf_schedule() with its default 16-week window from
# the current semester start (Sept 1 when month >= 8 or
# month <= 1, else Feb 1), for every group that
# tvarkarasciai.vu.lt lists — by far the slowest of the
# four scrapers. Always 200: its failure result is the
# all-zero {"groups_scraped": 0, "lessons_found": 0,
# "lessons_new": 0} with no "error" key, indistinguishable
# from an empty semester except via /status. New lessons
# fire the "schedule" push channel from inside the scraper.
#
# Used by:
#   - nothing calls this at the moment (documented in
#     swagger/swagger.yaml, no mobile caller)
############################################################

@scraper_bp.route("/schedule", methods=["POST"])
@require_role("admin")
def trigger_schedule_scrape():
    result = scrape_knf_schedule()
    return jsonify(result)








############################################################
# trigger_info_scrape
############################################################
#
# POST /api/scraper/info
#
# scrape_faculty_info() — knf.vu.lt contacts, study programs
# and department structure into faculty_info (served by
# /api/info). The one trigger that maps the scraper's
# "error" key to a 500, with the result dict as the body
# (unescaped: after_request skips statuses >= 400). The
# isinstance guard is defensive only — the scraper always
# returns a dict.
#
# Used by:
#   - nothing calls this at the moment (documented in
#     swagger/swagger.yaml, no mobile caller)
############################################################

@scraper_bp.route("/info", methods=["POST"])
@require_role("admin")
def trigger_info_scrape():
    result = scrape_faculty_info()
    if isinstance(result, dict) and result.get("error"):
        return jsonify(result), 500
    return jsonify(result)
