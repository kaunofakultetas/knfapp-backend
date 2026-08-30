# -----------------------------------------------------------
#  [*] Tests — database CORE, exhaustive pass
#
#  The gap-closing pass over the six functions that boot and
#  hand out the database, and nothing else:
#
#    init_db, get_db, utc_now_iso,
#    sweep_expired_sessions, _seed_defaults, _run_migrations
#
#  The broad suite already proves these work; this module goes
#  after every arm, guard and boundary they own:
#
#    - utc_now_iso: the exact travelled instant, the whole-
#      second form that DROPS its microseconds, and the
#      lexicographic ordering that variable-length form still
#      has to keep (the whole reason migration v17 exists)
#    - get_db: the falsiness guard (an EMPTY path is refused
#      too), a separate connection per call, WAL persisted in
#      the file, the per-connection foreign_keys=ON that a
#      plain sqlite3.connect does NOT get, and what happens
#      when the registered path is missing, a directory, or
#      never initialised
#    - sweep_expired_sessions: the strict `<` boundary at the
#      exact stamp, the caller-owns-the-commit contract, the
#      travelled clock, the legacy space-form hazard, and the
#      absent-table error it does not guard
#    - _seed_defaults: every ADMIN_PASSWORD shape (absent,
#      empty, whitespace, non-ASCII, past bcrypt's 72 bytes),
#      the one-and-only log of a generated secret, the
#      "KNF-" + 16 hex code shape, its 365-day expiry, and the
#      IntegrityError a second seeding earns
#    - _run_migrations: bookkeeping, ascending order, the skip
#      of a recorded version, the abort that leaves its own
#      version unwritten while COMMITTING the ones before it,
#      the retry on the next pass, and a full replay over live
#      data changing nothing
#    - init_db: fresh vs existing file, the zero-user refusal,
#      the five-attempt lock ladder and every error taxonomy
#      around it, the foreign-key audit ordering, the boot
#      sweep, path pinning even on failure, and the wrong-type
#      paths a misconfigured deploy can hand it
# -----------------------------------------------------------

import logging
import os
import pathlib
import re
import shutil
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import pytest
import time_machine

from app import database as dbmod
from app.database import get_db, init_db, sweep_expired_sessions, utc_now_iso


# The password the module-scoped template database is seeded
# with, so a test copying it can still log its admin in
_TEMPLATE_ADMIN_PASSWORD = "sablonas-slaptazodis-42"

# _seed_defaults prints a generated secret in exactly one
# shape; this is how a test reads it back out of the log
_GENERATED_PW = re.compile(r"generated password: (\S+) —")

# "KNF-" + secrets.token_hex(8).upper()
_CODE_SHAPE = re.compile(r"^KNF-[0-9A-F]{16}$")

# secrets.token_urlsafe(16) — 22 characters of URL-safe base64
_URLSAFE_22 = re.compile(r"^[A-Za-z0-9_-]{22}$")

# Secrets a seeded admin must never end up with
_FORBIDDEN_PASSWORDS = ("admin123", "admin", "password", "changeme", "")




# -----------------------------------------------------------
# _keep_db_path
# -----------------------------------------------------------
#
# init_db() pins a MODULE-level _db_path that get_db() reads,
# so a test booting its own file repoints the whole process.
# Restoring it keeps that invisible to the next test.
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _keep_db_path():
    saved = dbmod._db_path
    yield
    dbmod._db_path = saved




# -----------------------------------------------------------
# no_admin_password / env_admin_password
# -----------------------------------------------------------
#
# The two _seed_defaults branches, as environment states. The
# test image sets no ADMIN_PASSWORD, but the `app` fixture
# does — deleting it explicitly keeps the branch under test
# from depending on ordering.
# -----------------------------------------------------------

@pytest.fixture
def no_admin_password(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)


@pytest.fixture
def env_admin_password(monkeypatch):

    def _set(value):
        monkeypatch.setenv("ADMIN_PASSWORD", value)
        return value

    return _set




# -----------------------------------------------------------
# no_sleeping
# -----------------------------------------------------------
#
# init_db's lock ladder sleeps attempt*3 seconds. The list it
# returns collects what WOULD have been slept, so the backoff
# is asserted without any wall-clock time passing.
# -----------------------------------------------------------

@pytest.fixture
def no_sleeping(monkeypatch):
    slept = []
    monkeypatch.setattr(dbmod.time, "sleep", lambda seconds: slept.append(seconds))
    return slept




# -----------------------------------------------------------
# db_log
# -----------------------------------------------------------
#
# caplog wired to this module's logger at INFO, which is where
# every migration line, the seeding secrets and the foreign-key
# audit land.
# -----------------------------------------------------------

@pytest.fixture
def db_log(caplog):
    caplog.set_level(logging.INFO, logger="app.database")
    return caplog




# -----------------------------------------------------------
# _template_db / booted
# -----------------------------------------------------------
#
# A booted database costs one bcrypt hash plus the whole
# migration chain. Tests that only need a database SHAPED like
# a booted one copy this module-scoped template instead, and
# `booted` also pins _db_path so get_db() reads the copy.
# -----------------------------------------------------------

@pytest.fixture(scope="module")
def _template_db(tmp_path_factory):
    saved_path = dbmod._db_path
    saved_pw = os.environ.get("ADMIN_PASSWORD")
    os.environ["ADMIN_PASSWORD"] = _TEMPLATE_ADMIN_PASSWORD
    try:
        path = str(tmp_path_factory.mktemp("db-template") / "template.db")
        init_db(path)
    finally:
        if saved_pw is None:
            os.environ.pop("ADMIN_PASSWORD", None)
        else:
            os.environ["ADMIN_PASSWORD"] = saved_pw
        dbmod._db_path = saved_path
    return path


@pytest.fixture
def booted(_template_db, tmp_path):
    path = str(tmp_path / "booted.db")
    shutil.copyfile(_template_db, path)
    dbmod._db_path = path
    return path




# -----------------------------------------------------------
# Local helpers
# -----------------------------------------------------------
#
# _boot        — a real init_db() on a brand-new file
# _conn        — a connection shaped like init_db's own: no
#                row_factory (rows are TUPLES) and FK ON
# _schema_only — the _SCHEMA script and nothing else: no
#                seeding, no _migrations table
# _bare        — an empty database file, not even a schema
# _plant_*     — rows a route cannot create
# _messages    — the log lines carrying a needle
# -----------------------------------------------------------

def _boot(tmp_path, name="knfapp.db"):
    path = str(tmp_path / name)
    init_db(path)
    return path


def _conn(path):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _schema_only(tmp_path, name="schema-only.db"):
    conn = sqlite3.connect(str(tmp_path / name))
    conn.executescript(dbmod._SCHEMA)
    conn.commit()
    return conn


def _bare(tmp_path, name="bare.db"):
    return sqlite3.connect(str(tmp_path / name))


def _scalar(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()[0]


def _plant_user(conn, username="ona"):
    user_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO users (id, username, email, display_name, password_hash, role)"
        " VALUES (?, ?, ?, ?, 'x', 'student')",
        (user_id, username, f"{username}@knf.vu.lt", username.title()),
    )
    return user_id


def _plant_session(conn, user_id, session_id, expires_at):
    conn.execute(
        "INSERT INTO sessions (id, user_id, token, expires_at) VALUES (?, ?, ?, ?)",
        (session_id, user_id, f"token-{session_id}", expires_at),
    )


def _messages(log, needle):
    return [r.getMessage() for r in log.records if needle in r.getMessage()]


def _versions(conn):
    return [r[0] for r in conn.execute("SELECT version FROM _migrations ORDER BY version")]


# A raiser with a name, so a monkeypatched migration reads
# like the failure it stands in for
def _raiser(error):

    def _fn(_conn):
        raise error

    return _fn




# -----------------------------------------------------------
# utc_now_iso — shape
# -----------------------------------------------------------

def test_utc_now_iso_stamps_the_exact_travelled_instant():
    moment = datetime(2026, 8, 29, 12, 34, 56, 789012, tzinfo=timezone.utc)
    with time_machine.travel(moment, tick=False):
        assert utc_now_iso() == "2026-08-29T12:34:56.789012+00:00"


def test_utc_now_iso_always_carries_the_explicit_utc_offset():
    for moment in (
        datetime(1970, 1, 2, tzinfo=timezone.utc),
        datetime(2000, 2, 29, 23, 59, 59, 999999, tzinfo=timezone.utc),
        datetime(2026, 8, 29, 12, tzinfo=timezone.utc),
        datetime(2999, 12, 31, 23, 59, 59, tzinfo=timezone.utc),
    ):
        with time_machine.travel(moment, tick=False):
            stamp = utc_now_iso()
        assert stamp.endswith("+00:00"), stamp


def test_utc_now_iso_never_uses_the_z_suffix_form():
    with time_machine.travel(datetime(2026, 8, 29, 12, tzinfo=timezone.utc), tick=False):
        assert not utc_now_iso().endswith("Z")


def test_utc_now_iso_never_uses_the_space_form_v17_had_to_repair():
    with time_machine.travel(datetime(2026, 8, 29, 12, 0, 0, 1, tzinfo=timezone.utc), tick=False):
        assert " " not in utc_now_iso()


def test_a_whole_second_instant_drops_its_microseconds():
    # isoformat() omits the fraction when it is zero, so the
    # stamp is 25 characters instead of the usual 32
    with time_machine.travel(datetime(2026, 8, 29, 12, 34, 56, tzinfo=timezone.utc), tick=False):
        stamp = utc_now_iso()

    assert stamp == "2026-08-29T12:34:56+00:00"
    assert len(stamp) == 25


def test_a_microsecond_instant_keeps_the_full_thirty_two_characters():
    with time_machine.travel(datetime(2026, 8, 29, 12, 34, 56, 1, tzinfo=timezone.utc), tick=False):
        stamp = utc_now_iso()

    assert stamp == "2026-08-29T12:34:56.000001+00:00"
    assert len(stamp) == 32


def test_two_stamps_taken_at_one_frozen_instant_are_identical():
    with time_machine.travel(datetime(2026, 8, 29, 12, tzinfo=timezone.utc), tick=False):
        assert utc_now_iso() == utc_now_iso()




# -----------------------------------------------------------
# utc_now_iso — the ordering every comparison in the app rests on
# -----------------------------------------------------------

def _stamp_at(moment):
    with time_machine.travel(moment, tick=False):
        return utc_now_iso()


def test_a_whole_second_stamp_sorts_before_the_same_second_with_microseconds():
    # '+' (0x2B) sorts below '.' (0x2E), so the short form
    # lands where .000000 would — earlier, which is correct
    whole = _stamp_at(datetime(2026, 8, 29, 12, 34, 56, tzinfo=timezone.utc))
    fractional = _stamp_at(datetime(2026, 8, 29, 12, 34, 56, 1, tzinfo=timezone.utc))

    assert whole < fractional


def test_a_whole_second_stamp_sorts_after_the_end_of_the_previous_second():
    previous = _stamp_at(datetime(2026, 8, 29, 12, 34, 55, 999999, tzinfo=timezone.utc))
    whole = _stamp_at(datetime(2026, 8, 29, 12, 34, 56, tzinfo=timezone.utc))

    assert previous < whole


def test_stamps_sort_lexicographically_across_a_year_boundary():
    old_year = _stamp_at(datetime(2025, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc))
    new_year = _stamp_at(datetime(2026, 1, 1, 0, 0, 0, 1, tzinfo=timezone.utc))

    assert old_year < new_year


def test_stamps_sort_lexicographically_across_a_leap_day():
    leap = _stamp_at(datetime(2028, 2, 29, 12, tzinfo=timezone.utc))
    after = _stamp_at(datetime(2028, 3, 1, 12, tzinfo=timezone.utc))

    assert leap < after


def test_a_full_day_of_travel_never_inverts_the_order():
    stamps = [
        _stamp_at(datetime(2026, 8, 29, hour, minute, tzinfo=timezone.utc))
        for hour in (0, 6, 12, 18, 23)
        for minute in (0, 30, 59)
    ]

    assert stamps == sorted(stamps)


def test_the_space_form_v17_removed_still_sorts_before_every_t_form_stamp():
    stamp = _stamp_at(datetime(2026, 8, 29, 0, 0, 1, tzinfo=timezone.utc))

    # A LATER space-form value still sorts before an EARLIER
    # T-form one — exactly what migration v17 had to normalise
    assert "2026-08-29 23:59:59" < stamp


def test_sqlite_parses_the_stamp_as_a_real_datetime(booted):
    conn = _conn(booted)
    try:
        stamp = utc_now_iso()
        assert _scalar(conn, "SELECT datetime(?)", (stamp,)) is not None
    finally:
        conn.close()




# -----------------------------------------------------------
# get_db — the guard
# -----------------------------------------------------------

def test_get_db_refuses_before_init_db_has_run():
    dbmod._db_path = None
    with pytest.raises(RuntimeError, match="init_db"):
        get_db()


def test_the_guard_tests_for_falsiness_and_not_only_for_none(tmp_path):
    # An EMPTY path must not sail past the guard: sqlite3 would
    # hand back a private temporary database holding no tables
    # at all instead of refusing
    dbmod._db_path = ""

    with pytest.raises(RuntimeError, match="init_db"):
        get_db()




# -----------------------------------------------------------
# get_db — the four PRAGMAs
# -----------------------------------------------------------

def test_get_db_sets_write_ahead_logging(booted):
    conn = get_db()
    try:
        assert _scalar(conn, "PRAGMA journal_mode") == "wal"
    finally:
        conn.close()


def test_get_db_sets_the_thirty_second_busy_timeout(booted):
    conn = get_db()
    try:
        assert _scalar(conn, "PRAGMA busy_timeout") == 30000
    finally:
        conn.close()


def test_get_db_pairs_wal_with_synchronous_normal(booted):
    conn = get_db()
    try:
        assert _scalar(conn, "PRAGMA synchronous") == 1
    finally:
        conn.close()


def test_wal_mode_stays_in_the_file_after_the_connection_closes(booted):
    get_db().close()

    plain = sqlite3.connect(booted)
    try:
        assert _scalar(plain, "PRAGMA journal_mode") == "wal"
    finally:
        plain.close()


def test_get_db_enforces_foreign_keys_on_every_connection(booted):
    conn = get_db()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO sessions (id, user_id, token, expires_at)"
                " VALUES ('s1', 'no-such-user', 'tok', '2099-01-01T00:00:00+00:00')"
            )
    finally:
        conn.close()


def test_a_get_db_connection_cascades_a_user_delete(booted):
    conn = get_db()
    try:
        user_id = _plant_user(conn, "kaskadas")
        _plant_session(conn, user_id, "live", "2099-01-01T00:00:00+00:00")
        conn.commit()

        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()

        assert _scalar(conn, "SELECT COUNT(*) FROM sessions WHERE user_id = ?", (user_id,)) == 0
    finally:
        conn.close()


def test_a_plain_connection_does_not_cascade_which_is_why_the_pragma_is_here(booted):
    # foreign_keys is PER CONNECTION: the same delete on a
    # connection that never ran the PRAGMA leaves the orphan
    plain = sqlite3.connect(booted)
    try:
        user_id = _plant_user(plain, "be-kaskados")
        _plant_session(plain, user_id, "orphan", "2099-01-01T00:00:00+00:00")
        plain.commit()

        plain.execute("DELETE FROM users WHERE id = ?", (user_id,))
        plain.commit()

        assert _scalar(plain, "SELECT COUNT(*) FROM sessions WHERE user_id = ?", (user_id,)) == 1
    finally:
        plain.close()




# -----------------------------------------------------------
# get_db — connection ownership
# -----------------------------------------------------------

def test_get_db_hands_out_dict_and_index_addressable_rows(booted):
    conn = get_db()
    try:
        row = conn.execute("SELECT username, role FROM users WHERE username = 'admin'").fetchone()
        assert isinstance(row, sqlite3.Row)
        assert row["username"] == "admin"
        assert row[0] == "admin"
        assert set(row.keys()) == {"username", "role"}
        assert len(row) == 2
    finally:
        conn.close()


def test_every_call_is_a_separate_connection(booted):
    first, second = get_db(), get_db()
    try:
        assert first is not second
    finally:
        first.close()
        second.close()


def test_an_uncommitted_write_is_invisible_to_the_next_connection(booted):
    writer = get_db()
    reader = get_db()
    try:
        _plant_user(writer, "neivykdyta")
        assert _scalar(reader, "SELECT COUNT(*) FROM users WHERE username = 'neivykdyta'") == 0

        writer.commit()
        assert _scalar(reader, "SELECT COUNT(*) FROM users WHERE username = 'neivykdyta'") == 1
    finally:
        writer.close()
        reader.close()


def test_the_caller_owns_the_connection_and_must_close_it(booted):
    conn = get_db()
    assert _scalar(conn, "SELECT 1") == 1

    conn.close()
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_get_db_follows_a_repointed_module_path(booted, _template_db, tmp_path):
    other = str(tmp_path / "kitas.db")
    shutil.copyfile(_template_db, other)
    _conn(other).close()

    dbmod._db_path = other
    conn = get_db()
    try:
        assert conn.execute("PRAGMA database_list").fetchone()[2] == other
    finally:
        conn.close()




# -----------------------------------------------------------
# get_db — unhappy paths
# -----------------------------------------------------------

def test_get_db_creates_an_empty_file_when_the_registered_path_has_no_database(tmp_path):
    missing = str(tmp_path / "dar-nera.db")
    dbmod._db_path = missing

    conn = get_db()
    try:
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            conn.execute("SELECT COUNT(*) FROM users")
    finally:
        conn.close()

    assert os.path.exists(missing), "sqlite3.connect creates the file it cannot find"


def test_get_db_fails_when_the_registered_path_is_a_directory(tmp_path):
    dbmod._db_path = str(tmp_path)
    with pytest.raises(sqlite3.OperationalError):
        get_db()


def test_get_db_fails_when_the_registered_directory_does_not_exist(tmp_path):
    dbmod._db_path = str(tmp_path / "nera" / "knfapp.db")
    with pytest.raises(sqlite3.OperationalError):
        get_db()




# -----------------------------------------------------------
# sweep_expired_sessions — the strict `<` boundary
# -----------------------------------------------------------

def test_a_session_that_expired_a_microsecond_ago_is_swept(booted):
    moment = datetime(2026, 8, 29, 12, 0, 0, 500000, tzinfo=timezone.utc)
    conn = _conn(booted)
    try:
        user_id = _plant_user(conn)
        _plant_session(conn, user_id, "dead", (moment - timedelta(microseconds=1)).isoformat())

        with time_machine.travel(moment, tick=False):
            sweep_expired_sessions(conn)

        assert _scalar(conn, "SELECT COUNT(*) FROM sessions") == 0
    finally:
        conn.close()


def test_a_session_expiring_at_exactly_now_survives_the_sweep(booted):
    moment = datetime(2026, 8, 29, 12, 0, 0, 500000, tzinfo=timezone.utc)
    conn = _conn(booted)
    try:
        user_id = _plant_user(conn)
        _plant_session(conn, user_id, "riba", moment.isoformat())

        with time_machine.travel(moment, tick=False):
            sweep_expired_sessions(conn)

        assert [r[0] for r in conn.execute("SELECT id FROM sessions")] == ["riba"]
    finally:
        conn.close()


def test_a_session_expiring_a_microsecond_from_now_survives_the_sweep(booted):
    moment = datetime(2026, 8, 29, 12, 0, 0, 500000, tzinfo=timezone.utc)
    conn = _conn(booted)
    try:
        user_id = _plant_user(conn)
        _plant_session(conn, user_id, "gyva", (moment + timedelta(microseconds=1)).isoformat())

        with time_machine.travel(moment, tick=False):
            sweep_expired_sessions(conn)

        assert _scalar(conn, "SELECT COUNT(*) FROM sessions") == 1
    finally:
        conn.close()


def test_the_sweep_follows_the_clock_forward(booted):
    conn = _conn(booted)
    try:
        user_id = _plant_user(conn)
        _plant_session(conn, user_id, "menesis",
                       (datetime(2026, 8, 29, tzinfo=timezone.utc) + timedelta(days=30)).isoformat())

        with time_machine.travel(datetime(2026, 9, 20, tzinfo=timezone.utc), tick=False):
            sweep_expired_sessions(conn)
        assert _scalar(conn, "SELECT COUNT(*) FROM sessions") == 1

        with time_machine.travel(datetime(2026, 10, 20, tzinfo=timezone.utc), tick=False):
            sweep_expired_sessions(conn)
        assert _scalar(conn, "SELECT COUNT(*) FROM sessions") == 0
    finally:
        conn.close()




# -----------------------------------------------------------
# sweep_expired_sessions — contract and scale
# -----------------------------------------------------------

def test_the_sweep_returns_nothing(booted):
    conn = _conn(booted)
    try:
        assert sweep_expired_sessions(conn) is None
    finally:
        conn.close()


def test_the_sweep_leaves_the_delete_for_the_caller_to_commit(booted):
    conn = _conn(booted)
    try:
        user_id = _plant_user(conn)
        _plant_session(conn, user_id, "dead", "2000-01-01T00:00:00+00:00")
        conn.commit()

        sweep_expired_sessions(conn)
        assert _scalar(conn, "SELECT COUNT(*) FROM sessions") == 0

        conn.rollback()
        assert _scalar(conn, "SELECT COUNT(*) FROM sessions") == 1, \
            "the sweep must not commit — its caller owns the transaction"
    finally:
        conn.close()


def test_a_committed_sweep_is_visible_to_the_next_connection(booted):
    conn = _conn(booted)
    try:
        user_id = _plant_user(conn)
        _plant_session(conn, user_id, "dead", "2000-01-01T00:00:00+00:00")
        conn.commit()

        sweep_expired_sessions(conn)
        conn.commit()
    finally:
        conn.close()

    again = _conn(booted)
    try:
        assert _scalar(again, "SELECT COUNT(*) FROM sessions") == 0
    finally:
        again.close()


def test_the_sweep_is_idempotent(booted, db_log):
    conn = _conn(booted)
    try:
        user_id = _plant_user(conn)
        _plant_session(conn, user_id, "dead", "2000-01-01T00:00:00+00:00")

        sweep_expired_sessions(conn)
        db_log.clear()
        sweep_expired_sessions(conn)

        assert _scalar(conn, "SELECT COUNT(*) FROM sessions") == 0
        assert _messages(db_log, "Swept") == []
    finally:
        conn.close()


def test_a_sweep_that_removes_nothing_stays_silent(booted, db_log):
    conn = _conn(booted)
    try:
        db_log.clear()
        sweep_expired_sessions(conn)
        assert _messages(db_log, "Swept") == []
    finally:
        conn.close()


@pytest.mark.parametrize("expired", [1, 2, 7])
def test_the_sweep_logs_the_number_of_rows_it_removed(booted, db_log, expired):
    conn = _conn(booted)
    try:
        user_id = _plant_user(conn)
        for n in range(expired):
            _plant_session(conn, user_id, f"dead-{n}", "2000-01-01T00:00:00+00:00")
        db_log.clear()

        sweep_expired_sessions(conn)

        assert _messages(db_log, f"Swept {expired} expired session row(s)")
    finally:
        conn.close()


def test_the_sweep_clears_three_hundred_rows_in_one_statement(booted):
    conn = _conn(booted)
    try:
        user_id = _plant_user(conn)
        conn.executemany(
            "INSERT INTO sessions (id, user_id, token, expires_at) VALUES (?, ?, ?, '2000-01-01T00:00:00+00:00')",
            [(f"s{n}", user_id, f"t{n}") for n in range(300)],
        )

        sweep_expired_sessions(conn)

        assert _scalar(conn, "SELECT COUNT(*) FROM sessions") == 0
    finally:
        conn.close()


def test_the_sweep_is_not_scoped_to_one_user(booted):
    conn = _conn(booted)
    try:
        first = _plant_user(conn, "pirma")
        second = _plant_user(conn, "antra")
        _plant_session(conn, first, "a", "2000-01-01T00:00:00+00:00")
        _plant_session(conn, second, "b", "2000-01-01T00:00:00+00:00")

        sweep_expired_sessions(conn)

        assert _scalar(conn, "SELECT COUNT(*) FROM sessions") == 0
    finally:
        conn.close()


def test_the_sweep_touches_nothing_but_sessions(booted):
    conn = _conn(booted)
    try:
        user_id = _plant_user(conn)
        _plant_session(conn, user_id, "dead", "2000-01-01T00:00:00+00:00")
        users_before = _scalar(conn, "SELECT COUNT(*) FROM users")
        codes_before = _scalar(conn, "SELECT COUNT(*) FROM invitation_codes")

        sweep_expired_sessions(conn)

        assert _scalar(conn, "SELECT COUNT(*) FROM users") == users_before
        assert _scalar(conn, "SELECT COUNT(*) FROM invitation_codes") == codes_before
    finally:
        conn.close()


def test_the_sweep_works_on_a_row_factory_connection(booted):
    conn = sqlite3.connect(booted)
    conn.row_factory = sqlite3.Row
    try:
        user_id = _plant_user(conn)
        _plant_session(conn, user_id, "dead", "2000-01-01T00:00:00+00:00")

        sweep_expired_sessions(conn)

        assert _scalar(conn, "SELECT COUNT(*) FROM sessions") == 0
    finally:
        conn.close()


def test_a_legacy_space_form_expiry_is_swept_even_when_it_lies_in_the_future(booted):
    # The v17 hazard, pinned: under string comparison a
    # space-form value sorts before every T-form one, so a
    # future space-form session looks expired. Nothing writes
    # that shape any more — this is why v17 had to normalise it
    conn = _conn(booted)
    try:
        user_id = _plant_user(conn)
        _plant_session(conn, user_id, "legacy", "2026-08-29 23:59:59")

        with time_machine.travel(datetime(2026, 8, 29, 12, tzinfo=timezone.utc), tick=False):
            sweep_expired_sessions(conn)

        assert _scalar(conn, "SELECT COUNT(*) FROM sessions") == 0
    finally:
        conn.close()


def test_a_future_space_form_expiry_on_a_later_date_still_survives(booted):
    conn = _conn(booted)
    try:
        user_id = _plant_user(conn)
        _plant_session(conn, user_id, "legacy", "2099-01-01 00:00:00")

        with time_machine.travel(datetime(2026, 8, 29, 12, tzinfo=timezone.utc), tick=False):
            sweep_expired_sessions(conn)

        assert _scalar(conn, "SELECT COUNT(*) FROM sessions") == 1
    finally:
        conn.close()


def test_a_junk_empty_expiry_string_sorts_below_every_stamp_and_is_swept(booted):
    conn = _conn(booted)
    try:
        user_id = _plant_user(conn)
        _plant_session(conn, user_id, "junk", "")

        sweep_expired_sessions(conn)

        assert _scalar(conn, "SELECT COUNT(*) FROM sessions") == 0
    finally:
        conn.close()


def test_the_largest_representable_expiry_survives_the_sweep(booted):
    conn = _conn(booted)
    try:
        user_id = _plant_user(conn)
        _plant_session(conn, user_id, "amzina", "9999-12-31T23:59:59.999999+00:00")

        sweep_expired_sessions(conn)

        assert _scalar(conn, "SELECT COUNT(*) FROM sessions") == 1
    finally:
        conn.close()


def test_a_non_utc_offset_is_compared_as_text_not_as_an_instant(booted):
    # 14:00+03:00 is 11:00 UTC — already past — but the plain
    # string comparison reads "14" and keeps the row. Nothing
    # writes offsets other than +00:00 (utc_now_iso is the one
    # stamper), which is exactly why it may compare as text
    conn = _conn(booted)
    try:
        user_id = _plant_user(conn)
        _plant_session(conn, user_id, "kitas-laiko-juostos", "2026-08-29T14:00:00+03:00")

        with time_machine.travel(datetime(2026, 8, 29, 12, tzinfo=timezone.utc), tick=False):
            sweep_expired_sessions(conn)

        assert _scalar(conn, "SELECT COUNT(*) FROM sessions") == 1
    finally:
        conn.close()


def test_the_sweep_needs_a_sessions_table_and_says_so(tmp_path):
    conn = _bare(tmp_path)
    try:
        with pytest.raises(sqlite3.OperationalError, match="sessions"):
            sweep_expired_sessions(conn)
    finally:
        conn.close()




# -----------------------------------------------------------
# _seed_defaults — the admin row
# -----------------------------------------------------------

def test_seeding_plants_exactly_one_admin_with_the_documented_identity(tmp_path, no_admin_password):
    conn = _schema_only(tmp_path)
    try:
        dbmod._seed_defaults(conn)

        rows = conn.execute(
            "SELECT id, username, email, display_name, role, invited, active FROM users"
        ).fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row[1:] == ("admin", "admin@knf.vu.lt", "Administratorius", "admin", 1, 1)
        assert uuid.UUID(row[0]).version == 4
    finally:
        conn.close()


def test_the_env_password_is_the_one_the_admin_ends_up_with(tmp_path, env_admin_password):
    password = env_admin_password("Kaunas-Fakultetas-2026")
    conn = _schema_only(tmp_path)
    try:
        dbmod._seed_defaults(conn)
        stored = _scalar(conn, "SELECT password_hash FROM users WHERE username = 'admin'")
    finally:
        conn.close()

    assert bcrypt.checkpw(password.encode(), stored.encode())


def test_an_empty_admin_password_env_var_falls_back_to_a_generated_secret(tmp_path, env_admin_password, db_log):
    env_admin_password("")
    conn = _schema_only(tmp_path)
    try:
        dbmod._seed_defaults(conn)
        stored = _scalar(conn, "SELECT password_hash FROM users WHERE username = 'admin'")
    finally:
        conn.close()

    generated = _GENERATED_PW.search("\n".join(_messages(db_log, "FIRST BOOT")))
    assert generated, "an empty ADMIN_PASSWORD is falsy — the generated branch must run"
    assert bcrypt.checkpw(generated.group(1).encode(), stored.encode())
    assert not bcrypt.checkpw(b"", stored.encode())


def test_a_single_space_admin_password_is_taken_literally(tmp_path, env_admin_password, db_log):
    env_admin_password(" ")
    conn = _schema_only(tmp_path)
    try:
        dbmod._seed_defaults(conn)
        stored = _scalar(conn, "SELECT password_hash FROM users WHERE username = 'admin'")
    finally:
        conn.close()

    assert bcrypt.checkpw(b" ", stored.encode())
    assert _messages(db_log, "password from ADMIN_PASSWORD")


def test_a_lithuanian_admin_password_survives_the_utf_8_hash(tmp_path, env_admin_password):
    password = env_admin_password("slaptažodis-ąčęėįšųūž")
    conn = _schema_only(tmp_path)
    try:
        dbmod._seed_defaults(conn)
        stored = _scalar(conn, "SELECT password_hash FROM users WHERE username = 'admin'")
    finally:
        conn.close()

    assert bcrypt.checkpw(password.encode(), stored.encode())


def test_an_admin_password_past_bcrypts_seventy_two_bytes_still_seeds(tmp_path, env_admin_password):
    password = env_admin_password("i" * 200)
    conn = _schema_only(tmp_path)
    try:
        dbmod._seed_defaults(conn)
        stored = _scalar(conn, "SELECT password_hash FROM users WHERE username = 'admin'")
    finally:
        conn.close()

    assert bcrypt.checkpw(password.encode(), stored.encode())


def test_the_generated_password_is_a_twenty_two_character_urlsafe_secret(tmp_path, no_admin_password, db_log):
    conn = _schema_only(tmp_path)
    try:
        dbmod._seed_defaults(conn)
    finally:
        conn.close()

    generated = _GENERATED_PW.search("\n".join(_messages(db_log, "FIRST BOOT")))
    assert generated
    assert _URLSAFE_22.match(generated.group(1)), generated.group(1)


def test_the_generated_password_differs_between_two_databases(tmp_path, no_admin_password, db_log):
    for name in ("pirma.db", "antra.db"):
        conn = _schema_only(tmp_path, name)
        try:
            dbmod._seed_defaults(conn)
        finally:
            conn.close()

    generated = _GENERATED_PW.findall("\n".join(_messages(db_log, "FIRST BOOT")))
    assert len(generated) == 2
    assert generated[0] != generated[1], "the seeded password must never be a fixed string"


def test_the_generated_password_is_never_one_of_the_known_bad_literals(tmp_path, no_admin_password, db_log):
    conn = _schema_only(tmp_path)
    try:
        dbmod._seed_defaults(conn)
        stored = _scalar(conn, "SELECT password_hash FROM users WHERE username = 'admin'")
    finally:
        conn.close()

    for forbidden in _FORBIDDEN_PASSWORDS:
        assert not bcrypt.checkpw(forbidden.encode(), stored.encode()), forbidden


def test_the_generated_password_is_logged_exactly_once(tmp_path, no_admin_password, db_log):
    conn = _schema_only(tmp_path)
    try:
        dbmod._seed_defaults(conn)
    finally:
        conn.close()

    assert len(_messages(db_log, "generated password")) == 1


def test_an_env_password_is_acknowledged_but_never_echoed(tmp_path, env_admin_password, db_log):
    password = env_admin_password("nespausdinti-manes-123")
    conn = _schema_only(tmp_path)
    try:
        dbmod._seed_defaults(conn)
    finally:
        conn.close()

    logged = "\n".join(_messages(db_log, "FIRST BOOT"))
    assert "from ADMIN_PASSWORD" in logged
    assert password not in logged, "an env-provided password must never reach the log"
    assert "generated password" not in logged




# -----------------------------------------------------------
# _seed_defaults — the bootstrap invitation code
# -----------------------------------------------------------

def test_the_bootstrap_code_has_the_documented_shape(tmp_path, no_admin_password):
    conn = _schema_only(tmp_path)
    try:
        dbmod._seed_defaults(conn)
        code = _scalar(conn, "SELECT code FROM invitation_codes")
    finally:
        conn.close()

    assert _CODE_SHAPE.match(code), code


def test_the_bootstrap_code_is_not_the_leaked_literal(tmp_path, no_admin_password):
    conn = _schema_only(tmp_path)
    try:
        dbmod._seed_defaults(conn)
        code = _scalar(conn, "SELECT code FROM invitation_codes")
    finally:
        conn.close()

    assert code != "WELCOME-KNF-2026", "the literal that shipped in git must never come back"


def test_the_bootstrap_code_is_owned_by_the_seeded_admin(tmp_path, no_admin_password):
    conn = _schema_only(tmp_path)
    try:
        dbmod._seed_defaults(conn)
        admin_id = _scalar(conn, "SELECT id FROM users WHERE username = 'admin'")
        row = conn.execute("SELECT role, created_by, max_uses, use_count FROM invitation_codes").fetchone()
    finally:
        conn.close()

    assert row == ("student", admin_id, 100, 0)


def test_the_bootstrap_code_expires_exactly_a_year_after_seeding(tmp_path, no_admin_password):
    moment = datetime(2026, 8, 29, 12, 34, 56, 789012, tzinfo=timezone.utc)
    conn = _schema_only(tmp_path)
    try:
        with time_machine.travel(moment, tick=False):
            dbmod._seed_defaults(conn)
        expires = _scalar(conn, "SELECT expires_at FROM invitation_codes")
    finally:
        conn.close()

    assert expires == (moment + timedelta(days=365)).isoformat()


def test_the_bootstrap_code_differs_between_two_databases(tmp_path, no_admin_password):
    codes = []
    for name in ("pirma.db", "antra.db"):
        conn = _schema_only(tmp_path, name)
        try:
            dbmod._seed_defaults(conn)
            codes.append(_scalar(conn, "SELECT code FROM invitation_codes"))
        finally:
            conn.close()

    assert codes[0] != codes[1]


def test_the_code_is_logged_in_the_generated_password_branch(tmp_path, no_admin_password, db_log):
    conn = _schema_only(tmp_path)
    try:
        dbmod._seed_defaults(conn)
        code = _scalar(conn, "SELECT code FROM invitation_codes")
    finally:
        conn.close()

    assert code in "\n".join(_messages(db_log, "FIRST BOOT"))


def test_the_code_is_logged_in_the_env_password_branch(tmp_path, env_admin_password, db_log):
    env_admin_password("aplinkos-slaptazodis")
    conn = _schema_only(tmp_path)
    try:
        dbmod._seed_defaults(conn)
        code = _scalar(conn, "SELECT code FROM invitation_codes")
    finally:
        conn.close()

    assert code in "\n".join(_messages(db_log, "FIRST BOOT"))




# -----------------------------------------------------------
# _seed_defaults — transaction and idempotency
# -----------------------------------------------------------

def test_seeding_commits_its_own_rows(tmp_path, no_admin_password):
    conn = _schema_only(tmp_path)
    try:
        dbmod._seed_defaults(conn)
        conn.rollback()

        assert _scalar(conn, "SELECT COUNT(*) FROM users") == 1
        assert _scalar(conn, "SELECT COUNT(*) FROM invitation_codes") == 1
    finally:
        conn.close()


def test_seeding_the_same_database_twice_is_refused_by_the_unique_username(tmp_path, no_admin_password):
    conn = _schema_only(tmp_path)
    try:
        dbmod._seed_defaults(conn)

        with pytest.raises(sqlite3.IntegrityError):
            dbmod._seed_defaults(conn)
        conn.rollback()

        assert _scalar(conn, "SELECT COUNT(*) FROM users") == 1
    finally:
        conn.close()


def test_seeding_needs_the_schema_and_says_so(tmp_path, no_admin_password):
    conn = _bare(tmp_path)
    try:
        with pytest.raises(sqlite3.OperationalError, match="users"):
            dbmod._seed_defaults(conn)
    finally:
        conn.close()


def test_seeding_a_database_that_already_holds_other_users_still_plants_the_admin(tmp_path, no_admin_password):
    # The "only on a brand-new file" rule lives in init_db, not
    # here — _seed_defaults itself asks no questions
    conn = _schema_only(tmp_path)
    try:
        _plant_user(conn, "jonas")
        conn.commit()

        dbmod._seed_defaults(conn)

        assert _scalar(conn, "SELECT COUNT(*) FROM users") == 2
        assert _scalar(conn, "SELECT COUNT(*) FROM users WHERE role = 'admin'") == 1
    finally:
        conn.close()


def test_the_seeded_code_satisfies_the_foreign_key_it_declares(tmp_path, no_admin_password):
    # The admin is inserted first ON PURPOSE: created_by
    # points at it and init_db seeds with foreign_keys ON
    conn = _schema_only(tmp_path)
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        dbmod._seed_defaults(conn)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()




# -----------------------------------------------------------
# _run_migrations — bookkeeping
# -----------------------------------------------------------

def test_the_bookkeeping_table_is_created_outside_the_schema_script(tmp_path):
    conn = _schema_only(tmp_path)
    try:
        assert "_migrations" not in {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }

        dbmod._run_migrations(conn)

        columns = {r[1]: r for r in conn.execute("PRAGMA table_info(_migrations)")}
        assert set(columns) == {"version", "applied_at"}
        assert columns["version"][5] == 1, "version is the primary key"
        assert columns["applied_at"][3] == 1, "applied_at is NOT NULL"
    finally:
        conn.close()


def test_every_registered_version_is_recorded_once(tmp_path):
    conn = _schema_only(tmp_path)
    try:
        dbmod._run_migrations(conn)
        versions = _versions(conn)
    finally:
        conn.close()

    assert versions, "the registry applied nothing at all"
    assert len(versions) == len(set(versions))
    assert versions == sorted(versions)


def test_every_recorded_version_carries_an_applied_at_stamp(tmp_path):
    conn = _schema_only(tmp_path)
    try:
        dbmod._run_migrations(conn)
        assert _scalar(conn, "SELECT COUNT(*) FROM _migrations WHERE applied_at IS NULL") == 0
    finally:
        conn.close()


def test_the_versions_run_in_ascending_order(tmp_path, db_log):
    conn = _schema_only(tmp_path)
    try:
        dbmod._run_migrations(conn)
    finally:
        conn.close()

    ran = [int(m) for m in re.findall(r"Running data migration v(\d+):",
                                      "\n".join(_messages(db_log, "Running data migration")))]
    assert ran == sorted(ran)
    assert len(ran) == len(set(ran))


def test_a_second_pass_runs_and_records_nothing(tmp_path, db_log):
    conn = _schema_only(tmp_path)
    try:
        dbmod._run_migrations(conn)
        before = _versions(conn)
        db_log.clear()

        dbmod._run_migrations(conn)

        assert _versions(conn) == before
        assert _messages(db_log, "Running data migration") == []
    finally:
        conn.close()


def test_a_third_pass_still_records_no_duplicate(tmp_path):
    conn = _schema_only(tmp_path)
    try:
        dbmod._run_migrations(conn)
        dbmod._run_migrations(conn)
        dbmod._run_migrations(conn)
        versions = _versions(conn)
    finally:
        conn.close()

    assert len(versions) == len(set(versions))


def test_a_recorded_version_is_skipped_even_when_its_work_is_gone(tmp_path):
    # The banner's promise: a migration runs exactly once per
    # DB file, so undoing its work by hand does not bring it
    # back — only deleting its _migrations row does
    conn = _schema_only(tmp_path)
    try:
        dbmod._run_migrations(conn)
        conn.execute("DROP INDEX idx_polls_post")
        conn.commit()

        dbmod._run_migrations(conn)

        assert _scalar(conn, "SELECT COUNT(*) FROM sqlite_master WHERE name = 'idx_polls_post'") == 0

        conn.execute("DELETE FROM _migrations WHERE version = 26")
        conn.commit()
        dbmod._run_migrations(conn)

        assert _scalar(conn, "SELECT COUNT(*) FROM sqlite_master WHERE name = 'idx_polls_post'") == 1
    finally:
        conn.close()


def test_a_recorded_version_the_registry_does_not_know_is_ignored(tmp_path):
    conn = _schema_only(tmp_path)
    try:
        dbmod._run_migrations(conn)
        conn.execute("INSERT INTO _migrations (version) VALUES (999)")
        conn.execute("INSERT INTO _migrations (version) VALUES (0)")
        conn.execute("INSERT INTO _migrations (version) VALUES (-1)")
        conn.commit()

        dbmod._run_migrations(conn)

        assert {999, 0, -1} <= set(_versions(conn))
    finally:
        conn.close()


def test_the_pass_tolerates_a_row_factory_connection(tmp_path):
    # init_db passes a PLAIN connection, but nothing stops a
    # caller handing over a sqlite3.Row one — the positional
    # unpacking inside the migrations must still work
    conn = _schema_only(tmp_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "INSERT INTO push_tokens (id, user_id, token) VALUES ('t1', 'u1', 'ne-tikras-zetonas')"
        )
        conn.execute(
            "INSERT INTO push_tokens (id, user_id, token)"
            " VALUES ('t2', 'u1', 'ExponentPushToken[aaaaaaaaaa]')"
        )
        conn.commit()

        dbmod._run_migrations(conn)

        assert [r[0] for r in conn.execute("SELECT id FROM push_tokens")] == ["t2"]
    finally:
        conn.close()




# -----------------------------------------------------------
# _run_migrations — a migration that fails
# -----------------------------------------------------------

def test_a_failing_migration_leaves_its_own_version_unrecorded(tmp_path, monkeypatch, db_log):
    conn = _schema_only(tmp_path)
    try:
        monkeypatch.setattr(dbmod, "_migration_v13_hash_session_tokens",
                            _raiser(sqlite3.OperationalError("disk I/O error")))

        with pytest.raises(sqlite3.OperationalError):
            dbmod._run_migrations(conn)
        monkeypatch.undo()

        assert 13 not in _versions(conn), "a migration that raised must be retried next boot"
    finally:
        conn.close()


def test_a_failing_migration_keeps_the_versions_before_it(tmp_path, monkeypatch):
    conn = _schema_only(tmp_path)
    try:
        monkeypatch.setattr(dbmod, "_migration_v13_hash_session_tokens",
                            _raiser(sqlite3.OperationalError("disk I/O error")))
        with pytest.raises(sqlite3.OperationalError):
            dbmod._run_migrations(conn)
        monkeypatch.undo()

        assert _versions(conn) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
    finally:
        conn.close()


def test_a_failing_migration_stops_every_version_after_it(tmp_path, monkeypatch):
    conn = _schema_only(tmp_path)
    try:
        monkeypatch.setattr(dbmod, "_migration_v13_hash_session_tokens",
                            _raiser(sqlite3.OperationalError("disk I/O error")))
        with pytest.raises(sqlite3.OperationalError):
            dbmod._run_migrations(conn)
        monkeypatch.undo()

        assert max(_versions(conn)) < 13
    finally:
        conn.close()


def test_the_versions_before_a_failure_are_committed_not_merely_written(tmp_path, monkeypatch):
    path = str(tmp_path / "half-migrated.db")
    conn = sqlite3.connect(path)
    conn.executescript(dbmod._SCHEMA)
    conn.commit()
    try:
        monkeypatch.setattr(dbmod, "_migration_v13_hash_session_tokens",
                            _raiser(sqlite3.OperationalError("disk I/O error")))
        with pytest.raises(sqlite3.OperationalError):
            dbmod._run_migrations(conn)
        monkeypatch.undo()
    finally:
        conn.close()

    reopened = sqlite3.connect(path)
    try:
        assert _versions(reopened) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
    finally:
        reopened.close()


def test_the_failure_line_names_the_version_and_its_label(tmp_path, monkeypatch, db_log):
    conn = _schema_only(tmp_path)
    try:
        monkeypatch.setattr(dbmod, "_migration_v13_hash_session_tokens",
                            _raiser(sqlite3.OperationalError("disk I/O error")))
        with pytest.raises(sqlite3.OperationalError):
            dbmod._run_migrations(conn)
        monkeypatch.undo()
    finally:
        conn.close()

    critical = [r.getMessage() for r in db_log.records if r.levelno == logging.CRITICAL]
    assert critical, "a failing migration must log one CRITICAL line for the restart loop"
    assert "Data migration v13" in critical[0]
    assert "Store session tokens as sha256" in critical[0]
    assert "disk I/O error" in critical[0]


def test_the_next_pass_retries_the_migration_that_failed(tmp_path, monkeypatch):
    conn = _schema_only(tmp_path)
    try:
        monkeypatch.setattr(dbmod, "_migration_v13_hash_session_tokens",
                            _raiser(sqlite3.OperationalError("disk I/O error")))
        with pytest.raises(sqlite3.OperationalError):
            dbmod._run_migrations(conn)
        monkeypatch.undo()

        dbmod._run_migrations(conn)

        versions = _versions(conn)
        assert 13 in versions
        assert 55 in versions, "the pass must carry on past the version that had failed"
    finally:
        conn.close()


def test_a_migration_raising_something_other_than_a_database_error_still_aborts(tmp_path, monkeypatch, db_log):
    conn = _schema_only(tmp_path)
    try:
        monkeypatch.setattr(dbmod, "_migration_v14_reconcile_counters", _raiser(MemoryError("out of memory")))

        with pytest.raises(MemoryError):
            dbmod._run_migrations(conn)
        monkeypatch.undo()

        assert 14 not in _versions(conn)
        assert any(r.levelno == logging.CRITICAL for r in db_log.records)
    finally:
        conn.close()


def test_a_pass_over_a_database_without_the_schema_aborts_at_the_first_real_migration(tmp_path, db_log):
    # v1 and v2 are recorded no-ops, so they succeed on an
    # EMPTY file; v3's ALTER TABLE users then has nothing to
    # alter and the not-a-duplicate-column guard re-raises
    conn = _bare(tmp_path)
    try:
        with pytest.raises(sqlite3.OperationalError, match="users"):
            dbmod._run_migrations(conn)

        assert _versions(conn) == [1, 2]
    finally:
        conn.close()

    assert any("Data migration v3" in r.getMessage() and r.levelno == logging.CRITICAL
               for r in db_log.records)




# -----------------------------------------------------------
# _run_migrations — a full replay must not destroy data
# -----------------------------------------------------------
#
# v1 and v2 are retired no-ops precisely because a rebuilt
# _migrations table would otherwise re-escape, re-truncate and
# re-unescape live text. This drives that replay for real.
# -----------------------------------------------------------

def test_replaying_every_migration_over_live_rows_changes_nothing(tmp_path, no_admin_password):
    conn = _conn(_boot(tmp_path))
    try:
        author = _plant_user(conn, "autorius")
        conn.execute("UPDATE users SET avatar_url = '/api/uploads/avataras.jpg' WHERE id = ?", (author,))
        title = "Ilga antraštė " + "ą" * 400
        content = "Tekstas su &amp; ir <b>žymėmis</b> bei „kabutėmis“"
        conn.execute(
            "INSERT INTO news_posts (id, title, content, author_id, source, post_type)"
            " VALUES ('p1', ?, ?, ?, 'user', 'social')",
            (title, content, author),
        )
        conn.execute(
            "INSERT INTO push_tokens (id, user_id, token) VALUES ('t1', ?, 'ExponentPushToken[abcdefghij]')",
            (author,),
        )
        other = _plant_user(conn, "kitas")
        conn.execute("INSERT INTO conversations (id, type) VALUES ('c1', 'direct')")
        conn.executemany(
            "INSERT INTO conversation_participants (conversation_id, user_id) VALUES ('c1', ?)",
            [(author,), (other,)],
        )
        conn.commit()

        conn.execute("DELETE FROM _migrations")
        conn.commit()
        dbmod._run_migrations(conn)

        row = conn.execute("SELECT title, content FROM news_posts WHERE id = 'p1'").fetchone()
        assert row == (title, content), "a replay must not re-escape or truncate live text"
        assert _scalar(conn, "SELECT avatar_url FROM users WHERE id = ?", (author,)) == \
            "/api/uploads/avataras.jpg", "a replay must not wipe app-hosted avatars"
        assert _scalar(conn, "SELECT COUNT(*) FROM push_tokens WHERE id = 't1'") == 1
        assert _scalar(conn, "SELECT type FROM conversations WHERE id = 'c1'") == "direct", \
            "a two-member direct room is legitimate and must survive v49"
    finally:
        conn.close()


def test_a_replay_normalises_a_space_form_stamp_the_defaults_wrote(tmp_path, no_admin_password):
    conn = _conn(_boot(tmp_path))
    try:
        conn.execute("UPDATE users SET created_at = '2026-08-29 10:00:00' WHERE username = 'admin'")
        conn.execute("DELETE FROM _migrations WHERE version = 17")
        conn.commit()

        dbmod._run_migrations(conn)

        assert _scalar(conn, "SELECT created_at FROM users WHERE username = 'admin'") == \
            "2026-08-29T10:00:00"
    finally:
        conn.close()




# -----------------------------------------------------------
# init_db — a fresh file
# -----------------------------------------------------------

def test_a_fresh_boot_returns_nothing_and_pins_the_path(tmp_path, no_admin_password):
    dbmod._db_path = None
    path = str(tmp_path / "knfapp.db")

    assert init_db(path) is None
    assert dbmod._db_path == path


def test_a_fresh_boot_seeds_the_admin_and_the_code(tmp_path, no_admin_password):
    conn = _conn(_boot(tmp_path))
    try:
        assert _scalar(conn, "SELECT COUNT(*) FROM users WHERE role = 'admin'") == 1
        assert _CODE_SHAPE.match(_scalar(conn, "SELECT code FROM invitation_codes"))
    finally:
        conn.close()


def test_the_seeded_rows_carry_t_form_timestamps_after_a_full_boot(tmp_path, no_admin_password):
    # _seed_defaults lets the datetime('now') DEFAULTs fire,
    # which write space-form text; v17 runs later in the SAME
    # boot and normalises them
    conn = _conn(_boot(tmp_path))
    try:
        for stamp in conn.execute(
            "SELECT created_at FROM users UNION ALL SELECT created_at FROM invitation_codes"
        ).fetchall():
            assert " " not in stamp[0], stamp[0]
            assert "T" in stamp[0], stamp[0]
    finally:
        conn.close()


def test_a_boot_leaves_no_foreign_key_violation_behind(tmp_path, no_admin_password):
    conn = _conn(_boot(tmp_path))
    try:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_a_pathlib_path_boots_the_same_way(tmp_path, no_admin_password):
    path = tmp_path / "pathlib.db"

    init_db(path)

    assert isinstance(dbmod._db_path, pathlib.Path)
    conn = _conn(str(path))
    try:
        assert _scalar(conn, "SELECT COUNT(*) FROM users") == 1
    finally:
        conn.close()




# -----------------------------------------------------------
# init_db — an existing file
# -----------------------------------------------------------

def test_a_second_boot_replays_no_migration(tmp_path, no_admin_password):
    path = _boot(tmp_path)
    conn = _conn(path)
    before = conn.execute("SELECT version, applied_at FROM _migrations ORDER BY version").fetchall()
    conn.close()

    init_db(path)

    conn = _conn(path)
    try:
        assert conn.execute("SELECT version, applied_at FROM _migrations ORDER BY version").fetchall() == before
    finally:
        conn.close()


def test_a_second_boot_keeps_the_admin_password_and_the_code(tmp_path, no_admin_password):
    path = _boot(tmp_path)
    conn = _conn(path)
    before = (_scalar(conn, "SELECT password_hash FROM users WHERE username = 'admin'"),
              _scalar(conn, "SELECT code FROM invitation_codes"))
    conn.close()

    init_db(path)
    init_db(path)

    conn = _conn(path)
    try:
        assert (_scalar(conn, "SELECT password_hash FROM users WHERE username = 'admin'"),
                _scalar(conn, "SELECT code FROM invitation_codes")) == before
        assert _scalar(conn, "SELECT COUNT(*) FROM users") == 1
    finally:
        conn.close()


def test_an_existing_file_with_zero_users_is_never_re_seeded(tmp_path, no_admin_password, db_log):
    path = str(tmp_path / "wrong-volume.db")
    open(path, "wb").close()

    init_db(path)

    conn = _conn(path)
    try:
        assert _scalar(conn, "SELECT COUNT(*) FROM users") == 0
        assert _scalar(conn, "SELECT COUNT(*) FROM invitation_codes") == 0
        assert _versions(conn), "the migrations must still run on a wrongly-pointed file"
    finally:
        conn.close()

    warnings = _messages(db_log, "refusing to re-seed")
    assert len(warnings) == 1
    assert path in warnings[0], "the warning must name the path so the deploy is debuggable"


def test_the_zero_user_refusal_is_re_warned_on_every_boot(tmp_path, no_admin_password, db_log):
    # Nothing remembers the refusal, so the operator keeps
    # seeing it until DB_PATH is fixed
    path = str(tmp_path / "wrong-volume.db")
    open(path, "wb").close()

    init_db(path)
    init_db(path)

    assert len(_messages(db_log, "refusing to re-seed")) == 2


def test_a_bare_filename_boots_in_the_working_directory(tmp_path, monkeypatch, no_admin_password):
    # create_app tolerates a DB_PATH with no directory part;
    # init_db has to as well
    monkeypatch.chdir(tmp_path)

    init_db("knfapp.db")

    assert os.path.exists(tmp_path / "knfapp.db")
    conn = _conn(str(tmp_path / "knfapp.db"))
    try:
        assert _scalar(conn, "SELECT COUNT(*) FROM users WHERE username = 'admin'") == 1
    finally:
        conn.close()


def test_a_boot_after_a_failure_that_had_already_seeded_plants_no_second_admin(
    tmp_path, no_admin_password, monkeypatch,
):
    path = str(tmp_path / "half-booted.db")
    monkeypatch.setattr(dbmod, "_run_migrations", _raiser(MemoryError("out of memory")))
    with pytest.raises(MemoryError):
        init_db(path)
    monkeypatch.undo()

    conn = _conn(path)
    seeded_hash = _scalar(conn, "SELECT password_hash FROM users WHERE username = 'admin'")
    conn.close()

    init_db(path)

    conn = _conn(path)
    try:
        assert _scalar(conn, "SELECT COUNT(*) FROM users") == 1
        assert _scalar(conn, "SELECT password_hash FROM users WHERE username = 'admin'") == seeded_hash
        assert _versions(conn), "the retry boot must finish the migrations the first one aborted"
    finally:
        conn.close()


def test_a_boot_over_populated_rows_leaves_them_alone(tmp_path, no_admin_password):
    path = _boot(tmp_path)
    conn = _conn(path)
    _plant_user(conn, "jonas")
    conn.execute("INSERT INTO news_posts (id, title, content) VALUES ('p1', 'Naujiena', 'Turinys')")
    conn.commit()
    conn.close()

    init_db(path)

    conn = _conn(path)
    try:
        assert _scalar(conn, "SELECT COUNT(*) FROM users") == 2
        assert _scalar(conn, "SELECT content FROM news_posts WHERE id = 'p1'") == "Turinys"
    finally:
        conn.close()




# -----------------------------------------------------------
# init_db — the boot sweep and the foreign-key audit
# -----------------------------------------------------------

def test_the_boot_sweeps_sessions_that_expired_while_the_container_was_down(tmp_path, no_admin_password):
    path = _boot(tmp_path)
    conn = _conn(path)
    user_id = _plant_user(conn)
    _plant_session(conn, user_id, "dead",
                   (datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
    _plant_session(conn, user_id, "live",
                   (datetime.now(timezone.utc) + timedelta(days=90)).isoformat())
    conn.commit()
    conn.close()

    with time_machine.travel(datetime.now(timezone.utc) + timedelta(days=30), tick=False):
        init_db(path)

    conn = _conn(path)
    try:
        assert [r[0] for r in conn.execute("SELECT id FROM sessions")] == ["live"]
    finally:
        conn.close()


def test_the_boot_sweep_is_committed(tmp_path, no_admin_password):
    path = _boot(tmp_path)
    conn = _conn(path)
    user_id = _plant_user(conn)
    _plant_session(conn, user_id, "dead", "2000-01-01T00:00:00+00:00")
    conn.commit()
    conn.close()

    init_db(path)

    conn = _conn(path)
    try:
        assert _scalar(conn, "SELECT COUNT(*) FROM sessions") == 0
    finally:
        conn.close()


def test_a_boot_with_nothing_expired_keeps_every_session(tmp_path, no_admin_password):
    path = _boot(tmp_path)
    conn = _conn(path)
    user_id = _plant_user(conn)
    _plant_session(conn, user_id, "live", (datetime.now(timezone.utc) + timedelta(days=7)).isoformat())
    conn.commit()
    conn.close()

    init_db(path)

    conn = _conn(path)
    try:
        assert _scalar(conn, "SELECT COUNT(*) FROM sessions") == 1
    finally:
        conn.close()


def test_every_legacy_orphan_is_reported_one_line_each(tmp_path, no_admin_password, db_log):
    path = _boot(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("INSERT INTO sessions (id, user_id, token, expires_at)"
                 " VALUES ('orphan', 'vaiduoklis', 'tok', '2099-01-01T00:00:00+00:00')")
    conn.execute("INSERT INTO news_comments (id, post_id, user_id, text)"
                 " VALUES ('cmt', 'nera-irasu', 'vaiduoklis', 'labas')")
    conn.commit()
    conn.close()

    init_db(path)

    violations = _messages(db_log, "Foreign-key violation")
    assert len(violations) >= 2
    assert any("sessions" in line for line in violations)
    assert any("news_comments" in line for line in violations)


def test_the_foreign_key_audit_runs_after_the_migrations(tmp_path, no_admin_password, db_log):
    path = _boot(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("INSERT INTO sessions (id, user_id, token, expires_at)"
                 " VALUES ('orphan', 'vaiduoklis', 'tok', '2099-01-01T00:00:00+00:00')")
    conn.execute("DELETE FROM _migrations WHERE version = 55")
    conn.commit()
    conn.close()

    db_log.clear()
    init_db(path)

    order = [r.getMessage() for r in db_log.records]
    migration_line = max(i for i, m in enumerate(order) if "Data migration v55" in m)
    audit_line = min(i for i, m in enumerate(order) if "Foreign-key violation" in m)
    assert migration_line < audit_line, "the audit must see what the migrations left behind"


def test_a_clean_database_logs_no_foreign_key_violation(tmp_path, no_admin_password, db_log):
    _boot(tmp_path)
    assert _messages(db_log, "Foreign-key violation") == []




# -----------------------------------------------------------
# init_db — the lock ladder
# -----------------------------------------------------------
#
# _CountingConnect stands in for sqlite3.connect and fails the
# first `failures` attempts, so the retry ladder runs without a
# second process actually holding the file. _FlakyStep does the
# same for a failure raised AFTER the connection opened.
# -----------------------------------------------------------

class _CountingConnect:

    def __init__(self, failures, error):
        self.failures = failures
        self.error = error
        self.calls = 0
        self.opened = []
        self.real = sqlite3.connect

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        conn = self.real(*args, **kwargs)
        self.opened.append(conn)
        return conn


class _FlakyStep:

    def __init__(self, failures, error, real):
        self.failures = failures
        self.error = error
        self.real = real
        self.calls = 0

    def __call__(self, conn):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return self.real(conn)


def test_the_ladder_retries_a_locked_database_and_finally_boots(tmp_path, no_admin_password, no_sleeping, monkeypatch):
    connect = _CountingConnect(4, sqlite3.OperationalError("database is locked"))
    monkeypatch.setattr(dbmod.sqlite3, "connect", connect)

    path = str(tmp_path / "locked.db")
    init_db(path)
    monkeypatch.undo()

    assert connect.calls == 5, "the fifth attempt is still allowed to succeed"
    assert no_sleeping == [3, 6, 9, 12], "the ladder is attempt*3 seconds"
    conn = _conn(path)
    try:
        assert _scalar(conn, "SELECT COUNT(*) FROM users") == 1
    finally:
        conn.close()


def test_the_ladder_gives_up_after_exactly_five_attempts(tmp_path, no_sleeping, monkeypatch, db_log):
    connect = _CountingConnect(99, sqlite3.OperationalError("database is locked"))
    monkeypatch.setattr(dbmod.sqlite3, "connect", connect)

    with pytest.raises(sqlite3.OperationalError):
        init_db(str(tmp_path / "wedged.db"))
    monkeypatch.undo()

    assert connect.calls == 5
    assert no_sleeping == [3, 6, 9, 12], "the fifth failure must not sleep before giving up"
    assert len(_messages(db_log, "locked during init")) == 4
    assert any(r.levelno == logging.CRITICAL and "init FAILED" in r.getMessage() for r in db_log.records)


@pytest.mark.parametrize("message", [
    "database is locked",
    "DATABASE IS LOCKED",
    "Database Is Locked",
    "sqlite3: database is locked (5)",
])
def test_the_lock_phrase_is_matched_case_insensitively(tmp_path, no_admin_password, no_sleeping,
                                                       monkeypatch, message):
    connect = _CountingConnect(1, sqlite3.OperationalError(message))
    monkeypatch.setattr(dbmod.sqlite3, "connect", connect)

    init_db(str(tmp_path / "case.db"))
    monkeypatch.undo()

    assert no_sleeping == [3]


@pytest.mark.parametrize("message", [
    "database table is locked",
    "attempt to write a readonly database",
    "database or disk is full",
    "unable to open database file",
    "",
])
def test_any_other_operational_error_aborts_on_the_first_attempt(tmp_path, no_sleeping, monkeypatch,
                                                                 db_log, message):
    connect = _CountingConnect(99, sqlite3.OperationalError(message))
    monkeypatch.setattr(dbmod.sqlite3, "connect", connect)

    with pytest.raises(sqlite3.OperationalError):
        init_db(str(tmp_path / "abort.db"))
    monkeypatch.undo()

    assert connect.calls == 1, f"{message!r} must not be retried"
    assert no_sleeping == []
    assert any(r.levelno == logging.CRITICAL for r in db_log.records)


def test_a_lock_raised_after_the_connection_opened_is_retried_too(tmp_path, no_admin_password,
                                                                  no_sleeping, monkeypatch):
    flaky = _FlakyStep(1, sqlite3.OperationalError("database is locked"), dbmod._run_migrations)
    monkeypatch.setattr(dbmod, "_run_migrations", flaky)

    path = str(tmp_path / "late-lock.db")
    init_db(path)
    monkeypatch.undo()

    assert flaky.calls == 2
    assert no_sleeping == [3]
    conn = _conn(path)
    try:
        assert _versions(conn), "the retry attempt has to finish the migrations"
        assert _scalar(conn, "SELECT COUNT(*) FROM users") == 1, "and must not seed a second admin"
    finally:
        conn.close()


def test_every_attempt_closes_its_connection(tmp_path, no_sleeping, monkeypatch):
    connect = _CountingConnect(0, None)
    monkeypatch.setattr(dbmod.sqlite3, "connect", connect)
    monkeypatch.setattr(dbmod, "_run_migrations", _raiser(sqlite3.OperationalError("database is locked")))

    with pytest.raises(sqlite3.OperationalError):
        init_db(str(tmp_path / "leaky.db"))
    monkeypatch.undo()

    assert len(connect.opened) == 5
    for conn in connect.opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


def test_the_backoff_sleeps_before_the_attempts_connection_is_closed(tmp_path, monkeypatch):
    # Pinning the order the code has: the except arm sleeps
    # first and the finally closes afterwards
    connect = _CountingConnect(0, None)
    monkeypatch.setattr(dbmod.sqlite3, "connect", connect)
    monkeypatch.setattr(dbmod, "_run_migrations", _raiser(sqlite3.OperationalError("database is locked")))

    still_open = []

    def _watching_sleep(_seconds):
        try:
            connect.opened[-1].execute("SELECT 1")
            still_open.append(True)
        except sqlite3.ProgrammingError:
            still_open.append(False)

    monkeypatch.setattr(dbmod.time, "sleep", _watching_sleep)

    with pytest.raises(sqlite3.OperationalError):
        init_db(str(tmp_path / "order.db"))
    monkeypatch.undo()

    assert still_open == [True, True, True, True]




# -----------------------------------------------------------
# init_db — everything else that can go wrong
# -----------------------------------------------------------

def test_a_missing_parent_directory_aborts_the_boot(tmp_path, no_sleeping, db_log):
    with pytest.raises(sqlite3.OperationalError):
        init_db(str(tmp_path / "nera" / "knfapp.db"))

    assert no_sleeping == []
    assert any(r.levelno == logging.CRITICAL and "init FAILED" in r.getMessage() for r in db_log.records)


def test_a_directory_in_place_of_the_database_aborts_the_boot(tmp_path, db_log):
    with pytest.raises(sqlite3.OperationalError):
        init_db(str(tmp_path))

    assert any(r.levelno == logging.CRITICAL for r in db_log.records)


def test_a_file_that_is_not_a_database_aborts_the_boot(tmp_path, db_log):
    path = str(tmp_path / "sugadinta.db")
    with open(path, "wb") as handle:
        handle.write(b"tai tikrai ne sqlite failas" * 64)

    with pytest.raises(sqlite3.DatabaseError):
        init_db(path)

    assert any(r.levelno == logging.CRITICAL and "init FAILED" in r.getMessage() for r in db_log.records)


def test_a_none_path_raises_before_the_boot_is_even_attempted(db_log):
    with pytest.raises(TypeError):
        init_db(None)

    # The global is assigned FIRST, so a failed boot still
    # repoints get_db — at None, which get_db then refuses
    assert dbmod._db_path is None
    assert not _messages(db_log, "init FAILED"), "the failure happens before the try block"
    with pytest.raises(RuntimeError, match="init_db"):
        get_db()


def test_an_integer_path_is_reported_as_a_boot_failure(db_log):
    with pytest.raises(TypeError):
        init_db(999999)

    assert any(r.levelno == logging.CRITICAL and "init FAILED" in r.getMessage() for r in db_log.records)


def test_the_path_is_pinned_even_when_the_boot_fails(tmp_path):
    doomed = str(tmp_path / "nera" / "knfapp.db")

    with pytest.raises(sqlite3.OperationalError):
        init_db(doomed)

    assert dbmod._db_path == doomed
    with pytest.raises(sqlite3.OperationalError):
        get_db()


def test_a_keyboard_interrupt_is_not_swallowed_by_the_error_handling(tmp_path, monkeypatch, db_log):
    connect = _CountingConnect(0, None)
    monkeypatch.setattr(dbmod.sqlite3, "connect", connect)
    monkeypatch.setattr(dbmod, "_run_migrations", _raiser(KeyboardInterrupt()))

    with pytest.raises(KeyboardInterrupt):
        init_db(str(tmp_path / "interrupted.db"))
    monkeypatch.undo()

    assert _messages(db_log, "init FAILED") == [], \
        "except Exception must not dress a shutdown signal up as a database failure"
    assert len(connect.opened) == 1
    # the finally arm still closes the attempt's connection
    with pytest.raises(sqlite3.ProgrammingError):
        connect.opened[0].execute("SELECT 1")


def test_an_empty_database_path_is_refused(no_admin_password):
    # `DB_PATH=` in a compose file arrives as "", and
    # sqlite3.connect("") would open a PRIVATE TEMPORARY
    # database that reports a perfect boot and vanishes on
    # close, leaving every request to 500 on "no such table"
    with pytest.raises((ValueError, RuntimeError, sqlite3.Error)):
        init_db("")


def test_a_whitespace_database_path_is_refused_too(no_admin_password):
    with pytest.raises((ValueError, RuntimeError, sqlite3.Error)):
        init_db("   ")


def test_a_refused_path_never_repoints_get_db(booted, no_admin_password):
    with pytest.raises((ValueError, RuntimeError, sqlite3.Error)):
        init_db("")

    # The refusal lands BEFORE _db_path is pinned, so get_db()
    # still serves the database that booted successfully
    assert dbmod._db_path == booted
    conn = get_db()
    try:
        assert _scalar(conn, "SELECT COUNT(*) FROM users WHERE username = 'admin'") == 1
    finally:
        conn.close()




# -----------------------------------------------------------
# The wiring create_app() depends on
# -----------------------------------------------------------

def test_create_app_pins_get_db_at_the_configured_database(app):
    assert dbmod._db_path == app.config["DB_PATH"]

    conn = get_db()
    try:
        assert _scalar(conn, "SELECT COUNT(*) FROM users WHERE username = 'admin'") == 1
    finally:
        conn.close()


def test_the_seeded_admin_can_log_in_with_the_env_password(client):
    response = client.post("/api/auth/login", json={
        "username": "admin",
        "password": "test-admin-password",
    })

    assert response.status_code == 200, response.get_json()
    assert response.get_json()["user"]["role"] == "admin"


def test_the_seeded_bootstrap_code_registers_a_student(client, seeded_code, db):
    assert _CODE_SHAPE.match(seeded_code), seeded_code

    response = client.post("/api/auth/register", json={
        "username": "kristina",
        "password": "Kaunas-Fakultetas-42",
        "display_name": "Kristina",
        "email": "kristina@knf.vu.lt",
        "invitation_code": seeded_code,
    }, environ_base={"REMOTE_ADDR": "10.31.4.9"})

    assert response.status_code == 201, response.get_json()
    assert db.execute("SELECT invited FROM users WHERE username = 'kristina'").fetchone()["invited"] == 1


def test_the_login_session_the_app_mints_survives_the_boot_sweep(app, client, actor, db):
    _, headers = actor
    assert client.get("/api/auth/me", headers=headers).status_code == 200

    init_db(app.config["DB_PATH"])

    assert client.get("/api/auth/me", headers=headers).status_code == 200, \
        "a live session must not be swept by a re-boot"
