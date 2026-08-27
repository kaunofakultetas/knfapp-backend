############################################################
#  [*] Info — package marker for the faculty handbook
#
#  routes.py is the whole module: one public GET that serves
#  the contacts, study programs and staff the info scraper
#  stored in faculty_info, falling back to the hard-coded
#  FACULTY_INFO dict when the table is empty. Answers a
#  whole handbook for ?lang, or a single ?section.
#
#  info_bp is NOT re-exported here — create_app() imports it
#  from app.info.routes directly.
#
#  Used by:
#    - app/__init__.py — registers info_bp at /api/info
#    - scraper/info_scraper.py — fills the table this reads
#    - mobile services/api/info.ts — the info screen
############################################################
