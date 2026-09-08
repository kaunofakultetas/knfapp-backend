############################################################
#  [*] scraper — app registration
#
#  The four site scrapers (news x2, timetable, faculty
#  info), their shared plumbing, the admin control routes
#  and the management commands the cron container runs.
############################################################


from django.apps import AppConfig


class ScraperConfig(AppConfig):
    name = "knfapp.scraper"
    label = "scraper"
