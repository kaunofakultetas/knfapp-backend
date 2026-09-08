############################################################
#  [*] scrape_info — the daily faculty-info tick
#
#  knf.vu.lt contacts, study programs and structure into
#  faculty_info. The scraper records its own outcome and
#  never raises; the guard here only catches what escapes.
#
#  Run by the cron container:
#    docker exec knfapp-django python3 manage.py scrape_info
############################################################


import logging

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Scrape knf.vu.lt contacts/programs/structure into faculty_info"

    def handle(self, *args, **options):
        try:
            from knfapp.scraper.info_scraper import scrape_faculty_info
            result = scrape_faculty_info()
            self.stdout.write(f"Faculty info scrape done: {result}")
        except Exception:
            logger.exception("Scheduled faculty info scrape failed")
