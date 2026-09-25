############################################################
#  [*] schedule — the scraped lecture timetable, dated
#
#  Shape policy as in users/models.py.
#
#  Dated event instances, not weekly patterns: a lecture on
#  2026-03-05 is a row about that date, so irregular and
#  one-off lectures need no folding tricks, and history
#  survives (retention is the scraper's job). The EVENT is
#  the unit — the same physical lecture reached through two
#  group feeds (or, in the tracer system, through a room
#  feed) lands on ONE row, with link tables carrying which
#  groups and teachers participate. The tracer system can
#  hang its attendance verdicts, Salto generation and AD
#  autolinking off these same tables: ScheduleTeacher
#  carries external_id / ad_account columns for exactly
#  that, unused by this app.
#
#  Models:
#    - ScheduleEvent        — one dated lecture instance
#    - ScheduleGroup        — one VU student group (slug)
#    - ScheduleTeacher      — one teacher display name
#    - ScheduleEventGroup   — event ↔ group link
#    - ScheduleEventTeacher — event ↔ teacher link
#
#  Changes to these models require running:
#    python3 manage.py makemigrations
#    python3 manage.py migrate
############################################################


from django.db import models








############################################################
# ScheduleEvent
############################################################
#
# One dated lecture instance. Identity is the tracer-proven
# (date, times, title, type, room) — teacher and group are
# deliberately NOT part of it: a teacher swap updates the
# row, and every group feed that serves the lecture
# confirms the same row through the link table. The times
# are "HH:MM" wall-clock text and `date` a plain DATE, so
# the wire and the display never touch timezones (VU
# publishes Vilnius wall clock; DST math has no business
# here). `teacher` keeps the feed's display string verbatim
# for the read side; the link table is the joinable form.
# last_seen_at is the confirmation stamp: a run confirms
# what it saw, and the cleanup deletes FUTURE events whose
# stamp went stale (the site stopped serving them) while
# past events stay as history until retention.
#
# lecture_type holds the event's type as the site names it
# and, after a "|", the subgroups it names — "Pratybos|1",
# "Egzaminas", "" for rows stored before the scraper read
# types. It rides the natural key, yet a slot keeps ONE row:
# the scraper merges feeds on (date, times, title, room) and
# relabels in place (schedule_scraper.join_lecture_type /
# split_lecture_type own the format; the events route splits
# it into lectureType + subgroups).
#
# Table: schedule_events
############################################################

class ScheduleEvent(models.Model):
    # Columns — every identity text column is NOT NULL on
    # purpose: a NULL inside the unique key would be distinct
    # from every twin (SQLite), so dedup only works over ''
    id = models.TextField(primary_key=True)
    title = models.TextField()
    lecture_type = models.TextField(default="", blank=True)
    teacher = models.TextField(default="", blank=True)
    room = models.TextField(default="", blank=True)
    date = models.DateField()
    time_start = models.TextField()
    time_end = models.TextField()
    # "YYYY-R"/"YYYY-P", derived from `date` — stored so the
    # picker options and the stray-label threshold stay one
    # cheap GROUP BY
    semester = models.TextField(default="", blank=True)
    last_seen_at = models.DateTimeField()
    created_at = models.DateTimeField()

    # Table metadata
    class Meta:
        db_table = "schedule_events"
        constraints = [
            # The natural key — what the scraper's conflict-
            # ignoring insert dedups on, across group feeds
            models.UniqueConstraint(
                fields=["date", "time_start", "time_end", "title", "lecture_type", "room"],
                name="idx_schedule_events_natural",
            ),
        ]
        indexes = [
            models.Index(fields=["date"], name="idx_schedule_events_date"),
            models.Index(fields=["semester"], name="idx_schedule_events_sem"),
        ]








############################################################
# ScheduleGroup
############################################################
#
# One VU student group, keyed by the site's slug. The
# folded group_name ("ISKS-1" — programme + course, the
# "1 grupė / 2 grupė" split dropped) is what the mobile
# picker shows and filters by, so several slugs may share
# one group_name. last_seen_at marks the last run whose
# group list still carried the slug.
#
# Table: schedule_groups
############################################################

class ScheduleGroup(models.Model):
    slug = models.TextField(primary_key=True)
    display_name = models.TextField(default="", blank=True)
    group_name = models.TextField()
    last_seen_at = models.DateTimeField()

    # Table metadata
    class Meta:
        db_table = "schedule_groups"
        indexes = [
            models.Index(fields=["group_name"], name="idx_schedule_groups_name"),
        ]








############################################################
# ScheduleTeacher
############################################################
#
# One teacher, identified by the display string the feed
# serves (academic titles included) — the feed's one
# teacher field is stored whole, never split on commas,
# because "doc. dr." would shred. external_id (the VU
# employee id) and ad_account are the tracer system's
# columns: its per-room feed and AD autolinker fill them;
# this app writes neither.
#
# Table: schedule_teachers
############################################################

class ScheduleTeacher(models.Model):
    id = models.TextField(primary_key=True)
    name = models.TextField(unique=True)
    external_id = models.TextField(default="", blank=True)
    ad_account = models.TextField(default="", blank=True)
    last_seen_at = models.DateTimeField()

    # Table metadata
    class Meta:
        db_table = "schedule_teachers"








############################################################
# ScheduleEventGroup
############################################################
#
# Which groups a dated event belongs to — the join the
# schedule reads filter on. Its own last_seen_at lets a run
# retire ONE group's claim on a shared event (the lecture
# moved out of that group's feed) without touching the
# event or the other groups' links.
#
# Table: schedule_event_groups
############################################################

class ScheduleEventGroup(models.Model):
    event = models.ForeignKey(ScheduleEvent, on_delete=models.CASCADE, db_column="event_id")
    group = models.ForeignKey(ScheduleGroup, on_delete=models.CASCADE, db_column="group_slug")
    last_seen_at = models.DateTimeField()

    # Table metadata
    class Meta:
        db_table = "schedule_event_groups"
        constraints = [
            models.UniqueConstraint(fields=["event", "group"],
                                    name="idx_schedule_event_groups_pair"),
        ]








############################################################
# ScheduleEventTeacher
############################################################
#
# Which teachers hold a dated event — regenerated on every
# confirmation from the event's teacher string, so it can
# never drift from the read side. The tracer system's
# per-teacher timetable and Salto generation join through
# here.
#
# Table: schedule_event_teachers
############################################################

class ScheduleEventTeacher(models.Model):
    event = models.ForeignKey(ScheduleEvent, on_delete=models.CASCADE, db_column="event_id")
    teacher = models.ForeignKey(ScheduleTeacher, on_delete=models.CASCADE, db_column="teacher_id")
    last_seen_at = models.DateTimeField()

    # Table metadata
    class Meta:
        db_table = "schedule_event_teachers"
        constraints = [
            models.UniqueConstraint(fields=["event", "teacher"],
                                    name="idx_schedule_event_teachers_pair"),
        ]
