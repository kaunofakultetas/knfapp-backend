# -----------------------------------------------------------
#  [*] Tests — the shared slice, exhaustively
#
#  The gap-closing pass over the four pieces the rest of the
#  backend leans on, one branch and one boundary at a time:
#
#    app/api/__init__.py     — parse_pagination, the post-body
#                              limits and FEED_SCORE_SQL
#    app/schedule/routes.py  — both timetable reads plus
#                              _parse_count, _semester_options,
#                              _table_version, _conditional_json
#    app/info/routes.py      — the handbook: language
#                              normalisation, the section gate,
#                              the scraped overlay, the
#                              timestamps and the cache
#    app/scraper/plurals.py  — lt_plural
#
#  What this module adds on top of the broad suite:
#
#    lt_plural
#      - the "other" arm (0, 10-19, every round ten, 111,
#        112-119) that NOTHING else in the suite executed —
#        the one uncovered line in the whole slice
#      - the full CLDR table 0..2000 against an independently
#        written rule, negatives folded to their magnitude,
#        counts past 10^18, and a two-form tuple raising
#        rather than silently mis-declining
#
#    parse_pagination
#      - the guards int() itself owns: CPython's 4300-digit
#        string limit is a 400, not a MemoryError; Unicode and
#        full-width digits are ACCEPTED here while the
#        schedule's own parser refuses them
#      - the refusal is (None, None, (response, 400)) — the
#        400 lives in the TUPLE, the response object still
#        says 200 until Flask unpacks it
#      - an absent ?page never meets max_page, so a caller
#        pinning max_page=0 still gets page 1
#
#    the schedule reads
#      - _parse_count as a unit: clamped ends, ten digits
#        refused where nine are clamped, and every non-ASCII
#        spelling int() would have swallowed
#      - what is NOT stripped (?group, ?semester), what a
#        refusal must not carry (an ETag, a public max-age),
#        and the empty-string group name the filter sheet
#        offers but no query can select
#      - the text sort behind ORDER BY time_start: an
#        unpadded "9:00" lands after "10:00"
#
#    the handbook
#      - every ?lang spelling that survives the normaliser and
#        every one that falls back to lt
#      - the overlay's shape guards from the wrong side: a
#        category whose "items" is a dict, a list of nulls, a
#        general_contact of False/0/[]
#      - a scraped blob nested past the output escaper's depth
#        cap fails CLOSED (500) instead of shipping raw
#      - an English row of ANY section blocks the Lithuanian
#        borrow, floor failure or not
#      - _parse_timestamp's TypeError arm (a datetime, not a
#        string) and the mixed-shape "newest" comparison,
#        which ranks the rows by parsed instant now that it
#        no longer sorts the raw strings against each other
#
#  Nothing here sleeps, reaches the network or writes through
#  an API: schedule_lessons and faculty_info have no write
#  route at all, so direct SQL is the only writer there is.
# -----------------------------------------------------------


import html
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.api import (
    FEED_SCORE_SQL,
    MAX_CONTENT_LENGTH,
    MAX_TITLE_LENGTH,
    SUMMARY_LENGTH,
    parse_pagination,
)
from app.auth import routes as auth_routes
from app.info.routes import (
    FACULTY_INFO,
    MIN_SCRAPED_CONTACT_ITEMS,
    MIN_SCRAPED_PROGRAMS,
    SCRAPED_MAX_AGE_DAYS,
    _parse_timestamp,
    _warned,
)
from app.schedule.routes import _parse_count, _semester_options, _table_version
from app.scraper.plurals import lt_plural


SCHEDULE = "/api/schedule"
FILTERS = "/api/schedule/filters"
INFO = "/api/info"

# Pinned by hand rather than imported: a test that reads the
# module's own constant cannot notice the constant moving
CAP = 500
MAX_OFFSET = 100000
THRESHOLD = 5
SCHEDULE_CACHE = "public, max-age=21600"
INFO_CACHE = "public, max-age=86400"

# The three Lithuanian forms of "new article", the shape the
# scrapers build their push bodies from
STRAIPSNIS = ("naujas straipsnis", "nauji straipsniai", "naujų straipsnių")




# -----------------------------------------------------------
# _clean_process_state
# -----------------------------------------------------------
#
# Two module-level stores outlive the `app` fixture and would
# otherwise carry one test's history into the next: auth's
# rate-limit buckets (this file makes hundreds of reads from
# one address, and the global before_request budget spends
# from that dict) and info's _warned set, which mutes a
# warning for the life of the PROCESS.
#
# Used by:
#   - every test in this module (autouse)
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_process_state():
    auth_routes._rate_limit_store.clear()
    _warned.clear()
    yield
    auth_routes._rate_limit_store.clear()
    _warned.clear()




# -----------------------------------------------------------
# _parse
# -----------------------------------------------------------
#
#   _parse(app, "?page=2", max_page=5)
#
# parse_pagination reads request.args and answers with
# jsonify, so it needs a request context; this is the whole
# call in one line.
#
# Used by:
#   - every parse_pagination test below
# -----------------------------------------------------------

def _parse(app, query="", **kwargs):
    with app.test_request_context(f"/api/probe{query}"):
        return parse_pagination(**kwargs)




# -----------------------------------------------------------
# _refusal
# -----------------------------------------------------------
#
# Asserts the shape every parse_pagination guard answers with
# — (None, None, (response, 400)) — and hands back the
# message. Unescaped, because the app's JSON provider entity-
# escapes every string it serialises, error bodies included.
#
# Used by:
#   - the page and per_page guard tests
# -----------------------------------------------------------

def _refusal(result):
    page, per_page, err = result

    assert (page, per_page) == (None, None)
    response, status = err
    assert status == 400

    return html.unescape(response.get_json()["error"])




# -----------------------------------------------------------
# lesson
# -----------------------------------------------------------
#
#   lesson(day=5, group="ISKS-2", time_start="09:00")
#
# One schedule_lessons row, returning its id. Direct SQL is
# not a shortcut: the read API has no write method and the
# demo-seed route is retired, so nothing but the scraper can
# create a lesson. Titles default to something unique because
# migration v18 put a UNIQUE index on the natural key.
#
# Used by:
#   - every schedule test that needs rows
# -----------------------------------------------------------

@pytest.fixture
def lesson(app):

    def _lesson(day=0, group="ISKS-1", semester="2025-R", time_start="08:30",
                time_end="10:00", title=None, teacher="Dest. Petraitis", room="301",
                created_at=None):
        lesson_id = str(uuid.uuid4())
        conn = sqlite3.connect(app.config["DB_PATH"])
        try:
            conn.execute(
                """INSERT INTO schedule_lessons
                   (id, title, teacher, room, time_start, time_end,
                    day_of_week, group_name, semester, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))""",
                (lesson_id, title if title is not None else f"Paskaita {lesson_id[:8]}",
                 teacher, room, time_start, time_end, day, group, semester, created_at),
            )
            conn.commit()
        finally:
            conn.close()

        return lesson_id

    return _lesson




# -----------------------------------------------------------
# semester_rows
# -----------------------------------------------------------
#
#   semester_rows("2025-P")       — five rows, a real label
#   semester_rows("2026-P", 4)    — four rows, still a stray
#
# A label only becomes a picker value (or the default) once it
# carries MIN_SEMESTER_LESSONS rows, so most tests here need a
# cheap way over — or deliberately under — that line.
#
# Used by:
#   - the semester default, threshold and filter-sheet tests
# -----------------------------------------------------------

@pytest.fixture
def semester_rows(lesson):

    def _fill(semester, count=THRESHOLD, group="ISKS-1", day=1):
        return [lesson(semester=semester, group=group, day=day,
                       title=f"{semester} {group} #{index}")
                for index in range(count)]

    return _fill




# -----------------------------------------------------------
# info_row
# -----------------------------------------------------------
#
#   info_row(db, "contacts", blob)
#   info_row(db, "contacts", raw="{oops", lang="en")
#
# One faculty_info row written the way scraper/info_scraper.py
# writes it. `raw` bypasses the encoder for the undecodable
# cases; `scraped_at` defaults to now so the row is fresh.
#
# Used by:
#   - every overlay, borrow and updatedAt test
# -----------------------------------------------------------

def info_row(db, section, data=None, lang="lt", scraped_at=None, raw=None):
    db.execute(
        "INSERT OR REPLACE INTO faculty_info (id, lang, section, data_json, scraped_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (uuid.uuid4().hex, lang, section,
         raw if raw is not None else json.dumps(data, ensure_ascii=False),
         datetime.now(timezone.utc).isoformat() if scraped_at is None else scraped_at),
    )
    db.commit()




# -----------------------------------------------------------
# _contacts / _programs / _ago / _served
# -----------------------------------------------------------
#
# Scraped blobs in info_scraper's own shape, sized by the
# caller so a test can sit exactly on, under or over the
# overlay floors. Free of &, <, " and ' unless a test is
# about the escaping. _ago builds a stamp relative to NOW,
# because a hardcoded date drifts past SCRAPED_MAX_AGE_DAYS
# and turns every overlay test into a fallback test. _served
# is the curated handbook as the WIRE carries it: the app's
# JSON provider entity-escapes every string it serialises, so
# "Dean's Office" leaves as "Dean&#x27;s Office" and a
# comparison against the raw dict would fail on apostrophes
# alone.
#
# Used by:
#   - the overlay, borrow, staleness and fallback tests
# -----------------------------------------------------------

def _contacts(items=MIN_SCRAPED_CONTACT_ITEMS + 1, category="Nuskaityta"):
    return [{"category": category,
             "items": [{"name": f"Asmuo {i}", "phone": f"+370 37 000 0{i}",
                        "email": f"asmuo{i}@knf.vu.lt", "room": str(100 + i)}
                       for i in range(items)]}]


def _programs(count=MIN_SCRAPED_PROGRAMS + 1):
    return [{"name": f"Nuskaityta programa {i}", "degree": "Bakalauras", "duration": "4 metai"}
            for i in range(count)]


def _ago(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


def _served(value):
    if isinstance(value, str):
        return html.escape(value, quote=True)
    if isinstance(value, list):
        return [_served(item) for item in value]
    if isinstance(value, dict):
        return {key: _served(item) for key, item in value.items()}

    return value




# -----------------------------------------------------------
# _lessons / _ids / _bulk / _score
# -----------------------------------------------------------
#
# The unwrappings the assertions start from. _bulk writes more
# lessons than the page cap in one transaction — one
# connection per row is the slow way to 501 rows. _score runs
# the ranking fragment through real SQLite over one synthetic
# row, which is the only way a formula-shaped constant can be
# proven at all.
# -----------------------------------------------------------

def _lessons(response):
    return response.get_json()["lessons"]


def _ids(response):
    return [row["id"] for row in _lessons(response)]


def _bulk(app, count, semester="2025-R", group="ISKS-1", day=1):
    rows = [(f"eile-{index:05d}", f"Paskaita {index:05d}", "Dest.", "301",
             f"{8 + index // 60:02d}:{index % 60:02d}", "23:59", day, group, semester)
            for index in range(count)]

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


def _score(db, published_at, likes=0, comments=0, shares=0, now=None):
    pinned = now is not None
    sql = ("SELECT " + FEED_SCORE_SQL.format(now="?" if pinned else "'now'") +
           " FROM (SELECT ? AS published_at, ? AS likes_count,"
           " ? AS comments_count, ? AS shares_count)")
    params = ((now,) if pinned else ()) + (published_at, likes, comments, shares)

    return db.execute(sql, params).fetchone()[0]




# -----------------------------------------------------------
# _cldr_form
# -----------------------------------------------------------
#
# The Lithuanian cardinal rule written the OTHER way round —
# the last two digits decide first, then the last one — so the
# table test below compares lt_plural against a rule that is
# not simply a copy of the implementation.
#
# Used by:
#   - test_the_whole_cldr_table_agrees_with_the_rule
# -----------------------------------------------------------

def _cldr_form(n):
    n = abs(n)

    if 11 <= n % 100 <= 19:
        return 2
    if n % 10 == 1:
        return 0
    if n % 10 == 0:
        return 2

    return 1








# ===========================================================
# lt_plural — the three cardinal forms
#
# The "other" arm is the one line of this slice the whole
# 12 000-test suite never executed: every scraper test that
# reaches the push copy happens to carry a count of 1 or 2.
# ===========================================================


def test_one_takes_the_singular_form():
    assert lt_plural(1, STRAIPSNIS) == "naujas straipsnis"


@pytest.mark.parametrize("n", [21, 31, 41, 51, 61, 71, 81, 91, 101, 121, 1001])
def test_a_count_ending_in_one_is_singular_again(n):
    assert lt_plural(n, STRAIPSNIS) == STRAIPSNIS[0]


@pytest.mark.parametrize("n", [2, 3, 4, 5, 6, 7, 8, 9])
def test_two_through_nine_take_the_few_form(n):
    assert lt_plural(n, STRAIPSNIS) == "nauji straipsniai"


@pytest.mark.parametrize("n", [22, 23, 24, 25, 26, 27, 28, 29, 102, 1002])
def test_a_count_ending_in_two_through_nine_stays_few(n):
    assert lt_plural(n, STRAIPSNIS) == STRAIPSNIS[1]


def test_zero_takes_the_other_form():
    assert lt_plural(0, STRAIPSNIS) == "naujų straipsnių"


@pytest.mark.parametrize("n", [10, 11, 12, 13, 14, 15, 16, 17, 18, 19])
def test_every_teen_takes_the_other_form(n):
    # The arm no other test in the suite reaches: 11 would be
    # "one" and 12-19 would be "few" on the last digit alone,
    # which is exactly what the %100 guards are for
    assert lt_plural(n, STRAIPSNIS) == STRAIPSNIS[2]


@pytest.mark.parametrize("n", [20, 30, 40, 50, 60, 70, 80, 90, 100, 1000])
def test_every_round_ten_takes_the_other_form(n):
    assert lt_plural(n, STRAIPSNIS) == STRAIPSNIS[2]


@pytest.mark.parametrize("n", [111, 211, 1011, 100011])
def test_a_hundred_and_eleven_is_other_not_singular(n):
    assert lt_plural(n, STRAIPSNIS) == STRAIPSNIS[2]


@pytest.mark.parametrize("n", [112, 113, 114, 115, 116, 117, 118, 119, 1013])
def test_a_hundred_and_twelve_is_other_not_few(n):
    assert lt_plural(n, STRAIPSNIS) == STRAIPSNIS[2]


@pytest.mark.parametrize("n,index", [(-1, 0), (-21, 0), (-2, 1), (-9, 1),
                                     (-11, 2), (-10, 2), (-100, 2)])
def test_a_negative_count_is_folded_to_its_magnitude(n, index):
    assert lt_plural(n, STRAIPSNIS) == STRAIPSNIS[index]


def test_the_whole_cldr_table_agrees_with_the_rule():
    forms = ("one", "few", "other")

    assert all(lt_plural(n, forms) == forms[_cldr_form(n)] for n in range(0, 2001))


def test_the_table_holds_for_negative_counts_too():
    forms = ("one", "few", "other")

    assert all(lt_plural(-n, forms) == forms[_cldr_form(n)] for n in range(0, 501))


@pytest.mark.parametrize("n,index", [(10**18, 2), (10**18 + 1, 0), (10**18 + 2, 1),
                                     (10**18 + 11, 2)])
def test_a_count_past_a_quintillion_still_declines(n, index):
    assert lt_plural(n, STRAIPSNIS) == STRAIPSNIS[index]


def test_the_chosen_form_is_the_callers_own_object():
    forms = (["vienas"], ["keli"], ["daug"])

    assert lt_plural(5, forms) is forms[1]


def test_a_list_of_forms_works_exactly_like_a_tuple():
    assert lt_plural(0, ["a", "b", "c"]) == "c"


@pytest.mark.parametrize("n,index", [(True, 0), (False, 2)])
def test_booleans_decline_as_the_integers_they_are(n, index):
    assert lt_plural(n, STRAIPSNIS) == STRAIPSNIS[index]


def test_a_whole_float_declines_like_its_integer():
    # No caller sends one — the annotation says int — but the
    # arithmetic is the same and 1.0 must not fall to "other"
    assert lt_plural(1.0, STRAIPSNIS) == STRAIPSNIS[0]


def test_the_forms_are_never_mutated():
    forms = ("vienas", "keli", "daug")

    for n in range(0, 40):
        lt_plural(n, forms)

    assert forms == ("vienas", "keli", "daug")


def test_a_two_form_tuple_raises_rather_than_mis_declining():
    # The signature asks for three; the "other" arm is the one
    # that would silently read past the end
    with pytest.raises(IndexError):
        lt_plural(0, ("vienas", "keli"))


def test_the_push_copy_reads_as_lithuanian_at_every_boundary():
    sentences = [f"{n} {lt_plural(n, STRAIPSNIS)}" for n in (0, 1, 2, 10, 11, 21, 22, 100, 101)]

    assert sentences == [
        "0 naujų straipsnių",
        "1 naujas straipsnis",
        "2 nauji straipsniai",
        "10 naujų straipsnių",
        "11 naujų straipsnių",
        "21 naujas straipsnis",
        "22 nauji straipsniai",
        "100 naujų straipsnių",
        "101 naujas straipsnis",
    ]








# ===========================================================
# parse_pagination — the arms the broad suite leaves open
# ===========================================================


def test_the_refusal_carries_its_status_in_the_tuple_not_on_the_response(app):
    _page, _per_page, err = _parse(app, "?page=0")
    response, status = err

    # Flask applies the tuple's 400 when the route returns
    # `err`; the object itself is still a fresh 200 response
    assert status == 400
    assert response.status_code == 200


def test_a_refusal_answers_exactly_one_error_key(app):
    _page, _per_page, err = _parse(app, "?per_page=0")

    assert set(err[0].get_json()) == {"error"}


def test_the_two_guards_do_not_share_their_wording(app):
    assert _refusal(_parse(app, "?page=x")) == "page must be a positive integer"
    assert _refusal(_parse(app, "?per_page=x")) == "per_page must be a positive integer"


def test_an_absent_page_never_meets_the_page_cap(app):
    # The cap check lives inside the "?page was given" branch,
    # so a caller pinning max_page=0 still gets page 1 rather
    # than a 400 nobody could avoid
    assert _parse(app, max_page=0) == (1, 20, None)


def test_an_absent_per_page_never_meets_its_own_cap(app):
    assert _parse(app, max_per_page=1, default_per_page=500) == (1, 500, None)


def test_a_default_per_page_of_zero_is_handed_back_untouched(app):
    assert _parse(app, default_per_page=0) == (1, 0, None)


@pytest.mark.parametrize("raw,expected", [("%0A3%0A", 3), ("%094%09", 4),
                                          ("%20%205", 5), ("6%20%20", 6)])
def test_surrounding_whitespace_of_every_kind_is_accepted(app, raw, expected):
    assert _parse(app, f"?page={raw}") == (expected, 20, None)


def test_a_unicode_digit_is_accepted_as_a_page(app):
    # int() takes any Unicode decimal digit; the schedule's own
    # parser refuses the same string (see the ASCII-digit tests
    # below) — the two parsers disagree on purpose and this
    # pins both sides of it
    assert _parse(app, "?page=%D9%A3") == (3, 20, None)


def test_a_full_width_digit_is_accepted_as_a_page(app):
    assert _parse(app, "?per_page=%EF%BC%97") == (1, 7, None)


def test_a_page_of_exactly_the_interpreter_digit_limit_is_a_number(app):
    # CPython refuses to parse an int from more than 4300
    # digits; at exactly the limit it is a real number and
    # meets the CAP guard, message and all
    assert _refusal(_parse(app, "?page=" + "9" * 4300)) == "page must be at most 10000"


def test_a_page_past_the_interpreter_digit_limit_is_refused_as_a_non_integer(app):
    # One digit more and int() raises ValueError instead — the
    # guard that keeps a 5 000-character query string from
    # becoming an unhandled 500
    assert _refusal(_parse(app, "?page=" + "9" * 4301)) == "page must be a positive integer"


def test_a_per_page_past_the_digit_limit_is_refused_too(app):
    assert _refusal(_parse(app, "?per_page=" + "1" * 5000)) == "per_page must be a positive integer"


def test_a_thousand_digit_page_is_refused_by_the_cap_not_the_parser(app):
    assert _refusal(_parse(app, "?page=" + "7" * 1000)) == "page must be at most 10000"


def test_a_caller_can_widen_the_page_cap_as_far_as_it_likes(app):
    assert _parse(app, "?page=999999999", max_page=10**9) == (999999999, 20, None)


def test_both_bounds_at_once_are_accepted_at_their_maxima(app):
    assert _parse(app, "?page=10000&per_page=50") == (10000, 50, None)


def test_one_past_each_bound_is_refused_with_that_bounds_message(app):
    assert _refusal(_parse(app, "?page=10001")) == "page must be at most 10000"
    assert _refusal(_parse(app, "?per_page=51")) == "per_page must be at most 50"


def test_the_shared_defaults_are_the_documented_numbers(app):
    # 50/20/10 000 — swagger's published maximum, the feed page
    # size and the OFFSET ceiling
    assert _parse(app, "?page=10000&per_page=50") == (10000, 50, None)
    assert _parse(app) == (1, 20, None)
    assert _refusal(_parse(app, "?per_page=51")).endswith("50")


@pytest.mark.parametrize("query", ["?page=1&per_page=abc", "?page=abc&per_page=1",
                                   "?page=abc&per_page=abc"])
def test_any_bad_number_discards_both_numbers(app, query):
    page, per_page, err = _parse(app, query)

    assert (page, per_page) == (None, None)
    assert err is not None








# ===========================================================
# The shared post-body limits and FEED_SCORE_SQL
# ===========================================================


def test_every_shared_limit_is_a_positive_int():
    assert all(isinstance(value, int) and value > 0
               for value in (MAX_TITLE_LENGTH, MAX_CONTENT_LENGTH, SUMMARY_LENGTH))


def test_a_summary_is_shorter_than_the_body_it_trims_and_the_title_it_follows():
    assert SUMMARY_LENGTH < MAX_CONTENT_LENGTH
    assert SUMMARY_LENGTH <= MAX_TITLE_LENGTH


def test_a_brand_new_post_scores_exactly_one_hundred(db):
    assert _score(db, "2026-01-08T00:00:00+00:00", now="2026-01-08T00:00:00+00:00") == pytest.approx(100.0)


def test_a_day_old_post_keeps_half_the_recency_term(db):
    assert _score(db, "2026-01-07T00:00:00+00:00", now="2026-01-08T00:00:00+00:00") == pytest.approx(50.0)


def test_a_three_day_old_post_keeps_a_quarter(db):
    assert _score(db, "2026-01-05T00:00:00+00:00", now="2026-01-08T00:00:00+00:00") == pytest.approx(25.0)


@pytest.mark.parametrize("likes,comments,shares,expected", [(1, 0, 0, 0.5), (0, 1, 0, 1.0),
                                                            (0, 0, 1, 1.5), (2, 3, 4, 10.0)])
def test_the_engagement_weights_are_one_two_and_three(db, likes, comments, shares, expected):
    fresh = _score(db, "2026-01-08T00:00:00+00:00", likes=likes, comments=comments,
                   shares=shares, now="2026-01-08T00:00:00+00:00")

    assert fresh == pytest.approx(100.0 + expected)


@pytest.mark.parametrize("likes,expected", [(99, 49.5), (100, 50.0), (101, 50.0), (10_000, 50.0)])
def test_engagement_is_capped_at_a_hundred_before_it_is_halved(db, likes, expected):
    # A century of age leaves the recency term under 0.01, so
    # what is left is the engagement half alone
    aged = _score(db, "2026-01-01T00:00:00+00:00", likes=likes, now="2126-01-01T00:00:00+00:00")

    assert aged == pytest.approx(expected, abs=0.01)


def test_the_score_can_never_pass_a_hundred_and_fifty(db):
    best = _score(db, "2026-01-08T00:00:00+00:00", likes=10**6, comments=10**6, shares=10**6,
                  now="2026-01-08T00:00:00+00:00")

    assert best == pytest.approx(150.0)


def test_a_fresh_post_and_a_saturated_day_old_one_score_exactly_the_same(db):
    # The banner says a brand new post "always outranks" a
    # day-old one; at exactly one day and saturated engagement
    # it is a TIE and the query's next ORDER BY key decides
    fresh = _score(db, "2026-01-08T00:00:00+00:00", now="2026-01-08T00:00:00+00:00")
    day_old = _score(db, "2026-01-07T00:00:00+00:00", likes=100,
                     now="2026-01-08T00:00:00+00:00")

    assert fresh == pytest.approx(day_old)


def test_an_hour_old_post_loses_to_a_saturated_day_old_one(db):
    # ...and one hour later the fresh post is behind: recency
    # alone is 96, engagement is worth up to 50
    hour_old = _score(db, "2026-01-07T23:00:00+00:00", now="2026-01-08T00:00:00+00:00")
    day_old = _score(db, "2026-01-07T00:00:00+00:00", likes=100,
                     now="2026-01-08T00:00:00+00:00")

    assert hour_old < day_old


def test_a_negative_counter_drags_the_score_below_zero(db):
    # MIN() has no floor under it — nothing writes a negative
    # counter today, but a reconciliation bug would rank the
    # row below every unpublished draft rather than at 0
    assert _score(db, "2026-01-08T00:00:00+00:00", likes=-1000,
                  now="2026-01-08T00:00:00+00:00") < 0


def test_a_null_published_at_scores_only_its_engagement(db):
    sql = ("SELECT " + FEED_SCORE_SQL.format(now="'now'") +
           " FROM (SELECT NULL AS published_at, 10 AS likes_count,"
           " 0 AS comments_count, 0 AS shares_count)")

    assert db.execute(sql).fetchone()[0] == pytest.approx(5.0)


def test_the_fragment_has_exactly_one_now_hole(db):
    assert FEED_SCORE_SQL.count("{now}") == 1
    assert FEED_SCORE_SQL.format(now="?").count("?") == 1
    assert FEED_SCORE_SQL.format(now="'now'").count("?") == 0


def test_the_unformatted_fragment_is_not_valid_sql(db):
    with pytest.raises(sqlite3.OperationalError):
        db.execute("SELECT " + FEED_SCORE_SQL + " FROM (SELECT 1 AS published_at)")








# ===========================================================
# schedule — _parse_count as a unit
# ===========================================================


def test_an_absent_count_takes_the_default_without_clamping(app):
    with app.app_context():
        assert _parse_count(None, "limit", 999, 1, 500) == (999, None)


def test_a_count_below_the_minimum_is_clamped_up(app):
    with app.app_context():
        assert _parse_count("0", "limit", 500, 1, 500) == (1, None)


def test_a_count_above_the_maximum_is_clamped_down(app):
    with app.app_context():
        assert _parse_count("999999999", "limit", 500, 1, 500) == (500, None)


def test_a_count_inside_the_range_comes_back_as_an_int(app):
    with app.app_context():
        value, err = _parse_count("017", "offset", 0, 0, MAX_OFFSET)

    assert (value, err) == (17, None)
    assert isinstance(value, int)


def test_both_ends_of_the_range_are_kept_as_they_are(app):
    with app.app_context():
        assert _parse_count("1", "limit", 500, 1, 500) == (1, None)
        assert _parse_count("500", "limit", 500, 1, 500) == (500, None)


def test_nine_digits_are_a_number_and_ten_are_not(app):
    with app.app_context():
        assert _parse_count("9" * 9, "limit", 500, 1, 500) == (500, None)
        value, err = _parse_count("9" * 10, "limit", 500, 1, 500)

    assert value is None
    assert err[1] == 400


@pytest.mark.parametrize("raw", ["", " ", "1 ", " 1", "+1", "-1", "1_0", "1.0", "1e3",
                                 "٣", "７", "0x10", "abc", "1,2", "\n1"])
def test_every_spelling_int_would_have_swallowed_is_refused(app, raw):
    with app.app_context():
        value, err = _parse_count(raw, "limit", 500, 1, 500)

    assert value is None
    assert err[1] == 400


def test_the_refusal_names_the_parameter_it_read(app):
    with app.app_context():
        _value, limit_err = _parse_count("x", "limit", 500, 1, 500)
        _value, offset_err = _parse_count("x", "offset", 0, 0, MAX_OFFSET)

    assert "limit" in html.unescape(limit_err[0].get_json()["error"])
    assert "offset" in html.unescape(offset_err[0].get_json()["error"])


def test_the_refusal_is_a_ready_made_tuple_with_a_single_error_key(app):
    with app.app_context():
        _value, err = _parse_count("x", "limit", 500, 1, 500)

    assert set(err[0].get_json()) == {"error"}
    assert err[1] == 400








# ===========================================================
# schedule — _semester_options and _table_version
# ===========================================================


def test_an_empty_table_offers_no_semester_and_fingerprints_as_zero(db):
    assert _semester_options(db) == []
    assert _table_version(db) == "0:-"


def test_the_fingerprint_counts_rows_and_carries_the_newest_stamp(db, lesson):
    lesson(created_at="2026-01-01T00:00:00")
    lesson(created_at="2026-06-01T00:00:00", time_start="09:00")

    assert _table_version(db) == "2:2026-06-01T00:00:00"


def test_a_label_needs_the_threshold_before_it_is_offered(db, semester_rows):
    semester_rows("2025-R", THRESHOLD - 1)

    assert _semester_options(db) == []

    semester_rows("2025-R", 1, group="ISKS-9")

    assert _semester_options(db) == ["2025-R"]


def test_the_options_are_newest_first_and_case_folded(db, semester_rows):
    semester_rows("2019-R")
    semester_rows("2025-pavasaris")
    semester_rows("2025-R")

    assert _semester_options(db) == ["2025-R", "2025-pavasaris", "2019-R"]


def test_a_blank_or_missing_label_is_never_an_option(db, semester_rows, lesson):
    semester_rows("")
    for index in range(THRESHOLD):
        lesson(semester=None, title=f"Be semestro {index}")

    assert _semester_options(db) == []








# ===========================================================
# GET /api/schedule — the guards and what they must not carry
# ===========================================================


def test_a_ten_digit_day_is_refused_as_a_non_integer_not_as_a_range(client):
    response = client.get(f"{SCHEDULE}?day={'0' * 10}")
    message = html.unescape(response.get_json()["error"])

    assert response.status_code == 400
    assert "must be an integer" in message


def test_a_seven_zero_day_is_still_monday(client, lesson):
    lesson(day=0)
    lesson(day=3, time_start="12:00")

    assert len(_lessons(client.get(f"{SCHEDULE}?day=0000000"))) == 1


def test_a_unicode_digit_day_is_refused_though_pagination_would_take_it(client):
    assert client.get(f"{SCHEDULE}?day=%D9%A3").status_code == 400


@pytest.mark.parametrize("query", ["day=9", "day=abc", "limit=x", "offset=-1"])
def test_a_refused_read_is_never_cacheable(client, query):
    response = client.get(f"{SCHEDULE}?{query}")

    assert response.status_code == 400
    assert "ETag" not in response.headers
    assert response.headers["Cache-Control"] == "no-store"


def test_the_day_guard_runs_before_the_semester_default_touches_the_database(client, semester_rows):
    semester_rows("2025-R")

    assert client.get(f"{SCHEDULE}?day=7&semester=2025-R").status_code == 400


def test_a_bad_offset_is_refused_after_a_good_limit(client):
    response = client.get(f"{SCHEDULE}?limit=10&offset=-5")

    assert response.status_code == 400
    assert "offset" in html.unescape(response.get_json()["error"])








# ===========================================================
# GET /api/schedule — the filters themselves
# ===========================================================


def test_a_group_is_matched_exactly_and_never_stripped(client, lesson):
    lesson(group="ISKS-1")

    assert _lessons(client.get(f"{SCHEDULE}?group=ISKS-1")) != []
    assert _lessons(client.get(f"{SCHEDULE}?group=%20ISKS-1%20")) == []


def test_a_semester_is_matched_exactly_and_never_stripped(client, semester_rows):
    semester_rows("2025-R")

    assert _lessons(client.get(f"{SCHEDULE}?semester=2025-R")) != []
    assert _lessons(client.get(f"{SCHEDULE}?semester=%202025-R%20")) == []


def test_a_semester_literally_named_all_can_never_be_selected(client, semester_rows):
    # ?semester=all is the every-semester opt-out, so a label
    # the scraper spelled "all" is reachable by nothing
    semester_rows("all")
    semester_rows("2025-R")

    served = {row["semester"] for row in _lessons(client.get(f"{SCHEDULE}?semester=all"))}

    assert served == {"all", "2025-R"}


@pytest.mark.parametrize("raw", ["all", "ALL", "%20all%20", "aLl", "%09all%0a"])
def test_every_spelling_of_all_opts_out_of_the_default(client, semester_rows, raw):
    semester_rows("2024-R")
    semester_rows("2025-R")

    served = {row["semester"] for row in _lessons(client.get(f"{SCHEDULE}?semester={raw}"))}

    assert served == {"2024-R", "2025-R"}


def test_a_group_carrying_a_nul_byte_answers_an_empty_page(client, lesson):
    lesson(group="ISKS-1")
    response = client.get(f"{SCHEDULE}?group=ISKS-1%00")

    assert response.status_code == 200
    assert _lessons(response) == []


def test_an_enormous_group_value_answers_an_empty_page(client, lesson):
    lesson(group="ISKS-1")
    response = client.get(f"{SCHEDULE}?group={'A' * 8000}")

    assert response.status_code == 200
    assert _lessons(response) == []


def test_an_empty_group_name_row_can_never_be_filtered_to(client, lesson):
    # `if group:` treats "" as "no filter", so the row the
    # filter sheet DOES offer (see the sheet tests) is not
    # selectable through the timetable read
    lesson(group="", title="Be grupes")
    lesson(group="ISKS-1")

    assert len(_lessons(client.get(f"{SCHEDULE}?group="))) == 2


def test_every_filter_and_both_page_bounds_at_once(client, lesson, semester_rows):
    semester_rows("2025-R", THRESHOLD, group="ISKS-1", day=1)
    wanted = lesson(day=1, group="ISKS-1", semester="2025-R", time_start="23:00",
                    title="Paskutine")

    response = client.get(f"{SCHEDULE}?day=1&group=ISKS-1&semester=2025-R&limit=1&offset=5")

    assert _ids(response) == [wanted]








# ===========================================================
# GET /api/schedule — the page and its order
# ===========================================================


def test_an_unpadded_hour_sorts_after_a_padded_ten(client, lesson):
    # time_start is TEXT and the ORDER BY is a text sort: it is
    # chronological only because every writer zero-pads
    early = lesson(time_start="9:00", title="Nepapildyta")
    late = lesson(time_start="10:00", title="Papildyta")

    assert _ids(client.get(SCHEDULE)) == [late, early]


def test_a_page_of_exactly_the_cap_is_served_whole(client, app):
    _bulk(app, CAP)

    assert len(_lessons(client.get(SCHEDULE))) == CAP


def test_the_row_past_the_cap_is_only_reachable_through_offset(client, app):
    ids = _bulk(app, CAP + 1)

    assert _ids(client.get(SCHEDULE)) == ids[:CAP]
    assert _ids(client.get(f"{SCHEDULE}?offset={CAP}")) == ids[CAP:]


def test_an_offset_at_its_ceiling_answers_an_empty_page(client, lesson):
    lesson()

    response = client.get(f"{SCHEDULE}?offset={MAX_OFFSET}")

    assert response.status_code == 200
    assert _lessons(response) == []


def test_an_offset_above_the_ceiling_is_clamped_not_refused(client, lesson):
    lesson()

    assert client.get(f"{SCHEDULE}?offset=999999999").status_code == 200


def test_the_default_semester_is_chosen_before_the_page_is_cut(client, semester_rows):
    semester_rows("2024-R", 10)
    semester_rows("2025-R", 3)

    # 2025-R is under the threshold, so the newest REAL label
    # is 2024-R and the limit applies to that label's rows
    served = {row["semester"] for row in _lessons(client.get(f"{SCHEDULE}?limit=2"))}

    assert served == {"2024-R"}


def test_an_empty_table_still_answers_a_cacheable_envelope(client):
    response = client.get(SCHEDULE)

    assert response.get_json() == {"lessons": []}
    assert response.headers["Cache-Control"] == SCHEDULE_CACHE
    assert response.headers["ETag"].startswith("W/")








# ===========================================================
# GET /api/schedule — the conditional answer
# ===========================================================


def test_a_matching_tag_among_several_answers_304(client, lesson):
    lesson()
    tag = client.get(SCHEDULE).headers["ETag"]

    response = client.get(SCHEDULE, headers={"If-None-Match": f'W/"kitas", {tag}, W/"trecias"'})

    assert response.status_code == 304


def test_a_304_carries_no_body_at_all(client, lesson):
    lesson()
    tag = client.get(SCHEDULE).headers["ETag"]

    response = client.get(SCHEDULE, headers={"If-None-Match": tag})

    assert response.get_data() == b""
    assert response.headers["Cache-Control"] == SCHEDULE_CACHE


def test_a_head_request_with_a_matching_tag_is_a_304(client, lesson):
    lesson()
    tag = client.get(SCHEDULE).headers["ETag"]

    assert client.head(SCHEDULE, headers={"If-None-Match": tag}).status_code == 304


def test_the_tag_does_not_depend_on_the_order_the_filters_arrived_in(client, lesson):
    lesson(day=1, group="ISKS-1")

    first = client.get(f"{SCHEDULE}?day=1&group=ISKS-1").headers["ETag"]
    second = client.get(f"{SCHEDULE}?group=ISKS-1&day=1").headers["ETag"]

    assert first == second


def test_a_quote_inside_a_group_name_cannot_collide_two_queries(client, lesson):
    # The seed is a repr'd tuple, so a value carrying the
    # separator characters of the OTHER encoding stays distinct
    lesson()

    first = client.get(f"{SCHEDULE}?group=a', 'b&semester=c").headers["ETag"]
    second = client.get(f"{SCHEDULE}?group=a&semester=b', 'c").headers["ETag"]

    assert first != second


def test_a_garbage_if_none_match_gets_the_whole_body(client, lesson):
    lesson()

    response = client.get(SCHEDULE, headers={"If-None-Match": "not-a-tag"})

    assert response.status_code == 200
    assert len(_lessons(response)) == 1








# ===========================================================
# GET /api/schedule/filters — the sheet
# ===========================================================


def test_the_sheet_carries_exactly_its_four_keys(client, semester_rows):
    semester_rows("2025-R")

    assert set(client.get(FILTERS).get_json()) == {"groups", "semesters", "days", "semesterGroups"}


def test_an_empty_string_group_name_is_offered_as_a_filter_value(client, lesson):
    # WHERE group_name IS NOT NULL keeps "" — the sheet offers a
    # value the timetable read cannot use (see the read tests)
    lesson(group="", title="Be grupes")
    lesson(group="ISKS-1")

    assert client.get(FILTERS).get_json()["groups"] == ["", "ISKS-1"]


def test_a_null_group_name_is_not_offered(client, lesson):
    lesson(group=None, title="Nera grupes")
    lesson(group="ISKS-1")

    assert client.get(FILTERS).get_json()["groups"] == ["ISKS-1"]


def test_a_scope_below_the_threshold_still_lists_its_own_groups(client, semester_rows):
    # The label is too small for the picker, yet scoping to it
    # by hand still answers its groups and days
    semester_rows("2025-R", THRESHOLD - 1, group="ISKS-7", day=4)

    sheet = client.get(f"{FILTERS}?semester=2025-R").get_json()

    assert sheet["semesters"] == []
    assert sheet["groups"] == ["ISKS-7"]
    assert sheet["days"] == [4]


def test_a_repeated_scope_uses_the_first_value(client, semester_rows):
    semester_rows("2024-R", group="VADY-1")
    semester_rows("2025-R", group="ISKS-1")

    sheet = client.get(f"{FILTERS}?semester=2024-R&semester=2025-R").get_json()

    assert sheet["groups"] == ["VADY-1"]


def test_the_scope_is_an_exact_match_not_a_prefix(client, semester_rows):
    semester_rows("2025-R", group="ISKS-1")

    assert client.get(f"{FILTERS}?semester=2025").get_json()["groups"] == []


def test_the_sheet_ignores_every_parameter_but_the_scope(client, semester_rows):
    semester_rows("2025-R", group="ISKS-1", day=2)

    plain = client.get(FILTERS).get_json()
    noisy = client.get(f"{FILTERS}?day=6&limit=1&offset=99&group=ISKS-9&page=4").get_json()

    assert plain == noisy


def test_a_group_is_listed_once_per_semester_however_many_lessons_it_has(client, semester_rows):
    semester_rows("2025-R", 8, group="ISKS-1")

    pairs = client.get(FILTERS).get_json()["semesterGroups"]

    assert pairs == [{"semester": "2025-R", "groups": ["ISKS-1"]}]


def test_two_semesters_sharing_a_group_each_list_it(client, semester_rows):
    semester_rows("2024-R", group="ISKS-1")
    semester_rows("2025-R", group="ISKS-1")

    pairs = client.get(FILTERS).get_json()["semesterGroups"]

    assert pairs == [{"semester": "2025-R", "groups": ["ISKS-1"]},
                     {"semester": "2024-R", "groups": ["ISKS-1"]}]


def test_the_groups_inside_a_pair_are_sorted_by_name(client, semester_rows):
    semester_rows("2025-R", group="VADY-2")
    semester_rows("2025-R", group="ISKS-1")
    semester_rows("2025-R", group="SOCD-3")

    pairs = client.get(FILTERS).get_json()["semesterGroups"]

    assert pairs[0]["groups"] == ["ISKS-1", "SOCD-3", "VADY-2"]


def test_the_pairs_follow_the_semester_order_newest_first(client, semester_rows):
    for label in ("2019-R", "2025-R", "2021-R"):
        semester_rows(label, group=f"G{label[:4]}")

    sheet = client.get(FILTERS).get_json()

    assert [pair["semester"] for pair in sheet["semesterGroups"]] == sheet["semesters"]
    assert sheet["semesters"] == ["2025-R", "2021-R", "2019-R"]


def test_the_sheet_answers_304_to_its_own_tag_and_200_to_the_timetables(client, semester_rows):
    semester_rows("2025-R")
    sheet_tag = client.get(FILTERS).headers["ETag"]
    schedule_tag = client.get(SCHEDULE).headers["ETag"]

    assert client.get(FILTERS, headers={"If-None-Match": sheet_tag}).status_code == 304
    assert client.get(FILTERS, headers={"If-None-Match": schedule_tag}).status_code == 200


def test_the_sheets_public_max_age_survives_the_global_no_store_hook(client):
    # add_security_headers setdefaults no-store on every /api/
    # answer and the sheet's own value has to win — and the
    # hook's HTTP/1.0 Pragma: no-cache stays away, since it
    # would tell an old cache the exact opposite
    response = client.get(FILTERS)

    assert response.headers["Cache-Control"] == SCHEDULE_CACHE
    assert "no-store" not in response.headers["Cache-Control"]
    assert "Pragma" not in response.headers








# ===========================================================
# GET /api/info — ?lang normalisation
# ===========================================================


@pytest.mark.parametrize("raw", ["en", "EN", "En", "en-GB", "EN-gb", "en_GB", "en-Latn-GB",
                                 "en-", "%20en%20", "eN-us"])
def test_every_spelling_that_still_means_english(client, raw):
    assert client.get(f"{INFO}?lang={raw}").get_json()["lang"] == "en"


@pytest.mark.parametrize("raw", ["", "%20", "-en", "_en", "___", "xx", "123", "e",
                                 "english", "l" * 500, "lt", "LT", "lt_LT"])
def test_everything_else_serves_lithuanian_and_says_so(client, raw):
    assert client.get(f"{INFO}?lang={raw}").get_json()["lang"] == "lt"


def test_a_rejected_language_still_gets_the_lithuanian_dataset(client):
    handbook = client.get(f"{INFO}?lang=de").get_json()

    assert handbook["lang"] == "lt"
    assert handbook["programs"] == client.get(INFO).get_json()["programs"]


def test_the_language_decides_before_the_section_gate(client):
    # ?section is checked against the payload's own keys, which
    # are the same in both languages — a bad lang cannot turn a
    # good section into a 400
    assert client.get(f"{INFO}?lang=xx&section=faq").status_code == 200








# ===========================================================
# GET /api/info — ?section
# ===========================================================


@pytest.mark.parametrize("section", ["contacts", "links", "hours", "programs", "faq"])
def test_a_section_answer_is_that_section_plus_the_language(client, section):
    payload = client.get(f"{INFO}?section={section}").get_json()

    assert set(payload) == {section, "lang"}
    assert payload[section] == client.get(INFO).get_json()[section]


def test_a_repeated_section_uses_the_first_value(client):
    assert set(client.get(f"{INFO}?section=faq&section=links").get_json()) == {"faq", "lang"}


@pytest.mark.parametrize("section", ["Contacts", "staff", "general_contact", "lang",
                                     "updatedAt", "contacts ", "faq%00"])
def test_an_unknown_section_is_a_two_key_refusal(client, section):
    response = client.get(f"{INFO}?section={section}")

    assert response.status_code == 400
    assert response.get_json() == {"error": "Unknown section", "code": "unknown_section"}


def test_a_refused_section_carries_no_tag_and_is_never_cached(client):
    response = client.get(f"{INFO}?section=nera")

    assert "ETag" not in response.headers
    assert response.headers["Cache-Control"] == "no-store"


def test_a_section_and_the_whole_handbook_never_share_a_tag(client):
    whole = client.get(INFO).headers["ETag"]
    part = client.get(f"{INFO}?section=faq").headers["ETag"]

    assert whole != part
    assert client.get(f"{INFO}?section=faq", headers={"If-None-Match": whole}).status_code == 200


def test_a_section_answer_is_conditional_like_any_other(client):
    tag = client.get(f"{INFO}?section=hours").headers["ETag"]

    assert client.get(f"{INFO}?section=hours", headers={"If-None-Match": tag}).status_code == 304


def test_general_contact_becomes_addressable_only_after_a_scrape(client, db):
    assert client.get(f"{INFO}?section=general_contact").status_code == 400

    info_row(db, "general_contact", {"email": "knf@knf.vu.lt"})
    payload = client.get(f"{INFO}?section=general_contact").get_json()

    assert payload["general_contact"] == {"email": "knf@knf.vu.lt"}
    assert set(payload) == {"general_contact", "lang", "updatedAt"}








# ===========================================================
# GET /api/info — the scraped overlay, from the wrong side
# ===========================================================


def test_a_category_whose_items_are_a_dict_counts_for_nothing(client, db):
    info_row(db, "contacts", [{"category": "Dekanatas", "items": {"name": "Ona"}}])

    assert client.get(INFO).get_json()["contacts"] == _served(FACULTY_INFO["lt"]["contacts"])


@pytest.mark.parametrize("blob", [{"category": "Dekanatas"}, 7, "kontaktai", True])
def test_a_contacts_blob_that_is_not_a_list_at_all_keeps_the_curated_list(client, db, blob):
    # The mobile FacultyInfoResponse types contacts as an array
    # and maps over it: a dict here used to crash the Info
    # screen outright
    info_row(db, "contacts", blob)

    assert client.get(INFO).get_json()["contacts"] == _served(FACULTY_INFO["lt"]["contacts"])


def test_a_rejected_contacts_shape_names_the_type_once_per_process(client, db, caplog):
    info_row(db, "contacts", {"category": "Dekanatas"})

    with caplog.at_level(logging.WARNING, logger="app.info.routes"):
        for _ in range(3):
            client.get(INFO)

    shape = [record for record in caplog.records
             if record.name == "app.info.routes" and "not a list" in record.getMessage()]

    assert len(shape) == 1
    assert "dict" in shape[0].getMessage()


def test_a_contacts_list_of_nulls_keeps_the_curated_list(client, db):
    info_row(db, "contacts", [None, None, None, None, None, None])

    assert len(client.get(INFO).get_json()["contacts"]) == len(FACULTY_INFO["lt"]["contacts"])


def test_a_contacts_list_of_lists_keeps_the_curated_list(client, db):
    info_row(db, "contacts", [[{"name": "Ona"}] * 6])

    assert len(client.get(INFO).get_json()["contacts"]) == len(FACULTY_INFO["lt"]["contacts"])


def test_a_category_without_an_items_key_counts_for_nothing(client, db):
    info_row(db, "contacts", [{"category": "Dekanatas"}] * 9)

    assert len(client.get(INFO).get_json()["contacts"]) == len(FACULTY_INFO["lt"]["contacts"])


def test_the_contact_floor_counts_items_across_categories(client, db):
    exactly = [{"category": f"K{i}", "items": [{"name": f"Ona {i}"}]}
               for i in range(MIN_SCRAPED_CONTACT_ITEMS)]
    info_row(db, "contacts", exactly)

    assert len(client.get(INFO).get_json()["contacts"]) == MIN_SCRAPED_CONTACT_ITEMS


def test_one_item_short_of_the_floor_keeps_the_curated_list(client, db):
    short = [{"category": f"K{i}", "items": [{"name": f"Ona {i}"}]}
             for i in range(MIN_SCRAPED_CONTACT_ITEMS - 1)]
    info_row(db, "contacts", short)

    assert len(client.get(INFO).get_json()["contacts"]) == len(FACULTY_INFO["lt"]["contacts"])


@pytest.mark.parametrize("blob", [False, 0, [], {}, "", "knf@knf.vu.lt", 3.5])
def test_a_general_contact_that_is_not_a_real_object_is_dropped(client, db, blob):
    info_row(db, "general_contact", blob)

    assert "general_contact" not in client.get(INFO).get_json()


def test_a_general_contact_of_one_null_field_is_still_an_object(client, db):
    info_row(db, "general_contact", {"phone": None})

    assert client.get(INFO).get_json()["general_contact"] == {"phone": None}


@pytest.mark.parametrize("blob", [{}, {"programs": []}, 7, "programs"])
def test_a_programs_blob_of_the_wrong_shape_keeps_the_curated_list(client, db, blob):
    info_row(db, "programs", blob)

    assert client.get(INFO).get_json()["programs"] == _served(FACULTY_INFO["lt"]["programs"])


def test_the_program_floor_is_inclusive_on_both_sides(client, db):
    info_row(db, "programs", _programs(MIN_SCRAPED_PROGRAMS - 1))

    assert len(client.get(INFO).get_json()["programs"]) == len(FACULTY_INFO["lt"]["programs"])

    info_row(db, "programs", _programs(MIN_SCRAPED_PROGRAMS))

    assert len(client.get(INFO).get_json()["programs"]) == MIN_SCRAPED_PROGRAMS


def test_a_scraped_contact_name_reaches_the_wire_escaped(client, db):
    info_row(db, "contacts", [{"category": "Dekanatas",
                               "items": [{"name": "<script>alert('ok')</script>"}] * 6}])

    body = client.get(INFO).get_data(as_text=True)

    assert "<script>" not in body
    assert "&lt;script&gt;" in body


def test_a_scraped_blob_nested_past_the_escaper_fails_closed(client, db, app):
    # The output escaper caps container nesting at 32 and
    # REFUSES to ship a body it could not escape — a 500 beats
    # an unescaped handbook
    deep = current = {}
    for _ in range(60):
        current["nested"] = {}
        current = current["nested"]

    info_row(db, "general_contact", deep)
    app.config["PROPAGATE_EXCEPTIONS"] = False

    response = client.get(INFO)

    assert response.status_code == 500
    assert response.get_json() == {"error": "Internal server error"}


def test_the_overlay_leaves_the_module_level_handbook_alone(client, db):
    before = json.dumps(FACULTY_INFO, sort_keys=True)
    info_row(db, "contacts", _contacts())
    info_row(db, "programs", _programs())
    info_row(db, "general_contact", {"email": "knf@knf.vu.lt"})

    client.get(INFO)

    assert json.dumps(FACULTY_INFO, sort_keys=True) == before








# ===========================================================
# GET /api/info — the Lithuanian borrow
# ===========================================================


def test_english_borrows_the_lithuanian_scrape_when_it_has_nothing(client, db):
    info_row(db, "contacts", _contacts())

    served = client.get(f"{INFO}?lang=en").get_json()["contacts"]

    assert served[0]["category"] == "Nuskaityta"


def test_an_english_row_of_any_section_blocks_the_borrow(client, db):
    # _get_scraped_info("en") answered SOMETHING, so the route
    # never asks for 'lt' — even though the row it found is a
    # section the overlay does not know
    info_row(db, "contacts", _contacts(), lang="lt")
    info_row(db, "links", [{"title": "Nieko"}], lang="en")

    served = client.get(f"{INFO}?lang=en").get_json()

    assert served["contacts"] == _served(FACULTY_INFO["en"]["contacts"])
    assert "updatedAt" in served


def test_an_english_blob_under_its_floor_blocks_the_borrow_too(client, db):
    info_row(db, "contacts", _contacts(), lang="lt")
    info_row(db, "contacts", _contacts(1), lang="en")

    served = client.get(f"{INFO}?lang=en").get_json()["contacts"]

    assert served == _served(FACULTY_INFO["en"]["contacts"])


def test_an_english_row_that_is_only_stale_does_not_block_the_borrow(client, db):
    old = (datetime.now(timezone.utc) - timedelta(days=SCRAPED_MAX_AGE_DAYS + 1)).isoformat()
    info_row(db, "contacts", _contacts(), lang="lt")
    info_row(db, "contacts", _contacts(9), lang="en", scraped_at=old)

    served = client.get(f"{INFO}?lang=en").get_json()["contacts"]

    assert served[0]["category"] == "Nuskaityta"
    assert len(served[0]["items"]) == MIN_SCRAPED_CONTACT_ITEMS + 1


def test_lithuanian_never_borrows_from_english(client, db):
    info_row(db, "contacts", _contacts(), lang="en")

    assert client.get(INFO).get_json()["contacts"] == _served(FACULTY_INFO["lt"]["contacts"])
    assert "updatedAt" not in client.get(INFO).get_json()








# ===========================================================
# GET /api/info — timestamps, staleness and updatedAt
# ===========================================================


@pytest.mark.parametrize("value", [None, "", 0, [], {}, False])
def test_an_empty_timestamp_is_no_timestamp_at_all(value):
    assert _parse_timestamp(value) is None


def test_a_datetime_object_is_not_a_timestamp():
    # The TypeError arm of the except tuple: datetime.replace
    # takes keyword arguments, not " " and "T"
    assert _parse_timestamp(datetime(2026, 8, 29, tzinfo=timezone.utc)) is None


@pytest.mark.parametrize("value", ["2026-08-29T10:00:00.123456+00:00", "2026-08-29",
                                   "2026-08-29 10:00:00", "2026-08-29T10:00:00Z",
                                   "2026-08-29T10:00:00+03:00"])
def test_every_shape_a_writer_here_produces_is_understood(value):
    assert _parse_timestamp(value) is not None


def test_a_naive_stamp_is_read_as_utc_not_as_local_time():
    assert _parse_timestamp("2026-08-29T10:00:00") == _parse_timestamp("2026-08-29T10:00:00Z")


def test_a_stamp_from_the_future_is_never_stale(client, db):
    info_row(db, "contacts", _contacts(), scraped_at="2099-01-01T00:00:00+00:00")

    payload = client.get(INFO).get_json()

    assert payload["contacts"][0]["category"] == "Nuskaityta"
    assert payload["updatedAt"] == "2099-01-01T00:00:00+00:00"


def test_a_stamp_exactly_on_the_age_limit_is_still_served(client, db):
    edge = (datetime.now(timezone.utc) - timedelta(days=SCRAPED_MAX_AGE_DAYS)
            + timedelta(minutes=1)).isoformat()
    info_row(db, "contacts", _contacts(), scraped_at=edge)

    assert client.get(INFO).get_json()["contacts"][0]["category"] == "Nuskaityta"


def test_an_unparseable_stamp_fails_open_and_the_blob_is_served(client, db):
    info_row(db, "contacts", _contacts(), scraped_at="niekada")

    payload = client.get(INFO).get_json()

    assert payload["contacts"][0]["category"] == "Nuskaityta"
    assert payload["updatedAt"] == "niekada"


def test_updated_at_is_the_newest_stamp_of_the_surviving_rows(client, db):
    older, newer = _ago(days=20), _ago(days=2)
    info_row(db, "contacts", _contacts(), scraped_at=older)
    info_row(db, "programs", _programs(), scraped_at=newer)

    assert client.get(INFO).get_json()["updatedAt"] == newer


def test_updated_at_prefers_the_later_stamp_whatever_shape_it_was_stored_in(client, db):
    day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    earlier = (day + timedelta(hours=9)).isoformat()
    later = (day + timedelta(hours=11)).isoformat().replace("T", " ")

    info_row(db, "contacts", _contacts(), scraped_at=earlier)
    info_row(db, "programs", _programs(), scraped_at=later)

    assert client.get(INFO).get_json()["updatedAt"] == later


def test_a_new_scrape_moves_the_tag_and_a_relaunch_between_two_is_a_304(client, db):
    info_row(db, "contacts", _contacts(), scraped_at=_ago(days=2))
    first = client.get(INFO).headers["ETag"]

    assert client.get(INFO, headers={"If-None-Match": first}).status_code == 304

    info_row(db, "contacts", _contacts(), scraped_at=_ago(minutes=1))

    assert client.get(INFO, headers={"If-None-Match": first}).status_code == 200


def test_an_unreadable_table_falls_back_without_a_stamp(client, db):
    db.execute("DROP TABLE faculty_info")
    db.commit()

    payload = client.get(INFO).get_json()

    assert payload["contacts"] == _served(FACULTY_INFO["lt"]["contacts"])
    assert "updatedAt" not in payload


def test_a_connection_that_cannot_be_opened_falls_back_too(client, monkeypatch):
    def _refuse():
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr("app.info.routes.get_db", _refuse)

    assert client.get(INFO).get_json()["contacts"] == _served(FACULTY_INFO["lt"]["contacts"])


def test_an_undecodable_row_is_skipped_and_its_stamp_ignored(client, db):
    good = _ago(days=1)
    info_row(db, "contacts", raw="{ne json", scraped_at="2099-01-01T00:00:00+00:00")
    info_row(db, "programs", _programs(), scraped_at=good)

    payload = client.get(INFO).get_json()

    assert payload["updatedAt"] == good
    assert payload["contacts"] == _served(FACULTY_INFO["lt"]["contacts"])


def test_a_handbook_with_nothing_usable_left_carries_no_stamp(client, db):
    info_row(db, "contacts", raw="{ne json")

    assert "updatedAt" not in client.get(INFO).get_json()








# ===========================================================
# GET /api/info — the public contract around it
# ===========================================================


def test_the_handbook_is_the_same_bytes_for_a_guest_a_student_and_an_admin(client, actor, admin):
    _user, student_headers = actor
    _admin, admin_headers = admin

    guest = client.get(INFO).get_data()

    assert client.get(INFO, headers=student_headers).get_data() == guest
    assert client.get(INFO, headers=admin_headers).get_data() == guest
    assert client.get(INFO, headers={"Authorization": "Bearer sugadintas"}).get_data() == guest


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_the_handbook_refuses_every_write_method(client, method):
    assert getattr(client, method)(INFO).status_code == 405


def test_the_handbooks_day_long_cache_survives_the_global_no_store_hook(client):
    response = client.get(INFO)

    assert response.headers["Cache-Control"] == INFO_CACHE
    assert "no-store" not in response.headers["Cache-Control"]


def test_a_head_request_carries_the_tag_and_no_body(client):
    response = client.head(INFO)

    assert response.get_data() == b""
    assert response.headers["ETag"].startswith("W/")


