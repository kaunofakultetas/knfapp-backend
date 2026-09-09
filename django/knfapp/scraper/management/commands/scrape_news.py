############################################################
#  [*] scrape_news — the 20-minute news tick
#
#  knf.vu.lt (2 listing pages) then vu.lt (1), each under
#  its own guard so one source failing outside its own
#  handler cannot leave the other unscraped for a tick.
#  Push notifications are ON here — this command IS the
#  scheduled run; the admin trigger routes are the ones
#  that pass notify=False. The scrapers record their own
#  success/failure in scraper_runs; the guards here only
#  catch what escapes them (an import error, a broken
#  module) and keep it in the log.
#
#  Run by the cron container:
#    docker exec knfapp-django python3 manage.py scrape_news
############################################################


import logging

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Scrape knf.vu.lt and vu.lt news into news_posts"

    def handle(self, *args, **options):
        knf_result = None
        vu_result = None

        try:
            from knfapp.scraper.knf_scraper import scrape_knf_news
            knf_result = scrape_knf_news(pages=2)
        except Exception:
            logger.exception("Scheduled scrape failed for knf.vu.lt")

        try:
            from knfapp.scraper.vu_scraper import scrape_vu_news
            vu_result = scrape_vu_news(pages=1)
        except Exception:
            logger.exception("Scheduled scrape failed for vu.lt")

        self.stdout.write(f"Scrape done: knf={knf_result}, vu={vu_result}")
