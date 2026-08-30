# -----------------------------------------------------------
#  [*] Tests — database bootstrap, schema, seeding, migrations
#
#  Everything this suite stands on is built by init_db(), so
#  this module tests the builder itself. What it proves about
#  app/database/__init__.py:
#
#    - a brand-new file gets EVERY table and index the app
#      queries, with the CHECK constraints and the ON DELETE
#      actions actually enforced (not merely declared)
#    - _seed_defaults plants exactly ONE admin, whose password
#      is a fresh random secret when ADMIN_PASSWORD is unset —
#      never a literal, never repeated between databases — and
#      is printed exactly once
#    - the bootstrap invitation code is generated per database
#      ("KNF-" + 16 hex), owned by that admin, 100 uses, 365
#      days — and is NOT the leaked WELCOME-KNF-2026 literal
#    - re-running init_db is a no-op: no second admin, no
#      second code, no rotated password, no replayed migration
#    - seeding is refused on an existing file with zero users
#      (wrong DB_PATH / wiped volume) — it warns instead
#    - get_db refuses before init, and every connection it
#      hands out carries WAL, foreign_keys=ON, synchronous
#      NORMAL and a 30 s busy timeout
#    - utc_now_iso is T-form aware UTC that sorts in time
#      order (the whole reason migration v17 exists)
#    - a locked database is retried with backoff, any other
#      error aborts the boot loudly
#    - each migration still does its job when replayed against
#      the legacy data it was written for, and stays
#      idempotent on the run after that
# -----------------------------------------------------------

import hashlib
import logging
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import pytest
import time_machine

from app import database as dbmod
from app.database import get_db, init_db, sweep_expired_sessions, utc_now_iso


# Every table the app queries somewhere. A subset check, so a
# NEW table does not fail the suite — a REMOVED one does
_EXPECTED_TABLES = {
    "users", "invitation_codes", "sessions",
    "news_posts", "news_likes", "news_comments",
    "polls", "poll_options", "poll_votes",
    "deleted_source_urls", "schedule_lessons", "scraper_runs",
    "conversations", "conversation_participants", "messages",
    "message_reactions", "message_reads",
    "friendships", "friend_requests",
    "faculty_info", "push_tokens", "notification_channels",
    "uploads", "admin_audit",
    "_migrations", "messages_fts",
}

# _SCHEMA's indexes plus the migration-only ones (v10, v15,
# v18–v21, v26, v46, v55)
_EXPECTED_INDEXES = {
    "idx_news_posts_published", "idx_news_posts_source", "idx_news_comments_post",
    "idx_messages_conversation", "idx_conversation_participants_user",
    "idx_message_reads_user", "idx_friendships_friend",
    "idx_friend_requests_to", "idx_friend_requests_from",
    "idx_push_tokens_user", "idx_push_tokens_token",
    "idx_uploads_user", "idx_admin_audit_created", "idx_admin_audit_actor",
    "idx_messages_client_msg",
    "idx_sessions_user", "idx_news_likes_post", "idx_news_comments_user",
    "idx_messages_sender", "idx_messages_reply_to", "idx_news_posts_author",
    "idx_message_reactions_user", "idx_conversations_created_by",
    "idx_invitation_codes_created_by",
    "idx_schedule_lessons_natural", "idx_schedule_lessons_filter",
    "idx_scraper_runs_started", "idx_friend_requests_pending",
    "idx_polls_post", "idx_poll_options_poll",
    "idx_push_tokens_active", "idx_news_posts_author_source",
}

# The six v22 dropped as pure write amplification — they must
# not come back on a fresh boot
_RETIRED_INDEXES = {
    "idx_sessions_token", "idx_invitation_codes_code",
    "idx_message_reactions_message", "idx_message_reads_message",
    "idx_friendships_user", "idx_notification_channels_user",
}

# Passwords a seeded admin must NEVER have. The first is the
# literal that shipped in this repo's own history
_FORBIDDEN_ADMIN_PASSWORDS = (
    "admin123", "admin", "password", "changeme", "knfapp",
    "slaptazodis", "WELCOME-KNF-2026", "admin@knf.vu.lt",
    "Administratorius", "",
)




# -----------------------------------------------------------
# _keep_module_db_path
# -----------------------------------------------------------
#
# init_db() pins a MODULE-level _db_path that get_db() reads,
# so a test booting its own database repoints the whole
# process. Restoring it keeps that invisible to the next test.
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _keep_module_db_path():
    saved = dbmod._db_path
    yield
    dbmod._db_path = saved




# -----------------------------------------------------------
# no_admin_password
# -----------------------------------------------------------
#
# The production first-boot case the FOCUS of this module
# cares about: no ADMIN_PASSWORD in the environment, so
# _seed_defaults must generate one.
# -----------------------------------------------------------

@pytest.fixture
def no_admin_password(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)




# -----------------------------------------------------------
# Local helpers
# -----------------------------------------------------------
#
# _boot   — init_db() on a brand-new file, returns its path
# _conn   — a connection shaped like init_db's own: no
#           row_factory (rows are TUPLES, as the migrations
#           see them) and foreign_keys ON
# _replay — forget one migration's version row and run the
#           registry again, so exactly that migration executes
#           a second time against whatever the test planted
# -----------------------------------------------------------

def _boot(tmp_path, name="knfapp.db"):
    path = str(tmp_path / name)
    init_db(path)
    return path


def _conn(path):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _replay(conn, version):
    conn.execute("DELETE FROM _migrations WHERE version = ?", (version,))
    conn.commit()
    dbmod._run_migrations(conn)


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _indexes(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'")}


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _on_delete(conn, table, column):
    for fk in conn.execute(f"PRAGMA foreign_key_list({table})"):
        if fk[3] == column:
            return fk[6]
    return None


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


def _plant_conversation(conn, kind="direct", title=None, members=()):
    conv_id = str(uuid.uuid4())
    conn.execute("INSERT INTO conversations (id, type, title) VALUES (?, ?, ?)", (conv_id, kind, title))
    for member in members:
        conn.execute(
            "INSERT INTO conversation_participants (conversation_id, user_id) VALUES (?, ?)",
            (conv_id, member),
        )
    return conv_id


def _plant_poll(conn):
    post_id, poll_id, option_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    conn.execute("INSERT INTO news_posts (id, title, content) VALUES (?, 'T', 'C')", (post_id,))
    conn.execute("INSERT INTO polls (id, post_id, title) VALUES (?, ?, 'Klausimas')", (poll_id, post_id))
    conn.execute("INSERT INTO poll_options (id, poll_id, text) VALUES (?, ?, 'Taip')", (option_id, poll_id))
    return post_id, poll_id, option_id


# The one and only print of a generated admin password
_GENERATED_PW_RE = re.compile(r"generated password: (\S+) —")




# -----------------------------------------------------------
# The schema a fresh database gets
# -----------------------------------------------------------

def test_a_fresh_database_gets_every_table(tmp_path):
    conn = _conn(_boot(tmp_path))
    missing = _EXPECTED_TABLES - _tables(conn)
    conn.close()
    assert missing == set(), f"init_db left these tables uncreated: {sorted(missing)}"


def test_a_fresh_database_gets_every_index(tmp_path):
    conn = _conn(_boot(tmp_path))
    missing = _EXPECTED_INDEXES - _indexes(conn)
    conn.close()
    assert missing == set(), f"init_db left these indexes uncreated: {sorted(missing)}"


def test_the_indexes_v22_dropped_do_not_come_back(tmp_path):
    conn = _conn(_boot(tmp_path))
    resurrected = _RETIRED_INDEXES & _indexes(conn)
    conn.close()
    assert resurrected == set()


def test_users_table_carries_every_column_the_app_writes(tmp_path):
    conn = _conn(_boot(tmp_path))
    columns = _columns(conn, "users")
    conn.close()
    assert {
        "id", "username", "email", "display_name", "password_hash", "role",
        "invited", "avatar_url", "student_number", "study_group",
        "study_program", "active", "created_at", "updated_at",
    } <= columns


def test_messages_table_carries_the_migration_added_columns(tmp_path):
    conn = _conn(_boot(tmp_path))
    columns = _columns(conn, "messages")
    conn.close()
    assert {"reply_to_id", "deleted_at", "client_msg_id"} <= columns


def test_push_tokens_carries_the_language_column(tmp_path):
    conn = _conn(_boot(tmp_path))
    columns = _columns(conn, "push_tokens")
    conn.close()
    assert "language" in columns


def test_the_role_check_constraint_refuses_an_unknown_role(tmp_path):
    conn = _conn(_boot(tmp_path))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO users (id, username, email, display_name, password_hash, role)"
            " VALUES ('r1', 'root', 'root@knf.vu.lt', 'Root', 'x', 'superuser')"
        )
    conn.close()


def test_day_of_week_accepts_only_zero_through_six(tmp_path):
    conn = _conn(_boot(tmp_path))

    for day in (0, 6):
        conn.execute(
            "INSERT INTO schedule_lessons (id, title, time_start, time_end, day_of_week)"
            " VALUES (?, 'Paskaita', '08:00', '09:30', ?)",
            (f"ok-{day}", day),
        )

    for day in (-1, 7):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO schedule_lessons (id, title, time_start, time_end, day_of_week)"
                " VALUES (?, 'Paskaita', '08:00', '09:30', ?)",
                (f"bad-{day}", day),
            )
    conn.close()


def test_notification_channels_refuses_an_unknown_channel(tmp_path):
    conn = _conn(_boot(tmp_path))
    user_id = _plant_user(conn)

    conn.execute("INSERT INTO notification_channels (user_id, channel) VALUES (?, 'news')", (user_id,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO notification_channels (user_id, channel) VALUES (?, 'sms')", (user_id,))
    conn.close()


def test_two_posts_cannot_share_a_source_url(tmp_path):
    conn = _conn(_boot(tmp_path))
    conn.execute("INSERT INTO news_posts (id, title, content, source_url) VALUES ('p1', 'T', 'C', 'https://knf.vu.lt/a')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO news_posts (id, title, content, source_url) VALUES ('p2', 'T', 'C', 'https://knf.vu.lt/a')")
    conn.close()


def test_deleting_a_user_cascades_to_their_sessions(tmp_path):
    conn = _conn(_boot(tmp_path))
    user_id = _plant_user(conn)
    conn.execute(
        "INSERT INTO sessions (id, user_id, token, expires_at) VALUES ('s1', ?, 'tok', ?)",
        (user_id, (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()),
    )

    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    assert _scalar(conn, "SELECT COUNT(*) FROM sessions") == 0
    conn.close()


def test_deleting_an_author_keeps_their_posts_with_a_null_author(tmp_path):
    conn = _conn(_boot(tmp_path))
    user_id = _plant_user(conn)
    conn.execute(
        "INSERT INTO news_posts (id, title, content, author_id) VALUES ('p1', 'T', 'C', ?)",
        (user_id,),
    )

    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    assert _scalar(conn, "SELECT COUNT(*) FROM news_posts") == 1
    assert _scalar(conn, "SELECT author_id FROM news_posts WHERE id = 'p1'") is None
    conn.close()


def test_a_fresh_database_declares_the_v16_on_delete_actions(tmp_path):
    conn = _conn(_boot(tmp_path))
    assert _on_delete(conn, "invitation_codes", "created_by") == "SET NULL"
    assert _on_delete(conn, "news_posts", "author_id") == "SET NULL"
    assert _on_delete(conn, "conversations", "created_by") == "SET NULL"
    assert _on_delete(conn, "poll_votes", "user_id") == "CASCADE"
    conn.close()


def test_a_fresh_boot_leaves_no_foreign_key_violations(tmp_path):
    conn = _conn(_boot(tmp_path))
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()




# -----------------------------------------------------------
# _seed_defaults — the admin account
# -----------------------------------------------------------

def test_seeding_creates_exactly_one_admin(tmp_path, no_admin_password):
    conn = _conn(_boot(tmp_path))
    rows = conn.execute("SELECT username, email, display_name, role, invited, active FROM users").fetchall()
    conn.close()

    assert len(rows) == 1
    username, email, display_name, role, invited, active = rows[0]
    assert (username, email, role) == ("admin", "admin@knf.vu.lt", "admin")
    assert display_name == "Administratorius"
    assert invited == 1
    assert active == 1


def test_the_seeded_admin_password_is_random_when_the_env_is_unset(tmp_path, no_admin_password):
    hashes = []
    for name in ("one.db", "two.db"):
        conn = _conn(_boot(tmp_path, name))
        hashes.append(_scalar(conn, "SELECT password_hash FROM users WHERE username = 'admin'"))
        conn.close()

    assert hashes[0] != hashes[1], "two fresh databases share an admin password hash"
    for pw_hash in hashes:
        for literal in _FORBIDDEN_ADMIN_PASSWORDS:
            assert not bcrypt.checkpw(literal.encode(), pw_hash.encode()), \
                f"the seeded admin password is the literal {literal!r}"


def test_the_generated_admin_password_is_logged_once_and_actually_works(tmp_path, no_admin_password, caplog):
    caplog.set_level(logging.WARNING, logger="app.database")
    conn = _conn(_boot(tmp_path))
    pw_hash = _scalar(conn, "SELECT password_hash FROM users WHERE username = 'admin'")
    conn.close()

    first_boot = [r.getMessage() for r in caplog.records if "FIRST BOOT" in r.getMessage()]
    assert len(first_boot) == 1, "the generated secrets must be printed exactly once"

    match = _GENERATED_PW_RE.search(first_boot[0])
    assert match, f"the generated password is not readable from the log line: {first_boot[0]!r}"
    password = match.group(1)
    assert len(password) >= 16, "a generated admin password shorter than token_urlsafe(16)"
    assert bcrypt.checkpw(password.encode(), pw_hash.encode())


def test_an_env_supplied_admin_password_is_used_and_never_echoed(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ADMIN_PASSWORD", "Kauno-Fakultetas-2026")
    caplog.set_level(logging.WARNING, logger="app.database")

    conn = _conn(_boot(tmp_path))
    pw_hash = _scalar(conn, "SELECT password_hash FROM users WHERE username = 'admin'")
    conn.close()

    assert bcrypt.checkpw(b"Kauno-Fakultetas-2026", pw_hash.encode())
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "Kauno-Fakultetas-2026" not in logged, "the env-set admin password was echoed into the log"
    assert "ADMIN_PASSWORD" in logged


def test_an_empty_admin_password_env_falls_back_to_a_generated_secret(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    caplog.set_level(logging.WARNING, logger="app.database")

    conn = _conn(_boot(tmp_path))
    pw_hash = _scalar(conn, "SELECT password_hash FROM users WHERE username = 'admin'")
    conn.close()

    assert not bcrypt.checkpw(b"", pw_hash.encode())
    match = _GENERATED_PW_RE.search("\n".join(r.getMessage() for r in caplog.records))
    assert match, "an empty ADMIN_PASSWORD must generate one, not seed an empty password"
    assert bcrypt.checkpw(match.group(1).encode(), pw_hash.encode())




# -----------------------------------------------------------
# _seed_defaults — the bootstrap invitation code
# -----------------------------------------------------------

def test_the_bootstrap_invitation_code_has_the_generated_shape(tmp_path, no_admin_password):
    conn = _conn(_boot(tmp_path))
    code = _scalar(conn, "SELECT code FROM invitation_codes")
    conn.close()

    assert re.fullmatch(r"KNF-[0-9A-F]{16}", code), f"unexpected bootstrap code: {code!r}"


def test_the_bootstrap_code_is_not_the_leaked_literal(tmp_path, no_admin_password):
    conn = _conn(_boot(tmp_path))
    codes = [r[0] for r in conn.execute("SELECT code FROM invitation_codes")]
    conn.close()
    assert "WELCOME-KNF-2026" not in codes


def test_each_database_gets_its_own_bootstrap_code(tmp_path, no_admin_password):
    codes = []
    for name in ("one.db", "two.db"):
        conn = _conn(_boot(tmp_path, name))
        codes.append(_scalar(conn, "SELECT code FROM invitation_codes"))
        conn.close()
    assert codes[0] != codes[1]


def test_the_bootstrap_code_is_a_student_code_owned_by_the_admin(tmp_path, no_admin_password):
    conn = _conn(_boot(tmp_path))
    role, created_by, max_uses, use_count = conn.execute(
        "SELECT role, created_by, max_uses, use_count FROM invitation_codes").fetchone()
    admin_id = _scalar(conn, "SELECT id FROM users WHERE username = 'admin'")
    conn.close()

    assert role == "student", "the bootstrap code must never mint admins"
    assert created_by == admin_id
    assert (max_uses, use_count) == (100, 0)


def test_the_bootstrap_code_expires_a_year_after_seeding(tmp_path, no_admin_password):
    with time_machine.travel(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc), tick=False):
        conn = _conn(_boot(tmp_path))
        expires = _scalar(conn, "SELECT expires_at FROM invitation_codes")
        conn.close()

    assert expires == "2027-01-01T12:00:00+00:00"


def test_the_seeded_code_registers_an_invited_student(client, seeded_code, db):
    # Own client IP: the register rate-limit budget is a
    # process-global keyed on the address, and every other test
    # file in the suite registers from 127.0.0.1 too
    response = client.post("/api/auth/register", json={
        "username": "kristina",
        "password": "Kaunas-Fakultetas-42",
        "display_name": "Kristina",
        "email": "kristina@knf.vu.lt",
        "invitation_code": seeded_code,
    }, environ_base={"REMOTE_ADDR": "10.31.4.7"})

    assert response.status_code == 201, response.get_json()
    row = db.execute("SELECT role, invited FROM users WHERE username = 'kristina'").fetchone()
    assert (row["role"], row["invited"]) == ("student", 1)




# -----------------------------------------------------------
# Re-running init_db
# -----------------------------------------------------------

def test_a_second_boot_does_not_seed_a_second_admin(tmp_path, no_admin_password):
    path = _boot(tmp_path)
    conn = _conn(path)
    before = _scalar(conn, "SELECT password_hash FROM users WHERE username = 'admin'")
    code_before = _scalar(conn, "SELECT code FROM invitation_codes")
    conn.close()

    init_db(path)

    conn = _conn(path)
    assert _scalar(conn, "SELECT COUNT(*) FROM users WHERE role = 'admin'") == 1
    assert _scalar(conn, "SELECT COUNT(*) FROM invitation_codes") == 1
    assert _scalar(conn, "SELECT password_hash FROM users WHERE username = 'admin'") == before, \
        "a re-boot rotated the admin password"
    assert _scalar(conn, "SELECT code FROM invitation_codes") == code_before
    conn.close()


def test_the_schema_and_migrations_are_unchanged_by_a_second_boot(tmp_path, no_admin_password):
    path = _boot(tmp_path)
    conn = _conn(path)
    tables, indexes = _tables(conn), _indexes(conn)
    versions = [r[0] for r in conn.execute("SELECT version FROM _migrations ORDER BY version")]
    conn.close()

    init_db(path)
    init_db(path)

    conn = _conn(path)
    assert _tables(conn) == tables
    assert _indexes(conn) == indexes
    replayed = [r[0] for r in conn.execute("SELECT version FROM _migrations ORDER BY version")]
    conn.close()

    assert replayed == versions
    assert len(replayed) == len(set(replayed)), "a migration version was recorded twice"


def test_a_boot_over_a_populated_database_leaves_its_rows_alone(tmp_path, no_admin_password):
    path = _boot(tmp_path)
    conn = _conn(path)
    _plant_user(conn, "jonas")
    conn.execute("INSERT INTO news_posts (id, title, content) VALUES ('p1', 'Naujiena', 'Turinys')")
    conn.commit()
    conn.close()

    init_db(path)

    conn = _conn(path)
    assert _scalar(conn, "SELECT COUNT(*) FROM users") == 2
    assert _scalar(conn, "SELECT title FROM news_posts WHERE id = 'p1'") == "Naujiena"
    conn.close()


def test_an_existing_empty_file_is_never_re_seeded(tmp_path, no_admin_password, caplog):
    caplog.set_level(logging.WARNING, logger="app.database")
    path = str(tmp_path / "wrong-volume.db")
    open(path, "wb").close()

    init_db(path)

    conn = _conn(path)
    assert _scalar(conn, "SELECT COUNT(*) FROM users") == 0
    assert _scalar(conn, "SELECT COUNT(*) FROM invitation_codes") == 0
    conn.close()

    assert any("refusing to re-seed" in r.getMessage() for r in caplog.records), \
        "an existing file with zero users must warn instead of planting a new admin"


def test_a_wiped_users_table_on_an_existing_file_is_not_re_seeded(tmp_path, no_admin_password, caplog):
    caplog.set_level(logging.WARNING, logger="app.database")
    path = _boot(tmp_path)

    conn = _conn(path)
    conn.execute("DELETE FROM users")
    conn.commit()
    conn.close()

    init_db(path)

    conn = _conn(path)
    assert _scalar(conn, "SELECT COUNT(*) FROM users") == 0
    conn.close()
    assert any("refusing to re-seed" in r.getMessage() for r in caplog.records)




# -----------------------------------------------------------
# get_db
# -----------------------------------------------------------

def test_get_db_refuses_before_init_db_has_run():
    dbmod._db_path = None
    with pytest.raises(RuntimeError, match="init_db"):
        get_db()


def test_get_db_sets_wal_foreign_keys_and_the_busy_timeout(tmp_path):
    _boot(tmp_path)
    conn = get_db()
    try:
        assert _scalar(conn, "PRAGMA journal_mode") == "wal"
        assert _scalar(conn, "PRAGMA foreign_keys") == 1
        assert _scalar(conn, "PRAGMA synchronous") == 1
        assert _scalar(conn, "PRAGMA busy_timeout") == 30000
    finally:
        conn.close()


def test_get_db_hands_out_dict_style_rows(tmp_path, no_admin_password):
    _boot(tmp_path)
    conn = get_db()
    try:
        row = conn.execute("SELECT username, role FROM users WHERE username = 'admin'").fetchone()
        assert isinstance(row, sqlite3.Row)
        assert row["username"] == "admin"
        assert row["role"] == "admin"
    finally:
        conn.close()


def test_get_db_connections_enforce_foreign_keys(tmp_path):
    _boot(tmp_path)
    conn = get_db()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO sessions (id, user_id, token, expires_at)"
                " VALUES ('s1', 'no-such-user', 'tok', '2099-01-01T00:00:00+00:00')"
            )
    finally:
        conn.close()


def test_get_db_follows_the_path_the_last_init_db_registered(tmp_path, no_admin_password):
    _boot(tmp_path, "first.db")
    second = _boot(tmp_path, "second.db")

    conn = get_db()
    try:
        opened = conn.execute("PRAGMA database_list").fetchone()[2]
    finally:
        conn.close()

    assert opened == second




# -----------------------------------------------------------
# utc_now_iso
# -----------------------------------------------------------

def test_utc_now_iso_is_t_form_utc():
    with time_machine.travel(datetime(2026, 8, 29, 12, 34, 56, 789012, tzinfo=timezone.utc), tick=False):
        stamp = utc_now_iso()

    assert stamp == "2026-08-29T12:34:56.789012+00:00"
    assert " " not in stamp, "space-form timestamps sort wrong against T-form (see migration v17)"


def test_utc_now_iso_round_trips_as_an_aware_utc_datetime():
    parsed = datetime.fromisoformat(utc_now_iso())
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


def test_utc_now_iso_sorts_lexicographically_in_time_order():
    with time_machine.travel(datetime(2026, 8, 29, 9, 0, tzinfo=timezone.utc), tick=False):
        earlier = utc_now_iso()
    with time_machine.travel(datetime(2026, 8, 29, 21, 0, tzinfo=timezone.utc), tick=False):
        later = utc_now_iso()

    assert earlier < later
    # And the shape v17 had to remove: a LATER space-form value
    # still sorts before an EARLIER T-form one
    assert "2026-08-29 23:00:00" < earlier




# -----------------------------------------------------------
# sweep_expired_sessions
# -----------------------------------------------------------

def test_the_sweep_deletes_only_expired_sessions(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="app.database")
    conn = _conn(_boot(tmp_path))
    user_id = _plant_user(conn)
    conn.execute("INSERT INTO sessions (id, user_id, token, expires_at) VALUES ('live', ?, 'a', ?)",
                 (user_id, (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()))
    conn.execute("INSERT INTO sessions (id, user_id, token, expires_at) VALUES ('dead', ?, 'b', ?)",
                 (user_id, (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()))

    sweep_expired_sessions(conn)

    assert [r[0] for r in conn.execute("SELECT id FROM sessions")] == ["live"]
    conn.close()
    assert any("Swept 1 expired session row" in r.getMessage() for r in caplog.records)


def test_the_sweep_is_silent_when_nothing_expired(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="app.database")
    conn = _conn(_boot(tmp_path))
    caplog.clear()

    sweep_expired_sessions(conn)
    conn.close()

    assert not any("Swept" in r.getMessage() for r in caplog.records)


def test_booting_sweeps_the_sessions_that_expired_while_down(tmp_path):
    path = _boot(tmp_path)
    conn = _conn(path)
    user_id = _plant_user(conn)
    conn.execute("INSERT INTO sessions (id, user_id, token, expires_at) VALUES ('dead', ?, 'b', ?)",
                 (user_id, (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()))
    conn.commit()
    conn.close()

    init_db(path)

    conn = _conn(path)
    assert _scalar(conn, "SELECT COUNT(*) FROM sessions") == 0
    conn.close()




# -----------------------------------------------------------
# init_db resilience
# -----------------------------------------------------------
#
# _FlakyConnect stands in for sqlite3.connect and fails the
# first `failures` attempts, so the retry/backoff ladder runs
# without a second process actually holding a lock.
# -----------------------------------------------------------

class _FlakyConnect:

    def __init__(self, failures, error):
        self.failures = failures
        self.error = error
        self.calls = 0
        self.real = sqlite3.connect

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return self.real(*args, **kwargs)


@pytest.fixture
def no_sleeping(monkeypatch):
    slept = []
    monkeypatch.setattr(dbmod.time, "sleep", lambda seconds: slept.append(seconds))
    return slept


def test_a_locked_database_is_retried_until_it_opens(tmp_path, no_admin_password, no_sleeping, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="app.database")
    flaky = _FlakyConnect(2, sqlite3.OperationalError("database is locked"))
    monkeypatch.setattr(dbmod.sqlite3, "connect", flaky)

    path = str(tmp_path / "locked.db")
    init_db(path)

    monkeypatch.undo()
    assert no_sleeping == [3, 6], "the backoff ladder is attempt*3 seconds"
    conn = _conn(path)
    assert _scalar(conn, "SELECT COUNT(*) FROM users WHERE username = 'admin'") == 1
    conn.close()
    assert sum("locked during init" in r.getMessage() for r in caplog.records) == 2


def test_a_lock_that_never_clears_aborts_the_boot(tmp_path, no_sleeping, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="app.database")
    flaky = _FlakyConnect(99, sqlite3.OperationalError("database is locked"))
    monkeypatch.setattr(dbmod.sqlite3, "connect", flaky)

    with pytest.raises(sqlite3.OperationalError):
        init_db(str(tmp_path / "wedged.db"))

    monkeypatch.undo()
    assert flaky.calls == 5, "five attempts, no more"
    assert no_sleeping == [3, 6, 9, 12]
    assert any(r.levelno == logging.CRITICAL and "init FAILED" in r.getMessage() for r in caplog.records)


def test_a_non_lock_database_error_aborts_immediately(tmp_path, no_sleeping, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="app.database")
    flaky = _FlakyConnect(99, sqlite3.OperationalError("attempt to write a readonly database"))
    monkeypatch.setattr(dbmod.sqlite3, "connect", flaky)

    with pytest.raises(sqlite3.OperationalError):
        init_db(str(tmp_path / "readonly.db"))

    monkeypatch.undo()
    assert flaky.calls == 1, "a readonly/full database must not be retried"
    assert no_sleeping == []
    assert any(r.levelno == logging.CRITICAL for r in caplog.records)


def test_an_unexpected_failure_is_logged_critical_and_re_raised(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="app.database")
    flaky = _FlakyConnect(99, MemoryError("out of memory"))
    monkeypatch.setattr(dbmod.sqlite3, "connect", flaky)

    with pytest.raises(MemoryError):
        init_db(str(tmp_path / "doomed.db"))

    monkeypatch.undo()
    assert flaky.calls == 1
    assert any(r.levelno == logging.CRITICAL and "init FAILED" in r.getMessage() for r in caplog.records)


def test_init_db_registers_the_path_for_get_db(tmp_path):
    dbmod._db_path = None
    path = _boot(tmp_path)
    assert dbmod._db_path == path


def test_legacy_foreign_key_violations_are_reported_at_boot(tmp_path, caplog):
    path = _boot(tmp_path)

    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("INSERT INTO sessions (id, user_id, token, expires_at) VALUES ('orphan', 'ghost', 'tok', ?)",
                 ((datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),))
    conn.commit()
    conn.close()

    caplog.set_level(logging.WARNING, logger="app.database")
    init_db(path)

    violations = [r.getMessage() for r in caplog.records if "Foreign-key violation" in r.getMessage()]
    assert violations, "PRAGMA foreign_key_check findings must reach the boot log"
    assert "sessions" in violations[0]




# -----------------------------------------------------------
# _run_migrations bookkeeping
# -----------------------------------------------------------

def test_every_migration_the_registry_lists_is_recorded_on_a_fresh_boot(tmp_path):
    source = open(dbmod.__file__).read()
    registered = {int(v) for v in re.findall(r"^\s{8}(\d+): \(", source, re.M)}

    conn = _conn(_boot(tmp_path))
    applied = {r[0] for r in conn.execute("SELECT version FROM _migrations")}
    conn.close()

    assert registered, "the _MIGRATIONS registry could not be read"
    assert registered == applied


def test_a_failing_migration_aborts_the_pass_without_recording_itself(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="app.database")
    conn = _conn(_boot(tmp_path))

    def _boom(_conn):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(dbmod, "_migration_v55_news_posts_author_index", _boom)
    with pytest.raises(sqlite3.OperationalError):
        _replay(conn, 55)
    monkeypatch.undo()

    assert _scalar(conn, "SELECT COUNT(*) FROM _migrations WHERE version = 55") == 0, \
        "a migration that raised must be retried on the next boot"
    conn.close()
    assert any("Data migration v55" in r.getMessage() and r.levelno == logging.CRITICAL
               for r in caplog.records)


def test_a_migration_pass_recreates_its_bookkeeping_table(tmp_path):
    path = _boot(tmp_path)
    conn = _conn(path)
    conn.execute("DROP TABLE _migrations")
    conn.commit()

    dbmod._run_migrations(conn)

    assert _scalar(conn, "SELECT COUNT(*) FROM _migrations") > 0
    conn.close()




# -----------------------------------------------------------
# Column-adding migrations — the re-raise guard
# -----------------------------------------------------------
#
# _BrokenConn answers every execute with an OperationalError
# that is NOT "duplicate column", which is the one case those
# migrations may swallow. Anything else must abort the boot.
# -----------------------------------------------------------

class _BrokenConn:

    def __init__(self, message):
        self.message = message

    def execute(self, *args, **kwargs):
        raise sqlite3.OperationalError(self.message)

    def commit(self):
        pass


@pytest.mark.parametrize("migration", [
    "_migration_v3_add_invited_column",
    "_migration_v5_add_student_fields",
    "_migration_v8_add_active_column",
    "_migration_v9_add_reply_and_delete",
    "_migration_v10_add_client_msg_id",
    "_migration_v11_add_push_language",
])
def test_a_column_migration_re_raises_anything_but_a_duplicate_column(migration):
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        getattr(dbmod, migration)(_BrokenConn("attempt to write a readonly database"))


@pytest.mark.parametrize("migration", [
    "_migration_v3_add_invited_column",
    "_migration_v5_add_student_fields",
    "_migration_v8_add_active_column",
    "_migration_v9_add_reply_and_delete",
    "_migration_v10_add_client_msg_id",
    "_migration_v11_add_push_language",
])
def test_a_column_migration_swallows_a_duplicate_column(tmp_path, migration):
    conn = _conn(_boot(tmp_path))
    getattr(dbmod, migration)(conn)  # the columns are already in _SCHEMA
    conn.close()




# -----------------------------------------------------------
# Column migrations against a pre-migration file
# -----------------------------------------------------------
#
# _SCHEMA carries the v3–v11 columns, so on a fresh database
# every one of those ALTERs is a swallowed duplicate and the
# code that actually ADDS the column never runs. These build
# the table shape a deployed file really had and prove the
# ALTER lands with the default the banner promises.
# -----------------------------------------------------------

_PRE_V3_USERS = """
CREATE TABLE users (
    id TEXT PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    email TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'student',
    avatar_url TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT INTO users (id, username, email, display_name, password_hash)
VALUES ('u1', 'ona', 'ona@knf.vu.lt', 'Ona', 'x');
"""

_PRE_V9_MESSAGES = """
CREATE TABLE messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    image_url TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT INTO messages (id, conversation_id, sender_id, text) VALUES ('m1', 'c1', 'u1', 'labas');
"""

_PRE_V11_PUSH_TOKENS = """
CREATE TABLE push_tokens (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    token TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT 'unknown',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT INTO push_tokens (id, user_id, token) VALUES ('t1', 'u1', 'ExponentPushToken[aaaaaaaaaa]');
"""


def _legacy_conn(tmp_path, name, ddl):
    conn = sqlite3.connect(str(tmp_path / name))
    conn.executescript(ddl)
    conn.commit()
    return conn


def test_v3_marks_every_pre_existing_user_as_invited(tmp_path):
    conn = _legacy_conn(tmp_path, "pre-v3.db", _PRE_V3_USERS)

    dbmod._migration_v3_add_invited_column(conn)

    assert "invited" in _columns(conn, "users")
    assert _scalar(conn, "SELECT invited FROM users WHERE id = 'u1'") == 1, \
        "accounts that predate v3 all came through an invitation code"
    conn.close()


def test_v5_adds_the_three_student_id_columns_as_nullable(tmp_path):
    conn = _legacy_conn(tmp_path, "pre-v5.db", _PRE_V3_USERS)

    dbmod._migration_v5_add_student_fields(conn)

    assert {"student_number", "study_group", "study_program"} <= _columns(conn, "users")
    assert conn.execute(
        "SELECT student_number, study_group, study_program FROM users WHERE id = 'u1'"
    ).fetchone() == (None, None, None)
    conn.close()


def test_v8_activates_every_pre_existing_account(tmp_path):
    conn = _legacy_conn(tmp_path, "pre-v8.db", _PRE_V3_USERS)

    dbmod._migration_v8_add_active_column(conn)

    assert _scalar(conn, "SELECT active FROM users WHERE id = 'u1'") == 1, \
        "the deactivation column must not lock existing users out"
    conn.close()


def test_v9_adds_the_reply_and_soft_delete_columns(tmp_path):
    conn = _legacy_conn(tmp_path, "pre-v9.db", _PRE_V9_MESSAGES)

    dbmod._migration_v9_add_reply_and_delete(conn)

    assert {"reply_to_id", "deleted_at"} <= _columns(conn, "messages")
    assert conn.execute("SELECT reply_to_id, deleted_at FROM messages WHERE id = 'm1'").fetchone() == (None, None)
    conn.close()


def test_v10_adds_the_send_nonce_and_the_index_that_makes_it_idempotent(tmp_path):
    conn = _legacy_conn(tmp_path, "pre-v10.db", _PRE_V9_MESSAGES)

    dbmod._migration_v10_add_client_msg_id(conn)

    assert "client_msg_id" in _columns(conn, "messages")
    assert "idx_messages_client_msg" in _indexes(conn)

    conn.execute("INSERT INTO messages (id, conversation_id, sender_id, text, client_msg_id)"
                 " VALUES ('m2', 'c1', 'u1', 'vienas', 'nonce-1')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO messages (id, conversation_id, sender_id, text, client_msg_id)"
                     " VALUES ('m3', 'c1', 'u1', 'vienas', 'nonce-1')")

    # NULL nonces are all distinct — old rows and clients that
    # send none must never collide
    conn.execute("INSERT INTO messages (id, conversation_id, sender_id, text) VALUES ('m4', 'c1', 'u1', 'du')")
    conn.execute("INSERT INTO messages (id, conversation_id, sender_id, text) VALUES ('m5', 'c1', 'u1', 'trys')")
    conn.close()


def test_v11_defaults_existing_devices_to_lithuanian_push_copy(tmp_path):
    conn = _legacy_conn(tmp_path, "pre-v11.db", _PRE_V11_PUSH_TOKENS)

    dbmod._migration_v11_add_push_language(conn)

    assert _scalar(conn, "SELECT language FROM push_tokens WHERE id = 't1'") == "lt"
    conn.close()




# -----------------------------------------------------------
# Migrations replayed against the legacy data they exist for
# -----------------------------------------------------------

def test_v12_expires_the_leaked_bootstrap_code_and_leaves_it_expired(tmp_path):
    conn = _conn(_boot(tmp_path))
    conn.execute(
        "INSERT INTO invitation_codes (id, code, role, max_uses, expires_at)"
        " VALUES ('legacy', 'WELCOME-KNF-2026', 'student', 100, '2099-01-01T00:00:00+00:00')"
    )
    conn.commit()

    _replay(conn, 12)
    expired = _scalar(conn, "SELECT expires_at FROM invitation_codes WHERE code = 'WELCOME-KNF-2026'")
    assert expired <= utc_now_iso()

    _replay(conn, 12)
    assert _scalar(conn, "SELECT expires_at FROM invitation_codes WHERE code = 'WELCOME-KNF-2026'") == expired
    conn.close()


def test_v13_hashes_live_tokens_purges_expired_and_never_double_hashes(tmp_path):
    conn = _conn(_boot(tmp_path))
    user_id = _plant_user(conn)
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    already = hashlib.sha256(b"already").hexdigest()

    conn.execute("INSERT INTO sessions (id, user_id, token, expires_at) VALUES ('raw', ?, 'raw-token', ?)",
                 (user_id, future))
    conn.execute("INSERT INTO sessions (id, user_id, token, expires_at) VALUES ('old', ?, 'stale-token', ?)",
                 (user_id, (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()))
    conn.execute("INSERT INTO sessions (id, user_id, token, expires_at) VALUES ('hashed', ?, ?, ?)",
                 (user_id, already, future))
    conn.commit()

    _replay(conn, 13)

    assert _scalar(conn, "SELECT COUNT(*) FROM sessions WHERE id = 'old'") == 0
    assert _scalar(conn, "SELECT token FROM sessions WHERE id = 'raw'") == \
        hashlib.sha256(b"raw-token").hexdigest()
    assert _scalar(conn, "SELECT token FROM sessions WHERE id = 'hashed'") == already

    _replay(conn, 13)
    assert _scalar(conn, "SELECT token FROM sessions WHERE id = 'raw'") == \
        hashlib.sha256(b"raw-token").hexdigest()
    conn.close()


def test_v14_puts_drifted_counters_back_on_the_rows(tmp_path):
    conn = _conn(_boot(tmp_path))
    voters = [_plant_user(conn, f"balsuotojas{i}") for i in range(3)]
    post_id, poll_id, option_id = _plant_poll(conn)

    conn.execute("UPDATE news_posts SET likes_count = 99, comments_count = 42 WHERE id = ?", (post_id,))
    conn.execute("INSERT INTO news_likes (user_id, post_id) VALUES (?, ?)", (voters[0], post_id))
    conn.execute("INSERT INTO news_comments (id, post_id, user_id, text) VALUES ('c1', ?, ?, 'Sveiki')",
                 (post_id, voters[0]))
    for voter in voters:
        conn.execute("INSERT INTO poll_votes (user_id, poll_id, option_id) VALUES (?, ?, ?)",
                     (voter, poll_id, option_id))
    conn.execute("UPDATE polls SET total_votes = 0")
    conn.execute("UPDATE poll_options SET votes = 0")
    conn.commit()

    _replay(conn, 14)

    likes, comments = conn.execute(
        "SELECT likes_count, comments_count FROM news_posts WHERE id = ?", (post_id,)).fetchone()
    assert (likes, comments) == (1, 1)
    assert _scalar(conn, "SELECT total_votes FROM polls WHERE id = ?", (poll_id,)) == 3
    assert _scalar(conn, "SELECT votes FROM poll_options WHERE id = ?", (option_id,)) == 3
    conn.close()


def test_v16_rebuilds_a_table_whose_foreign_key_action_is_wrong(tmp_path):
    conn = _conn(_boot(tmp_path))
    user_id = _plant_user(conn)
    post_id, poll_id, option_id = _plant_poll(conn)
    conn.commit()

    # An old file's poll_votes: no foreign key on user_id at all
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DROP TABLE poll_votes")
    conn.execute("""
        CREATE TABLE poll_votes (
            user_id TEXT NOT NULL,
            poll_id TEXT NOT NULL REFERENCES polls(id) ON DELETE CASCADE,
            option_id TEXT NOT NULL REFERENCES poll_options(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, poll_id)
        )
    """)
    conn.execute("INSERT INTO poll_votes (user_id, poll_id, option_id) VALUES (?, ?, ?)",
                 (user_id, poll_id, option_id))
    conn.commit()

    _replay(conn, 16)

    assert _on_delete(conn, "poll_votes", "user_id") == "CASCADE"
    assert _scalar(conn, "SELECT COUNT(*) FROM poll_votes") == 1, "the rebuild dropped the rows it copied"
    assert _scalar(conn, "PRAGMA foreign_keys") == 1, "foreign_keys must be back ON after the rebuild"

    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    assert _scalar(conn, "SELECT COUNT(*) FROM poll_votes") == 0, "the new CASCADE does not fire"
    conn.close()


def test_v16_is_a_no_op_once_every_action_is_correct(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="app.database")
    conn = _conn(_boot(tmp_path))

    _replay(conn, 16)

    assert any("already correct" in r.getMessage() for r in caplog.records)
    conn.close()


def test_v17_rewrites_legacy_space_form_timestamps(tmp_path):
    conn = _conn(_boot(tmp_path))
    user_id = _plant_user(conn)
    conn.execute("UPDATE users SET created_at = '2025-03-04 05:06:07' WHERE id = ?", (user_id,))
    conn.execute("UPDATE users SET updated_at = '2025-03-04T05:06:07+00:00' WHERE id = ?", (user_id,))
    conn.commit()

    _replay(conn, 17)

    created, updated = conn.execute("SELECT created_at, updated_at FROM users WHERE id = ?", (user_id,)).fetchone()
    assert created == "2025-03-04T05:06:07"
    assert updated == "2025-03-04T05:06:07+00:00", "an already-normalised value must not be touched"
    conn.close()


def test_v18_dedupes_schedule_lessons_and_then_forbids_duplicates(tmp_path):
    conn = _conn(_boot(tmp_path))
    conn.execute("DROP INDEX idx_schedule_lessons_natural")
    for lesson_id in ("a", "b"):
        conn.execute(
            "INSERT INTO schedule_lessons (id, title, teacher, room, time_start, time_end, day_of_week,"
            " group_name, semester) VALUES (?, 'Algebra', 'Petraitis', '101', '08:00', '09:30', 1, 'IT-1', '2026')",
            (lesson_id,),
        )
    conn.execute(
        "INSERT INTO schedule_lessons (id, title, teacher, room, time_start, time_end, day_of_week,"
        " group_name, semester) VALUES ('c', 'Fizika', 'Petraitis', '101', '10:00', '11:30', 1, 'IT-1', '2026')"
    )
    conn.commit()

    _replay(conn, 18)

    assert [r[0] for r in conn.execute("SELECT id FROM schedule_lessons ORDER BY id")] == ["a", "c"]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO schedule_lessons (id, title, teacher, room, time_start, time_end, day_of_week,"
            " group_name, semester) VALUES ('d', 'Algebra', 'Petraitis', '101', '08:00', '09:30', 1, 'IT-1', '2026')"
        )
    conn.close()


def test_v19_prunes_scraper_runs_older_than_thirty_days(tmp_path):
    conn = _conn(_boot(tmp_path))
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    recent = (datetime.now(timezone.utc) - timedelta(days=29)).isoformat()
    conn.execute("INSERT INTO scraper_runs (id, source, started_at) VALUES ('old', 'knf.vu.lt', ?)", (old,))
    conn.execute("INSERT INTO scraper_runs (id, source, started_at) VALUES ('new', 'knf.vu.lt', ?)", (recent,))
    conn.commit()

    _replay(conn, 19)

    assert [r[0] for r in conn.execute("SELECT id FROM scraper_runs")] == ["new"]
    conn.close()


def test_v20_keeps_the_message_search_index_in_step_with_messages(tmp_path):
    conn = _conn(_boot(tmp_path))
    user_id = _plant_user(conn)
    conv_id = _plant_conversation(conn, members=(user_id,))
    conn.execute("INSERT INTO messages (id, conversation_id, sender_id, text) VALUES ('m1', ?, ?, 'paskaita rytoj')",
                 (conv_id, user_id))
    conn.commit()

    def _search(term):
        return [r[0] for r in conn.execute(
            "SELECT m.id FROM messages_fts JOIN messages m ON m.rowid = messages_fts.rowid"
            " WHERE messages_fts MATCH ?", (term,))]

    assert _search("paskaita") == ["m1"]

    conn.execute("UPDATE messages SET text = 'egzaminas rytoj' WHERE id = 'm1'")
    assert _search("paskaita") == []
    assert _search("egzaminas") == ["m1"]

    conn.execute("DELETE FROM messages WHERE id = 'm1'")
    assert _search("egzaminas") == []
    conn.close()


def test_v20_falls_back_to_like_search_when_fts5_is_missing(caplog):
    caplog.set_level(logging.WARNING, logger="app.database")

    class _NoFts5:
        def execute(self, *args, **kwargs):
            raise sqlite3.OperationalError("no such module: fts5")

    dbmod._migration_v20_messages_fts(_NoFts5())

    assert any("FTS5 unavailable" in r.getMessage() for r in caplog.records), \
        "a SQLite without FTS5 must degrade, not crash the boot"


def test_v21_collapses_duplicate_pending_requests_and_blocks_new_ones(tmp_path):
    conn = _conn(_boot(tmp_path))
    sender, receiver = _plant_user(conn, "siuntejas"), _plant_user(conn, "gavejas")
    conn.execute("DROP INDEX idx_friend_requests_pending")
    for request_id in ("r1", "r2"):
        conn.execute("INSERT INTO friend_requests (id, from_user_id, to_user_id, status)"
                     " VALUES (?, ?, ?, 'pending')", (request_id, sender, receiver))
    conn.execute("INSERT INTO friend_requests (id, from_user_id, to_user_id, status)"
                 " VALUES ('r3', ?, ?, 'rejected')", (sender, receiver))
    conn.commit()

    _replay(conn, 21)

    pending = [r[0] for r in conn.execute("SELECT id FROM friend_requests WHERE status = 'pending'")]
    assert pending == ["r1"]
    assert _scalar(conn, "SELECT COUNT(*) FROM friend_requests WHERE status = 'rejected'") == 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO friend_requests (id, from_user_id, to_user_id, status)"
                     " VALUES ('r4', ?, ?, 'pending')", (sender, receiver))
    conn.close()


def test_v22_drops_an_index_that_duplicates_an_implicit_one(tmp_path):
    conn = _conn(_boot(tmp_path))
    conn.execute("CREATE INDEX idx_sessions_token ON sessions(token)")
    conn.execute("CREATE INDEX idx_friendships_user ON friendships(user_id)")
    conn.commit()

    _replay(conn, 22)

    assert not (_RETIRED_INDEXES & _indexes(conn))
    conn.close()


def test_v23_deletes_read_receipts_whose_message_or_user_is_gone(tmp_path):
    conn = _conn(_boot(tmp_path))
    user_id = _plant_user(conn)
    conv_id = _plant_conversation(conn, members=(user_id,))
    conn.execute("INSERT INTO messages (id, conversation_id, sender_id, text) VALUES ('m1', ?, ?, 'labas')",
                 (conv_id, user_id))
    conn.execute("INSERT INTO message_reads (message_id, user_id) VALUES ('m1', ?)", (user_id,))
    conn.commit()

    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("INSERT INTO message_reads (message_id, user_id) VALUES ('ghost-message', ?)", (user_id,))
    conn.execute("INSERT INTO message_reads (message_id, user_id) VALUES ('m1', 'ghost-user')")
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")

    _replay(conn, 23)

    assert [r[0] for r in conn.execute("SELECT message_id FROM message_reads")] == ["m1"]
    conn.close()


def test_v25_and_v43_and_v40_tables_survive_a_replay(tmp_path):
    conn = _conn(_boot(tmp_path))
    for version in (25, 40, 43):
        _replay(conn, version)

    assert {"deleted_source_urls", "uploads", "admin_audit"} <= _tables(conn)
    conn.close()


def test_v26_keeps_the_oldest_poll_per_post_and_then_enforces_one(tmp_path):
    conn = _conn(_boot(tmp_path))
    post_id, poll_id, _option = _plant_poll(conn)
    conn.execute("DROP INDEX idx_polls_post")
    conn.execute("INSERT INTO polls (id, post_id, title) VALUES ('later', ?, 'Antras')", (post_id,))
    conn.commit()

    _replay(conn, 26)

    assert [r[0] for r in conn.execute("SELECT id FROM polls")] == [poll_id]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO polls (id, post_id, title) VALUES ('third', ?, 'Trecias')", (post_id,))
    conn.close()


def test_v26_sweeps_options_and_votes_that_outlived_their_poll(tmp_path):
    conn = _conn(_boot(tmp_path))
    user_id = _plant_user(conn)
    post_id, poll_id, option_id = _plant_poll(conn)
    conn.execute("INSERT INTO poll_votes (user_id, poll_id, option_id) VALUES (?, ?, ?)",
                 (user_id, poll_id, option_id))
    conn.commit()

    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DELETE FROM polls WHERE id = ?", (poll_id,))
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")

    _replay(conn, 26)

    assert _scalar(conn, "SELECT COUNT(*) FROM poll_options") == 0
    assert _scalar(conn, "SELECT COUNT(*) FROM poll_votes") == 0
    conn.close()


def test_v35_canonicalises_source_urls_and_drops_the_duplicates(tmp_path):
    conn = _conn(_boot(tmp_path))
    conn.execute("INSERT INTO news_posts (id, title, content, source, source_url)"
                 " VALUES ('first', 'T', 'C', 'knf.vu.lt', 'https://www.knf.vu.lt/naujienos/x/')")
    conn.execute("INSERT INTO news_posts (id, title, content, source, source_url)"
                 " VALUES ('second', 'T', 'C', 'knf.vu.lt', 'https://knf.vu.lt/naujienos/x?utm_source=fb')")
    conn.commit()

    _replay(conn, 35)

    rows = conn.execute("SELECT id, source_url FROM news_posts").fetchall()
    assert rows == [("first", "https://knf.vu.lt/naujienos/x")]
    conn.close()


def test_v47_purges_every_push_token_the_intake_grammar_would_refuse(tmp_path):
    conn = _conn(_boot(tmp_path))
    user_id = _plant_user(conn)
    tokens = {
        "keep-min": "ExponentPushToken[" + "a" * 10 + "]",
        "keep-max": "ExponentPushToken[" + "a" * 64 + "]",
        "drop-short": "ExponentPushToken[" + "a" * 9 + "]",
        "drop-long": "ExponentPushToken[" + "a" * 65 + "]",
        "drop-unclosed": "ExponentPushToken[abcdefghij",
        "drop-markup": "ExponentPushToken[<script>alert(1)</script>]",
        "drop-plain": "not-a-token",
    }
    for token_id, token in tokens.items():
        conn.execute("INSERT INTO push_tokens (id, user_id, token) VALUES (?, ?, ?)", (token_id, user_id, token))
    conn.commit()

    _replay(conn, 47)

    assert sorted(r[0] for r in conn.execute("SELECT id FROM push_tokens")) == ["keep-max", "keep-min"]
    conn.close()


def test_v47_deletes_more_rows_than_one_statement_can_bind(tmp_path):
    conn = _conn(_boot(tmp_path))
    user_id = _plant_user(conn)
    for i in range(401):
        conn.execute("INSERT INTO push_tokens (id, user_id, token) VALUES (?, ?, ?)",
                     (f"bad-{i}", user_id, f"garbage-{i}"))
    conn.commit()

    _replay(conn, 47)

    assert _scalar(conn, "SELECT COUNT(*) FROM push_tokens") == 0, \
        "the chunked delete must cover every doomed row, not just the first 400"
    conn.close()


def test_v49_demotes_a_multi_member_direct_room_to_a_titled_group(tmp_path):
    conn = _conn(_boot(tmp_path))
    members = [_plant_user(conn, f"narys{i}") for i in range(3)]
    crowded = _plant_conversation(conn, "direct", None, members)
    titled = _plant_conversation(conn, "direct", "Projektas", members)
    pair = _plant_conversation(conn, "direct", None, members[:2])
    alone = _plant_conversation(conn, "direct", None, members[:1])
    conn.commit()

    _replay(conn, 49)

    def _room(conv_id):
        return conn.execute("SELECT type, title FROM conversations WHERE id = ?", (conv_id,)).fetchone()

    assert _room(crowded) == ("group", "Grupė")
    assert _room(titled) == ("group", "Projektas")
    assert _room(pair) == ("direct", None)
    assert _room(alone) == ("direct", None), "a counterpart who left still leaves a legitimate direct room"
    conn.close()


def test_v49_is_idempotent_on_a_second_pass(tmp_path):
    conn = _conn(_boot(tmp_path))
    members = [_plant_user(conn, f"narys{i}") for i in range(3)]
    crowded = _plant_conversation(conn, "direct", None, members)
    conn.commit()

    _replay(conn, 49)
    _replay(conn, 49)

    assert conn.execute("SELECT type, title FROM conversations WHERE id = ?", (crowded,)).fetchone() == \
        ("group", "Grupė")
    conn.close()
