############################################################
#  [*] schedule — the scraped lecture timetable
#
#  Shape policy as in users/models.py.
#
#  Models:
#    - ScheduleLesson — one timetable slot per natural key
#
#  Changes to these models require running:
#    python3 manage.py makemigrations
#    python3 manage.py migrate
############################################################


from django.db import models








# -----------------------------------------------------------
# ScheduleLesson
# -----------------------------------------------------------
#
# Read-only from the API's side: the timetable scraper
# (scraper/schedule_scraper.py) is the only writer. Times
# are "HH:MM" wall-clock text, day_of_week 0=Monday..6=
# Sunday (the CHECK), semester labels the scraper's
# "YYYY-P" (spring) / "YYYY-R" (autumn).
#
# Table: schedule_lessons
# -----------------------------------------------------------

class ScheduleLesson(models.Model):
    # Columns — the four natural-key text columns are NOT
    # NULL on purpose: a NULL inside the unique key would be
    # distinct from every twin (SQLite), so dedup and the
    # partition purge only work over '' — NOT NULL makes ''
    # the one shape a missing value can take
    id = models.TextField(primary_key=True)
    title = models.TextField()
    teacher = models.TextField(default="", blank=True)
    room = models.TextField(default="", blank=True)
    time_start = models.TextField()
    time_end = models.TextField()
    day_of_week = models.IntegerField()
    group_name = models.TextField(default="", blank=True)
    semester = models.TextField(default="", blank=True)
    created_at = models.DateTimeField()

    # Table metadata
    class Meta:
        db_table = "schedule_lessons"
        constraints = [
            models.CheckConstraint(condition=models.Q(day_of_week__gte=0, day_of_week__lte=6),
                                   name="schedule_lessons_day_check"),
            # The natural key — the whole row IS the identity,
            # and the scraper's conflict-ignoring insert dedups
            # on it. The NOT NULL defaults above keep it total:
            # no row can ever hold the NULL that would dodge it
            models.UniqueConstraint(
                fields=["semester", "group_name", "day_of_week", "time_start", "time_end",
                        "title", "teacher", "room"],
                name="idx_schedule_lessons_natural",
            ),
        ]
        indexes = [
            models.Index(fields=["semester", "group_name", "day_of_week"],
                         name="idx_schedule_lessons_filter"),
        ]
