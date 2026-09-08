############################################################
#  [*] info — the scraped handbook overlay rows
#
#  One row per (lang, section) holding the info scraper's
#  JSON blob (contacts / programs / general_contact) and its
#  stamp; the API lays whatever survives the freshness and
#  shape floors over the curated handbook. The info
#  scraper (scraper/info_scraper.py) is the only writer,
#  and it writes lang 'lt' only. Shape policy as in users/models.py.
############################################################


from django.db import models


class FacultyInfo(models.Model):
    id = models.TextField(primary_key=True)
    lang = models.TextField(default="lt")
    section = models.TextField()
    data_json = models.TextField()
    scraped_at = models.TextField()

    class Meta:
        db_table = "faculty_info"
        constraints = [
            models.UniqueConstraint(fields=["lang", "section"], name="faculty_info_lang_section"),
        ]
