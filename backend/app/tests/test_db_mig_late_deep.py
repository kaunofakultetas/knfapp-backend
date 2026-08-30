# -----------------------------------------------------------
#  [*] Tests — app/database: migrations v12–v55, exhaustively
#
#  The sibling file test_database_migrations.py proves the
#  migration CHAIN: build one legacy file, boot init_db() over
#  it, assert the end state. This file does the opposite —
#  it calls each late migration FUNCTION directly, one at a
#  time, against a hand-built legacy database arranged to hit
#  one specific arm of that function, and asserts exactly what
#  that arm did.
#
#  What "exhaustively" means here, per migration:
#
#    - every guard clause and early return (v16's "nothing to
#      rebuild", v20's FTS5-less build)
#    - every `if rowcount:` log arm, in both directions
#    - the BOUNDARY of every comparison: v13's 63/64/65-char
#      token, v19's cutoff to the microsecond, v47's 9/10/64/65
#      character token bodies and its 400-row chunk edge,
#      v49's two-vs-three members
#    - idempotency — every one of these runs on every boot of
#      a database whose _migrations row was ever rebuilt, so a
#      second call must be a no-op
#    - ORDERING dependencies: v15 needs v9's column, v19's
#      prune mis-fires on the space-form timestamps v17 exists
#      to remove, v35 must delete before it rewrites
#    - the SQL traps the statements sit on: NOT IN against a
#      NULL, GROUP BY treating NULLs as equal while a UNIQUE
#      index treats them as distinct, REPLACE hitting every
#      space in a value and not just the first
#
#  Everything is driven on plain sqlite3 connections built by
#  _open_legacy() below — the same shape init_db hands the
#  migrations (no row_factory, so rows are TUPLES) — and no
#  test in this file touches the app, the network or the clock
#  except through time_machine.
# -----------------------------------------------------------

import hashlib
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import time_machine

import app.database as database


LOGGER_NAME = "app.database"

_EXPO_OK = "ExponentPushToken[abcdefghij1234567890]"




# -----------------------------------------------------------
# _restore_db_path
# -----------------------------------------------------------
#
# _db_path is a MODULE global that get_db() reads and that the
# migration failure log names. A couple of tests below pin it;
# putting the previous value back keeps that out of whatever
# test runs next.
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _restore_db_path():
    previous = database._db_path
    yield
    database._db_path = previous




# -----------------------------------------------------------
# _LEGACY_SCHEMA
# -----------------------------------------------------------
#
# A database as it looked AFTER v11 and BEFORE v12: every
# column the late migrations read already exists (invited,
# active, the student fields, messages.reply_to_id /
# deleted_at / client_msg_id, push_tokens.language), but
# nothing v12+ adds does:
#
#   - created_by / author_id are plain REFERENCES with no ON
#     DELETE action and poll_votes.user_id has no foreign key
#     at all, so v16 has real work
#   - the six indexes v22 drops are present
#   - deleted_source_urls / uploads / admin_audit are absent,
#     so v25 / v43 / v40 create them
#   - none of the migration-only indexes exist
#
# Used by:
#   - _open_legacy below, and through it every test here
# -----------------------------------------------------------

_LEGACY_SCHEMA = """
CREATE TABLE users (
    id TEXT PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    email TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'student',
    invited INTEGER NOT NULL DEFAULT 1,
    avatar_url TEXT,
    student_number TEXT,
    study_group TEXT,
    study_program TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE invitation_codes (
    id TEXT PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    role TEXT NOT NULL DEFAULT 'student',
    created_by TEXT REFERENCES users(id),
    max_uses INTEGER NOT NULL DEFAULT 1,
    use_count INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL
);

CREATE TABLE news_posts (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    summary TEXT,
    image_url TEXT,
    author_id TEXT REFERENCES users(id),
    author_name TEXT,
    source TEXT NOT NULL DEFAULT 'app',
    source_url TEXT UNIQUE,
    post_type TEXT NOT NULL DEFAULT 'article',
    is_public INTEGER NOT NULL DEFAULT 1,
    likes_count INTEGER NOT NULL DEFAULT 0,
    comments_count INTEGER NOT NULL DEFAULT 0,
    shares_count INTEGER NOT NULL DEFAULT 0,
    published_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE news_likes (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    post_id TEXT NOT NULL REFERENCES news_posts(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, post_id)
);

CREATE TABLE news_comments (
    id TEXT PRIMARY KEY,
    post_id TEXT NOT NULL REFERENCES news_posts(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE polls (
    id TEXT PRIMARY KEY,
    post_id TEXT NOT NULL REFERENCES news_posts(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    end_date TEXT,
    total_votes INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE poll_options (
    id TEXT PRIMARY KEY,
    poll_id TEXT NOT NULL REFERENCES polls(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    votes INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE poll_votes (
    user_id TEXT NOT NULL,
    poll_id TEXT NOT NULL REFERENCES polls(id) ON DELETE CASCADE,
    option_id TEXT NOT NULL REFERENCES poll_options(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, poll_id)
);

CREATE TABLE schedule_lessons (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    teacher TEXT,
    room TEXT,
    time_start TEXT NOT NULL,
    time_end TEXT NOT NULL,
    day_of_week INTEGER NOT NULL CHECK(day_of_week BETWEEN 0 AND 6),
    group_name TEXT,
    semester TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE scraper_runs (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    articles_found INTEGER NOT NULL DEFAULT 0,
    articles_new INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT
);

CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL DEFAULT 'direct',
    title TEXT,
    avatar_emoji TEXT,
    created_by TEXT REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE conversation_participants (
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    pinned INTEGER NOT NULL DEFAULT 0,
    last_read_at TEXT,
    joined_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (conversation_id, user_id)
);

CREATE TABLE messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    sender_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    text TEXT NOT NULL DEFAULT '',
    image_url TEXT,
    reply_to_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
    deleted_at TEXT,
    client_msg_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE message_reactions (
    message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    emoji TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (message_id, user_id)
);

CREATE TABLE message_reads (
    message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    read_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (message_id, user_id)
);

CREATE TABLE friendships (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    friend_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, friend_id)
);

CREATE TABLE friend_requests (
    id TEXT PRIMARY KEY,
    from_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    to_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE faculty_info (
    id TEXT PRIMARY KEY,
    lang TEXT NOT NULL DEFAULT 'lt',
    section TEXT NOT NULL,
    data_json TEXT NOT NULL,
    scraped_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(lang, section)
);

CREATE TABLE push_tokens (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT 'unknown',
    language TEXT NOT NULL DEFAULT 'lt',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE notification_channels (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, channel)
);

CREATE UNIQUE INDEX idx_sessions_token ON sessions(token);
CREATE UNIQUE INDEX idx_invitation_codes_code ON invitation_codes(code);
CREATE INDEX idx_message_reactions_message ON message_reactions(message_id);
CREATE INDEX idx_message_reads_message ON message_reads(message_id);
CREATE INDEX idx_friendships_user ON friendships(user_id);
CREATE INDEX idx_notification_channels_user ON notification_channels(user_id);
"""




# -----------------------------------------------------------
# _open_legacy
# -----------------------------------------------------------
#
# A file-backed legacy database on a PLAIN connection — no
# row_factory, exactly what init_db hands the migrations, so a
# migration reading rows positionally is tested the way it
# runs. foreign_keys is off unless asked for, which is what
# lets the deliberate orphans be planted at all.
#
# Used by:
#   - the legacy / legacy_fk fixtures below
# -----------------------------------------------------------

def _open_legacy(path, foreign_keys=False):
    conn = sqlite3.connect(str(path))
    if foreign_keys:
        conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_LEGACY_SCHEMA)
    conn.commit()
    return conn


@pytest.fixture
def legacy(tmp_path):
    conn = _open_legacy(tmp_path / "legacy.db")
    yield conn
    conn.close()


@pytest.fixture
def legacy_fk(tmp_path):
    conn = _open_legacy(tmp_path / "legacy-fk.db", foreign_keys=True)
    yield conn
    conn.close()




# -----------------------------------------------------------
# planting helpers
# -----------------------------------------------------------
#
# Tiny inserters so each test body is the arrangement it is
# actually about and nothing else. Every one returns the id it
# planted.
#
# Used by:
#   - the per-migration sections below
# -----------------------------------------------------------

def _user(conn, uid=None, username=None, role="student"):
    uid = uid or str(uuid.uuid4())
    username = username or f"u_{uuid.uuid4().hex[:8]}"
    conn.execute(
        "INSERT INTO users (id, username, email, display_name, password_hash, role)"
        " VALUES (?, ?, ?, ?, 'x', ?)",
        (uid, username, f"{username}@knf.vu.lt", username.title(), role),
    )
    return uid


def _post(conn, pid=None, author_id=None, source_url=None, source="app",
          likes=0, comments=0):
    pid = pid or str(uuid.uuid4())
    conn.execute(
        "INSERT INTO news_posts (id, title, content, author_id, source, source_url,"
        " likes_count, comments_count) VALUES (?, 'T', 'C', ?, ?, ?, ?, ?)",
        (pid, author_id, source, source_url, likes, comments),
    )
    return pid


def _room(conn, cid=None, kind="direct", title=None, members=()):
    cid = cid or str(uuid.uuid4())
    conn.execute(
        "INSERT INTO conversations (id, type, title) VALUES (?, ?, ?)",
        (cid, kind, title),
    )
    for member in members:
        conn.execute(
            "INSERT INTO conversation_participants (conversation_id, user_id) VALUES (?, ?)",
            (cid, member),
        )
    return cid


def _message(conn, mid=None, conversation_id="c1", sender_id="u1", text="labas"):
    mid = mid or str(uuid.uuid4())
    conn.execute(
        "INSERT INTO messages (id, conversation_id, sender_id, text) VALUES (?, ?, ?, ?)",
        (mid, conversation_id, sender_id, text),
    )
    return mid


def _push_token(conn, token, tid=None, user_id="u1", active=1):
    tid = tid or str(uuid.uuid4())
    conn.execute(
        "INSERT INTO push_tokens (id, user_id, token, active) VALUES (?, ?, ?, ?)",
        (tid, user_id, token, active),
    )
    return tid


def _run(conn, source="knf.vu.lt", status="completed", started_at=None, rid=None):
    rid = rid or str(uuid.uuid4())
    conn.execute(
        "INSERT INTO scraper_runs (id, source, status, started_at) VALUES (?, ?, ?, ?)",
        (rid, source, status, started_at or database.utc_now_iso()),
    )
    return rid




# -----------------------------------------------------------
# inspection helpers
# -----------------------------------------------------------
#
# _indexes maps index name → its CREATE text (None for the
# implicit sqlite_autoindex_* rows), which is how the index
# migrations are asserted without re-typing their SQL.
#
# Used by:
#   - v15 / v18 / v19 / v22 / v26 / v36 / v46 / v55 sections
# -----------------------------------------------------------

def _indexes(conn):
    return {
        name: sql
        for name, sql in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }


def _tables(conn):
    return {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }


def _on_delete(conn, table, column):
    for fk in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall():
        if fk[3] == column:
            return fk[6]
    return None


def _committed(conn, path):
    return sqlite3.connect(str(path))




# -----------------------------------------------------------
# _Spy
# -----------------------------------------------------------
#
# A connection stand-in that records every statement and can
# be told to raise on one of them. Two migrations can only be
# driven into an arm this way: v20's FTS5-less SQLite build,
# and v47's chunk boundary, where the POINT is how many DELETE
# statements were issued rather than what survived.
#
# Used by:
#   - the v20 and v47 sections
# -----------------------------------------------------------

class _Spy:

    def __init__(self, conn, fail_on=None, error=None):
        self._conn = conn
        self._fail_on = fail_on
        self._error = error
        self.statements = []

    def execute(self, sql, *args):
        self.statements.append(sql)
        if self._fail_on is not None and self._fail_on in sql:
            raise self._error
        return self._conn.execute(sql, *args)

    def executescript(self, sql):
        self.statements.append(sql)
        return self._conn.executescript(sql)

    def commit(self):
        return self._conn.commit()

    def deletes(self):
        return [sql for sql in self.statements if sql.lstrip().upper().startswith("DELETE")]




# ===========================================================
# v12 — expire the legacy WELCOME-KNF-2026 bootstrap invite
# ===========================================================

def _invite(conn, code, expires_at, iid=None, max_uses=100, use_count=0, created_by=None):
    iid = iid or str(uuid.uuid4())
    conn.execute(
        "INSERT INTO invitation_codes (id, code, role, created_by, max_uses, use_count, expires_at)"
        " VALUES (?, ?, 'student', ?, ?, ?, ?)",
        (iid, code, created_by, max_uses, use_count, expires_at),
    )
    return iid


def test_v12_expires_the_shipped_code_to_exactly_now(legacy):
    frozen = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    _invite(legacy, "WELCOME-KNF-2026", "2030-01-01T00:00:00+00:00")

    with time_machine.travel(frozen, tick=False):
        database._migration_v12_expire_bootstrap_invite(legacy)
        expected = database.utc_now_iso()

    row = legacy.execute(
        "SELECT expires_at FROM invitation_codes WHERE code = 'WELCOME-KNF-2026'"
    ).fetchone()
    assert row[0] == expected


def test_v12_leaves_the_row_in_place_rather_than_deleting_it(legacy):
    _invite(legacy, "WELCOME-KNF-2026", "2030-01-01T00:00:00+00:00")
    database._migration_v12_expire_bootstrap_invite(legacy)

    assert legacy.execute(
        "SELECT COUNT(*) FROM invitation_codes WHERE code = 'WELCOME-KNF-2026'"
    ).fetchone()[0] == 1


def test_v12_keeps_the_use_history_and_role_of_the_expired_code(legacy):
    creator = _user(legacy)
    _invite(legacy, "WELCOME-KNF-2026", "2030-01-01T00:00:00+00:00",
            max_uses=100, use_count=37, created_by=creator)
    database._migration_v12_expire_bootstrap_invite(legacy)

    row = legacy.execute(
        "SELECT role, max_uses, use_count, created_by FROM invitation_codes"
        " WHERE code = 'WELCOME-KNF-2026'"
    ).fetchone()
    assert row == ("student", 100, 37, creator)


def test_v12_ignores_a_code_that_already_expired(legacy):
    _invite(legacy, "WELCOME-KNF-2026", "2000-01-01T00:00:00+00:00")
    database._migration_v12_expire_bootstrap_invite(legacy)

    assert legacy.execute(
        "SELECT expires_at FROM invitation_codes WHERE code = 'WELCOME-KNF-2026'"
    ).fetchone()[0] == "2000-01-01T00:00:00+00:00"


def test_v12_ignores_a_code_expiring_at_exactly_this_instant(legacy):
    frozen = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    with time_machine.travel(frozen, tick=False):
        now = database.utc_now_iso()
        _invite(legacy, "WELCOME-KNF-2026", now)
        database._migration_v12_expire_bootstrap_invite(legacy)

    assert legacy.execute(
        "SELECT expires_at FROM invitation_codes WHERE code = 'WELCOME-KNF-2026'"
    ).fetchone()[0] == now


def test_v12_never_touches_a_generated_bootstrap_code(legacy):
    _invite(legacy, "KNF-DEADBEEFDEADBEEF", "2030-01-01T00:00:00+00:00")
    database._migration_v12_expire_bootstrap_invite(legacy)

    assert legacy.execute(
        "SELECT expires_at FROM invitation_codes WHERE code = 'KNF-DEADBEEFDEADBEEF'"
    ).fetchone()[0] == "2030-01-01T00:00:00+00:00"


@pytest.mark.parametrize("code", [
    "welcome-knf-2026",
    "WELCOME-KNF-2026 ",
    " WELCOME-KNF-2026",
    "WELCOME_KNF_2026",
    "WELCOME-KNF-2027",
])
def test_v12_matches_the_literal_code_and_nothing_near_it(legacy, code):
    _invite(legacy, code, "2030-01-01T00:00:00+00:00")
    database._migration_v12_expire_bootstrap_invite(legacy)

    assert legacy.execute(
        "SELECT expires_at FROM invitation_codes WHERE code = ?", (code,)
    ).fetchone()[0] == "2030-01-01T00:00:00+00:00"


def test_v12_on_an_empty_invitation_table_is_silent(legacy, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    database._migration_v12_expire_bootstrap_invite(legacy)

    assert "Expired the legacy" not in caplog.text


def test_v12_announces_the_expiry_only_when_it_changed_a_row(legacy, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    _invite(legacy, "WELCOME-KNF-2026", "2030-01-01T00:00:00+00:00")
    database._migration_v12_expire_bootstrap_invite(legacy)

    assert "Expired the legacy WELCOME-KNF-2026 invitation code" in caplog.text


def test_v12_is_idempotent_on_a_second_call(legacy, caplog):
    _invite(legacy, "WELCOME-KNF-2026", "2030-01-01T00:00:00+00:00")
    database._migration_v12_expire_bootstrap_invite(legacy)
    first = legacy.execute(
        "SELECT expires_at FROM invitation_codes WHERE code = 'WELCOME-KNF-2026'"
    ).fetchone()[0]

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    database._migration_v12_expire_bootstrap_invite(legacy)

    assert legacy.execute(
        "SELECT expires_at FROM invitation_codes WHERE code = 'WELCOME-KNF-2026'"
    ).fetchone()[0] == first
    assert "Expired the legacy" not in caplog.text


def test_v12_commits_so_another_connection_sees_the_expiry(tmp_path):
    path = tmp_path / "v12.db"
    conn = _open_legacy(path)
    _invite(conn, "WELCOME-KNF-2026", "2030-01-01T00:00:00+00:00")
    conn.commit()
    database._migration_v12_expire_bootstrap_invite(conn)

    other = _committed(conn, path)
    try:
        assert other.execute(
            "SELECT expires_at FROM invitation_codes WHERE code = 'WELCOME-KNF-2026'"
        ).fetchone()[0] != "2030-01-01T00:00:00+00:00"
    finally:
        other.close()
        conn.close()




# ===========================================================
# v13 — sessions.token becomes sha256, expired rows purged
# ===========================================================

def _session(conn, token, expires_at="2099-01-01T00:00:00+00:00", sid=None, user_id="u1"):
    sid = sid or str(uuid.uuid4())
    conn.execute(
        "INSERT INTO sessions (id, user_id, token, expires_at) VALUES (?, ?, ?, ?)",
        (sid, user_id, token, expires_at),
    )
    return sid


def test_v13_hashes_a_raw_uuid_token(legacy):
    raw = "11111111-2222-3333-4444-555555555555"
    sid = _session(legacy, raw)
    database._migration_v13_hash_session_tokens(legacy)

    assert legacy.execute("SELECT token FROM sessions WHERE id = ?", (sid,)).fetchone()[0] == \
        hashlib.sha256(raw.encode()).hexdigest()


def test_v13_leaves_a_lowercase_hex_digest_alone(legacy):
    digest = hashlib.sha256(b"already").hexdigest()
    sid = _session(legacy, digest)
    database._migration_v13_hash_session_tokens(legacy)

    assert legacy.execute("SELECT token FROM sessions WHERE id = ?", (sid,)).fetchone()[0] == digest


def test_v13_rehashes_a_64_character_uppercase_hex_token(legacy):
    upper = hashlib.sha256(b"upper").hexdigest().upper()
    sid = _session(legacy, upper)
    database._migration_v13_hash_session_tokens(legacy)

    assert legacy.execute("SELECT token FROM sessions WHERE id = ?", (sid,)).fetchone()[0] == \
        hashlib.sha256(upper.encode()).hexdigest()


@pytest.mark.parametrize("token, why", [
    ("a" * 63, "one character short of a digest"),
    ("a" * 65, "one character past a digest"),
    ("g" * 64, "right length, outside the hex alphabet"),
    ("0123456789abcdef" * 3 + "0123456789abcde!", "right length, one non-hex character"),
])
def test_v13_hashes_anything_that_is_not_exactly_a_digest(legacy, token, why):
    sid = _session(legacy, token)
    database._migration_v13_hash_session_tokens(legacy)

    assert legacy.execute("SELECT token FROM sessions WHERE id = ?", (sid,)).fetchone()[0] == \
        hashlib.sha256(token.encode()).hexdigest(), why


def test_v13_hashes_an_empty_token(legacy):
    sid = _session(legacy, "")
    database._migration_v13_hash_session_tokens(legacy)

    assert legacy.execute("SELECT token FROM sessions WHERE id = ?", (sid,)).fetchone()[0] == \
        hashlib.sha256(b"").hexdigest()


def test_v13_hashes_a_non_ascii_token_as_utf8(legacy):
    raw = "žetonas-ąčę"
    sid = _session(legacy, raw)
    database._migration_v13_hash_session_tokens(legacy)

    assert legacy.execute("SELECT token FROM sessions WHERE id = ?", (sid,)).fetchone()[0] == \
        hashlib.sha256(raw.encode("utf-8")).hexdigest()


def test_v13_purges_an_expired_row_instead_of_hashing_it(legacy):
    _session(legacy, "expired-raw-token", expires_at="2000-01-01T00:00:00+00:00")
    database._migration_v13_hash_session_tokens(legacy)

    assert legacy.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_v13_keeps_a_session_expiring_at_exactly_this_instant_and_hashes_it(legacy):
    frozen = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    with time_machine.travel(frozen, tick=False):
        now = database.utc_now_iso()
        sid = _session(legacy, "boundary-token", expires_at=now)
        database._migration_v13_hash_session_tokens(legacy)

    assert legacy.execute("SELECT token FROM sessions WHERE id = ?", (sid,)).fetchone()[0] == \
        hashlib.sha256(b"boundary-token").hexdigest()


def test_v13_on_an_empty_sessions_table_says_nothing(legacy, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    database._migration_v13_hash_session_tokens(legacy)

    assert "Purged" not in caplog.text
    assert "Hashed" not in caplog.text


def test_v13_counts_the_purged_and_the_hashed_rows_separately(legacy, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    _session(legacy, "dead-1", expires_at="2000-01-01T00:00:00+00:00")
    _session(legacy, "dead-2", expires_at="2000-01-01T00:00:00+00:00")
    _session(legacy, "live-1")
    _session(legacy, "live-2")
    _session(legacy, hashlib.sha256(b"digest").hexdigest())

    database._migration_v13_hash_session_tokens(legacy)

    assert "Purged 2 expired session row(s)" in caplog.text
    assert "Hashed 2 live session token(s)" in caplog.text


def test_v13_is_idempotent_because_every_token_is_a_digest_afterwards(legacy, caplog):
    _session(legacy, "raw-one")
    _session(legacy, "raw-two")
    database._migration_v13_hash_session_tokens(legacy)
    before = sorted(t for (t,) in legacy.execute("SELECT token FROM sessions").fetchall())

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    database._migration_v13_hash_session_tokens(legacy)

    assert sorted(t for (t,) in legacy.execute("SELECT token FROM sessions").fetchall()) == before
    assert "Hashed" not in caplog.text


def test_v13_keeps_every_other_session_column_untouched(legacy):
    sid = _session(legacy, "raw", user_id="u42")
    legacy.execute("UPDATE sessions SET created_at = '2020-02-02T02:02:02+00:00' WHERE id = ?", (sid,))
    database._migration_v13_hash_session_tokens(legacy)

    row = legacy.execute(
        "SELECT user_id, created_at, expires_at FROM sessions WHERE id = ?", (sid,)
    ).fetchone()
    assert row == ("u42", "2020-02-02T02:02:02+00:00", "2099-01-01T00:00:00+00:00")


def test_v13_refuses_to_collide_a_rewritten_token_with_an_existing_digest(legacy):
    # A raw row whose digest is ALREADY held by another row is
    # the one shape the UNIQUE(token) index cannot absorb — the
    # migration raises and the boot retries it, rather than
    # silently dropping one of the two sessions
    raw = "collision-source"
    _session(legacy, raw, sid="aaa")
    _session(legacy, hashlib.sha256(raw.encode()).hexdigest(), sid="bbb")

    with pytest.raises(sqlite3.IntegrityError):
        database._migration_v13_hash_session_tokens(legacy)




# ===========================================================
# v14 — reconcile the denormalised news / poll counters
# ===========================================================

def _poll(conn, post_id, pid=None, total_votes=0):
    pid = pid or str(uuid.uuid4())
    conn.execute(
        "INSERT INTO polls (id, post_id, title, total_votes) VALUES (?, ?, 'Klausimas', ?)",
        (pid, post_id, total_votes),
    )
    return pid


def _option(conn, poll_id, oid=None, votes=0):
    oid = oid or str(uuid.uuid4())
    conn.execute(
        "INSERT INTO poll_options (id, poll_id, text, votes) VALUES (?, ?, 'A', ?)",
        (oid, poll_id, votes),
    )
    return oid


def test_v14_pulls_an_inflated_like_counter_back_down(legacy):
    post = _post(legacy, likes=999)
    user = _user(legacy)
    legacy.execute("INSERT INTO news_likes (user_id, post_id) VALUES (?, ?)", (user, post))
    database._migration_v14_reconcile_counters(legacy)

    assert legacy.execute("SELECT likes_count FROM news_posts WHERE id = ?", (post,)).fetchone()[0] == 1


def test_v14_pushes_a_deflated_comment_counter_back_up(legacy):
    post = _post(legacy, comments=0)
    user = _user(legacy)
    for _ in range(3):
        legacy.execute(
            "INSERT INTO news_comments (id, post_id, user_id, text) VALUES (?, ?, ?, 'k')",
            (str(uuid.uuid4()), post, user),
        )
    database._migration_v14_reconcile_counters(legacy)

    assert legacy.execute("SELECT comments_count FROM news_posts WHERE id = ?", (post,)).fetchone()[0] == 3


def test_v14_zeroes_the_counters_of_a_post_with_no_children(legacy):
    post = _post(legacy, likes=7, comments=11)
    database._migration_v14_reconcile_counters(legacy)

    assert legacy.execute(
        "SELECT likes_count, comments_count FROM news_posts WHERE id = ?", (post,)
    ).fetchone() == (0, 0)


def test_v14_recomputes_option_votes_from_the_votes_table(legacy):
    post = _post(legacy)
    poll = _poll(legacy, post)
    option_a = _option(legacy, poll, votes=99)
    option_b = _option(legacy, poll, votes=0)
    for _ in range(2):
        legacy.execute(
            "INSERT INTO poll_votes (user_id, poll_id, option_id) VALUES (?, ?, ?)",
            (str(uuid.uuid4()), poll, option_a),
        )
    database._migration_v14_reconcile_counters(legacy)

    assert legacy.execute("SELECT votes FROM poll_options WHERE id = ?", (option_a,)).fetchone()[0] == 2
    assert legacy.execute("SELECT votes FROM poll_options WHERE id = ?", (option_b,)).fetchone()[0] == 0


def test_v14_recomputes_the_poll_total_from_its_own_votes(legacy):
    post = _post(legacy)
    poll = _poll(legacy, post, total_votes=500)
    option = _option(legacy, poll)
    for _ in range(4):
        legacy.execute(
            "INSERT INTO poll_votes (user_id, poll_id, option_id) VALUES (?, ?, ?)",
            (str(uuid.uuid4()), poll, option),
        )
    database._migration_v14_reconcile_counters(legacy)

    assert legacy.execute("SELECT total_votes FROM polls WHERE id = ?", (poll,)).fetchone()[0] == 4


def test_v14_counts_a_polls_votes_by_poll_id_not_by_its_options(legacy):
    # A vote that names another poll's option still belongs to
    # the poll its poll_id points at — the two counters are
    # computed from different columns on purpose
    post_a, post_b = _post(legacy), _post(legacy)
    poll_a, poll_b = _poll(legacy, post_a), _poll(legacy, post_b)
    foreign_option = _option(legacy, poll_b)
    legacy.execute(
        "INSERT INTO poll_votes (user_id, poll_id, option_id) VALUES (?, ?, ?)",
        (str(uuid.uuid4()), poll_a, foreign_option),
    )
    database._migration_v14_reconcile_counters(legacy)

    assert legacy.execute("SELECT total_votes FROM polls WHERE id = ?", (poll_a,)).fetchone()[0] == 1
    assert legacy.execute("SELECT total_votes FROM polls WHERE id = ?", (poll_b,)).fetchone()[0] == 0
    assert legacy.execute("SELECT votes FROM poll_options WHERE id = ?", (foreign_option,)).fetchone()[0] == 1


def test_v14_leaves_shares_count_alone(legacy):
    post = _post(legacy)
    legacy.execute("UPDATE news_posts SET shares_count = 12 WHERE id = ?", (post,))
    database._migration_v14_reconcile_counters(legacy)

    assert legacy.execute("SELECT shares_count FROM news_posts WHERE id = ?", (post,)).fetchone()[0] == 12


def test_v14_on_entirely_empty_tables_still_logs_its_line(legacy, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    database._migration_v14_reconcile_counters(legacy)

    assert "Reconciled news like/comment counters and poll vote counters" in caplog.text


def test_v14_run_twice_lands_on_the_same_numbers(legacy):
    post = _post(legacy, likes=3)
    user = _user(legacy)
    legacy.execute("INSERT INTO news_likes (user_id, post_id) VALUES (?, ?)", (user, post))
    database._migration_v14_reconcile_counters(legacy)
    once = legacy.execute("SELECT likes_count FROM news_posts WHERE id = ?", (post,)).fetchone()[0]
    database._migration_v14_reconcile_counters(legacy)

    assert legacy.execute("SELECT likes_count FROM news_posts WHERE id = ?", (post,)).fetchone()[0] == once




# ===========================================================
# v15 — indexes on the hot foreign-key child columns
# ===========================================================

_V15_INDEXES = (
    "idx_sessions_user",
    "idx_news_likes_post",
    "idx_news_comments_user",
    "idx_messages_sender",
    "idx_messages_reply_to",
    "idx_news_posts_author",
    "idx_message_reactions_user",
    "idx_conversations_created_by",
    "idx_invitation_codes_created_by",
)


@pytest.mark.parametrize("name", _V15_INDEXES)
def test_v15_creates_each_foreign_key_child_index(legacy, name):
    database._migration_v15_add_fk_indexes(legacy)

    assert name in _indexes(legacy)


def test_v15_creates_exactly_nine_indexes_and_no_more(legacy):
    before = set(_indexes(legacy))
    database._migration_v15_add_fk_indexes(legacy)

    assert set(_indexes(legacy)) - before == set(_V15_INDEXES)


def test_v15_is_silent_and_harmless_on_a_second_call(legacy, caplog):
    database._migration_v15_add_fk_indexes(legacy)
    first = _indexes(legacy)

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    database._migration_v15_add_fk_indexes(legacy)

    assert _indexes(legacy) == first
    assert "Created the missing foreign-key child indexes" in caplog.text


def test_v15_leaves_an_existing_index_of_the_same_name_exactly_as_it_found_it(legacy):
    # IF NOT EXISTS matches on the NAME alone, so a same-named
    # index over a different column is kept and the wanted one
    # is never built — worth knowing before renaming anything
    legacy.execute("CREATE INDEX idx_sessions_user ON sessions(expires_at)")
    database._migration_v15_add_fk_indexes(legacy)

    assert "expires_at" in _indexes(legacy)["idx_sessions_user"]


def test_v15_needs_the_column_v9_added_and_says_so_when_it_is_missing(tmp_path):
    conn = _open_legacy(tmp_path / "prev9.db")
    try:
        conn.executescript("""
            DROP TABLE messages;
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)
        with pytest.raises(sqlite3.OperationalError, match="reply_to_id"):
            database._migration_v15_add_fk_indexes(conn)
    finally:
        conn.close()


def test_v15_indexes_survive_being_committed(tmp_path):
    path = tmp_path / "v15.db"
    conn = _open_legacy(path)
    database._migration_v15_add_fk_indexes(conn)

    other = _committed(conn, path)
    try:
        assert "idx_messages_sender" in _indexes(other)
    finally:
        other.close()
        conn.close()




# ===========================================================
# v16 — rebuild four tables for their ON DELETE actions
# ===========================================================

_V16_TARGETS = (
    ("invitation_codes", "created_by", "SET NULL"),
    ("news_posts", "author_id", "SET NULL"),
    ("conversations", "created_by", "SET NULL"),
    ("poll_votes", "user_id", "CASCADE"),
)


@pytest.mark.parametrize("table, column, wanted", _V16_TARGETS)
def test_v16_gives_each_target_column_its_declared_action(legacy, table, column, wanted):
    database._migration_v16_rebuild_fk_actions(legacy)

    assert _on_delete(legacy, table, column) == wanted


def test_v16_sees_a_column_with_no_foreign_key_at_all_as_needing_a_rebuild(legacy):
    assert _on_delete(legacy, "poll_votes", "user_id") is None
    database._migration_v16_rebuild_fk_actions(legacy)

    assert _on_delete(legacy, "poll_votes", "user_id") == "CASCADE"


def test_v16_returns_early_when_every_action_is_already_right(legacy, caplog):
    database._migration_v16_rebuild_fk_actions(legacy)

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    database._migration_v16_rebuild_fk_actions(legacy)

    assert "All FK actions already correct — nothing to rebuild" in caplog.text
    assert "Rebuilt" not in caplog.text


def test_v16_rebuilds_only_the_tables_that_still_need_it(legacy, caplog):
    # Fix three of the four by hand, leave poll_votes legacy —
    # only the fourth may be touched
    database._migration_v16_rebuild_fk_actions(legacy)
    legacy.execute("PRAGMA foreign_keys=OFF")
    legacy.executescript("""
        DROP TABLE poll_votes;
        CREATE TABLE poll_votes (
            user_id TEXT NOT NULL,
            poll_id TEXT NOT NULL REFERENCES polls(id) ON DELETE CASCADE,
            option_id TEXT NOT NULL REFERENCES poll_options(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, poll_id)
        );
    """)

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    database._migration_v16_rebuild_fk_actions(legacy)

    assert "Rebuilt poll_votes" in caplog.text
    assert "Rebuilt news_posts" not in caplog.text
    assert "Rebuilt invitation_codes" not in caplog.text
    assert "Rebuilt conversations" not in caplog.text


def test_v16_copies_every_column_of_every_row_including_the_nulls(legacy):
    author = _user(legacy)
    post = _post(legacy, author_id=author, source_url="https://knf.vu.lt/a")
    legacy.execute(
        "UPDATE news_posts SET summary = NULL, image_url = NULL, author_name = 'Ona',"
        " shares_count = 5, published_at = '2021-01-01T00:00:00+00:00' WHERE id = ?",
        (post,),
    )
    before = legacy.execute("SELECT * FROM news_posts WHERE id = ?", (post,)).fetchone()

    database._migration_v16_rebuild_fk_actions(legacy)

    assert legacy.execute("SELECT * FROM news_posts WHERE id = ?", (post,)).fetchone() == before


def test_v16_recreates_the_indexes_the_dropped_tables_took_with_them(legacy):
    database._migration_v15_add_fk_indexes(legacy)
    database._migration_v16_rebuild_fk_actions(legacy)
    names = _indexes(legacy)

    for name in ("idx_invitation_codes_created_by", "idx_news_posts_published",
                 "idx_news_posts_source", "idx_news_posts_author",
                 "idx_conversations_created_by"):
        assert name in names


def test_v16_clears_a_leftover_new_table_from_an_aborted_earlier_run(legacy):
    legacy.execute("CREATE TABLE news_posts_new (junk TEXT)")
    legacy.execute("INSERT INTO news_posts_new VALUES ('stale')")
    database._migration_v16_rebuild_fk_actions(legacy)

    assert "news_posts_new" not in _tables(legacy)
    assert _on_delete(legacy, "news_posts", "author_id") == "SET NULL"


def test_v16_leaves_foreign_key_enforcement_on_after_a_successful_rebuild(legacy):
    database._migration_v16_rebuild_fk_actions(legacy)

    assert legacy.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_v16_propagates_a_failure_out_of_the_rebuild_dance(legacy):
    # A VIEW wearing the *_new name is the one thing DROP TABLE
    # IF EXISTS will not absorb
    legacy.execute("CREATE VIEW news_posts_new AS SELECT 1 AS a")

    with pytest.raises(sqlite3.OperationalError, match="DROP VIEW"):
        database._migration_v16_rebuild_fk_actions(legacy)


def test_v16_after_the_rebuild_a_deleted_user_nulls_their_authored_rows(legacy_fk):
    author = _user(legacy_fk)
    post = _post(legacy_fk, author_id=author)
    room = _room(legacy_fk, kind="group")
    legacy_fk.execute("UPDATE conversations SET created_by = ? WHERE id = ?", (author, room))
    _invite(legacy_fk, "CODE-X", "2099-01-01T00:00:00+00:00", created_by=author)
    legacy_fk.commit()

    database._migration_v16_rebuild_fk_actions(legacy_fk)
    legacy_fk.execute("DELETE FROM users WHERE id = ?", (author,))

    assert legacy_fk.execute("SELECT author_id FROM news_posts WHERE id = ?", (post,)).fetchone()[0] is None
    assert legacy_fk.execute("SELECT created_by FROM conversations WHERE id = ?", (room,)).fetchone()[0] is None
    assert legacy_fk.execute("SELECT created_by FROM invitation_codes WHERE code = 'CODE-X'").fetchone()[0] is None


def test_v16_after_the_rebuild_a_deleted_user_takes_their_poll_votes(legacy_fk):
    voter = _user(legacy_fk)
    post = _post(legacy_fk)
    poll = _poll(legacy_fk, post)
    option = _option(legacy_fk, poll)
    legacy_fk.execute(
        "INSERT INTO poll_votes (user_id, poll_id, option_id) VALUES (?, ?, ?)",
        (voter, poll, option),
    )
    legacy_fk.commit()

    database._migration_v16_rebuild_fk_actions(legacy_fk)
    legacy_fk.execute("DELETE FROM users WHERE id = ?", (voter,))

    assert legacy_fk.execute("SELECT COUNT(*) FROM poll_votes").fetchone()[0] == 0


def test_v16_keeps_the_row_count_of_every_rebuilt_table(legacy):
    _user(legacy, uid="a1")
    _post(legacy, author_id="a1")
    _post(legacy, author_id="a1")
    _invite(legacy, "C-1", "2099-01-01T00:00:00+00:00")
    _room(legacy, kind="group")
    before = {
        table: legacy.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("invitation_codes", "news_posts", "conversations", "poll_votes")
    }

    database._migration_v16_rebuild_fk_actions(legacy)

    after = {
        table: legacy.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("invitation_codes", "news_posts", "conversations", "poll_votes")
    }
    assert after == before


def test_v16_keeps_the_check_constraints_the_replacement_tables_declare(legacy):
    database._migration_v16_rebuild_fk_actions(legacy)

    with pytest.raises(sqlite3.IntegrityError):
        legacy.execute(
            "INSERT INTO news_posts (id, title, content, source) VALUES ('x', 'T', 'C', 'facebook')"
        )




# ===========================================================
# v17 — space-form timestamps become ISO-T
# ===========================================================

def test_v17_rewrites_a_space_form_value(legacy):
    _user(legacy, uid="u1", username="ona")
    legacy.execute("UPDATE users SET created_at = '2024-03-04 05:06:07' WHERE id = 'u1'")
    database._migration_v17_normalize_timestamps(legacy)

    assert legacy.execute("SELECT created_at FROM users WHERE id = 'u1'").fetchone()[0] == \
        "2024-03-04T05:06:07"


def test_v17_leaves_an_iso_t_value_byte_identical(legacy):
    _user(legacy, uid="u1", username="ona")
    legacy.execute("UPDATE users SET created_at = '2024-03-04T05:06:07.123456+00:00' WHERE id = 'u1'")
    database._migration_v17_normalize_timestamps(legacy)

    assert legacy.execute("SELECT created_at FROM users WHERE id = 'u1'").fetchone()[0] == \
        "2024-03-04T05:06:07.123456+00:00"


@pytest.mark.parametrize("value", [
    "2024-3-4 05:06:07",
    "24-03-04 05:06:07",
    "abcd-03-04 05:06:07",
    "2024-03-04",
    "",
    "labas rytas",
])
def test_v17_never_touches_a_value_outside_the_legacy_shape(legacy, value):
    _user(legacy, uid="u1", username="ona")
    legacy.execute("UPDATE users SET created_at = ? WHERE id = 'u1'", (value,))
    database._migration_v17_normalize_timestamps(legacy)

    assert legacy.execute("SELECT created_at FROM users WHERE id = 'u1'").fetchone()[0] == value


def test_v17_leaves_a_null_timestamp_null(legacy):
    _user(legacy, uid="u1", username="ona")
    _room(legacy, cid="c1", members=())
    legacy.execute("INSERT INTO conversation_participants (conversation_id, user_id, last_read_at)"
                   " VALUES ('c1', 'u1', NULL)")
    database._migration_v17_normalize_timestamps(legacy)

    assert legacy.execute(
        "SELECT last_read_at FROM conversation_participants"
    ).fetchone()[0] is None


def test_v17_normalises_a_date_with_a_trailing_space_and_nothing_after_it(legacy):
    _user(legacy, uid="u1", username="ona")
    legacy.execute("UPDATE users SET created_at = '2024-03-04 ' WHERE id = 'u1'")
    database._migration_v17_normalize_timestamps(legacy)

    assert legacy.execute("SELECT created_at FROM users WHERE id = 'u1'").fetchone()[0] == "2024-03-04T"


def test_v17_replaces_every_space_in_a_matching_value_not_only_the_first(legacy):
    # REPLACE is global — a value that somehow carried a second
    # space comes out with a second 'T' too. No writer produces
    # one, but the transformation is what it is
    _user(legacy, uid="u1", username="ona")
    legacy.execute("UPDATE users SET created_at = '2024-03-04 05:06:07 UTC' WHERE id = 'u1'")
    database._migration_v17_normalize_timestamps(legacy)

    assert legacy.execute("SELECT created_at FROM users WHERE id = 'u1'").fetchone()[0] == \
        "2024-03-04T05:06:07TUTC"


@pytest.mark.parametrize("table, column, planter", [
    ("invitation_codes", "expires_at",
     "INSERT INTO invitation_codes (id, code, expires_at) VALUES ('i', 'C', '2024-03-04 05:06:07')"),
    ("sessions", "expires_at",
     "INSERT INTO sessions (id, user_id, token, expires_at) VALUES ('s', 'u1', 't', '2024-03-04 05:06:07')"),
    ("news_likes", "created_at",
     "INSERT INTO news_likes (user_id, post_id, created_at) VALUES ('u1', 'p1', '2024-03-04 05:06:07')"),
    ("polls", "end_date",
     "INSERT INTO polls (id, post_id, title, end_date) VALUES ('pl', 'p1', 'T', '2024-03-04 05:06:07')"),
    ("scraper_runs", "finished_at",
     "INSERT INTO scraper_runs (id, source, finished_at) VALUES ('r', 'knf.vu.lt', '2024-03-04 05:06:07')"),
    ("messages", "deleted_at",
     "INSERT INTO messages (id, conversation_id, sender_id, deleted_at)"
     " VALUES ('m', 'c1', 'u1', '2024-03-04 05:06:07')"),
    ("message_reads", "read_at",
     "INSERT INTO message_reads (message_id, user_id, read_at) VALUES ('m2', 'u1', '2024-03-04 05:06:07')"),
    ("friend_requests", "updated_at",
     "INSERT INTO friend_requests (id, from_user_id, to_user_id, updated_at)"
     " VALUES ('f', 'u1', 'u2', '2024-03-04 05:06:07')"),
    ("faculty_info", "scraped_at",
     "INSERT INTO faculty_info (id, section, data_json, scraped_at)"
     " VALUES ('fi', 'contacts', '{}', '2024-03-04 05:06:07')"),
    ("push_tokens", "updated_at",
     "INSERT INTO push_tokens (id, user_id, token, updated_at)"
     " VALUES ('pt', 'u1', 'tok', '2024-03-04 05:06:07')"),
    ("notification_channels", "updated_at",
     "INSERT INTO notification_channels (user_id, channel, updated_at)"
     " VALUES ('u1', 'news', '2024-03-04 05:06:07')"),
])
def test_v17_covers_the_nullable_and_the_obscure_columns_too(legacy, table, column, planter):
    _user(legacy, uid="u1", username="ona")
    _user(legacy, uid="u2", username="jonas")
    _post(legacy, pid="p1")
    _room(legacy, cid="c1")
    legacy.execute(planter)
    database._migration_v17_normalize_timestamps(legacy)

    assert legacy.execute(f"SELECT {column} FROM {table}").fetchone()[0] == "2024-03-04T05:06:07"


def test_v17_counts_every_value_it_changed_across_every_table(legacy, caplog):
    # The firing DEFAULTs write space-form too, so the rows are
    # normalised once first and the count then belongs to the
    # three values this test plants
    _user(legacy, uid="u1", username="ona")
    _post(legacy, pid="p1")
    database._migration_v17_normalize_timestamps(legacy)

    legacy.execute("UPDATE users SET created_at = '2024-03-04 05:06:07',"
                   " updated_at = '2024-03-04 05:06:07' WHERE id = 'u1'")
    legacy.execute("UPDATE news_posts SET published_at = '2024-03-04 05:06:07' WHERE id = 'p1'")

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    database._migration_v17_normalize_timestamps(legacy)

    assert "Normalised 3 space-form timestamp value(s) to ISO-T" in caplog.text


def test_v17_reports_zero_on_a_database_that_is_already_clean(legacy, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    database._migration_v17_normalize_timestamps(legacy)

    assert "Normalised 0 space-form timestamp value(s) to ISO-T" in caplog.text


def test_v17_run_twice_changes_nothing_the_second_time(legacy, caplog):
    _user(legacy, uid="u1", username="ona")
    legacy.execute("UPDATE users SET created_at = '2024-03-04 05:06:07' WHERE id = 'u1'")
    database._migration_v17_normalize_timestamps(legacy)

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    database._migration_v17_normalize_timestamps(legacy)

    assert "Normalised 0 space-form" in caplog.text
    assert legacy.execute("SELECT created_at FROM users WHERE id = 'u1'").fetchone()[0] == \
        "2024-03-04T05:06:07"


def test_v17_makes_a_legacy_row_sort_after_an_older_iso_t_row(legacy):
    # The whole point: before the pass a space-form 23:00 sorted
    # BEFORE a T-form 01:00 of the same day
    _user(legacy, uid="u1", username="ona")
    _user(legacy, uid="u2", username="jonas")
    legacy.execute("UPDATE users SET created_at = '2024-03-04 23:00:00' WHERE id = 'u1'")
    legacy.execute("UPDATE users SET created_at = '2024-03-04T01:00:00' WHERE id = 'u2'")

    assert [r[0] for r in legacy.execute("SELECT id FROM users ORDER BY created_at").fetchall()] == \
        ["u1", "u2"]

    database._migration_v17_normalize_timestamps(legacy)

    assert [r[0] for r in legacy.execute("SELECT id FROM users ORDER BY created_at").fetchall()] == \
        ["u2", "u1"]




# ===========================================================
# v18 — schedule_lessons dedupe plus its natural-key indexes
# ===========================================================

def _lesson(conn, lid=None, title="Matematika", teacher="Petras", room="101",
            time_start="08:00", time_end="09:30", day=0, group_name="IF-1",
            semester="2026-pavasaris"):
    lid = lid or str(uuid.uuid4())
    conn.execute(
        "INSERT INTO schedule_lessons (id, title, teacher, room, time_start, time_end,"
        " day_of_week, group_name, semester) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (lid, title, teacher, room, time_start, time_end, day, group_name, semester),
    )
    return lid


def test_v18_collapses_duplicates_to_the_lowest_rowid(legacy):
    first = _lesson(legacy, lid="keep")
    _lesson(legacy, lid="drop-1")
    _lesson(legacy, lid="drop-2")
    database._migration_v18_schedule_lessons_indexes(legacy)

    assert [r[0] for r in legacy.execute("SELECT id FROM schedule_lessons").fetchall()] == [first]


def test_v18_counts_the_duplicates_it_removed(legacy, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    _lesson(legacy)
    _lesson(legacy)
    _lesson(legacy)
    database._migration_v18_schedule_lessons_indexes(legacy)

    assert "Removed 2 duplicate schedule_lessons row(s)" in caplog.text


def test_v18_says_nothing_when_there_are_no_duplicates(legacy, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    _lesson(legacy)
    database._migration_v18_schedule_lessons_indexes(legacy)

    assert "duplicate schedule_lessons" not in caplog.text


@pytest.mark.parametrize("field, value", [
    ("title", "Fizika"),
    ("teacher", "Ona"),
    ("room", "202"),
    ("time_start", "10:00"),
    ("time_end", "11:30"),
    ("day", 3),
    ("group_name", "IF-2"),
    ("semester", "2026-ruduo"),
])
def test_v18_keeps_two_lessons_that_differ_in_any_one_natural_key_field(legacy, field, value):
    _lesson(legacy)
    _lesson(legacy, **{field: value})
    database._migration_v18_schedule_lessons_indexes(legacy)

    assert legacy.execute("SELECT COUNT(*) FROM schedule_lessons").fetchone()[0] == 2


def test_v18_dedupes_rows_whose_natural_key_holds_nulls(legacy):
    # GROUP BY treats two NULL teachers as the same group even
    # though a UNIQUE index would not
    _lesson(legacy, lid="keep", teacher=None, room=None, group_name=None, semester=None)
    _lesson(legacy, lid="drop", teacher=None, room=None, group_name=None, semester=None)
    database._migration_v18_schedule_lessons_indexes(legacy)

    assert [r[0] for r in legacy.execute("SELECT id FROM schedule_lessons").fetchall()] == ["keep"]


def test_v18_unique_index_cannot_stop_a_null_bearing_duplicate_afterwards(legacy):
    # The mirror image of the previous test: NULLs are distinct
    # to a UNIQUE index, so the constraint the dedupe enabled
    # does not cover the rows the dedupe collapsed
    _lesson(legacy, teacher=None)
    database._migration_v18_schedule_lessons_indexes(legacy)
    _lesson(legacy, teacher=None)

    assert legacy.execute("SELECT COUNT(*) FROM schedule_lessons").fetchone()[0] == 2


def test_v18_unique_index_refuses_a_fully_specified_duplicate(legacy):
    _lesson(legacy)
    database._migration_v18_schedule_lessons_indexes(legacy)

    with pytest.raises(sqlite3.IntegrityError):
        _lesson(legacy)


def test_v18_creates_the_filter_index_the_day_view_reads_through(legacy):
    database._migration_v18_schedule_lessons_indexes(legacy)
    sql = _indexes(legacy)["idx_schedule_lessons_filter"]

    assert "semester" in sql and "group_name" in sql and "day_of_week" in sql


def test_v18_on_an_empty_table_creates_both_indexes(legacy):
    database._migration_v18_schedule_lessons_indexes(legacy)
    names = _indexes(legacy)

    assert "idx_schedule_lessons_natural" in names
    assert "idx_schedule_lessons_filter" in names


def test_v18_is_idempotent(legacy):
    _lesson(legacy)
    _lesson(legacy)
    database._migration_v18_schedule_lessons_indexes(legacy)
    database._migration_v18_schedule_lessons_indexes(legacy)

    assert legacy.execute("SELECT COUNT(*) FROM schedule_lessons").fetchone()[0] == 1




# ===========================================================
# v19 — scraper_runs(started_at) index and the 30-day prune
# ===========================================================

def test_v19_creates_the_started_at_index(legacy):
    database._migration_v19_scraper_runs_index(legacy)

    assert "idx_scraper_runs_started" in _indexes(legacy)


def test_v19_prunes_a_run_older_than_thirty_days(legacy):
    frozen = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    with time_machine.travel(frozen, tick=False):
        _run(legacy, started_at=(frozen - timedelta(days=31)).isoformat())
        database._migration_v19_scraper_runs_index(legacy)

    assert legacy.execute("SELECT COUNT(*) FROM scraper_runs").fetchone()[0] == 0


def test_v19_keeps_a_run_exactly_on_the_thirty_day_cutoff(legacy):
    frozen = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    with time_machine.travel(frozen, tick=False):
        _run(legacy, rid="edge", started_at=(frozen - timedelta(days=30)).isoformat())
        database._migration_v19_scraper_runs_index(legacy)

    assert [r[0] for r in legacy.execute("SELECT id FROM scraper_runs").fetchall()] == ["edge"]


def test_v19_prunes_a_run_one_microsecond_past_the_cutoff(legacy):
    frozen = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    with time_machine.travel(frozen, tick=False):
        _run(legacy, started_at=(frozen - timedelta(days=30, microseconds=1)).isoformat())
        database._migration_v19_scraper_runs_index(legacy)

    assert legacy.execute("SELECT COUNT(*) FROM scraper_runs").fetchone()[0] == 0


def test_v19_keeps_todays_run(legacy):
    frozen = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    with time_machine.travel(frozen, tick=False):
        _run(legacy, rid="fresh")
        database._migration_v19_scraper_runs_index(legacy)

    assert [r[0] for r in legacy.execute("SELECT id FROM scraper_runs").fetchall()] == ["fresh"]


def test_v19_counts_the_rows_it_pruned(legacy, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    frozen = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    with time_machine.travel(frozen, tick=False):
        for _ in range(4):
            _run(legacy, started_at=(frozen - timedelta(days=90)).isoformat())
        database._migration_v19_scraper_runs_index(legacy)

    assert "Pruned 4 scraper_runs row(s) older than 30 days" in caplog.text


def test_v19_says_nothing_when_no_run_is_old_enough(legacy, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    _run(legacy)
    database._migration_v19_scraper_runs_index(legacy)

    assert "Pruned" not in caplog.text


def test_v19_prunes_regardless_of_the_runs_status(legacy):
    frozen = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    with time_machine.travel(frozen, tick=False):
        for status in ("running", "completed", "failed"):
            _run(legacy, status=status, started_at=(frozen - timedelta(days=60)).isoformat())
        database._migration_v19_scraper_runs_index(legacy)

    assert legacy.execute("SELECT COUNT(*) FROM scraper_runs").fetchone()[0] == 0


def test_v19_would_mis_prune_a_same_day_space_form_row_v17_had_not_normalised(legacy):
    # Why v17 has to run first: ' ' sorts below 'T', so an
    # un-normalised run from INSIDE the window compares as older
    # than the T-form cutoff and dies
    frozen = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    cutoff_day = (frozen - timedelta(days=30)).strftime("%Y-%m-%d")
    with time_machine.travel(frozen, tick=False):
        _run(legacy, rid="space", started_at=f"{cutoff_day} 23:59:59")
        database._migration_v19_scraper_runs_index(legacy)

    assert legacy.execute("SELECT COUNT(*) FROM scraper_runs").fetchone()[0] == 0


def test_v19_is_idempotent(legacy, caplog):
    frozen = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    with time_machine.travel(frozen, tick=False):
        _run(legacy, started_at=(frozen - timedelta(days=90)).isoformat())
        database._migration_v19_scraper_runs_index(legacy)

        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        database._migration_v19_scraper_runs_index(legacy)

    assert "Pruned" not in caplog.text




# ===========================================================
# v20 — the messages_fts shadow table and its triggers
# ===========================================================

def _fts_room(conn):
    _user(conn, uid="u1", username="ona")
    _room(conn, cid="c1")


def test_v20_creates_the_shadow_table_and_its_three_triggers(legacy):
    database._migration_v20_messages_fts(legacy)
    objects = {
        row[0]
        for row in legacy.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'messages_fts%'"
        ).fetchall()
    }

    assert {"messages_fts", "messages_fts_ai", "messages_fts_ad", "messages_fts_au"} <= objects


def test_v20_backfills_the_messages_that_predate_it(legacy):
    _fts_room(legacy)
    _message(legacy, mid="m1", text="paskaita apie duomenų bazes")
    database._migration_v20_messages_fts(legacy)

    hits = legacy.execute(
        "SELECT m.id FROM messages_fts f JOIN messages m ON m.rowid = f.rowid"
        " WHERE messages_fts MATCH 'duomenų'"
    ).fetchall()
    assert [r[0] for r in hits] == ["m1"]


def test_v20_insert_trigger_indexes_a_new_message(legacy):
    _fts_room(legacy)
    database._migration_v20_messages_fts(legacy)
    _message(legacy, mid="m2", text="egzaminas rytoj")

    assert legacy.execute(
        "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'egzaminas'"
    ).fetchone()[0] == 1


def test_v20_update_trigger_swaps_the_old_text_for_the_new(legacy):
    _fts_room(legacy)
    _message(legacy, mid="m3", text="senas tekstas")
    database._migration_v20_messages_fts(legacy)
    legacy.execute("UPDATE messages SET text = 'naujas tekstas' WHERE id = 'm3'")

    assert legacy.execute(
        "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'senas'"
    ).fetchone()[0] == 0
    assert legacy.execute(
        "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'naujas'"
    ).fetchone()[0] == 1


def test_v20_delete_trigger_removes_the_row_from_the_index(legacy):
    _fts_room(legacy)
    _message(legacy, mid="m4", text="istrinsiu")
    database._migration_v20_messages_fts(legacy)
    legacy.execute("DELETE FROM messages WHERE id = 'm4'")

    assert legacy.execute(
        "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'istrinsiu'"
    ).fetchone()[0] == 0


def test_v20_update_of_another_column_leaves_the_index_alone(legacy):
    # The trigger is AFTER UPDATE OF text — a soft delete stamp
    # must not disturb the index the search reads
    _fts_room(legacy)
    _message(legacy, mid="m5", text="isliks")
    database._migration_v20_messages_fts(legacy)
    legacy.execute("UPDATE messages SET deleted_at = '2026-01-01T00:00:00+00:00' WHERE id = 'm5'")

    assert legacy.execute(
        "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'isliks'"
    ).fetchone()[0] == 1


def test_v20_rebuild_on_a_second_run_does_not_double_index(legacy):
    _fts_room(legacy)
    _message(legacy, mid="m6", text="unikalus")
    database._migration_v20_messages_fts(legacy)
    database._migration_v20_messages_fts(legacy)

    assert legacy.execute(
        "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'unikalus'"
    ).fetchone()[0] == 1


def test_v20_on_an_empty_messages_table_still_builds_the_index(legacy, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    database._migration_v20_messages_fts(legacy)

    assert "Created messages_fts with sync triggers and backfilled it" in caplog.text
    assert legacy.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0] == 0


def test_v20_returns_early_and_warns_when_the_build_has_no_fts5(legacy, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    spy = _Spy(legacy, fail_on="fts5", error=sqlite3.OperationalError("no such module: fts5"))

    database._migration_v20_messages_fts(spy)

    assert "FTS5 unavailable" in caplog.text
    assert "message search stays on its LIKE fallback" in caplog.text
    assert len(spy.statements) == 1


def test_v20_creates_no_triggers_when_fts5_is_missing(legacy):
    spy = _Spy(legacy, fail_on="fts5", error=sqlite3.OperationalError("no such module: fts5"))
    database._migration_v20_messages_fts(spy)

    assert legacy.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'messages_fts%'"
    ).fetchone()[0] == 0


def test_v20_a_message_whose_text_is_empty_is_indexed_without_matching(legacy):
    _fts_room(legacy)
    _message(legacy, mid="m7", text="")
    database._migration_v20_messages_fts(legacy)

    assert legacy.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0] == 1




# ===========================================================
# v21 — one pending friend request per (from, to)
# ===========================================================

def _request(conn, from_id, to_id, status="pending", rid=None):
    rid = rid or str(uuid.uuid4())
    conn.execute(
        "INSERT INTO friend_requests (id, from_user_id, to_user_id, status) VALUES (?, ?, ?, ?)",
        (rid, from_id, to_id, status),
    )
    return rid


def test_v21_collapses_pending_duplicates_to_the_oldest(legacy):
    _request(legacy, "a", "b", rid="keep")
    _request(legacy, "a", "b", rid="drop-1")
    _request(legacy, "a", "b", rid="drop-2")
    database._migration_v21_friend_requests_pending_unique(legacy)

    assert [r[0] for r in legacy.execute("SELECT id FROM friend_requests").fetchall()] == ["keep"]


def test_v21_counts_the_duplicates_it_removed(legacy, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    _request(legacy, "a", "b")
    _request(legacy, "a", "b")
    _request(legacy, "a", "b")
    database._migration_v21_friend_requests_pending_unique(legacy)

    assert "Removed 2 duplicate pending friend request(s)" in caplog.text


def test_v21_says_nothing_when_there_is_nothing_to_collapse(legacy, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    _request(legacy, "a", "b")
    database._migration_v21_friend_requests_pending_unique(legacy)

    assert "duplicate pending friend request" not in caplog.text


def test_v21_keeps_the_pending_request_pointing_the_other_way(legacy):
    _request(legacy, "a", "b", rid="ab")
    _request(legacy, "b", "a", rid="ba")
    database._migration_v21_friend_requests_pending_unique(legacy)

    assert sorted(r[0] for r in legacy.execute("SELECT id FROM friend_requests").fetchall()) == \
        ["ab", "ba"]


@pytest.mark.parametrize("status", ["accepted", "rejected"])
def test_v21_never_collapses_settled_history_rows(legacy, status):
    _request(legacy, "a", "b", status=status)
    _request(legacy, "a", "b", status=status)
    database._migration_v21_friend_requests_pending_unique(legacy)

    assert legacy.execute("SELECT COUNT(*) FROM friend_requests").fetchone()[0] == 2


def test_v21_index_refuses_a_second_pending_row_for_the_same_pair(legacy):
    _request(legacy, "a", "b")
    database._migration_v21_friend_requests_pending_unique(legacy)

    with pytest.raises(sqlite3.IntegrityError):
        _request(legacy, "a", "b")


def test_v21_index_allows_a_fresh_pending_row_once_the_old_one_settled(legacy):
    first = _request(legacy, "a", "b")
    database._migration_v21_friend_requests_pending_unique(legacy)
    legacy.execute("UPDATE friend_requests SET status = 'rejected' WHERE id = ?", (first,))
    _request(legacy, "a", "b")

    assert legacy.execute(
        "SELECT COUNT(*) FROM friend_requests WHERE status = 'pending'"
    ).fetchone()[0] == 1


def test_v21_index_still_allows_many_settled_rows_for_one_pair(legacy):
    database._migration_v21_friend_requests_pending_unique(legacy)
    _request(legacy, "a", "b", status="rejected")
    _request(legacy, "a", "b", status="rejected")
    _request(legacy, "a", "b", status="accepted")

    assert legacy.execute("SELECT COUNT(*) FROM friend_requests").fetchone()[0] == 3


def test_v21_is_idempotent(legacy):
    _request(legacy, "a", "b")
    _request(legacy, "a", "b")
    database._migration_v21_friend_requests_pending_unique(legacy)
    database._migration_v21_friend_requests_pending_unique(legacy)

    assert legacy.execute("SELECT COUNT(*) FROM friend_requests").fetchone()[0] == 1




# ===========================================================
# v22 — drop the six indexes duplicating implicit ones
# ===========================================================

_V22_DROPPED = (
    "idx_sessions_token",
    "idx_invitation_codes_code",
    "idx_message_reactions_message",
    "idx_message_reads_message",
    "idx_friendships_user",
    "idx_notification_channels_user",
)


@pytest.mark.parametrize("name", _V22_DROPPED)
def test_v22_drops_each_named_index(legacy, name):
    assert name in _indexes(legacy)
    database._migration_v22_drop_duplicate_indexes(legacy)

    assert name not in _indexes(legacy)


def test_v22_drops_nothing_else(legacy):
    legacy.execute("CREATE INDEX idx_keep_me ON users(role)")
    database._migration_v22_drop_duplicate_indexes(legacy)

    assert "idx_keep_me" in _indexes(legacy)


def test_v22_on_a_database_that_never_had_them_is_a_no_op(legacy, caplog):
    database._migration_v22_drop_duplicate_indexes(legacy)
    after_first = _indexes(legacy)

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    database._migration_v22_drop_duplicate_indexes(legacy)

    assert _indexes(legacy) == after_first
    assert "Dropped the six duplicate indexes" in caplog.text


def test_v22_keeps_the_uniqueness_the_dropped_index_duplicated(legacy):
    _session(legacy, "one-token")
    database._migration_v22_drop_duplicate_indexes(legacy)

    with pytest.raises(sqlite3.IntegrityError):
        _session(legacy, "one-token")


def test_v22_keeps_the_invitation_code_uniqueness_too(legacy):
    _invite(legacy, "CODE-ONE", "2099-01-01T00:00:00+00:00")
    database._migration_v22_drop_duplicate_indexes(legacy)

    with pytest.raises(sqlite3.IntegrityError):
        _invite(legacy, "CODE-ONE", "2099-01-01T00:00:00+00:00")


def test_v22_drops_by_name_even_when_the_index_sits_on_another_table(legacy):
    # Index names are global in SQLite, so the DROP is by name
    # and nothing verifies what it covered
    legacy.execute("DROP INDEX idx_friendships_user")
    legacy.execute("CREATE INDEX idx_friendships_user ON users(username)")
    database._migration_v22_drop_duplicate_indexes(legacy)

    assert "idx_friendships_user" not in _indexes(legacy)


def test_v22_leaves_the_rows_of_every_table_it_touched(legacy):
    _session(legacy, "tok")
    _invite(legacy, "C", "2099-01-01T00:00:00+00:00")
    database._migration_v22_drop_duplicate_indexes(legacy)

    assert legacy.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    assert legacy.execute("SELECT COUNT(*) FROM invitation_codes").fetchone()[0] == 1




# ===========================================================
# v23 — delete orphaned message_reads rows
# ===========================================================

def _read(conn, message_id, user_id):
    conn.execute(
        "INSERT INTO message_reads (message_id, user_id) VALUES (?, ?)",
        (message_id, user_id),
    )


def test_v23_deletes_a_read_whose_message_is_gone(legacy):
    _user(legacy, uid="u1", username="ona")
    _read(legacy, "no-such-message", "u1")
    database._migration_v23_delete_orphan_message_reads(legacy)

    assert legacy.execute("SELECT COUNT(*) FROM message_reads").fetchone()[0] == 0


def test_v23_deletes_a_read_whose_user_is_gone(legacy):
    _user(legacy, uid="u1", username="ona")
    _room(legacy, cid="c1")
    _message(legacy, mid="m1", conversation_id="c1", sender_id="u1")
    _read(legacy, "m1", "no-such-user")
    database._migration_v23_delete_orphan_message_reads(legacy)

    assert legacy.execute("SELECT COUNT(*) FROM message_reads").fetchone()[0] == 0


def test_v23_deletes_a_read_whose_message_and_user_are_both_gone(legacy):
    _user(legacy, uid="u1", username="ona")
    _read(legacy, "gone", "also-gone")
    database._migration_v23_delete_orphan_message_reads(legacy)

    assert legacy.execute("SELECT COUNT(*) FROM message_reads").fetchone()[0] == 0


def test_v23_keeps_a_read_whose_both_legs_resolve(legacy):
    _user(legacy, uid="u1", username="ona")
    _room(legacy, cid="c1")
    _message(legacy, mid="m1", conversation_id="c1", sender_id="u1")
    _read(legacy, "m1", "u1")
    database._migration_v23_delete_orphan_message_reads(legacy)

    assert legacy.execute("SELECT COUNT(*) FROM message_reads").fetchone()[0] == 1


def test_v23_counts_only_the_orphans(legacy, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    _user(legacy, uid="u1", username="ona")
    _room(legacy, cid="c1")
    _message(legacy, mid="m1", conversation_id="c1", sender_id="u1")
    _read(legacy, "m1", "u1")
    _read(legacy, "ghost-1", "u1")
    _read(legacy, "ghost-2", "u1")
    database._migration_v23_delete_orphan_message_reads(legacy)

    assert "Deleted 2 orphaned message_reads row(s)" in caplog.text


def test_v23_says_nothing_on_a_clean_table(legacy, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    database._migration_v23_delete_orphan_message_reads(legacy)

    assert "orphaned message_reads" not in caplog.text


def test_v23_is_idempotent(legacy, caplog):
    _user(legacy, uid="u1", username="ona")
    _read(legacy, "ghost", "u1")
    database._migration_v23_delete_orphan_message_reads(legacy)

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    database._migration_v23_delete_orphan_message_reads(legacy)

    assert "orphaned message_reads" not in caplog.text


def test_v23_still_sweeps_when_a_message_row_carries_a_null_id(legacy):
    # The NOT IN this migration used to run evaluated to NULL —
    # never true — as soon as the subquery yielded one NULL, so
    # a single NULL messages.id (TEXT PRIMARY KEY permits one)
    # disabled the whole sweep; NOT EXISTS is immune
    _user(legacy, uid="u1", username="ona")
    _room(legacy, cid="c1")
    legacy.execute(
        "INSERT INTO messages (id, conversation_id, sender_id, text) VALUES (NULL, 'c1', 'u1', 'x')"
    )
    _read(legacy, "ghost", "u1")
    database._migration_v23_delete_orphan_message_reads(legacy)

    assert legacy.execute("SELECT COUNT(*) FROM message_reads").fetchone()[0] == 0




# ===========================================================
# v25 — the deleted_source_urls tombstone table
# ===========================================================

def test_v25_creates_the_tombstone_table(legacy):
    assert "deleted_source_urls" not in _tables(legacy)
    database._migration_v25_deleted_source_urls(legacy)

    assert "deleted_source_urls" in _tables(legacy)


def test_v25_makes_source_url_the_primary_key(legacy):
    database._migration_v25_deleted_source_urls(legacy)
    legacy.execute("INSERT INTO deleted_source_urls (source_url) VALUES ('https://knf.vu.lt/a')")

    with pytest.raises(sqlite3.IntegrityError):
        legacy.execute("INSERT INTO deleted_source_urls (source_url) VALUES ('https://knf.vu.lt/a')")


def test_v25_declares_deleted_by_as_set_null(legacy):
    database._migration_v25_deleted_source_urls(legacy)

    assert _on_delete(legacy, "deleted_source_urls", "deleted_by") == "SET NULL"


def test_v25_removing_the_deleting_admin_keeps_the_tombstone(legacy_fk):
    database._migration_v25_deleted_source_urls(legacy_fk)
    admin = _user(legacy_fk, role="admin")
    legacy_fk.execute(
        "INSERT INTO deleted_source_urls (source_url, deleted_by) VALUES ('https://knf.vu.lt/a', ?)",
        (admin,),
    )
    legacy_fk.execute("DELETE FROM users WHERE id = ?", (admin,))

    assert legacy_fk.execute(
        "SELECT deleted_by FROM deleted_source_urls WHERE source_url = 'https://knf.vu.lt/a'"
    ).fetchone() == (None,)


def test_v25_on_a_second_run_keeps_the_rows_already_there(legacy, caplog):
    database._migration_v25_deleted_source_urls(legacy)
    legacy.execute("INSERT INTO deleted_source_urls (source_url) VALUES ('https://knf.vu.lt/b')")

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    database._migration_v25_deleted_source_urls(legacy)

    assert legacy.execute("SELECT COUNT(*) FROM deleted_source_urls").fetchone()[0] == 1
    assert "deleted_source_urls tombstone table ready" in caplog.text




# ===========================================================
# v26 — one poll per post, plus the poll foreign-key indexes
# ===========================================================

def test_v26_keeps_the_oldest_poll_of_a_post(legacy):
    post = _post(legacy)
    _poll(legacy, post, pid="first")
    _poll(legacy, post, pid="second")
    _poll(legacy, post, pid="third")
    database._migration_v26_poll_indexes(legacy)

    assert [r[0] for r in legacy.execute("SELECT id FROM polls").fetchall()] == ["first"]


def test_v26_counts_the_duplicate_polls_it_deleted(legacy, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    post = _post(legacy)
    _poll(legacy, post)
    _poll(legacy, post)
    _poll(legacy, post)
    database._migration_v26_poll_indexes(legacy)

    assert "Deleted 2 duplicate poll(s) — one poll per post from now on" in caplog.text


def test_v26_says_nothing_about_duplicates_when_there_are_none(legacy, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    _poll(legacy, _post(legacy))
    database._migration_v26_poll_indexes(legacy)

    assert "duplicate poll(s)" not in caplog.text


def test_v26_keeps_one_poll_per_post_across_several_posts(legacy):
    post_a, post_b = _post(legacy), _post(legacy)
    _poll(legacy, post_a, pid="a1")
    _poll(legacy, post_a, pid="a2")
    _poll(legacy, post_b, pid="b1")
    database._migration_v26_poll_indexes(legacy)

    assert sorted(r[0] for r in legacy.execute("SELECT id FROM polls").fetchall()) == ["a1", "b1"]


def test_v26_sweeps_options_left_behind_while_foreign_keys_were_off(legacy):
    orphan = _option(legacy, "no-such-poll", oid="orphan-option")
    database._migration_v26_poll_indexes(legacy)

    assert legacy.execute(
        "SELECT COUNT(*) FROM poll_options WHERE id = ?", (orphan,)
    ).fetchone()[0] == 0


def test_v26_sweeps_votes_whose_poll_is_gone(legacy):
    legacy.execute(
        "INSERT INTO poll_votes (user_id, poll_id, option_id) VALUES ('u1', 'ghost-poll', 'o1')"
    )
    database._migration_v26_poll_indexes(legacy)

    assert legacy.execute("SELECT COUNT(*) FROM poll_votes").fetchone()[0] == 0


def test_v26_sweeps_a_vote_whose_option_died_in_the_same_pass(legacy):
    # The option sweep runs BEFORE the option-leg vote sweep, so
    # a vote pointing at an option that is itself orphaned goes
    # too — the order of the three DELETEs is load bearing
    post = _post(legacy)
    poll = _poll(legacy, post)
    dangling = _option(legacy, "no-such-poll", oid="dangling")
    legacy.execute(
        "INSERT INTO poll_votes (user_id, poll_id, option_id) VALUES ('u1', ?, ?)",
        (poll, dangling),
    )
    database._migration_v26_poll_indexes(legacy)

    assert legacy.execute("SELECT COUNT(*) FROM poll_votes").fetchone()[0] == 0


def test_v26_keeps_a_vote_whose_poll_and_option_both_resolve(legacy):
    post = _post(legacy)
    poll = _poll(legacy, post)
    option = _option(legacy, poll)
    legacy.execute(
        "INSERT INTO poll_votes (user_id, poll_id, option_id) VALUES ('u1', ?, ?)",
        (poll, option),
    )
    database._migration_v26_poll_indexes(legacy)

    assert legacy.execute("SELECT COUNT(*) FROM poll_votes").fetchone()[0] == 1


def test_v26_cascades_the_dropped_polls_children_when_enforcement_is_on(legacy_fk):
    post = _post(legacy_fk)
    _poll(legacy_fk, post, pid="keep")
    _poll(legacy_fk, post, pid="drop")
    _option(legacy_fk, "drop", oid="doomed-option")
    legacy_fk.commit()

    database._migration_v26_poll_indexes(legacy_fk)

    assert legacy_fk.execute("SELECT COUNT(*) FROM poll_options").fetchone()[0] == 0


def test_v26_builds_the_unique_index_that_refuses_a_second_poll(legacy):
    post = _post(legacy)
    _poll(legacy, post)
    database._migration_v26_poll_indexes(legacy)

    with pytest.raises(sqlite3.IntegrityError):
        _poll(legacy, post)


def test_v26_creates_the_poll_options_child_index(legacy):
    database._migration_v26_poll_indexes(legacy)

    assert "idx_poll_options_poll" in _indexes(legacy)


def test_v26_on_empty_poll_tables_still_builds_both_indexes(legacy, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    database._migration_v26_poll_indexes(legacy)

    assert "idx_polls_post" in _indexes(legacy)
    assert "Indexed polls(post_id) UNIQUE and poll_options(poll_id)" in caplog.text


def test_v26_is_idempotent(legacy, caplog):
    post = _post(legacy)
    _poll(legacy, post)
    _poll(legacy, post)
    database._migration_v26_poll_indexes(legacy)

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    database._migration_v26_poll_indexes(legacy)

    assert legacy.execute("SELECT COUNT(*) FROM polls").fetchone()[0] == 1
    assert "duplicate poll(s)" not in caplog.text




# ===========================================================
# v35 — canonical news_posts.source_url
# ===========================================================

def test_v35_rewrites_a_url_into_its_canonical_shape(legacy):
    post = _post(legacy, source_url="http://www.knf.vu.lt/naujienos/straipsnis/")
    database._migration_v35_canonical_source_urls(legacy)

    assert legacy.execute("SELECT source_url FROM news_posts WHERE id = ?", (post,)).fetchone()[0] == \
        "https://knf.vu.lt/naujienos/straipsnis"


def test_v35_strips_tracking_parameters_and_the_fragment(legacy):
    post = _post(legacy, source_url="https://knf.vu.lt/a?utm_source=fb&id=7#skyrius")
    database._migration_v35_canonical_source_urls(legacy)

    assert legacy.execute("SELECT source_url FROM news_posts WHERE id = ?", (post,)).fetchone()[0] == \
        "https://knf.vu.lt/a?id=7"


def test_v35_leaves_an_already_canonical_url_untouched(legacy, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    post = _post(legacy, source_url="https://knf.vu.lt/a")
    database._migration_v35_canonical_source_urls(legacy)

    assert legacy.execute("SELECT source_url FROM news_posts WHERE id = ?", (post,)).fetchone()[0] == \
        "https://knf.vu.lt/a"
    assert "Canonicalised 0 source_url value(s), removed 0 duplicate article(s)" in caplog.text


def test_v35_never_considers_a_post_without_a_source_url(legacy):
    post = _post(legacy, source_url=None)
    database._migration_v35_canonical_source_urls(legacy)

    assert legacy.execute("SELECT source_url FROM news_posts WHERE id = ?", (post,)).fetchone()[0] is None


def test_v35_keeps_the_oldest_row_of_a_canonical_collision(legacy):
    _post(legacy, pid="old", source_url="http://www.knf.vu.lt/a")
    _post(legacy, pid="new", source_url="https://knf.vu.lt/a/")
    database._migration_v35_canonical_source_urls(legacy)

    assert [r[0] for r in legacy.execute("SELECT id FROM news_posts").fetchall()] == ["old"]
    assert legacy.execute("SELECT source_url FROM news_posts").fetchone()[0] == "https://knf.vu.lt/a"


def test_v35_deletes_before_it_rewrites_so_the_unique_column_never_collides(legacy):
    # The kept row is about to take exactly the spelling the
    # doomed row still holds; rewriting first would abort the
    # boot on the UNIQUE constraint
    _post(legacy, pid="old", source_url="http://www.knf.vu.lt/a")
    _post(legacy, pid="new", source_url="https://knf.vu.lt/a")
    database._migration_v35_canonical_source_urls(legacy)

    assert legacy.execute("SELECT id, source_url FROM news_posts").fetchall() == \
        [("old", "https://knf.vu.lt/a")]


def test_v35_collapses_three_spellings_of_one_article(legacy, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    _post(legacy, pid="one", source_url="http://www.knf.vu.lt/a/")
    _post(legacy, pid="two", source_url="https://www.knf.vu.lt/a")
    _post(legacy, pid="three", source_url="https://knf.vu.lt/a?utm_medium=mail")
    database._migration_v35_canonical_source_urls(legacy)

    assert [r[0] for r in legacy.execute("SELECT id FROM news_posts").fetchall()] == ["one"]
    assert "Canonicalised 1 source_url value(s), removed 2 duplicate article(s)" in caplog.text


def test_v35_takes_the_duplicates_engagement_with_it(legacy_fk):
    user = _user(legacy_fk)
    _post(legacy_fk, pid="old", source_url="http://www.knf.vu.lt/a")
    _post(legacy_fk, pid="new", source_url="https://knf.vu.lt/a/")
    legacy_fk.execute("INSERT INTO news_likes (user_id, post_id) VALUES (?, 'new')", (user,))
    legacy_fk.execute(
        "INSERT INTO news_comments (id, post_id, user_id, text) VALUES ('cm', 'new', ?, 'k')",
        (user,),
    )
    legacy_fk.commit()

    database._migration_v35_canonical_source_urls(legacy_fk)

    assert legacy_fk.execute("SELECT COUNT(*) FROM news_likes").fetchone()[0] == 0
    assert legacy_fk.execute("SELECT COUNT(*) FROM news_comments").fetchone()[0] == 0


def test_v35_leaves_a_relative_reference_exactly_as_stored(legacy):
    post = _post(legacy, source_url="/naujienos/straipsnis")
    database._migration_v35_canonical_source_urls(legacy)

    assert legacy.execute("SELECT source_url FROM news_posts WHERE id = ?", (post,)).fetchone()[0] == \
        "/naujienos/straipsnis"


def test_v35_on_an_empty_table_reports_two_zeroes(legacy, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    database._migration_v35_canonical_source_urls(legacy)

    assert "Canonicalised 0 source_url value(s), removed 0 duplicate article(s)" in caplog.text


def test_v35_is_idempotent(legacy, caplog):
    _post(legacy, pid="old", source_url="http://www.knf.vu.lt/a/")
    _post(legacy, pid="new", source_url="https://knf.vu.lt/a")
    database._migration_v35_canonical_source_urls(legacy)

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    database._migration_v35_canonical_source_urls(legacy)

    assert "Canonicalised 0 source_url value(s), removed 0 duplicate article(s)" in caplog.text
    assert legacy.execute("SELECT COUNT(*) FROM news_posts").fetchone()[0] == 1


def test_v35_keeps_two_genuinely_different_articles(legacy):
    _post(legacy, pid="a", source_url="https://knf.vu.lt/a")
    _post(legacy, pid="b", source_url="https://knf.vu.lt/b")
    database._migration_v35_canonical_source_urls(legacy)

    assert sorted(r[0] for r in legacy.execute("SELECT id FROM news_posts").fetchall()) == ["a", "b"]




# ===========================================================
# v36 — per-source index and the interrupted-run reconciliation
# ===========================================================

def test_v36_creates_the_source_leading_index(legacy):
    database._migration_v36_scraper_runs_source_index(legacy)
    columns = _indexes(legacy)["idx_scraper_runs_source_started"].split("scraper_runs(", 1)[1]

    assert columns.index("source") < columns.index("started_at")


def test_v36_closes_a_run_left_running(legacy):
    frozen = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    rid = _run(legacy, status="running")
    with time_machine.travel(frozen, tick=False):
        database._migration_v36_scraper_runs_source_index(legacy)
        expected = datetime.now(timezone.utc).isoformat()

    assert legacy.execute(
        "SELECT status, error_message, finished_at FROM scraper_runs WHERE id = ?", (rid,)
    ).fetchone() == ("failed", "interrupted", expected)


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_v36_leaves_a_settled_run_alone(legacy, status):
    rid = _run(legacy, status=status)
    legacy.execute("UPDATE scraper_runs SET error_message = 'original' WHERE id = ?", (rid,))
    database._migration_v36_scraper_runs_source_index(legacy)

    assert legacy.execute(
        "SELECT status, error_message, finished_at FROM scraper_runs WHERE id = ?", (rid,)
    ).fetchone() == (status, "original", None)


def test_v36_counts_every_run_it_closed(legacy, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    for _ in range(3):
        _run(legacy, status="running")
    _run(legacy, status="completed")
    database._migration_v36_scraper_runs_source_index(legacy)

    assert "Closed 3 scraper run(s) left 'running' by an interrupted process" in caplog.text


def test_v36_says_nothing_when_no_run_was_in_flight(legacy, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    _run(legacy, status="completed")
    database._migration_v36_scraper_runs_source_index(legacy)

    assert "Closed" not in caplog.text


def test_v36_on_an_empty_table_only_builds_the_index(legacy, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    database._migration_v36_scraper_runs_source_index(legacy)

    assert "idx_scraper_runs_source_started" in _indexes(legacy)
    assert "Closed" not in caplog.text


def test_v36_is_idempotent(legacy, caplog):
    _run(legacy, status="running")
    database._migration_v36_scraper_runs_source_index(legacy)

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    database._migration_v36_scraper_runs_source_index(legacy)

    assert "Closed" not in caplog.text


def test_v36_keeps_the_started_at_and_source_of_a_closed_run(legacy):
    rid = _run(legacy, source="vu.lt", status="running", started_at="2026-01-01T00:00:00+00:00")
    database._migration_v36_scraper_runs_source_index(legacy)

    assert legacy.execute(
        "SELECT source, started_at FROM scraper_runs WHERE id = ?", (rid,)
    ).fetchone() == ("vu.lt", "2026-01-01T00:00:00+00:00")




# ===========================================================
# v40 — the admin_audit trail table
# ===========================================================

def test_v40_creates_the_table_and_both_indexes(legacy):
    assert "admin_audit" not in _tables(legacy)
    database._migration_v40_admin_audit(legacy)

    assert "admin_audit" in _tables(legacy)
    assert "idx_admin_audit_created" in _indexes(legacy)
    assert "idx_admin_audit_actor" in _indexes(legacy)


def test_v40_accepts_a_row_with_only_its_required_columns(legacy):
    database._migration_v40_admin_audit(legacy)
    legacy.execute("INSERT INTO admin_audit (id, action) VALUES ('a1', 'role_change')")

    assert legacy.execute(
        "SELECT actor_id, target, payload FROM admin_audit WHERE id = 'a1'"
    ).fetchone() == (None, None, None)


def test_v40_refuses_a_row_without_an_action(legacy):
    database._migration_v40_admin_audit(legacy)

    with pytest.raises(sqlite3.IntegrityError):
        legacy.execute("INSERT INTO admin_audit (id, action) VALUES ('a2', NULL)")


def test_v40_declares_actor_id_as_set_null(legacy):
    database._migration_v40_admin_audit(legacy)

    assert _on_delete(legacy, "admin_audit", "actor_id") == "SET NULL"


def test_v40_a_deleted_admin_leaves_their_trail_behind(legacy_fk):
    database._migration_v40_admin_audit(legacy_fk)
    admin = _user(legacy_fk, role="admin")
    legacy_fk.execute(
        "INSERT INTO admin_audit (id, actor_id, action, payload) VALUES ('a3', ?, 'deactivate', '{}')",
        (admin,),
    )
    legacy_fk.execute("DELETE FROM users WHERE id = ?", (admin,))

    assert legacy_fk.execute("SELECT actor_id, action FROM admin_audit WHERE id = 'a3'").fetchone() == \
        (None, "deactivate")


def test_v40_keeps_existing_rows_on_a_second_run(legacy, caplog):
    database._migration_v40_admin_audit(legacy)
    legacy.execute("INSERT INTO admin_audit (id, action) VALUES ('a4', 'broadcast')")

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    database._migration_v40_admin_audit(legacy)

    assert legacy.execute("SELECT COUNT(*) FROM admin_audit").fetchone()[0] == 1
    assert "admin_audit table ready" in caplog.text




# ===========================================================
# v43 — the uploads ownership table
# ===========================================================

def test_v43_creates_the_table_and_its_owner_index(legacy):
    assert "uploads" not in _tables(legacy)
    database._migration_v43_add_uploads_table(legacy)

    assert "uploads" in _tables(legacy)
    assert "idx_uploads_user" in _indexes(legacy)


def test_v43_defaults_byte_size_to_zero(legacy):
    database._migration_v43_add_uploads_table(legacy)
    legacy.execute("INSERT INTO uploads (id, filename) VALUES ('f1', 'a.jpg')")

    assert legacy.execute("SELECT byte_size FROM uploads WHERE id = 'f1'").fetchone()[0] == 0


def test_v43_makes_the_filename_unique(legacy):
    database._migration_v43_add_uploads_table(legacy)
    legacy.execute("INSERT INTO uploads (id, filename) VALUES ('f1', 'a.jpg')")

    with pytest.raises(sqlite3.IntegrityError):
        legacy.execute("INSERT INTO uploads (id, filename) VALUES ('f2', 'a.jpg')")


def test_v43_declares_user_id_as_set_null_so_the_file_stays_findable(legacy_fk):
    database._migration_v43_add_uploads_table(legacy_fk)
    owner = _user(legacy_fk)
    legacy_fk.execute(
        "INSERT INTO uploads (id, filename, user_id, byte_size) VALUES ('f3', 'b.jpg', ?, 1234)",
        (owner,),
    )
    legacy_fk.execute("DELETE FROM users WHERE id = ?", (owner,))

    assert legacy_fk.execute("SELECT filename, user_id FROM uploads WHERE id = 'f3'").fetchone() == \
        ("b.jpg", None)


def test_v43_allows_an_ownerless_row_for_a_file_that_predates_it(legacy):
    database._migration_v43_add_uploads_table(legacy)
    legacy.execute("INSERT INTO uploads (id, filename, user_id) VALUES ('f4', 'c.jpg', NULL)")

    assert legacy.execute("SELECT user_id FROM uploads WHERE id = 'f4'").fetchone()[0] is None


def test_v43_keeps_existing_rows_on_a_second_run(legacy):
    database._migration_v43_add_uploads_table(legacy)
    legacy.execute("INSERT INTO uploads (id, filename) VALUES ('f5', 'd.jpg')")
    database._migration_v43_add_uploads_table(legacy)

    assert legacy.execute("SELECT COUNT(*) FROM uploads").fetchone()[0] == 1




# ===========================================================
# v46 — push_tokens(active, user_id) for the fan-out
# ===========================================================

def test_v46_creates_the_fanout_index_with_the_filter_column_first(legacy):
    database._migration_v46_push_tokens_active_index(legacy)
    columns = _indexes(legacy)["idx_push_tokens_active"].split("push_tokens(", 1)[1]

    assert columns.index("active") < columns.index("user_id")


def test_v46_is_idempotent_and_logs_each_time(legacy, caplog):
    database._migration_v46_push_tokens_active_index(legacy)
    first = _indexes(legacy)

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    database._migration_v46_push_tokens_active_index(legacy)

    assert _indexes(legacy) == first
    assert "Created the push_tokens(active, user_id) index" in caplog.text


def test_v46_does_not_disturb_the_rows_it_indexes(legacy):
    _push_token(legacy, _EXPO_OK)
    database._migration_v46_push_tokens_active_index(legacy)

    assert legacy.execute("SELECT token FROM push_tokens").fetchone()[0] == _EXPO_OK




# ===========================================================
# v47 — purge push tokens the intake grammar now refuses
# ===========================================================

@pytest.mark.parametrize("token", [
    "ExponentPushToken[" + "a" * 10 + "]",
    "ExponentPushToken[" + "a" * 64 + "]",
    "ExponentPushToken[Aa0_-Aa0_-]",
    _EXPO_OK,
])
def test_v47_keeps_a_token_the_grammar_accepts(legacy, token):
    _push_token(legacy, token, tid="keep")
    database._migration_v47_purge_invalid_push_tokens(legacy)

    assert [r[0] for r in legacy.execute("SELECT id FROM push_tokens").fetchall()] == ["keep"]


@pytest.mark.parametrize("token, why", [
    ("ExponentPushToken[" + "a" * 9 + "]", "one character short of the minimum body"),
    ("ExponentPushToken[" + "a" * 65 + "]", "one character past the maximum body"),
    ("ExponentPushToken[]", "empty body"),
    ("ExponentPushToken[abcdefghij", "no closing bracket"),
    ("abcdefghij1234567890]", "no prefix"),
    ("exponentpushtoken[abcdefghij1234567890]", "lower-case prefix"),
    ("ExpoPushToken[abcdefghij1234567890]", "the other Expo prefix"),
    ("ExponentPushToken[abcdefghij.234567890]", "a dot in the body"),
    ("ExponentPushToken[abcdefghij 234567890]", "a space in the body"),
    ("ExponentPushToken[abcdefghij1234567890] ", "trailing space"),
    (" ExponentPushToken[abcdefghij1234567890]", "leading space"),
    ("ExponentPushToken[abcdefghij1234567890]\n", "trailing newline"),
    ("xExponentPushToken[abcdefghij1234567890]", "prefixed junk"),
    ("ExponentPushToken[abcdefghij1234567890]<script>", "markup glued on"),
    ("", "empty string"),
    ("ExponentPushToken[abcdefghij1234567890]; DROP TABLE users", "SQL-looking payload"),
])
def test_v47_deletes_a_token_the_grammar_refuses(legacy, token, why):
    _push_token(legacy, token)
    database._migration_v47_purge_invalid_push_tokens(legacy)

    assert legacy.execute("SELECT COUNT(*) FROM push_tokens").fetchone()[0] == 0, why


@pytest.mark.parametrize("token", [42, 3.5, b"ExponentPushToken[abcdefghij1234567890]"])
def test_v47_deletes_a_token_that_is_not_text_at_all(legacy, token):
    # push_tokens.token is NOT NULL but SQLite does not enforce
    # the declared TEXT type, so an integer, a float or a BLOB
    # can be in the file; the isinstance guard is what keeps
    # fullmatch from raising TypeError on it
    legacy.execute("INSERT INTO push_tokens (id, user_id, token) VALUES ('n', 'u1', ?)", (token,))
    database._migration_v47_purge_invalid_push_tokens(legacy)

    assert legacy.execute("SELECT COUNT(*) FROM push_tokens").fetchone()[0] == 0


def test_v47_keeps_the_good_rows_while_deleting_the_bad_ones(legacy):
    _push_token(legacy, _EXPO_OK, tid="good")
    _push_token(legacy, "garbage", tid="bad-1")
    _push_token(legacy, "ExponentPushToken[!!]", tid="bad-2")
    database._migration_v47_purge_invalid_push_tokens(legacy)

    assert [r[0] for r in legacy.execute("SELECT id FROM push_tokens").fetchall()] == ["good"]


def test_v47_ignores_whether_the_row_is_active(legacy):
    _push_token(legacy, "garbage", tid="inactive", active=0)
    database._migration_v47_purge_invalid_push_tokens(legacy)

    assert legacy.execute("SELECT COUNT(*) FROM push_tokens").fetchone()[0] == 0


def test_v47_counts_the_rows_it_deleted(legacy, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    _push_token(legacy, "junk-1")
    _push_token(legacy, "junk-2")
    _push_token(legacy, _EXPO_OK)
    database._migration_v47_purge_invalid_push_tokens(legacy)

    assert "Deleted 2 malformed push_tokens row(s)" in caplog.text


def test_v47_says_nothing_about_a_clean_table(legacy, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    _push_token(legacy, _EXPO_OK)
    database._migration_v47_purge_invalid_push_tokens(legacy)

    assert "malformed push_tokens" not in caplog.text


def test_v47_on_an_empty_table_issues_no_delete_at_all(legacy):
    spy = _Spy(legacy)
    database._migration_v47_purge_invalid_push_tokens(spy)

    assert spy.deletes() == []


@pytest.mark.parametrize("rows, chunks", [(1, 1), (399, 1), (400, 1), (401, 2), (800, 2), (801, 3)])
def test_v47_chunks_its_deletes_at_four_hundred_ids(legacy, rows, chunks):
    for i in range(rows):
        _push_token(legacy, f"junk-{i}", tid=f"t{i}")
    spy = _Spy(legacy)
    database._migration_v47_purge_invalid_push_tokens(spy)

    assert len(spy.deletes()) == chunks
    assert legacy.execute("SELECT COUNT(*) FROM push_tokens").fetchone()[0] == 0


def test_v47_is_idempotent(legacy, caplog):
    _push_token(legacy, "junk")
    database._migration_v47_purge_invalid_push_tokens(legacy)

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    database._migration_v47_purge_invalid_push_tokens(legacy)

    assert "malformed push_tokens" not in caplog.text




# ===========================================================
# v49 — demote planted multi-member 'direct' rooms
# ===========================================================

def _members(conn, count):
    return [_user(conn) for _ in range(count)]


def test_v49_demotes_a_three_member_direct_room(legacy):
    room = _room(legacy, kind="direct", members=_members(legacy, 3))
    database._migration_v49_direct_room_audit(legacy)

    assert legacy.execute("SELECT type FROM conversations WHERE id = ?", (room,)).fetchone()[0] == "group"


def test_v49_gives_a_titleless_demoted_room_the_lithuanian_fallback(legacy):
    room = _room(legacy, kind="direct", title=None, members=_members(legacy, 3))
    database._migration_v49_direct_room_audit(legacy)

    assert legacy.execute("SELECT title FROM conversations WHERE id = ?", (room,)).fetchone()[0] == "Grupė"


def test_v49_replaces_an_empty_title_with_the_fallback(legacy):
    room = _room(legacy, kind="direct", title="", members=_members(legacy, 3))
    database._migration_v49_direct_room_audit(legacy)

    assert legacy.execute("SELECT title FROM conversations WHERE id = ?", (room,)).fetchone()[0] == "Grupė"


def test_v49_keeps_a_title_the_room_already_had(legacy):
    room = _room(legacy, kind="direct", title="Projektas", members=_members(legacy, 4))
    database._migration_v49_direct_room_audit(legacy)

    assert legacy.execute("SELECT title FROM conversations WHERE id = ?", (room,)).fetchone()[0] == \
        "Projektas"


@pytest.mark.parametrize("count", [0, 1, 2])
def test_v49_leaves_a_room_with_two_or_fewer_members_direct(legacy, count):
    room = _room(legacy, kind="direct", members=_members(legacy, count))
    database._migration_v49_direct_room_audit(legacy)

    assert legacy.execute(
        "SELECT type, title FROM conversations WHERE id = ?", (room,)
    ).fetchone() == ("direct", None)


def test_v49_treats_three_members_as_the_first_qualifying_size(legacy):
    two = _room(legacy, kind="direct", members=_members(legacy, 2))
    three = _room(legacy, kind="direct", members=_members(legacy, 3))
    database._migration_v49_direct_room_audit(legacy)

    assert legacy.execute("SELECT type FROM conversations WHERE id = ?", (two,)).fetchone()[0] == "direct"
    assert legacy.execute("SELECT type FROM conversations WHERE id = ?", (three,)).fetchone()[0] == "group"


def test_v49_never_touches_a_room_that_is_already_a_group(legacy):
    room = _room(legacy, kind="group", title=None, members=_members(legacy, 5))
    database._migration_v49_direct_room_audit(legacy)

    assert legacy.execute("SELECT type, title FROM conversations WHERE id = ?", (room,)).fetchone() == \
        ("group", None)


def test_v49_counts_only_the_rooms_it_demoted(legacy, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    _room(legacy, kind="direct", members=_members(legacy, 3))
    _room(legacy, kind="direct", members=_members(legacy, 7))
    _room(legacy, kind="direct", members=_members(legacy, 2))
    database._migration_v49_direct_room_audit(legacy)

    assert "Demoted 2 multi-member 'direct' conversation(s) to 'group'" in caplog.text


def test_v49_says_nothing_when_no_room_qualifies(legacy, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    _room(legacy, kind="direct", members=_members(legacy, 2))
    database._migration_v49_direct_room_audit(legacy)

    assert "Demoted" not in caplog.text


def test_v49_is_idempotent(legacy, caplog):
    _room(legacy, kind="direct", members=_members(legacy, 3))
    database._migration_v49_direct_room_audit(legacy)

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    database._migration_v49_direct_room_audit(legacy)

    assert "Demoted" not in caplog.text


def test_v49_leaves_the_participant_rows_exactly_as_they_were(legacy):
    people = _members(legacy, 3)
    room = _room(legacy, kind="direct", members=people)
    database._migration_v49_direct_room_audit(legacy)

    assert sorted(
        r[0] for r in legacy.execute(
            "SELECT user_id FROM conversation_participants WHERE conversation_id = ?", (room,)
        ).fetchall()
    ) == sorted(people)




# ===========================================================
# v55 — composite news_posts(author_id, source, published_at)
# ===========================================================

def test_v55_creates_the_composite_profile_index(legacy):
    database._migration_v55_news_posts_author_index(legacy)

    assert "idx_news_posts_author_source" in _indexes(legacy)


def test_v55_orders_the_index_columns_filter_first_then_sort(legacy):
    database._migration_v55_news_posts_author_index(legacy)
    columns = _indexes(legacy)["idx_news_posts_author_source"].split("news_posts(", 1)[1]

    assert columns.index("author_id") < columns.index("source") < columns.index("published_at")
    assert "DESC" in columns


def test_v55_is_idempotent_and_logs_each_time(legacy, caplog):
    database._migration_v55_news_posts_author_index(legacy)
    first = _indexes(legacy)

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    database._migration_v55_news_posts_author_index(legacy)

    assert _indexes(legacy) == first
    assert "Created the composite news_posts(author_id, source, published_at) index" in caplog.text


def test_v55_leaves_the_single_column_author_index_in_place(legacy):
    database._migration_v15_add_fk_indexes(legacy)
    database._migration_v55_news_posts_author_index(legacy)
    names = _indexes(legacy)

    assert "idx_news_posts_author" in names
    assert "idx_news_posts_author_source" in names


def test_v55_index_is_the_one_the_profile_query_plan_picks(legacy):
    database._migration_v15_add_fk_indexes(legacy)
    database._migration_v55_news_posts_author_index(legacy)
    plan = legacy.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM news_posts WHERE author_id = 'x'"
        " AND source IN ('user', 'faculty') ORDER BY published_at DESC"
    ).fetchall()

    assert any("idx_news_posts_author_source" in str(row) for row in plan)




# ===========================================================
# cross-cutting — every late migration is a committing no-op
# on a database that has already had it
# ===========================================================

_LATE_MIGRATIONS = (
    database._migration_v12_expire_bootstrap_invite,
    database._migration_v13_hash_session_tokens,
    database._migration_v14_reconcile_counters,
    database._migration_v15_add_fk_indexes,
    database._migration_v16_rebuild_fk_actions,
    database._migration_v17_normalize_timestamps,
    database._migration_v18_schedule_lessons_indexes,
    database._migration_v19_scraper_runs_index,
    database._migration_v20_messages_fts,
    database._migration_v21_friend_requests_pending_unique,
    database._migration_v22_drop_duplicate_indexes,
    database._migration_v23_delete_orphan_message_reads,
    database._migration_v25_deleted_source_urls,
    database._migration_v26_poll_indexes,
    database._migration_v35_canonical_source_urls,
    database._migration_v36_scraper_runs_source_index,
    database._migration_v40_admin_audit,
    database._migration_v43_add_uploads_table,
    database._migration_v46_push_tokens_active_index,
    database._migration_v47_purge_invalid_push_tokens,
    database._migration_v49_direct_room_audit,
    database._migration_v55_news_posts_author_index,
)


@pytest.mark.parametrize("migration", _LATE_MIGRATIONS, ids=lambda fn: fn.__name__)
def test_every_late_migration_returns_none(legacy, migration):
    assert migration(legacy) is None


@pytest.mark.parametrize("migration", _LATE_MIGRATIONS, ids=lambda fn: fn.__name__)
def test_every_late_migration_leaves_no_open_transaction(legacy, migration):
    migration(legacy)

    assert legacy.in_transaction is False


@pytest.mark.parametrize("migration", _LATE_MIGRATIONS, ids=lambda fn: fn.__name__)
def test_every_late_migration_survives_a_third_consecutive_run(legacy, migration):
    migration(legacy)
    migration(legacy)
    migration(legacy)

    assert legacy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_the_whole_late_chain_in_order_leaves_a_consistent_database(tmp_path):
    path = tmp_path / "chain.db"
    conn = _open_legacy(path, foreign_keys=True)
    try:
        author = _user(conn, username="ona")
        _post(conn, author_id=author, source_url="http://www.knf.vu.lt/a/")
        _session(conn, "raw-token", user_id=author)
        _invite(conn, "WELCOME-KNF-2026", "2099-01-01T00:00:00+00:00")
        _lesson(conn)
        _lesson(conn)
        _run(conn, status="running")
        _push_token(conn, "not-a-token", user_id=author)
        _room(conn, kind="direct", members=[author, _user(conn), _user(conn)])
        conn.commit()

        for migration in _LATE_MIGRATIONS:
            migration(conn)

        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM schedule_lessons").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM push_tokens").fetchone()[0] == 0
        assert conn.execute("SELECT type FROM conversations").fetchone()[0] == "group"
    finally:
        conn.close()
