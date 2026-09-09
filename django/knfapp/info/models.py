############################################################
#  [*] info — the scraped handbook overlay rows
#
#  Shape policy as in users/models.py.
#
#  Models:
#    - FacultyInfo — one scraped blob per (lang, section)
#
#  Changes to these models require running:
#    python3 manage.py makemigrations
#    python3 manage.py migrate
############################################################


from django.db import models








# -----------------------------------------------------------
# FacultyInfo
# -----------------------------------------------------------
#
# One row per (lang, section) holding the info scraper's
# JSON blob (contacts / programs / general_contact) and its
# stamp; the API lays whatever survives the freshness and
# shape floors over the curated handbook. The info scraper
# (scraper/info_scraper.py) is the only writer, and it
# writes lang 'lt' only.
#
# Table: faculty_info
# -----------------------------------------------------------

class FacultyInfo(models.Model):
    # Columns
    id = models.TextField(primary_key=True)
    lang = models.TextField(default="lt")
    section = models.TextField()
    data_json = models.JSONField()
    scraped_at = models.DateTimeField()

    # Table metadata
    class Meta:
        db_table = "faculty_info"
        constraints = [
            models.UniqueConstraint(fields=["lang", "section"], name="faculty_info_lang_section"),
        ]
