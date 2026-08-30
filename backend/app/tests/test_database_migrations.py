# -----------------------------------------------------------
#  [*] Tests — app/database: schema, seeding and migrations
#
#  Migrations are data transformations, so this module tests
#  them as such. It builds a LEGACY database BY HAND — the old
#  table shapes, deliberately older than v11 so every ALTER
#  actually fires instead of being skipped as a "duplicate
#  column" — plants representative rows in it, runs the real
#  init_db() over that file, and then asserts what each
#  migration DID to the data:
#
#    v3/v5/v8/v9/v10/v11  the columns appear on rows that
#                         predate them, with the documented
#                         defaults (invited=1, active=1,
#                         language='lt')
#    v12  the shipped WELCOME-KNF-2026 invite is expired
#    v13  raw session tokens become sha256; a digest is left
#         alone; expired rows die unhashed
#    v14  drifted like/comment/vote counters go back on their
#         rows
#    v15  the foreign-key child indexes exist
#    v16  created_by/author_id become ON DELETE SET NULL and
#         poll_votes.user_id gains its CASCADE
#    v17  space-form timestamps become ISO-T — and then sort
#         correctly, which was the whole point
#    v18/v21/v26  duplicates collapse FIRST, then the unique
#         index builds and refuses the next one
#    v19  scraper_runs older than 30 days are pruned
#    v20  messages become searchable through the FTS shadow
#         table, kept in sync by its triggers
#    v22  the six duplicate indexes are dropped
#    v23  orphaned message_reads die, valid ones stay
#    v25/v40/v43  the tombstone / audit / uploads tables are
#         ready with their documented FK actions
#    v35  source_url is canonicalised and the duplicates it
#         exposes are dropped
#    v46/v55  the fan-out and profile indexes exist
#    v47  malformed push tokens are purged — rows here are
#         TUPLES, because migrations run with no row_factory
#    v49  multi-member 'direct' rooms become titled groups
#
#  Around that: user text is byte-identical afterwards (v1 and
#  v2 are RETIRED no-ops and must stay that way), PRAGMA
#  integrity_check and foreign_key_check come back clean, a
#  second boot re-runs nothing, and every guard clause in
#  init_db / get_db / the ALTER migrations is tripped on
#  purpose — a locked file, a readonly file, a migration that
#  raises, an existing file with zero users, get_db before
#  init_db, and an FTS5-less SQLite build.
# -----------------------------------------------------------

import hashlib
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
import time_machine

import app.database as database


LOGGER_NAME = "app.database"




# -----------------------------------------------------------
# _restore_db_path
# -----------------------------------------------------------
#
# init_db() pins a MODULE-level _db_path that get_db() reads,
# and this file calls init_db() on databases of its own making.
# Putting the previous value back keeps those calls from
# leaking into whatever test runs next.
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
# The pre-migration table shapes: no users.invited / student
# fields / active, no messages.reply_to_id / deleted_at /
# client_msg_id, no push_tokens.language, plain REFERENCES with
# no ON DELETE action, and poll_votes.user_id with no foreign
# key at all. Every ALTER and every v16 rebuild therefore does
# real work here, where on a fresh database they are skipped.
# The six indexes v22 exists to drop are planted too.
# -----------------------------------------------------------

_LEGACY_SCHEMA = """
CREATE TABLE users (
    id TEXT PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    email TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'student' CHECK(role IN ('student', 'teacher', 'admin', 'curator')),
    avatar_url TEXT,
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


# The token minted before v13; its digest is what the row must
# hold afterwards, and the RAW value must no longer appear
_RAW_TOKEN = "11111111-2222-3333-4444-555555555555"
_RAW_TOKEN_SHA = hashlib.sha256(_RAW_TOKEN.encode()).hexdigest()

# Text a retired v1/v2 replay would mangle — it must come back
# byte-identical
_RAW_HTML_CONTENT = "Turinys su <b>paryškinimu</b> ir &amp; ženklu"

_EXPO_OK = "ExponentPushToken[abcdefghij1234567890]"




# -----------------------------------------------------------
# _build_legacy_database
# -----------------------------------------------------------
#
# Plants the legacy schema plus one representative row per
# thing a migration has to fix. Written on a PLAIN connection,
# so foreign_keys is OFF and the deliberate orphans (the two
# message_reads rows v23 exists to delete) can be inserted at
# all.
#
# Used by:
#   - the legacy_path fixture below
# -----------------------------------------------------------

def _build_legacy_database(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(_LEGACY_SCHEMA)

    conn.executemany(
        "INSERT INTO users (id, username, email, display_name, password_hash, role, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("u1", "ona", "ona@knf.vu.lt", "Ona Onaitė", "hash-1", "student",
             "2024-01-02 10:00:00", "2024-01-02 10:00:00"),
            ("u2", "jonas", "jonas@knf.vu.lt", "Jonas Jonaitis", "hash-2", "teacher",
             "2024-02-03T11:00:00+00:00", "2024-02-03T11:00:00+00:00"),
            ("u3", "admin", "admin@knf.vu.lt", "Administratorius", "hash-3", "admin",
             "2024-01-01 09:00:00", "2024-01-01 09:00:00"),
        ],
    )

    conn.executemany(
        "INSERT INTO invitation_codes (id, code, role, created_by, max_uses, use_count, expires_at, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("i1", "WELCOME-KNF-2026", "student", "u3", 100, 3, "2030-01-01 00:00:00", "2024-01-01 00:00:00"),
            ("i2", "KNF-DEADBEEFDEADBEEF", "student", "u3", 1, 0, "2030-06-01 00:00:00", "2024-01-01 00:00:00"),
        ],
    )

    conn.executemany(
        "INSERT INTO sessions (id, user_id, token, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
        [
            # raw uuid4 token, still live — must be hashed
            ("s1", "u1", _RAW_TOKEN, "2024-07-01 12:00:00", "2030-01-01T00:00:00+00:00"),
            # already a 64-char lowercase-hex digest — must be left alone
            ("s2", "u2", "a" * 64, "2024-07-01T12:00:00+00:00", "2030-01-01T00:00:00+00:00"),
            # expired — purged before hashing ever looks at it
            ("s3", "u3", "expired-raw-token", "2020-01-01 00:00:00", "2020-01-02T00:00:00+00:00"),
            # 63 hex chars: one short of a digest, so it is hashed
            ("s4", "u1", "b" * 63, "2024-07-02 08:00:00", "2031-05-05 08:00:00"),
            # 64 chars but UPPERCASE hex — not a digest either
            ("s5", "u2", "A" * 64, "2024-07-03 08:00:00", "2031-05-05T08:00:00+00:00"),
        ],
    )

    conn.executemany(
        "INSERT INTO news_posts (id, title, content, summary, image_url, author_id, author_name, source,"
        " source_url, post_type, is_public, likes_count, comments_count, shares_count, published_at,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            # counters drifted far from the rows; URL in the pre-v35 spelling
            ("p1", "Pirma naujiena", _RAW_HTML_CONTENT, None, None, "u1", "Ona", "knf.vu.lt",
             "http://www.knf.vu.lt/naujienos/pirma/?utm_source=facebook", "article", 1, 99, 42, 0,
             "2024-03-01 08:00:00", "2024-03-01 08:00:00", "2024-03-01 08:00:00"),
            # the SAME article behind a second spelling — v35 drops it
            ("p2", "Pirma naujiena (dublis)", "Dublis", None, None, "u1", "Ona", "knf.vu.lt",
             "https://www.knf.vu.lt/naujienos/pirma/", "article", 1, 0, 0, 0,
             "2024-03-01 08:30:00", "2024-03-01 08:30:00", "2024-03-01 08:30:00"),
            ("p3", "Apklausa", "Balsuokim", None, None, "u3", "Administratorius", "app",
             None, "poll", 1, 0, 0, 0,
             "2024-03-02 08:00:00", "2024-03-02 08:00:00", "2024-03-02 08:00:00"),
            ("p4", "Vartotojo įrašas", "Sveiki!", None, None, "u2", "Jonas", "user",
             None, "social", 1, 0, 0, 0,
             "2024-03-03T08:00:00+00:00", "2024-03-03T08:00:00+00:00", "2024-03-03T08:00:00+00:00"),
            # already canonical — v35 must leave it exactly as it is
            ("p5", "VU naujiena", "Turinys", None, None, None, "VU", "vu.lt",
             "https://vu.lt/naujienos/antra", "article", 1, 0, 0, 0,
             "2024-03-04 08:00:00", "2024-03-04 08:00:00", "2024-03-04 08:00:00"),
        ],
    )

    conn.executemany(
        "INSERT INTO news_likes (user_id, post_id, created_at) VALUES (?, ?, ?)",
        [
            ("u1", "p1", "2024-03-01 09:00:00"),
            ("u2", "p1", "2024-03-01 09:30:00"),
            # hangs off the duplicate article — must cascade away with it
            ("u1", "p2", "2024-03-01 09:40:00"),
        ],
    )

    conn.execute(
        "INSERT INTO news_comments (id, post_id, user_id, text, created_at) VALUES (?, ?, ?, ?, ?)",
        ("nc1", "p1", "u2", "Puiki naujiena!", "2024-03-05 09:00:00"),
    )

    conn.executemany(
        "INSERT INTO polls (id, post_id, title, end_date, total_votes, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("poll1", "p3", "Kada susitinkam?", None, 77, "2024-03-02 09:00:00"),
            # a second poll on the SAME post — v26 deletes it
            ("poll2", "p3", "Dublis", None, 5, "2024-03-02 10:00:00"),
        ],
    )

    conn.executemany(
        "INSERT INTO poll_options (id, poll_id, text, votes) VALUES (?, ?, ?, ?)",
        [
            ("o1", "poll1", "Pirmadienį", 50),
            ("o2", "poll1", "Antradienį", 0),
            ("o3", "poll2", "Nesvarbu", 3),
        ],
    )

    conn.executemany(
        "INSERT INTO poll_votes (user_id, poll_id, option_id, created_at) VALUES (?, ?, ?, ?)",
        [
            ("u1", "poll1", "o1", "2024-03-02 10:00:00"),
            ("u2", "poll1", "o1", "2024-03-02 11:00:00"),
        ],
    )

    conn.executemany(
        "INSERT INTO schedule_lessons (id, title, teacher, room, time_start, time_end, day_of_week,"
        " group_name, semester, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("sl1", "Programavimas", "Dr. Kazlauskas", "301", "08:30", "10:00", 1,
             "PS-1", "2024-pavasaris", "2024-01-01 07:00:00"),
            # byte-identical natural key — v18 keeps the oldest rowid only
            ("sl2", "Programavimas", "Dr. Kazlauskas", "301", "08:30", "10:00", 1,
             "PS-1", "2024-pavasaris", "2024-01-02 07:00:00"),
            ("sl3", "Matematika", "Doc. Petraitis", "205", "10:15", "11:45", 2,
             "PS-1", "2024-pavasaris", "2024-01-01 07:00:00"),
        ],
    )

    recent = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        "INSERT INTO scraper_runs (id, source, status, articles_found, articles_new, error_message,"
        " started_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("r1", "knf.vu.lt", "completed", 10, 2, None, "2020-01-01 00:00:00", "2020-01-01 00:05:00"),
            ("r2", "vu.lt", "completed", 5, 1, None, recent, None),
            # left 'running' by a process that never came back —
            # v36 reconciles it
            ("r3", "knf.vu.lt", "running", 0, 0, None, recent, None),
        ],
    )

    conn.executemany(
        "INSERT INTO conversations (id, type, title, avatar_emoji, created_by, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("c1", "direct", None, None, "u1", "2024-01-01 00:00:00", "2024-01-01 00:00:00"),
            ("c2", "direct", None, None, "u1", "2024-01-01 00:00:00", "2024-01-01 00:00:00"),
            ("c3", "direct", "Senas pokalbis", None, "u2", "2024-01-01 00:00:00", "2024-01-01 00:00:00"),
            ("c4", "direct", None, None, "u3", "2024-01-01 00:00:00", "2024-01-01 00:00:00"),
            ("c5", "direct", "", None, "u1", "2024-01-01 00:00:00", "2024-01-01 00:00:00"),
        ],
    )

    conn.executemany(
        "INSERT INTO conversation_participants (conversation_id, user_id, pinned, last_read_at, joined_at)"
        " VALUES (?, ?, ?, ?, ?)",
        [
            ("c1", "u1", 0, "2024-04-01 12:05:00", "2024-01-01 00:00:00"),
            ("c1", "u2", 0, None, "2024-01-01 00:00:00"),
            ("c2", "u1", 0, None, "2024-01-01 00:00:00"),
            ("c2", "u2", 0, None, "2024-01-01 00:00:00"),
            ("c2", "u3", 0, None, "2024-01-01 00:00:00"),
            ("c3", "u1", 0, None, "2024-01-01 00:00:00"),
            ("c3", "u2", 0, None, "2024-01-01 00:00:00"),
            ("c3", "u3", 0, None, "2024-01-01 00:00:00"),
            ("c4", "u3", 0, None, "2024-01-01 00:00:00"),
            ("c5", "u1", 0, None, "2024-01-01 00:00:00"),
            ("c5", "u2", 0, None, "2024-01-01 00:00:00"),
            ("c5", "u3", 0, None, "2024-01-01 00:00:00"),
        ],
    )

    conn.executemany(
        "INSERT INTO messages (id, conversation_id, sender_id, text, image_url, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("m1", "c1", "u1", "Labas, kaip sekasi?", None, "2024-04-01 12:00:00"),
            ("m2", "c1", "u2", "Gerai, ačiū &amp; iki!", None, "2024-04-01 12:01:00"),
            ("m3", "c2", "u1", "Grupės žinutė", None, "2024-04-02 09:00:00"),
        ],
    )

    conn.execute(
        "INSERT INTO message_reactions (message_id, user_id, emoji, created_at) VALUES (?, ?, ?, ?)",
        ("m1", "u2", "👍", "2024-04-01 12:02:00"),
    )

    conn.executemany(
        "INSERT INTO message_reads (message_id, user_id, read_at) VALUES (?, ?, ?)",
        [
            ("m1", "u2", "2024-04-01 12:03:00"),
            # both legs of v23: a vanished message and a vanished user
            ("m-missing", "u1", "2024-04-01 12:03:00"),
            ("m2", "u-missing", "2024-04-01 12:03:00"),
        ],
    )

    conn.executemany(
        "INSERT INTO friendships (user_id, friend_id, created_at) VALUES (?, ?, ?)",
        [("u1", "u2", "2024-02-01 10:00:00"), ("u2", "u1", "2024-02-01 10:00:00")],
    )

    conn.executemany(
        "INSERT INTO friend_requests (id, from_user_id, to_user_id, status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("fr1", "u1", "u3", "pending", "2024-05-01 10:00:00", "2024-05-01 10:00:00"),
            # same pair, still pending — v21 collapses it into fr1
            ("fr2", "u1", "u3", "pending", "2024-05-02 10:00:00", "2024-05-02 10:00:00"),
            ("fr3", "u3", "u2", "accepted", "2024-05-03 10:00:00", "2024-05-03 10:00:00"),
            ("fr4", "u3", "u2", "pending", "2024-05-04 10:00:00", "2024-05-04 10:00:00"),
        ],
    )

    conn.execute(
        "INSERT INTO faculty_info (id, lang, section, data_json, scraped_at) VALUES (?, ?, ?, ?, ?)",
        ("fi1", "lt", "contacts", '{"phone": "+370 37 422 344"}', "2024-06-01 06:00:00"),
    )

    conn.executemany(
        "INSERT INTO push_tokens (id, user_id, token, platform, active, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("pt1", "u1", _EXPO_OK, "ios", 1, "2024-01-01 00:00:00", "2024-01-01 00:00:00"),
            ("pt2", "u2", "ExponentPushToken[", "android", 1, "2024-01-01 00:00:00", "2024-01-01 00:00:00"),
            ("pt3", "u3", "ExponentPushToken[" + "a" * 9 + "]", "android", 1,
             "2024-01-01 00:00:00", "2024-01-01 00:00:00"),
            ("pt4", "u1", "ExponentPushToken[" + "a" * 10 + "]", "ios", 1,
             "2024-01-01 00:00:00", "2024-01-01 00:00:00"),
            ("pt5", "u2", "ExponentPushToken[" + "b" * 64 + "]", "ios", 1,
             "2024-01-01 00:00:00", "2024-01-01 00:00:00"),
            ("pt6", "u3", "ExponentPushToken[" + "c" * 65 + "]", "android", 1,
             "2024-01-01 00:00:00", "2024-01-01 00:00:00"),
            ("pt7", "u1", "ExponentPushToken[valid_token]\n", "web", 1,
             "2024-01-01 00:00:00", "2024-01-01 00:00:00"),
            ("pt8", "u2", "'; DROP TABLE users; --", "web", 1,
             "2024-01-01 00:00:00", "2024-01-01 00:00:00"),
            ("pt9", "u3", "ExponentPushToken[ok_with-dashes_1]", "ios", 1,
             "2024-01-01 00:00:00", "2024-01-01 00:00:00"),
            ("pt10", "u1", "ExpoPushToken[abcdefghij]", "android", 1,
             "2024-01-01 00:00:00", "2024-01-01 00:00:00"),
        ],
    )

    conn.executemany(
        "INSERT INTO notification_channels (user_id, channel, enabled, updated_at) VALUES (?, ?, ?, ?)",
        [("u1", "news", 1, "2024-01-01 00:00:00"), ("u2", "chat", 0, "2024-01-01 00:00:00")],
    )

    conn.commit()
    conn.close()




# -----------------------------------------------------------
# Small readers used all over this module
# -----------------------------------------------------------

def _open(path):
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _columns(conn, table):
    return {row[1]: row for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _index_names(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()}


def _on_delete(conn, table, column):
    for fk in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall():
        if fk[3] == column:
            return fk[6]
    return None


def _one(conn, sql, params=()):
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def _applied_versions(conn):
    return [r[0] for r in conn.execute("SELECT version FROM _migrations ORDER BY version").fetchall()]




# -----------------------------------------------------------
# _fingerprint
# -----------------------------------------------------------
#
# Row counts per table plus the whole schema text — enough to
# prove a second boot re-ran nothing, without listing every
# column of every table.
# -----------------------------------------------------------

def _fingerprint(conn):
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()]
    counts = {name: _one(conn, f"SELECT COUNT(*) FROM {name}") for name in names}
    schema = sorted((r[0] or "") for r in conn.execute("SELECT sql FROM sqlite_master").fetchall())
    return counts, schema




# -----------------------------------------------------------
# legacy_path / migrated
# -----------------------------------------------------------
#
# legacy_path — the hand-built old database, NOT yet migrated
# migrated    — that same file after the real init_db() ran
#               over it, handed back as an open connection
# -----------------------------------------------------------

@pytest.fixture
def legacy_path(tmp_path):
    path = tmp_path / "legacy.db"
    _build_legacy_database(path)
    return path


@pytest.fixture
def migrated(legacy_path):
    database.init_db(str(legacy_path))
    conn = _open(legacy_path)
    yield conn
    conn.close()




# ===========================================================
# The migration chain itself
# ===========================================================

def test_a_legacy_database_records_every_registered_migration(migrated):
    versions = _applied_versions(migrated)

    assert versions == sorted(set(versions)), "a version was recorded twice or out of order"
    for expected in (1, 2, 3, 10, 13, 16, 17, 20, 26, 35, 47, 49, 55):
        assert expected in versions


def test_a_legacy_database_ends_up_at_the_same_version_set_as_a_fresh_one(migrated, tmp_path):
    fresh = tmp_path / "fresh.db"
    database.init_db(str(fresh))

    fresh_conn = _open(fresh)
    try:
        assert _applied_versions(migrated) == _applied_versions(fresh_conn)
    finally:
        fresh_conn.close()




# ===========================================================
# v3 / v5 / v8 / v9 / v10 / v11 — the column ALTERs
# ===========================================================

def test_legacy_users_gain_the_invited_column_marked_invited(migrated):
    assert "invited" in _columns(migrated, "users")
    # v3 keeps DEFAULT 1 on purpose: everyone predating it came
    # through an invitation code
    assert _one(migrated, "SELECT COUNT(*) FROM users WHERE invited = 1") == 3


def test_legacy_users_gain_the_student_id_columns_as_null(migrated):
    columns = _columns(migrated, "users")

    for name in ("student_number", "study_group", "study_program"):
        assert name in columns
    assert _one(migrated, "SELECT COUNT(*) FROM users WHERE student_number IS NOT NULL") == 0


def test_legacy_users_gain_the_active_column_defaulting_to_active(migrated):
    assert "active" in _columns(migrated, "users")
    assert _one(migrated, "SELECT COUNT(*) FROM users WHERE active = 1") == 3


def test_legacy_messages_gain_the_reply_and_soft_delete_columns(migrated):
    columns = _columns(migrated, "messages")

    assert "reply_to_id" in columns
    assert "deleted_at" in columns
    assert _one(migrated, "SELECT COUNT(*) FROM messages WHERE reply_to_id IS NULL AND deleted_at IS NULL") == 3


def test_legacy_messages_gain_client_msg_id_and_its_unique_index(migrated):
    assert "client_msg_id" in _columns(migrated, "messages")
    assert "idx_messages_client_msg" in _index_names(migrated)


def test_the_client_msg_id_index_refuses_a_replayed_send_nonce(migrated):
    migrated.execute(
        "INSERT INTO messages (id, conversation_id, sender_id, text, client_msg_id, created_at)"
        " VALUES ('mx1', 'c1', 'u1', 'pirmas', 'nonce-a', '2026-01-01T10:00:00+00:00')"
    )

    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            "INSERT INTO messages (id, conversation_id, sender_id, text, client_msg_id, created_at)"
            " VALUES ('mx2', 'c1', 'u1', 'kartotinis', 'nonce-a', '2026-01-01T10:00:01+00:00')"
        )


def test_the_same_nonce_from_another_sender_is_still_accepted(migrated):
    migrated.execute(
        "INSERT INTO messages (id, conversation_id, sender_id, text, client_msg_id, created_at)"
        " VALUES ('mx3', 'c1', 'u1', 'a', 'nonce-b', '2026-01-01T10:00:00+00:00')"
    )
    migrated.execute(
        "INSERT INTO messages (id, conversation_id, sender_id, text, client_msg_id, created_at)"
        " VALUES ('mx4', 'c1', 'u2', 'b', 'nonce-b', '2026-01-01T10:00:01+00:00')"
    )

    assert _one(migrated, "SELECT COUNT(*) FROM messages WHERE client_msg_id = 'nonce-b'") == 2


def test_null_send_nonces_never_collide(migrated):
    # SQLite treats each NULL in a unique index as distinct —
    # old rows and nonce-less clients must keep working
    for suffix in ("a", "b", "c"):
        migrated.execute(
            "INSERT INTO messages (id, conversation_id, sender_id, text, created_at)"
            f" VALUES ('mn-{suffix}', 'c1', 'u1', 'be nonce', '2026-01-01T10:00:00+00:00')"
        )

    assert _one(migrated, "SELECT COUNT(*) FROM messages WHERE client_msg_id IS NULL") == 6


def test_legacy_push_tokens_gain_the_language_column_defaulting_to_lt(migrated):
    assert "language" in _columns(migrated, "push_tokens")
    assert _one(migrated, "SELECT COUNT(*) FROM push_tokens WHERE language != 'lt'") == 0




# ===========================================================
# v12 — the shipped bootstrap invitation code
# ===========================================================

def test_the_legacy_bootstrap_invitation_code_is_expired(migrated):
    expires = _one(migrated, "SELECT expires_at FROM invitation_codes WHERE code = 'WELCOME-KNF-2026'")

    assert expires is not None, "v12 expires the row, it must never delete it"
    assert expires < database.utc_now_iso()


def test_expiring_the_bootstrap_code_leaves_its_use_history_intact(migrated):
    row = migrated.execute(
        "SELECT max_uses, use_count, created_by FROM invitation_codes WHERE code = 'WELCOME-KNF-2026'"
    ).fetchone()

    assert row == (100, 3, "u3")


def test_a_generated_invitation_code_keeps_its_expiry(migrated):
    # only the hard-coded literal is targeted; every other code
    # must survive untouched (bar v17's normalisation)
    assert _one(migrated, "SELECT expires_at FROM invitation_codes WHERE code = 'KNF-DEADBEEFDEADBEEF'") \
        == "2030-06-01T00:00:00"


def test_expiring_the_bootstrap_code_is_a_no_op_when_it_is_already_expired(tmp_path):
    path = tmp_path / "v12.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE invitation_codes (id TEXT, code TEXT, expires_at TEXT)")
    conn.execute("INSERT INTO invitation_codes VALUES ('i1', 'WELCOME-KNF-2026', '2000-01-01T00:00:00+00:00')")
    conn.commit()

    database._migration_v12_expire_bootstrap_invite(conn)

    assert _one(conn, "SELECT expires_at FROM invitation_codes") == "2000-01-01T00:00:00+00:00"
    conn.close()




# ===========================================================
# v13 — session tokens at rest
# ===========================================================

def test_a_raw_session_token_is_replaced_by_its_sha256(migrated):
    assert _one(migrated, "SELECT token FROM sessions WHERE id = 's1'") == _RAW_TOKEN_SHA
    assert _one(migrated, "SELECT COUNT(*) FROM sessions WHERE token = ?", (_RAW_TOKEN,)) == 0


def test_an_already_hashed_session_token_is_left_alone(migrated):
    # the hex-shape test is what keeps a re-run from
    # double-hashing every live session out of existence
    assert _one(migrated, "SELECT token FROM sessions WHERE id = 's2'") == "a" * 64


def test_a_token_that_is_merely_hex_shaped_is_still_hashed(migrated):
    # 63 hex chars and 64 UPPERCASE hex chars are both "not a
    # digest" — the length and the alphabet are both boundaries
    assert _one(migrated, "SELECT token FROM sessions WHERE id = 's4'") \
        == hashlib.sha256(("b" * 63).encode()).hexdigest()
    assert _one(migrated, "SELECT token FROM sessions WHERE id = 's5'") \
        == hashlib.sha256(("A" * 64).encode()).hexdigest()


def test_expired_sessions_are_purged_instead_of_being_hashed(migrated):
    assert _one(migrated, "SELECT COUNT(*) FROM sessions WHERE id = 's3'") == 0
    assert _one(migrated, "SELECT COUNT(*) FROM sessions") == 4


def test_hashing_session_tokens_is_idempotent(legacy_path):
    database.init_db(str(legacy_path))
    once = _open(legacy_path)
    tokens_after_first = dict(once.execute("SELECT id, token FROM sessions").fetchall())
    once.close()

    conn = _open(legacy_path)
    database._migration_v13_hash_session_tokens(conn)

    assert dict(conn.execute("SELECT id, token FROM sessions").fetchall()) == tokens_after_first
    conn.close()




# ===========================================================
# v14 — denormalised counters put back on their rows
# ===========================================================

def test_drifted_news_counters_are_reconciled_from_their_rows(migrated):
    row = migrated.execute("SELECT likes_count, comments_count FROM news_posts WHERE id = 'p1'").fetchone()

    assert row == (2, 1), "99 likes and 42 comments were stored over 2 likes and 1 comment"


def test_drifted_poll_counters_are_reconciled_from_their_votes(migrated):
    assert _one(migrated, "SELECT total_votes FROM polls WHERE id = 'poll1'") == 2
    assert _one(migrated, "SELECT votes FROM poll_options WHERE id = 'o1'") == 2
    assert _one(migrated, "SELECT votes FROM poll_options WHERE id = 'o2'") == 0


def test_reconciling_counters_twice_lands_on_the_same_numbers(migrated):
    database._migration_v14_reconcile_counters(migrated)

    assert migrated.execute(
        "SELECT likes_count, comments_count FROM news_posts WHERE id = 'p1'").fetchone() == (2, 1)
    assert _one(migrated, "SELECT total_votes FROM polls WHERE id = 'poll1'") == 2




# ===========================================================
# v15 / v46 / v55 — the index-only migrations
# ===========================================================

def test_the_foreign_key_child_indexes_exist(migrated):
    names = _index_names(migrated)

    for expected in (
        "idx_sessions_user", "idx_news_likes_post", "idx_news_comments_user",
        "idx_messages_sender", "idx_messages_reply_to", "idx_news_posts_author",
        "idx_message_reactions_user", "idx_conversations_created_by",
        "idx_invitation_codes_created_by",
    ):
        assert expected in names


def test_the_push_fanout_index_exists(migrated):
    assert "idx_push_tokens_active" in _index_names(migrated)


def test_the_profile_composite_index_exists(migrated):
    assert "idx_news_posts_author_source" in _index_names(migrated)




# ===========================================================
# v16 — the ON DELETE rebuilds
# ===========================================================

@pytest.mark.parametrize("table,column", [
    ("invitation_codes", "created_by"),
    ("news_posts", "author_id"),
    ("conversations", "created_by"),
])
def test_rebuilt_tables_declare_on_delete_set_null(migrated, table, column):
    assert _on_delete(migrated, table, column) == "SET NULL"


def test_poll_votes_gains_a_cascading_user_foreign_key(migrated):
    # it had NO foreign key on user_id at all, so votes outlived
    # their voter
    assert _on_delete(migrated, "poll_votes", "user_id") == "CASCADE"


def test_deleting_a_user_no_longer_fails_on_the_rows_they_authored(migrated):
    migrated.execute("DELETE FROM users WHERE id = 'u1'")

    assert _one(migrated, "SELECT author_id FROM news_posts WHERE id = 'p1'") is None
    assert _one(migrated, "SELECT created_by FROM conversations WHERE id = 'c1'") is None
    assert _one(migrated, "SELECT COUNT(*) FROM poll_votes WHERE user_id = 'u1'") == 0


def test_deleting_the_inviting_admin_keeps_the_invitation_row(migrated):
    migrated.execute("DELETE FROM users WHERE id = 'u3'")

    assert _one(migrated, "SELECT created_by FROM invitation_codes WHERE id = 'i2'") is None
    assert _one(migrated, "SELECT COUNT(*) FROM invitation_codes WHERE id = 'i2'") == 1


def test_the_rebuild_preserves_every_row_it_copied(migrated):
    assert _one(migrated, "SELECT COUNT(*) FROM invitation_codes") == 2
    assert _one(migrated, "SELECT COUNT(*) FROM conversations") == 5
    assert _one(migrated, "SELECT COUNT(*) FROM poll_votes") == 2
    assert _one(migrated, "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE '%_new'") == 0


def test_the_rebuild_is_skipped_when_every_action_is_already_right(tmp_path, caplog):
    fresh = tmp_path / "fresh-fk.db"
    database.init_db(str(fresh))
    conn = _open(fresh)

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        database._migration_v16_rebuild_fk_actions(conn)

    assert "nothing to rebuild" in caplog.text
    conn.close()




# ===========================================================
# v17 — one text shape for timestamps
# ===========================================================

@pytest.mark.parametrize("sql,expected", [
    ("SELECT created_at FROM users WHERE id = 'u1'", "2024-01-02T10:00:00"),
    ("SELECT created_at FROM invitation_codes WHERE id = 'i2'", "2024-01-01T00:00:00"),
    ("SELECT created_at FROM sessions WHERE id = 's1'", "2024-07-01T12:00:00"),
    ("SELECT expires_at FROM sessions WHERE id = 's4'", "2031-05-05T08:00:00"),
    ("SELECT published_at FROM news_posts WHERE id = 'p1'", "2024-03-01T08:00:00"),
    ("SELECT created_at FROM news_likes WHERE user_id = 'u1' AND post_id = 'p1'", "2024-03-01T09:00:00"),
    ("SELECT created_at FROM news_comments WHERE id = 'nc1'", "2024-03-05T09:00:00"),
    ("SELECT created_at FROM polls WHERE id = 'poll1'", "2024-03-02T09:00:00"),
    ("SELECT created_at FROM poll_votes WHERE user_id = 'u1'", "2024-03-02T10:00:00"),
    ("SELECT created_at FROM schedule_lessons WHERE id = 'sl1'", "2024-01-01T07:00:00"),
    ("SELECT created_at FROM conversations WHERE id = 'c1'", "2024-01-01T00:00:00"),
    ("SELECT last_read_at FROM conversation_participants WHERE conversation_id = 'c1' AND user_id = 'u1'",
     "2024-04-01T12:05:00"),
    ("SELECT created_at FROM messages WHERE id = 'm1'", "2024-04-01T12:00:00"),
    ("SELECT created_at FROM message_reactions WHERE message_id = 'm1'", "2024-04-01T12:02:00"),
    ("SELECT read_at FROM message_reads WHERE message_id = 'm1'", "2024-04-01T12:03:00"),
    ("SELECT created_at FROM friendships WHERE user_id = 'u1'", "2024-02-01T10:00:00"),
    ("SELECT created_at FROM friend_requests WHERE id = 'fr1'", "2024-05-01T10:00:00"),
    ("SELECT scraped_at FROM faculty_info WHERE id = 'fi1'", "2024-06-01T06:00:00"),
    ("SELECT created_at FROM push_tokens WHERE id = 'pt1'", "2024-01-01T00:00:00"),
    ("SELECT updated_at FROM notification_channels WHERE user_id = 'u1'", "2024-01-01T00:00:00"),
])
def test_space_form_timestamps_become_iso_t(migrated, sql, expected):
    assert _one(migrated, sql) == expected


def test_already_iso_t_timestamps_are_left_exactly_as_they_were(migrated):
    # the GLOB only matches the legacy shape, so a re-run — and
    # every row already stamped by utc_now_iso() — is untouched
    assert _one(migrated, "SELECT created_at FROM users WHERE id = 'u2'") == "2024-02-03T11:00:00+00:00"
    assert _one(migrated, "SELECT published_at FROM news_posts WHERE id = 'p4'") == "2024-03-03T08:00:00+00:00"


def test_null_timestamps_survive_normalisation(migrated):
    assert _one(migrated, "SELECT finished_at FROM scraper_runs WHERE id = 'r2'") is None
    assert _one(migrated, "SELECT end_date FROM polls WHERE id = 'poll1'") is None
    assert _one(migrated,
                "SELECT last_read_at FROM conversation_participants"
                " WHERE conversation_id = 'c1' AND user_id = 'u2'") is None


def test_normalised_timestamps_sort_against_new_iso_t_writes(migrated):
    # the bug v17 fixes: '2024-04-01 12:00:00' sorts BEFORE
    # '2024-04-01T00:00:00' under SQLite's string comparison
    migrated.execute(
        "INSERT INTO messages (id, conversation_id, sender_id, text, created_at)"
        " VALUES ('m4', 'c1', 'u1', 'naujausia', '2024-04-01T18:00:00+00:00')"
    )
    order = [r[0] for r in migrated.execute(
        "SELECT id FROM messages WHERE conversation_id = 'c1' ORDER BY created_at").fetchall()]

    assert order == ["m1", "m2", "m4"]


def test_normalising_timestamps_twice_changes_nothing(migrated):
    before = migrated.execute("SELECT id, created_at FROM messages ORDER BY id").fetchall()

    database._migration_v17_normalize_timestamps(migrated)

    assert migrated.execute("SELECT id, created_at FROM messages ORDER BY id").fetchall() == before




# ===========================================================
# v18 — schedule_lessons dedupe + natural key
# ===========================================================

def test_duplicate_schedule_lessons_are_collapsed_to_the_oldest_row(migrated):
    ids = sorted(r[0] for r in migrated.execute("SELECT id FROM schedule_lessons").fetchall())

    assert ids == ["sl1", "sl3"], "the duplicate must die and the FIRST row survive"


def test_the_schedule_natural_key_index_refuses_a_duplicate_lesson(migrated):
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            "INSERT INTO schedule_lessons (id, title, teacher, room, time_start, time_end, day_of_week,"
            " group_name, semester, created_at)"
            " VALUES ('sl9', 'Programavimas', 'Dr. Kazlauskas', '301', '08:30', '10:00', 1,"
            " 'PS-1', '2024-pavasaris', '2026-01-01T00:00:00+00:00')"
        )


def test_a_lesson_differing_in_one_natural_key_field_is_still_accepted(migrated):
    migrated.execute(
        "INSERT INTO schedule_lessons (id, title, teacher, room, time_start, time_end, day_of_week,"
        " group_name, semester, created_at)"
        " VALUES ('sl10', 'Programavimas', 'Dr. Kazlauskas', '302', '08:30', '10:00', 1,"
        " 'PS-1', '2024-pavasaris', '2026-01-01T00:00:00+00:00')"
    )

    assert _one(migrated, "SELECT COUNT(*) FROM schedule_lessons") == 3


def test_the_schedule_filter_index_exists(migrated):
    assert "idx_schedule_lessons_filter" in _index_names(migrated)




# ===========================================================
# v19 — scraper_runs retention
# ===========================================================

def test_ancient_scraper_runs_are_pruned_and_recent_ones_kept(migrated):
    ids = sorted(r[0] for r in migrated.execute("SELECT id FROM scraper_runs").fetchall())

    assert ids == ["r2", "r3"]
    assert "idx_scraper_runs_started" in _index_names(migrated)


def test_a_run_just_inside_the_thirty_day_window_survives(tmp_path):
    path = tmp_path / "v19.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE scraper_runs (id TEXT PRIMARY KEY, started_at TEXT)")
    inside = (datetime.now(timezone.utc) - timedelta(days=29, hours=23)).isoformat()
    outside = (datetime.now(timezone.utc) - timedelta(days=30, hours=1)).isoformat()
    conn.executemany("INSERT INTO scraper_runs VALUES (?, ?)", [("inside", inside), ("outside", outside)])
    conn.commit()

    database._migration_v19_scraper_runs_index(conn)

    assert [r[0] for r in conn.execute("SELECT id FROM scraper_runs").fetchall()] == ["inside"]
    conn.close()




# ===========================================================
# v36 — per-source index + the interrupted runs
# ===========================================================

def test_a_run_left_running_by_an_interrupted_process_is_closed_as_failed(migrated):
    row = migrated.execute(
        "SELECT status, error_message, finished_at FROM scraper_runs WHERE id = 'r3'").fetchone()

    assert row[0] == "failed"
    assert row[1] == "interrupted"
    assert row[2] is not None, "a closed run must carry the moment it was reconciled"


def test_a_completed_run_is_not_touched_by_the_reconciliation(migrated):
    assert migrated.execute(
        "SELECT status, error_message FROM scraper_runs WHERE id = 'r2'").fetchone() == ("completed", None)


def test_the_per_source_scraper_runs_index_exists(migrated):
    assert "idx_scraper_runs_source_started" in _index_names(migrated)


def test_reconciling_interrupted_runs_says_nothing_when_none_are_running(migrated, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        database._migration_v36_scraper_runs_source_index(migrated)

    assert "Closed" not in caplog.text




# ===========================================================
# v20 — the FTS5 shadow table
# ===========================================================

def test_messages_are_searchable_through_the_fts_shadow_table(migrated):
    hits = [r[0] for r in migrated.execute(
        "SELECT m.id FROM messages_fts JOIN messages m ON m.rowid = messages_fts.rowid"
        " WHERE messages_fts MATCH 'sekasi'").fetchall()]

    assert hits == ["m1"], "the backfill must index the rows that predate the migration"


def test_the_fts_triggers_follow_an_insert_an_update_and_a_delete(migrated):
    migrated.execute(
        "INSERT INTO messages (id, conversation_id, sender_id, text, created_at)"
        " VALUES ('m5', 'c1', 'u1', 'paskaita prasideda', '2026-01-01T09:00:00+00:00')"
    )
    assert _one(migrated, "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'paskaita'") == 1

    migrated.execute("UPDATE messages SET text = 'egzaminas baigtas' WHERE id = 'm5'")
    assert _one(migrated, "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'paskaita'") == 0
    assert _one(migrated, "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'egzaminas'") == 1

    migrated.execute("DELETE FROM messages WHERE id = 'm5'")
    assert _one(migrated, "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'egzaminas'") == 0


def test_the_fts_migration_degrades_to_the_like_fallback_without_fts5(caplog):
    refusing = _FakeConnection("no such module: fts5", refuse_prefix="CREATE VIRTUAL")

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        database._migration_v20_messages_fts(refusing)

    assert "FTS5 unavailable" in caplog.text
    assert refusing.scripts == [], "an FTS5-less build must not try to create the triggers"




# ===========================================================
# v21 — one pending friend request per pair
# ===========================================================

def test_duplicate_pending_friend_requests_are_collapsed_to_the_oldest(migrated):
    ids = sorted(r[0] for r in migrated.execute(
        "SELECT id FROM friend_requests WHERE from_user_id = 'u1' AND to_user_id = 'u3'").fetchall())

    assert ids == ["fr1"]


def test_the_pending_index_refuses_a_second_pending_request(migrated):
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            "INSERT INTO friend_requests (id, from_user_id, to_user_id, status, created_at, updated_at)"
            " VALUES ('fr9', 'u1', 'u3', 'pending', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
        )


def test_accepted_and_rejected_history_rows_stay_unconstrained(migrated):
    for request_id in ("fr10", "fr11"):
        migrated.execute(
            "INSERT INTO friend_requests (id, from_user_id, to_user_id, status, created_at, updated_at)"
            f" VALUES ('{request_id}', 'u1', 'u3', 'rejected',"
            " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
        )

    assert _one(migrated, "SELECT COUNT(*) FROM friend_requests WHERE status = 'rejected'") == 2


def test_a_pending_request_in_the_other_direction_is_untouched(migrated):
    assert _one(migrated, "SELECT COUNT(*) FROM friend_requests WHERE id = 'fr4'") == 1




# ===========================================================
# v22 — the six duplicate indexes
# ===========================================================

def test_the_six_duplicate_indexes_are_gone(migrated):
    names = _index_names(migrated)

    for dropped in (
        "idx_sessions_token", "idx_invitation_codes_code", "idx_message_reactions_message",
        "idx_message_reads_message", "idx_friendships_user", "idx_notification_channels_user",
    ):
        assert dropped not in names


def test_dropping_them_does_not_weaken_the_uniqueness_they_duplicated(migrated):
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            "INSERT INTO sessions (id, user_id, token, created_at, expires_at)"
            " VALUES ('s9', 'u1', ?, '2026-01-01T00:00:00+00:00', '2030-01-01T00:00:00+00:00')",
            (_RAW_TOKEN_SHA,),
        )




# ===========================================================
# v23 — orphaned message_reads
# ===========================================================

def test_orphaned_message_reads_are_deleted_and_the_valid_one_kept(migrated):
    rows = migrated.execute("SELECT message_id, user_id FROM message_reads").fetchall()

    assert rows == [("m1", "u2")]




# ===========================================================
# v25 / v40 / v43 — the brand-new tables
# ===========================================================

def test_the_deleted_source_urls_tombstone_table_is_ready(migrated):
    columns = _columns(migrated, "deleted_source_urls")

    assert set(columns) == {"source_url", "deleted_by", "deleted_at"}
    assert _on_delete(migrated, "deleted_source_urls", "deleted_by") == "SET NULL"


def test_removing_the_deleting_admin_does_not_resurrect_the_article(migrated):
    migrated.execute(
        "INSERT INTO deleted_source_urls (source_url, deleted_by, deleted_at)"
        " VALUES ('https://knf.vu.lt/x', 'u3', '2026-01-01T00:00:00+00:00')"
    )

    migrated.execute("DELETE FROM users WHERE id = 'u3'")

    assert _one(migrated, "SELECT deleted_by FROM deleted_source_urls WHERE source_url = 'https://knf.vu.lt/x'") is None


def test_the_admin_audit_table_is_ready(migrated):
    assert set(_columns(migrated, "admin_audit")) == \
        {"id", "actor_id", "action", "target", "payload", "created_at"}
    assert _on_delete(migrated, "admin_audit", "actor_id") == "SET NULL"
    assert {"idx_admin_audit_created", "idx_admin_audit_actor"} <= _index_names(migrated)


def test_the_uploads_table_is_ready(migrated):
    assert set(_columns(migrated, "uploads")) == {"id", "filename", "user_id", "byte_size", "created_at"}
    # SET NULL, never CASCADE: a deleted account's FILES must
    # stay findable by the orphan sweep
    assert _on_delete(migrated, "uploads", "user_id") == "SET NULL"
    assert "idx_uploads_user" in _index_names(migrated)


def test_a_brand_new_table_migration_creates_it_on_a_database_that_lacks_it(tmp_path):
    path = tmp_path / "bare.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE users (id TEXT PRIMARY KEY)")
    conn.commit()

    database._migration_v4_add_faculty_info_table(conn)
    database._migration_v6_add_push_tokens(conn)
    database._migration_v7_add_notification_channels(conn)
    database._migration_v25_deleted_source_urls(conn)
    database._migration_v40_admin_audit(conn)
    database._migration_v43_add_uploads_table(conn)

    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
    assert {"faculty_info", "push_tokens", "notification_channels",
            "deleted_source_urls", "admin_audit", "uploads"} <= tables
    conn.close()


def test_rerunning_a_brand_new_table_migration_keeps_the_rows(migrated):
    migrated.execute(
        "INSERT INTO admin_audit (id, actor_id, action, created_at)"
        " VALUES ('a1', 'u3', 'role_change', '2026-01-01T00:00:00+00:00')"
    )
    migrated.commit()

    database._migration_v40_admin_audit(migrated)

    assert _one(migrated, "SELECT COUNT(*) FROM admin_audit") == 1




# ===========================================================
# v26 — one poll per post
# ===========================================================

def test_duplicate_polls_are_collapsed_to_the_oldest_per_post(migrated):
    assert [r[0] for r in migrated.execute("SELECT id FROM polls").fetchall()] == ["poll1"]


def test_the_deleted_polls_options_go_with_it(migrated):
    assert sorted(r[0] for r in migrated.execute("SELECT id FROM poll_options").fetchall()) == ["o1", "o2"]


def test_the_unique_poll_index_refuses_a_second_poll_on_one_post(migrated):
    with pytest.raises(sqlite3.IntegrityError):
        migrated.execute(
            "INSERT INTO polls (id, post_id, title, total_votes, created_at)"
            " VALUES ('poll9', 'p3', 'Dar viena', 0, '2026-01-01T00:00:00+00:00')"
        )


def test_the_poll_option_index_exists(migrated):
    assert {"idx_polls_post", "idx_poll_options_poll"} <= _index_names(migrated)


def test_votes_and_options_orphaned_while_foreign_keys_were_off_are_swept(tmp_path):
    path = tmp_path / "v26.db"
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE polls (id TEXT PRIMARY KEY, post_id TEXT NOT NULL);
        CREATE TABLE poll_options (id TEXT PRIMARY KEY, poll_id TEXT NOT NULL);
        CREATE TABLE poll_votes (user_id TEXT, poll_id TEXT, option_id TEXT);
        INSERT INTO polls VALUES ('poll1', 'p1');
        INSERT INTO poll_options VALUES ('o1', 'poll1'), ('o-orphan', 'poll-gone');
        INSERT INTO poll_votes VALUES ('u1', 'poll1', 'o1'),
                                      ('u2', 'poll-gone', 'o-orphan'),
                                      ('u3', 'poll1', 'o-vanished');
    """)
    conn.commit()

    database._migration_v26_poll_indexes(conn)

    assert conn.execute("SELECT user_id FROM poll_votes").fetchall() == [("u1",)]
    assert conn.execute("SELECT id FROM poll_options").fetchall() == [("o1",)]
    conn.close()




# ===========================================================
# v35 — canonical source_url
# ===========================================================

def test_a_legacy_source_url_is_rewritten_into_its_canonical_shape(migrated):
    assert _one(migrated, "SELECT source_url FROM news_posts WHERE id = 'p1'") \
        == "https://knf.vu.lt/naujienos/pirma"


def test_the_duplicate_article_the_canonical_key_exposes_is_deleted(migrated):
    assert _one(migrated, "SELECT COUNT(*) FROM news_posts WHERE id = 'p2'") == 0
    assert _one(migrated, "SELECT COUNT(*) FROM news_posts") == 4


def test_deleting_the_duplicate_takes_its_likes_with_it(migrated):
    assert _one(migrated, "SELECT COUNT(*) FROM news_likes WHERE post_id = 'p2'") == 0


def test_an_already_canonical_source_url_is_left_untouched(migrated):
    assert _one(migrated, "SELECT source_url FROM news_posts WHERE id = 'p5'") \
        == "https://vu.lt/naujienos/antra"


def test_a_post_without_a_source_url_is_never_considered(migrated):
    assert _one(migrated, "SELECT COUNT(*) FROM news_posts WHERE source_url IS NULL") == 2


def test_canonicalising_a_url_a_later_row_already_holds_does_not_break_the_boot(tmp_path):
    # REGRESSION: the older row is UPDATEd to the canonical
    # spelling BEFORE the newer duplicate holding that exact
    # string is deleted — and news_posts.source_url is UNIQUE,
    # so the collision aborted the whole boot
    path = tmp_path / "collision.db"
    _build_legacy_database(path)
    conn = sqlite3.connect(str(path))
    conn.execute("UPDATE news_posts SET source_url = 'https://knf.vu.lt/naujienos/pirma' WHERE id = 'p2'")
    conn.commit()
    conn.close()

    database.init_db(str(path))

    conn = _open(path)
    try:
        assert _one(conn, "SELECT source_url FROM news_posts WHERE id = 'p1'") \
            == "https://knf.vu.lt/naujienos/pirma"
        assert _one(conn, "SELECT COUNT(*) FROM news_posts WHERE id = 'p2'") == 0
    finally:
        conn.close()




# ===========================================================
# v47 — malformed push tokens
# ===========================================================

def test_valid_expo_push_tokens_survive_the_purge(migrated):
    kept = sorted(r[0] for r in migrated.execute("SELECT id FROM push_tokens").fetchall())

    assert kept == ["pt1", "pt4", "pt5", "pt9"]


@pytest.mark.parametrize("row_id,why", [
    ("pt2", "the bare prefix the old intake check accepted"),
    ("pt3", "nine inner characters — one under the minimum"),
    ("pt6", "sixty-five inner characters — one over the maximum"),
    ("pt7", "a trailing newline outside the grammar"),
    ("pt8", "a SQL-looking payload"),
    ("pt10", "the wrong prefix entirely"),
])
def test_a_malformed_push_token_is_deleted(migrated, row_id, why):
    assert _one(migrated, "SELECT COUNT(*) FROM push_tokens WHERE id = ?", (row_id,)) == 0, why


def test_the_purge_reads_rows_positionally_on_a_plain_connection(tmp_path):
    # migrations run on init_db's connection, which has NO
    # row_factory — a lookup by column name raises TypeError and
    # takes the whole boot down, which is how v47 crash-looped
    path = tmp_path / "v47.db"
    conn = sqlite3.connect(str(path))
    assert conn.row_factory is None
    conn.execute("CREATE TABLE push_tokens (id TEXT PRIMARY KEY, token TEXT)")
    conn.execute("INSERT INTO push_tokens VALUES ('a', ?)", (_EXPO_OK,))
    conn.commit()

    database._migration_v47_purge_invalid_push_tokens(conn)

    assert _one(conn, "SELECT COUNT(*) FROM push_tokens") == 1
    conn.close()


def test_the_purge_chunks_past_the_sqlite_variable_limit(tmp_path):
    # 450 doomed rows means two DELETE statements; a single one
    # would be well past SQLite's 999-variable ceiling
    path = tmp_path / "v47-bulk.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE push_tokens (id TEXT PRIMARY KEY, token TEXT)")
    conn.executemany("INSERT INTO push_tokens VALUES (?, ?)",
                     [(f"bad-{n}", f"not-a-token-{n}") for n in range(450)])
    conn.execute("INSERT INTO push_tokens VALUES ('good', ?)", (_EXPO_OK,))
    conn.commit()

    database._migration_v47_purge_invalid_push_tokens(conn)

    assert [r[0] for r in conn.execute("SELECT id FROM push_tokens").fetchall()] == ["good"]
    conn.close()


def test_a_non_text_push_token_is_treated_as_malformed(tmp_path):
    path = tmp_path / "v47-blob.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE push_tokens (id TEXT PRIMARY KEY, token BLOB)")
    conn.execute("INSERT INTO push_tokens VALUES ('blob', ?)", (b"\x00\x01",))
    conn.execute("INSERT INTO push_tokens VALUES ('null', NULL)")
    conn.commit()

    database._migration_v47_purge_invalid_push_tokens(conn)

    assert _one(conn, "SELECT COUNT(*) FROM push_tokens") == 0
    conn.close()


def test_the_purge_leaves_a_clean_table_alone(tmp_path, caplog):
    path = tmp_path / "v47-clean.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE push_tokens (id TEXT PRIMARY KEY, token TEXT)")
    conn.execute("INSERT INTO push_tokens VALUES ('a', ?)", (_EXPO_OK,))
    conn.commit()

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        database._migration_v47_purge_invalid_push_tokens(conn)

    assert "malformed push_tokens" not in caplog.text
    conn.close()




# ===========================================================
# v49 — planted multi-member 'direct' rooms
# ===========================================================

def test_a_three_member_direct_room_is_demoted_and_titled(migrated):
    row = migrated.execute("SELECT type, title FROM conversations WHERE id = 'c2'").fetchone()

    assert row == ("group", "Grupė")


def test_a_demoted_room_keeps_the_title_it_already_had(migrated):
    assert migrated.execute("SELECT type, title FROM conversations WHERE id = 'c3'").fetchone() \
        == ("group", "Senas pokalbis")


def test_an_empty_title_is_replaced_by_the_group_fallback(migrated):
    assert migrated.execute("SELECT type, title FROM conversations WHERE id = 'c5'").fetchone() \
        == ("group", "Grupė")


def test_a_two_member_direct_room_stays_direct(migrated):
    assert migrated.execute("SELECT type, title FROM conversations WHERE id = 'c1'").fetchone() \
        == ("direct", None)


def test_a_one_member_direct_room_stays_direct(migrated):
    # the counterpart left — a legitimate state, not a plant
    assert _one(migrated, "SELECT type FROM conversations WHERE id = 'c4'") == "direct"


def test_the_room_audit_finds_nothing_on_a_second_run(migrated, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        database._migration_v49_direct_room_audit(migrated)

    assert "Demoted" not in caplog.text




# ===========================================================
# v1 / v2 — the retired no-ops
# ===========================================================

def test_the_retired_migrations_leave_user_text_byte_identical(migrated):
    assert _one(migrated, "SELECT content FROM news_posts WHERE id = 'p1'") == _RAW_HTML_CONTENT
    assert _one(migrated, "SELECT text FROM messages WHERE id = 'm2'") == "Gerai, ačiū &amp; iki!"


def test_the_retired_migrations_record_themselves_without_touching_anything(migrated, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        database._migration_v1_xss_cleanup(migrated)
        database._migration_v2_unescape_double_escapes(migrated)

    assert "v1 is retired" in caplog.text
    assert "v2 is retired" in caplog.text
    assert _one(migrated, "SELECT content FROM news_posts WHERE id = 'p1'") == _RAW_HTML_CONTENT




# ===========================================================
# The whole-database audits
# ===========================================================

def test_the_migrated_database_passes_integrity_check(migrated):
    assert migrated.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


def test_the_migrated_database_has_no_foreign_key_violations(migrated):
    assert migrated.execute("PRAGMA foreign_key_check").fetchall() == []


def test_every_user_and_message_survives_the_migration(migrated):
    users = migrated.execute("SELECT id, username, display_name, password_hash, role"
                             " FROM users ORDER BY id").fetchall()

    assert users == [
        ("u1", "ona", "Ona Onaitė", "hash-1", "student"),
        ("u2", "jonas", "Jonas Jonaitis", "hash-2", "teacher"),
        ("u3", "admin", "Administratorius", "hash-3", "admin"),
    ]
    assert _one(migrated, "SELECT COUNT(*) FROM messages") == 3
    assert _one(migrated, "SELECT COUNT(*) FROM friendships") == 2
    assert _one(migrated, "SELECT COUNT(*) FROM notification_channels") == 2


def test_migrating_a_legacy_file_never_seeds_a_second_admin(migrated):
    assert _one(migrated, "SELECT COUNT(*) FROM users WHERE username = 'admin'") == 1
    assert _one(migrated, "SELECT COUNT(*) FROM invitation_codes") == 2


def test_a_second_boot_reruns_nothing_and_changes_nothing(legacy_path, caplog):
    database.init_db(str(legacy_path))
    first = _open(legacy_path)
    before = _fingerprint(first)
    versions_before = _applied_versions(first)
    first.close()

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        database.init_db(str(legacy_path))

    second = _open(legacy_path)
    try:
        assert _fingerprint(second) == before
        assert _applied_versions(second) == versions_before
        assert "Running data migration" not in caplog.text
    finally:
        second.close()


def test_a_legacy_orphan_no_migration_covers_is_reported_by_the_boot_audit(legacy_path, caplog):
    conn = sqlite3.connect(str(legacy_path))
    conn.execute("INSERT INTO friendships (user_id, friend_id, created_at)"
                 " VALUES ('u1', 'u-vanished', '2024-02-01 10:00:00')")
    conn.commit()
    conn.close()

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        database.init_db(str(legacy_path))

    assert "Foreign-key violation" in caplog.text
    assert "friendships" in caplog.text




# ===========================================================
# _seed_defaults — first boot only
# ===========================================================

def test_a_brand_new_file_seeds_an_admin_and_a_bootstrap_code(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "pirmojo-paleidimo-slaptazodis")
    path = tmp_path / "seeded.db"

    database.init_db(str(path))

    conn = _open(path)
    try:
        assert conn.execute("SELECT username, email, role, invited FROM users").fetchall() \
            == [("admin", "admin@knf.vu.lt", "admin", 1)]
        code, role, max_uses, use_count, created_by = conn.execute(
            "SELECT code, role, max_uses, use_count, created_by FROM invitation_codes").fetchone()
        assert code.startswith("KNF-") and len(code) == 4 + 16
        assert code[4:] == code[4:].upper()
        assert (role, max_uses, use_count) == ("student", 100, 0)
        assert created_by == _one(conn, "SELECT id FROM users WHERE username = 'admin'")
    finally:
        conn.close()


def test_the_bootstrap_code_is_valid_for_a_year(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "x")
    path = tmp_path / "expiry.db"
    frozen = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)

    with time_machine.travel(frozen, tick=False):
        database.init_db(str(path))

    conn = _open(path)
    try:
        assert _one(conn, "SELECT expires_at FROM invitation_codes") \
            == (frozen + timedelta(days=365)).isoformat()
    finally:
        conn.close()


def test_an_env_admin_password_is_used_and_never_echoed(tmp_path, monkeypatch, caplog):
    import bcrypt

    monkeypatch.setenv("ADMIN_PASSWORD", "labai-slaptas-2026")
    path = tmp_path / "env-pw.db"

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        database.init_db(str(path))

    conn = _open(path)
    try:
        stored = _one(conn, "SELECT password_hash FROM users WHERE username = 'admin'")
    finally:
        conn.close()

    assert bcrypt.checkpw(b"labai-slaptas-2026", stored.encode())
    assert "from ADMIN_PASSWORD" in caplog.text
    assert "labai-slaptas-2026" not in caplog.text


def test_a_generated_admin_password_is_logged_exactly_once(tmp_path, monkeypatch, caplog):
    import bcrypt

    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    path = tmp_path / "gen-pw.db"

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        database.init_db(str(path))

    first_boot = [r for r in caplog.records if "FIRST BOOT" in r.getMessage()]
    assert len(first_boot) == 1
    generated = first_boot[0].args[0]

    conn = _open(path)
    try:
        stored = _one(conn, "SELECT password_hash FROM users WHERE username = 'admin'")
    finally:
        conn.close()

    assert bcrypt.checkpw(generated.encode(), stored.encode())


def test_seeding_never_repeats_on_a_later_boot(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "x")
    path = tmp_path / "twice.db"

    database.init_db(str(path))
    database.init_db(str(path))

    conn = _open(path)
    try:
        assert _one(conn, "SELECT COUNT(*) FROM users") == 1
        assert _one(conn, "SELECT COUNT(*) FROM invitation_codes") == 1
    finally:
        conn.close()


def test_an_existing_file_with_zero_users_is_refused_a_reseed(tmp_path, monkeypatch, caplog):
    # the wrong DB_PATH or a wiped volume — planting a fresh
    # admin over someone's data directory is the wrong answer
    monkeypatch.setenv("ADMIN_PASSWORD", "x")
    path = tmp_path / "empty-but-present.db"
    path.touch()

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        database.init_db(str(path))

    conn = _open(path)
    try:
        assert _one(conn, "SELECT COUNT(*) FROM users") == 0
        assert _one(conn, "SELECT COUNT(*) FROM invitation_codes") == 0
    finally:
        conn.close()

    assert "refusing to re-seed defaults" in caplog.text


def test_an_empty_database_still_gets_its_schema_and_migrations(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "x")
    path = tmp_path / "empty-schema.db"
    path.touch()

    database.init_db(str(path))

    conn = _open(path)
    try:
        assert _applied_versions(conn), "an existing-but-empty file must still be migrated"
        assert "idx_messages_client_msg" in _index_names(conn)
    finally:
        conn.close()




# ===========================================================
# init_db — the retry and abort guards
# ===========================================================

# -----------------------------------------------------------
# _FakeConnection
# -----------------------------------------------------------
#
# A connection stand-in that raises one chosen OperationalError
# for statements starting with `refuse_prefix` and swallows the
# rest. It is how the ALTER migrations' "duplicate column" skip
# and their re-raise are both reached without a corrupted file.
# -----------------------------------------------------------

class _FakeConnection:

    def __init__(self, message, refuse_prefix="ALTER"):
        self.message = message
        self.refuse_prefix = refuse_prefix
        self.statements = []
        self.scripts = []

    def execute(self, sql, *params):
        if sql.lstrip().upper().startswith(self.refuse_prefix.upper()):
            raise sqlite3.OperationalError(self.message)
        self.statements.append(sql)
        return self

    def executescript(self, sql):
        self.scripts.append(sql)
        return self

    def commit(self):
        pass

    def fetchall(self):
        return []


# -----------------------------------------------------------
# _flaky_connect
# -----------------------------------------------------------
#
# Replaces sqlite3.connect for the length of one test: the
# first `failures` calls raise, the rest open the real file.
# -----------------------------------------------------------

def _flaky_connect(monkeypatch, message, failures):
    real_connect = sqlite3.connect
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= failures:
            raise sqlite3.OperationalError(message)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(database.sqlite3, "connect", flaky)
    return calls


def test_a_locked_database_is_retried_until_it_opens(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ADMIN_PASSWORD", "x")
    slept = []
    monkeypatch.setattr(database.time, "sleep", slept.append)
    calls = _flaky_connect(monkeypatch, "database is locked", failures=2)
    path = tmp_path / "locked.db"

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        database.init_db(str(path))

    assert calls["n"] == 3
    assert slept == [3, 6], "attempt N backs off N*3 seconds"
    assert "locked during init (attempt 1/5)" in caplog.text


def test_a_permanently_locked_database_gives_up_after_five_attempts(tmp_path, monkeypatch, caplog):
    slept = []
    monkeypatch.setattr(database.time, "sleep", slept.append)
    calls = _flaky_connect(monkeypatch, "database is locked", failures=99)
    path = tmp_path / "always-locked.db"

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        with pytest.raises(sqlite3.OperationalError):
            database.init_db(str(path))

    assert calls["n"] == 5
    assert slept == [3, 6, 9, 12], "the fifth attempt raises instead of sleeping again"
    assert "Database init FAILED" in caplog.text


def test_a_non_lock_operational_error_aborts_the_boot_immediately(tmp_path, monkeypatch, caplog):
    slept = []
    monkeypatch.setattr(database.time, "sleep", slept.append)
    calls = _flaky_connect(monkeypatch, "attempt to write a readonly database", failures=99)
    path = tmp_path / "readonly.db"

    with caplog.at_level(logging.CRITICAL, logger=LOGGER_NAME):
        with pytest.raises(sqlite3.OperationalError):
            database.init_db(str(path))

    assert calls["n"] == 1, "a readonly database is never worth retrying"
    assert slept == []
    assert "Database init FAILED" in caplog.text


def test_an_unexpected_error_aborts_the_boot_with_one_critical_line(tmp_path, monkeypatch, caplog):
    def explode(*args, **kwargs):
        raise MemoryError("out of memory")

    monkeypatch.setattr(database.sqlite3, "connect", explode)

    with caplog.at_level(logging.CRITICAL, logger=LOGGER_NAME):
        with pytest.raises(MemoryError):
            database.init_db(str(tmp_path / "boom.db"))

    assert len([r for r in caplog.records if r.levelno == logging.CRITICAL]) == 1


def test_a_failing_migration_aborts_the_boot_and_records_no_version(legacy_path, monkeypatch, caplog):
    def refuse(conn):
        raise sqlite3.IntegrityError("UNIQUE constraint failed")

    monkeypatch.setattr(database, "_migration_v55_news_posts_author_index", refuse)

    with caplog.at_level(logging.CRITICAL, logger=LOGGER_NAME):
        with pytest.raises(sqlite3.IntegrityError):
            database.init_db(str(legacy_path))

    conn = _open(legacy_path)
    try:
        versions = _applied_versions(conn)
        assert 55 not in versions, "the version row is written only AFTER the function returns"
        assert 49 in versions, "everything before it stays committed"
    finally:
        conn.close()

    assert "Data migration v55" in caplog.text

    # the promise the missing row makes: the next boot retries
    # exactly this migration
    monkeypatch.undo()
    database.init_db(str(legacy_path))

    conn = _open(legacy_path)
    try:
        assert 55 in _applied_versions(conn)
        assert "idx_news_posts_author_source" in _index_names(conn)
    finally:
        conn.close()




# ===========================================================
# The ALTER guard clauses, both ways
# ===========================================================

@pytest.mark.parametrize("migration", [
    "_migration_v3_add_invited_column",
    "_migration_v5_add_student_fields",
    "_migration_v8_add_active_column",
    "_migration_v9_add_reply_and_delete",
    "_migration_v10_add_client_msg_id",
    "_migration_v11_add_push_language",
])
def test_a_duplicate_column_is_the_one_alter_failure_that_is_skipped(migration, caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        getattr(database, migration)(_FakeConnection("duplicate column name: whatever"))

    assert "already exists, skipping" in caplog.text


@pytest.mark.parametrize("migration", [
    "_migration_v3_add_invited_column",
    "_migration_v5_add_student_fields",
    "_migration_v8_add_active_column",
    "_migration_v9_add_reply_and_delete",
    "_migration_v10_add_client_msg_id",
    "_migration_v11_add_push_language",
])
def test_any_other_alter_failure_aborts_the_boot(migration):
    # a locked, full or readonly database must NOT be swallowed
    # as if the column were merely already there
    with pytest.raises(sqlite3.OperationalError):
        getattr(database, migration)(_FakeConnection("attempt to write a readonly database"))




# ===========================================================
# get_db
# ===========================================================

def test_get_db_refuses_before_init_db_has_run(monkeypatch):
    monkeypatch.setattr(database, "_db_path", None)

    with pytest.raises(RuntimeError, match="init_db"):
        database.get_db()


def test_get_db_sets_the_four_pragmas_every_open(app):
    conn = database.get_db()
    try:
        assert _one(conn, "PRAGMA journal_mode") == "wal"
        # per-connection, and ON DELETE CASCADE silently stops
        # working without it
        assert _one(conn, "PRAGMA foreign_keys") == 1
        assert _one(conn, "PRAGMA synchronous") == 1
        assert _one(conn, "PRAGMA busy_timeout") == 30000
    finally:
        conn.close()


def test_get_db_rows_are_addressable_by_column_name(app):
    conn = database.get_db()
    try:
        row = conn.execute("SELECT username, role FROM users WHERE username = 'admin'").fetchone()
        assert row["username"] == "admin"
        assert row["role"] == "admin"
    finally:
        conn.close()


def test_get_db_opens_the_file_init_db_registered(legacy_path):
    database.init_db(str(legacy_path))

    conn = database.get_db()
    try:
        assert _one(conn, "SELECT COUNT(*) FROM users") == 3
    finally:
        conn.close()




# ===========================================================
# utc_now_iso and sweep_expired_sessions
# ===========================================================

def test_utc_now_iso_is_timezone_aware_utc_in_t_form():
    frozen = datetime(2026, 8, 29, 12, 34, 56, 789012, tzinfo=timezone.utc)

    with time_machine.travel(frozen, tick=False):
        stamp = database.utc_now_iso()

    assert stamp == "2026-08-29T12:34:56.789012+00:00"
    assert " " not in stamp, "a space-form stamp is exactly what v17 had to clean up"


def _sessions_only_connection():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, expires_at TEXT NOT NULL)")
    return conn


def test_the_sweep_deletes_only_sessions_past_their_expiry(caplog):
    conn = _sessions_only_connection()
    conn.executemany("INSERT INTO sessions VALUES (?, ?)", [
        ("soon", datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc).isoformat()),
        ("later", datetime(2026, 1, 1, 23, 0, tzinfo=timezone.utc).isoformat()),
    ])

    with time_machine.travel(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc), tick=False):
        database.sweep_expired_sessions(conn)
    assert _one(conn, "SELECT COUNT(*) FROM sessions") == 2

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        with time_machine.travel(datetime(2026, 1, 1, 14, 0, tzinfo=timezone.utc), tick=False):
            database.sweep_expired_sessions(conn)

    assert [r[0] for r in conn.execute("SELECT id FROM sessions").fetchall()] == ["later"]
    assert "Swept 1 expired session row(s)" in caplog.text
    conn.close()


def test_a_session_expiring_exactly_now_is_not_yet_swept():
    frozen = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    conn = _sessions_only_connection()
    conn.execute("INSERT INTO sessions VALUES ('edge', ?)", (frozen.isoformat(),))

    with time_machine.travel(frozen, tick=False):
        database.sweep_expired_sessions(conn)

    assert _one(conn, "SELECT COUNT(*) FROM sessions") == 1, "the comparison is strictly less-than"
    conn.close()


def test_the_sweep_says_nothing_when_nothing_expired(caplog):
    conn = _sessions_only_connection()
    conn.execute("INSERT INTO sessions VALUES ('live', '2099-01-01T00:00:00+00:00')")

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        database.sweep_expired_sessions(conn)

    assert "Swept" not in caplog.text
    conn.close()


def test_a_later_boot_sweeps_a_session_that_expired_in_the_meantime(legacy_path, caplog):
    database.init_db(str(legacy_path))
    conn = _open(legacy_path)
    conn.execute("INSERT INTO sessions (id, user_id, token, created_at, expires_at)"
                 " VALUES ('s-stale', 'u1', 'stale-token', '2026-01-01T00:00:00+00:00',"
                 " '2026-01-02T00:00:00+00:00')")
    conn.commit()
    conn.close()

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        database.init_db(str(legacy_path))

    conn = _open(legacy_path)
    try:
        assert _one(conn, "SELECT COUNT(*) FROM sessions WHERE id = 's-stale'") == 0
    finally:
        conn.close()
    assert "Swept 1 expired session row(s)" in caplog.text
