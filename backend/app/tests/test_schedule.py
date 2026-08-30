# -----------------------------------------------------------
#  [*] Tests — GET /api/schedule and GET /api/schedule/filters
#
#  What this module proves about schedule/routes.py and its
#  helpers (_parse_count, _semester_options, _table_version,
#  _conditional_json):
#
#    - the timetable is the app's works-without-login screen:
#      a guest, a student and a caller waving a garbage token
#      all get the same page, and no write method exists
#    - ?day=0 is MONDAY, not "no day given" — the regression
#      the mobile day strip depends on (fetchSchedule sends
#      day=0 whenever Monday is selected), plus every day up
#      to Sunday and both weekend days
#    - the day guard: 7, -1, "monday", "", " 3", "3_0", a
#      Unicode digit and a ten-digit number are all 400s,
#      while "06" is a legal Sunday
#    - the semester default: no ?semester means the NEWEST
#      label carrying MIN_SEMESTER_LESSONS rows, four rows is
#      not enough and five is, ?semester=all is the opt-out,
#      and an explicit stray label is still queryable
#    - limit/offset: clamped rather than refused at both ends,
#      refused when they are not a run of ASCII digits, and a
#      page that neither repeats nor drops a lesson
#    - the total order day → time → group → id, so days stop
#      interleaving while the client groups by dayOfWeek
#    - the filter sheet: distinct sorted groups, the distinct
#      day list that tells the client Saturday lectures exist,
#      the threshold that keeps a mislabelled lesson out of
#      the picker, the ?semester scope, and the
#      semesterGroups pairing that replaced two independent
#      global DISTINCTs
#    - the caching contract: a weak ETag over the data version
#      plus the query, Cache-Control: public, max-age=21600
#      surviving the global no-store hook, and a 304 with no
#      body — including the tag collision two "|"-carrying
#      filters used to share
#    - the wire shape the mobile ScheduleLesson type consumes,
#      camelCase and all
#
#  Nothing here sleeps or reaches the network: rows go in
#  through direct SQL, which is also the only way to write a
#  lesson at all — the admin demo-seed route is retired and
#  this module keeps it that way.
# -----------------------------------------------------------


import html
import sqlite3
import uuid

import pytest

from app.auth import routes as auth_routes


SCHEDULE = "/api/schedule"
FILTERS = "/api/schedule/filters"

# mobile/app/services/api/schedule.ts — ScheduleLesson
LESSON_KEYS = {"id", "title", "teacher", "room", "timeStart", "timeEnd",
               "dayOfWeek", "group", "semester"}

# ScheduleResponse is a one-key envelope; the filter sheet is
# the two contract lists plus the two additive ones
ENVELOPE_KEYS = {"lessons"}
FILTER_KEYS = {"groups", "semesters", "days", "semesterGroups"}

# Pinned by hand, NOT imported from the module: a test that
# reads MAX_LESSONS cannot notice MAX_LESSONS changing
CAP = 500
MAX_OFFSET = 100000
THRESHOLD = 5
CACHE_CONTROL = "public, max-age=21600"




# -----------------------------------------------------------
# _clean_rate_limit_store
# -----------------------------------------------------------
#
# The global before_request budget (600 requests per IP per
# five minutes) spends from a module-level dict that outlives
# the app fixture. This module makes hundreds of reads from
# one address, so the store is cleared on both sides — no
# sibling module's flood leaks in, and none of these reads
# leaks out.
#
# Used by:
#   - every test in this module (autouse)
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_rate_limit_store():
    auth_routes._rate_limit_store.clear()
    yield
    auth_routes._rate_limit_store.clear()




# -----------------------------------------------------------
# seed_lesson
# -----------------------------------------------------------
#
#   seed_lesson(day=5, group="ISKS-2", semester="2025-P")
#
# Inserts one schedule_lessons row and returns its id. Direct
# SQL is not a shortcut here — it is the ONLY writer left:
# the read API has no POST and the demo-seed route was
# retired, so nothing but the scraper (and this) can create a
# lesson.
#
# The default title is unique per row because migration v18
# put a unique index on the natural key
# (semester, group, day, times, title, teacher, room) —
# two identical fixtures would otherwise raise.
#
# Used by:
#   - every test that needs rows in the table
# -----------------------------------------------------------

@pytest.fixture
def seed_lesson(app):

    def _seed(day=0, group="ISKS-1", semester="2025-R", time_start="08:30", time_end="10:00",
              title=None, teacher="Dest. Petraitis", room="301", lesson_id=None, created_at=None):
        lesson_id = lesson_id or str(uuid.uuid4())
        title = title if title is not None else f"Paskaita {lesson_id[:8]}"

        conn = sqlite3.connect(app.config["DB_PATH"])
        try:
            conn.execute(
                """INSERT INTO schedule_lessons
                   (id, title, teacher, room, time_start, time_end,
                    day_of_week, group_name, semester, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))""",
                (lesson_id, title, teacher, room, time_start, time_end,
                 day, group, semester, created_at),
            )
            conn.commit()
        finally:
            conn.close()

        return lesson_id

    return _seed




# -----------------------------------------------------------
# fill_semester
# -----------------------------------------------------------
#
#   fill_semester("2025-P")        — five rows, a real label
#   fill_semester("2026-P", 4)     — four rows, still a stray
#
# A semester only becomes a picker value / the default once it
# carries MIN_SEMESTER_LESSONS rows, so most tests here need a
# cheap way to push a label over (or deliberately under) that
# line. Returns the ids in insertion order.
#
# Used by:
#   - the semester default, threshold and filter tests
# -----------------------------------------------------------

@pytest.fixture
def fill_semester(seed_lesson):

    def _fill(semester, count=THRESHOLD, group="ISKS-1", day=1):
        return [
            seed_lesson(semester=semester, group=group, day=day,
                        title=f"{semester} {group} #{index}")
            for index in range(count)
        ]

    return _fill




# -----------------------------------------------------------
# _bulk_lessons
# -----------------------------------------------------------
#
# `count` distinct lessons in a single transaction — the cap
# tests need more rows than MAX_LESSONS and one connection per
# row is the slow way there. time_start counts minutes from
# 08:00 so the ORDER BY is total and the expected page is
# simply the first N ids.
#
# Used by:
#   - the MAX_LESSONS ceiling tests
# -----------------------------------------------------------

def _bulk_lessons(app, count, semester="2025-R", group="ISKS-1", day=1):
    rows = [
        (f"bulk-{index:05d}", f"Paskaita {index:05d}", "Dest.", "301",
         f"{8 + index // 60:02d}:{index % 60:02d}", "23:59", day, group, semester)
        for index in range(count)
    ]

    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        conn.executemany(
            """INSERT INTO schedule_lessons
               (id, title, teacher, room, time_start, time_end,
                day_of_week, group_name, semester)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
    finally:
        conn.close()

    return [row[0] for row in rows]




# -----------------------------------------------------------
# _lessons / _ids / _titles / _etag / _error
# -----------------------------------------------------------
#
# The unwrappings every assertion below starts from. _error
# unescapes the message because the app's JSON provider
# escapes EVERY string it ships, error bodies included, so
# "Parameter 'day' …" reaches the wire as
# "Parameter &#x27;day&#x27; …" — exactly what the mobile
# client hands to decodeHtmlEntities (services/api/client.ts).
# The escaping itself is asserted raw, once, below.
# -----------------------------------------------------------

def _lessons(response):
    return response.get_json()["lessons"]


def _ids(response):
    return [lesson["id"] for lesson in _lessons(response)]


def _titles(response):
    return [lesson["title"] for lesson in _lessons(response)]


def _etag(response):
    return response.headers["ETag"]


def _error(response):
    return html.unescape(response.get_json()["error"])








# ===========================================================
# The public read — no login anywhere near it
# ===========================================================

def test_a_guest_reads_the_timetable_without_a_token(client, seed_lesson):
    seed_lesson(title="Duomenu bazes")

    response = client.get(SCHEDULE)

    assert response.status_code == 200
    assert _titles(response) == ["Duomenu bazes"]


def test_a_signed_in_student_sees_the_same_page_as_a_guest(client, actor, seed_lesson):
    _, headers = actor
    seed_lesson(title="Algoritmai")

    guest = client.get(SCHEDULE)
    member = client.get(SCHEDULE, headers=headers)

    assert member.status_code == 200
    assert _lessons(member) == _lessons(guest)


def test_an_admin_sees_no_more_lessons_than_a_guest(client, admin, seed_lesson):
    _, headers = admin
    seed_lesson(title="Statistika")

    assert _lessons(client.get(SCHEDULE, headers=headers)) == _lessons(client.get(SCHEDULE))


def test_a_garbage_bearer_token_does_not_block_the_public_read(client, seed_lesson):
    seed_lesson(title="Fizika")

    response = client.get(SCHEDULE, headers={"Authorization": "Bearer ne-tokenas"})

    assert response.status_code == 200
    assert _titles(response) == ["Fizika"]


def test_an_empty_table_answers_an_empty_lesson_list(client):
    response = client.get(SCHEDULE)

    assert response.status_code == 200
    assert response.get_json() == {"lessons": []}


def test_the_filter_sheet_needs_no_token_either(client):
    assert client.get(FILTERS).status_code == 200








# ===========================================================
# The wire shape the mobile app consumes
# ===========================================================

@pytest.mark.contract
def test_a_lesson_carries_exactly_the_mobile_wire_keys(client, seed_lesson):
    seed_lesson()

    lesson = _lessons(client.get(SCHEDULE))[0]

    assert set(lesson) == LESSON_KEYS


@pytest.mark.contract
def test_the_envelope_is_a_single_lessons_key(client, seed_lesson):
    seed_lesson()

    assert set(client.get(SCHEDULE).get_json()) == ENVELOPE_KEYS


@pytest.mark.contract
def test_the_snake_case_columns_reach_the_wire_camelcased(client, seed_lesson):
    seed_lesson(title="Tinklai", teacher="Doc. Kazlauskas", room="B-204",
                time_start="10:15", time_end="11:45", day=2,
                group="ISKS-2", semester="2025-R")

    lesson = _lessons(client.get(SCHEDULE))[0]

    assert lesson["timeStart"] == "10:15"
    assert lesson["timeEnd"] == "11:45"
    assert lesson["dayOfWeek"] == 2
    assert lesson["group"] == "ISKS-2"
    assert lesson["semester"] == "2025-R"
    assert lesson["title"] == "Tinklai"
    assert lesson["teacher"] == "Doc. Kazlauskas"
    assert lesson["room"] == "B-204"


@pytest.mark.contract
def test_day_of_week_stays_a_number_on_the_wire(client, seed_lesson):
    seed_lesson(day=4)

    assert _lessons(client.get(SCHEDULE))[0]["dayOfWeek"] == 4


@pytest.mark.contract
def test_a_lesson_with_no_teacher_or_room_carries_nulls_not_missing_keys(client, seed_lesson):
    seed_lesson(teacher=None, room=None, group=None, semester=None)

    lesson = _lessons(client.get(SCHEDULE))[0]

    assert set(lesson) == LESSON_KEYS
    assert lesson["teacher"] is None
    assert lesson["room"] is None
    assert lesson["group"] is None
    assert lesson["semester"] is None


def test_lithuanian_text_survives_as_utf8_not_escape_sequences(client, seed_lesson):
    seed_lesson(title="Programų sistemų inžinerija")

    response = client.get(SCHEDULE)

    assert _titles(response) == ["Programų sistemų inžinerija"]
    assert "\\u" not in response.get_data(as_text=True)


def test_a_lesson_title_is_html_escaped_on_output(client, seed_lesson):
    seed_lesson(title="<script>alert(1)</script>")

    body = client.get(SCHEDULE).get_data(as_text=True)

    assert "<script>" not in body
    assert "&lt;script&gt;" in body








# ===========================================================
# ?day — 0 is Monday, and the guard around it
# ===========================================================

def test_day_zero_is_monday_and_not_treated_as_unset(client, seed_lesson):
    # The regression: `if day is not None`, never `if day`.
    # fetchSchedule(0, ...) sends day=0 for Monday, and a
    # falsy-check here served the whole week instead
    seed_lesson(day=0, title="Pirmadienio paskaita")
    seed_lesson(day=1, title="Antradienio paskaita")

    response = client.get(SCHEDULE, query_string={"day": 0})

    assert response.status_code == 200
    assert _titles(response) == ["Pirmadienio paskaita"]


@pytest.mark.parametrize("day", [0, 1, 2, 3, 4, 5, 6])
def test_every_day_from_monday_to_sunday_filters_to_itself(client, seed_lesson, day):
    for weekday in range(7):
        seed_lesson(day=weekday, title=f"Diena {weekday}")

    response = client.get(SCHEDULE, query_string={"day": day})

    assert _titles(response) == [f"Diena {day}"]
    assert [lesson["dayOfWeek"] for lesson in _lessons(response)] == [day]


def test_weekend_lessons_are_served_like_any_other_day(client, seed_lesson):
    seed_lesson(day=5, title="Sestadienio sesija")
    seed_lesson(day=6, title="Sekmadienio sesija")

    saturday = client.get(SCHEDULE, query_string={"day": 5})
    sunday = client.get(SCHEDULE, query_string={"day": 6})

    assert _titles(saturday) == ["Sestadienio sesija"]
    assert _titles(sunday) == ["Sekmadienio sesija"]


def test_no_day_parameter_returns_every_day(client, seed_lesson):
    for weekday in range(7):
        seed_lesson(day=weekday, title=f"Diena {weekday}")

    response = client.get(SCHEDULE)

    assert [lesson["dayOfWeek"] for lesson in _lessons(response)] == [0, 1, 2, 3, 4, 5, 6]


def test_a_day_with_no_lessons_answers_an_empty_list(client, seed_lesson):
    seed_lesson(day=1)

    assert _lessons(client.get(SCHEDULE, query_string={"day": 3})) == []


def test_a_zero_padded_day_is_accepted(client, seed_lesson):
    seed_lesson(day=6, title="Sekmadienis")

    response = client.get(SCHEDULE, query_string={"day": "06"})

    assert response.status_code == 200
    assert _titles(response) == ["Sekmadienis"]


def test_day_seven_is_refused(client):
    response = client.get(SCHEDULE, query_string={"day": 7})

    assert response.status_code == 400
    assert _error(response) == "Parameter 'day' must be between 0 (Monday) and 6 (Sunday)"


def test_a_day_far_above_the_week_is_refused(client):
    assert client.get(SCHEDULE, query_string={"day": 999999999}).status_code == 400


def test_a_negative_day_is_refused(client):
    response = client.get(SCHEDULE, query_string={"day": -1})

    assert response.status_code == 400
    assert _error(response) == "Parameter 'day' must be an integer (0=Monday..6=Sunday)"


@pytest.mark.parametrize("raw", ["monday", "", " 3", "3 ", "3_0", "٣", "1.0", "+1", "0x1",
                                 "1234567890", "null", "NaN"])
def test_a_day_that_is_not_a_run_of_ascii_digits_is_refused(client, raw):
    response = client.get(SCHEDULE, query_string={"day": raw})

    assert response.status_code == 400
    assert _error(response).startswith("Parameter 'day' must be an integer")


def test_a_repeated_day_parameter_uses_the_first_value(client, seed_lesson):
    seed_lesson(day=0, title="Pirmadienis")
    seed_lesson(day=1, title="Antradienis")

    response = client.get(f"{SCHEDULE}?day=0&day=1")

    assert _titles(response) == ["Pirmadienis"]


def test_a_refused_day_is_a_json_error_not_an_html_page(client):
    response = client.get(SCHEDULE, query_string={"day": "septintadienis"})

    assert response.status_code == 400
    assert response.is_json
    assert set(response.get_json()) == {"error"}








# ===========================================================
# ?group
# ===========================================================

def test_the_group_filter_returns_only_that_group(client, seed_lesson):
    seed_lesson(group="ISKS-1", title="Grupes viena")
    seed_lesson(group="ISKS-2", title="Grupes dvi")

    response = client.get(SCHEDULE, query_string={"group": "ISKS-2"})

    assert _titles(response) == ["Grupes dvi"]


def test_an_unknown_group_answers_an_empty_list(client, seed_lesson):
    seed_lesson(group="ISKS-1")

    assert _lessons(client.get(SCHEDULE, query_string={"group": "NEBUVO"})) == []


def test_an_empty_group_counts_as_no_filter(client, seed_lesson):
    seed_lesson(group="ISKS-1")
    seed_lesson(group="ISKS-2")

    response = client.get(SCHEDULE, query_string={"group": ""})

    assert len(_lessons(response)) == 2


def test_the_group_filter_is_exact_and_not_a_prefix(client, seed_lesson):
    seed_lesson(group="ISKS-1", title="Trumpa")
    seed_lesson(group="ISKS-10", title="Ilga")

    assert _titles(client.get(SCHEDULE, query_string={"group": "ISKS-1"})) == ["Trumpa"]


def test_the_group_filter_is_case_sensitive(client, seed_lesson):
    seed_lesson(group="ISKS-1")

    assert _lessons(client.get(SCHEDULE, query_string={"group": "isks-1"})) == []


def test_a_group_carrying_sql_is_bound_not_interpolated(client, seed_lesson):
    seed_lesson(group="ISKS-1")

    response = client.get(SCHEDULE, query_string={"group": "' OR 1=1 --"})

    assert response.status_code == 200
    assert _lessons(response) == []


def test_a_group_carrying_a_drop_table_leaves_the_table_standing(client, db, seed_lesson):
    seed_lesson(group="ISKS-1")

    client.get(SCHEDULE, query_string={"group": "x'; DROP TABLE schedule_lessons; --"})

    assert db.execute("SELECT COUNT(*) FROM schedule_lessons").fetchone()[0] == 1


def test_the_day_and_group_filters_combine(client, seed_lesson):
    seed_lesson(day=1, group="ISKS-1", title="Taikinys")
    seed_lesson(day=1, group="ISKS-2", title="Kita grupe")
    seed_lesson(day=2, group="ISKS-1", title="Kita diena")

    response = client.get(SCHEDULE, query_string={"day": 1, "group": "ISKS-1"})

    assert _titles(response) == ["Taikinys"]


def test_every_filter_at_once_narrows_to_one_lesson(client, seed_lesson, fill_semester):
    fill_semester("2025-R", group="ISKS-1", day=3)
    seed_lesson(day=0, group="ISKS-9", semester="2025-R", title="Vienintele")

    response = client.get(SCHEDULE, query_string={
        "day": 0, "group": "ISKS-9", "semester": "2025-R", "limit": 10, "offset": 0,
    })

    assert _titles(response) == ["Vienintele"]








# ===========================================================
# ?semester and the default
# ===========================================================

def test_no_semester_serves_only_the_newest_real_semester(client, fill_semester):
    fill_semester("2024-R", group="SENA")
    fill_semester("2025-R", group="NAUJA")

    response = client.get(SCHEDULE)

    assert {lesson["semester"] for lesson in _lessons(response)} == {"2025-R"}
    assert len(_lessons(response)) == THRESHOLD


def test_four_lessons_are_not_enough_to_become_the_default_semester(client, fill_semester):
    fill_semester("2025-R")
    fill_semester("2026-R", count=THRESHOLD - 1)

    response = client.get(SCHEDULE)

    assert {lesson["semester"] for lesson in _lessons(response)} == {"2025-R"}


def test_five_lessons_are_enough_to_become_the_default_semester(client, fill_semester):
    fill_semester("2025-R")
    fill_semester("2026-R", count=THRESHOLD)

    response = client.get(SCHEDULE)

    assert {lesson["semester"] for lesson in _lessons(response)} == {"2026-R"}


def test_a_semester_below_the_threshold_is_still_queryable_by_name(client, fill_semester,
                                                                  seed_lesson):
    fill_semester("2025-R")
    seed_lesson(semester="2026-R", title="Nuklydusi")

    response = client.get(SCHEDULE, query_string={"semester": "2026-R"})

    assert _titles(response) == ["Nuklydusi"]


def test_an_empty_semester_parameter_falls_back_to_the_default(client, fill_semester):
    fill_semester("2024-R")
    fill_semester("2025-R")

    response = client.get(SCHEDULE, query_string={"semester": ""})

    assert {lesson["semester"] for lesson in _lessons(response)} == {"2025-R"}


def test_semester_all_serves_every_semester_at_once(client, fill_semester):
    fill_semester("2024-R")
    fill_semester("2025-R")

    response = client.get(SCHEDULE, query_string={"semester": "all"})

    assert {lesson["semester"] for lesson in _lessons(response)} == {"2024-R", "2025-R"}


@pytest.mark.parametrize("raw", ["all", "ALL", "All", "  all  ", "\tall\n"])
def test_semester_all_ignores_case_and_surrounding_space(client, fill_semester, raw):
    fill_semester("2024-R")
    fill_semester("2025-R")

    response = client.get(SCHEDULE, query_string={"semester": raw})

    assert {lesson["semester"] for lesson in _lessons(response)} == {"2024-R", "2025-R"}


def test_semester_all_also_reaches_rows_below_the_threshold(client, fill_semester, seed_lesson):
    fill_semester("2025-R")
    seed_lesson(semester="2026-R", title="Nuklydusi")

    titles = _titles(client.get(SCHEDULE, query_string={"semester": "all"}))

    assert "Nuklydusi" in titles


def test_an_unknown_semester_answers_an_empty_list(client, fill_semester):
    fill_semester("2025-R")

    assert _lessons(client.get(SCHEDULE, query_string={"semester": "1999-R"})) == []


def test_a_table_where_no_label_clears_the_threshold_serves_every_row(client, seed_lesson):
    # No semester option exists, so the default is None and the
    # WHERE falls back to 1=1 rather than filtering everything out
    for index in range(THRESHOLD - 1):
        seed_lesson(semester="2025-R", title=f"Likutis {index}")

    response = client.get(SCHEDULE)

    assert len(_lessons(response)) == THRESHOLD - 1


def test_rows_without_a_semester_label_are_served_when_no_default_exists(client, seed_lesson):
    seed_lesson(semester=None, title="Be semestro")

    assert _titles(client.get(SCHEDULE)) == ["Be semestro"]


def test_an_empty_semester_label_never_becomes_the_default(client, fill_semester):
    fill_semester("")
    fill_semester("2025-R")

    response = client.get(SCHEDULE)

    assert {lesson["semester"] for lesson in _lessons(response)} == {"2025-R"}


def test_a_later_year_outranks_an_earlier_one(client, fill_semester):
    fill_semester("2019-R")
    fill_semester("2025-R")
    fill_semester("2021-R")

    assert {lesson["semester"] for lesson in _lessons(client.get(SCHEDULE))} == {"2025-R"}


def test_a_lowercase_legacy_label_does_not_outrank_a_real_semester(client, fill_semester):
    # COLLATE NOCASE on the ORDER BY: under the plain BINARY
    # sort the retired "2025-pavasaris" seed sorted above every
    # "2025-R" and became the default semester
    fill_semester("2025-pavasaris")
    fill_semester("2025-R")

    assert {lesson["semester"] for lesson in _lessons(client.get(SCHEDULE))} == {"2025-R"}


@pytest.mark.xfail(strict=True, reason="the default semester sorts labels as TEXT, but "
                                       "scraper _get_semester_label makes YYYY-P the spring "
                                       "of YYYY+1 — newer than YYYY-R, which text DESC puts first")
def test_spring_is_newer_than_the_autumn_of_the_same_label_year(client, fill_semester):
    # _get_semester_label: Aug-Dec -> "<year>-R", Jan-Jul ->
    # "<year-1>-P", so "2025-P" is spring 2026 and comes AFTER
    # "2025-R" (autumn 2025). Text DESC sorts R above P and the
    # app opens on the finished semester whenever both are in
    # the table
    fill_semester("2025-R")
    fill_semester("2025-P")

    assert {lesson["semester"] for lesson in _lessons(client.get(SCHEDULE))} == {"2025-P"}


def test_the_default_semester_does_not_swallow_the_day_filter(client, seed_lesson,
                                                              fill_semester):
    fill_semester("2025-R", day=1)
    seed_lesson(day=0, semester="2025-R", title="Pirmadienis dabartiniame")
    seed_lesson(day=0, semester="2024-R", title="Pirmadienis senajame")

    response = client.get(SCHEDULE, query_string={"day": 0})

    assert _titles(response) == ["Pirmadienis dabartiniame"]








# ===========================================================
# ?limit and ?offset — clamped, never silently wrong
# ===========================================================

def test_limit_caps_the_page(client, seed_lesson):
    for index in range(5):
        seed_lesson(day=index, title=f"Diena {index}")

    response = client.get(SCHEDULE, query_string={"limit": 2})

    assert _titles(response) == ["Diena 0", "Diena 1"]


def test_limit_zero_is_clamped_up_to_one_lesson(client, seed_lesson):
    seed_lesson(day=0)
    seed_lesson(day=1)

    response = client.get(SCHEDULE, query_string={"limit": 0})

    assert response.status_code == 200
    assert len(_lessons(response)) == 1


def test_a_limit_above_the_cap_is_clamped_not_refused(client, seed_lesson):
    seed_lesson()

    response = client.get(SCHEDULE, query_string={"limit": 10000})

    assert response.status_code == 200
    assert len(_lessons(response)) == 1


def test_a_nine_digit_limit_is_clamped_not_refused(client, seed_lesson):
    seed_lesson()

    assert client.get(SCHEDULE, query_string={"limit": "999999999"}).status_code == 200


@pytest.mark.slow
def test_the_page_never_exceeds_five_hundred_lessons(client, app):
    ids = _bulk_lessons(app, CAP + 5)

    unfiltered = client.get(SCHEDULE)
    asked_for_more = client.get(SCHEDULE, query_string={"limit": CAP + 5})

    assert len(_lessons(unfiltered)) == CAP
    assert _ids(unfiltered) == ids[:CAP]
    assert len(_lessons(asked_for_more)) == CAP


@pytest.mark.slow
def test_the_tail_past_the_cap_is_reachable_through_offset(client, app):
    ids = _bulk_lessons(app, CAP + 5)

    response = client.get(SCHEDULE, query_string={"offset": CAP})

    assert _ids(response) == ids[CAP:]


def test_offset_pages_through_the_ordered_list(client, seed_lesson):
    for index in range(5):
        seed_lesson(day=index, title=f"Diena {index}")

    response = client.get(SCHEDULE, query_string={"limit": 2, "offset": 2})

    assert _titles(response) == ["Diena 2", "Diena 3"]


def test_paging_neither_repeats_nor_drops_a_lesson(client, seed_lesson):
    for index in range(6):
        seed_lesson(day=index % 7, time_start=f"{8 + index:02d}:00", title=f"Paskaita {index}")

    whole = _ids(client.get(SCHEDULE))
    paged = []
    for offset in (0, 2, 4):
        paged += _ids(client.get(SCHEDULE, query_string={"limit": 2, "offset": offset}))

    assert paged == whole
    assert len(set(paged)) == 6


def test_an_offset_past_the_end_answers_an_empty_page(client, seed_lesson):
    seed_lesson()

    response = client.get(SCHEDULE, query_string={"offset": 50})

    assert response.status_code == 200
    assert _lessons(response) == []


def test_an_offset_above_its_ceiling_is_clamped_not_refused(client, seed_lesson):
    seed_lesson()

    response = client.get(SCHEDULE, query_string={"offset": MAX_OFFSET + 1})

    assert response.status_code == 200
    assert _lessons(response) == []


def test_offset_zero_is_the_first_page(client, seed_lesson):
    seed_lesson(day=0, title="Pirma")
    seed_lesson(day=1, title="Antra")

    assert _titles(client.get(SCHEDULE, query_string={"offset": 0})) == ["Pirma", "Antra"]


@pytest.mark.parametrize("name", ["limit", "offset"])
@pytest.mark.parametrize("raw", ["", " 5", "5 ", "5_0", "-5", "abc", "٣", "5.0", "1234567890"])
def test_a_count_that_is_not_a_run_of_ascii_digits_is_refused(client, name, raw):
    response = client.get(SCHEDULE, query_string={name: raw})

    assert response.status_code == 400
    assert _error(response) == f"Parameter '{name}' must be a non-negative integer"


def test_a_bad_limit_is_refused_before_the_offset_is_read(client):
    response = client.get(SCHEDULE, query_string={"limit": "abc", "offset": "def"})

    assert _error(response) == "Parameter 'limit' must be a non-negative integer"


def test_a_bad_day_is_refused_before_the_limit_is_read(client):
    response = client.get(SCHEDULE, query_string={"day": "abc", "limit": "abc"})

    assert _error(response).startswith("Parameter 'day'")








# ===========================================================
# Ordering — days must not interleave
# ===========================================================

def test_lessons_come_back_ordered_by_day_then_time(client, seed_lesson):
    seed_lesson(day=2, time_start="08:30", title="Antradienis rytas")
    seed_lesson(day=0, time_start="14:00", title="Pirmadienis popiete")
    seed_lesson(day=0, time_start="08:30", title="Pirmadienis rytas")

    assert _titles(client.get(SCHEDULE)) == [
        "Pirmadienis rytas", "Pirmadienis popiete", "Antradienis rytas",
    ]


def test_two_lessons_at_the_same_minute_are_ordered_by_group(client, seed_lesson):
    seed_lesson(day=1, time_start="09:00", group="ISKS-2", title="Antra grupe")
    seed_lesson(day=1, time_start="09:00", group="ISKS-1", title="Pirma grupe")

    assert _titles(client.get(SCHEDULE)) == ["Pirma grupe", "Antra grupe"]


def test_lessons_of_the_same_group_and_minute_are_ordered_by_id(client, seed_lesson):
    seed_lesson(day=1, time_start="09:00", lesson_id="bbb", title="Antra")
    seed_lesson(day=1, time_start="09:00", lesson_id="aaa", title="Pirma")

    assert _titles(client.get(SCHEDULE)) == ["Pirma", "Antra"]


def test_days_never_interleave(client, seed_lesson):
    for day in (3, 0, 5, 1):
        seed_lesson(day=day, time_start="12:00", title=f"Diena {day}")
        seed_lesson(day=day, time_start="08:00", title=f"Diena {day} rytas")

    days = [lesson["dayOfWeek"] for lesson in _lessons(client.get(SCHEDULE))]

    assert days == sorted(days)
    assert days == [0, 0, 1, 1, 3, 3, 5, 5]








# ===========================================================
# The caching contract
# ===========================================================

def test_the_answer_carries_a_weak_etag_and_a_six_hour_public_cache(client, seed_lesson):
    seed_lesson()

    response = client.get(SCHEDULE)

    assert response.headers["ETag"].startswith('W/"')
    assert response.headers["Cache-Control"] == CACHE_CONTROL


def test_the_public_cache_control_survives_the_global_no_store_hook(client):
    # add_security_headers setdefaults no-store on every /api/
    # path; the timetable's own value has to win
    response = client.get(SCHEDULE)

    assert "no-store" not in response.headers["Cache-Control"]


def test_a_matching_if_none_match_answers_304_without_a_body(client, seed_lesson):
    seed_lesson()
    first = client.get(SCHEDULE)

    second = client.get(SCHEDULE, headers={"If-None-Match": _etag(first)})

    assert second.status_code == 304
    assert second.get_data() == b""


def test_the_304_still_carries_the_etag_and_the_cache_headers(client, seed_lesson):
    seed_lesson()
    first = client.get(SCHEDULE)

    second = client.get(SCHEDULE, headers={"If-None-Match": _etag(first)})

    assert _etag(second) == _etag(first)
    assert second.headers["Cache-Control"] == CACHE_CONTROL


def test_the_strong_form_of_the_tag_also_answers_304(client, seed_lesson):
    seed_lesson()
    tag = _etag(client.get(SCHEDULE)).removeprefix("W/")

    assert client.get(SCHEDULE, headers={"If-None-Match": tag}).status_code == 304


def test_a_star_if_none_match_answers_304(client, seed_lesson):
    seed_lesson()

    assert client.get(SCHEDULE, headers={"If-None-Match": "*"}).status_code == 304


@pytest.mark.xfail(strict=True, reason="_table_version fingerprints COUNT(*) + MAX(created_at), "
                                       "so an in-place edit (a DbGate fix, a future UPDATE) "
                                       "keeps the old tag and clients hold the stale timetable")
def test_editing_a_lesson_in_place_moves_the_etag(client, db, seed_lesson):
    lesson_id = seed_lesson(title="Klaidingas pavadinimas")
    before = _etag(client.get(SCHEDULE))

    db.execute("UPDATE schedule_lessons SET title = 'Pataisytas' WHERE id = ?", (lesson_id,))
    db.commit()

    assert _etag(client.get(SCHEDULE)) != before


def test_a_stale_etag_gets_a_fresh_body(client, seed_lesson):
    seed_lesson()

    response = client.get(SCHEDULE, headers={"If-None-Match": 'W/"senas-tagas"'})

    assert response.status_code == 200
    assert len(_lessons(response)) == 1


def test_adding_a_lesson_moves_the_etag(client, seed_lesson):
    seed_lesson(day=0)
    before = _etag(client.get(SCHEDULE))

    seed_lesson(day=1)
    after = client.get(SCHEDULE, headers={"If-None-Match": before})

    assert after.status_code == 200
    assert _etag(after) != before


def test_removing_a_lesson_moves_the_etag(client, db, seed_lesson):
    lesson_id = seed_lesson(day=0)
    seed_lesson(day=1)
    before = _etag(client.get(SCHEDULE))

    db.execute("DELETE FROM schedule_lessons WHERE id = ?", (lesson_id,))
    db.commit()

    assert _etag(client.get(SCHEDULE)) != before


def test_an_unchanged_table_keeps_the_same_tag_across_calls(client, seed_lesson):
    seed_lesson()

    assert _etag(client.get(SCHEDULE)) == _etag(client.get(SCHEDULE))


def test_each_query_gets_its_own_etag(client, seed_lesson):
    seed_lesson(day=0)
    seed_lesson(day=1)

    tags = {
        _etag(client.get(SCHEDULE)),
        _etag(client.get(SCHEDULE, query_string={"day": 0})),
        _etag(client.get(SCHEDULE, query_string={"day": 1})),
        _etag(client.get(SCHEDULE, query_string={"group": "ISKS-1"})),
        _etag(client.get(SCHEDULE, query_string={"limit": 1})),
        _etag(client.get(SCHEDULE, query_string={"offset": 1})),
    }

    assert len(tags) == 6


def test_one_query_tag_never_serves_another_querys_lessons(client, seed_lesson):
    seed_lesson(day=0, title="Pirmadienis")
    seed_lesson(day=1, title="Antradienis")
    monday_tag = _etag(client.get(SCHEDULE, query_string={"day": 0}))

    tuesday = client.get(SCHEDULE, query_string={"day": 1},
                         headers={"If-None-Match": monday_tag})

    assert tuesday.status_code == 200
    assert _titles(tuesday) == ["Antradienis"]


def test_a_pipe_in_a_filter_cannot_collide_two_queries_into_one_tag(client, seed_lesson):
    # The seed used to be pipe-joined raw, so ?group=a|b&
    # semester=c and ?group=a&semester=b|c hashed the same
    # string and a client could be handed the wrong 304
    seed_lesson(group="a|b", semester="c", title="Sujungta")
    seed_lesson(group="a", semester="b|c", title="Kitaip sujungta")

    first = client.get(SCHEDULE, query_string={"group": "a|b", "semester": "c"})
    second = client.get(SCHEDULE, query_string={"group": "a", "semester": "b|c"})

    assert _titles(first) == ["Sujungta"]
    assert _titles(second) == ["Kitaip sujungta"]
    assert _etag(first) != _etag(second)


def test_a_colliding_tag_does_not_serve_the_wrong_lessons(client, seed_lesson):
    seed_lesson(group="a|b", semester="c", title="Sujungta")
    seed_lesson(group="a", semester="b|c", title="Kitaip sujungta")
    tag = _etag(client.get(SCHEDULE, query_string={"group": "a|b", "semester": "c"}))

    other = client.get(SCHEDULE, query_string={"group": "a", "semester": "b|c"},
                       headers={"If-None-Match": tag})

    assert other.status_code == 200
    assert _titles(other) == ["Kitaip sujungta"]








# ===========================================================
# GET /api/schedule/filters
# ===========================================================

@pytest.mark.contract
def test_the_filter_sheet_carries_groups_semesters_days_and_pairs(client, fill_semester):
    fill_semester("2025-R", group="ISKS-1", day=2)

    payload = client.get(FILTERS).get_json()

    assert set(payload) == FILTER_KEYS
    assert payload["groups"] == ["ISKS-1"]
    assert payload["semesters"] == ["2025-R"]
    assert payload["days"] == [2]
    assert payload["semesterGroups"] == [{"semester": "2025-R", "groups": ["ISKS-1"]}]


@pytest.mark.contract
def test_a_semester_group_pair_carries_exactly_two_keys(client, fill_semester):
    fill_semester("2025-R")

    pair = client.get(FILTERS).get_json()["semesterGroups"][0]

    assert set(pair) == {"semester", "groups"}


def test_the_filter_sheet_answers_empty_lists_on_an_empty_table(client):
    payload = client.get(FILTERS).get_json()

    assert payload == {"groups": [], "semesters": [], "days": [], "semesterGroups": []}


def test_groups_are_distinct_and_sorted(client, seed_lesson):
    for group in ("ISKS-2", "ISKS-1", "ISKS-2", "AKST-1"):
        seed_lesson(group=group, title=f"{group} {uuid.uuid4().hex[:6]}")

    assert client.get(FILTERS).get_json()["groups"] == ["AKST-1", "ISKS-1", "ISKS-2"]


def test_days_are_distinct_and_ascending(client, seed_lesson):
    for day in (4, 1, 4, 0):
        seed_lesson(day=day, title=f"Diena {day} {uuid.uuid4().hex[:6]}")

    assert client.get(FILTERS).get_json()["days"] == [0, 1, 4]


def test_the_day_list_tells_the_client_that_weekend_lectures_exist(client, seed_lesson):
    seed_lesson(day=1)
    seed_lesson(day=5)
    seed_lesson(day=6)

    assert client.get(FILTERS).get_json()["days"] == [1, 5, 6]


def test_a_group_with_no_name_is_not_offered_as_a_filter_value(client, seed_lesson):
    seed_lesson(group=None)
    seed_lesson(group="ISKS-1")

    assert client.get(FILTERS).get_json()["groups"] == ["ISKS-1"]


def test_a_semester_below_the_threshold_is_not_offered(client, fill_semester):
    fill_semester("2025-R")
    fill_semester("2026-R", count=THRESHOLD - 1)

    assert client.get(FILTERS).get_json()["semesters"] == ["2025-R"]


def test_the_threshold_boundary_puts_a_semester_in_the_picker(client, fill_semester):
    fill_semester("2025-R")
    fill_semester("2026-R", count=THRESHOLD)

    assert client.get(FILTERS).get_json()["semesters"] == ["2026-R", "2025-R"]


def test_a_blank_semester_label_is_never_offered(client, fill_semester):
    fill_semester("")
    fill_semester("2025-R")

    assert client.get(FILTERS).get_json()["semesters"] == ["2025-R"]


def test_semesters_are_listed_newest_first(client, fill_semester):
    fill_semester("2023-R")
    fill_semester("2025-R")
    fill_semester("2024-R")

    assert client.get(FILTERS).get_json()["semesters"] == ["2025-R", "2024-R", "2023-R"]


def test_a_semester_scope_narrows_groups_and_days(client, fill_semester):
    fill_semester("2024-R", group="SENA", day=1)
    fill_semester("2025-R", group="NAUJA", day=4)

    payload = client.get(FILTERS, query_string={"semester": "2024-R"}).get_json()

    assert payload["groups"] == ["SENA"]
    assert payload["days"] == [1]


def test_the_semester_list_ignores_the_scope(client, fill_semester):
    fill_semester("2024-R", group="SENA")
    fill_semester("2025-R", group="NAUJA")

    payload = client.get(FILTERS, query_string={"semester": "2024-R"}).get_json()

    assert payload["semesters"] == ["2025-R", "2024-R"]
    assert [pair["semester"] for pair in payload["semesterGroups"]] == ["2025-R", "2024-R"]


def test_an_unknown_semester_scope_yields_no_groups_or_days(client, fill_semester):
    fill_semester("2025-R")

    payload = client.get(FILTERS, query_string={"semester": "1999-R"}).get_json()

    assert payload["groups"] == []
    assert payload["days"] == []
    assert payload["semesters"] == ["2025-R"]


def test_an_empty_semester_parameter_is_not_a_scope(client, fill_semester):
    fill_semester("2024-R", group="SENA")
    fill_semester("2025-R", group="NAUJA")

    payload = client.get(FILTERS, query_string={"semester": ""}).get_json()

    assert payload["groups"] == ["NAUJA", "SENA"]


def test_the_filter_sheet_reads_all_as_a_literal_label(client, fill_semester):
    # Unlike GET /api/schedule, the filter sheet has no "all"
    # opt-out: the word is just a semester nobody wrote
    fill_semester("2025-R")

    payload = client.get(FILTERS, query_string={"semester": "all"}).get_json()

    assert payload["groups"] == []
    assert payload["semesters"] == ["2025-R"]


def test_a_semester_scope_carrying_sql_is_bound_not_interpolated(client, db, fill_semester):
    fill_semester("2025-R")

    payload = client.get(FILTERS, query_string={
        "semester": "x' OR '1'='1"}).get_json()

    assert payload["groups"] == []
    assert db.execute("SELECT COUNT(*) FROM schedule_lessons").fetchone()[0] == THRESHOLD


def test_semester_groups_pairs_each_semester_with_its_own_groups(client, fill_semester):
    fill_semester("2024-R", group="SENA")
    fill_semester("2025-R", group="NAUJA")
    fill_semester("2025-R", group="ALFA")

    pairs = client.get(FILTERS).get_json()["semesterGroups"]

    assert pairs == [
        {"semester": "2025-R", "groups": ["ALFA", "NAUJA"]},
        {"semester": "2024-R", "groups": ["SENA"]},
    ]


def test_semester_groups_drops_a_label_below_the_threshold(client, fill_semester,
                                                           seed_lesson):
    fill_semester("2025-R", group="NAUJA")
    seed_lesson(semester="2026-R", group="NUKLYDUSI", title="Viena")

    payload = client.get(FILTERS).get_json()

    assert [pair["semester"] for pair in payload["semesterGroups"]] == ["2025-R"]
    assert payload["semesters"] == ["2025-R"]


def test_a_semester_with_no_named_group_pairs_with_an_empty_list(client, fill_semester):
    fill_semester("2025-R", group=None)

    payload = client.get(FILTERS).get_json()

    assert payload["groups"] == []
    assert payload["semesterGroups"] == [{"semester": "2025-R", "groups": []}]


def test_a_group_pair_is_not_invented_for_a_semester_that_lost_its_rows(client, db,
                                                                        fill_semester):
    fill_semester("2025-R", group="NAUJA")
    fill_semester("2024-R", group="SENA")
    db.execute("DELETE FROM schedule_lessons WHERE semester = '2024-R'")
    db.commit()

    payload = client.get(FILTERS).get_json()

    assert payload["semesters"] == ["2025-R"]
    assert payload["semesterGroups"] == [{"semester": "2025-R", "groups": ["NAUJA"]}]


def test_the_filter_sheet_carries_its_own_weak_etag_and_cache_control(client, fill_semester):
    fill_semester("2025-R")

    response = client.get(FILTERS)

    assert response.headers["ETag"].startswith('W/"')
    assert response.headers["Cache-Control"] == CACHE_CONTROL


def test_a_matching_if_none_match_answers_304_on_the_filter_sheet(client, fill_semester):
    fill_semester("2025-R")
    first = client.get(FILTERS)

    second = client.get(FILTERS, headers={"If-None-Match": _etag(first)})

    assert second.status_code == 304
    assert second.get_data() == b""


def test_the_filter_etag_moves_when_a_lesson_is_added(client, fill_semester, seed_lesson):
    fill_semester("2025-R")
    before = _etag(client.get(FILTERS))

    seed_lesson(group="NAUJA", title="Nauja paskaita")

    assert _etag(client.get(FILTERS)) != before


def test_the_filter_etag_differs_per_scope(client, fill_semester):
    fill_semester("2024-R")
    fill_semester("2025-R")

    tags = {
        _etag(client.get(FILTERS)),
        _etag(client.get(FILTERS, query_string={"semester": "2024-R"})),
        _etag(client.get(FILTERS, query_string={"semester": "2025-R"})),
    }

    assert len(tags) == 3


def test_the_schedule_and_filter_tags_never_collide(client, fill_semester):
    fill_semester("2025-R")

    assert _etag(client.get(SCHEDULE)) != _etag(client.get(FILTERS))


def test_the_filter_sheet_escapes_a_group_name_on_output(client, seed_lesson):
    seed_lesson(group="<b>ISKS</b>")

    body = client.get(FILTERS).get_data(as_text=True)

    assert "<b>" not in body
    assert "&lt;b&gt;" in body








# ===========================================================
# Method and route gates — the read API stays read-only
# ===========================================================

@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_the_timetable_refuses_every_write_method(client, method):
    response = getattr(client, method)(SCHEDULE, json={"title": "Isvis ne"})

    assert response.status_code == 405
    assert response.get_json() == {"error": "Method not allowed"}


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_the_filter_sheet_refuses_every_write_method(client, method):
    assert getattr(client, method)(FILTERS, json={}).status_code == 405


def test_an_admin_cannot_write_a_lesson_through_the_timetable(client, admin, db):
    _, headers = admin

    response = client.post(SCHEDULE, headers=headers, json={
        "title": "Ranka irasyta", "timeStart": "08:00", "timeEnd": "09:00", "dayOfWeek": 0,
    })

    assert response.status_code == 405
    assert db.execute("SELECT COUNT(*) FROM schedule_lessons").fetchone()[0] == 0


@pytest.mark.parametrize("method", ["get", "post"])
def test_the_retired_demo_seed_route_is_gone_for_everyone(client, admin, actor, db, method):
    _, admin_headers = admin
    _, student_headers = actor

    for headers in ({}, student_headers, admin_headers):
        response = getattr(client, method)(f"{SCHEDULE}/seed", headers=headers)
        assert response.status_code == 404
        assert response.get_json() == {"error": "Not found"}

    assert db.execute("SELECT COUNT(*) FROM schedule_lessons").fetchone()[0] == 0


def test_no_unknown_child_route_hides_under_the_timetable(client):
    assert client.get(f"{SCHEDULE}/lessons").status_code == 404
    assert client.get(f"{SCHEDULE}/filters/all").status_code == 404


@pytest.mark.contract
def test_an_error_body_reaches_the_wire_html_escaped(client):
    # The JSON provider escapes EVERY string, error bodies
    # included, and the mobile client decodes them again — the
    # quotes around the parameter name are the visible proof
    response = client.get(SCHEDULE, query_string={"day": "pirmadienis"})

    assert response.get_json()["error"] == \
        "Parameter &#x27;day&#x27; must be an integer (0=Monday..6=Sunday)"


@pytest.mark.contract
def test_both_reads_answer_json(client, seed_lesson):
    seed_lesson()

    assert client.get(SCHEDULE).headers["Content-Type"].startswith("application/json")
    assert client.get(FILTERS).headers["Content-Type"].startswith("application/json")


def test_semester_all_still_honours_the_day_and_group_filters(client, fill_semester,
                                                              seed_lesson):
    fill_semester("2024-R", group="ISKS-1", day=1)
    seed_lesson(semester="2019-R", group="ISKS-1", day=0, title="Sena pirmadienio")
    seed_lesson(semester="2019-R", group="ISKS-2", day=0, title="Sena kitos grupes")

    response = client.get(SCHEDULE, query_string={
        "semester": "all", "day": 0, "group": "ISKS-1"})

    assert _titles(response) == ["Sena pirmadienio"]


def test_a_head_request_gets_the_headers_without_a_body(client, seed_lesson):
    seed_lesson()

    response = client.head(SCHEDULE)

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == CACHE_CONTROL
    assert response.get_data() == b""
