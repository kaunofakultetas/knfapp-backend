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


import re
import uuid
from datetime import date, datetime, timedelta, timezone


from django.test import Client, TestCase


from knfapp.common.timestamps import utc_now, utc_now_iso
from knfapp.info.models import FacultyInfo
from knfapp.schedule.models import ScheduleEvent, ScheduleEventGroup, ScheduleGroup
from .utils import create_user  # noqa: F401 — parity import for suite conventions


def _monday_of(semester):
    # A deterministic Monday INSIDE the labelled semester, so a
    # fixture's (day, time, title) twins in different semesters
    # never collide on the dated natural key
    match = re.fullmatch(r"(\d{4})-([RP])", semester or "")
    if match:
        year, season = int(match.group(1)), match.group(2)
        anchor = date(year, 9, 1) if season == "R" else date(year + 1, 2, 1)
    else:
        anchor = date(2020, 1, 1)
    return anchor + timedelta(days=(7 - anchor.weekday()) % 7)


def _lesson(semester, group="IS-1", day=0, time="09:00", title="Programavimas"):
    # One dated event on the semester's fixture week, linked to
    # its group — the shape the legacy fold and the filters read
    stamp = utc_now()
    event = ScheduleEvent.objects.create(
        id=str(uuid.uuid4()), title=title, teacher="J. Jonaitis", room="301",
        date=_monday_of(semester) + timedelta(days=day),
        time_start=time, time_end="10:30",
        semester=semester, last_seen_at=stamp, created_at=stamp,
    )
    group_row, _ = ScheduleGroup.objects.get_or_create(
        slug=f"slug-{group}", defaults={"group_name": group, "last_seen_at": stamp})
    ScheduleEventGroup.objects.create(event=event, group=group_row, last_seen_at=stamp)
    return event


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

        # SEASONAL order: "2025-P" is spring of calendar 2026 and
        # outranks "2025-R" (autumn 2025) though text sorts it lower
        served = {r["semester"] for r in self.client.get("/api/schedule").json()["lessons"]}
        self.assertEqual(served, {"2025-P"})

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
        # Seasonal order — the label year's spring is the newer term
        self.assertEqual(body["semesters"], ["2025-P", "2025-R"])
        self.assertEqual(body["semesterGroups"],
                         [{"semester": "2025-P", "groups": ["VV-2"]},
                          {"semester": "2025-R", "groups": ["IS-1"]}])
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

    def test_dated_events_come_back_with_their_real_dates(self):
        monday = _monday_of("2025-R")
        _lesson("2025-R", day=0)
        _lesson("2025-R", day=2, title="Duomenų bazės")
        # An IRREGULAR one-off — the whole point of the dated model
        _lesson("2025-R", day=3, time="18:00", title="Kviestinė paskaita")

        window = f"from={monday.isoformat()}&to={(monday + timedelta(days=6)).isoformat()}"
        body = self.client.get(f"/api/schedule/events?group=IS-1&{window}").json()

        self.assertEqual([(e["date"], e["title"], e["dayOfWeek"]) for e in body["events"]], [
            (monday.isoformat(), "Programavimas", 0),
            ((monday + timedelta(days=2)).isoformat(), "Duomenų bazės", 2),
            ((monday + timedelta(days=3)).isoformat(), "Kviestinė paskaita", 3),
        ])
        # Outside the window: nothing — the range is the filter
        empty = self.client.get(
            f"/api/schedule/events?group=IS-1&from={(monday + timedelta(days=30)).isoformat()}"
            f"&to={(monday + timedelta(days=36)).isoformat()}").json()
        self.assertEqual(empty["events"], [])

    def test_events_range_validation(self):
        self.assertEqual(self.client.get("/api/schedule/events?from=2026-13-01").status_code, 400)
        self.assertEqual(self.client.get("/api/schedule/events?from=garbage").status_code, 400)
        self.assertEqual(
            self.client.get("/api/schedule/events?from=2026-03-02&to=2026-03-01").status_code, 400)
        self.assertEqual(
            self.client.get("/api/schedule/events?from=2025-01-01&to=2026-01-01").status_code, 400)

    def test_a_teacher_filter_serves_only_their_dated_rows(self):
        # The Rebždys case: a lecture alternating Monday one week
        # and Tuesday the next is ONE event per week under the
        # dated wire — the folded shape used to show both slots
        # every week (6 lectures instead of 5)
        monday = _monday_of("2025-R")
        _lesson("2025-R", day=0, time="13:45", title="Virtualizacijos pagrindai")
        other = _lesson("2025-R", day=0, time="09:00", title="Kita paskaita")
        ScheduleEvent.objects.filter(pk=other.pk).update(teacher="B. Kitas")
        # The alternating twin lands NEXT week, on Tuesday
        next_tue = ScheduleEvent.objects.get(pk=_lesson("2025-R", day=1, time="13:45",
                                                        title="Virtualizacijos pagrindai").pk)
        ScheduleEvent.objects.filter(pk=next_tue.pk).update(date=next_tue.date + timedelta(days=7))

        def week(start):
            window = f"from={start.isoformat()}&to={(start + timedelta(days=6)).isoformat()}"
            url = f"/api/schedule/events?teacher=J.%20Jonaitis&{window}"
            return [(e["date"], e["timeStart"]) for e in self.client.get(url).json()["events"]]

        self.assertEqual(week(monday), [(monday.isoformat(), "13:45")])
        self.assertEqual(week(monday + timedelta(days=7)),
                         [((monday + timedelta(days=8)).isoformat(), "13:45")])

    def test_the_filters_sheet_carries_the_teacher_roster(self):
        from knfapp.schedule.models import ScheduleTeacher
        stamp = utc_now()
        ScheduleTeacher.objects.create(id="t1", name="Eimantas Rebždys, Lekt.", last_seen_at=stamp)
        ScheduleTeacher.objects.create(id="t2", name="agne Zemaite", last_seen_at=stamp)
        body = self.client.get("/api/schedule/filters").json()
        # Case-folded order — a lowercase name does not sink
        self.assertEqual(body["teachers"], ["agne Zemaite", "Eimantas Rebždys, Lekt."])

    def test_two_slugs_folding_to_one_group_name_answer_once(self):
        # The "1 grupė / 2 grupė" case: both subgroup slugs fold
        # to the same group_name and both link the lecture — the
        # wire must carry ONE row per (event, group_name), or the
        # mobile list renders duplicate React keys
        event = _lesson("2025-R", group="LFR-1")
        stamp = utc_now()
        twin, _ = ScheduleGroup.objects.get_or_create(
            slug="slug-LFR-1-b", defaults={"group_name": "LFR-1", "last_seen_at": stamp})
        ScheduleEventGroup.objects.create(event=event, group=twin, last_seen_at=stamp)

        window = f"from={event.date.isoformat()}&to={event.date.isoformat()}"
        body = self.client.get(f"/api/schedule/events?group=LFR-1&{window}").json()
        self.assertEqual(len(body["events"]), 1)

    def test_two_groups_sharing_one_event_answer_under_each(self):
        event = _lesson("2025-R", group="IS-1")
        stamp = utc_now()
        other, _ = ScheduleGroup.objects.get_or_create(
            slug="slug-VV-2", defaults={"group_name": "VV-2", "last_seen_at": stamp})
        ScheduleEventGroup.objects.create(event=event, group=other, last_seen_at=stamp)

        window = f"from={event.date.isoformat()}&to={event.date.isoformat()}"
        for group, expected in (("IS-1", 1), ("VV-2", 1), ("", 2)):
            body = self.client.get(f"/api/schedule/events?group={group}&{window}").json()
            self.assertEqual(len(body["events"]), expected, group)


def _overlay_row(section, data, lang="lt", scraped_at=None):
    # data_json is a JSON column — the structure goes in as-is
    return FacultyInfo.objects.create(
        id=str(uuid.uuid4()), lang=lang, section=section,
        data_json=data,
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
