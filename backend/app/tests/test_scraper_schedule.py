############################################################
#  [*] Tests — schedule_scraper (tvarkarasciai.vu.lt)
#
#  What this module proves about app/scraper/schedule_scraper:
#
#    - the FEED PARSER folds a fortnight of dated FullCalendar
#      events into weekly lesson patterns: weekday + "HH:MM"
#      times, the site's own wall clock kept across an offset,
#      Saturday/Sunday as 5/6, a midnight-crossing lesson
#      keeping its starting day, and the four drop filters
#      (all-day, retake, unparsable, untitled) counted rather
#      than silent
#    - the GROUP NAME collapses to programme + course, the
#      parallel "N grupė" split is dropped, a specialisation
#      is not swallowed by the programme whose words it
#      repeats, and an unparsable name keeps its unique slug
#    - the IMPORT IS IDEMPOTENT: re-running the same timetable
#      inserts nothing, retires nothing and pushes nothing.
#      The anchor semester is RECONCILED (a lesson that left
#      the feed disappears, a moved one does not double), the
#      neighbouring semesters the window clipped are only ever
#      added to through the natural-key index, and a stray
#      label never becomes a picker option
#    - the FAILURE PATHS fail loudly: no group list, no group
#      links, no lesson in any feed, a write that blows up
#      mid-reconciliation — each marks the run 'failed' and
#      answers 502 through the admin trigger, which is
#      admin-only
#
#  Every fetch is driven through `responses` with fixture
#  HTML/JSON authored here; the container has no network, so
#  a test that reached tvarkarasciai.vu.lt would fail anyway.
#  Everything time-dependent runs under time_machine.
############################################################


import json
import logging
import sqlite3
import uuid
from html import escape

import pytest
import responses
import time_machine

import app.notifications.push as push_module
from app.scraper import common as scraper_common
from app.scraper import schedule_scraper as ss


# The clock nearly every whole-run test freezes: a Tuesday in
# the spring term, so the anchor label is "2025-P" and the
# rolling window is 2026-01-27 .. 2026-06-30
SPRING_RUN = "2026-02-10 09:00:00"
SPRING_ANCHOR = "2025-P"

# A summer Monday, where the anchor is still "2025-P" but the
# events in the window's forward half are already "2026-R" —
# a NEWER neighbour, so the purge leaves it alone
SUMMER_RUN = "2026-07-20 09:00:00"




############################################################
# http
############################################################
#
# The `responses` mock every test that fetches runs inside.
# assert_all_requests_are_fired is off on purpose: several
# tests register a feed precisely to prove it is NEVER
# requested (a malformed slug, a spent time budget).
#
# Used by:
#   - every test that reaches scrape_group_list,
#     scrape_group_schedule or scrape_knf_schedule
############################################################

@pytest.fixture
def http():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        yield rsps




############################################################
# _scraper_state
############################################################
#
# The module globals a scrape mutates: the one-run-at-a-time
# lock and common.py's per-process push clock. A test that
# left either set would decide the next test's outcome, so
# both are cleared around every one of them.
#
# Used by:
#   - autouse, every test in this module
############################################################

@pytest.fixture(autouse=True)
def _scraper_state():
    scraper_common._LAST_PUSH.pop("tvarkarasciai.vu.lt", None)

    yield

    if ss._RUN_LOCK.locked():
        ss._RUN_LOCK.release()
    scraper_common._LAST_PUSH.pop("tvarkarasciai.vu.lt", None)




############################################################
# _list_page / _group_block
############################################################
#
# The /knf/list/ markup in the shape scrape_group_list reads:
# a heading followed by a list whose links point at
# "/knf/groups/<slug>/". The link TEXT is deliberately
# useless ("1 Grupė"), exactly as the real page has it — the
# display name has to come from the heading above.
#
# Used by:
#   - the group-list tests and every whole-run test
############################################################

def _list_page(*blocks) -> str:
    return "<html><body><main>" + "".join(blocks) + "</main></body></html>"


def _group_block(heading, slug, link_text="1 Grupė", title_attr=None) -> str:
    title = f' title="{title_attr}"' if title_attr else ""
    head = f"<h3>{heading}</h3>" if heading else ""

    return f'{head}<ul><li><a href="/knf/groups/{slug}/"{title}>{link_text}</a></li></ul>'




############################################################
# _event / _feed
############################################################
#
# One FullCalendar event and the {"events": [...]} envelope
# the feed wraps them in. Extra keys (instructor, location,
# color, subtitle, …) go in as kwargs so each test names only
# the fields it is about.
#
# Used by:
#   - every parser and whole-run test
############################################################

def _event(start, end, title="Programavimas", **extra) -> dict:
    event = {"start": start, "end": end, "title": title}
    event.update(extra)

    return event


def _feed(events) -> str:
    return json.dumps({"events": events})




############################################################
# _popover_title
############################################################
#
# An event title in the site's popover shape: the course as
# the first <a>, and the lecturers/rooms in data-academics /
# data-rooms attributes whose values are HTML-ESCAPED HTML —
# the double lift-unescape-parse the extractors have to do.
#
# Used by:
#   - the teacher/room extraction tests
############################################################

def _popover_title(title, academics=None, rooms=None) -> str:
    markup = f'<a href="/dalykas/1/">{title}</a><span'
    if academics is not None:
        markup += f' data-academics="{escape(academics, quote=True)}"'
    if rooms is not None:
        markup += f' data-rooms="{escape(rooms, quote=True)}"'

    return markup + "></span>"




############################################################
# _serve_list / _serve_feed / _serve_one_group
############################################################
#
# The two URLs the scraper knows, registered on the mock. The
# feed is registered without a query string so `responses`
# matches whatever window the run computes — the window
# itself is asserted separately, off the recorded call.
#
# Used by:
#   - every test that fetches
############################################################

def _serve_list(http, body, status=200, content_type="text/html; charset=utf-8"):
    http.add(responses.GET, ss.GROUP_LIST_URL, body=body, status=status,
             content_type=content_type)


def _serve_feed(http, slug, events=None, body=None, status=200,
                content_type="application/json"):
    http.add(responses.GET, ss.EVENT_URL_TEMPLATE.format(slug=slug),
             body=body if body is not None else _feed(events or []),
             status=status, content_type=content_type)


def _serve_one_group(http, events, slug="isks-1k",
                     heading="Informacijos sistemos ir kibernetinė sauga - 1 kursas"):
    _serve_list(http, _list_page(_group_block(heading, slug)))
    _serve_feed(http, slug, events)




############################################################
# _seed_lesson / _seed_completed_run
############################################################
#
# State a scrape cannot arrange for itself: rows from a
# finished semester, a legacy hand-written label, and an
# EARLIER completed run — without one push_allowed calls the
# import a first backfill and suppresses every notification.
#
# Used by:
#   - the purge tests and the push tests
############################################################

def _seed_lesson(db, semester, group_name="ISKS-1", title="Senas dalykas",
                 teacher="A. Petraitis", room="301", time_start="08:30",
                 time_end="10:00", day_of_week=0):
    db.execute(
        """INSERT INTO schedule_lessons
           (id, title, teacher, room, time_start, time_end, day_of_week, group_name, semester)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (str(uuid.uuid4()), title, teacher, room, time_start, time_end,
         day_of_week, group_name, semester),
    )
    db.commit()


def _seed_completed_run(db, found=8, new=8, started_at="2026-02-10T06:00:00+00:00"):
    db.execute(
        """INSERT INTO scraper_runs
           (id, source, status, articles_found, articles_new, started_at, finished_at)
           VALUES (?, 'tvarkarasciai.vu.lt', 'completed', ?, ?, ?, ?)""",
        (str(uuid.uuid4()), found, new, started_at, started_at),
    )
    db.commit()




############################################################
# _lesson_rows
############################################################
#
# Every stored lesson as a comparable tuple, ordered so a
# whole timetable can be asserted in one equality.
#
# Used by:
#   - the import, reconciliation and purge tests
############################################################

def _lesson_rows(db, semester=None):
    sql = ("SELECT title, teacher, room, time_start, time_end, day_of_week, group_name, semester"
           " FROM schedule_lessons")
    params = []
    if semester:
        sql += " WHERE semester = ?"
        params.append(semester)
    sql += " ORDER BY semester, group_name, day_of_week, time_start, title"

    return [tuple(row) for row in db.execute(sql, params).fetchall()]


def _run_rows(db):
    return db.execute(
        "SELECT status, articles_found, articles_new, error_message, finished_at"
        " FROM scraper_runs WHERE source = 'tvarkarasciai.vu.lt' ORDER BY rowid"
    ).fetchall()




# ==========================================================
# Semester labels
# ==========================================================


def test_august_through_december_dates_get_the_autumn_label():
    from datetime import datetime

    assert ss._get_semester_label(datetime(2025, 8, 1)) == "2025-R"
    assert ss._get_semester_label(datetime(2025, 9, 15)) == "2025-R"
    assert ss._get_semester_label(datetime(2025, 12, 31)) == "2025-R"


def test_january_through_july_dates_belong_to_the_previous_academic_year():
    from datetime import datetime

    assert ss._get_semester_label(datetime(2026, 1, 5)) == "2025-P"
    assert ss._get_semester_label(datetime(2026, 2, 9)) == "2025-P"
    assert ss._get_semester_label(datetime(2026, 6, 20)) == "2025-P"


def test_the_semester_boundary_falls_between_july_and_august():
    from datetime import datetime

    assert ss._get_semester_label(datetime(2026, 7, 31)) == "2025-P"
    assert ss._get_semester_label(datetime(2026, 8, 1)) == "2026-R"




# ==========================================================
# Group display names
# ==========================================================


def test_a_display_name_with_its_course_collapses_to_programme_and_course():
    name = ss._parse_group_display_name(
        "isks-1k-1gr", "Informacijos sistemos ir kibernetinė sauga - 1 kursas")

    assert name == "ISKS-1"


def test_the_parallel_group_number_is_dropped_so_subgroups_share_a_timetable():
    first = ss._parse_group_display_name(
        "isks-1k-1gr", "Informacijos sistemos ir kibernetinė sauga - 1 kursas 1 grupė")
    second = ss._parse_group_display_name(
        "isks-1k-2gr", "Informacijos sistemos ir kibernetinė sauga - 1 kursas 2 grupė")

    assert first == second == "ISKS-1"


def test_a_specialisation_wins_over_the_broader_programme_it_names():
    # The display name carries BOTH "lietuvių filologija ir
    # reklama" and its specialisation; the ordered table has to
    # resolve the specific one or two real timetables merge
    name = ss._parse_group_display_name(
        "lfr-tkr-2k",
        "Lietuvių filologija ir reklama (turinio kūrimas ir rinkodara) - 2 kursas")

    assert name == "LFR-TKR-2"


def test_the_course_is_taken_from_the_slug_when_the_name_has_none():
    name = ss._parse_group_display_name("ev-3k-1gr", "Ekonomika ir vadyba")

    assert name == "EV-3"


def test_a_masters_group_carries_the_m_suffix():
    name = ss._parse_group_display_name(
        "mv-1k", "Meno vadyba, magistrantūros studijos - 1 kursas")

    assert name == "MV-M-1"


def test_an_explicit_english_marker_adds_the_en_suffix():
    name = ss._parse_group_display_name(
        "tvv-1k-en", "Tarptautinio verslo vadyba (in English) - 1 kursas")

    assert name == "TVV-EN-1"


def test_the_lithuanian_english_philology_programme_is_not_tagged_english():
    # "angl" as a bare substring fires on this programme's OWN
    # Lithuanian name — the marker has to be explicit
    name = ss._parse_group_display_name(
        "akuk-2k", "Anglų ir kita užsienio kalba - 2 kursas")

    assert name == "AKUK-2"


def test_the_courseless_subject_pools_keep_their_bare_abbreviation():
    assert ss._parse_group_display_name(
        "bus-dalykai", "Bendrųjų universitetinių studijų dalykai") == "BUS"
    assert ss._parse_group_display_name(
        "isd-dalykai", "Individualiųjų studijų dalykai") == "ISD"


def test_a_course_bearing_programme_without_a_course_falls_back_to_the_slug(caplog):
    with caplog.at_level(logging.WARNING, logger="app.scraper.schedule_scraper"):
        name = ss._parse_group_display_name("ekonomika-vadyba-grupe", "Ekonomika ir vadyba")

    # "EV" would pool every year of the programme into one
    # timetable, so the unique slug is kept instead
    assert name == "ekonomika-vadyba-grupe"
    assert "No course" in caplog.text


def test_an_unknown_programme_keeps_its_slug_capped_at_thirty_chars(caplog):
    slug = "a" * 50

    with caplog.at_level(logging.INFO, logger="app.scraper.schedule_scraper"):
        name = ss._parse_group_display_name(slug, "Visiškai nežinoma programa")

    assert name == "a" * 30
    assert "No programme matched" in caplog.text


def test_a_slug_only_name_still_resolves_through_the_slug_candidate():
    # Nothing in the display name matches, but the de-hyphenated
    # slug does — the second candidate is what saves the group
    name = ss._parse_group_display_name("finansu-analitika-1k", "1 Grupė")

    assert name == "FA-1"




# ==========================================================
# Colour normalisation
# ==========================================================


def test_colour_notations_collapse_to_one_lowercase_hex():
    assert ss._normalise_colour("#FF899D") == "#ff899d"
    assert ss._normalise_colour("  #ff899d ") == "#ff899d"
    assert ss._normalise_colour("rgb(255, 137, 157)") == "#ff899d"
    assert ss._normalise_colour("rgba(255,137,157,0.5)") == "#ff899d"
    assert ss._normalise_colour("#ABC") == "#aabbcc"


def test_an_out_of_range_rgb_channel_is_clamped():
    assert ss._normalise_colour("rgb(999, 0, 0)") == "#ff0000"


def test_an_empty_colour_is_empty():
    assert ss._normalise_colour("") == ""
    assert ss._normalise_colour(None) == ""


def test_an_unknown_colour_is_handed_back_lowercased():
    # Never dropped: the caller counts every colour it sees, and
    # a palette change is only visible in that histogram
    assert ss._normalise_colour("CornflowerBlue") == "cornflowerblue"
    assert ss._normalise_colour("linear-gradient(red, blue)") == "linear-gradient(red, blue)"




# ==========================================================
# Retake labels
# ==========================================================


@pytest.mark.parametrize("field", ["subtitle", "description", "type", "category", "eventType"])
def test_a_retake_is_recognised_from_every_labelled_field(field):
    assert ss._labelled_retake({field: "Egzaminas (PERLAIKYMAS)"}) is True


def test_a_structured_retake_flag_is_honoured():
    assert ss._labelled_retake({"retake": True}) is True
    # Truthy but not True — the check is deliberately identity
    assert ss._labelled_retake({"retake": 1}) is False


def test_an_ordinary_event_is_not_a_retake():
    assert ss._labelled_retake({"subtitle": "Paskaita", "title": "Programavimas"}) is False




# ==========================================================
# Title, teacher and room extraction
# ==========================================================


def test_a_plain_title_is_used_as_is():
    assert ss._extract_title_text("  Programavimas  ") == "Programavimas"
    assert ss._extract_title_text("") == ""


def test_the_first_link_of_a_popover_title_is_the_course():
    markup = _popover_title("Duomenų bazės", academics="J. Jonaitis")

    assert ss._extract_title_text(markup) == "Duomenų bazės"


def test_a_popover_without_links_falls_back_to_its_first_text_line():
    assert ss._extract_title_text("<div>Programavimas\nII kursas</div>") == "Programavimas"


def test_every_lecturer_of_a_co_taught_lecture_is_kept_and_sorted():
    # Duplicates collapse, blanks are dropped and the order is
    # stable — the teacher column is part of the natural key, so
    # an unstable order would be a new lesson on every run
    academics = ('<a href="#">B. Jonaitis</a><a href="#">A. Petraitis</a>'
                 '<a href="#">B. Jonaitis</a><a href="#"></a>')
    markup = _popover_title("Programavimas", academics=academics)

    assert ss._extract_teacher_from_html(markup) == "A. Petraitis, B. Jonaitis"


def test_a_linkless_lecturer_popover_loses_its_label():
    markup = _popover_title("Programavimas", academics="Dėstytojai: J. Jonaitis")

    assert ss._extract_teacher_from_html(markup) == "J. Jonaitis"


def test_a_missing_academics_attribute_yields_no_teacher():
    assert ss._extract_teacher_from_html("<a href='#'>Programavimas</a>") == ""


def test_both_rooms_of_a_split_lecture_are_kept():
    rooms = '<a href="#">305</a><a href="#">204</a>'
    markup = _popover_title("Programavimas", rooms=rooms)

    assert ss._extract_room_from_html(markup) == "204, 305"


def test_a_linkless_room_popover_loses_its_label():
    markup = _popover_title("Programavimas", rooms="Patalpos: 301")

    assert ss._extract_room_from_html(markup) == "301"


def test_a_missing_rooms_attribute_yields_no_room():
    assert ss._extract_room_from_html("<a href='#'>Programavimas</a>") == ""




# ==========================================================
# scrape_group_list
# ==========================================================


def test_the_group_list_reads_slug_and_heading_for_every_group_link(http):
    _serve_list(http, _list_page(
        _group_block("Informacijos sistemos ir kibernetinė sauga - 1 kursas", "isks-1k-1gr"),
        _group_block("Ekonomika ir vadyba - 2 kursas", "ev-2k-1gr"),
    ))

    groups = ss.scrape_group_list()

    assert groups == [
        {"slug": "isks-1k-1gr",
         "display_name": "Informacijos sistemos ir kibernetinė sauga - 1 kursas"},
        {"slug": "ev-2k-1gr", "display_name": "Ekonomika ir vadyba - 2 kursas"},
    ]


def test_a_repeated_group_link_is_listed_once(http):
    _serve_list(http, _list_page(
        _group_block("Ekonomika ir vadyba - 2 kursas", "ev-2k-1gr"),
        '<a href="/knf/groups/ev-2k-1gr/">Tvarkaraštis</a>',
    ))

    groups = ss.scrape_group_list()

    assert [g["slug"] for g in groups] == ["ev-2k-1gr"]


def test_a_malformed_group_slug_is_never_requested(http, caplog):
    # The slug is interpolated into the feed URL — one that is
    # not a plain path token is dropped here, not encoded
    _serve_list(http, _list_page(
        _group_block("Ekonomika ir vadyba - 2 kursas", "ev-2k-1gr"),
        _group_block("Bloga grupė", "blogas.slug"),
        _group_block("Per ilga", "x" * 90),
    ))

    with caplog.at_level(logging.WARNING, logger="app.scraper.schedule_scraper"):
        groups = ss.scrape_group_list()

    assert [g["slug"] for g in groups] == ["ev-2k-1gr"]
    assert "malformed group slug" in caplog.text


def test_a_link_without_a_heading_falls_back_to_its_title_attribute(http):
    _serve_list(http, _list_page(
        _group_block(None, "tvv-1k", title_attr="Tarptautinio verslo vadyba - 1 kursas"),
    ))

    assert ss.scrape_group_list() == [
        {"slug": "tvv-1k", "display_name": "Tarptautinio verslo vadyba - 1 kursas"},
    ]


def test_a_link_with_neither_heading_nor_title_falls_back_to_the_slug(http):
    # Also the "climbed five ancestors and ran out of parents"
    # path — there is no heading anywhere above the link
    _serve_list(http, '<a href="/knf/groups/finansu-analitika-1k/">Grupė</a>')

    assert ss.scrape_group_list() == [
        {"slug": "finansu-analitika-1k", "display_name": "finansu analitika 1k"},
    ]


def test_the_course_is_appended_from_the_slug_when_the_heading_has_none(http):
    _serve_list(http, _list_page(_group_block("Ekonomika ir vadyba", "ev-3k-1gr")))

    assert ss.scrape_group_list() == [
        {"slug": "ev-3k-1gr", "display_name": "Ekonomika ir vadyba - 3 kursas"},
    ]


def test_hrefs_that_are_not_group_pages_are_ignored(http):
    _serve_list(http, _list_page(
        '<a href="/knf/list/">Atgal</a>',
        '<a href="/knf/groups/ev-2k/subpage/">Gilyn</a>',
        '<a href="https://vu.lt/">VU</a>',
        _group_block("Ekonomika ir vadyba - 2 kursas", "ev-2k-1gr"),
    ))

    assert [g["slug"] for g in ss.scrape_group_list()] == ["ev-2k-1gr"]


def test_a_failed_group_list_fetch_raises(http):
    _serve_list(http, "", status=503)

    with pytest.raises(RuntimeError, match="Could not fetch the group list"):
        ss.scrape_group_list()




# ==========================================================
# scrape_group_schedule — parsing one feed
# ==========================================================


def test_a_dated_event_becomes_one_weekly_lesson(http):
    _serve_feed(http, "isks-1k", [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00",
               instructor="A. Petraitis", location="301"),
    ])

    lessons, stats = ss.scrape_group_schedule(
        "isks-1k", "Informacijos sistemos ir kibernetinė sauga - 1 kursas",
        "2026-01-27", "2026-06-30")

    assert lessons == [{
        "title": "Programavimas",
        "teacher": "A. Petraitis",
        "room": "301",
        "time_start": "08:30",
        "time_end": "10:00",
        "day_of_week": 0,
        "group_name": "ISKS-1",
        "semester": "2025-P",
    }]
    assert stats["events"] == 1
    assert (stats["all_day"], stats["retakes"], stats["unparsable"], stats["untitled"]) == (0, 0, 0, 0)


def test_lesson_times_keep_the_sites_wall_clock_across_an_offset(http):
    # A timetable is read in local time: 08:30+02:00 is the
    # 08:30 on the wall, NOT 06:30 UTC
    _serve_feed(http, "isks-1k", [
        _event("2026-02-09T08:30:00+02:00", "2026-02-09T10:00:00+02:00"),
    ])

    lessons, _ = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                          "2026-01-27", "2026-06-30")

    assert (lessons[0]["time_start"], lessons[0]["time_end"]) == ("08:30", "10:00")
    assert lessons[0]["day_of_week"] == 0


def test_a_lesson_crossing_midnight_keeps_its_starting_weekday(http):
    _serve_feed(http, "isks-1k", [
        _event("2026-02-13T23:00:00", "2026-02-14T00:30:00", title="Naktinė konsultacija"),
    ])

    lessons, _ = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                          "2026-01-27", "2026-06-30")

    assert lessons[0]["day_of_week"] == 4          # Friday, not Saturday
    assert lessons[0]["time_start"] == "23:00"
    assert lessons[0]["time_end"] == "00:30"


def test_a_weekend_lesson_keeps_its_weekend_weekday(http):
    _serve_feed(http, "isks-1k", [
        _event("2026-02-14T09:00:00", "2026-02-14T10:30:00", title="Šeštadienio paskaita"),
        _event("2026-02-15T09:00:00", "2026-02-15T10:30:00", title="Sekmadienio paskaita"),
    ])

    lessons, _ = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                          "2026-01-27", "2026-06-30")

    assert [lesson["day_of_week"] for lesson in lessons] == [5, 6]


def test_the_same_lecture_every_week_collapses_to_one_lesson(http):
    _serve_feed(http, "isks-1k", [
        _event(f"2026-02-{day}T08:30:00", f"2026-02-{day}T10:00:00",
               instructor="A. Petraitis", location="301")
        for day in ("09", "16", "23")
    ])

    lessons, stats = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                              "2026-01-27", "2026-06-30")

    assert len(lessons) == 1
    assert stats["events"] == 3


def test_a_one_week_room_change_survives_as_its_own_lesson(http):
    _serve_feed(http, "isks-1k", [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00", location="301"),
        _event("2026-02-16T08:30:00", "2026-02-16T10:00:00", location="204"),
    ])

    lessons, _ = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                          "2026-01-27", "2026-06-30")

    assert sorted(lesson["room"] for lesson in lessons) == ["204", "301"]


def test_all_day_events_are_dropped_and_counted(http):
    _serve_feed(http, "isks-1k", [
        _event("2026-02-16", "2026-02-17", title="Vasario 16-oji"),
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00"),
    ])

    lessons, stats = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                              "2026-01-27", "2026-06-30")

    assert len(lessons) == 1
    assert stats["all_day"] == 1


def test_a_retake_coloured_event_is_dropped(http):
    _serve_feed(http, "isks-1k", [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00", color="rgb(255, 137, 157)"),
        _event("2026-02-10T08:30:00", "2026-02-10T10:00:00", color="#3788D8"),
    ])

    lessons, stats = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                              "2026-01-27", "2026-06-30")

    assert len(lessons) == 1
    assert stats["retakes"] == 1
    assert stats["colours"] == {"#ff899d": 1, "#3788d8": 1}


def test_a_labelled_retake_in_an_unknown_colour_is_dropped_with_a_warning(http, caplog):
    # The label survives a palette change; the WARNING is what
    # makes the palette change visible before exams get imported
    # as weekly lessons
    _serve_feed(http, "isks-1k", [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00",
               color="#123456", subtitle="Egzaminas (PERLAIKYMAS)"),
    ])

    with caplog.at_level(logging.WARNING, logger="app.scraper.schedule_scraper"):
        lessons, stats = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                                  "2026-01-27", "2026-06-30")

    assert lessons == []
    assert stats["retakes"] == 1
    assert "the palette has probably changed" in caplog.text


def test_a_labelled_retake_in_the_known_colour_warns_about_nothing(http, caplog):
    _serve_feed(http, "isks-1k", [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00",
               color="#FF899D", subtitle="PERLAIKYMAS"),
    ])

    with caplog.at_level(logging.WARNING, logger="app.scraper.schedule_scraper"):
        lessons, stats = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                                  "2026-01-27", "2026-06-30")

    assert (lessons, stats["retakes"]) == ([], 1)
    assert "palette" not in caplog.text


def test_an_event_with_an_unparsable_end_is_dropped_and_counted(http):
    _serve_feed(http, "isks-1k", [
        _event("2026-02-09T08:30:00", ""),
        _event("2026-02-10T08:30:00", "vakar"),
    ])

    lessons, stats = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                              "2026-01-27", "2026-06-30")

    assert lessons == []
    assert stats["unparsable"] == 2


def test_an_event_with_a_null_end_is_dropped_and_counted(http):
    _serve_feed(http, "isks-1k", [_event("2026-02-09T08:30:00", None)])

    lessons, stats = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                              "2026-01-27", "2026-06-30")

    assert (lessons, stats["unparsable"]) == ([], 1)


def test_an_untitled_event_is_dropped_and_counted(http):
    _serve_feed(http, "isks-1k", [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00", title="   "),
    ])

    lessons, stats = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                              "2026-01-27", "2026-06-30")

    assert (lessons, stats["untitled"]) == ([], 1)


def test_a_null_start_drops_only_that_event_not_the_whole_group(http):
    # A null start is not an absent one: `"T" not in None`
    # raises, and that used to take every lesson of the group
    # down with it
    _serve_feed(http, "isks-1k", [
        _event(None, "2026-02-09T10:00:00", title="Sugedęs įrašas"),
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00", location="301"),
    ])

    lessons, stats = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                              "2026-01-27", "2026-06-30")

    assert [lesson["title"] for lesson in lessons] == ["Programavimas"]
    assert stats["all_day"] == 1


def test_a_null_title_drops_only_that_event_not_the_whole_group(http):
    _serve_feed(http, "isks-1k", [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00", title=None),
        _event("2026-02-10T08:30:00", "2026-02-10T10:00:00", title="Programavimas"),
    ])

    lessons, stats = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                              "2026-01-27", "2026-06-30")

    assert [lesson["title"] for lesson in lessons] == ["Programavimas"]
    assert stats["untitled"] == 1


def test_the_top_level_instructor_and_location_win_over_the_popover(http):
    markup = _popover_title("Programavimas",
                            academics='<a href="#">Popover dėstytojas</a>',
                            rooms='<a href="#">999</a>')
    _serve_feed(http, "isks-1k", [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00", title=markup,
               instructor="A. Petraitis", location="301"),
    ])

    lessons, _ = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                          "2026-01-27", "2026-06-30")

    assert (lessons[0]["teacher"], lessons[0]["room"]) == ("A. Petraitis", "301")


def test_the_popover_fills_in_what_the_top_level_fields_leave_empty(http):
    markup = _popover_title("Programavimas",
                            academics='<a href="#">B. Jonaitis</a><a href="#">A. Petraitis</a>',
                            rooms='<a href="#">204</a>')
    _serve_feed(http, "isks-1k", [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00", title=markup),
    ])

    lessons, _ = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                          "2026-01-27", "2026-06-30")

    assert lessons[0]["teacher"] == "A. Petraitis, B. Jonaitis"
    assert lessons[0]["room"] == "204"
    assert lessons[0]["title"] == "Programavimas"


def test_a_trailing_comma_is_trimmed_from_the_lecturer(http):
    _serve_feed(http, "isks-1k", [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00", instructor="  A. Petraitis,  "),
    ])

    lessons, _ = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                          "2026-01-27", "2026-06-30")

    assert lessons[0]["teacher"] == "A. Petraitis"


def test_a_feed_served_as_html_is_still_parsed_as_json(http):
    # tvarkarasciai.vu.lt has answered this endpoint with
    # text/html before now; the body is JSON either way
    _serve_feed(http, "isks-1k",
                [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")],
                content_type="text/html")

    lessons, _ = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                          "2026-01-27", "2026-06-30")

    assert len(lessons) == 1


def test_a_feed_without_an_events_key_yields_nothing(http):
    _serve_feed(http, "isks-1k", body=json.dumps({"ok": True}))

    lessons, stats = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                              "2026-01-27", "2026-06-30")

    assert (lessons, stats["events"]) == ([], 0)


def test_a_malformed_slug_is_refused_before_any_request(http):
    with pytest.raises(ValueError, match="malformed group slug"):
        ss.scrape_group_schedule("../../etc/passwd", "Ekonomika ir vadyba - 1 kursas",
                                 "2026-01-27", "2026-06-30")

    assert len(http.calls) == 0


def test_an_empty_slug_is_refused(http):
    with pytest.raises(ValueError):
        ss.scrape_group_schedule("", "Ekonomika ir vadyba - 1 kursas",
                                 "2026-01-27", "2026-06-30")


def test_a_failed_event_feed_raises(http):
    _serve_feed(http, "isks-1k", status=500)

    with pytest.raises(RuntimeError, match="Could not fetch the event feed"):
        ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                 "2026-01-27", "2026-06-30")


def test_a_feed_that_is_not_json_raises(http):
    _serve_feed(http, "isks-1k", body="<html>bandykite vėliau</html>", content_type="text/html")

    with pytest.raises(json.JSONDecodeError):
        ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                 "2026-01-27", "2026-06-30")




# ==========================================================
# scrape_knf_schedule — the whole import
# ==========================================================


def test_a_run_imports_every_scraped_lesson_and_completes_its_run_row(app, db, http):
    _serve_one_group(http, [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00",
               instructor="A. Petraitis", location="301"),
        _event("2026-02-10T10:15:00", "2026-02-10T11:45:00", title="Duomenų bazės",
               instructor="B. Jonaitis", location="204"),
    ])

    with time_machine.travel(SPRING_RUN, tick=False):
        result = ss.scrape_knf_schedule(notify=False)

    assert result["groups_scraped"] == 1
    assert result["lessons_found"] == 2
    assert result["lessons_new"] == 2
    assert result["dropped"] == {"all_day": 0, "retakes": 0, "unparsable": 0, "untitled": 0}
    assert "error" not in result

    assert _lesson_rows(db) == [
        ("Programavimas", "A. Petraitis", "301", "08:30", "10:00", 0, "ISKS-1", SPRING_ANCHOR),
        ("Duomenų bazės", "B. Jonaitis", "204", "10:15", "11:45", 1, "ISKS-1", SPRING_ANCHOR),
    ]

    runs = _run_rows(db)
    assert len(runs) == 1
    assert runs[0]["status"] == "completed"
    assert (runs[0]["articles_found"], runs[0]["articles_new"]) == (2, 2)
    assert runs[0]["finished_at"] is not None


def test_re_running_the_same_timetable_inserts_nothing_new(app, db, http):
    events = [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00",
               instructor="A. Petraitis", location="301"),
        _event("2026-02-10T10:15:00", "2026-02-10T11:45:00", title="Duomenų bazės",
               instructor="B. Jonaitis", location="204"),
    ]
    for _ in range(2):
        _serve_one_group(http, events)

    with time_machine.travel(SPRING_RUN, tick=False):
        first = ss.scrape_knf_schedule(notify=False)
        second = ss.scrape_knf_schedule(notify=False)

    assert first["lessons_new"] == 2
    assert second["lessons_new"] == 0
    assert second["lessons_found"] == 2
    assert len(_lesson_rows(db)) == 2


def test_a_lesson_that_left_the_feed_is_retired_from_the_anchor_semester(app, db, http):
    _serve_one_group(http, [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00", location="301"),
        _event("2026-02-10T10:15:00", "2026-02-10T11:45:00", title="Atšaukta", location="204"),
    ])
    _serve_one_group(http, [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00", location="301"),
    ])

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule(notify=False)
        second = ss.scrape_knf_schedule(notify=False)

    assert second["lessons_new"] == 0
    assert [row[0] for row in _lesson_rows(db)] == ["Programavimas"]


def test_a_moved_lecture_replaces_the_old_slot_instead_of_doubling_it(app, db, http):
    _serve_one_group(http, [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00", location="301"),
    ])
    _serve_one_group(http, [
        _event("2026-02-09T10:15:00", "2026-02-09T11:45:00", location="301"),
    ])

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule(notify=False)
        second = ss.scrape_knf_schedule(notify=False)

    assert second["lessons_new"] == 1
    assert _lesson_rows(db) == [
        ("Programavimas", "", "301", "10:15", "11:45", 0, "ISKS-1", SPRING_ANCHOR),
    ]


def test_a_group_that_fails_to_scrape_keeps_the_rows_it_already_had(app, db, http):
    both = _list_page(
        _group_block("Informacijos sistemos ir kibernetinė sauga - 1 kursas", "isks-1k"),
        _group_block("Ekonomika ir vadyba - 2 kursas", "ev-2k"),
    )

    _serve_list(http, both)
    _serve_feed(http, "isks-1k", [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])
    _serve_feed(http, "ev-2k", [_event("2026-02-10T08:30:00", "2026-02-10T10:00:00",
                                       title="Mikroekonomika")])

    _serve_list(http, both)
    _serve_feed(http, "isks-1k", [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])
    _serve_feed(http, "ev-2k", status=500)

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule(notify=False)
        second = ss.scrape_knf_schedule(notify=False)

    # The broken group is skipped, never emptied
    assert second["groups_scraped"] == 1
    assert sorted(row[6] for row in _lesson_rows(db)) == ["EV-2", "ISKS-1"]


def test_a_neighbouring_semester_is_only_added_to(app, db, http):
    # In July the anchor is still the spring label, while the
    # window's forward half already carries autumn dates — those
    # partitions are added to, never rewritten
    autumn = [
        _event(f"2026-09-1{day}T08:30:00", f"2026-09-1{day}T10:00:00", title=f"Dalykas {day}")
        for day in range(4, 9)
    ]
    for _ in range(2):
        _serve_one_group(http, autumn)

    with time_machine.travel(SUMMER_RUN, tick=False):
        first = ss.scrape_knf_schedule(notify=False)
        second = ss.scrape_knf_schedule(notify=False)

    assert first["lessons_new"] == 5
    assert second["lessons_new"] == 0
    assert len(_lesson_rows(db, semester="2026-R")) == 5
    assert _lesson_rows(db, semester=SPRING_ANCHOR) == []


def test_a_stray_semester_label_is_never_stored(app, db, http, caplog):
    # Five spring lessons and two misdated autumn ones: the
    # autumn label is under MIN_SEMESTER_LESSONS and must not
    # become an option in the mobile picker
    events = [
        _event(f"2026-02-{day}T08:30:00", f"2026-02-{day}T10:00:00", title=f"Dalykas {day}")
        for day in ("09", "10", "11", "12", "13")
    ] + [
        _event("2026-09-14T08:30:00", "2026-09-14T10:00:00", title="Klaidinga data"),
        _event("2026-09-15T08:30:00", "2026-09-15T10:00:00", title="Kita klaida"),
    ]
    _serve_one_group(http, events)

    with caplog.at_level(logging.INFO, logger="app.scraper.schedule_scraper"):
        with time_machine.travel(SPRING_RUN, tick=False):
            result = ss.scrape_knf_schedule(notify=False)

    assert result["lessons_found"] == 7
    assert result["lessons_new"] == 5
    assert {row[7] for row in _lesson_rows(db)} == {SPRING_ANCHOR}
    assert "Dropping stray semester label 2026-R" in caplog.text


def test_a_finished_semester_is_retired_once_the_anchor_has_rows(app, db, http):
    _seed_lesson(db, "2024-R", title="Sena paskaita")
    _seed_lesson(db, "2026-R", title="Būsima paskaita")
    _serve_one_group(http, [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule(notify=False)

    stored = {row[7] for row in _lesson_rows(db)}
    assert "2024-R" not in stored
    # Newer than the anchor, so it is not a finished semester
    assert stored == {SPRING_ANCHOR, "2026-R"}


def test_a_semester_label_this_scraper_never_wrote_is_left_alone(app, db, http):
    _seed_lesson(db, "2025-pavasaris", title="Rankinis įrašas")
    _serve_one_group(http, [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule(notify=False)

    assert "2025-pavasaris" in {row[7] for row in _lesson_rows(db)}


def test_nothing_is_retired_when_the_anchor_semester_harvested_nothing(app, db, http):
    # An anchor with no rows means the purge never runs — a
    # scrape that fetched only autumn must not empty the app
    _seed_lesson(db, "2024-R", title="Sena paskaita")
    autumn = [
        _event(f"2026-09-1{day}T08:30:00", f"2026-09-1{day}T10:00:00", title=f"Dalykas {day}")
        for day in range(4, 9)
    ]
    _serve_one_group(http, autumn)

    with time_machine.travel(SUMMER_RUN, tick=False):
        ss.scrape_knf_schedule(notify=False)

    assert "2024-R" in {row[7] for row in _lesson_rows(db)}


def test_the_run_reports_and_logs_everything_its_filters_dropped(app, http, caplog):
    _serve_one_group(http, [
        _event("2026-02-16", "2026-02-17", title="Vasario 16-oji"),
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00", title="Perlaikymas",
               color="#FF899D"),
        _event("2026-02-10T08:30:00", None, title="Sugedęs įrašas"),
        _event("2026-02-11T08:30:00", "2026-02-11T10:00:00", title="   "),
        _event("2026-02-12T08:30:00", "2026-02-12T10:00:00", color="rgb(55, 136, 216)"),
    ])

    with caplog.at_level(logging.INFO, logger="app.scraper.schedule_scraper"):
        with time_machine.travel(SPRING_RUN, tick=False):
            result = ss.scrape_knf_schedule(notify=False)

    assert result["lessons_found"] == 1
    assert result["dropped"] == {"all_day": 1, "retakes": 1, "unparsable": 1, "untitled": 1}
    # The run-level colour histogram is the only place a retake
    # filter that quietly stopped matching becomes visible
    assert "'#ff899d': 1" in caplog.text
    assert "'#3788d8': 1" in caplog.text


def test_the_window_is_two_weeks_back_and_twenty_forward(app, http):
    _serve_one_group(http, [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule(notify=False)

    feed_url = http.calls[1].request.url
    assert "start=2026-01-27" in feed_url
    assert "end=2026-06-30" in feed_url


def test_a_shorter_forward_window_is_honoured(app, http):
    _serve_one_group(http, [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule(forward_weeks=1, notify=False)

    feed_url = http.calls[1].request.url
    assert "start=2026-01-27" in feed_url
    assert "end=2026-02-17" in feed_url




# ==========================================================
# scrape_knf_schedule — failure paths
# ==========================================================


def test_an_empty_group_list_fails_the_run(app, db, http):
    # A list page that downloaded and held no group link is a
    # template change, not an empty faculty
    _serve_list(http, _list_page("<p>Nieko nerasta</p>"))

    with time_machine.travel(SPRING_RUN, tick=False):
        result = ss.scrape_knf_schedule(notify=False)

    assert result["groups_scraped"] == 0
    assert "markup has probably changed" in result["error"]
    assert _run_rows(db)[0]["status"] == "failed"
    assert _run_rows(db)[0]["finished_at"] is not None


def test_a_failed_group_list_fetch_fails_the_run(app, db, http):
    _serve_list(http, "", status=503)

    with time_machine.travel(SPRING_RUN, tick=False):
        result = ss.scrape_knf_schedule(notify=False)

    assert "Could not fetch the group list" in result["error"]
    assert result["runId"]
    assert _run_rows(db)[0]["status"] == "failed"


def test_group_feeds_without_a_single_lesson_fail_the_run(app, db, http):
    # Every group answered and not one lesson came out — the
    # feed shape changed, and nothing may be reconciled against
    # an empty scrape
    _seed_lesson(db, SPRING_ANCHOR, title="Turi išlikti")
    _serve_one_group(http, [])

    with time_machine.travel(SPRING_RUN, tick=False):
        result = ss.scrape_knf_schedule(notify=False)

    assert result["groups_scraped"] == 1
    assert "feed shape has probably changed" in result["error"]
    assert [row[0] for row in _lesson_rows(db)] == ["Turi išlikti"]
    assert _run_rows(db)[0]["status"] == "failed"


def test_a_second_run_steps_aside_while_the_lock_is_held(app, http):
    ss._RUN_LOCK.acquire()
    try:
        result = ss.scrape_knf_schedule(notify=False)
    finally:
        ss._RUN_LOCK.release()

    assert result == {"groups_scraped": 0, "lessons_found": 0, "lessons_new": 0, "skipped": True}
    assert len(http.calls) == 0


def test_the_wall_clock_budget_stops_the_run_before_the_first_feed(app, db, http, monkeypatch):
    monkeypatch.setattr(ss, "RUN_BUDGET_SECONDS", -1)
    _serve_one_group(http, [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])

    with time_machine.travel(SPRING_RUN, tick=False):
        result = ss.scrape_knf_schedule(notify=False)

    assert result["groups_scraped"] == 0
    assert result["lessons_found"] == 0
    # The list page was fetched; not one group feed was
    assert len(http.calls) == 1
    assert _run_rows(db)[0]["status"] == "completed"


def test_a_failure_in_the_write_phase_rolls_back_and_marks_the_run_failed(app, db, http, monkeypatch):
    def _explode(*args, **kwargs):
        raise RuntimeError("disko klaida")

    monkeypatch.setattr(ss, "_reconcile_partition", _explode)
    _serve_one_group(http, [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])

    with time_machine.travel(SPRING_RUN, tick=False):
        result = ss.scrape_knf_schedule(notify=False)

    assert result["error"] == "disko klaida"
    assert result["lessons_new"] == 0
    assert _lesson_rows(db) == []
    assert _run_rows(db)[0]["status"] == "failed"
    assert _run_rows(db)[0]["error_message"] == "disko klaida"


def test_a_rollback_that_itself_fails_still_marks_the_run_failed(app, db, http, monkeypatch):
    # The connection is exactly what may have broken, so the
    # rollback is allowed to fail without swallowing the run row
    real_get_db = ss.get_db

    class _RollbackBreaks:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def rollback(self):
            raise sqlite3.OperationalError("cannot rollback")

    monkeypatch.setattr(ss, "get_db", lambda: _RollbackBreaks(real_get_db()))

    def _explode(*args, **kwargs):
        raise RuntimeError("rašymo klaida")

    monkeypatch.setattr(ss, "_reconcile_partition", _explode)
    _serve_one_group(http, [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])

    with time_machine.travel(SPRING_RUN, tick=False):
        result = ss.scrape_knf_schedule(notify=False)

    assert result["error"] == "rašymo klaida"
    assert _run_rows(db)[0]["status"] == "failed"




# ==========================================================
# Push notifications
# ==========================================================


############################################################
# _recorded_push
############################################################
#
# notify_channel is imported lazily inside the run, so the
# module attribute is what a test has to replace. Returns the
# list the calls land in.
#
# Used by:
#   - the push tests below
############################################################

def _recorded_push(monkeypatch, boom=False):
    calls = []

    def _fake(channel, title, body, data=None, title_en=None, body_en=None, **kwargs):
        calls.append({"channel": channel, "title": title, "body": body, "data": data,
                      "title_en": title_en, "body_en": body_en})
        if boom:
            raise RuntimeError("push nepavyko")
        return 1

    monkeypatch.setattr(push_module, "notify_channel", _fake)

    return calls


def test_new_lessons_push_to_the_schedule_channel(app, db, http, monkeypatch):
    calls = _recorded_push(monkeypatch)
    _seed_completed_run(db)
    _serve_one_group(http, [
        _event(f"2026-02-{day}T08:30:00", f"2026-02-{day}T10:00:00", title=f"Dalykas {day}")
        for day in ("09", "10", "11")
    ])

    with time_machine.travel(SPRING_RUN, tick=False):
        result = ss.scrape_knf_schedule()

    assert result["lessons_new"] == 3
    assert len(calls) == 1
    assert calls[0]["channel"] == "schedule"
    assert calls[0]["data"] == {"type": "schedule_update", "newLessons": 3}
    assert calls[0]["body"] == "3 nauji įrašai tvarkaraštyje"
    assert calls[0]["body_en"] == "3 new timetable entries"


def test_a_single_new_lesson_gets_the_singular_lithuanian_body(app, db, http, monkeypatch):
    calls = _recorded_push(monkeypatch)
    _seed_completed_run(db)
    _serve_one_group(http, [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule()

    assert calls[0]["body"] == "Naujas įrašas tvarkaraštyje"
    assert calls[0]["body_en"] == "New timetable entry"


def test_the_admin_trigger_never_pushes(app, db, http, monkeypatch):
    calls = _recorded_push(monkeypatch)
    _seed_completed_run(db)
    _serve_one_group(http, [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule(notify=False)

    assert calls == []


def test_a_first_import_does_not_push(app, db, http, monkeypatch):
    # No earlier completed run for this source: whatever the
    # count, it is a backfill and not news
    calls = _recorded_push(monkeypatch)
    _serve_one_group(http, [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule()

    assert calls == []


def test_an_unchanged_timetable_pushes_nothing(app, db, http, monkeypatch):
    calls = _recorded_push(monkeypatch)
    _seed_completed_run(db)
    events = [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")]
    for _ in range(2):
        _serve_one_group(http, events)

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule()
        scraper_common._LAST_PUSH.pop("tvarkarasciai.vu.lt", None)
        second = ss.scrape_knf_schedule()

    assert second["lessons_new"] == 0
    assert len(calls) == 1


def test_a_push_failure_does_not_fail_the_run(app, db, http, monkeypatch, caplog):
    _recorded_push(monkeypatch, boom=True)
    _seed_completed_run(db)
    _serve_one_group(http, [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])

    with caplog.at_level(logging.ERROR, logger="app.scraper.schedule_scraper"):
        with time_machine.travel(SPRING_RUN, tick=False):
            result = ss.scrape_knf_schedule()

    assert "error" not in result
    assert result["lessons_new"] == 1
    assert _run_rows(db)[0]["status"] == "completed"
    assert "Failed to send push notification" in caplog.text




# ==========================================================
# The write helpers
# ==========================================================


def _lesson(title="Programavimas", teacher="A. Petraitis", room="301",
            time_start="08:30", time_end="10:00", day_of_week=0,
            group_name="ISKS-1", semester="2025-P"):
    return {"title": title, "teacher": teacher, "room": room, "time_start": time_start,
            "time_end": time_end, "day_of_week": day_of_week,
            "group_name": group_name, "semester": semester}


def test_insert_lessons_reports_only_the_rows_it_actually_added(app, db):
    lessons = [_lesson(), _lesson(title="Duomenų bazės", day_of_week=1)]

    assert ss._insert_lessons(db, lessons) == 2
    db.commit()
    # The natural-key index absorbs the repeat instead of a
    # SELECT-per-candidate table scan
    assert ss._insert_lessons(db, lessons) == 0
    db.commit()

    assert len(_lesson_rows(db)) == 2


def test_a_lesson_without_a_lecturer_or_room_still_dedupes(app, db):
    # Empty strings, never NULL: SQLite treats NULLs in a unique
    # index as distinct, so a NULL teacher would insert a fresh
    # duplicate row on every single run
    lessons = [_lesson(teacher="", room="")]

    assert ss._insert_lessons(db, lessons) == 1
    assert ss._insert_lessons(db, lessons) == 0
    db.commit()

    assert len(_lesson_rows(db)) == 1


def test_a_lesson_the_feed_gives_no_lecturer_for_does_not_duplicate_on_the_next_run(app, db, http):
    # The feed says "instructor": null / "location": null. Those
    # must reach the natural key as "" or the row is re-inserted
    # every six hours forever
    autumn = [
        _event(f"2026-09-1{day}T08:30:00", f"2026-09-1{day}T10:00:00",
               title=f"Dalykas {day}", instructor=None, location=None)
        for day in range(4, 9)
    ]
    for _ in range(2):
        _serve_one_group(http, autumn)

    with time_machine.travel(SUMMER_RUN, tick=False):
        ss.scrape_knf_schedule(notify=False)
        second = ss.scrape_knf_schedule(notify=False)

    assert second["lessons_new"] == 0
    assert len(_lesson_rows(db, semester="2026-R")) == 5


def test_reconcile_reports_added_and_removed(app, db):
    ss._insert_lessons(db, [_lesson(), _lesson(title="Atšaukta", day_of_week=1)])
    db.commit()

    added, removed = ss._reconcile_partition(db, "ISKS-1", "2025-P", [
        _lesson(),
        _lesson(title="Nauja", day_of_week=2),
    ])
    db.commit()

    assert (added, removed) == (1, 1)
    assert sorted(row[0] for row in _lesson_rows(db)) == ["Nauja", "Programavimas"]


def test_reconcile_leaves_an_unchanged_partition_untouched(app, db):
    lessons = [_lesson()]
    ss._insert_lessons(db, lessons)
    db.commit()
    row_id = db.execute("SELECT id FROM schedule_lessons").fetchone()[0]

    assert ss._reconcile_partition(db, "ISKS-1", "2025-P", lessons) == (0, 0)
    db.commit()

    # Not deleted and re-inserted — the same row survived
    assert db.execute("SELECT id FROM schedule_lessons").fetchone()[0] == row_id


def test_reconcile_only_touches_its_own_partition(app, db):
    ss._insert_lessons(db, [_lesson(), _lesson(group_name="EV-2"),
                            _lesson(semester="2026-R")])
    db.commit()

    ss._reconcile_partition(db, "ISKS-1", "2025-P", [])
    db.commit()

    assert sorted((row[6], row[7]) for row in _lesson_rows(db)) == [
        ("EV-2", "2025-P"), ("ISKS-1", "2026-R"),
    ]


def test_semester_key_orders_autumn_before_the_spring_that_follows():
    # Plain text sorting is wrong here: "2025-P" is spring 2026
    # and comes AFTER autumn 2025
    assert ss._semester_key("2025-R") < ss._semester_key("2025-P")
    assert ss._semester_key("2025-P") < ss._semester_key("2026-R")


def test_a_label_this_scraper_never_wrote_has_no_semester_key():
    assert ss._semester_key("2025-pavasaris") is None
    assert ss._semester_key("") is None
    assert ss._semester_key(None) is None
    assert ss._semester_key("25-R") is None


def test_purging_with_an_unparsable_anchor_deletes_nothing(app, db):
    ss._insert_lessons(db, [_lesson(semester="2020-R")])
    db.commit()

    ss._purge_old_semesters(db, "rudens semestras")

    assert len(_lesson_rows(db)) == 1




# ==========================================================
# The admin trigger — POST /api/scraper/schedule
# ==========================================================


def test_a_guest_cannot_trigger_the_schedule_scrape(client):
    response = client.post("/api/scraper/schedule")

    assert response.status_code == 401


def test_a_student_cannot_trigger_the_schedule_scrape(client, actor):
    _user, headers = actor

    response = client.post("/api/scraper/schedule", headers=headers)

    assert response.status_code == 403


def test_an_admin_trigger_returns_the_scrape_result(client, admin, db, http):
    _user, headers = admin
    _serve_one_group(http, [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])

    with time_machine.travel(SPRING_RUN, tick=False):
        response = client.post("/api/scraper/schedule", headers=headers)

    assert response.status_code == 200
    body = response.get_json()
    assert body["groups_scraped"] == 1
    assert body["lessons_new"] == 1
    assert body["dropped"] == {"all_day": 0, "retakes": 0, "unparsable": 0, "untitled": 0}
    assert len(_lesson_rows(db)) == 1


def test_a_failing_scrape_answers_502_with_a_stable_slug(client, admin, http):
    _user, headers = admin
    _serve_list(http, "", status=503)

    with time_machine.travel(SPRING_RUN, tick=False):
        response = client.post("/api/scraper/schedule", headers=headers)

    assert response.status_code == 502
    # The exception text stays in the log and scraper_runs, not
    # in an HTTP body
    assert response.get_json()["error"] == "scrape_failed"


def test_a_trigger_that_finds_the_lock_held_answers_409(client, admin):
    _user, headers = admin

    ss._RUN_LOCK.acquire()
    try:
        response = client.post("/api/scraper/schedule", headers=headers)
    finally:
        ss._RUN_LOCK.release()

    assert response.status_code == 409
    assert response.get_json()["skipped"] is True




# ==========================================================
# Contract — what the mobile schedule tab reads back
# ==========================================================


@pytest.mark.contract
def test_scraped_lessons_reach_the_schedule_api_in_its_wire_shape(app, client, http):
    _serve_one_group(http, [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00",
               instructor="A. Petraitis", location="301"),
    ])

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule(notify=False)

    response = client.get(f"/api/schedule?group=ISKS-1&semester={SPRING_ANCHOR}")

    assert response.status_code == 200
    lessons = response.get_json()["lessons"]
    assert len(lessons) == 1
    assert lessons[0]["title"] == "Programavimas"
    assert lessons[0]["dayOfWeek"] == 0
    assert lessons[0]["timeStart"] == "08:30"
    assert lessons[0]["timeEnd"] == "10:00"
