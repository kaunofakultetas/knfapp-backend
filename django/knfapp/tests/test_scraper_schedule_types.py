############################################################
#  [*] Regression tests — timetable event types, subgroups,
#      the feed legend and the feed byte cap
#
#  KNF-078: an exam must import AS an exam — the popover's
#  "Tipas:" (or the "(EGZAMINAS)" marker, or the legend)
#  becomes the kind, "Pogrupiai: N" the subgroups, both in
#  lecture_type — and the rows stored before types existed
#  are relabelled IN PLACE (same ids, nothing counted new,
#  no fortnight of history duplicated). The type is the
#  SESSION's attribute: one slot is one event however the
#  programmes' feeds type it (the first typed live run split
#  66 slots that "Paskaita" and "Paskaitos ir seminarai"
#  feeds shared — the next run folds each back into one,
#  past copies included). KNF-156: the feed's
#  own colour legend steers the retake filter and a label
#  the scraper does not know is a WARNING naming it.
#  KNF-075: the feed body is read under its own cap and a
#  body past it is refused whole, and a run that could not
#  read every feed says which in its error_message. The
#  fixtures are the live feed's markup, attribute escaping
#  and all.
############################################################


import html
from datetime import date, datetime, timedelta, timezone
from unittest import mock


from django.test import SimpleTestCase, TestCase


from knfapp.scraper import common, schedule_scraper as ss
from knfapp.scraper.models import ScraperRun
from knfapp.schedule.models import ScheduleEvent, ScheduleEventGroup, ScheduleGroup


def _popover(subject, showed_type="Pratybos, Privalomasis", subgroups="1", marker="",
             room="VI k. kl. (knf)", colour="#F1F1F1"):
    # One event title exactly as tvarkarasciai.vu.lt serves it:
    # the popover anchor with HTML-escaped HTML in its data-*
    # attributes, the marker and "Pogrupiai" after it, the
    # room line after the first <br>
    attributes = {
        "data-toggle": "popover",
        "data-title": f'<strong><a href="/knf/subjects/x/">{subject}</a></strong>',
        "data-showed_type": (f"<span><strong>Tipas: </strong>{showed_type}</span>"
                             if showed_type is not None else None),
        "data-academics": ('<span><strong>Dėstytojai: </strong>'
                           '<a href="/knf/employees/ilona-veitaite/">Ilona Veitaitė, Doc., Dr.</a></span>'),
        "data-rooms": f'<span><strong>Patalpos: </strong><a href="/knf/rooms/r/">{room}</a></span>',
        "data-comments": "",
        "data-subgroups": f"<span><strong>Pogrupiai: </strong>{subgroups}</span>" if subgroups else "",
        "data-color": colour,
    }
    attribute_text = " ".join(f'{name}="{html.escape(value, quote=True)}"'
                              for name, value in attributes.items() if value is not None)
    tail = (f" ({marker})" if marker else "") + (f" Pogrupiai: {subgroups}" if subgroups else "")
    return f"<a {attribute_text}>{subject}</a>{tail}<br>{room}<br>"


def _feed_event(start, end, colour="#F1F1F1", **popover):
    return {"className": "e1", "title": _popover(colour=colour, **popover),
            "start": start, "end": end, "color": colour, "borderColor": "#D8D8D8"}


class TitleParseTests(SimpleTestCase):

    def test_an_exam_popover_yields_subject_kind_and_subgroup(self):
        title = _popover("Akademinis ir informacinis raštingumas", showed_type="Egzaminas, Privalomasis",
                         marker="EGZAMINAS", room="II k. kl. (knf)", colour="#FFC2CC")
        self.assertEqual(ss._parse_title(title),
                         ("Akademinis ir informacinis raštingumas", "Egzaminas", ["1"]))

    def test_the_marker_stands_in_when_the_popover_names_no_type(self):
        title = _popover("Programavimas", showed_type=None, marker="EGZAMINAS", subgroups="")
        self.assertEqual(ss._parse_title(title), ("Programavimas", "Egzaminas", []))
        # The room's "(knf)" is never a marker — it sits past the
        # first <br>, and it is not upper-case anyway
        quiet = _popover("Programavimas", showed_type=None, subgroups="", room="II k. kl. (KNF)")
        self.assertEqual(ss._parse_title(quiet), ("Programavimas", "", []))

    def test_a_whole_group_lecture_names_no_subgroup(self):
        title = _popover("Aukštoji matematika", showed_type="Paskaita, Privalomasis", subgroups="")
        self.assertEqual(ss._parse_title(title), ("Aukštoji matematika", "Paskaita", []))

    def test_plain_titles_and_linkless_markup_keep_their_old_reading(self):
        self.assertEqual(ss._parse_title("  Programavimas "), ("Programavimas", "", []))
        self.assertEqual(ss._parse_title("<b>Kursas</b>"), ("Kursas", "", []))

    def test_subgroup_lists_split_in_natural_order(self):
        self.assertEqual(ss._split_subgroups("10, 2;1"), ["1", "2", "10"])
        self.assertEqual(ss._split_subgroups(""), [])


class KindPickTests(SimpleTestCase):

    def test_an_exam_wins_then_the_majority_then_the_text_order(self):
        self.assertEqual(ss._pick_kind(["Paskaita", "Egzaminas", "Paskaita"]), "Egzaminas")
        self.assertEqual(ss._pick_kind(["Pratybos", "Paskaita", "Pratybos"]), "Pratybos")
        # A tie settles by text, so two runs never flip a row
        self.assertEqual(ss._pick_kind(["Paskaitos ir seminarai", "Paskaita"]), "Paskaita")
        self.assertEqual(ss._pick_kind(["", ""]), "")
        self.assertEqual(ss._pick_kind(["", "Įskaita", "Paskaita"]), "Įskaita")


class LectureTypeFormatTests(SimpleTestCase):

    def test_join_and_split_are_exact_inverses(self):
        for kind, subgroups, stored in (
            ("Pratybos", ["2", "1"], "Pratybos|1,2"),
            ("Paskaita", [], "Paskaita"),
            ("", [], ""),
            ("Egzaminas", ["1"], "Egzaminas|1"),
        ):
            with self.subTest(stored=stored):
                self.assertEqual(ss.join_lecture_type(kind, subgroups), stored)
                self.assertEqual(ss.split_lecture_type(stored), (kind, sorted(subgroups, key=int)))

    def test_the_separator_never_survives_inside_a_part(self):
        stored = ss.join_lecture_type("A|B", ["1|2"])
        self.assertEqual(ss.split_lecture_type(stored), ("A/B", ["1/2"]))


class FeedParseTests(SimpleTestCase):

    def _scrape(self, events, legend=None):
        import json
        body = {"events": events}
        if legend is not None:
            body["event_colors"] = legend
        served = (json.dumps(body).encode("utf-8"), ss.EVENT_URL_TEMPLATE)
        with mock.patch.object(ss, "fetch", return_value=served) as fetch:
            lessons, stats = ss.scrape_group_schedule(
                "informacijos-sistemos-ir-kibernetine-sauga-1k-1g-8",
                "Informacijos sistemos ir kibernetinė sauga - 1 kursas", "2027-01-01", "2027-01-31")
        return lessons, stats, fetch

    def test_the_live_feed_shape_imports_kinds_and_subgroups(self):
        lessons, stats, _ = self._scrape([
            _feed_event("2027-01-07T09:00:00", "2027-01-07T11:00:00", colour="#FFC2CC",
                        subject="Akademinis ir informacinis raštingumas", showed_type="Egzaminas, Privalomasis",
                        marker="EGZAMINAS", room="II k. kl. (knf)"),
            # Two subgroups sitting ONE slot in ONE room: one
            # session, the subgroups unioned
            _feed_event("2027-01-08T09:45:00", "2027-01-08T11:15:00", subject="Programavimo įvadas",
                        subgroups="2"),
            _feed_event("2027-01-08T09:45:00", "2027-01-08T11:15:00", subject="Programavimo įvadas",
                        subgroups="1"),
            _feed_event("2027-01-08T13:45:00", "2027-01-08T15:15:00", subject="Aukštoji matematika",
                        showed_type="Paskaita, Privalomasis", subgroups="", room="V. Gronsko a. (knf)"),
        ], legend={"#FFC2CC": "EGZAMINAS"})

        self.assertEqual([(lesson["title"], lesson["lecture_type"]) for lesson in lessons], [
            ("Akademinis ir informacinis raštingumas", "Egzaminas|1"),
            ("Programavimo įvadas", "Pratybos|1,2"),
            ("Aukštoji matematika", "Paskaita"),
        ])
        self.assertEqual(lessons[0]["room"], "II k. kl. (knf)")
        self.assertEqual(lessons[0]["teacher"], "Ilona Veitaitė, Doc., Dr.")
        self.assertEqual(stats["kinds"], {"Egzaminas": 1, "Pratybos": 1, "Paskaita": 1})
        self.assertEqual(stats["legend"], {"#ffc2cc": "EGZAMINAS"})

    def test_the_legend_names_the_kind_only_when_the_popover_is_silent(self):
        lessons, _, _ = self._scrape([
            _feed_event("2027-01-07T09:00:00", "2027-01-07T11:00:00", colour="#FFC2CC",
                        subject="Programavimas", showed_type=None, subgroups=""),
            _feed_event("2027-01-07T12:00:00", "2027-01-07T13:00:00", subject="Tinklai",
                        showed_type=None, subgroups=""),
        ], legend={"#FFC2CC": "EGZAMINAS", "#F1F1F1": "Įprastas įvykis"})
        # The regular legend label is no kind at all
        self.assertEqual([lesson["lecture_type"] for lesson in lessons], ["Egzaminas", ""])

    def test_a_colour_the_legend_calls_a_retake_is_dropped_like_the_configured_one(self):
        lessons, stats, _ = self._scrape([
            _feed_event("2027-01-07T09:00:00", "2027-01-07T11:00:00", colour="#ABCDEF",
                        subject="Programavimas", showed_type="Egzaminas, Privalomasis"),
            _feed_event("2027-01-07T12:00:00", "2027-01-07T13:00:00", subject="Tinklai"),
        ], legend={"#ABCDEF": "PERLAIKYMAS"})
        self.assertEqual([lesson["title"] for lesson in lessons], ["Tinklai"])
        self.assertEqual(stats["retakes"], 1)

    def test_the_feed_is_read_under_its_own_cap_and_refused_whole_past_it(self):
        _, _, fetch = self._scrape([])
        kwargs = fetch.call_args.kwargs
        self.assertEqual((kwargs["max_bytes"], kwargs["allow_truncated"]), (ss.FEED_MAX_BYTES, False))
        with mock.patch.object(ss, "fetch", return_value=None):
            with self.assertRaises(RuntimeError):
                ss.scrape_group_schedule("isks-1", "ISKS 1 kursas", "2027-01-01", "2027-01-31")


class FetchCapTests(SimpleTestCase):

    class _Response:
        def __init__(self, body):
            self.body = body
            self.url = "https://tvarkarasciai.vu.lt/feed"
            self.headers = {"Content-Type": "application/json"}

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size):
            for i in range(0, len(self.body), chunk_size):
                yield self.body[i:i + chunk_size]

        def close(self):
            return None

    def _fetch(self, body, **kwargs):
        session = mock.Mock()
        session.get.return_value = self._Response(body)
        with mock.patch.object(common, "get_session", return_value=session):
            return common.fetch("https://tvarkarasciai.vu.lt/feed", common.SCHEDULE_HOSTS,
                                content_types=common.JSON_CONTENT_TYPES, **kwargs)

    def test_a_body_past_the_cap_is_cut_by_default_and_refused_on_request(self):
        body = b"x" * 40000
        cut = self._fetch(body, max_bytes=20000)
        self.assertEqual(len(cut[0]), 20000)
        self.assertIsNone(self._fetch(body, max_bytes=20000, allow_truncated=False))

    def test_a_body_exactly_at_the_cap_is_complete(self):
        body = b"y" * 32768
        with self.assertNoLogs(common.logger, "WARNING"):
            whole = self._fetch(body, max_bytes=32768, allow_truncated=False)
        self.assertEqual(whole[0], body)


class RelabelInPlaceTests(TestCase):

    GROUPS = {"isks-1-kursas": {"display_name": "ISKS 1 kursas", "group_name": "ISKS-1"},
              "isks-1-b": {"display_name": "ISKS 1 kursas 2 grupė", "group_name": "ISKS-1"}}

    def _lesson(self, lecture_type, days=7, **overrides):
        lesson = {"title": "Programavimas", "teacher": "A. Petraitis", "room": "302",
                  "lecture_type": lecture_type, "date": date.today() + timedelta(days=days),
                  "time_start": "10:00", "time_end": "11:30",
                  "group_name": "ISKS-1", "slug": "isks-1-kursas", "semester": "2026-R"}
        lesson.update(overrides)
        return lesson

    def _sync(self, lessons):
        return ss._sync_schedule(lessons, self.GROUPS, datetime.now(timezone.utc))

    def _types(self):
        return sorted(ScheduleEvent.objects.values_list("date", "lecture_type"))

    def test_untyped_rows_take_their_kind_in_place_past_and_future(self):
        # The rows every run before this one stored: no type.
        # The first typed run must relabel them — same ids,
        # nothing counted new (no push), and the fortnight of
        # past rows the window re-reads NOT duplicated
        self._sync([self._lesson(""), self._lesson("", days=-3)])
        ids = set(ScheduleEvent.objects.values_list("id", flat=True))

        self.assertEqual(self._sync([self._lesson("Pratybos|1"), self._lesson("Pratybos|1", days=-3)]), (0, 0))
        self.assertEqual(set(ScheduleEvent.objects.values_list("id", flat=True)), ids)
        self.assertEqual([t for _, t in self._types()], ["Pratybos|1", "Pratybos|1"])

    def test_a_moved_subgroup_set_keeps_the_event(self):
        self._sync([self._lesson("Pratybos|1")])
        event_id = ScheduleEvent.objects.get().id
        self.assertEqual(self._sync([self._lesson("Pratybos|1,2")]), (0, 0))
        self.assertEqual(ScheduleEvent.objects.get().id, event_id)
        self.assertEqual(ScheduleEvent.objects.get().lecture_type, "Pratybos|1,2")

    def test_two_feeds_typing_one_slot_differently_make_one_event_and_the_exam_wins(self):
        # One legacy row; two programmes' feeds type the same
        # session differently. It stays ONE event (the legacy
        # id), typed as the exam, linked to both groups
        self._sync([self._lesson("")])
        legacy = ScheduleEvent.objects.get().id
        self.assertEqual(self._sync([self._lesson("Paskaita"),
                                     self._lesson("Egzaminas", slug="isks-1-b")]), (0, 0))
        event = ScheduleEvent.objects.get()
        self.assertEqual((event.id, event.lecture_type), (legacy, "Egzaminas"))
        self.assertEqual(ScheduleEventGroup.objects.filter(event=event).count(), 2)
        # And a second run in the other feed order changes nothing
        self.assertEqual(self._sync([self._lesson("Egzaminas", slug="isks-1-b"),
                                     self._lesson("Paskaita")]), (0, 0))
        self.assertEqual(ScheduleEvent.objects.get().id, legacy)

    def _split(self, days, second_slug="isks-1-b"):
        # The state the first typed live run left on shared slots:
        # one physical session stored twice, one row per feed type
        stamp = datetime.now(timezone.utc) - timedelta(hours=6)
        rows = []
        for lecture_type, slug in (("Paskaita", "isks-1-kursas"), ("Paskaitos ir seminarai", second_slug)):
            event = ScheduleEvent.objects.create(
                id=f"split-{slug}", title="Programavimas", teacher="A. Petraitis", room="302",
                lecture_type=lecture_type, date=date.today() + timedelta(days=days),
                time_start="10:00", time_end="11:30", semester="2026-R",
                last_seen_at=stamp, created_at=stamp)
            group, _ = ScheduleGroup.objects.get_or_create(slug=slug, defaults={"group_name": "ISKS-1", "last_seen_at": stamp})
            ScheduleEventGroup.objects.create(event=event, group=group, last_seen_at=stamp)
            rows.append(event.id)
        return rows

    def test_a_split_slot_folds_back_into_one_event_past_included(self):
        kept, copy = self._split(days=-3)
        _added, removed = self._sync([self._lesson("Paskaita", days=-3),
                                      self._lesson("Paskaitos ir seminarai", days=-3, slug="isks-1-b")])
        self.assertEqual(removed, 1)
        self.assertEqual(list(ScheduleEvent.objects.values_list("id", flat=True)), [kept])
        self.assertEqual(sorted(ScheduleEventGroup.objects.values_list("group_id", flat=True)),
                         ["isks-1-b", "isks-1-kursas"])

    def test_a_copy_a_failed_feed_still_links_is_never_judged(self):
        # The second feed did not answer this run: its claim on the
        # copy cannot be read, so the copy stays
        kept, copy = self._split(days=-3, second_slug="ft-1-kursas")
        _added, removed = self._sync([self._lesson("Paskaita", days=-3)])
        self.assertEqual(removed, 0)
        self.assertEqual(sorted(ScheduleEvent.objects.values_list("id", flat=True)), sorted([kept, copy]))

    def test_two_feeds_union_their_subgroups_into_one_event(self):
        self.assertEqual(self._sync([self._lesson("Pratybos|1"),
                                     self._lesson("Pratybos|2", slug="isks-1-b")]), (1, 0))
        event = ScheduleEvent.objects.get()
        self.assertEqual(event.lecture_type, "Pratybos|1,2")
        self.assertEqual(ScheduleEventGroup.objects.filter(event=event).count(), 2)


class RunHealthTests(TestCase):

    def _feed(self, slug, legend=None):
        when = date.today() + timedelta(days=7)
        label = ss._get_semester_label(datetime(when.year, when.month, when.day))
        lessons = [{"title": "Programavimas", "teacher": "A. Petraitis", "room": "302",
                    "lecture_type": "Paskaita", "date": when,
                    "time_start": f"{9 + i:02d}:00", "time_end": f"{10 + i:02d}:30",
                    "group_name": "ISKS-1", "slug": slug, "semester": label}
                   for i in range(5)]
        stats = {"events": 5, "all_day": 0, "retakes": 0, "unparsable": 0, "untitled": 0,
                 "colours": {}, "kinds": {"Paskaita": 5}, "legend": legend or {}}
        return lessons, stats

    def _run(self, schedule):
        groups = [{"slug": "isks-1-kursas", "display_name": "ISKS 1 kursas"},
                  {"slug": "ft-1-kursas", "display_name": "FT 1 kursas"}]
        with mock.patch.object(ss, "scrape_group_list", return_value=groups), \
                mock.patch.object(ss, "scrape_group_schedule", side_effect=schedule):
            return ss.scrape_knf_schedule(notify=False)

    def test_a_completed_run_names_the_feeds_it_could_not_read(self):
        def schedule(slug, *_args):
            if slug == "ft-1-kursas":
                raise RuntimeError("Could not fetch the event feed for ft-1-kursas")
            return self._feed(slug)

        result = self._run(schedule)
        self.assertEqual(result["groups_scraped"], 1)
        run = ScraperRun.objects.get(source="tvarkarasciai.vu.lt")
        self.assertEqual(run.status, "completed")
        self.assertIn("ft-1-kursas", run.error_message)

    def test_a_clean_run_carries_no_note(self):
        self._run(lambda slug, *_args: self._feed(slug))
        self.assertIsNone(ScraperRun.objects.get(source="tvarkarasciai.vu.lt").error_message)

    def test_a_legend_label_the_scraper_does_not_know_is_a_warning_naming_it(self):
        legend = {"#123456": "NAUJAS TIPAS", "#ffc2cc": "EGZAMINAS"}
        with self.assertLogs(ss.logger, "WARNING") as logs:
            self._run(lambda slug, *_args: self._feed(slug, legend))
        warned = "\n".join(logs.output)
        self.assertIn("NAUJAS TIPAS", warned)
        self.assertIn("#123456", warned)
        self.assertNotIn("EGZAMINAS'", warned)

    def test_a_legend_retake_colour_missing_from_the_constant_is_named(self):
        legend = {"#abcdef": "PERLAIKYMAS"}
        with self.assertLogs(ss.logger, "WARNING") as logs:
            self._run(lambda slug, *_args: self._feed(slug, legend))
        self.assertIn("#abcdef", "\n".join(logs.output))
