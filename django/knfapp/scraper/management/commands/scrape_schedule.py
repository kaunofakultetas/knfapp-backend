############################################################
#  [*] scrape_schedule — the 6-hour timetable tick
#
#  The full tvarkarasciai.vu.lt import with its default
#  rolling window. Push is ON — this is the scheduled
#  run. The scraper records its own outcome in scraper_runs
#  and never raises; the guard here only catches what
#  escapes it.
#
#  Run by the cron container:
#    docker exec knfapp-django python3 manage.py scrape_schedule
############################################################


import logging

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Scrape tvarkarasciai.vu.lt timetables into the dated schedule_events tables"

    def handle(self, *args, **options):
        try:
            from knfapp.scraper.schedule_scraper import scrape_knf_schedule
            result = scrape_knf_schedule()
            self.stdout.write(f"Schedule scrape done: {result}")
        except Exception:
            logger.exception("Scheduled schedule scrape failed")
