############################################################
#  [*] Regression tests — the schedule read routes' cost and
#      shape
#
#  What the timetable reads promise beyond their answers:
#  a revalidation is decided on ONE aggregate before any
#  body work (KNF-134), the legacy weekly fold is one SQL
#  page instead of the whole semester in Python (KNF-133)
#  and still answers what the Python fold answered, the
#  filter sheet's group names come DISTINCT out of the
#  database (KNF-151), each term publishes its first and
#  last date so the app's semester jump lands on a real
#  week (KNF-037), and the stored "Kind|subgroups"
#  lecture_type reaches the wire as two clean fields
#  (KNF-078).
############################################################


import uuid
from datetime import date, timedelta


from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext


from knfapp.common.timestamps import utc_now
from knfapp.schedule.models import ScheduleEvent, ScheduleEventGroup, ScheduleGroup


# A fixed autumn Monday — every fixture date is an offset of it
MONDAY = date(2025, 9, 1)


def _event(day_offset, time="09:00", title="Programavimas", group="IS-1",
           semester="2025-R", lecture_type="", room="301"):
    # One dated event linked to one group — the shape every
    # read route folds, filters or counts
    stamp = utc_now()
    event = ScheduleEvent.objects.create(
        id=str(uuid.uuid4()), title=title, teacher="J. Jonaitis", room=room,
        lecture_type=lecture_type, date=MONDAY + timedelta(days=day_offset),
        time_start=time, time_end="10:30", semester=semester,
        last_seen_at=stamp, created_at=stamp,
    )
    group_row, _ = ScheduleGroup.objects.get_or_create(
        slug=f"slug-{group}", defaults={"group_name": group, "last_seen_at": stamp})
    ScheduleEventGroup.objects.create(event=event, group=group_row, last_seen_at=stamp)
    return event


def _selects(context):
    # The SELECTs a request ran — ATOMIC_REQUESTS adds
    # SAVEPOINT bookkeeping that is not work
    return [q["sql"] for q in context.captured_queries if q["sql"].lstrip().upper().startswith("SELECT")]


class RevalidationCostTests(TestCase):

    def setUp(self):
        self.client = Client()
        for i in range(6):
            _event(i % 5, time=f"{9 + i:02d}:00")

    def test_every_route_answers_a_matching_etag_from_one_aggregate(self):
        # The 304 is decided on the table version alone — the
        # fold, the page query and the filter lists never run
        # for an answer the client already holds
        window = f"from={MONDAY.isoformat()}&to={(MONDAY + timedelta(days=6)).isoformat()}"
        for url in ("/api/schedule", f"/api/schedule/events?{window}", "/api/schedule/filters"):
            with self.subTest(url=url):
                first = self.client.get(url)
                self.assertEqual(first.status_code, 200)
                with CaptureQueriesContext(connection) as context:
                    cached = self.client.get(url, HTTP_IF_NONE_MATCH=first["ETag"])
                self.assertEqual(cached.status_code, 304)
                self.assertEqual(len(_selects(context)), 1, _selects(context))
                self.assertEqual(cached["ETag"], first["ETag"])
                self.assertIn("public", cached["Cache-Control"])

    def test_a_changed_table_is_a_fresh_200(self):
        first = self.client.get("/api/schedule/filters")
        _event(3, time="16:00", title="Nauja paskaita")
        again = self.client.get("/api/schedule/filters", HTTP_IF_NONE_MATCH=first["ETag"])
        self.assertEqual(again.status_code, 200)


class LegacyFoldTests(TestCase):

    def setUp(self):
        self.client = Client()
        # Seven patterns over two weeks: every pattern twice (a
        # weekly lecture) must fold to ONE row, and the pages must
        # tile the whole set without a gap or a repeat
        self.patterns = [(d, f"{8 + d + h:02d}:00") for d in range(5) for h in (0, 3) if (d, h) != (4, 3)][:7]
        for day, time in self.patterns:
            _event(day, time=time, title=f"Dalykas {day}{time}")
            _event(day + 7, time=time, title=f"Dalykas {day}{time}")

    def _page(self, query):
        body = self.client.get(f"/api/schedule?semester=2025-R&{query}").json()
        return body["lessons"], body["hasMore"]

    def test_the_pages_tile_the_folded_set_in_day_time_order(self):
        first, more = self._page("limit=3")
        self.assertTrue(more)
        second, more = self._page("limit=3&offset=3")
        self.assertTrue(more)
        third, more = self._page("limit=3&offset=6")
        self.assertFalse(more)
        rows = first + second + third
        self.assertEqual(len(rows), len(self.patterns))
        self.assertEqual(len({r["id"] for r in rows}), len(self.patterns))
        self.assertEqual([(r["dayOfWeek"], r["timeStart"]) for r in rows], sorted(self.patterns))

    def test_the_day_filter_runs_in_the_database_and_keeps_its_answer(self):
        rows, more = self._page("day=2")
        self.assertFalse(more)
        self.assertEqual({r["dayOfWeek"] for r in rows}, {2})
        self.assertEqual(len(rows), len([p for p in self.patterns if p[0] == 2]))

    def test_a_200_is_a_constant_handful_of_queries_whatever_the_table_holds(self):
        # The old fold read every (event × group) row of the
        # semester into Python; the page is now one statement
        with CaptureQueriesContext(connection) as context:
            response = self.client.get("/api/schedule?limit=1")
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(_selects(context)), 3, _selects(context))
        fold = [sql for sql in _selects(context) if "DISTINCT" in sql.upper()]
        self.assertEqual(len(fold), 1)
        self.assertIn("LIMIT", fold[0].upper())

    def test_the_ids_are_the_same_hash_the_python_fold_minted(self):
        # Old app builds key their rows on the id — the SQL fold
        # must mint exactly the id the Python fold did
        import hashlib
        row = self._page("day=0&limit=1")[0][0]
        key = (0, row["timeStart"], row["timeEnd"], row["title"], row["teacher"], row["room"],
               row["group"], row["semester"])
        self.assertEqual(row["id"], hashlib.sha256("|".join(map(str, key)).encode()).hexdigest()[:16])


class FilterSheetTests(TestCase):

    def setUp(self):
        self.client = Client()

    def test_each_term_publishes_its_first_and_last_date(self):
        # 2025-R spans two weeks from a Tuesday; 2025-P is the
        # January session five months on; a stray label (one
        # row) is no term at all
        for i in range(5):
            _event(1 + i, semester="2025-R", time=f"{9 + i:02d}:00")
        _event(15, semester="2025-R", time="12:00", title="Paskutinė")
        for i in range(5):
            _event(126 + i, semester="2025-P", time=f"{9 + i:02d}:00")
        _event(40, semester="2026-R", title="Klaidinga")

        body = self.client.get("/api/schedule/filters").json()
        self.assertEqual(body["semesters"], ["2025-P", "2025-R"])
        self.assertEqual(body["terms"], [
            {"semester": "2025-P", "from": (MONDAY + timedelta(days=126)).isoformat(),
             "to": (MONDAY + timedelta(days=130)).isoformat()},
            {"semester": "2025-R", "from": (MONDAY + timedelta(days=1)).isoformat(),
             "to": (MONDAY + timedelta(days=15)).isoformat()},
        ])

    def test_group_names_come_distinct_from_the_database(self):
        for i in range(6):
            _event(i % 5, time=f"{9 + i:02d}:00", group="IS-1" if i % 2 else "VV-2")
        with CaptureQueriesContext(connection) as context:
            body = self.client.get("/api/schedule/filters").json()
        self.assertEqual(body["groups"], ["IS-1", "VV-2"])
        self.assertEqual(body["days"], [0, 1, 2, 3, 4])
        names = [sql for sql in _selects(context) if "group_name" in sql and "semester" not in sql]
        self.assertTrue(names and all("DISTINCT" in sql.upper() for sql in names), names)


class EventTypeWireTests(TestCase):

    def test_the_stored_type_reaches_the_wire_as_kind_and_subgroups(self):
        _event(0, time="09:00", lecture_type="Egzaminas|1,2", title="Egzaminas")
        _event(0, time="11:00", lecture_type="Paskaita", title="Paskaita")
        _event(0, time="13:00", lecture_type="", title="Senas įrašas")

        window = f"from={MONDAY.isoformat()}&to={MONDAY.isoformat()}"
        events = Client().get(f"/api/schedule/events?{window}").json()["events"]
        self.assertEqual([(e["title"], e["lectureType"], e["subgroups"]) for e in events], [
            ("Egzaminas", "Egzaminas", ["1", "2"]),
            ("Paskaita", "Paskaita", []),
            ("Senas įrašas", "", []),
        ])
