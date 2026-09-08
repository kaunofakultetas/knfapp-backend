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
    # Columns
    id = models.TextField(primary_key=True)
    title = models.TextField()
    teacher = models.TextField(null=True, blank=True)
    room = models.TextField(null=True, blank=True)
    time_start = models.TextField()
    time_end = models.TextField()
    day_of_week = models.IntegerField()
    group_name = models.TextField(null=True, blank=True)
    semester = models.TextField(null=True, blank=True)
    created_at = models.TextField()

    # Table metadata
    class Meta:
        db_table = "schedule_lessons"
        constraints = [
            models.CheckConstraint(condition=models.Q(day_of_week__gte=0, day_of_week__lte=6),
                                   name="schedule_lessons_day_check"),
            # The natural key — the whole row IS the
            # identity, and the scraper's INSERT OR IGNORE dedups on it.
            # NULLs count as distinct here, which is why the scraper
            # stores "" and never None for teacher/room
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
