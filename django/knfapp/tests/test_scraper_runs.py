############################################################
#  [*] Regression tests — scraper run plumbing and routes
#
#  The bookkeeping a rewrite must not lose: the two-layer
#  run lock's cross-process half (a live 'running' row
#  refuses the overlap, a corpse past its budget does not),
#  the reconcile that closes what a killed process left, the
#  retention pass that keeps every source's newest row, the
#  push-shape guards, the dated timetable sync (confirm
#  instead of reinsert, converge across group feeds, retire
#  only what a healthy feed dropped, keep the past until
#  retention), and the admin routes' status
#  mapping with the stable error slug — the raw exception
#  text stays out of HTTP bodies.
############################################################


import json
import uuid
from datetime import date, datetime, timedelta, timezone


from django.test import Client, TestCase


from knfapp.common import ratelimit
from knfapp.common.timestamps import utc_now_iso
from knfapp.scraper import common
from knfapp.scraper.api import views
from knfapp.scraper.models import ScraperRun
from knfapp.scraper.schedule_scraper import RETENTION_DAYS, _sync_schedule
from knfapp.schedule.models import (
    ScheduleEvent,
    ScheduleEventGroup,
    ScheduleEventTeacher,
    ScheduleGroup,
    ScheduleTeacher,
)
from knfapp.users import auth
from .utils import bearer, create_user


def _run_row(source, status="completed", age_days=0, found=0, run_id=None):
    return ScraperRun.objects.create(
        id=run_id or str(uuid.uuid4()), source=source, status=status,
        articles_found=found,
        started_at=(datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat(),
    )


class RunLockTests(TestCase):

    def test_a_live_running_row_refuses_the_overlap(self):
        first = common.open_run("knf.vu.lt", 600)
        self.assertIsNotNone(first)
        self.assertIsNone(common.open_run("knf.vu.lt", 600))
        # A different source is free to run
        self.assertIsNotNone(common.open_run("vu.lt", 600))

    def test_a_corpse_past_its_budget_is_overlapped_not_obeyed(self):
        # A SIGKILLed run must block its source for minutes, never
        # until the daily reconcile
        _run_row("knf.vu.lt", status="running", age_days=1)
        self.assertIsNotNone(common.open_run("knf.vu.lt", 600))

    def test_the_reconcile_closes_only_what_is_stale(self):
        _run_row("knf.vu.lt", status="running", age_days=1)
        fresh = common.open_run("vu.lt", 600)
        self.assertEqual(common.reconcile_interrupted_runs(), 1)
        self.assertEqual(ScraperRun.objects.get(source="knf.vu.lt").error_message, "interrupted")
        self.assertEqual(ScraperRun.objects.get(id=fresh).status, "running")


class RetentionTests(TestCase):

    def test_every_sources_newest_row_survives_the_cutoff(self):
        # A scraper that stopped months ago is exactly the one whose
        # last run /status must keep showing
        _run_row("knf.vu.lt", age_days=90)
        _run_row("knf.vu.lt", age_days=60)
        _run_row("vu.lt", age_days=1)
        common.prune_scraper_runs()
        remaining = sorted(ScraperRun.objects.values_list("source", flat=True))
        self.assertEqual(remaining, ["knf.vu.lt", "vu.lt"])


class PushShapeTests(TestCase):

    def setUp(self):
        common._LAST_PUSH.clear()
        self.addCleanup(common._LAST_PUSH.clear)

    def test_the_first_completed_run_is_a_backfill(self):
        run = _run_row("knf.vu.lt", run_id="run-1")
        self.assertFalse(common.push_allowed("knf.vu.lt", 5, run.id))

    def test_a_burst_is_an_import_not_an_edition(self):
        _run_row("knf.vu.lt")
        run = _run_row("knf.vu.lt", run_id="run-2")
        self.assertFalse(common.push_allowed("knf.vu.lt", common.PUSH_BURST_THRESHOLD + 1, run.id))

    def test_one_push_per_source_per_hour(self):
        _run_row("knf.vu.lt")
        run = _run_row("knf.vu.lt", run_id="run-3")
        self.assertTrue(common.push_allowed("knf.vu.lt", 3, run.id))
        self.assertFalse(common.push_allowed("knf.vu.lt", 3, run.id))


class TimetableSyncTests(TestCase):

    GROUPS_OK = {"isks-1-kursas": {"display_name": "ISKS 1 kursas", "group_name": "ISKS-1"}}

    def _lesson(self, **overrides):
        lesson = {"title": "Programavimas", "teacher": "A. Petraitis", "room": "302",
                  "lecture_type": "", "date": date.today() + timedelta(days=7),
                  "time_start": "10:00", "time_end": "11:30",
                  "group_name": "ISKS-1", "slug": "isks-1-kursas", "semester": "2026-R"}
        lesson.update(overrides)
        return lesson

    def _sync(self, lessons, groups_ok=None, stamp=None):
        return _sync_schedule(lessons, self.GROUPS_OK if groups_ok is None else groups_ok,
                              stamp or datetime.now(timezone.utc))

    def test_the_natural_key_dedups_and_confirms_instead_of_reinserting(self):
        self.assertEqual(self._sync([self._lesson()]), (1, 0))
        # The same event again: confirmed, not re-added — an
        # unchanged timetable pushes nothing
        self.assertEqual(self._sync([self._lesson()]), (0, 0))
        # A teacher swap is the SAME event (teacher is not
        # identity): the row updates in place
        self.assertEqual(self._sync([self._lesson(teacher="B. Kazlauskas")]), (0, 0))
        self.assertEqual(list(ScheduleEvent.objects.values_list("teacher", flat=True)),
                         ["B. Kazlauskas"])
        self.assertEqual(ScheduleEventTeacher.objects.count(), 1)

    def test_two_group_feeds_converge_on_one_event(self):
        groups = {**self.GROUPS_OK,
                  "isks-1b": {"display_name": "ISKS 1 k. 2 grupė", "group_name": "ISKS-1"}}
        added, _ = self._sync([self._lesson(), self._lesson(slug="isks-1b")], groups_ok=groups)
        self.assertEqual(added, 1)
        self.assertEqual(ScheduleEvent.objects.count(), 1)
        self.assertEqual(ScheduleEventGroup.objects.count(), 2)

    def test_a_vanished_future_event_is_retired_but_a_failed_feed_preserves_its_schedule(self):
        room_change = self._lesson(room="404")
        self._sync([self._lesson(), room_change])
        # The next healthy run serves only the room-change row:
        # the stale twin loses its link and goes
        self.assertEqual(self._sync([room_change]), (0, 1))
        self.assertEqual(list(ScheduleEvent.objects.values_list("room", flat=True)), ["404"])
        # A run where this group's feed FAILED (not in groups_ok)
        # must not touch its stored schedule
        self.assertEqual(self._sync([], groups_ok={}), (0, 0))
        self.assertEqual(ScheduleEvent.objects.count(), 1)

    def test_the_past_is_history_until_retention(self):
        past = self._lesson(date=date.today() - timedelta(days=30))
        ancient = self._lesson(date=date.today() - timedelta(days=RETENTION_DAYS + 30),
                               time_start="08:00")
        self._sync([past, ancient])
        # A healthy run that no longer serves either (both are
        # outside the window) keeps the past — only retention
        # removes, and only past the horizon
        self.assertEqual(self._sync([self._lesson()]), (1, 0))
        kept = sorted(ScheduleEvent.objects.values_list("time_start", flat=True))
        self.assertEqual(kept, ["10:00", "10:00"])

    def test_a_lecture_leaving_one_feed_keeps_the_event_through_the_other(self):
        # The subtlest retire: a shared event loses ONE group's
        # claim (that healthy feed dropped it) but survives on
        # the other group's link — deleted only when the last
        # link goes
        groups = {**self.GROUPS_OK,
                  "ft-1-kursas": {"display_name": "FT 1 kursas", "group_name": "FT-1"}}
        self._sync([self._lesson(), self._lesson(slug="ft-1-kursas", group_name="FT-1")],
                   groups_ok=groups)
        self.assertEqual(ScheduleEventGroup.objects.count(), 2)

        # Next healthy run: only FT-1 still serves the lecture
        self.assertEqual(self._sync([self._lesson(slug="ft-1-kursas", group_name="FT-1")],
                                    groups_ok=groups), (0, 0))
        self.assertEqual(ScheduleEvent.objects.count(), 1)
        links = list(ScheduleEventGroup.objects.values_list("group_id", flat=True))
        self.assertEqual(links, ["ft-1-kursas"])

    def test_a_failed_feed_keeps_the_teacher_links_too(self):
        # Teacher-link retirement is scoped to CONFIRMED events:
        # a week whose feed failed must keep its teachers, or a
        # blip would strip every lecturer until the next run
        self._sync([self._lesson()])
        self.assertEqual(ScheduleEventTeacher.objects.count(), 1)
        self.assertEqual(self._sync([], groups_ok={}), (0, 0))
        self.assertEqual(ScheduleEventTeacher.objects.count(), 1)

    def test_orphaned_entities_prune_only_past_the_horizon(self):
        # A link-less teacher or group is kept until the
        # retention horizon — pruning it early would strip the
        # columns the tracer system fills (ad_account) the
        # moment a teacher has a lecture-free fortnight
        now = datetime.now(timezone.utc)
        stale = now - timedelta(days=RETENTION_DAYS + 5)
        ScheduleTeacher.objects.create(id="t-senas", name="Senas", last_seen_at=stale)
        ScheduleTeacher.objects.create(id="t-naujas", name="Naujas", last_seen_at=now)
        ScheduleGroup.objects.create(slug="senas", group_name="SEN-1", last_seen_at=stale)
        ScheduleGroup.objects.create(slug="naujas", group_name="NAU-1", last_seen_at=now)

        self._sync([self._lesson()])

        teachers = sorted(ScheduleTeacher.objects.values_list("name", flat=True))
        self.assertEqual(teachers, ["A. Petraitis", "Naujas"])
        groups = sorted(ScheduleGroup.objects.values_list("slug", flat=True))
        self.assertEqual(groups, ["isks-1-kursas", "naujas"])


class TimetableRunTests(TestCase):

    # MIN_SEMESTER_LESSONS per feed, so the stray-label guard
    # never trips whatever real date the suite runs on
    def _feed(self, room):
        from knfapp.scraper import schedule_scraper as ss
        when = date.today() + timedelta(days=7)
        label = ss._get_semester_label(datetime(when.year, when.month, when.day))
        return [
            {"title": "Programavimas", "teacher": "A. Petraitis", "room": room,
             "lecture_type": "", "date": when,
             "time_start": f"{9 + i:02d}:00", "time_end": f"{10 + i:02d}:30",
             "group_name": "ISKS-1", "slug": "isks-1-kursas", "semester": label}
            for i in range(5)
        ]

    def test_the_full_run_writes_and_a_failed_feed_run_preserves(self):
        # The whole pipeline through scrape_knf_schedule with the
        # network mocked out: run one populates, run two has one
        # feed serving moved rooms and the other RAISING — the
        # failed feed's schedule must survive untouched while the
        # healthy feed's stale rows retire
        from knfapp.scraper import schedule_scraper as ss

        stats = {"events": 5, "all_day": 0, "retakes": 0, "unparsable": 0,
                 "untitled": 0, "colours": {}}
        groups = [{"slug": "isks-1-kursas", "display_name": "ISKS 1 kursas"},
                  {"slug": "ft-1-kursas", "display_name": "FT 1 kursas"}]
        isks = self._feed("302")
        ft = [{**lesson, "slug": "ft-1-kursas", "group_name": "FT-1", "room": "404"}
              for lesson in self._feed("404")]

        real_list, real_schedule = ss.scrape_group_list, ss.scrape_group_schedule
        ss.scrape_group_list = lambda: groups
        ss.scrape_group_schedule = lambda slug, *_args: (ft if slug == "ft-1-kursas" else isks, stats)
        try:
            result = ss.scrape_knf_schedule(notify=False)
            self.assertEqual((result["groups_scraped"], result["lessons_found"], result["lessons_new"]),
                             (2, 10, 10))
            self.assertEqual(ScheduleEvent.objects.count(), 10)

            # Run two: ISKS moved every lecture one room over,
            # FT's feed dies mid-scrape
            moved = [{**lesson, "room": "303"} for lesson in isks]

            def second(slug, *_args):
                if slug == "ft-1-kursas":
                    raise RuntimeError("feed down")
                return moved, stats

            ss.scrape_group_schedule = second
            result = ss.scrape_knf_schedule(notify=False)
            self.assertEqual((result["groups_scraped"], result["lessons_new"]), (1, 5))

            rooms = sorted(set(ScheduleEvent.objects.values_list("room", flat=True)))
            # 303 replaced 302; the failed feed's 404 rows intact
            self.assertEqual(rooms, ["303", "404"])
            self.assertEqual(ScheduleEvent.objects.count(), 10)
        finally:
            ss.scrape_group_list, ss.scrape_group_schedule = real_list, real_schedule


class ScraperRouteTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.admin = create_user(username="vadovas", role="admin")
        self.token = auth.mint_session(self.admin.id)
        self.client = Client()

    def _patch_scrapers(self, knf, vu):
        real_knf, real_vu = views.scrape_knf_news, views.scrape_vu_news
        views.scrape_knf_news = lambda **kw: knf
        views.scrape_vu_news = lambda **kw: vu
        self.addCleanup(lambda: setattr(views, "scrape_knf_news", real_knf))
        self.addCleanup(lambda: setattr(views, "scrape_vu_news", real_vu))

    def test_failure_wins_over_already_running_and_the_slug_hides_the_text(self):
        self._patch_scrapers({"found": 0, "new": 0, "error": "Traceback: boom at line 7"},
                             {"found": 1, "new": 0, "skipped": True})
        response = bearer(self.client.post, "/api/scraper/trigger", self.token)
        self.assertEqual(response.status_code, 502)
        body = json.loads(response.content)
        # The raw exception text never reaches an HTTP body
        self.assertEqual(body["knf"]["error"], "scrape_failed")
        self.assertNotIn("Traceback", response.content.decode())

    def test_a_lock_stepaside_is_a_409_and_a_clean_pair_is_200(self):
        self._patch_scrapers({"found": 2, "new": 1}, {"found": 1, "new": 0, "skipped": True})
        self.assertEqual(bearer(self.client.post, "/api/scraper/run", self.token).status_code, 409)
        self._patch_scrapers({"found": 2, "new": 1}, {"found": 1, "new": 0})
        self.assertEqual(bearer(self.client.post, "/api/scraper/trigger", self.token).status_code, 200)

    def test_the_status_summary_survives_twenty_mixed_rows(self):
        # 20 fresher rows of other sources push the info failure out
        # of the runs array — the per-source summary still shows it
        _run_row("knf.vu.lt/info", status="failed", age_days=25)
        for i in range(20):
            _run_row("knf.vu.lt", age_days=0, found=i)

        response = bearer(self.client.get, "/api/scraper/status", self.token)
        body = json.loads(response.content)
        self.assertEqual(len(body["runs"]), 20)
        self.assertNotIn("knf.vu.lt/info", [r["source"] for r in body["runs"]])

        info = next(s for s in body["sources"] if s["source"] == "knf.vu.lt/info")
        self.assertEqual(info["lastFailure"]["status"], "failed")
        self.assertIsNone(info["lastSuccess"])
        # Both wire spellings carry the same numbers
        run = body["runs"][0]
        self.assertEqual(run["itemsFound"], run["articlesFound"])

    def test_an_unknown_status_filter_is_ignored_not_refused(self):
        _run_row("vu.lt")
        response = bearer(self.client.get, "/api/scraper/status?status=bogus", self.token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(json.loads(response.content)["runs"]), 1)

    def test_the_surface_is_admin_only(self):
        student = create_user(username="studentas")
        student_token = auth.mint_session(student.id)
        for path, method in (("/api/scraper/status", self.client.get),
                             ("/api/scraper/trigger", self.client.post)):
            self.assertEqual(bearer(method, path, student_token).status_code, 403)
