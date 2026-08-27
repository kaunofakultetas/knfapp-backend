############################################################
#  [*] Schedule — package marker for timetables
#
#  routes.py is the whole module: the lesson list (filtered
#  by group, semester and date range), the distinct group /
#  semester values the filter UI offers, and an admin-only
#  seed route that plants 31 demo lessons.
#
#  schedule_bp is NOT re-exported here — create_app()
#  imports it from app.schedule.routes directly.
#
#  Used by:
#    - app/__init__.py — registers schedule_bp at
#      /api/schedule
#    - scraper/schedule_scraper.py — fills schedule_lessons
#    - mobile services/api/schedule.ts — the schedule tab
############################################################
