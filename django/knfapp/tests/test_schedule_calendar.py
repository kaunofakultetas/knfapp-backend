############################################################
#  [*] Regression tests — the timetable's iCalendar feed
#
#  GET /api/schedule/calendar.ics is what a student's phone
#  calendar polls for months, so the RFC 5545 details are the
#  contract: text/calendar in UTF-8 with CRLF lines, every
#  line folded at 75 OCTETS without splitting a Lithuanian
#  letter, TEXT escaped, UTC times from the Vilnius wall
#  clock across the October clock change, a UID that
#  survives the scraper's relabel-in-place (or every student
#  would see each lecture twice after a retype), exams
#  leading their SUMMARY, one aggregate for a 304 on the
#  STRONG tag, and 400/404 for a scope that is missing,
#  doubled or unknown.
############################################################


import uuid
from datetime import date, datetime, timedelta, timezone


from django.db import connection
from django.test import Client, SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext


from knfapp.common.timestamps import utc_now
from knfapp.schedule import ical
from knfapp.schedule.models import ScheduleEvent, ScheduleEventGroup, ScheduleGroup, ScheduleTeacher
from knfapp.scraper import schedule_scraper as ss


def _event(day, time_start="09:45", time_end="11:15", title="Programavimas", group="ISKS-2",
           lecture_type="Paskaita", teacher="Ilona Veitaitė, Doc., Dr.", room="II k. kl. (knf)"):
    # One dated event linked to one group (and its teacher on
    # the roster) — `day` an absolute date
    stamp = utc_now()
    event = ScheduleEvent.objects.create(
        id=str(uuid.uuid4()), title=title, teacher=teacher, room=room,
        lecture_type=lecture_type, date=day, time_start=time_start, time_end=time_end,
        semester="2026-R", last_seen_at=stamp, created_at=stamp,
    )
    group_row, _ = ScheduleGroup.objects.get_or_create(
        slug=f"slug-{group}", defaults={"group_name": group, "last_seen_at": stamp})
    ScheduleEventGroup.objects.create(event=event, group=group_row, last_seen_at=stamp)
    ScheduleTeacher.objects.get_or_create(name=teacher, defaults={"id": str(uuid.uuid4()), "last_seen_at": stamp})
    return event


def _unfold(body: str) -> list:
    # The logical lines: CRLF-split, continuations re-joined
    lines = []
    for physical in body.split("\r\n"):
        if physical.startswith(" ") and lines:
            lines[-1] += physical[1:]
        elif physical:
            lines.append(physical)
    return lines


def _events(body: str) -> list:
    # Each VEVENT as a {property: value} dict (parameters kept
    # in the key, values still escaped)
    out, current = [], None
    for line in _unfold(body):
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT":
            out.append(current)
            current = None
        elif current is not None:
            key, _, value = line.partition(":")
            current[key] = value
    return out


class RenderingTests(SimpleTestCase):

    def test_text_is_escaped_per_rfc_5545(self):
        self.assertEqual(ical.escape_text("a\\b;c,d\ne"), "a\\\\b\\;c\\,d\\ne")
        self.assertEqual(ical.escape_text("x\r\ny"), "x\\ny")

    def test_lines_fold_at_75_octets_and_never_inside_a_letter(self):
        line = "DESCRIPTION:" + "Informacinių sistemų saugos pagrindai, Švietimo ąžuolų šaknys " * 6
        folded = ical.fold_line(line)
        for number, physical in enumerate(folded.split("\r\n")):
            encoded = physical.encode("utf-8")
            self.assertLessEqual(len(encoded), 75, physical)
            # A split inside a two-byte letter would not decode
            encoded.decode("utf-8")
            if number:
                self.assertTrue(physical.startswith(" "))
        self.assertEqual(folded.replace("\r\n ", ""), line)

    def test_vilnius_wall_times_become_utc_across_the_october_change(self):
        # 2026-10-25 04:00 EEST → 03:00 EET: a 09:45 lecture is
        # 06:45Z on the Friday before, 07:45Z on the Monday after
        self.assertEqual(ical.utc_stamp(date(2026, 10, 23), "09:45"), "20261023T064500Z")
        self.assertEqual(ical.utc_stamp(date(2026, 10, 26), "09:45"), "20261026T074500Z")
        # and a winter date in January
        self.assertEqual(ical.utc_stamp(date(2027, 1, 7), "09:00"), "20270107T070000Z")
        self.assertIsNone(ical.utc_stamp(date(2026, 10, 23), "9.45"))


class CalendarRouteTests(TestCase):

    def setUp(self):
        self.client = Client()
        self.today = date.today()

    def _get(self, params=None, **headers):
        return self.client.get("/api/schedule/calendar.ics", params or {}, **headers)

    def test_a_group_feed_is_a_well_formed_utf8_calendar(self):
        _event(self.today + timedelta(days=2), title="Akademinis raštingumas; įvadas, 1 dalis")
        response = self._get({"group": "ISKS-2"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/calendar; charset=utf-8")
        self.assertIn("public", response["Cache-Control"])
        self.assertTrue(response["ETag"].startswith('"'), "the feed's ETag is strong")
        body = response.content.decode("utf-8")
        # CRLF everywhere: no bare LF anywhere in the body
        self.assertNotIn("\n", body.replace("\r\n", ""))
        self.assertTrue(body.endswith("END:VCALENDAR\r\n"))
        lines = _unfold(body)
        for expected in ("BEGIN:VCALENDAR", "VERSION:2.0", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
                         "X-WR-CALNAME:KNF tvarkaraštis — ISKS-2", "X-WR-TIMEZONE:Europe/Vilnius",
                         "REFRESH-INTERVAL;VALUE=DURATION:PT6H", "X-PUBLISHED-TTL:PT6H"):
            self.assertIn(expected, lines)
        self.assertTrue(any(line.startswith("PRODID:") for line in lines))
        for physical in body.split("\r\n"):
            self.assertLessEqual(len(physical.encode("utf-8")), 75)
        event = _events(body)[0]
        self.assertEqual(event["SUMMARY"], "Akademinis raštingumas\\; įvadas\\, 1 dalis")
        self.assertEqual(event["LOCATION"], "II k. kl. (knf)")
        self.assertTrue(event["DTSTART"].endswith("Z") and event["DTEND"].endswith("Z"))
        self.assertIn("DTSTAMP", event)

    def test_exams_lead_their_summary_and_the_details_name_everything(self):
        exam = _event(self.today + timedelta(days=3), title="Akademinis raštingumas",
                      lecture_type="Egzaminas|1,2")
        # A second group sharing the exam — both named
        other, _ = ScheduleGroup.objects.get_or_create(
            slug="slug-EV-1", defaults={"group_name": "EV-1", "last_seen_at": utc_now()})
        ScheduleEventGroup.objects.create(event=exam, group=other, last_seen_at=utc_now())
        event = _events(self._get({"group": "ISKS-2"}).content.decode("utf-8"))[0]
        self.assertEqual(event["SUMMARY"], "Egzaminas: Akademinis raštingumas")
        self.assertEqual(event["CATEGORIES"], "Egzaminas")
        self.assertEqual(event["DESCRIPTION"],
                         "Tipas: Egzaminas\\nDėstytojas: Ilona Veitaitė\\, Doc.\\, Dr.\\n"
                         "Pogrupiai: 1\\, 2\\nGrupės: EV-1\\, ISKS-2")
        english = _events(self._get({"group": "ISKS-2", "lang": "en"}).content.decode("utf-8"))[0]
        self.assertEqual(english["SUMMARY"], "Exam: Akademinis raštingumas")
        self.assertTrue(english["DESCRIPTION"].startswith("Type: Exam\\nTeacher: "))

    def test_the_window_runs_a_fortnight_back_and_forward_to_the_end_of_the_data(self):
        _event(self.today - timedelta(days=20), title="Seniai")
        _event(self.today - timedelta(days=10), title="Neseniai")
        _event(self.today + timedelta(days=120), title="Sausio egzaminas", lecture_type="Egzaminas")
        titles = [event["SUMMARY"] for event in _events(self._get({"group": "ISKS-2"}).content.decode("utf-8"))]
        self.assertEqual(titles, ["Neseniai", "Egzaminas: Sausio egzaminas"])

    def test_the_uid_survives_the_scrapers_relabel_in_place(self):
        # The row stored before types existed, then the first typed
        # run: same event id, so the same UID — a student's calendar
        # edits the event instead of doubling it
        lesson = {"title": "Programavimas", "teacher": "A. Petraitis", "room": "302",
                  "lecture_type": "", "date": self.today + timedelta(days=4),
                  "time_start": "10:00", "time_end": "11:30",
                  "group_name": "ISKS-2", "slug": "isks-2k-1gr", "semester": "2026-R"}
        groups = {"isks-2k-1gr": {"display_name": "ISKS 2 kursas", "group_name": "ISKS-2"}}
        ss._sync_schedule([lesson], groups, datetime.now(timezone.utc))
        before = _events(self._get({"group": "ISKS-2"}).content.decode("utf-8"))[0]["UID"]
        ss._sync_schedule([{**lesson, "lecture_type": "Pratybos|1"}], groups, datetime.now(timezone.utc))
        after = _events(self._get({"group": "ISKS-2"}).content.decode("utf-8"))
        self.assertEqual([event["UID"] for event in after], [before])
        self.assertTrue(before.endswith("@knfapp.knf-hosting.lt"))
        self.assertEqual(after[0]["CATEGORIES"], "Pratybos")

    def test_a_teacher_feed_serves_their_events_across_groups(self):
        _event(self.today + timedelta(days=1), title="Tinklai", teacher="Eimantas Rebždys, Lekt.")
        _event(self.today + timedelta(days=1), time_start="12:00", time_end="13:30",
               title="Duomenų bazės", group="EV-1", teacher="Eimantas Rebždys, Lekt.")
        _event(self.today + timedelta(days=1), time_start="14:00", time_end="15:30", title="Kita")
        body = self._get({"teacher": "Eimantas Rebždys, Lekt."}).content.decode("utf-8")
        self.assertEqual([event["SUMMARY"] for event in _events(body)], ["Tinklai", "Duomenų bazės"])
        self.assertIn("X-WR-CALNAME:KNF tvarkaraštis — Eimantas Rebždys\\, Lekt.", _unfold(body))

    def test_a_matching_strong_etag_is_a_304_from_one_aggregate(self):
        _event(self.today + timedelta(days=2))
        first = self._get({"group": "ISKS-2"})
        with CaptureQueriesContext(connection) as context:
            again = self._get({"group": "ISKS-2"}, HTTP_IF_NONE_MATCH=first["ETag"])
        selects = [q["sql"] for q in context.captured_queries if q["sql"].lstrip().upper().startswith("SELECT")]
        self.assertEqual(again.status_code, 304)
        self.assertEqual(len(selects), 1, selects)
        self.assertEqual(again["ETag"], first["ETag"])
        # A 200 is the aggregate, the existence probe and ONE data query
        with CaptureQueriesContext(connection) as context:
            self._get({"group": "ISKS-2", "lang": "en"})
        selects = [q["sql"] for q in context.captured_queries if q["sql"].lstrip().upper().startswith("SELECT")]
        self.assertEqual(len(selects), 3, selects)

    def test_a_missing_doubled_or_unknown_scope_is_refused(self):
        _event(self.today + timedelta(days=2))
        self.assertEqual(self._get().status_code, 400)
        self.assertEqual(self._get({"group": "ISKS-2", "teacher": "Ilona Veitaitė"}).status_code, 400)
        self.assertEqual(self._get({"group": " "}).status_code, 400)
        missing = self._get({"group": "NERA-9"})
        self.assertEqual((missing.status_code, missing.json()["code"]), (404, "calendar_scope_unknown"))
        self.assertEqual(self._get({"teacher": "Nežinomas"}).status_code, 404)
        # A known group with nothing in the window is an empty
        # calendar, not an error
        _event(self.today - timedelta(days=60), group="LFR-3", title="Seniai")
        empty = self._get({"group": "LFR-3"})
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(_events(empty.content.decode("utf-8")), [])
