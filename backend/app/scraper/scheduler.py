############################################################
#  [*] Scraper scheduler — the background scrape timers
#
#  One APScheduler BackgroundScheduler per process, started
#  from create_app (app/__init__.py) right after the
#  blueprints, running three interval jobs: news every
#  20 min (knf 2 pages + vu 1 page), timetables every 6 h,
#  faculty info every 24 h. An interval job first fires one
#  full interval after start(), so the startup gap is
#  covered by three threading.Timer one-shots at 2 s / 30 s
#  / 60 s — staggered so a boot does not hit knf.vu.lt and
#  tvarkarasciai.vu.lt all at once.
#
#  Worth knowing:
#    - the _scheduler guard is per PROCESS. With APP_DEBUG=1
#      the Werkzeug reloader builds the app in both the
#      parent and the child, so two schedulers run the same
#      jobs (compose keeps APP_DEBUG off).
#    - main.py calls create_app() before it looks at --http,
#      so `python main.py` with no flags prints the help AND
#      then sits through a full scrape round: the Timer
#      threads are non-daemon (inherited from the main
#      thread) and keep the interpreter alive until the 60 s
#      info scrape has finished.
#    - max_instances=1 makes APScheduler SKIP (with a
#      warning) a tick whose previous run is still going;
#      the Timer runs and the manual /api/scraper/* triggers
#      are plain threads it knows nothing about, so those
#      can overlap a job.
#    - nothing ever calls shutdown(); the daemon scheduler
#      thread simply dies with the process, and a scrape
#      cut off that way leaves its scraper_runs row
#      'running' forever.
#
#  The scrapers record success/failure in scraper_runs
#  themselves; the closures here only push an app context
#  (belt and braces — nothing on the scrape path reads
#  current_app, get_db opens the module-level _db_path that
#  init_db set) and add a last-resort log line.
############################################################


import logging
from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)

# Set on the first start_scraper_scheduler call and never
# cleared — this module-level singleton IS the re-entry guard
_scheduler = None








############################################################
# start_scraper_scheduler
############################################################
#
# Idempotent per process: a second call returns at once.
# Builds the scheduler, defines the three job closures over
# `app` (each wraps itself in app.app_context() and imports
# its scraper lazily, so a broken scraper module fails that
# run in the log instead of the app boot), registers them
# as interval jobs, starts the daemon thread, and arms the
# one-shot startup Timers. Returns nothing and there is no
# way to stop it afterwards.
#
# Used by:
#   - app/__init__.py — create_app, once per process
############################################################

def start_scraper_scheduler(app):
    # STEP 1: per-process re-entry guard, then the scheduler
    # itself (daemon=True: its thread never blocks exit)
    # ======================================================
    global _scheduler

    if _scheduler is not None:
        return

    _scheduler = BackgroundScheduler(daemon=True)


    # STEP 2: the job bodies — closures over `app` so every
    # run gets an app context (belt and braces — nothing on
    # the scrape path reads current_app); the scrapers are
    # imported per run. The except clauses only see what
    # happens OUTSIDE the scrapers (import errors, context
    # problems) — the scrapers catch their own failures and
    # mark the run 'failed' in scraper_runs
    # =====================================================
    def run_scrapers():
        with app.app_context():
            try:
                from app.scraper.knf_scraper import scrape_knf_news
                from app.scraper.vu_scraper import scrape_vu_news

                logger.info("Running scheduled scrape...")
                knf_result = scrape_knf_news(pages=2)
                vu_result = scrape_vu_news(pages=1)
                logger.info("Scrape done: knf=%s, vu=%s", knf_result, vu_result)
            except Exception:
                logger.exception("Scheduled scrape failed")

    def run_schedule_scraper():
        with app.app_context():
            try:
                from app.scraper.schedule_scraper import scrape_knf_schedule

                logger.info("Running scheduled schedule scrape...")
                result = scrape_knf_schedule()
                logger.info("Schedule scrape done: %s", result)
            except Exception:
                logger.exception("Scheduled schedule scrape failed")

    def run_info_scraper():
        with app.app_context():
            try:
                from app.scraper.info_scraper import scrape_faculty_info

                logger.info("Running scheduled faculty info scrape...")
                result = scrape_faculty_info()
                logger.info("Faculty info scrape done: %s", result)
            except Exception:
                logger.exception("Scheduled faculty info scrape failed")


    # STEP 3: register the interval jobs and start the thread;
    # each job's first tick is one full interval away
    # ========================================================
    _scheduler.add_job(run_scrapers, "interval", minutes=20, id="news_scraper", max_instances=1)
    # timetables change rarely — 6 h keeps tvarkarasciai.vu.lt load low
    _scheduler.add_job(run_schedule_scraper, "interval", hours=6, id="schedule_scraper", max_instances=1)
    # faculty info changes even more rarely
    _scheduler.add_job(run_info_scraper, "interval", hours=24, id="info_scraper", max_instances=1)
    _scheduler.start()


    # STEP 4: cover the startup gap with one-shot Timers,
    # staggered so a boot does not hit every site at once
    # ===================================================
    # mid-function import: threading is used nowhere else here
    import threading
    threading.Timer(2.0, run_scrapers).start()
    threading.Timer(30.0, run_schedule_scraper).start()
    threading.Timer(60.0, run_info_scraper).start()
    logger.info("Scraper scheduler started (news: 20min, schedule: 6h, info: 24h)")
