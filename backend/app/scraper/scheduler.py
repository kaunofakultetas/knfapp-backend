############################################################
#  [*] Scraper scheduler — the background scrape timers
#
#  One APScheduler BackgroundScheduler per process, started
#  from main.py's --http branch (never from create_app: a
#  CLI invocation, a test import or a `flask shell` used to
#  build the app and, seconds later, scrape live sites and
#  send real pushes). Seven interval jobs:
#
#    news every 20 min      — knf 2 pages + vu 1 page
#    timetables every 6 h   — tvarkarasciai.vu.lt
#    faculty info every 24 h— knf.vu.lt contacts/programs
#    push receipts every 15 min — the clock for
#      notifications/push.py's poll_push_receipts, which
#      trades Expo tickets for their delivery verdict
#    session sweep every 24 h — expired sessions rows
#    push-token prune every 24 h — tokens whose owner has
#      no live session left
#    run reconcile every 24 h — scraper_runs rows a killed
#      process left at 'running'
#
#  Two env gates, both defaulting to ON so compose behaves
#  as before:
#    SCRAPER_ENABLED=0        — no scheduler at all
#    SCRAPER_STARTUP_RUNS=0   — scheduler, but no startup
#                               one-shots
#
#  An interval job first fires one full interval after
#  start(), so the startup gap is covered by three
#  threading.Timer one-shots at ~2 s / ~30 s / ~60 s (plus
#  jitter) — staggered so a boot does not hit knf.vu.lt and
#  tvarkarasciai.vu.lt at once, daemon threads so a process
#  that is not serving can exit instead of sitting through a
#  scrape round, and SKIPPED when that source already
#  completed a run inside its own interval: a crash loop
#  must not hammer the source sites.
#
#  Worth knowing:
#    - the _scheduler guard is per PROCESS. With APP_DEBUG=1
#      the Werkzeug reloader would build the app in both the
#      parent and the child; main.py passes use_reloader=
#      False so there is only ever one process (compose
#      keeps APP_DEBUG off anyway).
#    - max_instances=1 makes APScheduler SKIP (with a
#      warning) a tick whose previous run is still going;
#      the Timer runs and the manual /api/scraper/* triggers
#      are plain threads it knows nothing about, so those
#      can overlap a job — each scraper holds its own
#      module-level lock and steps aside instead.
#    - misfire_grace_time is 300 s on every job: the default
#      is ONE second, so a tick delayed by a long scrape or
#      a busy container was simply dropped, silently. A
#      missed tick now logs its own WARNING.
#    - stop_scraper_scheduler() shuts the scheduler down and
#      clears the global; main.py calls it from its exit
#      path, and a scrape cut off anyway has its
#      scraper_runs row reconciled at the next start.
#
#  The scrapers record success/failure in scraper_runs
#  themselves; the closures here only push an app context
#  (belt and braces — nothing on the scrape path reads
#  current_app, get_db opens the module-level _db_path that
#  init_db set) and add a last-resort log line.
############################################################


import logging
import os
import random
from datetime import datetime, timedelta, timezone

from apscheduler.events import EVENT_JOB_MISSED
from apscheduler.schedulers.background import BackgroundScheduler

from app.database import get_db, sweep_expired_sessions, utc_now_iso

logger = logging.getLogger(__name__)

# Set on the first start_scraper_scheduler call and cleared
# by stop_scraper_scheduler — this module-level singleton IS
# the re-entry guard
_scheduler = None

# The startup one-shot Timers, kept so stop_scraper_scheduler
# can cancel the ones that have not fired yet
_startup_timers = []

# A tick delayed by more than one second used to be dropped
# outright (APScheduler's default); five minutes of grace is
# well inside every interval here
MISFIRE_GRACE_SECONDS = 300

# Startup one-shot delays, and the interval of the job each
# one stands in for — a source that already completed a run
# inside its interval is skipped, so a crash loop cannot
# hammer knf.vu.lt every 2 s
STARTUP_JITTER_SECONDS = 20

# The daily reconciliation only touches runs older than this
# — longer than any scraper's wall-clock budget, so a scrape
# going on RIGHT NOW in this process is never mistaken for
# one a dead process abandoned
STALE_RUN_SECONDS = 6 * 3600








############################################################
# start_scraper_scheduler
############################################################
#
# Idempotent per process: a second call returns at once.
# Checks the SCRAPER_ENABLED gate, reconciles the runs a
# previous process left at 'running', builds the scheduler,
# defines the job closures over `app` (each wraps itself in
# app.app_context() and imports its scraper lazily, so a
# broken scraper module fails that run in the log instead of
# the app boot), registers them as interval jobs with a real
# misfire grace, starts the daemon thread, and arms the
# one-shot startup Timers unless the source already ran
# recently. Answers True when a scheduler is running after
# the call.
#
# Used by:
#   - main.py — the --http branch, once per process (NOT
#     create_app: building the app must never start scraping)
############################################################

def start_scraper_scheduler(app):
    # STEP 1: the env gate, then the per-process re-entry
    # guard, then the scheduler itself (daemon=True: its
    # thread never blocks exit)
    # ===================================================
    global _scheduler

    if not _env_flag("SCRAPER_ENABLED", True):
        logger.info("SCRAPER_ENABLED is off — no scrape scheduler in this process")
        return False

    if _scheduler is not None:
        return True

    # Whatever a killed process left mid-scrape is closed now,
    # before this process opens runs of its own — bookkeeping,
    # so a failure here must never cost us the scheduler
    try:
        _reconcile_interrupted_runs()
    except Exception:
        logger.exception("Could not reconcile interrupted scraper runs at start")

    _scheduler = BackgroundScheduler(daemon=True)


    # STEP 2: the job bodies — closures over `app` so every
    # run gets an app context (belt and braces — nothing on
    # the scrape path reads current_app); the scrapers are
    # imported per run. The except clauses only see what
    # happens OUTSIDE the scrapers (import errors, context
    # problems) — the scrapers catch their own failures and
    # mark the run 'failed' in scraper_runs
    # =====================================================
    # The two news sources are independent, and so are their
    # guards: knf.vu.lt failing outside its own handler (a
    # broken import, a bug past it) used to end the tick and
    # leave vu.lt unscraped for another 20 minutes
    def run_scrapers():
        with app.app_context():
            logger.info("Running scheduled scrape...")
            knf_result = None
            vu_result = None

            try:
                from app.scraper.knf_scraper import scrape_knf_news

                knf_result = scrape_knf_news(pages=2)
            except Exception:
                logger.exception("Scheduled scrape failed for knf.vu.lt")

            try:
                from app.scraper.vu_scraper import scrape_vu_news

                vu_result = scrape_vu_news(pages=1)
            except Exception:
                logger.exception("Scheduled scrape failed for vu.lt")

            logger.info("Scrape done: knf=%s, vu=%s", knf_result, vu_result)

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

    def run_push_receipts():
        with app.app_context():
            try:
                # The queue and the Expo call belong to the
                # notifications package; this job is only its clock
                from app.notifications.push import poll_push_receipts

                checked = poll_push_receipts()
                if checked:
                    logger.info("Checked %d Expo push receipt(s)", checked)
            except Exception:
                logger.exception("Push receipt poll failed")

    def run_session_sweep():
        with app.app_context():
            try:
                db = get_db()
                try:
                    sweep_expired_sessions(db)
                    db.commit()
                finally:
                    db.close()
            except Exception:
                logger.exception("Expired-session sweep failed")

    def run_push_token_prune():
        with app.app_context():
            try:
                _prune_orphaned_push_tokens()
            except Exception:
                logger.exception("Push-token prune failed")

    def run_maintenance():
        with app.app_context():
            try:
                # Only rows older than every run budget — a scrape
                # in flight in THIS process is not an orphan
                _reconcile_interrupted_runs(STALE_RUN_SECONDS)
            except Exception:
                logger.exception("Scraper-run reconciliation failed")


    # STEP 3: register the interval jobs and start the thread;
    # each job's first tick is one full interval away, and
    # every one of them gets five minutes of misfire grace
    # instead of APScheduler's silent one second
    # ========================================================
    _scheduler.add_job(run_scrapers, "interval", minutes=20, id="news_scraper",
                       max_instances=1, misfire_grace_time=MISFIRE_GRACE_SECONDS)
    # timetables change rarely — 6 h keeps tvarkarasciai.vu.lt load low
    _scheduler.add_job(run_schedule_scraper, "interval", hours=6, id="schedule_scraper",
                       max_instances=1, misfire_grace_time=MISFIRE_GRACE_SECONDS)
    # faculty info changes even more rarely
    _scheduler.add_job(run_info_scraper, "interval", hours=24, id="info_scraper",
                       max_instances=1, misfire_grace_time=MISFIRE_GRACE_SECONDS)
    # Expo keeps a receipt for 24 h and wants ~15 min of patience
    _scheduler.add_job(run_push_receipts, "interval", minutes=15, id="push_receipts",
                       max_instances=1, misfire_grace_time=MISFIRE_GRACE_SECONDS)
    # housekeeping: rows nothing else ever deletes
    _scheduler.add_job(run_session_sweep, "interval", hours=24, id="session_sweep",
                       max_instances=1, misfire_grace_time=MISFIRE_GRACE_SECONDS)
    _scheduler.add_job(run_push_token_prune, "interval", hours=24, id="push_token_prune",
                       max_instances=1, misfire_grace_time=MISFIRE_GRACE_SECONDS)
    _scheduler.add_job(run_maintenance, "interval", hours=24, id="run_reconcile",
                       max_instances=1, misfire_grace_time=MISFIRE_GRACE_SECONDS)

    # A dropped tick is a real event, not a silent one
    _scheduler.add_listener(_log_missed_job, EVENT_JOB_MISSED)
    _scheduler.start()


    # STEP 4: cover the startup gap with one-shot Timers,
    # staggered so a boot does not hit every site at once and
    # jittered so a crash loop does not synchronise on the
    # second. daemon=True: a process that never serves must be
    # able to exit instead of waiting out the 60 s info scrape.
    # A source that completed a run inside its own interval is
    # skipped entirely — restart storms used to re-scrape
    # everything every time
    # ========================================================
    if not _env_flag("SCRAPER_STARTUP_RUNS", True):
        logger.info("SCRAPER_STARTUP_RUNS is off — the interval jobs are the only scrapes")
    else:
        # mid-function import: threading is used nowhere else here
        import threading
        startup_jobs = (
            (2.0, run_scrapers, "knf.vu.lt", 20 * 60),
            (30.0, run_schedule_scraper, "tvarkarasciai.vu.lt", 6 * 3600),
            (60.0, run_info_scraper, "knf.vu.lt/info", 24 * 3600),
        )
        for delay, job, source, interval in startup_jobs:
            if _ran_within(source, interval):
                logger.info("Skipping the startup %s scrape — a run completed inside its %d s interval",
                            source, interval)
                continue

            timer = threading.Timer(delay + random.uniform(0, STARTUP_JITTER_SECONDS), job)
            timer.daemon = True
            timer.start()
            _startup_timers.append(timer)

    logger.info("Scraper scheduler started (news: 20min, schedule: 6h, info: 24h, "
                "receipts: 15min, session sweep + token prune + run reconcile: 24h)")

    return True








############################################################
# stop_scraper_scheduler
############################################################
#
# Shuts the scheduler down, cancels the startup one-shots
# that have not fired, and clears the global so a later
# start_scraper_scheduler builds a fresh one. wait=False on
# purpose: a scrape in flight is not worth blocking an exit
# for, and the run row it abandons is reconciled at the next
# start. Safe to call when nothing is running.
#
# Used by:
#   - main.py — the exit path of the --http branch
############################################################

def stop_scraper_scheduler():
    global _scheduler

    for timer in _startup_timers:
        timer.cancel()
    _startup_timers.clear()

    if _scheduler is None:
        return

    try:
        _scheduler.shutdown(wait=False)
    except Exception:
        logger.warning("Scraper scheduler shutdown did not take", exc_info=True)

    _scheduler = None
    logger.info("Scraper scheduler stopped")








############################################################
# _env_flag
############################################################
#
# One env var as a boolean: "0", "false", "no" and "off"
# (any case) are False, anything else present is True, and
# an unset variable takes the default. Both scraper gates
# default to ON so an existing deployment behaves exactly as
# it did.
#
# Used by:
#   - start_scraper_scheduler (above) — SCRAPER_ENABLED and
#     SCRAPER_STARTUP_RUNS
############################################################

def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default

    return raw.strip().lower() not in ("0", "false", "no", "off")








############################################################
# _log_missed_job
############################################################
#
# The EVENT_JOB_MISSED listener. APScheduler drops a tick
# whose scheduled time has passed by more than the misfire
# grace and says nothing an operator would notice; this puts
# one WARNING per dropped tick in the container log, naming
# the job.
#
# Used by:
#   - start_scraper_scheduler (above) — registered on the
#     scheduler
############################################################

def _log_missed_job(event):
    logger.warning("Scheduled job %s missed its %s tick (over %d s late)",
                   event.job_id, event.scheduled_run_time, MISFIRE_GRACE_SECONDS)








############################################################
# _ran_within
############################################################
#
# True when the given source completed a scraper_runs row
# inside the last `seconds`. The startup one-shots exist to
# cover the gap before the first interval tick — when the
# gap is already covered, running again is pure load on
# knf.vu.lt, and a container that keeps crashing would run
# them every few seconds forever. Any error answers False:
# a broken lookup must not be a reason to stop scraping.
#
# Used by:
#   - start_scraper_scheduler (above) — once per startup
#     one-shot
############################################################

def _ran_within(source: str, seconds: int) -> bool:
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()

    try:
        db = get_db()
        try:
            row = db.execute(
                """SELECT 1 FROM scraper_runs
                   WHERE source = ? AND status = 'completed' AND started_at > ?
                   LIMIT 1""",
                (source, cutoff),
            ).fetchone()
        finally:
            db.close()
    except Exception:
        logger.warning("Could not check recent %s runs — running the startup scrape", source, exc_info=True)
        return False

    return row is not None








############################################################
# _reconcile_interrupted_runs
############################################################
#
# Closes every scraper_runs row still marked 'running' as
# 'failed' with error_message 'interrupted'. A row only
# reaches this state one way: the process died mid-scrape
# (SIGKILL, OOM, a container replaced), because every
# in-process failure path closes its own row. Nothing swept
# them before, so /api/scraper/status showed runs that had
# been "running" for months and a dead scraper looked busy.
#
# `older_than_seconds` is what keeps the daily pass off a
# scrape that is genuinely in flight: at scheduler start it
# is 0 (the previous process is known to be gone), and the
# daily job passes STALE_RUN_SECONDS.
#
# Used by:
#   - start_scraper_scheduler (above) — before the first job
#   - the run_maintenance job (above) — daily
############################################################

def _reconcile_interrupted_runs(older_than_seconds: int = 0):
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)).isoformat()

    db = get_db()
    try:
        closed = db.execute(
            """UPDATE scraper_runs
               SET status = 'failed', error_message = 'interrupted', finished_at = ?
               WHERE status = 'running' AND started_at < ?""",
            (utc_now_iso(), cutoff),
        ).rowcount
        db.commit()
        if closed:
            logger.warning("Closed %d scraper run(s) left 'running' by a killed process", closed)
    finally:
        db.close()








############################################################
# _prune_orphaned_push_tokens
############################################################
#
# Deletes push_tokens whose owner has no unexpired session
# left, so a phone that logged out (and never came back)
# stops receiving private chat previews. Deliberately NOT
# done at logout: a user's other logged-in devices must keep
# working, and a live device re-registers its token on the
# next cold start.
#
# Used by:
#   - start_scraper_scheduler (above) — the daily job
############################################################

def _prune_orphaned_push_tokens():
    db = get_db()
    try:
        removed = db.execute(
            """DELETE FROM push_tokens
               WHERE user_id NOT IN (
                   SELECT user_id FROM sessions WHERE expires_at > ?
               )""",
            (utc_now_iso(),),
        ).rowcount
        db.commit()
        if removed:
            logger.info("Pruned %d push token(s) with no live session", removed)
    finally:
        db.close()
