# -----------------------------------------------------------
#  [*] Tests — scraper runtime, the exhaustive pass
#
#  The gap-closing sweep over scraper/scheduler.py (the whole
#  scheduler wiring) and scraper/routes.py (every
#  /api/scraper/* route and the role gate above it). The
#  broad file next door proves the happy shapes; this one
#  walks the edges:
#
#    - the role gate from every direction a caller can reach
#      it: no header, a header without the Bearer scheme, the
#      scheme in any case, padding around the token, an
#      unknown token, an expired session, a user deactivated
#      or deleted AFTER the token was minted, and the two
#      live role changes (an admin demoted mid-session, a
#      student promoted mid-session)
#    - what answers BEFORE the gate does: a wrong verb is 405
#      and a non-object JSON body is 400, both without a
#      token, so neither ever reaches a scraper
#    - /status's two filters to the letter — trimmed, the
#      status one lower-cased and validated against the three
#      stored states, the source one exact and case-SENSITIVE
#      (no LIKE, no pattern, no injection), a repeated
#      parameter taking the first value, the summary block
#      honouring the source filter and ignoring the status
#      one, and the LIMIT 20 boundary at 19 / 20 / 21 rows
#    - the trigger status mapping as a full matrix over the
#      two news results — failure outranking conflict,
#      conflict outranking success, a non-dict counting as a
#      failure, and every FALSY "error"/"skipped" value
#      staying a 200
#    - the two database error paths a route can hit: a locked
#      database is a 503 with a retry code, any other SQLite
#      error a 500
#    - the scheduler's env gate in every spelling, its
#      per-process guard (including the one asymmetry: the
#      gate is read BEFORE the guard, so switching it off
#      does not stop a scheduler that is already up), the
#      stop path with and without a scheduler, and the
#      one-shot Timers' exact delays, jitter bounds, daemon
#      flag and identity with the interval jobs they stand in
#      for
#    - the three bookkeeping helpers at their boundaries:
#      _ran_within's strict ">", _reconcile_interrupted_runs'
#      strict "<" and its stale-run budget, and the push-token
#      prune's strict "expires_at >" — each one frozen with
#      time_machine so "exactly at the cutoff" is exact, each
#      one checked for the commit it owes and the connection
#      it must close even when the statement raises
#
#  No test here reaches the network: the four scrapers are
#  replaced by recorders at the names routes.py bound at
#  import. Nothing sleeps — time_machine freezes the clock
#  wherever a cutoff is on trial.
# -----------------------------------------------------------


import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import time_machine
from flask import has_app_context

from app.scraper import routes as scraper_routes
from app.scraper import scheduler


STATUS = "/api/scraper/status"
TRIGGER = "/api/scraper/trigger"
RUN = "/api/scraper/run"
SCHEDULE = "/api/scraper/schedule"
INFO = "/api/scraper/info"

# Every path this blueprint publishes with the verb it
# answers — the gate tests walk all five
ENDPOINTS = (
    ("get", STATUS),
    ("post", TRIGGER),
    ("post", RUN),
    ("post", SCHEDULE),
    ("post", INFO),
)

# The four that actually reach a scraper
TRIGGER_PATHS = (TRIGGER, RUN, SCHEDULE, INFO)

# The wire shape of one scraper_runs row (_run_row)
RUN_KEYS = {"id", "source", "status", "articlesFound", "articlesNew",
            "itemsFound", "itemsNew", "error", "startedAt", "finishedAt"}

# A fixed instant on today's date: the boundary tests need
# "exactly at the cutoff" to be exact, and staying on today
# keeps the bearer token the admin fixture minted valid
FROZEN = datetime.now(timezone.utc).replace(hour=9, minute=0, second=0, microsecond=0)

# The intervals the startup one-shots stand in for, and the
# base delay of each — the skip rule is keyed off the first,
# the Timer delay off the second
NEWS_INTERVAL = 20 * 60
SCHEDULE_INTERVAL = 6 * 3600
INFO_INTERVAL = 24 * 3600
STARTUP_DELAYS = (2.0, 30.0, 60.0)




# -----------------------------------------------------------
# _iso
# -----------------------------------------------------------
#
# An aware-UTC stamp offset from `base`, in exactly the shape
# utc_now_iso() writes — scraper_runs sorts and compares
# these as TEXT, so the format has to match or the SQL stops
# being chronological.
# -----------------------------------------------------------

def _iso(base=None, **delta):
    return ((base or datetime.now(timezone.utc)) + timedelta(**delta)).isoformat()




# -----------------------------------------------------------
# _clean_rate_limit_store
# -----------------------------------------------------------
#
# The per-IP budget in auth/routes.py is PROCESS state and
# every test in the suite logs in from 127.0.0.1. Clearing it
# around each test keeps a 429 out of an assertion about a
# 403.
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_rate_limit_store():
    from app.auth.routes import _rate_limit_store

    _rate_limit_store.clear()
    yield
    _rate_limit_store.clear()




# -----------------------------------------------------------
# _no_scheduler_survives_a_test
# -----------------------------------------------------------
#
# scheduler._scheduler and scheduler._startup_timers are
# module-level PROCESS state: one test that left a scheduler
# up would turn every later start into the "already running"
# no-op. Stopped on the way in as well as out, so a crashed
# test cannot poison the rest of the file.
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_scheduler_survives_a_test():
    scheduler.stop_scraper_scheduler()
    yield
    scheduler.stop_scraper_scheduler()




# -----------------------------------------------------------
# seed_run
# -----------------------------------------------------------
#
#   seed_run(source="vu.lt", status="failed", minutes=-90)
#
# One scraper_runs row inserted directly: /status has to
# render runs no route can create — a row a dead process left
# 'running', a source that has not succeeded since spring, a
# stamp exactly on a cutoff.
# -----------------------------------------------------------

@pytest.fixture
def seed_run(app):

    def _seed(source="knf.vu.lt", status="completed", started_at=None, found=0, new=0,
              error=None, finished_at=None, run_id=None, base=None, **delta):
        run_id = run_id or str(uuid.uuid4())
        started_at = started_at if started_at is not None else _iso(base=base, **delta)

        conn = sqlite3.connect(app.config["DB_PATH"])
        try:
            conn.execute(
                """INSERT INTO scraper_runs
                   (id, source, status, articles_found, articles_new, error_message,
                    started_at, finished_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, source, status, found, new, error, started_at, finished_at),
            )
            conn.commit()
        finally:
            conn.close()

        return run_id

    return _seed




# -----------------------------------------------------------
# seed_session / seed_push_token
# -----------------------------------------------------------
#
# The rows the two housekeeping jobs exist to delete. Only
# expires_at matters to them, so the token is any opaque
# string.
# -----------------------------------------------------------

@pytest.fixture
def seed_session(app):

    def _seed(user_id, expires_at=None, **delta):
        conn = sqlite3.connect(app.config["DB_PATH"])
        try:
            conn.execute(
                "INSERT INTO sessions (id, user_id, token, expires_at) VALUES (?, ?, ?, ?)",
                (str(uuid.uuid4()), user_id, uuid.uuid4().hex,
                 expires_at if expires_at is not None else _iso(**delta)),
            )
            conn.commit()
        finally:
            conn.close()

    return _seed


@pytest.fixture
def seed_push_token(app):

    def _seed(user_id, active=1):
        token = f"ExponentPushToken[{uuid.uuid4().hex[:16]}]"
        conn = sqlite3.connect(app.config["DB_PATH"])
        try:
            conn.execute(
                "INSERT INTO push_tokens (id, user_id, token, platform, active)"
                " VALUES (?, ?, ?, 'ios', ?)",
                (str(uuid.uuid4()), user_id, token, active),
            )
            conn.commit()
        finally:
            conn.close()

        return token

    return _seed




# -----------------------------------------------------------
# sql
# -----------------------------------------------------------
#
# A one-shot writer/reader on the test database for the
# arrangements a route cannot make (deactivating a user after
# its token was minted) and the assertions that must not ride
# the route's own connection.
# -----------------------------------------------------------

@pytest.fixture
def sql(app):

    def _sql(statement, params=(), fetch=False):
        conn = sqlite3.connect(app.config["DB_PATH"])
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.execute(statement, params)
            rows = cursor.fetchall() if fetch else None
            conn.commit()
            return rows
        finally:
            conn.close()

    return _sql




# -----------------------------------------------------------
# scrapers
# -----------------------------------------------------------
#
# The four scrapers as routes.py bound them at IMPORT time —
# the trigger routes call them synchronously inside the
# request, so every route test that is about status mapping,
# body shape or the gate substitutes them here.
#
#   scrapers.results["knf"] = {...}   — what the next call
#                                       returns (an exception
#                                       instance is raised)
#   scrapers.calls                    — (name, args, kwargs)
#
# The recorder hands back the SAME object it was given, not a
# copy, so a test can prove the route never writes into the
# scraper's own result dict.
# -----------------------------------------------------------

class _ScraperBench:

    def __init__(self):
        self.calls = []
        self.results = {
            "knf": {"found": 5, "new": 2, "runId": "knf-run"},
            "vu": {"found": 3, "new": 0, "runId": "vu-run"},
            "schedule": {"groups_scraped": 4, "lessons_found": 88, "lessons_new": 12,
                         "dropped": 1, "runId": "sch-run"},
            "info": {"pages_scraped": 3, "contacts_found": 21, "programs_found": 9,
                     "runId": "info-run"},
        }

    def names(self):
        return [name for name, _args, _kwargs in self.calls]

    def kwargs_of(self, name):
        return [kwargs for called, _args, kwargs in self.calls if called == name]

    def args_of(self, name):
        return [args for called, args, _kwargs in self.calls if called == name]


@pytest.fixture
def scrapers(monkeypatch):
    bench = _ScraperBench()

    def _recorder(name):
        def _fake(*args, **kwargs):
            bench.calls.append((name, args, kwargs))
            outcome = bench.results[name]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return _fake

    monkeypatch.setattr(scraper_routes, "scrape_knf_news", _recorder("knf"))
    monkeypatch.setattr(scraper_routes, "scrape_vu_news", _recorder("vu"))
    monkeypatch.setattr(scraper_routes, "scrape_knf_schedule", _recorder("schedule"))
    monkeypatch.setattr(scraper_routes, "scrape_faculty_info", _recorder("info"))

    return bench




# -----------------------------------------------------------
# armed_timers
# -----------------------------------------------------------
#
# threading.Timer replaced for one test, collecting what
# start_scraper_scheduler armed. The real thing would fire a
# live scrape seconds later, which is exactly what must never
# happen in the test container — and the recorder is how the
# delay, the daemon flag and the cancel-on-stop become
# assertable.
# -----------------------------------------------------------

@pytest.fixture
def armed_timers(monkeypatch):
    armed = []

    class _RecordingTimer:
        def __init__(self, interval, function):
            self.interval = interval
            self.function = function
            self.daemon = False
            self.started = False
            self.cancelled = False
            armed.append(self)

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

    monkeypatch.setattr(threading, "Timer", _RecordingTimer)
    return armed




# -----------------------------------------------------------
# fixed_jitter
# -----------------------------------------------------------
#
# scheduler.random replaced wholesale (never the stdlib
# module itself — the rest of the suite draws from it too),
# so a test can pin the exact Timer delay instead of a range,
# and read back what the code asked for.
# -----------------------------------------------------------

@pytest.fixture
def fixed_jitter(monkeypatch):

    class _FixedRandom:
        def __init__(self):
            self.value = 0.0
            self.calls = []

        def uniform(self, low, high):
            self.calls.append((low, high))
            return self.value

    fake = _FixedRandom()
    monkeypatch.setattr(scheduler, "random", fake)
    return fake




# -----------------------------------------------------------
# running_scheduler / job
# -----------------------------------------------------------
#
# A started scheduler with the startup one-shots off — the
# only configuration a test may start, since the interval
# jobs' first tick is fifteen minutes away and nothing fires
# while the test runs. `job` reaches into the registry for a
# closure defined inside start_scraper_scheduler and
# reachable nowhere else.
# -----------------------------------------------------------

@pytest.fixture
def running_scheduler(app, monkeypatch):
    monkeypatch.setenv("SCRAPER_ENABLED", "1")
    monkeypatch.setenv("SCRAPER_STARTUP_RUNS", "0")

    assert scheduler.start_scraper_scheduler(app) is True
    yield scheduler
    scheduler.stop_scraper_scheduler()


@pytest.fixture
def job(running_scheduler):

    def _job(job_id):
        found = running_scheduler._scheduler.get_job(job_id)
        assert found is not None, f"no job registered as {job_id}"
        return found

    return _job




# -----------------------------------------------------------
# _RecordingConnection
# -----------------------------------------------------------
#
# A get_db() stand-in that can fail on demand and always
# reports whether it was committed and closed. The bookkeeping
# helpers open their own connection and close it in a
# `finally`; nothing else can prove the finally fires.
# -----------------------------------------------------------

class _RecordingConnection:

    def __init__(self, real, fail=None):
        self._real = real
        self.fail = fail
        self.closed = 0
        self.commits = 0
        self.statements = []

    def execute(self, statement, params=()):
        self.statements.append(statement)
        if self.fail is not None:
            raise self.fail
        return self._real.execute(statement, params)

    def commit(self):
        self.commits += 1
        return self._real.commit()

    def close(self):
        self.closed += 1
        return self._real.close()


def _recording_db(app, monkeypatch, fail=None):
    holder = {}

    def _open():
        conn = sqlite3.connect(app.config["DB_PATH"])
        conn.row_factory = sqlite3.Row
        holder["conn"] = _RecordingConnection(conn, fail=fail)
        return holder["conn"]

    monkeypatch.setattr(scheduler, "get_db", _open)
    return holder




# ===========================================================
#  ROLE GATE — everything that can carry (or fail to carry)
#  an admin identity into these five routes
# ===========================================================

@pytest.mark.parametrize("method,path", ENDPOINTS)
def test_a_request_with_no_authorization_header_is_refused_and_reaches_no_scraper(
        client, scrapers, method, path):
    response = getattr(client, method)(path)

    assert response.status_code == 401
    assert response.get_json() == {"error": "Authentication required"}
    assert scrapers.calls == []


@pytest.mark.parametrize("header", [
    "Basic YWRtaW46cGFzcw==",
    "Token abc123",
    "abc123",
    "Bearer",
    "Bearer ",
    "Bearer    ",
    "",
])
def test_an_authorization_header_without_a_usable_bearer_token_is_refused(client, scrapers, header):
    response = client.post(TRIGGER, headers={"Authorization": header})

    assert response.status_code == 401
    assert scrapers.calls == []


def test_a_bearer_token_glued_to_the_scheme_is_not_a_token(client, admin, scrapers):
    _user, headers = admin
    glued = headers["Authorization"].replace("Bearer ", "Bearer")

    response = client.post(TRIGGER, headers={"Authorization": glued})

    assert response.status_code == 401
    assert scrapers.calls == []


@pytest.mark.parametrize("scheme", ["bearer", "BEARER", "BeArEr", "Bearer"])
def test_the_bearer_scheme_is_matched_whatever_its_case(client, admin, scheme):
    _user, headers = admin
    token = headers["Authorization"].split(" ", 1)[1]

    response = client.get(STATUS, headers={"Authorization": f"{scheme} {token}"})

    assert response.status_code == 200


@pytest.mark.parametrize("shape", ["Bearer   {t}", "Bearer {t} ", "Bearer  {t}  "])
def test_padding_around_the_token_is_trimmed_before_the_lookup(client, admin, shape):
    _user, headers = admin
    token = headers["Authorization"].split(" ", 1)[1]

    response = client.get(STATUS, headers={"Authorization": shape.format(t=token)})

    assert response.status_code == 200


@pytest.mark.parametrize("token", ["", " ", "not-a-token", "0" * 64, "null", "undefined"])
def test_a_token_that_matches_no_session_is_refused(client, scrapers, token):
    response = client.post(SCHEDULE, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert scrapers.calls == []


@pytest.mark.parametrize("method,path", ENDPOINTS)
def test_an_expired_admin_session_is_refused_on_every_route(client, admin, sql, scrapers, method, path):
    _user, headers = admin
    sql("UPDATE sessions SET expires_at = ?", (_iso(days=-1),))

    response = getattr(client, method)(path, headers=headers)

    assert response.status_code == 401
    assert scrapers.calls == []


def test_an_admin_deactivated_after_login_loses_the_routes(client, admin, sql, scrapers):
    user, headers = admin
    sql("UPDATE users SET active = 0 WHERE id = ?", (user["id"],))

    response = client.post(TRIGGER, headers=headers)

    assert response.status_code == 401
    assert scrapers.calls == []


def test_an_admin_whose_user_row_vanished_is_refused(client, make_user, auth_headers, sql, scrapers):
    ghost = make_user(role="admin")
    headers = auth_headers(ghost)
    sql("DELETE FROM users WHERE id = ?", (ghost["id"],))

    response = client.post(INFO, headers=headers)

    assert response.status_code == 401
    assert scrapers.calls == []


def test_an_admin_demoted_mid_session_is_refused_by_the_role_gate(client, admin, sql, scrapers):
    user, headers = admin
    sql("UPDATE users SET role = 'student' WHERE id = ?", (user["id"],))

    response = client.post(TRIGGER, headers=headers)

    assert response.status_code == 403
    assert response.get_json() == {"error": "Insufficient permissions"}
    assert scrapers.calls == []


def test_a_student_promoted_mid_session_is_let_through(client, actor, sql, scrapers):
    user, headers = actor
    sql("UPDATE users SET role = 'admin' WHERE id = ?", (user["id"],))

    response = client.post(TRIGGER, headers=headers)

    assert response.status_code == 200
    assert scrapers.names() == ["knf", "vu"]


@pytest.mark.parametrize("role", ["student", "teacher", "curator"])
@pytest.mark.parametrize("method,path", ENDPOINTS)
def test_every_role_below_admin_is_refused_with_the_same_slug(client, make_user, auth_headers,
                                                              scrapers, role, method, path):
    user = make_user(role=role)

    response = getattr(client, method)(path, headers=auth_headers(user))

    assert response.status_code == 403
    assert response.get_json() == {"error": "Insufficient permissions"}
    assert scrapers.calls == []


@pytest.mark.parametrize("method,path", ENDPOINTS)
def test_an_admin_reaches_every_route(client, admin, scrapers, method, path):
    _user, headers = admin

    response = getattr(client, method)(path, headers=headers)

    assert response.status_code == 200


def test_the_role_gate_compares_the_role_exactly(client, make_user, auth_headers, sql, scrapers):
    # 'ADMIN' cannot be stored (the users CHECK constraint
    # allows four lower-case roles), which is what keeps the
    # exact `not in roles` comparison safe
    user = make_user(role="student")
    headers = auth_headers(user)

    with pytest.raises(sqlite3.IntegrityError):
        sql("UPDATE users SET role = 'ADMIN' WHERE id = ?", (user["id"],))

    assert client.post(TRIGGER, headers=headers).status_code == 403
    assert scrapers.calls == []




# ===========================================================
#  WHAT ANSWERS BEFORE THE GATE — dispatch and body checks
#  that a caller hits without ever presenting a token
# ===========================================================

@pytest.mark.parametrize("path", TRIGGER_PATHS)
@pytest.mark.parametrize("method", ["get", "put", "patch", "delete"])
def test_a_wrong_verb_on_a_trigger_is_405_before_authentication(client, scrapers, path, method):
    response = getattr(client, method)(path)

    assert response.status_code == 405
    assert response.get_json() == {"error": "Method not allowed"}
    assert scrapers.calls == []


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_a_wrong_verb_on_status_is_405_before_authentication(client, method):
    response = getattr(client, method)(STATUS)

    assert response.status_code == 405
    assert response.get_json() == {"error": "Method not allowed"}


def test_status_answers_head_the_way_it_answers_get(client, admin):
    _user, headers = admin

    response = client.head(STATUS, headers=headers)

    assert response.status_code == 200
    assert response.get_data() == b""


@pytest.mark.parametrize("path", [STATUS + "/", TRIGGER + "/", "/api/scraper", "/api/scraper/",
                                  "/api/scraper/nope", "/api/scraper/status/1"])
def test_no_neighbouring_path_answers_for_these_routes(client, admin, scrapers, path):
    _user, headers = admin

    response = client.get(path, headers=headers)

    assert response.status_code in (404, 405)
    assert scrapers.calls == []


@pytest.mark.parametrize("path", TRIGGER_PATHS)
def test_a_preflight_needs_no_token_and_starts_no_scrape(client, scrapers, path):
    response = client.options(path, headers={"Origin": "http://localhost:8081",
                                             "Access-Control-Request-Method": "POST"})

    assert response.status_code == 200
    assert scrapers.calls == []


@pytest.mark.parametrize("body", [b"[1, 2, 3]", b'"a string"', b"42", b"true", b"0"])
def test_a_json_body_that_is_not_an_object_is_refused_before_the_gate(client, scrapers, body):
    # The before_request validator runs ahead of the view, so
    # this 400 arrives WITHOUT a token — and the scrapers stay
    # untouched either way
    response = client.post(TRIGGER, data=body, headers={"Content-Type": "application/json"})

    assert response.status_code == 400
    assert response.get_json() == {"error": "JSON body must be an object"}
    assert scrapers.calls == []


def test_a_json_null_body_is_indistinguishable_from_no_body_and_reaches_the_gate(client, scrapers):
    # `null` parses to None, which the validator reads as "no
    # body at all" — so this one falls through to the 401
    # instead of the 400 its four siblings get
    response = client.post(TRIGGER, data=b"null", headers={"Content-Type": "application/json"})

    assert response.status_code == 401
    assert scrapers.calls == []


@pytest.mark.parametrize("kwargs", [
    {"data": b"not json at all", "content_type": "application/json"},
    {"data": b"", "content_type": "application/json"},
    {"data": b"plain text", "content_type": "text/plain"},
    {"data": {"a": "b"}},
    {"json": {"pages": 99, "notify": True}},
])
def test_a_trigger_ignores_whatever_body_it_is_given(client, admin, scrapers, kwargs):
    _user, headers = admin

    response = client.post(TRIGGER, headers=headers, **kwargs)

    assert response.status_code == 200
    assert scrapers.kwargs_of("knf") == [{"pages": 2, "notify": False}]


def test_a_trigger_ignores_query_parameters_too(client, admin, scrapers):
    _user, headers = admin

    response = client.post(f"{TRIGGER}?pages=99&notify=1&source=vu.lt", headers=headers)

    assert response.status_code == 200
    assert scrapers.kwargs_of("knf") == [{"pages": 2, "notify": False}]
    assert scrapers.kwargs_of("vu") == [{"pages": 1, "notify": False}]




# ===========================================================
#  /status — the two filters, to the letter
# ===========================================================

def _runs(response):
    return response.get_json()["runs"]


def _sources(response):
    return response.get_json()["sources"]


@pytest.mark.parametrize("query", ["source=", "source=%20", "source=%20%20%20",
                                   "status=", "status=%20", "source=&status="])
def test_a_blank_filter_is_no_filter_at_all(client, admin, seed_run, query):
    _user, headers = admin
    seed_run(source="knf.vu.lt", status="completed", minutes=-5)
    seed_run(source="vu.lt", status="failed", minutes=-4)

    response = client.get(f"{STATUS}?{query}", headers=headers)

    assert response.status_code == 200
    assert len(_runs(response)) == 2


@pytest.mark.parametrize("raw", ["completed", "COMPLETED", "Completed", "%20completed%20",
                                 "%20COMPLETED", "completed%20"])
def test_the_status_filter_is_trimmed_and_case_folded(client, admin, seed_run, raw):
    _user, headers = admin
    seed_run(source="knf.vu.lt", status="completed", minutes=-5)
    seed_run(source="knf.vu.lt", status="failed", minutes=-4)

    response = client.get(f"{STATUS}?status={raw}", headers=headers)

    assert [r["status"] for r in _runs(response)] == ["completed"]


@pytest.mark.parametrize("status", ["running", "completed", "failed"])
def test_each_stored_status_can_be_asked_for_by_name(client, admin, seed_run, status):
    _user, headers = admin
    for index, name in enumerate(("running", "completed", "failed")):
        seed_run(source="knf.vu.lt", status=name, minutes=-index - 1)

    response = client.get(f"{STATUS}?status={status}", headers=headers)

    assert [r["status"] for r in _runs(response)] == [status]


@pytest.mark.parametrize("bogus", ["done", "COMPLETE", "succeeded", "0", "null", "running%20now",
                                   "completed%2Cfailed", "'%20OR%201=1%20--"])
def test_a_status_nobody_stores_is_ignored_rather_than_refused(client, admin, seed_run, bogus):
    _user, headers = admin
    seed_run(source="knf.vu.lt", status="completed", minutes=-5)
    seed_run(source="vu.lt", status="failed", minutes=-4)

    response = client.get(f"{STATUS}?status={bogus}", headers=headers)

    assert response.status_code == 200
    assert len(_runs(response)) == 2


def test_a_repeated_status_parameter_takes_the_first_value(client, admin, seed_run):
    _user, headers = admin
    seed_run(source="knf.vu.lt", status="completed", minutes=-5)
    seed_run(source="vu.lt", status="failed", minutes=-4)

    response = client.get(f"{STATUS}?status=failed&status=completed", headers=headers)

    assert [r["status"] for r in _runs(response)] == ["failed"]


def test_a_repeated_source_parameter_takes_the_first_value(client, admin, seed_run):
    _user, headers = admin
    seed_run(source="knf.vu.lt", minutes=-5)
    seed_run(source="vu.lt", minutes=-4)

    response = client.get(f"{STATUS}?source=vu.lt&source=knf.vu.lt", headers=headers)

    assert [r["source"] for r in _runs(response)] == ["vu.lt"]


def test_a_padded_source_filter_is_trimmed_to_the_bare_name(client, admin, seed_run):
    _user, headers = admin
    seed_run(source="knf.vu.lt", minutes=-5)

    response = client.get(f"{STATUS}?source=%20%20knf.vu.lt%20%20", headers=headers)

    assert [r["source"] for r in _runs(response)] == ["knf.vu.lt"]


def test_a_source_whose_name_carries_padding_cannot_be_filtered_for(client, admin, seed_run):
    # The filter is stripped before the comparison, so a
    # source stored WITH spaces is unreachable — it still
    # shows up unfiltered, which is the only way to see it
    _user, headers = admin
    seed_run(source="  spaced  ", minutes=-5)

    padded = client.get(f"{STATUS}?source=%20%20spaced%20%20", headers=headers)
    unfiltered = client.get(STATUS, headers=headers)

    assert _runs(padded) == []
    assert [r["source"] for r in _runs(unfiltered)] == ["  spaced  "]


@pytest.mark.parametrize("wrong_case", ["KNF.VU.LT", "Knf.Vu.Lt", "knf.VU.lt"])
def test_the_source_filter_is_case_sensitive_unlike_the_status_one(client, admin, seed_run, wrong_case):
    _user, headers = admin
    seed_run(source="knf.vu.lt", minutes=-5)

    response = client.get(f"{STATUS}?source={wrong_case}", headers=headers)

    assert _runs(response) == []
    assert _sources(response) == []


@pytest.mark.parametrize("pattern", ["knf%25", "%25", "%25vu%25", "_nf.vu.lt", "knf.vu.l_",
                                     "knf.vu.lt%00"])
def test_the_source_filter_is_an_exact_match_never_a_pattern(client, admin, seed_run, pattern):
    _user, headers = admin
    seed_run(source="knf.vu.lt", minutes=-5)

    response = client.get(f"{STATUS}?source={pattern}", headers=headers)

    assert _runs(response) == []


def test_a_quoted_source_filter_is_data_and_not_sql(client, admin, seed_run, sql):
    _user, headers = admin
    seed_run(source="knf.vu.lt", minutes=-5)

    response = client.get(STATUS, headers=headers,
                          query_string={"source": "' OR 1=1 --"})

    assert response.status_code == 200
    assert _runs(response) == []
    assert len(sql("SELECT id FROM scraper_runs", fetch=True)) == 1


def test_a_source_filter_that_tries_to_drop_the_table_leaves_it_standing(client, admin, seed_run, sql):
    _user, headers = admin
    seed_run(source="knf.vu.lt", minutes=-5)

    response = client.get(STATUS, headers=headers,
                          query_string={"source": "x'; DROP TABLE scraper_runs; --"})

    assert response.status_code == 200
    assert len(sql("SELECT id FROM scraper_runs", fetch=True)) == 1


def test_a_ten_thousand_character_source_filter_is_answered_empty(client, admin, seed_run):
    _user, headers = admin
    seed_run(source="knf.vu.lt", minutes=-5)

    response = client.get(STATUS, headers=headers, query_string={"source": "x" * 10_000})

    assert response.status_code == 200
    assert _runs(response) == []


def test_a_unicode_source_can_be_filtered_for(client, admin, seed_run):
    _user, headers = admin
    seed_run(source="tvarkaraščiai.vu.lt", minutes=-5)
    seed_run(source="knf.vu.lt", minutes=-4)

    response = client.get(STATUS, headers=headers, query_string={"source": "tvarkaraščiai.vu.lt"})

    assert [r["source"] for r in _runs(response)] == ["tvarkaraščiai.vu.lt"]


def test_both_filters_narrow_together(client, admin, seed_run):
    _user, headers = admin
    seed_run(source="knf.vu.lt", status="completed", minutes=-5)
    seed_run(source="knf.vu.lt", status="failed", minutes=-4)
    seed_run(source="vu.lt", status="failed", minutes=-3)

    response = client.get(STATUS, headers=headers,
                          query_string={"source": "knf.vu.lt", "status": "failed"})

    rows = _runs(response)
    assert len(rows) == 1
    assert (rows[0]["source"], rows[0]["status"]) == ("knf.vu.lt", "failed")


def test_two_filters_that_agree_on_nothing_answer_an_empty_run_list(client, admin, seed_run):
    _user, headers = admin
    seed_run(source="knf.vu.lt", status="completed", minutes=-5)

    response = client.get(STATUS, headers=headers,
                          query_string={"source": "knf.vu.lt", "status": "running"})

    assert _runs(response) == []
    assert [s["source"] for s in _sources(response)] == ["knf.vu.lt"]




# ===========================================================
#  /status — the twenty-row cap and the ordering under it
# ===========================================================

@pytest.mark.parametrize("seeded,expected", [(0, 0), (1, 1), (19, 19), (20, 20), (21, 20), (25, 20)])
def test_the_run_list_is_capped_at_twenty_rows(client, admin, seed_run, seeded, expected):
    _user, headers = admin
    for index in range(seeded):
        seed_run(source="knf.vu.lt", minutes=-index - 1)

    response = client.get(STATUS, headers=headers)

    assert len(_runs(response)) == expected


def test_the_row_the_cap_drops_is_the_oldest_one(client, admin, seed_run):
    _user, headers = admin
    oldest = seed_run(source="knf.vu.lt", minutes=-99)
    for index in range(20):
        seed_run(source="knf.vu.lt", minutes=-index - 1)

    response = client.get(STATUS, headers=headers)

    assert oldest not in [r["id"] for r in _runs(response)]
    assert len(_runs(response)) == 20


def test_the_cap_applies_after_the_filters_not_before(client, admin, seed_run):
    _user, headers = admin
    for index in range(25):
        seed_run(source="knf.vu.lt", minutes=-index - 100)
    for index in range(5):
        seed_run(source="vu.lt", minutes=-index - 1)

    filtered = client.get(STATUS, headers=headers, query_string={"source": "vu.lt"})
    unfiltered = client.get(STATUS, headers=headers)

    assert len(_runs(filtered)) == 5
    assert len(_runs(unfiltered)) == 20


def test_the_newest_run_leads_whatever_the_insertion_order(client, admin, seed_run):
    _user, headers = admin
    newest = seed_run(source="knf.vu.lt", minutes=-1)
    seed_run(source="knf.vu.lt", minutes=-60)
    seed_run(source="knf.vu.lt", minutes=-30)

    response = client.get(STATUS, headers=headers)

    assert _runs(response)[0]["id"] == newest


def test_the_text_sort_stays_chronological_across_a_year_boundary(client, admin, seed_run):
    _user, headers = admin
    old = seed_run(source="knf.vu.lt", started_at="2025-12-31T23:59:59+00:00")
    new = seed_run(source="knf.vu.lt", started_at="2026-01-01T00:00:01+00:00")

    response = client.get(STATUS, headers=headers)

    assert [r["id"] for r in _runs(response)] == [new, old]




# ===========================================================
#  /status — one row's wire shape at its edges
# ===========================================================

def test_a_running_row_reports_no_finish_and_no_error(client, admin, seed_run):
    _user, headers = admin
    seed_run(source="knf.vu.lt", status="running", minutes=-1)

    row = _runs(client.get(STATUS, headers=headers))[0]

    assert row["finishedAt"] is None
    assert row["error"] is None
    assert row["status"] == "running"


def test_a_completed_row_carries_both_stamps(client, admin, seed_run):
    _user, headers = admin
    seed_run(source="knf.vu.lt", status="completed", minutes=-10,
             finished_at=_iso(minutes=-9), found=7, new=3)

    row = _runs(client.get(STATUS, headers=headers))[0]

    assert row["finishedAt"] is not None
    assert (row["articlesFound"], row["articlesNew"]) == (7, 3)


def test_a_completed_row_may_still_carry_an_error(client, admin, seed_run):
    # An info run that finished with one broken section is
    # 'completed' WITH an error naming it
    _user, headers = admin
    seed_run(source="knf.vu.lt/info", status="completed", minutes=-1, error="contacts section missing")

    row = _runs(client.get(STATUS, headers=headers))[0]

    assert row["status"] == "completed"
    assert row["error"] == "contacts section missing"


@pytest.mark.parametrize("found,new", [(0, 0), (1, 0), (0, 1), (-1, -5), (9223372036854775807, 0)])
def test_the_counters_pass_through_whatever_their_value(client, admin, seed_run, found, new):
    _user, headers = admin
    seed_run(source="knf.vu.lt", minutes=-1, found=found, new=new)

    row = _runs(client.get(STATUS, headers=headers))[0]

    assert (row["articlesFound"], row["articlesNew"]) == (found, new)
    assert (row["itemsFound"], row["itemsNew"]) == (found, new)


def test_the_source_neutral_names_are_the_same_numbers_not_a_copy_of_the_row(client, admin, seed_run):
    _user, headers = admin
    seed_run(source="tvarkarasciai.vu.lt", minutes=-1, found=88, new=12)

    row = _runs(client.get(STATUS, headers=headers))[0]

    assert row["itemsFound"] == row["articlesFound"] == 88
    assert row["itemsNew"] == row["articlesNew"] == 12
    assert set(row) == RUN_KEYS


def test_markup_in_a_source_name_is_escaped_on_the_way_out(client, admin, seed_run):
    _user, headers = admin
    seed_run(source="<b>knf</b>", minutes=-1)

    response = client.get(STATUS, headers=headers)

    assert "<b>knf</b>" not in response.get_data(as_text=True)
    assert "&lt;b&gt;knf&lt;/b&gt;" in response.get_data(as_text=True)


def test_markup_in_a_summary_entry_is_escaped_too(client, admin, seed_run):
    _user, headers = admin
    seed_run(source="knf.vu.lt", status="failed", minutes=-1,
             error="<script>alert(1)</script>")

    body = client.get(STATUS, headers=headers).get_data(as_text=True)

    assert "<script>" not in body
    assert "&lt;script&gt;" in body




# ===========================================================
#  /status — the per-source summary block
# ===========================================================

def test_a_fresh_database_answers_two_empty_lists(client, admin):
    _user, headers = admin

    response = client.get(STATUS, headers=headers)

    assert response.get_json() == {"runs": [], "sources": []}


def test_the_summary_ignores_the_status_filter(client, admin, seed_run):
    # ?status only narrows the run LIST; the summary is what
    # tells an admin a source stopped succeeding, so it keeps
    # naming every source whatever the filter
    _user, headers = admin
    seed_run(source="knf.vu.lt", status="completed", minutes=-5)
    seed_run(source="vu.lt", status="failed", minutes=-4)

    response = client.get(f"{STATUS}?status=failed", headers=headers)

    assert [r["source"] for r in _runs(response)] == ["vu.lt"]
    assert [s["source"] for s in _sources(response)] == ["knf.vu.lt", "vu.lt"]


def test_the_summary_honours_the_source_filter(client, admin, seed_run):
    _user, headers = admin
    seed_run(source="knf.vu.lt", minutes=-5)
    seed_run(source="vu.lt", minutes=-4)

    response = client.get(f"{STATUS}?source=vu.lt", headers=headers)

    assert [s["source"] for s in _sources(response)] == ["vu.lt"]


def test_a_source_with_only_running_rows_reports_neither_success_nor_failure(client, admin, seed_run):
    _user, headers = admin
    seed_run(source="knf.vu.lt", status="running", minutes=-1)

    entry = _sources(client.get(STATUS, headers=headers))[0]

    assert entry["latest"]["status"] == "running"
    assert entry["lastSuccess"] is None
    assert entry["lastFailure"] is None


def test_the_latest_entry_is_the_newest_row_of_any_status(client, admin, seed_run):
    _user, headers = admin
    seed_run(source="knf.vu.lt", status="completed", minutes=-30)
    newest = seed_run(source="knf.vu.lt", status="failed", minutes=-1)

    entry = _sources(client.get(STATUS, headers=headers))[0]

    assert entry["latest"]["id"] == newest
    assert entry["lastFailure"]["id"] == newest


def test_each_summary_slot_picks_the_newest_row_of_its_own_status(client, admin, seed_run):
    _user, headers = admin
    seed_run(source="knf.vu.lt", status="completed", minutes=-90)
    recent_success = seed_run(source="knf.vu.lt", status="completed", minutes=-60)
    seed_run(source="knf.vu.lt", status="failed", minutes=-40)
    recent_failure = seed_run(source="knf.vu.lt", status="failed", minutes=-10)

    entry = _sources(client.get(STATUS, headers=headers))[0]

    assert entry["lastSuccess"]["id"] == recent_success
    assert entry["lastFailure"]["id"] == recent_failure


def test_a_summary_entry_carries_exactly_four_keys(client, admin, seed_run):
    _user, headers = admin
    seed_run(source="knf.vu.lt", minutes=-1)

    entry = _sources(client.get(STATUS, headers=headers))[0]

    assert set(entry) == {"source", "latest", "lastSuccess", "lastFailure"}
    assert set(entry["latest"]) == RUN_KEYS


def test_one_run_renders_identically_in_the_list_and_in_the_summary(client, admin, seed_run):
    _user, headers = admin
    seed_run(source="knf.vu.lt", status="completed", minutes=-1, found=4, new=1,
             finished_at=_iso(seconds=-30))

    response = client.get(STATUS, headers=headers)

    assert _runs(response)[0] == _sources(response)[0]["latest"]


def test_a_source_beyond_the_twenty_row_window_still_gets_a_summary(client, admin, seed_run):
    # Twenty mixed rows are four news runs' worth — the
    # summary is exactly what makes an invisible source
    # visible again
    _user, headers = admin
    forgotten = seed_run(source="knf.vu.lt/info", status="failed", days=-40)
    for index in range(25):
        seed_run(source="knf.vu.lt", minutes=-index - 1)

    response = client.get(STATUS, headers=headers)

    assert forgotten not in [r["id"] for r in _runs(response)]
    info = [s for s in _sources(response) if s["source"] == "knf.vu.lt/info"][0]
    assert info["lastFailure"]["id"] == forgotten


def test_the_sources_are_listed_in_the_databases_own_byte_order(client, admin, seed_run):
    _user, headers = admin
    for source in ("vu.lt", "knf.vu.lt", "Zeta", "tvarkarasciai.vu.lt"):
        seed_run(source=source, minutes=-1)

    response = client.get(STATUS, headers=headers)

    assert [s["source"] for s in _sources(response)] == \
        ["Zeta", "knf.vu.lt", "tvarkarasciai.vu.lt", "vu.lt"]


def test_a_source_is_summarised_once_however_many_runs_it_has(client, admin, seed_run):
    _user, headers = admin
    for index in range(6):
        seed_run(source="knf.vu.lt", minutes=-index - 1)

    assert len(_sources(client.get(STATUS, headers=headers))) == 1




# ===========================================================
#  /status — it reads, it never writes, and it fails the way
#  the rest of the app does
# ===========================================================

def test_reading_the_status_changes_nothing(client, admin, seed_run, sql):
    _user, headers = admin
    seed_run(source="knf.vu.lt", status="running", minutes=-1)

    before = sql("SELECT id, status, finished_at FROM scraper_runs", fetch=True)
    client.get(STATUS, headers=headers)
    after = sql("SELECT id, status, finished_at FROM scraper_runs", fetch=True)

    assert [tuple(r) for r in before] == [tuple(r) for r in after]


def test_two_identical_reads_answer_identically(client, admin, seed_run):
    _user, headers = admin
    seed_run(source="knf.vu.lt", minutes=-1)

    first = client.get(STATUS, headers=headers).get_json()
    second = client.get(STATUS, headers=headers).get_json()

    assert first == second


def test_a_locked_database_is_a_retryable_503(client, admin, monkeypatch):
    _user, headers = admin

    def _locked():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(scraper_routes, "get_db", _locked)

    response = client.get(STATUS, headers=headers)

    assert response.status_code == 503
    assert response.get_json()["code"] == "database_busy"


def test_any_other_sqlite_failure_is_a_plain_500(client, admin, monkeypatch):
    _user, headers = admin

    def _broken():
        raise sqlite3.OperationalError("no such table: scraper_runs")

    monkeypatch.setattr(scraper_routes, "get_db", _broken)

    response = client.get(STATUS, headers=headers)

    assert response.status_code == 500
    assert response.get_json() == {"error": "Internal server error"}




# ===========================================================
#  TRIGGERS — the status matrix over the two news results
# ===========================================================

OK_KNF = {"found": 5, "new": 2, "runId": "knf-run"}
OK_VU = {"found": 3, "new": 0, "runId": "vu-run"}
SKIPPED = {"found": 0, "new": 0, "skipped": True}
FAILED = {"found": 0, "new": 0, "error": "HTTPError: 503", "runId": "boom"}


@pytest.mark.parametrize("knf,vu,expected", [
    (OK_KNF, OK_VU, 200),
    (SKIPPED, OK_VU, 409),
    (OK_KNF, SKIPPED, 409),
    (SKIPPED, SKIPPED, 409),
    (FAILED, OK_VU, 502),
    (OK_KNF, FAILED, 502),
    (FAILED, FAILED, 502),
    (FAILED, SKIPPED, 502),
    (SKIPPED, FAILED, 502),
    ({"error": "boom", "skipped": True}, OK_VU, 502),
    (None, OK_VU, 502),
    (OK_KNF, None, 502),
    ([], OK_VU, 502),
    (OK_KNF, "", 502),
    (None, None, 502),
])
def test_the_trigger_status_is_the_worst_news_of_the_two_scrapes(client, admin, scrapers,
                                                                 knf, vu, expected):
    _user, headers = admin
    scrapers.results["knf"] = knf
    scrapers.results["vu"] = vu

    response = client.post(TRIGGER, headers=headers)

    assert response.status_code == expected


@pytest.mark.parametrize("falsy", [None, "", 0, False, [], {}])
def test_a_falsy_error_key_is_not_a_failure(client, admin, scrapers, falsy):
    _user, headers = admin
    scrapers.results["knf"] = {"found": 1, "new": 0, "error": falsy}

    response = client.post(TRIGGER, headers=headers)

    assert response.status_code == 200
    assert response.get_json()["knf"]["error"] == falsy


@pytest.mark.parametrize("falsy", [None, "", 0, False, []])
def test_a_falsy_skipped_key_is_not_a_conflict(client, admin, scrapers, falsy):
    _user, headers = admin
    scrapers.results["vu"] = {"found": 1, "new": 0, "skipped": falsy}

    response = client.post(TRIGGER, headers=headers)

    assert response.status_code == 200


@pytest.mark.parametrize("truthy", [True, 1, "yes", ["taken"], 0.5])
def test_any_truthy_skipped_value_is_a_conflict(client, admin, scrapers, truthy):
    _user, headers = admin
    scrapers.results["vu"] = {"found": 0, "new": 0, "skipped": truthy}

    assert client.post(TRIGGER, headers=headers).status_code == 409


@pytest.mark.parametrize("truthy", [True, 1, "traceback", ["boom"], 0.5])
def test_any_truthy_error_value_becomes_the_stable_slug(client, admin, scrapers, truthy):
    _user, headers = admin
    scrapers.results["knf"] = {"found": 0, "new": 0, "error": truthy}

    response = client.post(TRIGGER, headers=headers)

    assert response.status_code == 502
    assert response.get_json()["knf"]["error"] == scraper_routes.ERROR_SLUG


@pytest.mark.parametrize("not_a_dict", [None, [], "", "boom", 0, 3.5, True, {1, 2}])
def test_a_result_that_is_not_a_dict_is_reported_as_a_failed_source(client, admin, scrapers, not_a_dict):
    _user, headers = admin
    scrapers.results["knf"] = not_a_dict

    response = client.post(TRIGGER, headers=headers)

    assert response.status_code == 502
    assert response.get_json()["knf"] == {"error": "scrape_failed", "source": "knf.vu.lt"}


def test_an_empty_result_dict_is_a_success_with_an_empty_body(client, admin, scrapers):
    _user, headers = admin
    scrapers.results["knf"] = {}

    response = client.post(TRIGGER, headers=headers)

    assert response.status_code == 200
    assert response.get_json()["knf"] == {}


def test_the_body_always_carries_both_scrapers(client, admin, scrapers):
    _user, headers = admin
    scrapers.results["knf"] = None
    scrapers.results["vu"] = SKIPPED

    body = client.post(TRIGGER, headers=headers).get_json()

    assert set(body) == {"knf", "vu"}


def test_the_vu_scrape_still_runs_after_the_knf_scrape_failed(client, admin, scrapers):
    _user, headers = admin
    scrapers.results["knf"] = FAILED

    client.post(TRIGGER, headers=headers)

    assert scrapers.names() == ["knf", "vu"]


def test_the_knf_scrape_runs_before_the_vu_one(client, admin, scrapers):
    _user, headers = admin

    client.post(TRIGGER, headers=headers)

    assert scrapers.names() == ["knf", "vu"]


def test_a_conflict_body_keeps_the_skipped_flag_the_scraper_set(client, admin, scrapers):
    _user, headers = admin
    scrapers.results["vu"] = SKIPPED

    body = client.post(TRIGGER, headers=headers).get_json()

    assert body["vu"]["skipped"] is True


def test_the_exception_text_never_reaches_the_client(client, admin, scrapers, caplog):
    _user, headers = admin
    scrapers.results["knf"] = {"found": 0, "new": 0,
                               "error": "Traceback: /app/secret/path.py line 42 token=hunter2"}

    with caplog.at_level("WARNING"):
        response = client.post(TRIGGER, headers=headers)

    assert "hunter2" not in response.get_data(as_text=True)
    assert "secret/path.py" not in response.get_data(as_text=True)
    assert "hunter2" in caplog.text


def test_the_failure_log_names_the_source_that_broke(client, admin, scrapers, caplog):
    _user, headers = admin
    scrapers.results["vu"] = FAILED

    with caplog.at_level("WARNING"):
        client.post(TRIGGER, headers=headers)

    assert "vu.lt scrape failed" in caplog.text


def test_the_route_never_writes_into_the_scrapers_own_result(client, admin, scrapers):
    _user, headers = admin
    raw = {"found": 0, "new": 0, "error": "HTTPError: 503"}
    scrapers.results["knf"] = raw

    client.post(TRIGGER, headers=headers)

    assert raw["error"] == "HTTPError: 503"


def test_extra_keys_a_scraper_invents_ride_along_untouched(client, admin, scrapers):
    _user, headers = admin
    scrapers.results["knf"] = {"found": 1, "new": 1, "pagesWalked": 2, "note": "first page only"}

    body = client.post(TRIGGER, headers=headers).get_json()

    assert body["knf"]["pagesWalked"] == 2
    assert body["knf"]["note"] == "first page only"


def test_a_run_id_rides_along_so_the_full_error_can_be_looked_up(client, admin, scrapers):
    _user, headers = admin
    scrapers.results["knf"] = {"found": 0, "new": 0, "error": "boom", "runId": "run-77"}

    body = client.post(TRIGGER, headers=headers).get_json()

    assert body["knf"]["runId"] == "run-77"


def test_a_scraper_that_raises_is_a_500_and_not_a_502(app, client, admin, scrapers):
    # Every scraper catches its own failures; an exception
    # getting out is a bug in the scraper, and the route has
    # no opinion about it beyond the app's generic 500
    _user, headers = admin
    app.config["PROPAGATE_EXCEPTIONS"] = False
    scrapers.results["knf"] = RuntimeError("boom")

    response = client.post(TRIGGER, headers=headers)

    assert response.status_code == 500
    assert response.get_json() == {"error": "Internal server error"}
    assert scrapers.names() == ["knf"]




# ===========================================================
#  TRIGGERS — /run is /trigger, and the two single-scraper
#  routes carry their own source name
# ===========================================================

@pytest.mark.parametrize("path", [TRIGGER, RUN])
def test_trigger_and_run_answer_identically(client, admin, scrapers, path):
    _user, headers = admin

    response = client.post(path, headers=headers)

    assert response.status_code == 200
    assert set(response.get_json()) == {"knf", "vu"}


def test_trigger_and_run_are_one_view_function(app):
    endpoints = {str(rule): rule.endpoint for rule in app.url_map.iter_rules()
                 if str(rule) in (TRIGGER, RUN)}

    assert endpoints == {TRIGGER: "scraper.trigger_scrape", RUN: "scraper.trigger_scrape"}


def test_the_news_trigger_keeps_the_timer_runs_page_counts_and_never_pushes(client, admin, scrapers):
    _user, headers = admin

    client.post(RUN, headers=headers)

    assert scrapers.kwargs_of("knf") == [{"pages": 2, "notify": False}]
    assert scrapers.kwargs_of("vu") == [{"pages": 1, "notify": False}]
    assert scrapers.args_of("knf") == [()]


def test_the_timetable_trigger_passes_only_notify_false(client, admin, scrapers):
    _user, headers = admin

    response = client.post(SCHEDULE, headers=headers)

    assert response.status_code == 200
    assert scrapers.args_of("schedule") == [()]
    assert scrapers.kwargs_of("schedule") == [{"notify": False}]


def test_the_info_trigger_takes_no_arguments_at_all(client, admin, scrapers):
    _user, headers = admin

    response = client.post(INFO, headers=headers)

    assert response.status_code == 200
    assert scrapers.args_of("info") == [()]
    assert scrapers.kwargs_of("info") == [{}]


@pytest.mark.parametrize("path,name", [(SCHEDULE, "schedule"), (INFO, "info")])
def test_a_single_scraper_body_is_that_scrapers_dict_and_nothing_else(client, admin, scrapers, path, name):
    _user, headers = admin

    body = client.post(path, headers=headers).get_json()

    assert body == scrapers.results[name]


@pytest.mark.parametrize("path,name,expected", [
    (SCHEDULE, "schedule", 409),
    (INFO, "info", 409),
])
def test_a_single_scraper_that_stepped_aside_is_a_conflict(client, admin, scrapers, path, name, expected):
    _user, headers = admin
    scrapers.results[name] = {"skipped": True}

    response = client.post(path, headers=headers)

    assert response.status_code == expected
    assert response.get_json() == {"skipped": True}


@pytest.mark.parametrize("path,name", [(SCHEDULE, "schedule"), (INFO, "info")])
def test_a_single_scraper_failure_is_a_502_with_the_slug(client, admin, scrapers, path, name):
    _user, headers = admin
    scrapers.results[name] = {"error": "ConnectionError: source down", "runId": "r1"}

    response = client.post(path, headers=headers)

    assert response.status_code == 502
    assert response.get_json() == {"error": "scrape_failed", "runId": "r1"}


@pytest.mark.parametrize("path,name,source", [
    (SCHEDULE, "schedule", "tvarkarasciai.vu.lt"),
    (INFO, "info", "knf.vu.lt/info"),
])
def test_a_single_scraper_that_answers_nonsense_names_its_own_source(client, admin, scrapers,
                                                                     path, name, source):
    _user, headers = admin
    scrapers.results[name] = None

    response = client.post(path, headers=headers)

    assert response.status_code == 502
    assert response.get_json() == {"error": "scrape_failed", "source": source}


@pytest.mark.parametrize("path,name,source", [
    (SCHEDULE, "schedule", "tvarkarasciai.vu.lt"),
    (INFO, "info", "knf.vu.lt/info"),
])
def test_the_single_scraper_failure_log_names_its_source(client, admin, scrapers, caplog,
                                                         path, name, source):
    _user, headers = admin
    scrapers.results[name] = {"error": "boom"}

    with caplog.at_level("WARNING"):
        client.post(path, headers=headers)

    assert f"{source} scrape failed" in caplog.text




# ===========================================================
#  TRIGGERS — running one twice, and running two of them
# ===========================================================

def test_a_second_identical_trigger_runs_the_scrapers_again(client, admin, scrapers):
    _user, headers = admin

    first = client.post(TRIGGER, headers=headers)
    second = client.post(TRIGGER, headers=headers)

    assert (first.status_code, second.status_code) == (200, 200)
    assert scrapers.names() == ["knf", "vu", "knf", "vu"]


def test_a_trigger_recovers_the_moment_the_lock_is_free_again(client, admin, scrapers):
    _user, headers = admin
    scrapers.results["knf"] = SKIPPED

    conflicted = client.post(TRIGGER, headers=headers)
    scrapers.results["knf"] = OK_KNF
    recovered = client.post(TRIGGER, headers=headers)

    assert conflicted.status_code == 409
    assert recovered.status_code == 200


def test_the_four_triggers_do_not_share_a_lock(client, admin, scrapers):
    _user, headers = admin
    scrapers.results["schedule"] = {"skipped": True}

    conflicted = client.post(SCHEDULE, headers=headers)
    news = client.post(TRIGGER, headers=headers)
    info = client.post(INFO, headers=headers)

    assert (conflicted.status_code, news.status_code, info.status_code) == (409, 200, 200)


def test_a_failed_trigger_leaves_the_status_route_working(client, admin, scrapers, seed_run):
    _user, headers = admin
    seed_run(source="knf.vu.lt", minutes=-1)
    scrapers.results["knf"] = None

    assert client.post(TRIGGER, headers=headers).status_code == 502
    assert client.get(STATUS, headers=headers).status_code == 200




# ===========================================================
#  _public_result / _trigger_status — the two helpers at
#  boundaries no route can reach
# ===========================================================

def test_no_results_at_all_is_a_success():
    assert scraper_routes._trigger_status() == 200


@pytest.mark.parametrize("results,expected", [
    (({},), 200),
    (({}, {}, {}), 200),
    (({"skipped": True}, {}, {}), 409),
    (({}, {}, {"error": "x"}), 502),
    (({"skipped": True}, {"error": "x"}), 502),
    ((None,), 502),
    (({}, None), 502),
])
def test_the_worst_result_decides_the_status(results, expected):
    assert scraper_routes._trigger_status(*results) == expected


def test_public_result_hands_back_a_copy_not_the_original():
    original = {"found": 1, "new": 0}

    public = scraper_routes._public_result(original, "knf.vu.lt")

    assert public == original
    assert public is not original


def test_public_result_leaves_a_healthy_dict_alone():
    result = {"pages_scraped": 3, "contacts_found": 21, "programs_found": 9}

    assert scraper_routes._public_result(result, "knf.vu.lt/info") == result


@pytest.mark.parametrize("value", [None, [], "", 0, "boom", 3.5, True, ("a",)])
def test_public_result_turns_anything_that_is_not_a_dict_into_a_failed_source(value):
    assert scraper_routes._public_result(value, "vu.lt") == {"error": "scrape_failed",
                                                             "source": "vu.lt"}


def test_public_result_keeps_the_source_out_of_a_dict_it_was_given():
    # The "source" key only appears on the non-dict path — a
    # real result never gains a key it did not have
    public = scraper_routes._public_result({"found": 0, "new": 0, "error": "x"}, "vu.lt")

    assert "source" not in public
    assert public["error"] == scraper_routes.ERROR_SLUG


def test_the_error_slug_is_the_stable_string_the_banner_promises():
    assert scraper_routes.ERROR_SLUG == "scrape_failed"


def test_the_status_route_accepts_exactly_three_run_statuses():
    assert scraper_routes._RUN_STATUSES == ("running", "completed", "failed")




# ===========================================================
#  SCHEDULER — the env gate and the per-process guard
# ===========================================================

@pytest.mark.parametrize("raw,expected", [
    ("0", False), ("false", False), ("no", False), ("off", False),
    ("FALSE", False), ("No", False), ("OFF", False), ("Off", False),
    (" 0 ", False), ("\t0\n", False), ("  off  ", False),
    ("1", True), ("true", True), ("yes", True), ("on", True),
    ("00", True), ("0.0", True), ("-0", True), ("none", True), ("null", True),
    ("nope", True), ("0 0", True), ("o f f", True), ("falsey", True), ("N", True),
])
def test_the_env_flag_reads_only_the_four_documented_spellings_of_off(monkeypatch, raw, expected):
    monkeypatch.setenv("SCRAPER_TEST_FLAG", raw)

    assert scheduler._env_flag("SCRAPER_TEST_FLAG", True) is expected
    assert scheduler._env_flag("SCRAPER_TEST_FLAG", False) is expected


@pytest.mark.parametrize("raw", ["", " ", "   ", "\t", "\n"])
@pytest.mark.parametrize("default", [True, False])
def test_a_blank_value_falls_back_to_the_default(monkeypatch, raw, default):
    monkeypatch.setenv("SCRAPER_TEST_FLAG", raw)

    assert scheduler._env_flag("SCRAPER_TEST_FLAG", default) is default


@pytest.mark.parametrize("default", [True, False])
def test_an_unset_variable_falls_back_to_the_default(monkeypatch, default):
    monkeypatch.delenv("SCRAPER_TEST_FLAG", raising=False)

    assert scheduler._env_flag("SCRAPER_TEST_FLAG", default) is default


def test_a_disabled_gate_builds_nothing_and_answers_false(app, monkeypatch, armed_timers):
    monkeypatch.setenv("SCRAPER_ENABLED", "0")

    assert scheduler.start_scraper_scheduler(app) is False
    assert scheduler._scheduler is None
    assert armed_timers == []


def test_a_disabled_gate_does_not_even_reconcile_the_interrupted_runs(app, monkeypatch, seed_run, sql):
    monkeypatch.setenv("SCRAPER_ENABLED", "0")
    run_id = seed_run(source="knf.vu.lt", status="running", hours=-9)

    scheduler.start_scraper_scheduler(app)

    row = sql("SELECT status FROM scraper_runs WHERE id = ?", (run_id,), fetch=True)[0]
    assert row["status"] == "running"


def test_the_gate_is_read_at_every_start_not_cached(app, monkeypatch, armed_timers):
    monkeypatch.setenv("SCRAPER_STARTUP_RUNS", "0")
    monkeypatch.setenv("SCRAPER_ENABLED", "off")
    assert scheduler.start_scraper_scheduler(app) is False

    monkeypatch.setenv("SCRAPER_ENABLED", "on")
    assert scheduler.start_scraper_scheduler(app) is True


def test_turning_the_gate_off_does_not_stop_a_scheduler_that_is_already_up(app, monkeypatch,
                                                                          running_scheduler):
    # The env check sits ABOVE the re-entry guard, so a second
    # call with the gate off answers False while the running
    # scheduler carries on — only stop_scraper_scheduler ends it
    live = scheduler._scheduler
    monkeypatch.setenv("SCRAPER_ENABLED", "0")

    assert scheduler.start_scraper_scheduler(app) is False
    assert scheduler._scheduler is live
    assert scheduler._scheduler.running


def test_a_second_start_is_a_no_op_that_still_answers_true(app, running_scheduler):
    live = scheduler._scheduler

    assert scheduler.start_scraper_scheduler(app) is True
    assert scheduler._scheduler is live


def test_a_second_start_does_not_double_the_jobs(app, running_scheduler):
    scheduler.start_scraper_scheduler(app)
    scheduler.start_scraper_scheduler(app)

    assert len(scheduler._scheduler.get_jobs()) == 7


def test_a_second_start_arms_no_further_one_shots(app, monkeypatch, armed_timers):
    monkeypatch.setenv("SCRAPER_ENABLED", "1")
    monkeypatch.setenv("SCRAPER_STARTUP_RUNS", "1")

    scheduler.start_scraper_scheduler(app)
    armed_after_first = len(armed_timers)
    scheduler.start_scraper_scheduler(app)

    assert armed_after_first == 3
    assert len(armed_timers) == 3


def test_a_start_after_a_stop_builds_a_brand_new_scheduler(app, monkeypatch):
    monkeypatch.setenv("SCRAPER_ENABLED", "1")
    monkeypatch.setenv("SCRAPER_STARTUP_RUNS", "0")

    scheduler.start_scraper_scheduler(app)
    first = scheduler._scheduler
    scheduler.stop_scraper_scheduler()
    scheduler.start_scraper_scheduler(app)

    assert scheduler._scheduler is not first
    assert scheduler._scheduler.running




# ===========================================================
#  SCHEDULER — stopping
# ===========================================================

def test_stopping_twice_is_safe(app, monkeypatch):
    monkeypatch.setenv("SCRAPER_ENABLED", "1")
    monkeypatch.setenv("SCRAPER_STARTUP_RUNS", "0")
    scheduler.start_scraper_scheduler(app)

    scheduler.stop_scraper_scheduler()
    scheduler.stop_scraper_scheduler()

    assert scheduler._scheduler is None


def test_stopping_cancels_the_one_shots_even_when_no_scheduler_was_built():
    # The timer loop runs BEFORE the `_scheduler is None`
    # guard, which is what makes a half-built start safe to
    # unwind
    class _Timer:
        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

    timer = _Timer()
    scheduler._startup_timers.append(timer)

    scheduler.stop_scraper_scheduler()

    assert timer.cancelled is True
    assert scheduler._startup_timers == []


def test_stopping_empties_the_timer_list_it_cancelled(app, monkeypatch, armed_timers):
    monkeypatch.setenv("SCRAPER_ENABLED", "1")
    monkeypatch.setenv("SCRAPER_STARTUP_RUNS", "1")
    scheduler.start_scraper_scheduler(app)

    scheduler.stop_scraper_scheduler()

    assert [t.cancelled for t in armed_timers] == [True, True, True]
    assert scheduler._startup_timers == []


def test_a_shutdown_that_raises_still_clears_the_guard(app, monkeypatch, caplog):
    monkeypatch.setenv("SCRAPER_ENABLED", "1")
    monkeypatch.setenv("SCRAPER_STARTUP_RUNS", "0")
    scheduler.start_scraper_scheduler(app)
    live = scheduler._scheduler

    def _explode(wait=True):
        raise RuntimeError("shutdown refused")

    monkeypatch.setattr(live, "shutdown", _explode)

    with caplog.at_level("WARNING"):
        scheduler.stop_scraper_scheduler()

    assert scheduler._scheduler is None
    assert "shutdown did not take" in caplog.text

    # The guard is clear but that thread is not: put the real
    # shutdown back and end it, so nothing outlives this test
    monkeypatch.undo()
    live.shutdown(wait=False)


def test_the_shutdown_does_not_wait_for_a_scrape_in_flight(app, monkeypatch):
    monkeypatch.setenv("SCRAPER_ENABLED", "1")
    monkeypatch.setenv("SCRAPER_STARTUP_RUNS", "0")
    scheduler.start_scraper_scheduler(app)
    live = scheduler._scheduler
    seen = {}

    def _record(wait=True):
        seen["wait"] = wait

    monkeypatch.setattr(live, "shutdown", _record)

    scheduler.stop_scraper_scheduler()

    assert seen == {"wait": False}

    # The recorder swallowed the real shutdown — end the
    # thread here rather than leaving it to the next test
    monkeypatch.undo()
    live.shutdown(wait=False)




# ===========================================================
#  SCHEDULER — the startup one-shots
# ===========================================================

def test_the_one_shots_are_armed_started_and_daemonised(app, monkeypatch, armed_timers):
    monkeypatch.setenv("SCRAPER_ENABLED", "1")
    monkeypatch.setenv("SCRAPER_STARTUP_RUNS", "1")

    scheduler.start_scraper_scheduler(app)

    assert len(armed_timers) == 3
    assert all(t.started for t in armed_timers)
    assert all(t.daemon for t in armed_timers)


def test_with_no_jitter_the_delays_are_exactly_the_documented_ones(app, monkeypatch, armed_timers,
                                                                   fixed_jitter):
    monkeypatch.setenv("SCRAPER_ENABLED", "1")
    monkeypatch.setenv("SCRAPER_STARTUP_RUNS", "1")
    fixed_jitter.value = 0.0

    scheduler.start_scraper_scheduler(app)

    assert [t.interval for t in armed_timers] == list(STARTUP_DELAYS)


def test_the_jitter_can_push_each_one_shot_no_further_than_its_ceiling(app, monkeypatch, armed_timers,
                                                                       fixed_jitter):
    monkeypatch.setenv("SCRAPER_ENABLED", "1")
    monkeypatch.setenv("SCRAPER_STARTUP_RUNS", "1")
    fixed_jitter.value = float(scheduler.STARTUP_JITTER_SECONDS)

    scheduler.start_scraper_scheduler(app)

    assert [t.interval for t in armed_timers] == [2.0 + 20, 30.0 + 20, 60.0 + 20]


def test_the_jitter_is_drawn_from_zero_to_the_documented_ceiling(app, monkeypatch, armed_timers,
                                                                 fixed_jitter):
    monkeypatch.setenv("SCRAPER_ENABLED", "1")
    monkeypatch.setenv("SCRAPER_STARTUP_RUNS", "1")

    scheduler.start_scraper_scheduler(app)

    assert fixed_jitter.calls == [(0, scheduler.STARTUP_JITTER_SECONDS)] * 3


def test_the_one_shots_stay_staggered_whatever_the_jitter(app, monkeypatch, armed_timers):
    monkeypatch.setenv("SCRAPER_ENABLED", "1")
    monkeypatch.setenv("SCRAPER_STARTUP_RUNS", "1")

    scheduler.start_scraper_scheduler(app)

    delays = [t.interval for t in armed_timers]
    assert delays == sorted(delays)
    assert 2.0 <= delays[0] <= 22.0
    assert 30.0 <= delays[1] <= 50.0
    assert 60.0 <= delays[2] <= 80.0


def test_each_one_shot_fires_the_very_job_it_stands_in_for(app, monkeypatch, armed_timers, job):
    # The Timer gets the SAME closure object the interval job
    # was registered with — one body, two clocks
    monkeypatch.setenv("SCRAPER_STARTUP_RUNS", "1")
    scheduler.stop_scraper_scheduler()
    scheduler.start_scraper_scheduler(app)

    functions = [t.function for t in armed_timers]
    registered = [scheduler._scheduler.get_job(job_id).func
                  for job_id in ("news_scraper", "schedule_scraper", "info_scraper")]

    assert functions == registered


@pytest.mark.parametrize("flag", ["0", "off", "No", "FALSE"])
def test_startup_runs_off_leaves_the_interval_jobs_as_the_only_scrapes(app, monkeypatch, armed_timers,
                                                                       caplog, flag):
    monkeypatch.setenv("SCRAPER_ENABLED", "1")
    monkeypatch.setenv("SCRAPER_STARTUP_RUNS", flag)

    with caplog.at_level("INFO"):
        assert scheduler.start_scraper_scheduler(app) is True

    assert armed_timers == []
    assert "SCRAPER_STARTUP_RUNS is off" in caplog.text
    assert len(scheduler._scheduler.get_jobs()) == 7


@pytest.mark.parametrize("source,minutes_ago,still_armed", [
    ("knf.vu.lt", 19, 2),
    ("knf.vu.lt", 21, 3),
    ("tvarkarasciai.vu.lt", 6 * 60 - 1, 2),
    ("tvarkarasciai.vu.lt", 6 * 60 + 1, 3),
    ("knf.vu.lt/info", 24 * 60 - 1, 2),
    ("knf.vu.lt/info", 24 * 60 + 1, 3),
])
def test_a_startup_scrape_is_skipped_only_inside_its_own_interval(app, monkeypatch, armed_timers,
                                                                  seed_run, source, minutes_ago,
                                                                  still_armed):
    monkeypatch.setenv("SCRAPER_ENABLED", "1")
    monkeypatch.setenv("SCRAPER_STARTUP_RUNS", "1")
    seed_run(source=source, status="completed", minutes=-minutes_ago)

    scheduler.start_scraper_scheduler(app)

    assert len(armed_timers) == still_armed


def test_a_crash_loop_cannot_re_scrape_every_source_on_every_boot(app, monkeypatch, armed_timers,
                                                                  seed_run, caplog):
    monkeypatch.setenv("SCRAPER_ENABLED", "1")
    monkeypatch.setenv("SCRAPER_STARTUP_RUNS", "1")
    for source in ("knf.vu.lt", "tvarkarasciai.vu.lt", "knf.vu.lt/info"):
        seed_run(source=source, status="completed", minutes=-1)

    with caplog.at_level("INFO"):
        scheduler.start_scraper_scheduler(app)

    assert armed_timers == []
    assert caplog.text.count("Skipping the startup") == 3


@pytest.mark.parametrize("status", ["running", "failed"])
def test_an_unfinished_run_is_no_cover_for_the_startup_scrape(app, monkeypatch, armed_timers,
                                                              seed_run, status):
    monkeypatch.setenv("SCRAPER_ENABLED", "1")
    monkeypatch.setenv("SCRAPER_STARTUP_RUNS", "1")
    seed_run(source="knf.vu.lt", status=status, minutes=-1)

    scheduler.start_scraper_scheduler(app)

    assert len(armed_timers) == 3




# ===========================================================
#  SCHEDULER — the seven interval jobs
# ===========================================================

def test_exactly_these_seven_jobs_are_registered(running_scheduler):
    assert {j.id for j in running_scheduler._scheduler.get_jobs()} == {
        "news_scraper", "schedule_scraper", "info_scraper", "push_receipts",
        "session_sweep", "push_token_prune", "run_reconcile",
    }


@pytest.mark.parametrize("job_id", ["news_scraper", "schedule_scraper", "info_scraper",
                                    "push_receipts", "session_sweep", "push_token_prune",
                                    "run_reconcile"])
def test_no_job_may_run_beside_itself_and_all_get_the_same_grace(job, job_id):
    registered = job(job_id)

    assert registered.max_instances == 1
    assert registered.misfire_grace_time == scheduler.MISFIRE_GRACE_SECONDS


def test_the_misfire_grace_is_five_minutes_not_apschedulers_one_second():
    assert scheduler.MISFIRE_GRACE_SECONDS == 300


def test_the_startup_jitter_ceiling_is_twenty_seconds():
    assert scheduler.STARTUP_JITTER_SECONDS == 20


def test_the_stale_run_budget_outlasts_every_scraper():
    assert scheduler.STALE_RUN_SECONDS == 6 * 3600


def test_the_first_tick_of_every_job_is_a_full_interval_away(running_scheduler):
    now = datetime.now(timezone.utc)

    for registered in running_scheduler._scheduler.get_jobs():
        assert registered.next_run_time > now + timedelta(minutes=14)


def test_a_missed_tick_is_logged_with_the_job_that_lost_it(caplog):
    class _Event:
        job_id = "news_scraper"
        scheduled_run_time = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    with caplog.at_level("WARNING"):
        scheduler._log_missed_job(_Event())

    assert "news_scraper" in caplog.text
    assert "300 s late" in caplog.text




# ===========================================================
#  SCHEDULER — the seven job bodies
# ===========================================================

# Every job body wraps itself in app.app_context(); the only
# way to see that from outside is to record it from inside
# whatever the body calls
def _context_recorder(seen, name, result=None):
    def _fake(*args, **kwargs):
        seen[name] = has_app_context()
        return result
    return _fake


@pytest.mark.parametrize("job_id,module_path,attribute", [
    ("news_scraper", "app.scraper.knf_scraper", "scrape_knf_news"),
    ("schedule_scraper", "app.scraper.schedule_scraper", "scrape_knf_schedule"),
    ("info_scraper", "app.scraper.info_scraper", "scrape_faculty_info"),
    ("push_receipts", "app.notifications.push", "poll_push_receipts"),
])
def test_every_scraping_job_body_runs_inside_an_app_context(job, monkeypatch, job_id,
                                                            module_path, attribute):
    import importlib

    seen = {}
    module = importlib.import_module(module_path)
    monkeypatch.setattr(module, attribute, _context_recorder(seen, job_id, result=0))
    monkeypatch.setattr("app.scraper.vu_scraper.scrape_vu_news", lambda **kwargs: {"found": 0})

    job(job_id).func()

    assert seen == {job_id: True}


@pytest.mark.parametrize("job_id,attribute", [
    ("session_sweep", "sweep_expired_sessions"),
    ("push_token_prune", "_prune_orphaned_push_tokens"),
    ("run_reconcile", "_reconcile_interrupted_runs"),
])
def test_every_housekeeping_job_body_runs_inside_an_app_context(job, monkeypatch, job_id, attribute):
    seen = {}
    monkeypatch.setattr(scheduler, attribute, _context_recorder(seen, job_id))

    job(job_id).func()

    assert seen == {job_id: True}


def test_the_news_job_asks_for_two_knf_pages_and_one_vu_page(job, monkeypatch):
    calls = []
    monkeypatch.setattr("app.scraper.knf_scraper.scrape_knf_news",
                        lambda **kwargs: calls.append(("knf", kwargs)) or {"found": 0})
    monkeypatch.setattr("app.scraper.vu_scraper.scrape_vu_news",
                        lambda **kwargs: calls.append(("vu", kwargs)) or {"found": 0})

    job("news_scraper").func()

    assert calls == [("knf", {"pages": 2}), ("vu", {"pages": 1})]


def test_the_scheduled_news_run_still_notifies_unlike_the_manual_one(job, monkeypatch):
    calls = []
    monkeypatch.setattr("app.scraper.knf_scraper.scrape_knf_news",
                        lambda **kwargs: calls.append(kwargs) or {"found": 0})
    monkeypatch.setattr("app.scraper.vu_scraper.scrape_vu_news",
                        lambda **kwargs: calls.append(kwargs) or {"found": 0})

    job("news_scraper").func()

    assert all("notify" not in kwargs for kwargs in calls)


def test_a_knf_failure_no_longer_costs_the_vu_scrape_in_the_same_tick(job, monkeypatch, caplog):
    # The two sources have a guard each: an exception out of
    # knf.vu.lt is logged and the tick carries on to vu.lt,
    # which used to wait a full 20 minutes for the next one
    seen = []

    def _boom(**kwargs):
        raise RuntimeError("knf exploded")

    monkeypatch.setattr("app.scraper.knf_scraper.scrape_knf_news", _boom)
    monkeypatch.setattr("app.scraper.vu_scraper.scrape_vu_news",
                        lambda **kwargs: seen.append(kwargs) or {"found": 0})

    with caplog.at_level("ERROR"):
        job("news_scraper").func()

    assert seen == [{"pages": 1}]
    assert "Scheduled scrape failed for knf.vu.lt" in caplog.text


def test_the_daily_reconcile_passes_the_stale_budget_not_zero(job, monkeypatch):
    seen = []
    monkeypatch.setattr(scheduler, "_reconcile_interrupted_runs",
                        lambda *args: seen.append(args))

    job("run_reconcile").func()

    assert seen == [(scheduler.STALE_RUN_SECONDS,)]


def test_the_sweep_job_commits_and_closes_its_own_connection(app, job, monkeypatch):
    holder = _recording_db(app, monkeypatch)

    job("session_sweep").func()

    assert holder["conn"].commits == 1
    assert holder["conn"].closed == 1


def test_the_sweep_job_closes_its_connection_even_when_the_sweep_raises(app, job, monkeypatch, caplog):
    holder = _recording_db(app, monkeypatch)

    def _boom(db):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(scheduler, "sweep_expired_sessions", _boom)

    with caplog.at_level("ERROR"):
        job("session_sweep").func()

    assert holder["conn"].closed == 1
    assert "Expired-session sweep failed" in caplog.text


@pytest.mark.parametrize("job_id,message", [
    ("news_scraper", "Scheduled scrape failed"),
    ("schedule_scraper", "Scheduled schedule scrape failed"),
    ("info_scraper", "Scheduled faculty info scrape failed"),
    ("push_receipts", "Push receipt poll failed"),
    ("session_sweep", "Expired-session sweep failed"),
    ("push_token_prune", "Push-token prune failed"),
    ("run_reconcile", "Scraper-run reconciliation failed"),
])
def test_no_job_body_ever_lets_a_failure_escape(app, job, monkeypatch, caplog, job_id, message):
    def _boom(*args, **kwargs):
        raise RuntimeError("job exploded")

    monkeypatch.setattr("app.scraper.knf_scraper.scrape_knf_news", _boom)
    # vu.lt has a guard of its own since the news job stopped
    # letting knf.vu.lt end the tick — unpatched, it would be
    # the one call in this file to reach for the network
    monkeypatch.setattr("app.scraper.vu_scraper.scrape_vu_news", _boom)
    monkeypatch.setattr("app.scraper.schedule_scraper.scrape_knf_schedule", _boom)
    monkeypatch.setattr("app.scraper.info_scraper.scrape_faculty_info", _boom)
    monkeypatch.setattr("app.notifications.push.poll_push_receipts", _boom)
    monkeypatch.setattr(scheduler, "sweep_expired_sessions", _boom)
    monkeypatch.setattr(scheduler, "_prune_orphaned_push_tokens", _boom)
    monkeypatch.setattr(scheduler, "_reconcile_interrupted_runs", _boom)

    with caplog.at_level("ERROR"):
        job(job_id).func()

    assert message in caplog.text
    assert scheduler._scheduler.running


def test_a_failing_job_leaves_the_other_six_registered(app, job, monkeypatch, caplog):
    monkeypatch.setattr(scheduler, "_prune_orphaned_push_tokens",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    with caplog.at_level("ERROR"):
        job("push_token_prune").func()

    assert len(scheduler._scheduler.get_jobs()) == 7


@pytest.mark.parametrize("checked,logged", [(0, False), (1, True), (5, True), (None, False)])
def test_the_receipt_job_only_speaks_when_it_checked_something(job, monkeypatch, caplog,
                                                               checked, logged):
    monkeypatch.setattr("app.notifications.push.poll_push_receipts", lambda: checked)

    with caplog.at_level("INFO"):
        job("push_receipts").func()

    assert ("Expo push receipt" in caplog.text) is logged




# ===========================================================
#  _ran_within — the strict ">" and the windows around it
# ===========================================================

@time_machine.travel(FROZEN, tick=False)
def test_a_run_started_exactly_on_the_cutoff_is_not_cover(app, seed_run):
    seed_run(source="knf.vu.lt", status="completed",
             started_at=(FROZEN - timedelta(seconds=NEWS_INTERVAL)).isoformat())

    assert scheduler._ran_within("knf.vu.lt", NEWS_INTERVAL) is False


@time_machine.travel(FROZEN, tick=False)
def test_a_run_one_second_inside_the_window_is_cover(app, seed_run):
    seed_run(source="knf.vu.lt", status="completed",
             started_at=(FROZEN - timedelta(seconds=NEWS_INTERVAL - 1)).isoformat())

    assert scheduler._ran_within("knf.vu.lt", NEWS_INTERVAL) is True


@time_machine.travel(FROZEN, tick=False)
def test_a_zero_second_window_is_covered_by_nothing_but_a_future_run(app, seed_run):
    seed_run(source="knf.vu.lt", status="completed", started_at=FROZEN.isoformat(), run_id="now")

    assert scheduler._ran_within("knf.vu.lt", 0) is False

    seed_run(source="knf.vu.lt", status="completed",
             started_at=(FROZEN + timedelta(seconds=1)).isoformat(), run_id="later")

    assert scheduler._ran_within("knf.vu.lt", 0) is True


@time_machine.travel(FROZEN, tick=False)
def test_a_negative_window_looks_into_the_future_and_finds_nothing(app, seed_run):
    seed_run(source="knf.vu.lt", status="completed", started_at=FROZEN.isoformat())

    assert scheduler._ran_within("knf.vu.lt", -3600) is False


def test_an_enormous_window_covers_a_run_from_years_ago(app, seed_run):
    seed_run(source="knf.vu.lt", status="completed", days=-900)

    assert scheduler._ran_within("knf.vu.lt", 10 ** 9) is True


@pytest.mark.parametrize("status", ["running", "failed"])
def test_only_a_completed_run_counts_as_cover(app, seed_run, status):
    seed_run(source="knf.vu.lt", status=status, minutes=-1)

    assert scheduler._ran_within("knf.vu.lt", NEWS_INTERVAL) is False


def test_another_sources_run_is_no_cover(app, seed_run):
    seed_run(source="vu.lt", status="completed", minutes=-1)

    assert scheduler._ran_within("knf.vu.lt", NEWS_INTERVAL) is False


def test_the_source_match_is_exact_and_case_sensitive(app, seed_run):
    seed_run(source="KNF.VU.LT", status="completed", minutes=-1)

    assert scheduler._ran_within("knf.vu.lt", NEWS_INTERVAL) is False


def test_an_empty_source_matches_nothing(app, seed_run):
    seed_run(source="knf.vu.lt", status="completed", minutes=-1)

    assert scheduler._ran_within("", NEWS_INTERVAL) is False


def test_the_newest_of_many_runs_decides_the_answer(app, seed_run):
    seed_run(source="knf.vu.lt", status="completed", days=-30)
    seed_run(source="knf.vu.lt", status="failed", minutes=-1)

    assert scheduler._ran_within("knf.vu.lt", NEWS_INTERVAL) is False
    assert scheduler._ran_within("knf.vu.lt", 40 * 24 * 3600) is True


def test_a_broken_lookup_answers_false_and_still_closes_the_connection(app, monkeypatch, caplog):
    holder = _recording_db(app, monkeypatch, fail=sqlite3.OperationalError("locked"))

    with caplog.at_level("WARNING"):
        assert scheduler._ran_within("knf.vu.lt", NEWS_INTERVAL) is False

    assert holder["conn"].closed == 1
    assert "Could not check recent knf.vu.lt runs" in caplog.text


def test_a_lookup_that_cannot_even_open_the_database_answers_false(monkeypatch, caplog):
    def _no_database():
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(scheduler, "get_db", _no_database)

    with caplog.at_level("WARNING"):
        assert scheduler._ran_within("knf.vu.lt", NEWS_INTERVAL) is False




# ===========================================================
#  _reconcile_interrupted_runs — the strict "<" and the
#  stale-run budget
# ===========================================================

@time_machine.travel(FROZEN, tick=False)
def test_a_running_row_stamped_exactly_on_the_cutoff_is_left_alone(app, seed_run, sql):
    run_id = seed_run(source="knf.vu.lt", status="running", started_at=FROZEN.isoformat())

    scheduler._reconcile_interrupted_runs()

    assert sql("SELECT status FROM scraper_runs WHERE id = ?", (run_id,), fetch=True)[0]["status"] \
        == "running"


@time_machine.travel(FROZEN, tick=False)
def test_a_running_row_one_second_before_the_cutoff_is_closed(app, seed_run, sql):
    run_id = seed_run(source="knf.vu.lt", status="running",
                      started_at=(FROZEN - timedelta(seconds=1)).isoformat())

    scheduler._reconcile_interrupted_runs()

    row = sql("SELECT status, error_message, finished_at FROM scraper_runs WHERE id = ?",
              (run_id,), fetch=True)[0]
    assert row["status"] == "failed"
    assert row["error_message"] == "interrupted"
    assert row["finished_at"] == FROZEN.isoformat()


@time_machine.travel(FROZEN, tick=False)
@pytest.mark.parametrize("age_seconds,closed", [
    (scheduler.STALE_RUN_SECONDS - 1, False),
    (scheduler.STALE_RUN_SECONDS, False),
    (scheduler.STALE_RUN_SECONDS + 1, True),
])
def test_the_daily_pass_only_touches_runs_past_the_stale_budget(app, seed_run, sql,
                                                                age_seconds, closed):
    run_id = seed_run(source="knf.vu.lt", status="running",
                      started_at=(FROZEN - timedelta(seconds=age_seconds)).isoformat())

    scheduler._reconcile_interrupted_runs(scheduler.STALE_RUN_SECONDS)

    status = sql("SELECT status FROM scraper_runs WHERE id = ?", (run_id,), fetch=True)[0]["status"]
    assert (status == "failed") is closed


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_a_finished_run_is_never_reopened_or_re_stamped(app, seed_run, sql, status):
    finished = _iso(hours=-9)
    run_id = seed_run(source="knf.vu.lt", status=status, hours=-10,
                      error="original message", finished_at=finished)

    scheduler._reconcile_interrupted_runs()

    row = sql("SELECT status, error_message, finished_at FROM scraper_runs WHERE id = ?",
              (run_id,), fetch=True)[0]
    assert (row["status"], row["error_message"], row["finished_at"]) == (status, "original message",
                                                                        finished)


def test_a_future_dated_running_row_is_never_closed(app, seed_run, sql):
    run_id = seed_run(source="knf.vu.lt", status="running", hours=+2)

    scheduler._reconcile_interrupted_runs()

    assert sql("SELECT status FROM scraper_runs WHERE id = ?", (run_id,), fetch=True)[0]["status"] \
        == "running"


def test_every_source_left_running_is_closed_in_one_pass(app, seed_run, sql):
    for source in ("knf.vu.lt", "vu.lt", "tvarkarasciai.vu.lt", "knf.vu.lt/info"):
        seed_run(source=source, status="running", hours=-1)

    scheduler._reconcile_interrupted_runs()

    rows = sql("SELECT status FROM scraper_runs", fetch=True)
    assert [r["status"] for r in rows] == ["failed"] * 4


def test_reconciling_twice_changes_nothing_the_second_time(app, seed_run, sql, caplog):
    seed_run(source="knf.vu.lt", status="running", hours=-1)

    scheduler._reconcile_interrupted_runs()
    first = sql("SELECT finished_at FROM scraper_runs", fetch=True)[0]["finished_at"]

    caplog.clear()
    with caplog.at_level("WARNING"):
        scheduler._reconcile_interrupted_runs()

    assert sql("SELECT finished_at FROM scraper_runs", fetch=True)[0]["finished_at"] == first
    assert "Closed" not in caplog.text


def test_reconciling_an_empty_table_says_nothing(app, caplog):
    with caplog.at_level("WARNING"):
        scheduler._reconcile_interrupted_runs()

    assert caplog.text == ""


def test_the_reconciliation_reports_how_many_rows_it_closed(app, seed_run, caplog):
    for _ in range(3):
        seed_run(source="knf.vu.lt", status="running", hours=-1)

    with caplog.at_level("WARNING"):
        scheduler._reconcile_interrupted_runs()

    assert "Closed 3 scraper run(s)" in caplog.text


def test_the_reconciliation_commits_and_closes_its_connection(app, monkeypatch, seed_run):
    seed_run(source="knf.vu.lt", status="running", hours=-1)
    holder = _recording_db(app, monkeypatch)

    scheduler._reconcile_interrupted_runs()

    assert holder["conn"].commits == 1
    assert holder["conn"].closed == 1


def test_a_reconciliation_that_blows_up_still_closes_its_connection(app, monkeypatch):
    holder = _recording_db(app, monkeypatch, fail=sqlite3.OperationalError("locked"))

    with pytest.raises(sqlite3.OperationalError):
        scheduler._reconcile_interrupted_runs()

    assert holder["conn"].closed == 1


def test_a_reconciliation_failure_at_start_does_not_cost_us_the_scheduler(app, monkeypatch, caplog):
    monkeypatch.setenv("SCRAPER_ENABLED", "1")
    monkeypatch.setenv("SCRAPER_STARTUP_RUNS", "0")
    monkeypatch.setattr(scheduler, "_reconcile_interrupted_runs",
                        lambda *args: (_ for _ in ()).throw(RuntimeError("no table")))

    with caplog.at_level("ERROR"):
        assert scheduler.start_scraper_scheduler(app) is True

    assert "Could not reconcile interrupted scraper runs" in caplog.text
    assert scheduler._scheduler.running


def test_a_run_the_reconciliation_closed_reads_as_failed_on_the_status_route(client, admin, app,
                                                                             seed_run):
    _user, headers = admin
    seed_run(source="knf.vu.lt", status="running", hours=-9)

    scheduler._reconcile_interrupted_runs()

    row = _runs(client.get(STATUS, headers=headers))[0]
    assert (row["status"], row["error"]) == ("failed", "interrupted")
    assert row["finishedAt"] is not None




# ===========================================================
#  _prune_orphaned_push_tokens — one strict "expires_at >"
# ===========================================================

def test_a_token_whose_owner_still_has_a_live_session_survives(app, make_user, seed_session,
                                                               seed_push_token, sql):
    user = make_user()
    seed_session(user["id"], days=+7)
    seed_push_token(user["id"])

    scheduler._prune_orphaned_push_tokens()

    assert len(sql("SELECT id FROM push_tokens", fetch=True)) == 1


def test_a_token_whose_only_session_expired_is_pruned(app, make_user, seed_session,
                                                      seed_push_token, sql):
    user = make_user()
    seed_session(user["id"], days=-1)
    seed_push_token(user["id"])

    scheduler._prune_orphaned_push_tokens()

    assert sql("SELECT id FROM push_tokens", fetch=True) == []


@time_machine.travel(FROZEN, tick=False)
def test_a_session_expiring_exactly_now_no_longer_protects_its_token(app, make_user, seed_session,
                                                                     seed_push_token, sql):
    user = make_user()
    seed_session(user["id"], expires_at=FROZEN.isoformat())
    seed_push_token(user["id"])

    scheduler._prune_orphaned_push_tokens()

    assert sql("SELECT id FROM push_tokens", fetch=True) == []


@time_machine.travel(FROZEN, tick=False)
def test_a_session_expiring_one_second_from_now_still_protects_it(app, make_user, seed_session,
                                                                  seed_push_token, sql):
    user = make_user()
    seed_session(user["id"], expires_at=(FROZEN + timedelta(seconds=1)).isoformat())
    seed_push_token(user["id"])

    scheduler._prune_orphaned_push_tokens()

    assert len(sql("SELECT id FROM push_tokens", fetch=True)) == 1


def test_one_live_session_among_several_saves_every_token_of_that_user(app, make_user, seed_session,
                                                                      seed_push_token, sql):
    user = make_user()
    seed_session(user["id"], days=-9)
    seed_session(user["id"], days=-1)
    seed_session(user["id"], days=+1)
    seed_push_token(user["id"])
    seed_push_token(user["id"])

    scheduler._prune_orphaned_push_tokens()

    assert len(sql("SELECT id FROM push_tokens", fetch=True)) == 2


def test_a_user_that_never_had_a_session_loses_its_tokens(app, make_user, seed_push_token, sql):
    user = make_user()
    seed_push_token(user["id"])

    scheduler._prune_orphaned_push_tokens()

    assert sql("SELECT id FROM push_tokens", fetch=True) == []


def test_the_prune_leaves_another_users_tokens_alone(app, make_user, seed_session,
                                                     seed_push_token, sql):
    live = make_user()
    gone = make_user()
    seed_session(live["id"], days=+7)
    seed_session(gone["id"], days=-7)
    kept = seed_push_token(live["id"])
    seed_push_token(gone["id"])

    scheduler._prune_orphaned_push_tokens()

    rows = sql("SELECT token FROM push_tokens", fetch=True)
    assert [r["token"] for r in rows] == [kept]


def test_an_already_deactivated_token_of_a_live_user_is_kept(app, make_user, seed_session,
                                                             seed_push_token, sql):
    # The prune keys off the session only — `active` is the
    # push sender's business, not this job's
    user = make_user()
    seed_session(user["id"], days=+7)
    seed_push_token(user["id"], active=0)

    scheduler._prune_orphaned_push_tokens()

    assert len(sql("SELECT id FROM push_tokens", fetch=True)) == 1


def test_pruning_an_empty_table_is_a_silent_no_op(app, caplog):
    with caplog.at_level("INFO"):
        scheduler._prune_orphaned_push_tokens()

    assert "Pruned" not in caplog.text


def test_the_prune_says_how_many_tokens_it_removed(app, make_user, seed_push_token, caplog):
    user = make_user()
    seed_push_token(user["id"])
    seed_push_token(user["id"])

    with caplog.at_level("INFO"):
        scheduler._prune_orphaned_push_tokens()

    assert "Pruned 2 push token(s)" in caplog.text


def test_the_prune_is_idempotent(app, make_user, seed_push_token, sql, caplog):
    user = make_user()
    seed_push_token(user["id"])

    scheduler._prune_orphaned_push_tokens()
    caplog.clear()
    with caplog.at_level("INFO"):
        scheduler._prune_orphaned_push_tokens()

    assert sql("SELECT id FROM push_tokens", fetch=True) == []
    assert "Pruned" not in caplog.text


def test_the_prune_commits_and_closes_its_connection(app, monkeypatch, make_user, seed_push_token):
    user = make_user()
    seed_push_token(user["id"])
    holder = _recording_db(app, monkeypatch)

    scheduler._prune_orphaned_push_tokens()

    assert holder["conn"].commits == 1
    assert holder["conn"].closed == 1


def test_a_prune_that_blows_up_still_closes_its_connection(app, monkeypatch):
    holder = _recording_db(app, monkeypatch, fail=sqlite3.OperationalError("locked"))

    with pytest.raises(sqlite3.OperationalError):
        scheduler._prune_orphaned_push_tokens()

    assert holder["conn"].closed == 1




# ===========================================================
#  The two halves together — a scheduler run and a route
#  reading the same table
# ===========================================================

def test_the_startup_reconciliation_is_visible_on_the_status_route(client, admin, app, monkeypatch,
                                                                   seed_run):
    _user, headers = admin
    monkeypatch.setenv("SCRAPER_ENABLED", "1")
    monkeypatch.setenv("SCRAPER_STARTUP_RUNS", "0")
    abandoned = seed_run(source="vu.lt", status="running", hours=-30)

    scheduler.start_scraper_scheduler(app)

    entry = [s for s in _sources(client.get(STATUS, headers=headers)) if s["source"] == "vu.lt"][0]
    assert entry["lastFailure"]["id"] == abandoned
    assert entry["lastFailure"]["error"] == "interrupted"


def test_a_manual_trigger_and_the_status_route_agree_on_the_same_run(client, admin, scrapers,
                                                                     seed_run):
    # The scrapers open and close their own scraper_runs rows;
    # /status is the only place a trigger's runId can be
    # resolved into an error message
    _user, headers = admin
    run_id = seed_run(source="knf.vu.lt", status="failed", minutes=-1,
                      error="HTTPError: 503 from knf.vu.lt")
    scrapers.results["knf"] = {"found": 0, "new": 0, "error": "HTTPError: 503", "runId": run_id}

    triggered = client.post(TRIGGER, headers=headers)
    listed = _runs(client.get(STATUS, headers=headers))[0]

    assert triggered.status_code == 502
    assert triggered.get_json()["knf"]["runId"] == listed["id"]
    assert listed["error"] == "HTTPError: 503 from knf.vu.lt"


def test_a_raw_json_body_on_a_trigger_is_read_as_bytes_not_as_an_escaped_string(client, admin,
                                                                                scrapers):
    # Rule 10: `json=` would hand the app an ALREADY-escaped
    # body. These routes read no body at all, and posting the
    # raw bytes proves it stays that way
    _user, headers = admin

    response = client.post(TRIGGER, data=json.dumps({"note": "I <3 scraping"}),
                           headers={**headers, "Content-Type": "application/json"})

    assert response.status_code == 200
    assert scrapers.kwargs_of("knf") == [{"pages": 2, "notify": False}]
