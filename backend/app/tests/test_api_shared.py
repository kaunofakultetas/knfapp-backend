# -----------------------------------------------------------
#  [*] Tests — app/api/__init__.py and main.py
#
#  The two modules nothing else can be trusted without: the
#  helpers every feed-style route shares, and the process
#  entry point that wires a serving container together.
#
#  What this module proves:
#
#    parse_pagination
#      - absent params fall back to page 1 / default_per_page
#      - page is a positive int, capped at max_page
#      - per_page is a positive int and is REJECTED above
#        max_per_page — the regression that matters, because
#        it used to clamp silently and hand a client asking
#        for 100 a hundred rows while swagger said 50
#      - every guard answers the ready-made (response, 400)
#        tuple with (None, None) for the two numbers, so a
#        route can `return err` unchanged
#      - the callers' own bounds (social's 50/200/200, news's
#        defaults) reach the wire as those exact numbers
#
#    the shared constants
#      - MAX_TITLE_LENGTH / MAX_CONTENT_LENGTH / SUMMARY_LENGTH
#        still match the hand-synced copies in news/routes.py
#        and scraper/common.py — the drift the module header
#        calls "a matter of time"
#      - FEED_SCORE_SQL is valid SQLite under BOTH of its
#        interpolations, bounded 0..150, and NULL-proof
#
#    main.py
#      - --http is the ONLY thing that builds an app: without
#        it the help is printed and no database is touched
#      - logging is configured before create_app, the
#        interrupted scraper runs are reconciled after it, and
#        the server is started last
#      - the debugger and the reloader stay off even when
#        APP_DEBUG=1, and APP_DEBUG has to be exactly "1"
#      - SCRAPER_ENABLED=0 leaves the scheduler, the atexit
#        hook and the SIGTERM handler uninstalled
#      - the shutdown helpers survive a missing scheduler
#        module and a failing shutdown
#
#  Nothing here binds a port: socketio.run is a recorder in
#  every serving test, and the CLI is driven through sys.argv.
# -----------------------------------------------------------


import logging
import os
import runpy
import signal as signal_module
import sqlite3
import sys
import uuid

import pytest

import main
from app.api import (
    FEED_SCORE_SQL,
    MAX_CONTENT_LENGTH,
    MAX_TITLE_LENGTH,
    SUMMARY_LENGTH,
    parse_pagination,
)
from app.scraper import scheduler as scraper_scheduler


_MAIN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "main.py")




# -----------------------------------------------------------
# _parse
# -----------------------------------------------------------
#
# parse_pagination reads request.args, so it needs a request
# context; this puts one around a single call and hands back
# the (page, per_page, err) triple untouched.
#
# Used by:
#   - every direct parse_pagination test below
# -----------------------------------------------------------

def _parse(app, query="", **kwargs):
    with app.test_request_context("/api/probe" + query):
        return parse_pagination(**kwargs)




# -----------------------------------------------------------
# _assert_refused
# -----------------------------------------------------------
#
# The whole contract of a rejected parse in one assertion:
# both numbers are None, the third member is a (response,
# 400) tuple Flask can return as-is, and the body is the
# house {"error": ...} envelope with exactly that message.
#
# Used by:
#   - the page and per_page guard tests
# -----------------------------------------------------------

def _assert_refused(result, message):
    page, per_page, err = result

    assert page is None
    assert per_page is None
    assert err is not None

    response, status = err
    assert status == 400
    assert response.get_json() == {"error": message}
    assert response.mimetype == "application/json"




# -----------------------------------------------------------
# _seed_run
# -----------------------------------------------------------
#
# One scraper_runs row in whatever state the test needs —
# the table main._reconcile_scraper_runs sweeps at boot.
#
# Used by:
#   - the reconciliation tests
# -----------------------------------------------------------

def _seed_run(db, status="running", source="knf", finished_at=None):
    run_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO scraper_runs (id, source, status, started_at, finished_at)"
        " VALUES (?, ?, ?, '2026-01-01T00:00:00+00:00', ?)",
        (run_id, source, status, finished_at),
    )
    db.commit()
    return run_id




# -----------------------------------------------------------
# _serve
# -----------------------------------------------------------
#
# Runs main.main() with the caller's argv and NOTHING that
# would bind a port, build a second app, start a scheduler,
# install a process-wide signal handler or reconfigure the
# root logger — each of those is swapped for a recorder. The
# returned dict is the log of what main() actually did, with
# "order" holding the sequence, so the boot wiring can be
# asserted rather than guessed at.
#
# The reconciliation is wrapped, not replaced: the real
# function runs against the fixture's throwaway database.
#
# Used by:
#   - every main() test below
# -----------------------------------------------------------

def _serve(monkeypatch, app, argv):
    calls = {"order": [], "run": None, "created": 0, "logging": 0,
             "reconciled": [], "atexit": [], "signals": [], "scheduled": []}

    class _Recorder:
        def run(self, served_app, **kwargs):
            calls["order"].append("serve")
            calls["run"] = (served_app, kwargs)

    def _configure_logging():
        calls["order"].append("logging")
        calls["logging"] += 1

    def _create_app():
        calls["order"].append("create_app")
        calls["created"] += 1
        return app

    real_reconcile = main._reconcile_scraper_runs

    def _reconcile(target):
        calls["order"].append("reconcile")
        calls["reconciled"].append(target)
        real_reconcile(target)

    def _start_scheduler(target):
        calls["order"].append("start_scheduler")
        calls["scheduled"].append(target)
        return True

    monkeypatch.setattr(sys, "argv", list(argv))
    monkeypatch.setattr(main, "socketio", _Recorder())
    monkeypatch.setattr(main, "_configure_logging", _configure_logging)
    monkeypatch.setattr(main, "create_app", _create_app)
    monkeypatch.setattr(main, "_reconcile_scraper_runs", _reconcile)
    monkeypatch.setattr(scraper_scheduler, "start_scraper_scheduler", _start_scheduler)
    monkeypatch.setattr(main.atexit, "register", lambda fn: calls["atexit"].append(fn))
    monkeypatch.setattr(main.signal, "signal", lambda num, fn: calls["signals"].append((num, fn)))

    main.main()
    return calls








# -----------------------------------------------------------
# parse_pagination — the defaults
# -----------------------------------------------------------


def test_absent_params_fall_back_to_page_one_and_the_default_per_page(app):
    assert _parse(app) == (1, 20, None)


def test_an_explicit_page_and_per_page_are_returned_as_ints(app):
    page, per_page, err = _parse(app, "?page=3&per_page=7")

    assert (page, per_page, err) == (3, 7, None)
    assert isinstance(page, int) and isinstance(per_page, int)


def test_the_caller_can_move_the_default_per_page(app):
    assert _parse(app, max_per_page=200, default_per_page=200) == (1, 200, None)


def test_an_absent_per_page_skips_the_cap_check_entirely(app):
    # social's friends list passes 200 as BOTH the default and
    # the cap; a default above the cap would still be handed
    # back untouched, which is what the un-paged mobile call
    # relies on
    assert _parse(app, max_per_page=5, default_per_page=99) == (1, 99, None)


def test_unrelated_query_params_are_ignored(app):
    assert _parse(app, "?source=user&before=2026-01-01&limit=999") == (1, 20, None)


def test_only_the_first_value_of_a_repeated_param_is_read(app):
    assert _parse(app, "?page=2&page=9999999") == (2, 20, None)
    assert _parse(app, "?per_page=5&per_page=5000") == (1, 5, None)


def test_pagination_comes_from_the_query_string_not_the_body(app):
    with app.test_request_context("/api/probe", method="POST", json={"page": 4, "per_page": 40}):
        assert parse_pagination() == (1, 20, None)








# -----------------------------------------------------------
# parse_pagination — the page guard
# -----------------------------------------------------------


_PAGE_NOT_AN_INT = "page must be a positive integer"


def test_page_one_is_the_lowest_accepted_page(app):
    assert _parse(app, "?page=1") == (1, 20, None)


def test_page_zero_is_refused(app):
    _assert_refused(_parse(app, "?page=0"), _PAGE_NOT_AN_INT)


def test_a_negative_page_is_refused(app):
    _assert_refused(_parse(app, "?page=-1"), _PAGE_NOT_AN_INT)


@pytest.mark.parametrize(
    "raw",
    ["abc", "", "3.0", "1e3", "%20%20", "null", "1,2", "0x10", "3px", "--1", "NaN"],
)
def test_a_page_that_is_not_an_integer_is_refused(app, raw):
    _assert_refused(_parse(app, f"?page={raw}"), _PAGE_NOT_AN_INT)


def test_minus_zero_is_refused_as_a_non_positive_page(app):
    _assert_refused(_parse(app, "?page=-0"), _PAGE_NOT_AN_INT)


@pytest.mark.parametrize("raw,expected", [("+3", 3), ("%203%20", 3), ("03", 3), ("1_0", 10)])
def test_int_accepts_the_forms_python_int_accepts(app, raw, expected):
    # Documented in the helper's banner: the parser is int(),
    # so a leading "+", surrounding whitespace, leading zeroes
    # and PEP 515 underscores all pass. Pinned so a future
    # hand-rolled parser cannot silently narrow the contract
    assert _parse(app, f"?page={raw}") == (expected, 20, None)


def test_the_page_cap_is_inclusive(app):
    assert _parse(app, "?page=10000") == (10000, 20, None)


def test_a_page_above_the_cap_is_refused(app):
    _assert_refused(_parse(app, "?page=10001"), "page must be at most 10000")


def test_the_page_cap_message_names_the_callers_own_cap(app):
    _assert_refused(_parse(app, "?page=201", max_page=200), "page must be at most 200")


def test_a_caller_can_pin_the_feed_to_a_single_page(app):
    assert _parse(app, "?page=1", max_page=1) == (1, 20, None)
    _assert_refused(_parse(app, "?page=2", max_page=1), "page must be at most 1")


def test_an_absurdly_large_page_is_refused_rather_than_overflowing(app):
    _assert_refused(_parse(app, "?page=" + "9" * 40), "page must be at most 10000")


def test_the_page_guard_runs_before_the_per_page_guard(app):
    # Both are wrong; the answer names page, because a route
    # reporting the second problem first would send a client
    # round the houses
    _assert_refused(_parse(app, "?page=0&per_page=0"), _PAGE_NOT_AN_INT)








# -----------------------------------------------------------
# parse_pagination — the per_page guard
# -----------------------------------------------------------


_PER_PAGE_NOT_AN_INT = "per_page must be a positive integer"


def test_per_page_one_is_accepted(app):
    assert _parse(app, "?per_page=1") == (1, 1, None)


def test_per_page_zero_is_refused(app):
    _assert_refused(_parse(app, "?per_page=0"), _PER_PAGE_NOT_AN_INT)


def test_a_negative_per_page_is_refused(app):
    _assert_refused(_parse(app, "?per_page=-20"), _PER_PAGE_NOT_AN_INT)


@pytest.mark.parametrize("raw", ["abc", "", "20.0", "twenty", "1e1"])
def test_a_per_page_that_is_not_an_integer_is_refused(app, raw):
    _assert_refused(_parse(app, f"?per_page={raw}"), _PER_PAGE_NOT_AN_INT)


def test_the_per_page_cap_is_inclusive(app):
    assert _parse(app, "?per_page=50") == (1, 50, None)


def test_a_per_page_above_the_cap_is_refused_not_clamped(app):
    # THE regression: it used to default to 100 and clamp
    # silently, so a client asking for 100 got 100 rows while
    # the published contract said 50
    result = _parse(app, "?per_page=100")

    _assert_refused(result, "per_page must be at most 50")
    assert result[1] is None, "a rejected per_page must not come back clamped"


def test_the_per_page_cap_message_names_the_callers_own_cap(app):
    _assert_refused(_parse(app, "?per_page=201", max_per_page=200),
                    "per_page must be at most 200")


def test_one_over_the_callers_cap_is_the_boundary(app):
    assert _parse(app, "?per_page=200", max_per_page=200) == (1, 200, None)
    _assert_refused(_parse(app, "?per_page=201", max_per_page=200),
                    "per_page must be at most 200")


def test_a_valid_page_is_discarded_when_per_page_is_refused(app):
    _assert_refused(_parse(app, "?page=5&per_page=999"), "per_page must be at most 50")








# -----------------------------------------------------------
# The shared length constants
# -----------------------------------------------------------


def test_the_shared_length_constants_are_the_documented_numbers():
    assert MAX_TITLE_LENGTH == 200
    assert MAX_CONTENT_LENGTH == 10000
    assert SUMMARY_LENGTH == 200


def test_the_news_modules_hand_synced_copies_have_not_drifted():
    # news/routes.py still keeps its own pair (the module
    # header says so); this is the guard that they stay equal
    # until it moves onto the shared ones
    from app.news import routes as news_routes

    assert news_routes.MAX_TITLE_LENGTH == MAX_TITLE_LENGTH
    assert news_routes.MAX_CONTENT_LENGTH == MAX_CONTENT_LENGTH


def test_the_scraper_truncates_to_the_same_limits_the_routes_enforce():
    # A scraped row longer than the API limit could never be
    # edited through the API again
    from app.scraper import common as scraper_common

    assert scraper_common.MAX_TITLE_LENGTH == MAX_TITLE_LENGTH
    assert scraper_common.MAX_CONTENT_LENGTH == MAX_CONTENT_LENGTH


def test_a_summary_is_never_longer_than_a_whole_body():
    assert SUMMARY_LENGTH <= MAX_CONTENT_LENGTH








# -----------------------------------------------------------
# FEED_SCORE_SQL
# -----------------------------------------------------------
#
# The fragment is a format string with ONE {now} hole, filled
# with "'now'" for the live window and "?" when the caller
# pins it to a ?before timestamp. These run it through real
# SQLite so a syntax slip cannot ship.
# -----------------------------------------------------------


def _score(db, published_at, likes=0, comments=0, shares=0, now="'now'", params=()):
    sql = "SELECT " + FEED_SCORE_SQL.format(now=now) + " FROM (SELECT ? AS published_at," \
          " ? AS likes_count, ? AS comments_count, ? AS shares_count)"
    return db.execute(sql, (*params, published_at, likes, comments, shares)).fetchone()[0]


def test_the_live_interpolation_is_valid_sqlite(db):
    assert _score(db, "2026-08-29T00:00:00+00:00") > 0


def test_the_pinned_interpolation_takes_one_bound_parameter(db):
    assert FEED_SCORE_SQL.format(now="?").count("?") == 1

    pinned = _score(db, "2026-01-01T00:00:00+00:00", now="?",
                    params=("2026-01-01T00:00:00+00:00",))
    assert pinned == pytest.approx(100.0)


def test_engagement_can_never_lift_an_older_post_above_a_brand_new_one(db):
    # The banner's claim: engagement tops out at 50, recency
    # halves after a day, so a viral week-old post cannot
    # outrank a fresh one
    fresh = _score(db, "2026-01-08T00:00:00+00:00", now="?", params=("2026-01-08T00:00:00+00:00",))
    day_old = _score(db, "2026-01-07T00:00:00+00:00", likes=1000, comments=1000, shares=1000,
                     now="?", params=("2026-01-08T00:00:00+00:00",))
    week_old = _score(db, "2026-01-01T00:00:00+00:00", likes=1000, comments=1000, shares=1000,
                      now="?", params=("2026-01-08T00:00:00+00:00",))

    assert day_old <= fresh
    assert week_old < fresh


def test_engagement_tops_out_at_fifty(db):
    capped = _score(db, "2026-01-01T00:00:00+00:00", likes=10_000,
                    now="?", params=("2027-01-01T00:00:00+00:00",))

    assert capped == pytest.approx(50.0, abs=0.5)


def test_a_future_published_at_cannot_explode_the_recency_term(db):
    # MAX(0, ...) — without it a post dated next year would
    # divide by a negative age and outrank everything
    future = _score(db, "2030-01-01T00:00:00+00:00", now="?",
                    params=("2026-01-01T00:00:00+00:00",))

    assert future == pytest.approx(100.0)


def test_an_unparseable_timestamp_scores_zero_instead_of_null(db):
    assert _score(db, "not a date") == 0


def test_a_null_counter_scores_zero_instead_of_null(db):
    sql = ("SELECT " + FEED_SCORE_SQL.format(now="'now'") +
           " FROM (SELECT NULL AS published_at, NULL AS likes_count,"
           " NULL AS comments_count, NULL AS shares_count)")

    assert db.execute(sql).fetchone()[0] == 0








# -----------------------------------------------------------
# The bounds as the real routes publish them
# -----------------------------------------------------------
#
# parse_pagination is only worth anything if the numbers its
# callers pass reach the wire; these drive the shared helper
# through the routes that use it, guest and member alike.
# -----------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/news", "/api/social/feed"])
def test_a_public_feed_accepts_the_mobile_clients_defaults(client, path):
    response = client.get(f"{path}?page=1&per_page=20")

    assert response.status_code == 200


@pytest.mark.parametrize("path", ["/api/news", "/api/social/feed"])
@pytest.mark.parametrize("query", ["page=0", "page=-1", "page=abc", "per_page=0", "per_page=abc"])
def test_a_public_feed_refuses_garbage_pagination(client, path, query):
    response = client.get(f"{path}?{query}")

    assert response.status_code == 400
    assert "must be a positive integer" in response.get_json()["error"]


@pytest.mark.parametrize("path", ["/api/news", "/api/social/feed"])
def test_a_public_feed_refuses_a_per_page_of_a_hundred(client, path):
    response = client.get(f"{path}?per_page=100")

    assert response.status_code == 400
    assert response.get_json()["error"] == "per_page must be at most 50"


def test_the_social_feed_publishes_its_own_much_lower_page_cap(client):
    assert client.get("/api/social/feed?page=200").status_code == 200

    response = client.get("/api/social/feed?page=201")
    assert response.status_code == 400
    assert response.get_json()["error"] == "page must be at most 200"


def test_the_news_feed_keeps_the_shared_ten_thousand_page_cap(client):
    assert client.get("/api/news?page=10000").status_code == 200

    response = client.get("/api/news?page=10001")
    assert response.status_code == 400
    assert response.get_json()["error"] == "page must be at most 10000"


def test_the_friends_list_pages_two_hundred_at_a_time(client, actor):
    _, headers = actor

    assert client.get("/api/social/friends?per_page=200", headers=headers).status_code == 200

    response = client.get("/api/social/friends?per_page=201", headers=headers)
    assert response.status_code == 400
    assert response.get_json()["error"] == "per_page must be at most 200"


def test_the_friend_request_list_pages_two_hundred_at_a_time(client, actor):
    _, headers = actor

    response = client.get("/api/social/friends/requests?per_page=201", headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "per_page must be at most 200"


def test_a_paged_list_still_answers_the_role_gate_first(client):
    # The pagination helper must not become a way to probe an
    # authenticated route without a token
    response = client.get("/api/social/friends?per_page=201")

    assert response.status_code == 401


def test_bad_pagination_is_answered_before_the_post_is_looked_up(client):
    # get_comments parses first and looks the post up second,
    # so an unknown post with a bad page is a 400, not a 404 —
    # the guard order the route was written with
    response = client.get("/api/news/no-such-post/comments?page=0")

    assert response.status_code == 400
    assert response.get_json()["error"] == "page must be a positive integer"

    assert client.get("/api/news/no-such-post/comments").status_code == 404


def test_the_user_post_list_checks_its_required_param_before_paging(client):
    response = client.get("/api/social/posts?page=0")

    assert response.status_code == 400
    assert response.get_json()["error"] == "user_id query param required"


@pytest.mark.contract
def test_a_rejected_page_is_the_house_error_envelope(client):
    # services/api/client.ts reads {"error": "..."} off a 4xx;
    # anything else surfaces as an unknown failure in the app
    response = client.get("/api/news?per_page=0")

    body = response.get_json()
    assert response.status_code == 400
    assert list(body) == ["error"]
    assert isinstance(body["error"], str)








# -----------------------------------------------------------
# main.py — logging
# -----------------------------------------------------------


def test_logging_is_configured_at_info_by_default(monkeypatch):
    recorded = {}
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: recorded.update(kwargs))

    main._configure_logging()

    assert recorded["level"] == "INFO"


def test_the_log_level_comes_from_the_environment(monkeypatch):
    recorded = {}
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: recorded.update(kwargs))

    main._configure_logging()

    assert recorded["level"] == "DEBUG"


def test_logging_goes_to_stdout_with_a_named_formatter(monkeypatch):
    # stdout so `docker compose logs` collects it, and the
    # logger name in the line so a scrape entry is telling
    recorded = {}
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: recorded.update(kwargs))

    main._configure_logging()

    assert recorded["stream"] is sys.stdout
    for field in ("%(asctime)s", "%(levelname)s", "%(name)s", "%(message)s"):
        assert field in recorded["format"]








# -----------------------------------------------------------
# main.py — the interrupted-run reconciliation
# -----------------------------------------------------------


def test_a_run_left_running_by_a_restart_is_marked_failed(app, db):
    run_id = _seed_run(db)

    main._reconcile_scraper_runs(app)

    row = db.execute("SELECT status, error_message, finished_at FROM scraper_runs WHERE id = ?",
                     (run_id,)).fetchone()
    assert row["status"] == "failed"
    assert row["error_message"] == "interrupted"
    assert row["finished_at"]


def test_every_interrupted_run_is_closed_not_just_the_first(app, db):
    _seed_run(db, source="knf")
    _seed_run(db, source="vu")
    _seed_run(db, source="schedule")

    main._reconcile_scraper_runs(app)

    assert db.execute("SELECT COUNT(*) FROM scraper_runs WHERE status = 'running'").fetchone()[0] == 0


def test_a_finished_run_is_left_exactly_as_it_was(app, db):
    done = _seed_run(db, status="completed", finished_at="2026-01-01T01:00:00+00:00")
    failed = _seed_run(db, status="failed", finished_at="2026-01-01T01:00:00+00:00")

    main._reconcile_scraper_runs(app)

    for run_id, status in ((done, "completed"), (failed, "failed")):
        row = db.execute("SELECT status, error_message FROM scraper_runs WHERE id = ?",
                         (run_id,)).fetchone()
        assert row["status"] == status
        assert row["error_message"] is None


def test_reconciling_warns_with_the_number_of_runs_it_closed(app, db, caplog):
    _seed_run(db)
    _seed_run(db, source="vu")

    with caplog.at_level(logging.WARNING, logger="main"):
        main._reconcile_scraper_runs(app)

    assert "Reconciled 2 scraper run(s)" in caplog.text


def test_reconciling_an_untouched_table_says_nothing(app, caplog):
    with caplog.at_level(logging.WARNING, logger="main"):
        main._reconcile_scraper_runs(app)

    assert "Reconciled" not in caplog.text


def test_reconciling_is_idempotent_across_two_boots(app, db):
    run_id = _seed_run(db)

    main._reconcile_scraper_runs(app)
    first = db.execute("SELECT finished_at FROM scraper_runs WHERE id = ?", (run_id,)).fetchone()[0]
    main._reconcile_scraper_runs(app)
    second = db.execute("SELECT finished_at FROM scraper_runs WHERE id = ?", (run_id,)).fetchone()[0]

    assert first == second


def test_the_reconciliation_connection_is_closed_even_when_the_update_fails(app, monkeypatch):
    # The `finally: db.close()` — a leaked connection here
    # holds the WAL open for the life of the process
    class _Exploding:
        def __init__(self):
            self.closed = False

        def execute(self, *args):
            raise sqlite3.OperationalError("database is locked")

        def close(self):
            self.closed = True

    exploding = _Exploding()
    monkeypatch.setattr(main, "get_db", lambda: exploding)

    with pytest.raises(sqlite3.OperationalError):
        main._reconcile_scraper_runs(app)

    assert exploding.closed








# -----------------------------------------------------------
# main.py — the shutdown hooks
# -----------------------------------------------------------


def test_stopping_the_scrapers_shuts_the_scheduler_down(monkeypatch):
    stopped = []
    monkeypatch.setattr(scraper_scheduler, "stop_scraper_scheduler", lambda: stopped.append(True))

    main._stop_scrapers()

    assert stopped == [True]


def test_stopping_the_scrapers_is_a_no_op_without_a_scheduler_module(monkeypatch):
    # The banner's promise: until stop_scraper_scheduler exists
    # there is simply nothing to stop, and exit must not break
    monkeypatch.delattr(scraper_scheduler, "stop_scraper_scheduler")

    assert main._stop_scrapers() is None


def test_a_failing_shutdown_is_logged_and_swallowed(monkeypatch, caplog):
    def _boom():
        raise RuntimeError("scheduler wedged")

    monkeypatch.setattr(scraper_scheduler, "stop_scraper_scheduler", _boom)

    with caplog.at_level(logging.ERROR, logger="main"):
        main._stop_scrapers()

    assert "Scraper scheduler shutdown failed" in caplog.text
    assert "scheduler wedged" in caplog.text


def test_sigterm_becomes_a_clean_interpreter_exit(caplog):
    with caplog.at_level(logging.INFO, logger="main"):
        with pytest.raises(SystemExit) as exit_info:
            main._handle_sigterm(signal_module.SIGTERM, None)

    assert exit_info.value.code == 0
    assert "SIGTERM received" in caplog.text








# -----------------------------------------------------------
# main.py — the CLI
# -----------------------------------------------------------


def test_without_http_the_help_is_printed_and_nothing_is_built(monkeypatch, app, capsys):
    calls = _serve(monkeypatch, app, ["main.py"])
    out = capsys.readouterr().out

    assert calls["created"] == 0, "create_app must not run without --http"
    assert calls["logging"] == 0
    assert calls["run"] is None
    assert "--http" in out
    assert "usage" in out.lower()


def test_the_help_flag_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["main.py", "--help"])

    with pytest.raises(SystemExit) as exit_info:
        main.main()

    assert exit_info.value.code == 0
    assert "Port (default 8000)" in capsys.readouterr().out


def test_an_unknown_flag_is_refused(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["main.py", "--serve-everything"])

    with pytest.raises(SystemExit) as exit_info:
        main.main()

    assert exit_info.value.code == 2


def test_a_non_numeric_port_is_refused(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["main.py", "--http", "--port", "eight-thousand"])

    with pytest.raises(SystemExit) as exit_info:
        main.main()

    assert exit_info.value.code == 2


def test_running_the_module_as_a_script_prints_the_help(monkeypatch, capsys):
    # The __main__ guard the Dockerfile CMD lands on
    monkeypatch.setattr(sys, "argv", ["main.py"])

    runpy.run_path(_MAIN_PATH, run_name="__main__")

    assert "--http" in capsys.readouterr().out








# -----------------------------------------------------------
# main.py — serving
# -----------------------------------------------------------


def test_http_serves_the_built_app_on_the_documented_defaults(monkeypatch, app):
    # EXPOSE 8000 and the Caddyfile upstream both assume these
    calls = _serve(monkeypatch, app, ["main.py", "--http"])
    served_app, kwargs = calls["run"]

    assert served_app is app
    assert calls["created"] == 1
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["port"] == 8000
    assert kwargs["allow_unsafe_werkzeug"] is True


def test_host_and_port_override_the_defaults(monkeypatch, app):
    calls = _serve(monkeypatch, app, ["main.py", "--http", "--host", "127.0.0.1", "--port", "9331"])
    _, kwargs = calls["run"]

    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 9331


def test_the_boot_order_is_logging_then_app_then_reconcile_then_serve(monkeypatch, app):
    # Logging first or every line of the boot is discarded;
    # the reconciliation after create_app, which is what pins
    # get_db()'s path
    calls = _serve(monkeypatch, app, ["main.py", "--http"])

    assert calls["order"] == ["logging", "create_app", "reconcile", "serve"]
    assert calls["reconciled"] == [app]


def test_the_interrupted_runs_are_reconciled_for_real_on_the_way_up(monkeypatch, app, db):
    run_id = _seed_run(db)

    _serve(monkeypatch, app, ["main.py", "--http"])

    assert db.execute("SELECT status FROM scraper_runs WHERE id = ?",
                      (run_id,)).fetchone()[0] == "failed"


@pytest.mark.parametrize("value", [None, "0", "true", "yes", "1 ", "", "01"])
def test_debug_is_off_unless_app_debug_is_exactly_one(monkeypatch, app, value):
    if value is None:
        monkeypatch.delenv("APP_DEBUG", raising=False)
    else:
        monkeypatch.setenv("APP_DEBUG", value)

    _, kwargs = _serve(monkeypatch, app, ["main.py", "--http"])["run"]

    assert kwargs["debug"] is False


def test_app_debug_one_turns_the_flask_debug_flag_on(monkeypatch, app):
    monkeypatch.setenv("APP_DEBUG", "1")

    _, kwargs = _serve(monkeypatch, app, ["main.py", "--http"])["run"]

    assert kwargs["debug"] is True


@pytest.mark.parametrize("debug_env", ["0", "1"])
def test_the_debugger_and_the_reloader_stay_off_in_both_modes(monkeypatch, app, debug_env):
    # use_debugger would publish a code-execution console at
    # /api; use_reloader would build the app, the scheduler and
    # the startup timers twice
    monkeypatch.setenv("APP_DEBUG", debug_env)

    _, kwargs = _serve(monkeypatch, app, ["main.py", "--http"])["run"]

    assert kwargs["use_debugger"] is False
    assert kwargs["use_reloader"] is False


def test_the_scheduler_and_its_exit_hooks_start_when_scrapers_are_enabled(monkeypatch, app):
    monkeypatch.setenv("SCRAPER_ENABLED", "1")

    calls = _serve(monkeypatch, app, ["main.py", "--http"])

    assert calls["scheduled"] == [app]
    assert calls["atexit"] == [main._stop_scrapers]
    assert calls["signals"] == [(signal_module.SIGTERM, main._handle_sigterm)]
    assert calls["order"] == ["logging", "create_app", "reconcile", "start_scheduler", "serve"]


def test_the_scheduler_is_the_default_when_the_flag_is_unset(monkeypatch, app):
    monkeypatch.delenv("SCRAPER_ENABLED", raising=False)

    calls = _serve(monkeypatch, app, ["main.py", "--http"])

    assert calls["scheduled"] == [app]


def test_scraper_enabled_zero_leaves_every_background_thread_off(monkeypatch, app, caplog):
    monkeypatch.setenv("SCRAPER_ENABLED", "0")

    with caplog.at_level(logging.INFO, logger="main"):
        calls = _serve(monkeypatch, app, ["main.py", "--http"])

    assert calls["scheduled"] == []
    assert calls["atexit"] == []
    assert calls["signals"] == []
    assert calls["run"] is not None, "the server still starts with the scrapers off"
    assert "SCRAPER_ENABLED=0" in caplog.text


def test_only_the_serving_process_starts_a_scheduler(monkeypatch, app):
    # Building an app must never fire a scrape: the scheduler
    # is main()'s job, and only inside --http
    monkeypatch.setenv("SCRAPER_ENABLED", "1")

    calls = _serve(monkeypatch, app, ["main.py"])

    assert calls["scheduled"] == []
    assert calls["atexit"] == []
    assert calls["signals"] == []
