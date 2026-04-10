"""Background scheduler for periodic news scraping."""

import logging
from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)

_scheduler = None


def start_scraper_scheduler(app):
    """Start background scraper that runs every 20 minutes."""
    global _scheduler

    if _scheduler is not None:
        return

    _scheduler = BackgroundScheduler(daemon=True)

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

    # Run on interval every 20 minutes
    _scheduler.add_job(run_scrapers, "interval", minutes=20, id="news_scraper", max_instances=1)
    # Schedule scraper: once every 6 hours (timetables don't change often)
    _scheduler.add_job(run_schedule_scraper, "interval", hours=6, id="schedule_scraper", max_instances=1)
    _scheduler.start()

    # Also run once immediately on startup
    import threading
    threading.Timer(2.0, run_scrapers).start()
    # Delay schedule scraper to avoid startup load spike
    threading.Timer(30.0, run_schedule_scraper).start()
    logger.info("Scraper scheduler started (news: 20min, schedule: 6h)")
