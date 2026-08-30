# -----------------------------------------------------------
#  [*] Tests — scraper/common.py, the clock and the bookkeeping
#
#  The exhaustive pass over one slice of the shared scraper
#  plumbing: run_deadline, deadline_passed, utc_now_naive,
#  parse_source_datetime, sanitise_published_at,
#  mark_run_failed, prune_scraper_runs, load_deleted_urls and
#  check_yield_drop. The broad file (test_scraper_common.py)
#  already walks the happy paths; this one walks the EDGES,
#  and what it pins is:
#
#    - the budget is monotonic, so an NTP step neither extends
#      nor ends a run, and the exact instant of the deadline
#      already counts as spent (>=, not >). inf never expires;
#      nan never expires EITHER, which is the one way a caller
#      can hand a run an unbounded budget by accident
#    - utc_now_naive is UTC and not the container's local wall
#      clock, keeps its microseconds, and never goes backwards
#    - parse_source_datetime accepts far more than the four
#      shapes the docstring names (basic-format "20260829",
#      an ISO week date, a compact "+0300" offset) and refuses
#      the ones that look closest to working: lowercase "z",
#      an offset past 24 h, February 29th of a common year
#    - sanitise_published_at converts BEFORE it clamps, so a
#      stamp that is only in the future in its own zone
#      survives; both clamp bounds are inclusive at the
#      boundary and exclusive one microsecond past it
#    - mark_run_failed closes its own connection whatever the
#      UPDATE, the commit or get_db itself does, cuts the
#      message at exactly 1000 CHARACTERS, and has no status
#      guard: it reopens a 'completed' run as failed
#    - retention keeps a run started exactly on the cutoff,
#      prunes one a microsecond older, survives a read-only
#      database, and commits whatever the caller left pending
#    - load_deleted_urls answers an empty set for every shape
#      of broken table, and collapses every spelling of one
#      article to a single tombstone key
#    - check_yield_drop needs a FULL order of magnitude, is
#      silent at exactly a tenth, and is silenced entirely by
#      a None run_id — SQL's "id != NULL" matches no row
#
#  No network and no sleeping: nothing here fetches, and every
#  clock question is answered with time_machine or by patching
#  time.monotonic outright.
# -----------------------------------------------------------


import logging
import os
import sqlite3
import time
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
import time_machine

from app.scraper import common


LOGGER_NAME = "app.scraper.common"

# The instant every timestamp test reasons from — aware UTC so
# time_machine cannot read it as local time
FROZEN = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
FROZEN_NAIVE = FROZEN.replace(tzinfo=None)

# A POSIX TZ string, not an Olson name: the test image carries
# no tzdata, and "XXX-3" means "UTC+3" in POSIX's inverted
# spelling — close enough to Vilnius summer time to prove the
# point without a zoneinfo file
VILNIUS_ISH_TZ = "XXX-3"

# Reachable from a real page: parse_source_datetime accepts
# both of these, and astimezone() then walks off the end of
# the datetime range converting them
END_OF_TIME_TEXT = "9999-12-31T23:59:59-05:00"
DAWN_OF_TIME_TEXT = "0001-01-01T00:00:00+05:00"




# -----------------------------------------------------------
# clean_module_state
# -----------------------------------------------------------
#
# common.py keeps two globals that outlive a test — the pooled
# session and the per-source push clock. Neither belongs to
# this slice, but rebinding them keeps the order tests run in
# from deciding anything here either.
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_module_state(monkeypatch):
    monkeypatch.setattr(common, "_SESSION", None)
    monkeypatch.setattr(common, "_LAST_PUSH", {})




# -----------------------------------------------------------
# run_row
# -----------------------------------------------------------
#
#   run_id = run_row("knf", status="completed", found=40)
#   run_id = run_row("vu", started_at="2026-07-30T12:00:00+00:00")
#
# One scraper_runs row written straight through the caller's
# connection: there is no route that can plant a run in an
# arbitrary state, and the bookkeeping is what is under test.
# `days_ago` places started_at relative to whatever instant
# the clock is holding; `started_at` overrides it outright,
# which is how the retention boundary tests hit the cutoff
# string to the microsecond.
# -----------------------------------------------------------

@pytest.fixture
def run_row(db):

    def _row(source, status="completed", found=0, new=0, days_ago=0.0,
             run_id=None, started_at=None, finished_at=None):
        run_id = run_id or str(uuid.uuid4())
        stamp = started_at or (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        db.execute(
            """INSERT INTO scraper_runs
               (id, source, status, articles_found, articles_new, started_at, finished_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (run_id, source, status, found, new, stamp, finished_at),
        )
        db.commit()
        return run_id

    return _row




# -----------------------------------------------------------
# local_timezone
# -----------------------------------------------------------
#
#   local_timezone(VILNIUS_ISH_TZ)
#
# Points the process's LOCAL clock somewhere that is not UTC
# for the length of one test. monkeypatch.setenv alone will
# not do it: the C library caches the zone until time.tzset()
# is called, and it has to be called again on the way out or
# every later test in this worker inherits Vilnius.
# -----------------------------------------------------------

@pytest.fixture
def local_timezone():
    previous = os.environ.get("TZ")

    def _set(name):
        os.environ["TZ"] = name
        time.tzset()

    yield _set

    if previous is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = previous
    time.tzset()




# -----------------------------------------------------------
# records_of
# -----------------------------------------------------------
#
# caplog's handler catches every logger in the process, so an
# "it said nothing" assertion has to name common.py's own
# logger or an unrelated warning from Flask will fail it.
# -----------------------------------------------------------

def records_of(caplog, level=None):
    return [r for r in caplog.records
            if r.name == LOGGER_NAME and (level is None or r.levelno == level)]




# -----------------------------------------------------------
# _BrokenConnection
# -----------------------------------------------------------
#
# Stands in for the connection get_db hands mark_run_failed,
# failing at exactly one of the three points that matter —
# the UPDATE, the commit, or nothing at all — while recording
# whether close() still ran. `responses` fakes the wire; this
# fakes the disk.
# -----------------------------------------------------------

class _BrokenConnection:

    def __init__(self, fail_on=None, error=None):
        self.fail_on = fail_on
        self.error = error or sqlite3.OperationalError("disk I/O error")
        self.closed = False
        self.executed = []

    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        if self.fail_on == "execute":
            raise self.error
        return None

    def commit(self):
        if self.fail_on == "commit":
            raise self.error

    def close(self):
        self.closed = True




# ===========================================================
# run_deadline / deadline_passed — the wall-clock budget
# ===========================================================


def test_the_budget_is_taken_from_the_monotonic_clock_and_not_the_wall_clock(monkeypatch):
    # An NTP step moves time.time() and leaves time.monotonic()
    # alone; the budget has to be built on the second one
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)
    monkeypatch.setattr(time, "time", lambda: 5_000_000.0)

    assert common.run_deadline(30) == 1030.0


def test_the_budget_is_tested_against_the_monotonic_clock_too(monkeypatch):
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)
    monkeypatch.setattr(time, "time", lambda: 5_000_000.0)

    assert common.deadline_passed(1000.5) is False
    assert common.deadline_passed(999.5) is True


def test_the_deadline_instant_itself_already_counts_as_spent(monkeypatch):
    # The comparison is >=, so the run stops ON the boundary
    # rather than one tick after it
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)

    assert common.deadline_passed(1000.0) is True
    assert common.deadline_passed(1000.000001) is False


def test_a_zero_second_budget_is_spent_the_instant_it_is_taken(monkeypatch):
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)

    assert common.deadline_passed(common.run_deadline(0)) is True


def test_a_negative_budget_is_spent_before_it_starts(monkeypatch):
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)

    assert common.run_deadline(-30) == 970.0
    assert common.deadline_passed(common.run_deadline(-30)) is True


def test_a_fractional_budget_keeps_its_fraction(monkeypatch):
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)

    assert common.run_deadline(0.25) == 1000.25
    assert common.deadline_passed(1000.25) is False


def test_a_boolean_budget_is_the_integer_it_subclasses(monkeypatch):
    # Not a shape any scraper passes, but bool IS an int and
    # the arithmetic silently accepts it rather than raising
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)

    assert common.run_deadline(True) == 1001.0
    assert common.run_deadline(False) == 1000.0


def test_an_infinite_budget_never_expires():
    assert common.deadline_passed(common.run_deadline(float("inf"))) is False


def test_a_negatively_infinite_budget_is_always_spent():
    assert common.deadline_passed(common.run_deadline(float("-inf"))) is True


def test_a_not_a_number_budget_never_expires_either():
    # Every comparison against nan is False, so a nan budget is
    # an UNBOUNDED run — the one way a caller can lose the
    # guard without an exception telling them so
    deadline = common.run_deadline(float("nan"))

    assert deadline != deadline
    assert common.deadline_passed(deadline) is False


def test_a_budget_of_a_thousand_years_is_still_a_finite_number(monkeypatch):
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)

    deadline = common.run_deadline(31_536_000_000)

    assert deadline == 31_536_001_000.0
    assert common.deadline_passed(deadline) is False


@pytest.mark.parametrize("seconds", [None, "60", [60], {"seconds": 60}])
def test_a_budget_that_is_not_a_number_is_the_callers_error(seconds):
    with pytest.raises(TypeError):
        common.run_deadline(seconds)


@pytest.mark.parametrize("deadline", [None, "later", [1.0]])
def test_a_deadline_that_is_not_a_number_is_the_callers_error(deadline):
    with pytest.raises(TypeError):
        common.deadline_passed(deadline)


def test_a_longer_budget_outlasts_a_shorter_one_taken_at_the_same_instant(monkeypatch):
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)

    assert common.run_deadline(60) > common.run_deadline(30)


def test_an_ntp_step_neither_extends_nor_ends_the_budget(monkeypatch):
    # The scenario the monotonic clock exists for: the wall
    # clock jumps ten years while the run is between fetches
    deadline = common.run_deadline(3600)
    assert common.deadline_passed(deadline) is False

    monkeypatch.setattr(time, "time", lambda: 5_000_000_000.0)
    assert common.deadline_passed(deadline) is False

    monkeypatch.setattr(time, "time", lambda: 0.0)
    assert common.deadline_passed(deadline) is False


def test_asking_whether_the_budget_is_spent_does_not_spend_it():
    deadline = common.run_deadline(60)

    assert [common.deadline_passed(deadline) for _ in range(50)] == [False] * 50


def test_the_budget_expires_exactly_when_its_seconds_have_elapsed():
    with time_machine.travel(FROZEN, tick=False) as traveller:
        deadline = common.run_deadline(120)

        traveller.shift(timedelta(seconds=119.9))
        assert common.deadline_passed(deadline) is False

        traveller.shift(timedelta(seconds=0.2))
        assert common.deadline_passed(deadline) is True


def test_the_budget_is_a_float_even_for_an_integer_number_of_seconds():
    assert isinstance(common.run_deadline(60), float)
    assert isinstance(common.deadline_passed(common.run_deadline(60)), bool)




# ===========================================================
# utc_now_naive — the clock every stamp is measured against
# ===========================================================


def test_the_stamp_is_naive_and_agrees_with_an_aware_utc_clock():
    stamp = common.utc_now_naive()
    reference = datetime.now(timezone.utc).replace(tzinfo=None)

    assert stamp.tzinfo is None
    assert abs((reference - stamp).total_seconds()) < 2


def test_the_stamp_is_utc_and_not_the_containers_local_wall_clock(local_timezone):
    # published_at is compared against other UTC stamps; a
    # clock that quietly followed TZ would put every Vilnius
    # summer article three hours in the future
    local_timezone(VILNIUS_ISH_TZ)

    stamp = common.utc_now_naive()
    local = datetime.now()

    assert stamp.tzinfo is None
    assert round((local - stamp).total_seconds() / 3600) == 3


def test_the_stamp_keeps_its_microseconds():
    instant = datetime(2026, 8, 29, 12, 0, 0, 123456, tzinfo=timezone.utc)

    with time_machine.travel(instant, tick=False):
        assert common.utc_now_naive().microsecond == 123456


def test_the_stamp_is_a_datetime_and_not_a_bare_date():
    assert type(common.utc_now_naive()) is datetime


def test_two_hundred_readings_of_the_clock_never_go_backwards():
    readings = [common.utc_now_naive() for _ in range(200)]

    assert readings == sorted(readings)


def test_the_clock_follows_the_travelled_wall_clock():
    with time_machine.travel(FROZEN, tick=False) as traveller:
        assert common.utc_now_naive() == FROZEN_NAIVE

        traveller.shift(timedelta(hours=6))
        assert common.utc_now_naive() == FROZEN_NAIVE + timedelta(hours=6)




# ===========================================================
# parse_source_datetime — what a page can actually hand it
# ===========================================================


@pytest.mark.parametrize("value", [None, "", 0, False, [], {}, ()])
def test_a_falsy_value_is_none_before_any_parsing_is_attempted(value):
    assert common.parse_source_datetime(value) is None


def test_the_value_is_stripped_before_it_is_parsed():
    # A scraped attribute almost always arrives padded, and the
    # strip has to happen before BOTH parsers, not just the
    # first one
    assert common.parse_source_datetime("\r\n\t 2026-08-29 \r\n") == datetime(2026, 8, 29, 0, 0)
    assert common.parse_source_datetime("\x0b\x0c 2026-08-29 10:00:00 \t") == datetime(2026, 8, 29, 10, 0)


def test_a_whitespace_only_value_falls_through_both_parsers_to_none():
    assert common.parse_source_datetime(" \t\r\n ") is None


def test_the_capital_z_suffix_is_read_as_utc():
    parsed = common.parse_source_datetime("2026-08-29T07:00:00Z")

    assert parsed.utcoffset() == timedelta(0)


def test_a_lowercase_zulu_suffix_is_not_a_shape_the_parser_knows():
    # The Z -> +00:00 substitution is literal, and 3.13's
    # fromisoformat refuses the lowercase spelling, so an RFC
    # 3339 source writing "z" falls back to "stamp now"
    assert common.parse_source_datetime("2026-08-29T07:00:00z") is None


def test_a_basic_format_date_without_its_dashes_still_parses():
    assert common.parse_source_datetime("20260829") == datetime(2026, 8, 29, 0, 0)


def test_an_iso_week_date_still_parses():
    assert common.parse_source_datetime("2026-W35-6") == datetime(2026, 8, 29, 0, 0)


def test_a_compact_offset_without_its_colon_still_parses():
    parsed = common.parse_source_datetime("2026-08-29T10:00:00+0300")

    assert parsed.utcoffset() == timedelta(hours=3)


def test_a_space_separated_aware_stamp_parses_through_the_iso_reader():
    # Neither strptime format carries %z, so this one can only
    # come back aware if fromisoformat handled it
    parsed = common.parse_source_datetime("2026-08-29 10:00:00+03:00")

    assert parsed == datetime(2026, 8, 29, 10, 0, tzinfo=timezone(timedelta(hours=3)))


@pytest.mark.parametrize("offset_text, offset", [
    ("+03:00", timedelta(hours=3)),
    ("-05:00", timedelta(hours=-5)),
    ("+00:00", timedelta(0)),
    ("+14:00", timedelta(hours=14)),
    ("-12:00", timedelta(hours=-12)),
    ("+05:30", timedelta(hours=5, minutes=30)),
])
def test_an_offset_is_carried_through_for_sanitise_to_apply(offset_text, offset):
    parsed = common.parse_source_datetime("2026-08-29T10:00:00" + offset_text)

    assert parsed.utcoffset() == offset


def test_an_offset_past_a_full_day_is_refused():
    assert common.parse_source_datetime("2026-08-29T10:00:00+25:00") is None


def test_a_date_only_value_comes_back_naive_so_sanitise_reads_it_as_utc():
    parsed = common.parse_source_datetime("2026-08-29")

    assert parsed.tzinfo is None


@pytest.mark.parametrize("fraction, microseconds", [
    (".1", 100000), (".12", 120000), (".123", 123000),
    (".123456", 123456),
    # More than six digits is truncated, not refused
    (".1234567", 123456), (".123456789", 123456),
])
def test_fractional_seconds_of_any_length_parse(fraction, microseconds):
    parsed = common.parse_source_datetime("2026-08-29T07:00:00" + fraction + "+00:00")

    assert parsed.microsecond == microseconds


def test_the_second_strptime_format_catches_what_the_first_one_misses():
    # "%Y-%m-%d" raises, the loop continues, "%Y-%m-%d %H:%M:%S"
    # takes it — with unpadded fields fromisoformat refuses
    assert common.parse_source_datetime("2026-8-9 3:04:05") == datetime(2026, 8, 9, 3, 4, 5)


def test_a_leap_day_parses_and_the_same_date_in_a_common_year_does_not():
    assert common.parse_source_datetime("2024-02-29") == datetime(2024, 2, 29, 0, 0)
    assert common.parse_source_datetime("2025-02-29") is None


@pytest.mark.parametrize("value", [
    "2026-04-31", "2026-00-10", "2026-08-32", "2026-08-29T25:00:00", "2026-08-29T10:61:00",
])
def test_a_date_or_time_that_does_not_exist_is_none(value):
    assert common.parse_source_datetime(value) is None


@pytest.mark.parametrize("value", [
    "2026-08-29 labas", "2026-08-29T10:00:00 (EEST)", "paskelbta 2026-08-29",
    "2026 m. rugpjucio 29 d., 10:00", "sausio 1", "siandien", "prieš 3 dienas",
    "29-08-2026", "08/29/2026", "2026.08.29",
])
def test_a_human_readable_lithuanian_date_is_none(value):
    assert common.parse_source_datetime(value) is None


def test_a_very_long_value_is_refused_without_hanging():
    assert common.parse_source_datetime("z" * 20000) is None


def test_a_bytes_timestamp_is_not_a_shape_the_parser_supports():
    # The bytes.replace(str, str) TypeError IS caught — that is
    # what the TypeError arm of the first except is for — and
    # strptime's own TypeError then escapes. No scraper passes
    # bytes; BeautifulSoup hands out str
    with pytest.raises(TypeError):
        common.parse_source_datetime(b"2026-08-29T07:00:00Z")


def test_a_datetime_handed_in_instead_of_text_is_the_callers_error():
    with pytest.raises(AttributeError):
        common.parse_source_datetime(datetime(2026, 8, 29))


@pytest.mark.parametrize("value, expected", [
    ("0001-01-01", datetime(1, 1, 1, 0, 0)),
    ("9999-12-31", datetime(9999, 12, 31, 0, 0)),
    ("9999-12-31T23:59:59", datetime(9999, 12, 31, 23, 59, 59)),
])
def test_the_year_extremes_parse_and_leave_the_clamp_to_do_the_work(value, expected):
    assert common.parse_source_datetime(value) == expected


def test_parsing_is_pure_and_repeatable():
    value = "2026-08-29T10:00:00+03:00"

    assert common.parse_source_datetime(value) == common.parse_source_datetime(value)
    assert value == "2026-08-29T10:00:00+03:00"




# ===========================================================
# sanitise_published_at — convert first, then clamp
# ===========================================================


def test_the_offset_is_applied_before_the_range_is_checked():
    # 14:00 in Vilnius is 11:00 UTC — an hour in the PAST at a
    # frozen noon. Checking the range first would have thrown
    # this article's real date away
    with time_machine.travel(FROZEN, tick=False):
        stamped = common.sanitise_published_at(
            datetime(2026, 8, 29, 14, 0, tzinfo=timezone(timedelta(hours=3))))

    assert stamped == "2026-08-29T11:00:00"


def test_a_stamp_that_only_becomes_future_after_conversion_is_clamped(caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)

    # 12:30 at UTC-1 is 13:30 UTC: past by the wall clock it
    # was written on, future by ours
    with time_machine.travel(FROZEN, tick=False):
        stamped = common.sanitise_published_at(
            datetime(2026, 8, 29, 12, 30, tzinfo=timezone(timedelta(hours=-1))))

    assert stamped == FROZEN_NAIVE.isoformat()
    assert "out of range" in caplog.text


def test_the_upper_bound_is_now_itself_and_a_microsecond_past_it_is_clamped():
    with time_machine.travel(FROZEN, tick=False):
        assert common.sanitise_published_at(FROZEN_NAIVE) == FROZEN_NAIVE.isoformat()
        assert common.sanitise_published_at(
            FROZEN_NAIVE + timedelta(microseconds=1)) == FROZEN_NAIVE.isoformat()


def test_the_lower_bound_is_exactly_five_years_and_a_microsecond_older_is_clamped():
    edge = FROZEN_NAIVE - timedelta(days=common.MAX_ARTICLE_AGE_DAYS)

    with time_machine.travel(FROZEN, tick=False):
        assert common.sanitise_published_at(edge) == edge.isoformat()
        assert common.sanitise_published_at(
            edge - timedelta(microseconds=1)) == FROZEN_NAIVE.isoformat()


def test_the_warning_names_the_converted_stamp_and_not_the_source_offset(caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)

    # 20:00 at UTC-12 is 08:00 the NEXT day in UTC — a day
    # ahead of a frozen noon, and the log has to say so in the
    # shape the row would have stored
    with time_machine.travel(FROZEN, tick=False):
        common.sanitise_published_at(
            datetime(2026, 8, 29, 20, 0, tzinfo=timezone(timedelta(hours=-12))))

    warnings = records_of(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert "2026-08-30T08:00:00" in warnings[0].getMessage()
    assert "-12:00" not in warnings[0].getMessage()


def test_an_in_range_stamp_logs_nothing_at_all(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)

    with time_machine.travel(FROZEN, tick=False):
        common.sanitise_published_at(FROZEN_NAIVE - timedelta(days=1))

    assert records_of(caplog) == []


def test_a_missing_stamp_is_stamped_now_without_a_warning(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)

    with time_machine.travel(FROZEN, tick=False):
        assert common.sanitise_published_at(None) == FROZEN_NAIVE.isoformat()

    assert records_of(caplog) == []


@pytest.mark.parametrize("parsed", [
    None,
    datetime(2026, 8, 28, 9, 30),
    datetime(2026, 8, 29, 10, 0, tzinfo=timezone(timedelta(hours=3))),
    datetime(2126, 1, 1),
    datetime(1970, 1, 1),
])
def test_the_result_is_always_a_naive_iso_string(parsed):
    with time_machine.travel(FROZEN, tick=False):
        stamped = common.sanitise_published_at(parsed)

    assert isinstance(stamped, str)
    assert datetime.fromisoformat(stamped).tzinfo is None


def test_microseconds_survive_into_the_stored_stamp():
    parsed = datetime(2026, 8, 28, 9, 30, 15, 654321)

    with time_machine.travel(FROZEN, tick=False):
        assert common.sanitise_published_at(parsed) == "2026-08-28T09:30:15.654321"


def test_a_utc_aware_stamp_loses_only_its_tzinfo():
    with time_machine.travel(FROZEN, tick=False):
        stamped = common.sanitise_published_at(
            datetime(2026, 8, 29, 11, 0, tzinfo=timezone.utc))

    assert stamped == "2026-08-29T11:00:00"


def test_the_dawn_and_the_end_of_time_both_land_on_now(caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)

    with time_machine.travel(FROZEN, tick=False):
        assert common.sanitise_published_at(datetime.min) == FROZEN_NAIVE.isoformat()
        assert common.sanitise_published_at(datetime.max) == FROZEN_NAIVE.isoformat()

    assert len(records_of(caplog, logging.WARNING)) == 2


def test_a_naive_stamp_is_never_read_as_the_containers_local_time(local_timezone):
    local_timezone(VILNIUS_ISH_TZ)

    with time_machine.travel(FROZEN, tick=False):
        assert common.sanitise_published_at(datetime(2026, 8, 28, 9, 30)) == "2026-08-28T09:30:00"


def test_sanitising_an_already_sanitised_stamp_is_stable():
    parsed = datetime(2026, 8, 29, 10, 0, tzinfo=timezone(timedelta(hours=3)))

    with time_machine.travel(FROZEN, tick=False):
        once = common.sanitise_published_at(parsed)
        twice = common.sanitise_published_at(common.parse_source_datetime(once))

    assert once == twice == "2026-08-29T07:00:00"


@pytest.mark.parametrize("parsed", ["2026-08-29", date(2026, 8, 29), 1756468800])
def test_anything_that_is_not_a_datetime_is_the_callers_error(parsed):
    # sanitise takes what parse_source_datetime returns; a raw
    # string means the caller skipped the parser
    with pytest.raises(AttributeError):
        common.sanitise_published_at(parsed)


@pytest.mark.parametrize("text", [END_OF_TIME_TEXT, DAWN_OF_TIME_TEXT])
def test_an_aware_stamp_at_the_edge_of_time_is_clamped_like_any_other(text):
    # Reachable from a page: <time datetime="9999-12-31T23:59:59-05:00">
    # parses cleanly, and the OverflowError from converting it
    # used to escape _fetch_article into the scraper's outer
    # handler, failing the WHOLE run over one article
    parsed = common.parse_source_datetime(text)
    assert parsed is not None

    with time_machine.travel(FROZEN, tick=False):
        assert common.sanitise_published_at(parsed) == FROZEN_NAIVE.isoformat()




# ===========================================================
# mark_run_failed — closing a run on a fresh connection
# ===========================================================


def test_an_error_message_exactly_on_the_cap_is_stored_whole(app, db, run_row):
    run_id = run_row("knf", status="running")

    common.mark_run_failed(run_id, "e" * 1000)

    stored = db.execute("SELECT error_message FROM scraper_runs WHERE id = ?",
                        (run_id,)).fetchone()[0]
    assert stored == "e" * 1000


def test_one_character_over_the_cap_is_cut_back_to_it(app, db, run_row):
    run_id = run_row("knf", status="running")

    common.mark_run_failed(run_id, "e" * 1001)

    stored = db.execute("SELECT error_message FROM scraper_runs WHERE id = ?",
                        (run_id,)).fetchone()[0]
    assert len(stored) == 1000


def test_a_multibyte_message_is_cut_by_characters_and_not_bytes(app, db, run_row):
    run_id = run_row("knf", status="running")

    common.mark_run_failed(run_id, "ą" * 1500)

    stored = db.execute("SELECT error_message FROM scraper_runs WHERE id = ?",
                        (run_id,)).fetchone()[0]
    assert stored == "ą" * 1000


def test_a_missing_message_is_stored_as_the_word_none(app, db, run_row):
    run_id = run_row("knf", status="running")

    common.mark_run_failed(run_id, None)

    stored = db.execute("SELECT error_message FROM scraper_runs WHERE id = ?",
                        (run_id,)).fetchone()[0]
    assert stored == "None"


def test_an_empty_message_is_stored_empty_rather_than_null(app, db, run_row):
    run_id = run_row("knf", status="running")

    common.mark_run_failed(run_id, "")

    stored = db.execute("SELECT error_message FROM scraper_runs WHERE id = ?",
                        (run_id,)).fetchone()[0]
    assert stored == ""


def test_a_run_already_closed_as_completed_is_reopened_as_failed(app, db, run_row):
    # There is no status guard: the scrapers own the ordering,
    # and a late failure overwrites a premature success
    run_id = run_row("knf", status="completed", finished_at="2026-08-29T11:00:00+00:00")

    common.mark_run_failed(run_id, "died after the commit")

    row = db.execute("SELECT status, error_message FROM scraper_runs WHERE id = ?",
                     (run_id,)).fetchone()
    assert row["status"] == "failed"
    assert row["error_message"] == "died after the commit"


def test_marking_the_same_run_failed_twice_leaves_the_last_message(app, db, run_row):
    run_id = run_row("knf", status="running")

    common.mark_run_failed(run_id, "first")
    common.mark_run_failed(run_id, "second")

    row = db.execute("SELECT status, error_message FROM scraper_runs WHERE id = ?",
                     (run_id,)).fetchone()
    assert row["status"] == "failed"
    assert row["error_message"] == "second"


def test_the_finished_at_stamp_is_the_instant_it_ran(app, db, run_row):
    run_id = run_row("knf", status="running")

    with time_machine.travel(FROZEN, tick=False):
        common.mark_run_failed(run_id, "boom")

    stored = db.execute("SELECT finished_at FROM scraper_runs WHERE id = ?",
                        (run_id,)).fetchone()[0]
    assert stored == FROZEN.isoformat()


def test_only_the_named_run_is_touched(app, db, run_row):
    doomed = run_row("knf", status="running")
    bystander = run_row("vu", status="running")

    common.mark_run_failed(doomed, "boom")

    row = db.execute("SELECT status, error_message, finished_at FROM scraper_runs WHERE id = ?",
                     (bystander,)).fetchone()
    assert row["status"] == "running"
    assert row["error_message"] is None
    assert row["finished_at"] is None


@pytest.mark.parametrize("run_id", [None, "", "no-such-run", 12345])
def test_a_run_id_that_matches_nothing_leaves_every_row_alone(app, db, run_row, run_id):
    survivor = run_row("knf", status="running")

    common.mark_run_failed(run_id, "boom")

    assert db.execute("SELECT status FROM scraper_runs WHERE id = ?",
                      (survivor,)).fetchone()[0] == "running"


def test_the_connection_is_closed_even_when_the_update_fails(app, monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    broken = _BrokenConnection(fail_on="execute")
    monkeypatch.setattr(common, "get_db", lambda: broken)

    common.mark_run_failed("any-run", "boom")

    assert broken.closed is True
    assert "Failed to close scraper run any-run as failed" in caplog.text


def test_the_connection_is_closed_even_when_the_commit_fails(app, monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    broken = _BrokenConnection(fail_on="commit")
    monkeypatch.setattr(common, "get_db", lambda: broken)

    common.mark_run_failed("any-run", "boom")

    assert broken.closed is True
    assert broken.executed and "scraper_runs" in broken.executed[0][0]
    assert "Failed to close scraper run" in caplog.text


@pytest.mark.parametrize("error", [
    sqlite3.OperationalError("database is locked"),
    sqlite3.DatabaseError("file is not a database"),
    sqlite3.ProgrammingError("Cannot operate on a closed database."),
    RuntimeError("init_db() has not been called"),
    ValueError("nonsense"),
])
def test_it_never_raises_whatever_the_database_does(app, monkeypatch, caplog, error):
    # It runs inside the scrapers' except handler; raising here
    # would replace the real error with a database one and
    # escape into the admin route as a 500
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)

    def _explode():
        raise error

    monkeypatch.setattr(common, "get_db", _explode)

    assert common.mark_run_failed("any-run", "boom") is None
    assert "Failed to close scraper run" in caplog.text


def test_a_fresh_connection_is_opened_and_closed_for_every_call(app, db, run_row, monkeypatch):
    # The whole reason it does not take the caller's connection
    opened = []
    real_get_db = common.get_db

    def _spy():
        conn = real_get_db()
        opened.append(conn)
        return conn

    monkeypatch.setattr(common, "get_db", _spy)
    run_id = run_row("knf", status="running")

    common.mark_run_failed(run_id, "first")
    common.mark_run_failed(run_id, "second")

    assert len(opened) == 2
    assert opened[0] is not opened[1]
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


def test_the_write_is_committed_so_another_connection_sees_it(app, db, run_row):
    run_id = run_row("knf", status="running")

    common.mark_run_failed(run_id, "boom")

    check = sqlite3.connect(app.config["DB_PATH"])
    try:
        assert check.execute("SELECT status FROM scraper_runs WHERE id = ?",
                             (run_id,)).fetchone()[0] == "failed"
    finally:
        check.close()




# ===========================================================
# prune_scraper_runs — retention boundaries
# ===========================================================


def test_a_run_started_exactly_on_the_cutoff_survives(db, run_row):
    with time_machine.travel(FROZEN, tick=False):
        cutoff = (FROZEN - timedelta(days=common.RUN_RETENTION_DAYS)).isoformat()
        on_the_line = run_row("knf", started_at=cutoff)
        run_row("knf", days_ago=0)                    # keeps on_the_line from being the newest

        common.prune_scraper_runs(db)

    assert db.execute("SELECT COUNT(*) FROM scraper_runs WHERE id = ?",
                      (on_the_line,)).fetchone()[0] == 1


def test_a_run_a_microsecond_older_than_the_cutoff_is_pruned(db, run_row):
    with time_machine.travel(FROZEN, tick=False):
        just_over = (FROZEN - timedelta(days=common.RUN_RETENTION_DAYS)
                     - timedelta(microseconds=1)).isoformat()
        doomed = run_row("knf", started_at=just_over)
        run_row("knf", days_ago=0)

        common.prune_scraper_runs(db)

    assert db.execute("SELECT COUNT(*) FROM scraper_runs WHERE id = ?",
                      (doomed,)).fetchone()[0] == 0


def test_the_newest_run_of_every_source_survives_however_ancient(db, run_row):
    with time_machine.travel(FROZEN, tick=False):
        keepers = {
            run_row("knf", days_ago=200),
            run_row("vu", days_ago=400, status="failed"),
            run_row("schedule", days_ago=900, status="running"),
            run_row("info", days_ago=1500),
        }
        run_row("knf", days_ago=900)
        run_row("vu", days_ago=1000, status="failed")

        common.prune_scraper_runs(db)

    assert {r[0] for r in db.execute("SELECT id FROM scraper_runs")} == keepers


def test_two_ancient_runs_sharing_the_newest_stamp_leave_exactly_one_behind(db, run_row):
    # SQLite's bare-column rule picks ONE row per group, so a
    # tie on MAX(started_at) keeps one of the two and prunes
    # the other — a source never disappears from /status
    with time_machine.travel(FROZEN, tick=False):
        stamp = (FROZEN - timedelta(days=365)).isoformat()
        run_row("knf", started_at=stamp)
        run_row("knf", started_at=stamp)

        common.prune_scraper_runs(db)

    assert db.execute("SELECT COUNT(*) FROM scraper_runs WHERE source = 'knf'").fetchone()[0] == 1


def test_a_source_whose_runs_are_all_recent_keeps_every_one(db, run_row):
    with time_machine.travel(FROZEN, tick=False):
        for days in (0, 1, 7, 29.9):
            run_row("knf", days_ago=days)

        common.prune_scraper_runs(db)

    assert db.execute("SELECT COUNT(*) FROM scraper_runs").fetchone()[0] == 4


def test_pruning_an_already_pruned_table_removes_nothing_and_says_nothing(db, run_row, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    with time_machine.travel(FROZEN, tick=False):
        run_row("knf", days_ago=0)
        run_row("knf", days_ago=90)

        common.prune_scraper_runs(db)
        caplog.clear()
        common.prune_scraper_runs(db)

    assert records_of(caplog) == []
    assert db.execute("SELECT COUNT(*) FROM scraper_runs").fetchone()[0] == 1


@pytest.mark.slow
def test_a_hundred_ancient_runs_go_in_one_pass_and_the_newest_stays(db, run_row, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    with time_machine.travel(FROZEN, tick=False):
        newest = run_row("knf", days_ago=31)
        for extra in range(1, 100):
            run_row("knf", days_ago=31 + extra)

        common.prune_scraper_runs(db)

    assert {r[0] for r in db.execute("SELECT id FROM scraper_runs")} == {newest}
    assert "Pruned 99 scraper_runs row(s) older than 30 days" in caplog.text


def test_a_read_only_database_is_a_warning_and_never_a_failed_run(db, run_row, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)

    with time_machine.travel(FROZEN, tick=False):
        run_row("knf", days_ago=0)
        doomed = run_row("knf", days_ago=90)
        db.execute("PRAGMA query_only = ON")

        assert common.prune_scraper_runs(db) is None

    db.execute("PRAGMA query_only = OFF")
    assert "Could not prune scraper_runs" in caplog.text
    assert db.execute("SELECT COUNT(*) FROM scraper_runs WHERE id = ?",
                      (doomed,)).fetchone()[0] == 1


def test_the_prune_commits_whatever_the_caller_left_pending(app, db, run_row):
    # Documented behaviour, not an accident: the delete is
    # committed because pruning is the last thing a run does
    with time_machine.travel(FROZEN, tick=False):
        run_row("knf", days_ago=0)
        db.execute(
            """INSERT INTO scraper_runs (id, source, status, started_at)
               VALUES ('uncommitted', 'vu', 'completed', ?)""",
            (FROZEN.isoformat(),),
        )

        common.prune_scraper_runs(db)

    check = sqlite3.connect(app.config["DB_PATH"])
    try:
        assert check.execute("SELECT COUNT(*) FROM scraper_runs WHERE id = 'uncommitted'"
                             ).fetchone()[0] == 1
    finally:
        check.close()


def test_only_a_sqlite_error_is_swallowed():
    # The except names sqlite3.Error on purpose; a caller that
    # hands it something that is not a connection at all still
    # gets its mistake back
    with pytest.raises(AttributeError):
        common.prune_scraper_runs(None)


def test_the_log_line_names_the_row_count_and_the_window(db, run_row, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    with time_machine.travel(FROZEN, tick=False):
        run_row("knf", days_ago=0)
        run_row("knf", days_ago=60)
        run_row("knf", days_ago=61)

        common.prune_scraper_runs(db)

    lines = records_of(caplog, logging.INFO)
    assert len(lines) == 1
    assert lines[0].getMessage() == "Pruned 2 scraper_runs row(s) older than 30 days"


def test_the_cutoff_moves_with_the_clock(db, run_row):
    # The same row survives today and is pruned two months on
    with time_machine.travel(FROZEN, tick=False):
        older = run_row("knf", days_ago=20)
        run_row("knf", days_ago=0)

        common.prune_scraper_runs(db)
        assert db.execute("SELECT COUNT(*) FROM scraper_runs WHERE id = ?",
                          (older,)).fetchone()[0] == 1

    with time_machine.travel(FROZEN + timedelta(days=60), tick=False):
        run_row("knf", days_ago=0)
        common.prune_scraper_runs(db)

    assert db.execute("SELECT COUNT(*) FROM scraper_runs WHERE id = ?",
                      (older,)).fetchone()[0] == 0




# ===========================================================
# load_deleted_urls — the tombstone set
# ===========================================================


def _tombstone(db, url):
    db.execute("INSERT INTO deleted_source_urls (source_url) VALUES (?)", (url,))
    db.commit()


def test_the_answer_is_a_set_so_membership_is_the_only_question(db):
    _tombstone(db, "https://knf.vu.lt/naujienos/x")

    assert isinstance(common.load_deleted_urls(db), set)


def test_every_spelling_of_one_article_collapses_to_a_single_key(db):
    _tombstone(db, "http://www.knf.vu.lt/naujienos/x/")
    _tombstone(db, "https://knf.vu.lt/naujienos/x?utm_source=fb")
    _tombstone(db, "https://KNF.VU.LT/naujienos/x#turinys")

    assert common.load_deleted_urls(db) == {"https://knf.vu.lt/naujienos/x"}


def test_a_null_tombstone_row_is_ignored(db):
    # source_url is a TEXT primary key, and SQLite lets NULLs
    # into one — a half-written delete must not become a
    # tombstone that matches nothing
    db.execute("INSERT INTO deleted_source_urls (source_url) VALUES (NULL)")
    db.commit()
    _tombstone(db, "https://knf.vu.lt/naujienos/x")

    assert common.load_deleted_urls(db) == {"https://knf.vu.lt/naujienos/x"}


def test_a_whitespace_only_tombstone_matches_no_article(db):
    _tombstone(db, "   ")

    tombstones = common.load_deleted_urls(db)
    assert tombstones == {""}
    assert common.normalise_url("https://knf.vu.lt/naujienos/x") not in tombstones


def test_a_relative_tombstone_keeps_its_relative_shape(db):
    _tombstone(db, "/naujienos/studiju-pradzia")

    assert common.load_deleted_urls(db) == {"/naujienos/studiju-pradzia"}


def test_the_host_is_lowered_and_the_path_case_is_kept(db):
    _tombstone(db, "HTTPS://WWW.KNF.VU.LT/Naujienos/Studiju-Pradzia")

    assert common.load_deleted_urls(db) == {"https://knf.vu.lt/Naujienos/Studiju-Pradzia"}


def test_a_tombstone_for_another_host_never_matches_a_faculty_link(db):
    _tombstone(db, "https://vu.lt/naujienos/x")

    assert common.normalise_url("https://knf.vu.lt/naujienos/x") not in common.load_deleted_urls(db)


@pytest.mark.slow
def test_five_hundred_tombstones_all_come_back(db):
    for n in range(500):
        db.execute("INSERT INTO deleted_source_urls (source_url) VALUES (?)",
                   (f"https://knf.vu.lt/naujienos/{n}",))
    db.commit()

    assert len(common.load_deleted_urls(db)) == 500


def test_reading_the_tombstones_twice_gives_the_same_set(db):
    _tombstone(db, "https://knf.vu.lt/naujienos/x")

    assert common.load_deleted_urls(db) == common.load_deleted_urls(db)


def test_a_closed_connection_is_an_empty_set_and_a_debug_line(db, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    db.close()

    assert common.load_deleted_urls(db) == set()
    assert "No deleted_source_urls table yet" in caplog.text


def test_a_table_without_the_source_url_column_is_an_empty_set(db, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    db.execute("DROP TABLE deleted_source_urls")
    db.execute("CREATE TABLE deleted_source_urls (url TEXT PRIMARY KEY)")
    db.commit()

    assert common.load_deleted_urls(db) == set()
    assert "No deleted_source_urls table yet" in caplog.text


def test_the_missing_table_is_a_debug_line_and_never_a_warning(db, caplog):
    # Until the news package's migration lands this is the
    # normal state, not a fault worth waking anyone for
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    db.execute("DROP TABLE deleted_source_urls")
    db.commit()

    common.load_deleted_urls(db)

    assert records_of(caplog, logging.WARNING) == []
    assert len(records_of(caplog, logging.DEBUG)) == 1


def test_only_a_sqlite_error_is_swallowed_here_too():
    with pytest.raises(AttributeError):
        common.load_deleted_urls(None)


def test_a_tombstone_written_before_normalisation_existed_still_matches_todays_link(db):
    # The v35 shape: scheme, www, trailing slash, campaign tag
    # and fragment all still on the stored value
    _tombstone(db, "http://www.knf.vu.lt/naujienos/studiju-pradzia/?utm_campaign=rugsejis#top")

    scraped = common.normalise_url("https://knf.vu.lt/naujienos/studiju-pradzia")
    assert scraped in common.load_deleted_urls(db)




# ===========================================================
# check_yield_drop — the selector-rot alarm
# ===========================================================


def test_the_alarm_needs_a_full_order_of_magnitude(db, run_row, caplog):
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    run_row("knf", found=100)

    common.check_yield_drop(db, "knf", 10, "run-now")
    assert records_of(caplog) == []

    common.check_yield_drop(db, "knf", 9, "run-now")
    assert len(records_of(caplog, logging.ERROR)) == 1


def test_a_previous_yield_of_ten_is_the_smallest_that_can_collapse(db, run_row, caplog):
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    run_row("knf", found=10)

    # 1 * 10 is not < 10 — even the smallest possible baseline
    # needs the yield to reach zero
    common.check_yield_drop(db, "knf", 1, "run-now")
    assert records_of(caplog) == []

    common.check_yield_drop(db, "knf", 0, "run-now")
    assert len(records_of(caplog, logging.ERROR)) == 1


def test_a_previous_yield_of_nine_can_never_collapse(db, run_row, caplog):
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    run_row("knf", found=9)

    common.check_yield_drop(db, "knf", 0, "run-now")

    assert records_of(caplog) == []


def test_a_previous_run_that_found_nothing_is_never_a_baseline(db, run_row, caplog):
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    run_row("knf", found=0)

    common.check_yield_drop(db, "knf", 0, "run-now")

    assert records_of(caplog) == []


def test_a_run_that_beats_the_last_one_says_nothing(db, run_row, caplog):
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    run_row("knf", found=10)

    common.check_yield_drop(db, "knf", 1000, "run-now")

    assert records_of(caplog) == []


def test_a_huge_baseline_against_an_empty_run_is_a_collapse(db, run_row, caplog):
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    run_row("knf", found=1_000_000_000)

    common.check_yield_drop(db, "knf", 0, "run-now")

    assert "yield collapsed" in caplog.text


def test_an_enormous_yield_does_not_overflow_the_comparison(db, run_row, caplog):
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    run_row("knf", found=40)

    common.check_yield_drop(db, "knf", 10 ** 18, "run-now")

    assert records_of(caplog) == []


def test_a_running_run_is_not_a_baseline(db, run_row, caplog):
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    run_row("knf", status="running", found=400)

    common.check_yield_drop(db, "knf", 1, "run-now")

    assert records_of(caplog) == []


def test_the_baseline_is_the_latest_by_started_at_and_not_by_insert_order(db, run_row, caplog):
    # Written newest-first on purpose: taking the rows in insert
    # order would pick the 500-item run and cry collapse over a
    # source that is behaving exactly as it did yesterday
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    run_row("knf", found=12, days_ago=1)
    run_row("knf", found=500, days_ago=30)

    common.check_yield_drop(db, "knf", 2, "run-now")

    assert records_of(caplog) == []


def test_a_source_name_that_looks_like_sql_is_only_ever_a_parameter(db, run_row, caplog):
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    run_row("knf", found=400)

    common.check_yield_drop(db, "knf' OR '1'='1", 0, "run-now")

    assert records_of(caplog) == []
    assert db.execute("SELECT COUNT(*) FROM scraper_runs").fetchone()[0] == 1


def test_a_run_id_that_looks_like_sql_is_only_ever_a_parameter(db, run_row, caplog):
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    run_row("knf", found=400)

    common.check_yield_drop(db, "knf", 0, "x' OR '1'='1")

    assert "yield collapsed" in caplog.text
    assert db.execute("SELECT COUNT(*) FROM scraper_runs").fetchone()[0] == 1


def test_a_none_run_id_silences_the_alarm_entirely(db, run_row, caplog):
    # "id != NULL" is NULL for every row, so the baseline query
    # matches nothing and a real collapse goes unreported. Every
    # scraper passes its own run id, so no caller hits this
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    run_row("knf", found=400)

    common.check_yield_drop(db, "knf", 0, None)

    assert records_of(caplog) == []


def test_the_alarm_is_a_single_error_line_naming_the_source_and_both_counts(db, run_row, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    run_row("knf", found=40)

    common.check_yield_drop(db, "knf", 2, "run-now")

    lines = records_of(caplog)
    assert len(lines) == 1
    assert lines[0].levelno == logging.ERROR
    message = lines[0].getMessage()
    assert message.startswith("knf yield collapsed:")
    assert "2 item(s) this run against 40 last run" in message


def test_the_check_answers_nothing_and_leaves_the_table_alone(db, run_row):
    run_row("knf", found=40)

    assert common.check_yield_drop(db, "knf", 1, "run-now") is None
    assert db.execute("SELECT COUNT(*) FROM scraper_runs").fetchone()[0] == 1


def test_a_read_only_database_still_lets_the_alarm_read(db, run_row, caplog):
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    run_row("knf", found=40)
    db.execute("PRAGMA query_only = ON")
    try:
        common.check_yield_drop(db, "knf", 1, "run-now")
    finally:
        db.execute("PRAGMA query_only = OFF")

    assert "yield collapsed" in caplog.text


def test_a_baseline_the_database_cannot_answer_for_is_only_a_warning(db, caplog):
    # Purely diagnostic: the run's own status is its caller's
    # business, so a failed baseline read is one warning with
    # the traceback attached and nothing else
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    db.execute("DROP TABLE scraper_runs")
    db.commit()

    assert common.check_yield_drop(db, "knf", 1, "run-now") is None

    warnings = records_of(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert "Could not read the previous knf run" in warnings[0].getMessage()
    assert warnings[0].exc_info is not None


def test_only_a_sqlite_error_is_swallowed_by_the_alarm():
    with pytest.raises(AttributeError):
        common.check_yield_drop(None, "knf", 1, "run-now")


def test_each_source_is_measured_against_its_own_history(db, run_row, caplog):
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    run_row("knf", found=400)
    run_row("vu", found=12)

    common.check_yield_drop(db, "vu", 2, "run-now")

    assert records_of(caplog) == []




# ===========================================================
# The two ends joined — a page timestamp to a stored stamp
# ===========================================================


def test_a_vilnius_page_timestamp_lands_as_naive_utc():
    with time_machine.travel(FROZEN, tick=False):
        stamped = common.sanitise_published_at(
            common.parse_source_datetime("2026-08-29T10:00:00+03:00"))

    assert stamped == "2026-08-29T07:00:00"


@pytest.mark.parametrize("text", ["vakar", "rugpjucio 29", "", None, "2026-13-45"])
def test_an_unreadable_page_timestamp_ends_up_stamped_now(text):
    with time_machine.travel(FROZEN, tick=False):
        stamped = common.sanitise_published_at(common.parse_source_datetime(text))

    assert stamped == FROZEN_NAIVE.isoformat()


@pytest.mark.parametrize("text", ["9999-12-31", "0001-01-01", "20260830"])
def test_a_mistyped_year_on_a_page_ends_up_stamped_now(text):
    # 20260830 is tomorrow at the frozen instant: the clamp
    # treats a future date the same as an impossible one
    with time_machine.travel(FROZEN, tick=False):
        stamped = common.sanitise_published_at(common.parse_source_datetime(text))

    assert stamped == FROZEN_NAIVE.isoformat()


def test_the_stored_stamps_sort_lexically_the_way_the_feed_orders_them():
    # published_at is TEXT and the feed orders on it directly,
    # so string order has to match instant order
    with time_machine.travel(FROZEN, tick=False):
        stamps = [
            common.sanitise_published_at(common.parse_source_datetime(text))
            for text in ("2026-08-29T09:00:00+03:00",
                         "2026-08-29T07:30:00Z",
                         "2025-12-31",
                         "2026-08-29 06:00:00")
        ]

    assert sorted(stamps) == [
        "2025-12-31T00:00:00",
        "2026-08-29T06:00:00",
        "2026-08-29T06:00:00",
        "2026-08-29T07:30:00",
    ]
