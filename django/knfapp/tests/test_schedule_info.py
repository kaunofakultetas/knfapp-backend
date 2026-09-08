############################################################
#  [*] Regression tests — the timetable and the handbook
#
#  Schedule: the newest-semester default with its 'all'
#  opt-out (part of the mobile wire contract), the stray-
#  label threshold, parameter clamping, and the public ETag
#  cycle. Info: lang normalisation, the overlay's freshness
#  and size floors (a degraded scrape must never hide the
#  curated handbook), and the borrowed-'lt' overlay for
#  English.
############################################################


import json
import uuid
from datetime import datetime, timedelta, timezone


from django.test import Client, TestCase


from knfapp.common.timestamps import utc_now_iso
from knfapp.info.models import FacultyInfo
from knfapp.schedule.models import ScheduleLesson
from .utils import create_user  # noqa: F401 — parity import for suite conventions


def _lesson(semester, group="IS-1", day=0, time="09:00", title="Programavimas"):
    return ScheduleLesson.objects.create(
        id=str(uuid.uuid4()), title=title, teacher="J. Jonaitis", room="301",
        time_start=time, time_end="10:30", day_of_week=day, group_name=group,
        semester=semester, created_at=utc_now_iso(),
    )


def _seed_semester(label, lessons=5, group="IS-1"):
    for i in range(lessons):
        _lesson(label, group=group, day=i % 5, time=f"{9 + i:02d}:00")


class ScheduleTests(TestCase):

    def setUp(self):
        self.client = Client()

    def test_no_semester_means_the_newest_real_one(self):
        _seed_semester("2025-P")
        _seed_semester("2025-R")
        _lesson("2026-P")   # a stray single row — below the threshold

        served = {r["semester"] for r in self.client.get("/api/schedule").json()["lessons"]}
        self.assertEqual(served, {"2025-R"})

    def test_all_is_the_explicit_opt_out(self):
        _seed_semester("2025-P")
        _seed_semester("2025-R")
        served = {r["semester"] for r in self.client.get("/api/schedule?semester=all").json()["lessons"]}
        self.assertEqual(served, {"2025-P", "2025-R"})

    def test_day_validation_and_count_clamping(self):
        self.assertEqual(self.client.get("/api/schedule?day=7").status_code, 400)
        self.assertEqual(self.client.get("/api/schedule?day=x").status_code, 400)
        self.assertEqual(self.client.get("/api/schedule?limit=3_0").status_code, 400)
        # Out-of-range numbers are CLAMPED, not refused
        _seed_semester("2025-R")
        response = self.client.get("/api/schedule?limit=999999")
        self.assertEqual(response.status_code, 200)

    def test_the_filters_sheet_and_the_threshold(self):
        _seed_semester("2025-R", group="IS-1")
        _seed_semester("2025-P", group="VV-2")
        _lesson("2026-P", group="XX-9")   # stray — no picker entry

        body = self.client.get("/api/schedule/filters").json()
        self.assertEqual(body["semesters"], ["2025-R", "2025-P"])
        self.assertEqual(body["semesterGroups"],
                         [{"semester": "2025-R", "groups": ["IS-1"]},
                          {"semester": "2025-P", "groups": ["VV-2"]}])
        self.assertNotIn("2026-P", body["semesters"])

    def test_the_public_etag_cycle(self):
        _seed_semester("2025-R")
        first = self.client.get("/api/schedule")
        self.assertIn("public", first["Cache-Control"])
        cached = self.client.get("/api/schedule", HTTP_IF_NONE_MATCH=first["ETag"])
        self.assertEqual(cached.status_code, 304)
        _lesson("2025-R", title="Nauja paskaita")
        changed = self.client.get("/api/schedule", HTTP_IF_NONE_MATCH=first["ETag"])
        self.assertEqual(changed.status_code, 200)


def _overlay_row(section, data, lang="lt", scraped_at=None):
    return FacultyInfo.objects.create(
        id=str(uuid.uuid4()), lang=lang, section=section,
        data_json=json.dumps(data),
        scraped_at=scraped_at or utc_now_iso(),
    )


def _contacts(items):
    return [{"category": "Dekanatas",
             "items": [{"name": f"Kabinetas {i}", "room": str(100 + i)} for i in range(items)]}]


class InfoTests(TestCase):

    def setUp(self):
        self.client = Client()
        # warn_once state is process-wide — isolate per test
        from knfapp.info.api import views
        views._warned.clear()

    def test_lang_is_normalised_and_the_answer_names_it(self):
        for raw, served in (("EN-gb", "en"), ("en_GB", "en"), ("fr", "lt"), (None, "lt")):
            url = "/api/info" if raw is None else f"/api/info?lang={raw}"
            self.assertEqual(self.client.get(url).json()["lang"], served, raw)

    def test_a_surviving_scrape_replaces_contacts_and_dates_the_answer(self):
        _overlay_row("contacts", _contacts(6))
        body = self.client.get("/api/info").json()
        self.assertEqual(len(body["contacts"][0]["items"]), 6)
        self.assertIn("updatedAt", body)

    def test_the_floors_keep_the_curated_handbook(self):
        # Too few contacts, too few programs, wrong shapes — the
        # curated lists must stand
        _overlay_row("contacts", _contacts(2))
        _overlay_row("programs", [{"name": "Tik viena"}])
        _overlay_row("general_contact", [])
        body = self.client.get("/api/info").json()
        self.assertEqual(body["contacts"][0]["category"], "Dekanatas")
        self.assertEqual(len(body["programs"]), 5)          # the curated five
        self.assertNotIn("general_contact", body)

    def test_a_stale_scrape_is_ignored(self):
        old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        _overlay_row("contacts", _contacts(6), scraped_at=old)
        body = self.client.get("/api/info").json()
        self.assertEqual(body["contacts"][0]["category"], "Dekanatas")
        self.assertNotIn("updatedAt", body)

    def test_english_borrows_the_lt_overlay_but_keeps_its_own_faq(self):
        _overlay_row("contacts", _contacts(6), lang="lt")
        body = self.client.get("/api/info?lang=en").json()
        self.assertEqual(len(body["contacts"][0]["items"]), 6)   # borrowed
        self.assertIn("How do I", body["faq"][0]["q"])           # curated English

    def test_an_unknown_section_is_a_400_with_the_slug(self):
        response = self.client.get("/api/info?section=personalas")
        self.assertEqual((response.status_code, response.json()["code"]), (400, "unknown_section"))
        one = self.client.get("/api/info?section=faq").json()
        self.assertEqual(set(one), {"faq", "lang"})
