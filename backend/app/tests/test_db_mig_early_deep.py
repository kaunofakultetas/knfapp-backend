# -----------------------------------------------------------
#  [*] Tests — app/database: migrations v1 to v11, exhaustively
#
#  The early chain is what turns a database written before
#  2026 into one this app can boot on. Nothing here trusts the
#  fresh-database path: every test hand-builds a LEGACY file
#  with the old table shapes, so each ALTER and each CREATE
#  actually fires instead of being skipped as a duplicate, and
#  then drives ONE migration function against it.
#
#  What each section proves:
#
#    v1/v2  RETIRED to recorded no-ops. They must not touch a
#           byte — not an oversized title, not a relative
#           avatar_url, not a genuine "&amp;" a user typed —
#           and must not even commit, because a replay of the
#           old bodies would destroy exactly those things.
#    v3     users.invited arrives NOT NULL DEFAULT 1 (every
#           pre-v3 account came through an invite), which is
#           the opposite of _SCHEMA's DEFAULT 0; a second run
#           must not promote a guest back to invited.
#    v4     faculty_info with its UNIQUE(lang, section); an
#           existing table of another shape is left alone.
#    v5     the three student-ID columns, added one at a time,
#           so a half-migrated table gets only what it lacks.
#    v6     push_tokens WITHOUT language (v11's job), with the
#           unique token index and the cascade off users.
#    v7     notification_channels: four CHECKed channels, zero
#           rows planted (missing row == enabled), and no
#           index of its own — the documented gap.
#    v8     users.active NOT NULL DEFAULT 1; a second run must
#           not reactivate a banned account.
#    v9     messages.reply_to_id (self-FK, ON DELETE SET NULL)
#           and deleted_at, each skipped independently.
#    v10    messages.client_msg_id plus the unique send-nonce
#           index: NULLs never collide, '' does.
#    v11    push_tokens.language NOT NULL DEFAULT 'lt'; a
#           second run must not reset an 'en' device.
#
#  Around that: every guard clause both ways (duplicate column
#  swallowed, readonly / missing table / view re-raised), the
#  commit each function does or does not issue, the whole
#  v1..v11 chain end to end on one legacy file, a replay of
#  that chain after the _migrations rows are deleted, and the
#  ordering that matters (v11 before v6 aborts).
# -----------------------------------------------------------

import logging
import sqlite3

import pytest

import app.database as database


LOGGER_NAME = "app.database"




# -----------------------------------------------------------
# _restore_db_path
# -----------------------------------------------------------
#
# A couple of tests down the bottom call the real init_db(),
# which pins a MODULE-level _db_path that get_db() reads. Put
# the previous value back so those calls cannot leak into the
# next test in the session.
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _restore_db_path():
    previous = database._db_path
    yield
    database._db_path = previous




# -----------------------------------------------------------
# Legacy table shapes
# -----------------------------------------------------------
#
# The pre-migration DDL, assembled per test from the fragments
# a given migration needs. Deliberately older than v11: no
# users.invited / student fields / active, no messages
# reply/delete/nonce columns, push_tokens without language.
# -----------------------------------------------------------

_L_USERS = """
CREATE TABLE users (
    id TEXT PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    email TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'student',
    avatar_url TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_L_NEWS_POSTS = """
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
"""

_L_MESSAGES = """
CREATE TABLE messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    image_url TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_L_PUSH_TOKENS = """
CREATE TABLE push_tokens (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT 'unknown',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_push_tokens_user ON push_tokens(user_id);
CREATE UNIQUE INDEX idx_push_tokens_token ON push_tokens(token);
"""




# -----------------------------------------------------------
# _Legacy / legacy
# -----------------------------------------------------------
#
#   conn  = legacy.make(_L_USERS)        — a fresh file
#   peer  = legacy.peer(conn)            — a SECOND connection
#                                          to the same file,
#                                          which does NOT
#                                          commit the first
#   ro    = legacy.readonly(conn)        — the same file opened
#                                          read-only
#
# Every connection it hands out is closed at teardown, and
# foreign_keys is ON on the writable ones because that is how
# init_db runs its migrations.
# -----------------------------------------------------------

class _Legacy:

    def __init__(self, tmp_path):
        self._tmp = tmp_path
        self._conns = []
        self._n = 0

    def make(self, ddl=""):
        self._n += 1
        path = self._tmp / f"legacy-{self._n}.db"
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys=ON")
        if ddl:
            conn.executescript(ddl)
        conn.commit()
        self._conns.append(conn)
        return conn

    def peer(self, conn):
        other = sqlite3.connect(_file_of(conn))
        other.execute("PRAGMA foreign_keys=ON")
        self._conns.append(other)
        return other

    def readonly(self, conn):
        other = sqlite3.connect(f"file:{_file_of(conn)}?mode=ro", uri=True)
        self._conns.append(other)
        return other

    def close_all(self):
        for conn in self._conns:
            conn.close()


@pytest.fixture
def legacy(tmp_path):
    factory = _Legacy(tmp_path)
    yield factory
    factory.close_all()




# -----------------------------------------------------------
# _RaisingConnection
# -----------------------------------------------------------
#
# A stand-in connection for the two failure shapes no real
# SQLite file reproduces on demand: an OperationalError whose
# message is cased differently from the literal the guard
# compares against, and a hard failure on the SECOND statement
# of a multi-statement migration. `fails` is a predicate over
# the SQL; everything it says no to is recorded and succeeds.
# -----------------------------------------------------------

class _RaisingConnection:

    def __init__(self, message, fails=None):
        self.message = message
        self.fails = fails if fails is not None else (lambda sql: True)
        self.executed = []
        self.commits = 0

    def execute(self, sql, *args):
        self.executed.append(sql)
        if self.fails(sql):
            raise sqlite3.OperationalError(self.message)
        return None

    def commit(self):
        self.commits += 1




# -----------------------------------------------------------
# Schema readers
# -----------------------------------------------------------
#
# Thin wrappers over the PRAGMAs, so an assertion reads like a
# sentence instead of an index into a tuple. table_info rows
# are (cid, name, type, notnull, dflt_value, pk);
# foreign_key_list rows are (id, seq, table, from, to,
# on_update, on_delete, match).
# -----------------------------------------------------------

def _file_of(conn):
    return conn.execute("PRAGMA database_list").fetchall()[0][2]


def _columns(conn, table):
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _column(conn, table, name):
    for row in conn.execute(f"PRAGMA table_info({table})").fetchall():
        if row[1] == name:
            return {"type": row[2], "notnull": row[3], "default": row[4], "pk": row[5]}
    return None


def _indexes(conn, table):
    return {row[1]: bool(row[2]) for row in conn.execute(f"PRAGMA index_list({table})").fetchall()}


def _index_columns(conn, index):
    return [row[2] for row in conn.execute(f"PRAGMA index_info({index})").fetchall()]


def _foreign_keys(conn, table):
    return {row[3]: (row[2], row[4], row[6])
            for row in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()}


def _tables(conn):
    return {row[0] for row in
            conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}




# -----------------------------------------------------------
# _snapshot
# -----------------------------------------------------------
#
# Everything a replay must not change: the ordered rows of
# every table plus the schema text itself. Comparing two
# snapshots is how the idempotency tests below say "nothing
# moved" without listing columns by hand.
# -----------------------------------------------------------

def _snapshot(conn, tables):
    state = {"schema": sorted(
        row[0] for row in conn.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL").fetchall())}
    for table in tables:
        state[table] = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
    return state




# ===========================================================
# v1 — retired to a recorded no-op
# ===========================================================

# The payloads the ORIGINAL v1 body would have mangled: markup
# it escaped, an oversized title it truncated, and an app-local
# avatar it NULLed for not being http(s).
_V1_VICTIMS = [
    ("p-script", "<script>alert(1)</script>", "<b>bold</b> & <i>italic</i>"),
    ("p-entity", "Tom &amp; Jerry", "already &lt;escaped&gt;"),
    ("p-quote", 'He said "hi" & left', "it's fine"),
    ("p-long", "T" * 50000, "C" * 200000),
    ("p-unicode", "Šiauliai — ąčęėįšųūž", "emoji \U0001f600 stays"),
]


def _plant_v1_victims(conn):
    conn.execute(
        "INSERT INTO users (id, username, email, display_name, password_hash, avatar_url)"
        " VALUES ('u1', 'ona', 'ona@knf.vu.lt', '<b>Ona</b>', 'x', '/api/uploads/ona.png')")
    for post_id, title, content in _V1_VICTIMS:
        conn.execute(
            "INSERT INTO news_posts (id, title, content, author_id) VALUES (?, ?, ?, 'u1')",
            (post_id, title, content))
    conn.commit()


def test_v1_leaves_every_user_text_column_byte_identical(legacy):
    conn = legacy.make(_L_USERS + _L_NEWS_POSTS)
    _plant_v1_victims(conn)

    database._migration_v1_xss_cleanup(conn)

    stored = dict(conn.execute("SELECT id, title FROM news_posts").fetchall())
    for post_id, title, _content in _V1_VICTIMS:
        assert stored[post_id] == title


def test_v1_does_not_truncate_an_oversized_title_or_body(legacy):
    conn = legacy.make(_L_USERS + _L_NEWS_POSTS)
    _plant_v1_victims(conn)

    database._migration_v1_xss_cleanup(conn)

    row = conn.execute("SELECT title, content FROM news_posts WHERE id = 'p-long'").fetchone()
    assert len(row[0]) == 50000
    assert len(row[1]) == 200000


def test_v1_does_not_null_the_apps_own_relative_avatar_url(legacy):
    conn = legacy.make(_L_USERS + _L_NEWS_POSTS)
    _plant_v1_victims(conn)

    database._migration_v1_xss_cleanup(conn)

    assert conn.execute("SELECT avatar_url FROM users").fetchone()[0] == "/api/uploads/ona.png"


def test_v1_needs_no_tables_at_all(legacy):
    conn = legacy.make()

    database._migration_v1_xss_cleanup(conn)

    assert _tables(conn) == set()


def test_v1_returns_nothing(legacy):
    assert database._migration_v1_xss_cleanup(legacy.make()) is None


def test_v1_logs_exactly_one_retirement_line(legacy, caplog):
    conn = legacy.make()

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        database._migration_v1_xss_cleanup(conn)

    retired = [r for r in caplog.records if "v1 is retired" in r.message]
    assert len(retired) == 1
    assert retired[0].levelno == logging.INFO


def test_v1_never_commits_what_the_caller_left_pending(legacy):
    conn = legacy.make(_L_USERS)
    conn.execute(
        "INSERT INTO users (id, username, email, display_name, password_hash)"
        " VALUES ('u1', 'ona', 'ona@knf.vu.lt', 'Ona', 'x')")

    database._migration_v1_xss_cleanup(conn)

    assert conn.in_transaction
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def test_v1_stays_a_no_op_however_many_times_it_runs(legacy):
    conn = legacy.make(_L_USERS + _L_NEWS_POSTS)
    _plant_v1_victims(conn)
    before = _snapshot(conn, ["users", "news_posts"])

    for _ in range(5):
        database._migration_v1_xss_cleanup(conn)

    assert _snapshot(conn, ["users", "news_posts"]) == before




# ===========================================================
# v2 — retired to a recorded no-op
# ===========================================================

def test_v2_leaves_an_ampersand_entity_a_user_genuinely_typed(legacy):
    conn = legacy.make(_L_USERS + _L_NEWS_POSTS)
    _plant_v1_victims(conn)

    database._migration_v2_unescape_double_escapes(conn)

    assert conn.execute(
        "SELECT title FROM news_posts WHERE id = 'p-entity'").fetchone()[0] == "Tom &amp; Jerry"


def test_v2_leaves_double_escaped_text_exactly_as_stored(legacy):
    conn = legacy.make(_L_USERS + _L_NEWS_POSTS)
    conn.execute(
        "INSERT INTO users (id, username, email, display_name, password_hash)"
        " VALUES ('u1', 'ona', 'ona@knf.vu.lt', 'Ona', 'x')")
    conn.execute("INSERT INTO news_posts (id, title, content) VALUES ('p', ?, ?)",
                 ("&amp;amp;lt;b&amp;amp;gt;", "&amp;amp;amp;"))
    conn.commit()

    database._migration_v2_unescape_double_escapes(conn)

    row = conn.execute("SELECT title, content FROM news_posts WHERE id = 'p'").fetchone()
    assert row == ("&amp;amp;lt;b&amp;amp;gt;", "&amp;amp;amp;")


def test_v2_needs_no_tables_at_all(legacy):
    conn = legacy.make()

    database._migration_v2_unescape_double_escapes(conn)

    assert _tables(conn) == set()


def test_v2_returns_nothing(legacy):
    assert database._migration_v2_unescape_double_escapes(legacy.make()) is None


def test_v2_logs_exactly_one_retirement_line(legacy, caplog):
    conn = legacy.make()

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        database._migration_v2_unescape_double_escapes(conn)

    retired = [r for r in caplog.records if "v2 is retired" in r.message]
    assert len(retired) == 1
    assert retired[0].levelno == logging.INFO


def test_v2_never_commits_what_the_caller_left_pending(legacy):
    conn = legacy.make(_L_USERS)
    conn.execute(
        "INSERT INTO users (id, username, email, display_name, password_hash)"
        " VALUES ('u1', 'ona', 'ona@knf.vu.lt', 'Ona', 'x')")

    database._migration_v2_unescape_double_escapes(conn)

    assert conn.in_transaction
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def test_v2_stays_a_no_op_however_many_times_it_runs(legacy):
    conn = legacy.make(_L_USERS + _L_NEWS_POSTS)
    _plant_v1_victims(conn)
    before = _snapshot(conn, ["users", "news_posts"])

    for _ in range(5):
        database._migration_v2_unescape_double_escapes(conn)

    assert _snapshot(conn, ["users", "news_posts"]) == before




# ===========================================================
# v3 — users.invited
# ===========================================================

def _plant_user(conn, user_id="u1", username="ona", **extra):
    columns = ["id", "username", "email", "display_name", "password_hash"]
    values = [user_id, username, f"{username}@knf.vu.lt", username.title(), "x"]
    for key, value in extra.items():
        columns.append(key)
        values.append(value)
    conn.execute(f"INSERT INTO users ({', '.join(columns)})"
                 f" VALUES ({', '.join('?' * len(values))})", values)
    conn.commit()
    return user_id


def test_v3_adds_the_invited_column_to_a_legacy_users_table(legacy):
    conn = legacy.make(_L_USERS)
    assert "invited" not in _columns(conn, "users")

    database._migration_v3_add_invited_column(conn)

    assert "invited" in _columns(conn, "users")


def test_v3_marks_every_account_that_predates_it_as_invited(legacy):
    conn = legacy.make(_L_USERS)
    for i in range(3):
        _plant_user(conn, f"u{i}", f"user{i}")

    database._migration_v3_add_invited_column(conn)

    assert [row[0] for row in conn.execute("SELECT invited FROM users").fetchall()] == [1, 1, 1]


def test_v3_declares_invited_not_null_defaulting_to_one(legacy):
    conn = legacy.make(_L_USERS)

    database._migration_v3_add_invited_column(conn)

    column = _column(conn, "users", "invited")
    assert column["type"] == "INTEGER"
    assert column["notnull"] == 1
    assert column["default"] == "1"


def test_v3_defaults_a_new_row_to_invited_unlike_the_fresh_schema(legacy):
    migrated = legacy.make(_L_USERS)
    database._migration_v3_add_invited_column(migrated)
    _plant_user(migrated)

    fresh = legacy.make(database._SCHEMA)

    assert migrated.execute("SELECT invited FROM users").fetchone()[0] == 1
    assert _column(fresh, "users", "invited")["default"] == "0"


def test_v3_refuses_an_explicit_null_invited(legacy):
    conn = legacy.make(_L_USERS)
    database._migration_v3_add_invited_column(conn)

    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
        _plant_user(conn, invited=None)


def test_v3_skips_the_alter_when_the_column_is_already_there(legacy, caplog):
    conn = legacy.make(_L_USERS)
    database._migration_v3_add_invited_column(conn)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        database._migration_v3_add_invited_column(conn)

    assert "'invited' column already exists, skipping" in caplog.text


def test_v3_does_not_promote_a_guest_back_to_invited_on_a_second_run(legacy):
    conn = legacy.make(_L_USERS)
    database._migration_v3_add_invited_column(conn)
    _plant_user(conn, "guest", "svecias", invited=0)

    database._migration_v3_add_invited_column(conn)

    assert conn.execute("SELECT invited FROM users WHERE id = 'guest'").fetchone()[0] == 0


def test_v3_aborts_when_the_users_table_is_missing(legacy):
    conn = legacy.make()

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        database._migration_v3_add_invited_column(conn)


def test_v3_aborts_on_a_readonly_database(legacy):
    conn = legacy.make(_L_USERS)

    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        database._migration_v3_add_invited_column(legacy.readonly(conn))


def test_v3_aborts_when_users_is_a_view_rather_than_a_table(legacy):
    conn = legacy.make(_L_USERS.replace("TABLE users", "TABLE people"))
    conn.executescript("CREATE VIEW users AS SELECT * FROM people")

    with pytest.raises(sqlite3.OperationalError, match="view"):
        database._migration_v3_add_invited_column(conn)


def test_v3_commits_the_work_the_caller_left_pending(legacy):
    conn = legacy.make(_L_USERS)
    conn.execute(
        "INSERT INTO users (id, username, email, display_name, password_hash)"
        " VALUES ('u1', 'ona', 'ona@knf.vu.lt', 'Ona', 'x')")
    peer = legacy.peer(conn)
    assert peer.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0

    database._migration_v3_add_invited_column(conn)

    assert peer.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_v3_commits_even_when_the_alter_was_skipped(legacy):
    conn = legacy.make(_L_USERS)
    database._migration_v3_add_invited_column(conn)
    conn.execute(
        "INSERT INTO users (id, username, email, display_name, password_hash)"
        " VALUES ('u1', 'ona', 'ona@knf.vu.lt', 'Ona', 'x')")
    peer = legacy.peer(conn)

    database._migration_v3_add_invited_column(conn)

    assert peer.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_v3_matches_the_duplicate_column_message_case_insensitively(caplog):
    conn = _RaisingConnection("DUPLICATE COLUMN NAME: invited")

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        database._migration_v3_add_invited_column(conn)

    assert "already exists, skipping" in caplog.text
    assert conn.commits == 1




# ===========================================================
# v4 — faculty_info
# ===========================================================

def test_v4_creates_faculty_info_on_a_database_that_lacks_it(legacy):
    conn = legacy.make()

    database._migration_v4_add_faculty_info_table(conn)

    assert "faculty_info" in _tables(conn)


def test_v4_gives_faculty_info_its_documented_columns(legacy):
    conn = legacy.make()

    database._migration_v4_add_faculty_info_table(conn)

    assert _columns(conn, "faculty_info") == ["id", "lang", "section", "data_json", "scraped_at"]


def test_v4_defaults_the_language_to_lithuanian(legacy):
    conn = legacy.make()
    database._migration_v4_add_faculty_info_table(conn)

    conn.execute("INSERT INTO faculty_info (id, section, data_json) VALUES ('f1', 'contacts', '{}')")

    assert conn.execute("SELECT lang FROM faculty_info").fetchone()[0] == "lt"


def test_v4_stamps_scraped_at_in_the_legacy_space_form(legacy):
    conn = legacy.make()
    database._migration_v4_add_faculty_info_table(conn)

    conn.execute("INSERT INTO faculty_info (id, section, data_json) VALUES ('f1', 'contacts', '{}')")

    stamped = conn.execute("SELECT scraped_at FROM faculty_info").fetchone()[0]
    assert " " in stamped and "T" not in stamped


def test_v4_uniques_the_language_and_section_pair(legacy):
    conn = legacy.make()
    database._migration_v4_add_faculty_info_table(conn)
    conn.execute("INSERT INTO faculty_info (id, lang, section, data_json)"
                 " VALUES ('f1', 'lt', 'contacts', '{}')")

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        conn.execute("INSERT INTO faculty_info (id, lang, section, data_json)"
                     " VALUES ('f2', 'lt', 'contacts', '{}')")


def test_v4_allows_the_same_section_in_another_language(legacy):
    conn = legacy.make()
    database._migration_v4_add_faculty_info_table(conn)

    conn.execute("INSERT INTO faculty_info (id, lang, section, data_json)"
                 " VALUES ('f1', 'lt', 'contacts', '{}')")
    conn.execute("INSERT INTO faculty_info (id, lang, section, data_json)"
                 " VALUES ('f2', 'en', 'contacts', '{}')")

    assert conn.execute("SELECT COUNT(*) FROM faculty_info").fetchone()[0] == 2


def test_v4_requires_the_scraped_payload(legacy):
    conn = legacy.make()
    database._migration_v4_add_faculty_info_table(conn)

    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
        conn.execute("INSERT INTO faculty_info (id, section) VALUES ('f1', 'contacts')")


def test_v4_accepts_a_null_id_because_a_text_primary_key_is_nullable(legacy):
    conn = legacy.make()
    database._migration_v4_add_faculty_info_table(conn)

    conn.execute("INSERT INTO faculty_info (id, section, data_json) VALUES (NULL, 'contacts', '{}')")

    assert conn.execute("SELECT id FROM faculty_info").fetchone()[0] is None


def test_v4_keeps_the_rows_it_finds_when_it_runs_again(legacy):
    conn = legacy.make()
    database._migration_v4_add_faculty_info_table(conn)
    conn.execute("INSERT INTO faculty_info (id, section, data_json) VALUES ('f1', 'programs', '[1]')")
    conn.commit()

    database._migration_v4_add_faculty_info_table(conn)

    assert conn.execute("SELECT data_json FROM faculty_info").fetchone()[0] == "[1]"


def test_v4_leaves_a_faculty_info_table_of_another_shape_untouched(legacy):
    conn = legacy.make("CREATE TABLE faculty_info (id TEXT PRIMARY KEY, junk TEXT)")

    database._migration_v4_add_faculty_info_table(conn)

    assert _columns(conn, "faculty_info") == ["id", "junk"]


def test_v4_commits_the_table_it_created(legacy):
    conn = legacy.make()
    peer = legacy.peer(conn)

    database._migration_v4_add_faculty_info_table(conn)

    assert "faculty_info" in _tables(peer)


def test_v4_is_safe_to_run_many_times(legacy):
    conn = legacy.make()
    for _ in range(4):
        database._migration_v4_add_faculty_info_table(conn)

    assert _columns(conn, "faculty_info") == ["id", "lang", "section", "data_json", "scraped_at"]




# ===========================================================
# v5 — the three student-ID columns
# ===========================================================

_V5_COLUMNS = ["student_number", "study_group", "study_program"]


def test_v5_adds_all_three_student_columns_to_a_legacy_users_table(legacy):
    conn = legacy.make(_L_USERS)

    database._migration_v5_add_student_fields(conn)

    assert set(_V5_COLUMNS) <= set(_columns(conn, "users"))


def test_v5_appends_the_columns_in_the_documented_order(legacy):
    conn = legacy.make(_L_USERS)

    database._migration_v5_add_student_fields(conn)

    assert _columns(conn, "users")[-3:] == _V5_COLUMNS


@pytest.mark.parametrize("column", _V5_COLUMNS)
def test_v5_types_each_column_as_nullable_text(legacy, column):
    conn = legacy.make(_L_USERS)

    database._migration_v5_add_student_fields(conn)

    described = _column(conn, "users", column)
    assert described["type"] == "TEXT"
    assert described["notnull"] == 0
    assert described["default"] == "NULL"


def test_v5_leaves_the_new_columns_null_on_accounts_that_predate_it(legacy):
    conn = legacy.make(_L_USERS)
    _plant_user(conn)

    database._migration_v5_add_student_fields(conn)

    row = conn.execute("SELECT student_number, study_group, study_program FROM users").fetchone()
    assert row == (None, None, None)


def test_v5_adds_only_the_columns_a_half_migrated_table_lacks(legacy, caplog):
    conn = legacy.make(_L_USERS)
    conn.execute("ALTER TABLE users ADD COLUMN study_group TEXT DEFAULT NULL")
    conn.commit()

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        database._migration_v5_add_student_fields(conn)

    assert "'study_group' column already exists" in caplog.text
    assert "'student_number' column already exists" not in caplog.text
    assert set(_V5_COLUMNS) <= set(_columns(conn, "users"))


def test_v5_skips_all_three_on_a_second_run(legacy, caplog):
    conn = legacy.make(_L_USERS)
    database._migration_v5_add_student_fields(conn)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        database._migration_v5_add_student_fields(conn)

    skipped = [r for r in caplog.records if "already exists, skipping" in r.message]
    assert len(skipped) == 3


def test_v5_preserves_values_a_half_migrated_table_already_stored(legacy):
    conn = legacy.make(_L_USERS)
    conn.execute("ALTER TABLE users ADD COLUMN student_number TEXT DEFAULT NULL")
    _plant_user(conn, student_number="20250001")

    database._migration_v5_add_student_fields(conn)

    assert conn.execute("SELECT student_number FROM users").fetchone()[0] == "20250001"


@pytest.mark.parametrize("value", ["", "0", "x" * 4096, "ĄČĘ-2025/1", "20250001\n"])
def test_v5_constrains_nothing_about_a_student_number(legacy, value):
    conn = legacy.make(_L_USERS)
    database._migration_v5_add_student_fields(conn)

    _plant_user(conn, student_number=value)

    assert conn.execute("SELECT student_number FROM users").fetchone()[0] == value


def test_v5_aborts_when_the_users_table_is_missing(legacy):
    conn = legacy.make()

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        database._migration_v5_add_student_fields(conn)


def test_v5_aborts_on_a_readonly_database(legacy):
    conn = legacy.make(_L_USERS)

    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        database._migration_v5_add_student_fields(legacy.readonly(conn))


def test_v5_stops_at_the_first_hard_failure_and_never_commits(legacy):
    conn = _RaisingConnection("attempt to write a readonly database",
                              fails=lambda sql: "study_group" in sql)

    with pytest.raises(sqlite3.OperationalError):
        database._migration_v5_add_student_fields(conn)

    assert len(conn.executed) == 2
    assert "student_number" in conn.executed[0]
    assert conn.commits == 0


def test_v5_commits_even_when_every_alter_was_skipped(legacy):
    conn = legacy.make(_L_USERS)
    database._migration_v5_add_student_fields(conn)
    conn.execute(
        "INSERT INTO users (id, username, email, display_name, password_hash)"
        " VALUES ('u1', 'ona', 'ona@knf.vu.lt', 'Ona', 'x')")
    peer = legacy.peer(conn)

    database._migration_v5_add_student_fields(conn)

    assert peer.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1




# ===========================================================
# v6 — push_tokens
# ===========================================================

def test_v6_creates_push_tokens_on_a_database_that_lacks_it(legacy):
    conn = legacy.make(_L_USERS)

    database._migration_v6_add_push_tokens(conn)

    assert "push_tokens" in _tables(conn)


def test_v6_creates_the_table_without_the_language_column_v11_adds(legacy):
    conn = legacy.make(_L_USERS)

    database._migration_v6_add_push_tokens(conn)

    assert _columns(conn, "push_tokens") == [
        "id", "user_id", "token", "platform", "active", "created_at", "updated_at"]


def test_v6_creates_both_documented_indexes(legacy):
    conn = legacy.make(_L_USERS)

    database._migration_v6_add_push_tokens(conn)

    indexes = _indexes(conn, "push_tokens")
    assert indexes["idx_push_tokens_user"] is False
    assert indexes["idx_push_tokens_token"] is True
    assert _index_columns(conn, "idx_push_tokens_token") == ["token"]


def test_v6_refuses_the_same_expo_token_twice(legacy):
    conn = legacy.make(_L_USERS)
    database._migration_v6_add_push_tokens(conn)
    _plant_user(conn)
    conn.execute("INSERT INTO push_tokens (id, user_id, token) VALUES ('t1', 'u1', 'EXPO')")

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        conn.execute("INSERT INTO push_tokens (id, user_id, token) VALUES ('t2', 'u1', 'EXPO')")


def test_v6_lets_two_devices_of_one_user_coexist(legacy):
    conn = legacy.make(_L_USERS)
    database._migration_v6_add_push_tokens(conn)
    _plant_user(conn)

    conn.execute("INSERT INTO push_tokens (id, user_id, token) VALUES ('t1', 'u1', 'A')")
    conn.execute("INSERT INTO push_tokens (id, user_id, token) VALUES ('t2', 'u1', 'B')")

    assert conn.execute("SELECT COUNT(*) FROM push_tokens").fetchone()[0] == 2


def test_v6_defaults_a_device_to_unknown_platform_and_active(legacy):
    conn = legacy.make(_L_USERS)
    database._migration_v6_add_push_tokens(conn)
    _plant_user(conn)

    conn.execute("INSERT INTO push_tokens (id, user_id, token) VALUES ('t1', 'u1', 'A')")

    assert conn.execute("SELECT platform, active FROM push_tokens").fetchone() == ("unknown", 1)


def test_v6_requires_a_token(legacy):
    conn = legacy.make(_L_USERS)
    database._migration_v6_add_push_tokens(conn)
    _plant_user(conn)

    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
        conn.execute("INSERT INTO push_tokens (id, user_id, token) VALUES ('t1', 'u1', NULL)")


def test_v6_cascades_a_device_away_with_its_user(legacy):
    conn = legacy.make(_L_USERS)
    database._migration_v6_add_push_tokens(conn)
    _plant_user(conn)
    conn.execute("INSERT INTO push_tokens (id, user_id, token) VALUES ('t1', 'u1', 'A')")

    conn.execute("DELETE FROM users WHERE id = 'u1'")

    assert conn.execute("SELECT COUNT(*) FROM push_tokens").fetchone()[0] == 0
    assert _foreign_keys(conn, "push_tokens")["user_id"] == ("users", "id", "CASCADE")


def test_v6_refuses_a_device_for_a_user_that_does_not_exist(legacy):
    conn = legacy.make(_L_USERS)
    database._migration_v6_add_push_tokens(conn)

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute("INSERT INTO push_tokens (id, user_id, token) VALUES ('t1', 'ghost', 'A')")


def test_v6_creates_the_table_even_without_a_users_table_to_reference(legacy):
    # SQLite resolves a foreign key at write time, not at
    # CREATE time — the boot survives, the first INSERT does not
    conn = legacy.make()

    database._migration_v6_add_push_tokens(conn)

    assert "push_tokens" in _tables(conn)
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        conn.execute("INSERT INTO push_tokens (id, user_id, token) VALUES ('t1', 'u1', 'A')")


def test_v6_keeps_the_devices_it_finds_when_it_runs_again(legacy):
    conn = legacy.make(_L_USERS + _L_PUSH_TOKENS)
    _plant_user(conn)
    conn.execute("INSERT INTO push_tokens (id, user_id, token) VALUES ('t1', 'u1', 'A')")
    conn.commit()

    database._migration_v6_add_push_tokens(conn)

    assert conn.execute("SELECT token FROM push_tokens").fetchone()[0] == "A"


def test_v6_aborts_when_a_push_tokens_table_of_another_shape_shadows_its_own(legacy):
    # IF NOT EXISTS keeps the foreign table, and then the index
    # over the columns it does not have fails — unlike v4, which
    # creates no index and so passes silently over the same trap
    conn = legacy.make("CREATE TABLE push_tokens (id TEXT PRIMARY KEY, junk TEXT)")

    with pytest.raises(sqlite3.OperationalError, match="no such column"):
        database._migration_v6_add_push_tokens(conn)

    assert _columns(conn, "push_tokens") == ["id", "junk"]


def test_v6_aborts_when_a_legacy_table_already_holds_duplicate_tokens(legacy):
    # No production file can reach this — the unique index has
    # shipped with the table since v6 — but the failure must be
    # loud rather than a silently unindexed table
    conn = legacy.make("CREATE TABLE push_tokens (id TEXT PRIMARY KEY, user_id TEXT,"
                       " token TEXT NOT NULL)")
    conn.execute("INSERT INTO push_tokens VALUES ('t1', 'u1', 'SAME'), ('t2', 'u1', 'SAME')")
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        database._migration_v6_add_push_tokens(conn)


def test_v6_commits_the_table_it_created(legacy):
    conn = legacy.make(_L_USERS)
    peer = legacy.peer(conn)

    database._migration_v6_add_push_tokens(conn)

    assert "push_tokens" in _tables(peer)




# ===========================================================
# v7 — notification_channels
# ===========================================================

def test_v7_creates_notification_channels_on_a_database_that_lacks_it(legacy):
    conn = legacy.make(_L_USERS)

    database._migration_v7_add_notification_channels(conn)

    assert _columns(conn, "notification_channels") == [
        "user_id", "channel", "enabled", "updated_at"]


def test_v7_plants_no_rows_so_every_channel_starts_enabled(legacy):
    conn = legacy.make(_L_USERS)
    _plant_user(conn)

    database._migration_v7_add_notification_channels(conn)

    assert conn.execute("SELECT COUNT(*) FROM notification_channels").fetchone()[0] == 0


def test_v7_defaults_an_opt_in_row_to_enabled(legacy):
    conn = legacy.make(_L_USERS)
    database._migration_v7_add_notification_channels(conn)
    _plant_user(conn)

    conn.execute("INSERT INTO notification_channels (user_id, channel) VALUES ('u1', 'news')")

    assert conn.execute("SELECT enabled FROM notification_channels").fetchone()[0] == 1


@pytest.mark.parametrize("channel", ["news", "chat", "schedule", "admin"])
def test_v7_accepts_each_documented_channel(legacy, channel):
    conn = legacy.make(_L_USERS)
    database._migration_v7_add_notification_channels(conn)
    _plant_user(conn)

    conn.execute("INSERT INTO notification_channels (user_id, channel) VALUES ('u1', ?)", (channel,))

    assert conn.execute("SELECT channel FROM notification_channels").fetchone()[0] == channel


@pytest.mark.parametrize("channel", ["push", "News", "NEWS", "news ", " news", "", "newsletter", 0])
def test_v7_refuses_a_channel_outside_the_four(legacy, channel):
    conn = legacy.make(_L_USERS)
    database._migration_v7_add_notification_channels(conn)
    _plant_user(conn)

    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute("INSERT INTO notification_channels (user_id, channel) VALUES ('u1', ?)",
                     (channel,))


def test_v7_refuses_a_null_channel(legacy):
    conn = legacy.make(_L_USERS)
    database._migration_v7_add_notification_channels(conn)
    _plant_user(conn)

    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
        conn.execute("INSERT INTO notification_channels (user_id, channel) VALUES ('u1', NULL)")


def test_v7_keys_one_row_per_user_and_channel(legacy):
    conn = legacy.make(_L_USERS)
    database._migration_v7_add_notification_channels(conn)
    _plant_user(conn)
    conn.execute("INSERT INTO notification_channels (user_id, channel) VALUES ('u1', 'news')")

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        conn.execute("INSERT INTO notification_channels (user_id, channel) VALUES ('u1', 'news')")


def test_v7_lets_one_user_hold_all_four_channels(legacy):
    conn = legacy.make(_L_USERS)
    database._migration_v7_add_notification_channels(conn)
    _plant_user(conn)

    for channel in ("news", "chat", "schedule", "admin"):
        conn.execute("INSERT INTO notification_channels (user_id, channel, enabled)"
                     " VALUES ('u1', ?, 0)", (channel,))

    assert conn.execute("SELECT COUNT(*) FROM notification_channels").fetchone()[0] == 4


def test_v7_cascades_the_preferences_away_with_their_user(legacy):
    conn = legacy.make(_L_USERS)
    database._migration_v7_add_notification_channels(conn)
    _plant_user(conn)
    conn.execute("INSERT INTO notification_channels (user_id, channel) VALUES ('u1', 'chat')")

    conn.execute("DELETE FROM users WHERE id = 'u1'")

    assert conn.execute("SELECT COUNT(*) FROM notification_channels").fetchone()[0] == 0


def test_v7_creates_no_index_of_its_own_beyond_the_primary_key(legacy):
    # the documented gap: _SCHEMA adds a user_id index, this
    # migration does not, so a pre-v7 file never gets one
    conn = legacy.make(_L_USERS)

    database._migration_v7_add_notification_channels(conn)

    assert all(name.startswith("sqlite_autoindex")
               for name in _indexes(conn, "notification_channels"))


def test_v7_keeps_the_preferences_it_finds_when_it_runs_again(legacy):
    conn = legacy.make(_L_USERS)
    database._migration_v7_add_notification_channels(conn)
    _plant_user(conn)
    conn.execute("INSERT INTO notification_channels (user_id, channel, enabled)"
                 " VALUES ('u1', 'news', 0)")
    conn.commit()

    database._migration_v7_add_notification_channels(conn)

    assert conn.execute("SELECT enabled FROM notification_channels").fetchone()[0] == 0


def test_v7_commits_the_table_it_created(legacy):
    conn = legacy.make(_L_USERS)
    peer = legacy.peer(conn)

    database._migration_v7_add_notification_channels(conn)

    assert "notification_channels" in _tables(peer)




# ===========================================================
# v8 — users.active
# ===========================================================

def test_v8_adds_the_active_column_to_a_legacy_users_table(legacy):
    conn = legacy.make(_L_USERS)

    database._migration_v8_add_active_column(conn)

    assert "active" in _columns(conn, "users")


def test_v8_marks_every_account_that_predates_it_active(legacy):
    conn = legacy.make(_L_USERS)
    for i in range(3):
        _plant_user(conn, f"u{i}", f"user{i}")

    database._migration_v8_add_active_column(conn)

    assert [row[0] for row in conn.execute("SELECT active FROM users").fetchall()] == [1, 1, 1]


def test_v8_declares_active_not_null_defaulting_to_one(legacy):
    conn = legacy.make(_L_USERS)

    database._migration_v8_add_active_column(conn)

    column = _column(conn, "users", "active")
    assert column["type"] == "INTEGER"
    assert column["notnull"] == 1
    assert column["default"] == "1"


def test_v8_refuses_an_explicit_null_active(legacy):
    conn = legacy.make(_L_USERS)
    database._migration_v8_add_active_column(conn)

    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
        _plant_user(conn, active=None)


@pytest.mark.parametrize("value", [0, 1, 2, -1])
def test_v8_stores_any_integer_in_active_because_nothing_checks_it(legacy, value):
    conn = legacy.make(_L_USERS)
    database._migration_v8_add_active_column(conn)

    _plant_user(conn, active=value)

    assert conn.execute("SELECT active FROM users").fetchone()[0] == value


def test_v8_skips_the_alter_when_the_column_is_already_there(legacy, caplog):
    conn = legacy.make(_L_USERS)
    database._migration_v8_add_active_column(conn)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        database._migration_v8_add_active_column(conn)

    assert "'active' column already exists, skipping" in caplog.text


def test_v8_does_not_reactivate_a_banned_account_on_a_second_run(legacy):
    conn = legacy.make(_L_USERS)
    database._migration_v8_add_active_column(conn)
    _plant_user(conn, "banned", "blogas", active=0)

    database._migration_v8_add_active_column(conn)

    assert conn.execute("SELECT active FROM users WHERE id = 'banned'").fetchone()[0] == 0


def test_v8_aborts_when_the_users_table_is_missing(legacy):
    conn = legacy.make()

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        database._migration_v8_add_active_column(conn)


def test_v8_aborts_on_a_readonly_database(legacy):
    conn = legacy.make(_L_USERS)

    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        database._migration_v8_add_active_column(legacy.readonly(conn))


def test_v8_commits_the_work_the_caller_left_pending(legacy):
    conn = legacy.make(_L_USERS)
    conn.execute(
        "INSERT INTO users (id, username, email, display_name, password_hash)"
        " VALUES ('u1', 'ona', 'ona@knf.vu.lt', 'Ona', 'x')")
    peer = legacy.peer(conn)

    database._migration_v8_add_active_column(conn)

    assert peer.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1




# ===========================================================
# v9 — messages.reply_to_id + deleted_at
# ===========================================================

def _plant_message(conn, message_id, conversation="c1", sender="s1", **extra):
    columns = ["id", "conversation_id", "sender_id", "text"]
    values = [message_id, conversation, sender, "labas"]
    for key, value in extra.items():
        columns.append(key)
        values.append(value)
    conn.execute(f"INSERT INTO messages ({', '.join(columns)})"
                 f" VALUES ({', '.join('?' * len(values))})", values)


def test_v9_adds_both_message_columns(legacy):
    conn = legacy.make(_L_MESSAGES)

    database._migration_v9_add_reply_and_delete(conn)

    assert _columns(conn, "messages")[-2:] == ["reply_to_id", "deleted_at"]


def test_v9_declares_reply_to_id_as_a_self_reference_that_sets_null(legacy):
    conn = legacy.make(_L_MESSAGES)

    database._migration_v9_add_reply_and_delete(conn)

    assert _foreign_keys(conn, "messages")["reply_to_id"] == ("messages", "id", "SET NULL")


def test_v9_nulls_a_quote_when_the_message_it_quoted_is_deleted(legacy):
    conn = legacy.make(_L_MESSAGES)
    database._migration_v9_add_reply_and_delete(conn)
    _plant_message(conn, "m1")
    _plant_message(conn, "m2", reply_to_id="m1")

    conn.execute("DELETE FROM messages WHERE id = 'm1'")

    assert conn.execute("SELECT reply_to_id FROM messages WHERE id = 'm2'").fetchone()[0] is None


def test_v9_refuses_a_quote_of_a_message_that_never_existed(legacy):
    conn = legacy.make(_L_MESSAGES)
    database._migration_v9_add_reply_and_delete(conn)

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        _plant_message(conn, "m1", reply_to_id="ghost")


def test_v9_lets_a_message_quote_itself(legacy):
    conn = legacy.make(_L_MESSAGES)
    database._migration_v9_add_reply_and_delete(conn)

    _plant_message(conn, "m1", reply_to_id="m1")

    assert conn.execute("SELECT reply_to_id FROM messages").fetchone()[0] == "m1"


def test_v9_leaves_both_columns_null_on_messages_that_predate_it(legacy):
    conn = legacy.make(_L_MESSAGES)
    _plant_message(conn, "m1")
    conn.commit()

    database._migration_v9_add_reply_and_delete(conn)

    assert conn.execute("SELECT reply_to_id, deleted_at FROM messages").fetchone() == (None, None)


def test_v9_adds_only_the_column_a_half_migrated_table_lacks(legacy, caplog):
    conn = legacy.make(_L_MESSAGES)
    conn.execute("ALTER TABLE messages ADD COLUMN deleted_at TEXT")
    conn.commit()

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        database._migration_v9_add_reply_and_delete(conn)

    skipped = [r for r in caplog.records if "column already exists, skipping" in r.message]
    assert len(skipped) == 1
    assert "deleted_at" in skipped[0].getMessage()
    assert "reply_to_id" in _columns(conn, "messages")


def test_v9_skips_both_columns_on_a_second_run(legacy, caplog):
    conn = legacy.make(_L_MESSAGES)
    database._migration_v9_add_reply_and_delete(conn)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        database._migration_v9_add_reply_and_delete(conn)

    skipped = [r for r in caplog.records if "column already exists, skipping" in r.message]
    assert len(skipped) == 2


def test_v9_logs_the_statement_it_actually_ran(legacy, caplog):
    conn = legacy.make(_L_MESSAGES)

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        database._migration_v9_add_reply_and_delete(conn)

    assert "ALTER TABLE messages ADD COLUMN reply_to_id" in caplog.text
    assert "ALTER TABLE messages ADD COLUMN deleted_at TEXT" in caplog.text


def test_v9_aborts_when_the_messages_table_is_missing(legacy):
    conn = legacy.make()

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        database._migration_v9_add_reply_and_delete(conn)


def test_v9_aborts_on_a_readonly_database(legacy):
    conn = legacy.make(_L_MESSAGES)

    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        database._migration_v9_add_reply_and_delete(legacy.readonly(conn))


def test_v9_stops_at_the_second_statement_when_it_fails_hard(legacy):
    conn = _RaisingConnection("attempt to write a readonly database",
                              fails=lambda sql: "deleted_at" in sql)

    with pytest.raises(sqlite3.OperationalError):
        database._migration_v9_add_reply_and_delete(conn)

    assert len(conn.executed) == 2
    assert conn.commits == 0


def test_v9_finishes_the_job_when_a_failed_boot_is_retried(legacy):
    conn = legacy.make(_L_MESSAGES)
    conn.execute("ALTER TABLE messages ADD COLUMN reply_to_id TEXT"
                 " REFERENCES messages(id) ON DELETE SET NULL")
    conn.commit()

    database._migration_v9_add_reply_and_delete(conn)

    assert "deleted_at" in _columns(conn, "messages")


def test_v9_commits_the_work_the_caller_left_pending(legacy):
    conn = legacy.make(_L_MESSAGES)
    _plant_message(conn, "m1")
    peer = legacy.peer(conn)

    database._migration_v9_add_reply_and_delete(conn)

    assert peer.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1




# ===========================================================
# v10 — messages.client_msg_id + the send-nonce index
# ===========================================================

def test_v10_adds_client_msg_id_and_its_unique_index(legacy):
    conn = legacy.make(_L_MESSAGES)

    database._migration_v10_add_client_msg_id(conn)

    assert "client_msg_id" in _columns(conn, "messages")
    assert _indexes(conn, "messages")["idx_messages_client_msg"] is True


def test_v10_indexes_the_three_natural_key_columns_in_order(legacy):
    conn = legacy.make(_L_MESSAGES)

    database._migration_v10_add_client_msg_id(conn)

    assert _index_columns(conn, "idx_messages_client_msg") == [
        "conversation_id", "sender_id", "client_msg_id"]


def test_v10_refuses_a_replayed_nonce_from_the_same_sender_in_the_same_room(legacy):
    conn = legacy.make(_L_MESSAGES)
    database._migration_v10_add_client_msg_id(conn)
    _plant_message(conn, "m1", client_msg_id="nonce-1")

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        _plant_message(conn, "m2", client_msg_id="nonce-1")


def test_v10_accepts_the_same_nonce_from_another_sender(legacy):
    conn = legacy.make(_L_MESSAGES)
    database._migration_v10_add_client_msg_id(conn)
    _plant_message(conn, "m1", sender="s1", client_msg_id="nonce-1")

    _plant_message(conn, "m2", sender="s2", client_msg_id="nonce-1")

    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2


def test_v10_accepts_the_same_nonce_in_another_conversation(legacy):
    conn = legacy.make(_L_MESSAGES)
    database._migration_v10_add_client_msg_id(conn)
    _plant_message(conn, "m1", conversation="c1", client_msg_id="nonce-1")

    _plant_message(conn, "m2", conversation="c2", client_msg_id="nonce-1")

    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2


def test_v10_never_collides_two_null_nonces(legacy):
    conn = legacy.make(_L_MESSAGES)
    database._migration_v10_add_client_msg_id(conn)

    for i in range(25):
        _plant_message(conn, f"m{i}")

    assert conn.execute(
        "SELECT COUNT(*) FROM messages WHERE client_msg_id IS NULL").fetchone()[0] == 25


def test_v10_treats_an_empty_string_nonce_as_a_real_value(legacy):
    # '' is NOT NULL, so the second send with a blank nonce
    # collides where a missing nonce would not
    conn = legacy.make(_L_MESSAGES)
    database._migration_v10_add_client_msg_id(conn)
    _plant_message(conn, "m1", client_msg_id="")

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        _plant_message(conn, "m2", client_msg_id="")


def test_v10_leaves_the_nonce_null_on_messages_that_predate_it(legacy):
    conn = legacy.make(_L_MESSAGES)
    _plant_message(conn, "m1")
    conn.commit()

    database._migration_v10_add_client_msg_id(conn)

    assert conn.execute("SELECT client_msg_id FROM messages").fetchone()[0] is None


def test_v10_still_builds_the_index_when_only_the_column_survived(legacy, caplog):
    conn = legacy.make(_L_MESSAGES)
    conn.execute("ALTER TABLE messages ADD COLUMN client_msg_id TEXT")
    conn.commit()

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        database._migration_v10_add_client_msg_id(conn)

    assert "'client_msg_id' column already exists, skipping" in caplog.text
    assert "idx_messages_client_msg" in _indexes(conn, "messages")


def test_v10_leaves_the_index_alone_on_a_second_run(legacy):
    conn = legacy.make(_L_MESSAGES)
    database._migration_v10_add_client_msg_id(conn)
    _plant_message(conn, "m1", client_msg_id="nonce-1")
    conn.commit()

    database._migration_v10_add_client_msg_id(conn)

    assert _index_columns(conn, "idx_messages_client_msg") == [
        "conversation_id", "sender_id", "client_msg_id"]
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


def test_v10_aborts_when_the_column_already_holds_duplicates_it_cannot_index(legacy):
    conn = legacy.make(_L_MESSAGES)
    conn.execute("ALTER TABLE messages ADD COLUMN client_msg_id TEXT")
    _plant_message(conn, "m1", client_msg_id="nonce-1")
    _plant_message(conn, "m2", client_msg_id="nonce-1")
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        database._migration_v10_add_client_msg_id(conn)


def test_v10_aborts_when_the_messages_table_is_missing(legacy):
    conn = legacy.make()

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        database._migration_v10_add_client_msg_id(conn)


def test_v10_aborts_on_a_readonly_database(legacy):
    conn = legacy.make(_L_MESSAGES)

    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        database._migration_v10_add_client_msg_id(legacy.readonly(conn))


def test_v10_commits_the_work_the_caller_left_pending(legacy):
    conn = legacy.make(_L_MESSAGES)
    _plant_message(conn, "m1")
    peer = legacy.peer(conn)

    database._migration_v10_add_client_msg_id(conn)

    assert peer.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1




# ===========================================================
# v11 — push_tokens.language
# ===========================================================

def _plant_device(conn, token_id="t1", user="u1", token="A", **extra):
    columns = ["id", "user_id", "token"]
    values = [token_id, user, token]
    for key, value in extra.items():
        columns.append(key)
        values.append(value)
    conn.execute(f"INSERT INTO push_tokens ({', '.join(columns)})"
                 f" VALUES ({', '.join('?' * len(values))})", values)


def test_v11_adds_the_language_column_to_a_legacy_push_tokens_table(legacy):
    conn = legacy.make(_L_USERS + _L_PUSH_TOKENS)

    database._migration_v11_add_push_language(conn)

    assert "language" in _columns(conn, "push_tokens")


def test_v11_declares_language_not_null_defaulting_to_lithuanian(legacy):
    conn = legacy.make(_L_USERS + _L_PUSH_TOKENS)

    database._migration_v11_add_push_language(conn)

    column = _column(conn, "push_tokens", "language")
    assert column["type"] == "TEXT"
    assert column["notnull"] == 1
    assert column["default"] == "'lt'"


def test_v11_defaults_every_device_that_predates_it_to_lithuanian(legacy):
    conn = legacy.make(_L_USERS + _L_PUSH_TOKENS)
    _plant_user(conn)
    _plant_device(conn, "t1", token="A")
    _plant_device(conn, "t2", token="B")
    conn.commit()

    database._migration_v11_add_push_language(conn)

    assert [row[0] for row in conn.execute("SELECT language FROM push_tokens").fetchall()] == \
        ["lt", "lt"]


def test_v11_defaults_a_newly_registered_device_to_lithuanian(legacy):
    conn = legacy.make(_L_USERS + _L_PUSH_TOKENS)
    database._migration_v11_add_push_language(conn)
    _plant_user(conn)

    _plant_device(conn)

    assert conn.execute("SELECT language FROM push_tokens").fetchone()[0] == "lt"


def test_v11_stores_english_when_the_device_registers_in_english(legacy):
    conn = legacy.make(_L_USERS + _L_PUSH_TOKENS)
    database._migration_v11_add_push_language(conn)
    _plant_user(conn)

    _plant_device(conn, language="en")

    assert conn.execute("SELECT language FROM push_tokens").fetchone()[0] == "en"


def test_v11_refuses_an_explicit_null_language(legacy):
    conn = legacy.make(_L_USERS + _L_PUSH_TOKENS)
    database._migration_v11_add_push_language(conn)
    _plant_user(conn)

    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
        _plant_device(conn, language=None)


@pytest.mark.parametrize("language", ["", "LT", "lt-LT", "zz", "x" * 512])
def test_v11_constrains_nothing_about_the_language_string(legacy, language):
    conn = legacy.make(_L_USERS + _L_PUSH_TOKENS)
    database._migration_v11_add_push_language(conn)
    _plant_user(conn)

    _plant_device(conn, language=language)

    assert conn.execute("SELECT language FROM push_tokens").fetchone()[0] == language


def test_v11_skips_the_alter_when_the_column_is_already_there(legacy, caplog):
    conn = legacy.make(_L_USERS + _L_PUSH_TOKENS)
    database._migration_v11_add_push_language(conn)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        database._migration_v11_add_push_language(conn)

    assert "'language' column already exists, skipping" in caplog.text


def test_v11_does_not_reset_an_english_device_on_a_second_run(legacy):
    conn = legacy.make(_L_USERS + _L_PUSH_TOKENS)
    database._migration_v11_add_push_language(conn)
    _plant_user(conn)
    _plant_device(conn, language="en")
    conn.commit()

    database._migration_v11_add_push_language(conn)

    assert conn.execute("SELECT language FROM push_tokens").fetchone()[0] == "en"


def test_v11_aborts_when_push_tokens_does_not_exist_yet(legacy):
    # exactly what running v11 before v6 would do
    conn = legacy.make(_L_USERS)

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        database._migration_v11_add_push_language(conn)


def test_v11_aborts_on_a_readonly_database(legacy):
    conn = legacy.make(_L_USERS + _L_PUSH_TOKENS)

    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        database._migration_v11_add_push_language(legacy.readonly(conn))


def test_v11_commits_the_work_the_caller_left_pending(legacy):
    conn = legacy.make(_L_USERS + _L_PUSH_TOKENS)
    _plant_user(conn)
    _plant_device(conn)
    peer = legacy.peer(conn)

    database._migration_v11_add_push_language(conn)

    assert peer.execute("SELECT COUNT(*) FROM push_tokens").fetchone()[0] == 1




# ===========================================================
# The chain: v1 to v11 over one hand-built legacy file
# ===========================================================

_EARLY_CHAIN = [
    database._migration_v1_xss_cleanup,
    database._migration_v2_unescape_double_escapes,
    database._migration_v3_add_invited_column,
    database._migration_v4_add_faculty_info_table,
    database._migration_v5_add_student_fields,
    database._migration_v6_add_push_tokens,
    database._migration_v7_add_notification_channels,
    database._migration_v8_add_active_column,
    database._migration_v9_add_reply_and_delete,
    database._migration_v10_add_client_msg_id,
    database._migration_v11_add_push_language,
]


# The oldest shape this app has ever been deployed on: users
# without invited/student/active, messages without the reply,
# delete or nonce columns, and no push or notification tables
# at all.
_PRE_V3 = _L_USERS + _L_NEWS_POSTS + _L_MESSAGES


def _plant_legacy_content(conn):
    _plant_user(conn, "u1", "ona")
    _plant_user(conn, "u2", "jonas")
    conn.execute("INSERT INTO news_posts (id, title, content, author_id)"
                 " VALUES ('p1', 'Tom &amp; Jerry', '<b>labas</b>', 'u1')")
    _plant_message(conn, "m1", sender="u1")
    _plant_message(conn, "m2", sender="u2")
    conn.commit()


def _run_early_chain(conn):
    for migration in _EARLY_CHAIN:
        migration(conn)


def test_the_early_chain_migrates_one_legacy_file_end_to_end(legacy):
    conn = legacy.make(_PRE_V3)
    _plant_legacy_content(conn)

    _run_early_chain(conn)

    assert set(_V5_COLUMNS) | {"invited", "active"} <= set(_columns(conn, "users"))
    assert {"reply_to_id", "deleted_at", "client_msg_id"} <= set(_columns(conn, "messages"))
    assert {"faculty_info", "push_tokens", "notification_channels"} <= _tables(conn)
    assert "language" in _columns(conn, "push_tokens")


def test_the_early_chain_keeps_every_legacy_row(legacy):
    conn = legacy.make(_PRE_V3)
    _plant_legacy_content(conn)

    _run_early_chain(conn)

    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
    assert conn.execute("SELECT title, content FROM news_posts").fetchone() == \
        ("Tom &amp; Jerry", "<b>labas</b>")


def test_the_early_chain_leaves_every_legacy_account_trusted_and_enabled(legacy):
    conn = legacy.make(_PRE_V3)
    _plant_legacy_content(conn)

    _run_early_chain(conn)

    assert conn.execute("SELECT COUNT(*) FROM users WHERE invited = 1 AND active = 1"
                        ).fetchone()[0] == 2


def test_running_the_early_chain_twice_changes_nothing(legacy):
    conn = legacy.make(_PRE_V3)
    _plant_legacy_content(conn)
    _run_early_chain(conn)
    tables = sorted(_tables(conn))
    before = _snapshot(conn, tables)

    _run_early_chain(conn)

    assert _snapshot(conn, tables) == before


def test_the_early_chain_leaves_a_migrated_file_free_of_foreign_key_violations(legacy):
    conn = legacy.make(_PRE_V3)
    _plant_legacy_content(conn)

    _run_early_chain(conn)

    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_the_early_chain_is_the_only_thing_that_touches_a_prehistoric_file(legacy):
    # v1 and v2 run FIRST and must leave the payloads they once
    # rewrote exactly where the chain found them
    conn = legacy.make(_PRE_V3)
    conn.execute("INSERT INTO users (id, username, email, display_name, password_hash, avatar_url)"
                 " VALUES ('u1', 'ona', 'ona@knf.vu.lt', 'Ona', 'x', '/api/uploads/a.png')")
    conn.execute("INSERT INTO news_posts (id, title, content) VALUES ('p1', ?, ?)",
                 ("<script>x</script>", "&amp;lt;"))
    conn.commit()

    _run_early_chain(conn)

    assert conn.execute("SELECT avatar_url FROM users").fetchone()[0] == "/api/uploads/a.png"
    assert conn.execute("SELECT title, content FROM news_posts").fetchone() == \
        ("<script>x</script>", "&amp;lt;")




# ===========================================================
# The chain through the real boot
# ===========================================================

def test_a_fresh_boot_records_every_early_version(tmp_path):
    path = tmp_path / "fresh.db"

    database.init_db(str(path))

    conn = sqlite3.connect(path)
    try:
        applied = {row[0] for row in conn.execute("SELECT version FROM _migrations").fetchall()}
    finally:
        conn.close()
    assert set(range(1, 12)) <= applied


def test_a_boot_replays_the_early_chain_harmlessly_after_its_version_rows_are_deleted(tmp_path):
    path = tmp_path / "replay.db"
    database.init_db(str(path))

    conn = sqlite3.connect(path)
    try:
        conn.execute("INSERT INTO news_posts (id, title, content, source_url)"
                     " VALUES ('p1', 'Tom &amp; Jerry', ?, 'https://knf.vu.lt/a')",
                     ("<b>x</b>" + "y" * 30000,))
        conn.execute("UPDATE users SET avatar_url = '/api/uploads/a.png' WHERE username = 'admin'")
        conn.execute("DELETE FROM _migrations WHERE version <= 11")
        conn.commit()
        before = _snapshot(conn, ["users", "news_posts"])
    finally:
        conn.close()

    database.init_db(str(path))

    conn = sqlite3.connect(path)
    try:
        applied = {row[0] for row in conn.execute("SELECT version FROM _migrations").fetchall()}
        assert set(range(1, 12)) <= applied
        assert _snapshot(conn, ["users", "news_posts"]) == before
    finally:
        conn.close()


def test_a_boot_over_a_hand_built_legacy_file_applies_the_early_chain(tmp_path):
    path = tmp_path / "legacy-boot.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_PRE_V3)
        _plant_legacy_content(conn)
    finally:
        conn.close()

    database.init_db(str(path))

    conn = sqlite3.connect(path)
    try:
        assert set(_V5_COLUMNS) | {"invited", "active"} <= set(_columns(conn, "users"))
        assert "language" in _columns(conn, "push_tokens")
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2
        # an existing file with users is never re-seeded
        assert conn.execute(
            "SELECT COUNT(*) FROM users WHERE username = 'admin'").fetchone()[0] == 0
    finally:
        conn.close()
