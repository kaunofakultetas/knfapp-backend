############################################################
#  [*] Scraper — package marker for the ingest jobs
#
#  Eight modules live here:
#
#    common.py           — the shared plumbing: one pooled
#                          requests.Session with retries,
#                          the host allowlists, URL
#                          canonicalisation, timestamp
#                          sanitising and the scraper_runs
#                          bookkeeping
#    knf_scraper.py      — knf.vu.lt news (2+ pages)
#    vu_scraper.py       — vu.lt news (1+ pages)
#    schedule_scraper.py — tvarkarasciai.vu.lt timetables
#    info_scraper.py     — knf.vu.lt contacts, programs
#    plurals.py          — Lithuanian plural declension for
#                          the push copy
#    scheduler.py        — the APScheduler thread that
#                          create_app() starts: news every
#                          20 min, timetables every 6 h,
#                          faculty info daily, push receipts
#                          every 15 min, session sweep and
#                          push-token prune daily
#    routes.py           — the status log and the manual
#                          triggers (admin only)
#
#  scraper_bp is NOT re-exported here — create_app() imports
#  it from app.scraper.routes directly.
#
#  Used by:
#    - app/__init__.py — registers scraper_bp at
#      /api/scraper and starts the scheduler
#    - mobile: nothing calls these routes; the app reads the
#      results through /api/news, /api/schedule, /api/info
############################################################
