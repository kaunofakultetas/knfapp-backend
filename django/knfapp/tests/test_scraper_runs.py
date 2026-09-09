############################################################
#  [*] Regression tests — scraper run plumbing and routes
#
#  The bookkeeping a rewrite must not lose: the two-layer
#  run lock's cross-process half (a live 'running' row
#  refuses the overlap, a corpse past its budget does not),
#  the reconcile that closes what a killed process left, the
#  retention pass that keeps every source's newest row, the
#  push-shape guards, the timetable reconciliation counting
#  change before writing, and the admin routes' status
#  mapping with the stable error slug — the raw exception
#  text stays out of HTTP bodies.
############################################################


import json
import uuid
from datetime import datetime, timedelta, timezone


from django.test import Client, TestCase


from knfapp.common import ratelimit
from knfapp.common.timestamps import utc_now_iso
from knfapp.scraper import common
from knfapp.scraper.api import views
from knfapp.scraper.models import ScraperRun
from knfapp.scraper.schedule_scraper import _insert_lessons, _purge_old_semesters, _reconcile_partition
from knfapp.schedule.models import ScheduleLesson
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


class TimetableReconcileTests(TestCase):

    def _lesson(self, **overrides):
        lesson = {"title": "Programavimas", "teacher": "A. Petraitis", "room": "302",
                  "time_start": "10:00", "time_end": "11:30", "day_of_week": 0,
                  "group_name": "ISKS-1", "semester": "2026-R"}
        lesson.update(overrides)
        return lesson

    def test_the_natural_index_dedups_reinserts(self):
        self.assertEqual(_insert_lessons([self._lesson()]), 1)
        self.assertEqual(_insert_lessons([self._lesson()]), 0)
        # "" and None are DIFFERENT rows to a unique index — the
        # scraper stores "" so a teacherless lesson dedups too
        self.assertEqual(_insert_lessons([self._lesson(teacher="")]), 1)
        self.assertEqual(_insert_lessons([self._lesson(teacher="")]), 0)

    def test_change_is_counted_before_the_rewrite(self):
        _insert_lessons([self._lesson()])
        # Unchanged partition: (0, 0) and no rewrite → no push
        self.assertEqual(_reconcile_partition("ISKS-1", "2026-R", [self._lesson()]), (0, 0))
        # A room change is one gained and one lost — and the phantom
        # old row is GONE, not haunting every following week
        self.assertEqual(_reconcile_partition("ISKS-1", "2026-R", [self._lesson(room="404")]), (1, 1))
        rooms = list(ScheduleLesson.objects.values_list("room", flat=True))
        self.assertEqual(rooms, ["404"])

    def test_the_purge_retires_only_labels_this_scraper_wrote(self):
        _insert_lessons([self._lesson(semester="2025-R"),
                         self._lesson(semester="2026-R"),
                         self._lesson(semester="2025-pavasaris")])
        _purge_old_semesters("2026-R")
        kept = sorted(ScheduleLesson.objects.values_list("semester", flat=True))
        self.assertEqual(kept, ["2025-pavasaris", "2026-R"])


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
