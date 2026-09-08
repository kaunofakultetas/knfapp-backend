############################################################
#  [*] scraper — the run ledger
#
#  One row per scrape (running → completed/failed), the
#  table GET /api/scraper/status reads and the conditional
#  INSERT in common.open_run leans on as the cross-process
#  run lock. The counts keep the news scraper's column
#  names whatever the source counts (lessons, sections).
#  Shape policy as in users/models.py.
############################################################


from django.db import models


RUN_STATUSES = ("running", "completed", "failed")


class ScraperRun(models.Model):
    id = models.TextField(primary_key=True)
    source = models.TextField()
    status = models.TextField(default="running")
    articles_found = models.IntegerField(default=0)
    articles_new = models.IntegerField(default=0)
    error_message = models.TextField(null=True, blank=True)
    started_at = models.TextField()
    finished_at = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "scraper_runs"
        constraints = [
            models.CheckConstraint(condition=models.Q(status__in=RUN_STATUSES),
                                   name="scraper_runs_status_check"),
        ]
        indexes = [
            models.Index(fields=["-started_at"], name="idx_scraper_runs_started"),
            # The live index is idx_scraper_runs_source_started — 31
            # chars, one over Django's name cap, so this one carries a
            # shortened name. Index names play no part in the row copy
            models.Index(fields=["source", "-started_at"], name="idx_scraper_runs_src_started"),
        ]
