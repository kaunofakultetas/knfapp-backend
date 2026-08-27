############################################################
#  [*] Database — SQLite bootstrap, connections, migrations
#
#  The one place the schema lives. create_app() calls
#  init_db() once per process; every request and job then
#  opens its own short-lived connection through get_db().
#  Three moving parts, in the order init_db runs them:
#
#    _SCHEMA          — CREATE TABLE/INDEX IF NOT EXISTS
#                       script, rerun on every boot
#    _seed_defaults   — admin/admin123, invite code
#                       WELCOME-KNF-2026 and 11 demo lessons;
#                       ONLY when the users table is empty
#    _run_migrations  — versioned one-shot migrations, each
#                       recorded in the _migrations table
#
#  Migration index (the _MIGRATIONS dict in _run_migrations):
#    v1  HTML-escape user text + trim oversized posts
#    v2  undo v1 — unescape until stable (escaping is now
#        done on output)
#    v3  users.invited (guest vs invited trust level)
#    v4  faculty_info table (scraped contacts/programs)
#    v5  users.student_number / study_group / study_program
#    v6  push_tokens table (Expo push)
#    v7  notification_channels table (per-topic opt-in)
#    v8  users.active (admin deactivation)
#    v9  messages.reply_to_id / deleted_at (quoted replies,
#        unsend)
#
#  Gotchas:
#    - _SCHEMA already carries the v4–v9 objects, so on a
#      fresh DB those migrations are no-ops (the v5/v8/v9
#      ALTERs fail with "duplicate column" and are
#      swallowed). users.invited is the exception: it is
#      NOT in _SCHEMA — only v3 adds it, and auth
#      registration INSERTs into it.
#    - A migration runs exactly once per DB file; editing
#      an applied one changes nothing until its _migrations
#      row is deleted.
#    - _CURRENT_MIGRATION_VERSION is read by nothing.
#    - init_db's own connection never enables foreign_keys,
#      so seeding and migrations run without FK checks.
############################################################


import html
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt

_db_path = None
logger = logging.getLogger(__name__)

# Dead constant: nothing reads it. The runner walks the
# _MIGRATIONS dict in _run_migrations, so "bumping" this
# re-runs nothing — add a dict entry instead. Left in place
# under the no-logic-change rule.
_CURRENT_MIGRATION_VERSION = 9








############################################################
# init_db
############################################################
#
# Boots the database file: runs the idempotent _SCHEMA
# script, seeds defaults when the users table is empty,
# then applies pending migrations. Also pins the
# module-level _db_path that get_db() reads. Uses a plain
# sqlite3.connect — no PRAGMA foreign_keys — so seeding and
# migrations run WITHOUT FK enforcement, and the file stays
# in the default journal mode until the first get_db()
# switches it to WAL. A migration that raises propagates
# out of here and aborts create_app().
#
# Used by:
#   - app/__init__.py — create_app(), once, inside
#     app.app_context()
############################################################

def init_db(db_path):
    # STEP 1: remember the path for get_db(), then create or
    # extend the schema — every statement is IF NOT EXISTS
    # ======================================================
    global _db_path
    _db_path = db_path

    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    conn.commit()


    # STEP 2: first boot only — an empty users table means a
    # brand-new file, so plant the admin, invite and lessons
    # ======================================================
    count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if count == 0:
        _seed_defaults(conn)


    # STEP 3: pending data migrations, each recorded in the
    # _migrations table once it succeeds
    # =====================================================
    _run_migrations(conn)

    conn.close()








############################################################
# get_db
############################################################
#
# Opens a fresh connection to the file init_db() registered,
# rows as sqlite3.Row (dict-style access). Two PRAGMAs on
# every open: journal_mode=WAL is persistent in the file so
# the repeat is harmless, but foreign_keys=ON is
# PER-CONNECTION and must be set here or ON DELETE CASCADE
# silently stops working. Callers own the connection — the
# house pattern is db = get_db(); try: ... finally:
# db.close().
#
# Used by:
#   - auth/routes.py, admin/routes.py, news/routes.py,
#     social/routes.py, chat/routes.py, chat/events.py,
#     schedule/routes.py, info/routes.py,
#     notifications/routes.py, notifications/push.py,
#     scraper/routes.py, scraper/knf_scraper.py,
#     scraper/vu_scraper.py, scraper/schedule_scraper.py,
#     scraper/info_scraper.py — every DB access in the app
############################################################

def get_db():
    conn = sqlite3.connect(_db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn








############################################################
# _seed_defaults
############################################################
#
# First-boot fixtures so the app is usable straight away:
#   - user admin / admin123 (role admin, bcrypt hash)
#   - invitation code WELCOME-KNF-2026, role student,
#     100 uses, valid 365 days from seeding
#   - 11 demo lessons for group ISKS-1, semester
#     "2025-pavasaris", day_of_week 0..4 (0 = Monday, the
#     same convention the schedule scraper uses)
# The demo semester label does NOT follow the scraper's
# "2025-P" / "2025-R" format, so it appears as a separate
# entry in the schedule filter UI, and nothing ever deletes
# these rows. The admin password is a known credential —
# change it on any real deployment.
#
# Used by:
#   - init_db (above) — only when SELECT COUNT(*) FROM
#     users is 0
############################################################

def _seed_defaults(conn):
    # STEP 1: the admin account
    # =========================
    admin_id = str(uuid.uuid4())
    pw_hash = bcrypt.hashpw(b"admin123", bcrypt.gensalt()).decode()

    conn.execute(
        "INSERT INTO users (id, username, email, display_name, password_hash, role) VALUES (?, ?, ?, ?, ?, ?)",
        (admin_id, "admin", "admin@knf.vu.lt", "Administratorius", pw_hash, "admin"),
    )


    # STEP 2: a reusable invitation code (max_uses 100) that
    # the admin "created", expiring a year from now
    # ======================================================
    invite_id = str(uuid.uuid4())
    code = "WELCOME-KNF-2026"
    expires = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
    conn.execute(
        "INSERT INTO invitation_codes (id, code, role, created_by, max_uses, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
        (invite_id, code, "student", admin_id, 100, expires),
    )


    # STEP 3: demo timetable — tuple order is (title, teacher,
    # room, time_start, time_end, day_of_week, group, semester)
    # =========================================================
    lessons = [
        ("Kalbos kultūra ir akademinis raštingumas", "Doc. dr. R. Baranauskienė", "201", "08:30", "10:00", 0, "ISKS-1", "2025-pavasaris"),
        ("Informacinės technologijos", "Lekt. T. Vanagas", "305", "10:15", "11:45", 0, "ISKS-1", "2025-pavasaris"),
        ("Matematika", "Prof. dr. A. Kazlauskas", "101", "12:00", "13:30", 0, "ISKS-1", "2025-pavasaris"),
        ("Filosofijos įvadas", "Doc. dr. V. Rimkus", "202", "14:00", "15:30", 0, "ISKS-1", "2025-pavasaris"),
        ("Programavimo pagrindai", "Lekt. T. Vanagas", "305", "08:30", "10:00", 1, "ISKS-1", "2025-pavasaris"),
        ("Anglų kalba B2", "Lekt. J. Brown", "203", "10:15", "11:45", 1, "ISKS-1", "2025-pavasaris"),
        ("Statistika", "Doc. dr. S. Petravičius", "101", "08:30", "10:00", 2, "ISKS-1", "2025-pavasaris"),
        ("Ekonomikos pagrindai", "Prof. dr. K. Jonaitis", "202", "10:15", "11:45", 2, "ISKS-1", "2025-pavasaris"),
        ("Teisės pagrindai", "Doc. dr. A. Navickas", "201", "12:00", "13:30", 3, "ISKS-1", "2025-pavasaris"),
        ("Psichologijos įvadas", "Prof. dr. L. Mikalauskaitė", "301", "14:00", "15:30", 3, "ISKS-1", "2025-pavasaris"),
        ("Kūno kultūra", "Lekt. M. Sportininkas", "Sporto salė", "08:30", "10:00", 4, "ISKS-1", "2025-pavasaris"),
    ]
    for title, teacher, room, start, end, day, group, semester in lessons:
        conn.execute(
            "INSERT INTO schedule_lessons (id, title, teacher, room, time_start, time_end, day_of_week, group_name, semester) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), title, teacher, room, start, end, day, group, semester),
        )

    conn.commit()
    logger.info("Seeded default admin (admin/admin123), invitation code WELCOME-KNF-2026, and schedule")








############################################################
# _run_migrations
############################################################
#
# Versioned one-shot migrations. The _migrations table (not
# part of _SCHEMA — created here) records every applied
# version; the registry dict maps version → (label,
# function) and is walked in ascending order, skipping
# versions already recorded. The row is inserted only AFTER
# the function returns, so a migration that raises aborts
# startup and is retried on the next boot — write them
# idempotent (every existing one is). Adding a migration =
# a new _migration_vN function plus a dict entry; the
# _CURRENT_MIGRATION_VERSION constant plays no part.
#
# Used by:
#   - init_db (above) — every boot, after seeding
############################################################

def _run_migrations(conn):
    # STEP 1: bookkeeping table — lives outside _SCHEMA on
    # purpose, so a hand-built DB still gets it
    # ====================================================
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()


    # STEP 2: the registry — version → (log label, function).
    # Functions resolve at call time, which is why v8 and v9
    # may be defined below _SCHEMA
    # =======================================================
    _MIGRATIONS = {
        1: ("XSS payload cleanup + oversized data trim", _migration_v1_xss_cleanup),
        2: ("Reverse double-escaped HTML entities (input-escaping removed)", _migration_v2_unescape_double_escapes),
        3: ("Add invited column to users for trust levels", _migration_v3_add_invited_column),
        4: ("Add faculty_info table for scraped faculty data", _migration_v4_add_faculty_info_table),
        5: ("Add student fields to users table", _migration_v5_add_student_fields),
        6: ("Add push_tokens table for push notifications", _migration_v6_add_push_tokens),
        7: ("Add notification_channels table for per-topic opt-in", _migration_v7_add_notification_channels),
        8: ("Add active column to users for admin deactivation", _migration_v8_add_active_column),
        9: ("Add reply_to_id and deleted_at to messages", _migration_v9_add_reply_and_delete),
    }


    # STEP 3: apply what is missing, lowest version first;
    # the version row is committed together with the
    # migration's own writes
    # ====================================================
    for version in sorted(_MIGRATIONS.keys()):
        applied = conn.execute(
            "SELECT version FROM _migrations WHERE version = ?",
            (version,),
        ).fetchone()

        if applied:
            continue

        desc, fn = _MIGRATIONS[version]
        logger.info("Running data migration v%d: %s", version, desc)
        fn(conn)
        conn.execute(
            "INSERT INTO _migrations (version) VALUES (?)",
            (version,),
        )
        conn.commit()
        logger.info("Data migration v%d complete", version)








############################################################
# _migration_v1_xss_cleanup
############################################################
#
# Migration v1: HTML-escape (html.escape, quote=True) every
# user-generated text column and trim oversized records.
# Columns: users.display_name; news_posts.title / content /
# summary / author_name; news_comments.text; messages.text;
# conversations.title; polls.title; poll_options.text.
# Titles over 200 and content over 10000 chars are cut
# (prompted by post f162b474 carrying a 100k-char title),
# and users.avatar_url is NULLed unless its scheme is
# http/https. Superseded by v2, which reverses the
# escaping — kept so the version chain stays intact. On a
# fresh DB every table is empty and this is a no-op. The
# f-string table/column names come from the literal calls
# below, never from input.
#
# Used by:
#   - _run_migrations (above) — via the _MIGRATIONS dict
############################################################

def _migration_v1_xss_cleanup(conn):
    MAX_TITLE_LEN = 200
    MAX_CONTENT_LEN = 10000

    # Escapes every non-NULL value of one column in place;
    # rows already escaped compare equal and are skipped
    def _escape_column(table, column, id_column="id"):
        rows = conn.execute(
            f"SELECT {id_column}, {column} FROM {table} WHERE {column} IS NOT NULL"
        ).fetchall()
        updated = 0
        for row in rows:
            raw = row[1]
            escaped = html.escape(raw, quote=True)
            if escaped != raw:
                conn.execute(
                    f"UPDATE {table} SET {column} = ? WHERE {id_column} = ?",
                    (escaped, row[0]),
                )
                updated += 1
        if updated:
            logger.info("  Escaped %d rows in %s.%s", updated, table, column)


    # STEP 1: users and news_posts first — the truncation in
    # STEP 2 measures the ESCAPED length, so a cut can land
    # inside an &amp; entity
    # ======================================================
    _escape_column("users", "display_name")

    _escape_column("news_posts", "title")
    _escape_column("news_posts", "content")
    _escape_column("news_posts", "summary")
    _escape_column("news_posts", "author_name")


    # STEP 2: trim oversized titles and content
    # =========================================
    oversized = conn.execute(
        "SELECT id, title FROM news_posts WHERE LENGTH(title) > ?",
        (MAX_TITLE_LEN,),
    ).fetchall()
    for row in oversized:
        truncated = row[1][:MAX_TITLE_LEN]
        conn.execute("UPDATE news_posts SET title = ? WHERE id = ?", (truncated, row[0]))
        logger.info("  Truncated oversized title on post %s (was %d chars)", row[0], len(row[1]))

    oversized_content = conn.execute(
        "SELECT id, content FROM news_posts WHERE LENGTH(content) > ?",
        (MAX_CONTENT_LEN,),
    ).fetchall()
    for row in oversized_content:
        truncated = row[1][:MAX_CONTENT_LEN]
        conn.execute("UPDATE news_posts SET content = ? WHERE id = ?", (truncated, row[0]))
        logger.info("  Truncated oversized content on post %s (was %d chars)", row[0], len(row[1]))


    # STEP 3: the remaining text columns — comments, chat,
    # polls
    # ====================================================
    _escape_column("news_comments", "text")

    _escape_column("messages", "text")

    _escape_column("conversations", "title")

    _escape_column("polls", "title")

    _escape_column("poll_options", "text")


    # STEP 4: avatar URLs — anything that is not http(s)
    # (javascript:, data:, relative paths) is cleared;
    # urlparse only raises on malformed IPv6 brackets, the
    # except covers that
    # ====================================================
    from urllib.parse import urlparse
    bad_avatars = conn.execute(
        "SELECT id, avatar_url FROM users WHERE avatar_url IS NOT NULL AND avatar_url != ''"
    ).fetchall()
    for row in bad_avatars:
        try:
            parsed = urlparse(row[1])
            if parsed.scheme.lower() not in ("http", "https"):
                conn.execute("UPDATE users SET avatar_url = NULL WHERE id = ?", (row[0],))
                logger.info("  Cleared invalid avatar_url (scheme=%s) for user %s", parsed.scheme, row[0])
        except Exception:
            conn.execute("UPDATE users SET avatar_url = NULL WHERE id = ?", (row[0],))
            logger.info("  Cleared unparseable avatar_url for user %s", row[0])

    conn.commit()








############################################################
# _migration_v2_unescape_double_escapes
############################################################
#
# Migration v2: reverse the escaping v1 applied plus the
# layers piled on by the old before_request middleware,
# which html.escape()d every write — each round-trip edit
# added one more layer (& → &amp; → &amp;amp; …). Escaping
# now happens on OUTPUT only, so stored text must be raw:
# every value is html.unescape()d repeatedly until it stops
# changing. Scraper rows (source knf.vu.lt / vu.lt) are
# included on purpose — the scraper stored raw text and v1
# escaped it. Same column list as v1. Side effect: a user
# who genuinely typed "&amp;" loses that too.
#
# Used by:
#   - _run_migrations (above) — via the _MIGRATIONS dict
############################################################

def _migration_v2_unescape_double_escapes(conn):
    # Peels one escape layer per pass until a pass changes
    # nothing, then writes back only the rows that moved
    def _unescape_column(table, column, id_column="id"):
        rows = conn.execute(
            f"SELECT {id_column}, {column} FROM {table} WHERE {column} IS NOT NULL"
        ).fetchall()
        updated = 0
        for row in rows:
            current = row[1]
            unescaped = html.unescape(current)
            while unescaped != current:
                current = unescaped
                unescaped = html.unescape(current)
            if unescaped != row[1]:
                conn.execute(
                    f"UPDATE {table} SET {column} = ? WHERE {id_column} = ?",
                    (unescaped, row[0]),
                )
                updated += 1
        if updated:
            logger.info("  Unescaped %d rows in %s.%s", updated, table, column)

    _unescape_column("users", "display_name")

    _unescape_column("news_posts", "title")
    _unescape_column("news_posts", "content")
    _unescape_column("news_posts", "summary")
    _unescape_column("news_posts", "author_name")

    _unescape_column("news_comments", "text")

    _unescape_column("messages", "text")
    _unescape_column("conversations", "title")

    _unescape_column("polls", "title")
    _unescape_column("poll_options", "text")

    conn.commit()








############################################################
# _migration_v3_add_invited_column
############################################################
#
# Migration v3: users.invited INTEGER NOT NULL DEFAULT 1 —
# 1 when the account was registered with an invitation code
# (higher trust), 0 for guest sign-ups. Every pre-existing
# user came through an invitation code, hence the default
# of 1. This column is NOT in _SCHEMA: a fresh database
# gets it from this migration alone, and auth registration
# INSERTs into it — drop this entry and sign-up breaks.
# SQLite has no ADD COLUMN IF NOT EXISTS, so re-runs rely
# on the except swallowing "duplicate column".
#
# Used by:
#   - _run_migrations (above) — via the _MIGRATIONS dict
############################################################

def _migration_v3_add_invited_column(conn):
    try:
        conn.execute("ALTER TABLE users ADD COLUMN invited INTEGER NOT NULL DEFAULT 1")
        logger.info("  Added 'invited' column to users table (existing users marked invited=1)")
    except Exception:
        # "duplicate column name" — only on a re-run, since
        # _SCHEMA never creates this column
        logger.info("  'invited' column already exists, skipping")
    conn.commit()








############################################################
# _migration_v4_add_faculty_info_table
############################################################
#
# Migration v4: faculty_info — scraped contacts, programs
# and structure from knf.vu.lt as JSON blobs, one row per
# (lang, section). The identical CREATE TABLE now also
# lives in _SCHEMA, so on a fresh DB this is a silent
# no-op (IF NOT EXISTS).
#
# Used by:
#   - _run_migrations (above) — via the _MIGRATIONS dict
############################################################

def _migration_v4_add_faculty_info_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS faculty_info (
            id TEXT PRIMARY KEY,
            lang TEXT NOT NULL DEFAULT 'lt',
            section TEXT NOT NULL,
            data_json TEXT NOT NULL,
            scraped_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(lang, section)
        )
    """)
    conn.commit()
    logger.info("  Created faculty_info table")








############################################################
# _migration_v5_add_student_fields
############################################################
#
# Migration v5: users.student_number / study_group /
# study_program (TEXT, nullable) for the digital Student ID
# card. The columns are also in _SCHEMA, so on a fresh DB
# every ALTER fails with "duplicate column" and is
# swallowed — the three "already exists" log lines are
# expected there. The `default` value is interpolated into
# the DDL as SQL text ("NULL"), not bound as a parameter.
#
# Used by:
#   - _run_migrations (above) — via the _MIGRATIONS dict
############################################################

def _migration_v5_add_student_fields(conn):
    for col, default in [
        ("student_number", "NULL"),
        ("study_group", "NULL"),
        ("study_program", "NULL"),
    ]:
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT DEFAULT {default}")
            logger.info("  Added '%s' column to users table", col)
        except Exception:
            logger.info("  '%s' column already exists, skipping", col)
    conn.commit()








############################################################
# _migration_v6_add_push_tokens
############################################################
#
# Migration v6: push_tokens — one Expo push token per
# user/device (unique on token, indexed on user_id) so the
# server can push new messages, news and admin
# announcements. Duplicated in _SCHEMA; no-op on a fresh
# DB.
#
# Used by:
#   - _run_migrations (above) — via the _MIGRATIONS dict
############################################################

def _migration_v6_add_push_tokens(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS push_tokens (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token TEXT NOT NULL,
            platform TEXT NOT NULL DEFAULT 'unknown',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_push_tokens_user ON push_tokens(user_id)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_push_tokens_token ON push_tokens(token)")
    conn.commit()
    logger.info("  Created push_tokens table")








############################################################
# _migration_v7_add_notification_channels
############################################################
#
# Migration v7: notification_channels — per-user opt-in per
# topic, PRIMARY KEY (user_id, channel), channel CHECKed to
# news / chat / schedule / admin. No rows are inserted: a
# missing row means enabled (notifications/push.py only
# skips on an explicit enabled=0), so existing users keep
# every channel on. Duplicated in _SCHEMA, which also adds
# the user_id index this migration lacks; no-op on a fresh
# DB.
#
# Used by:
#   - _run_migrations (above) — via the _MIGRATIONS dict
############################################################

def _migration_v7_add_notification_channels(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notification_channels (
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            channel TEXT NOT NULL CHECK(channel IN ('news', 'chat', 'schedule', 'admin')),
            enabled INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, channel)
        )
    """)
    conn.commit()
    logger.info("  Created notification_channels table")








############################################################
# _SCHEMA
############################################################
#
# The full CREATE script, run by init_db() on every boot
# through executescript. Every statement is IF NOT EXISTS,
# so it only ever ADDS objects — a column added to an
# existing table here does nothing for deployed databases;
# that is what the migrations are for. Tables: users,
# invitation_codes, sessions, news_posts, news_likes,
# news_comments, polls, poll_options, poll_votes,
# schedule_lessons, scraper_runs, conversations,
# conversation_participants, messages, message_reactions,
# message_reads, friendships, friend_requests,
# faculty_info, push_tokens, notification_channels, plus
# their indexes. Conventions: TEXT uuid4 ids, ISO
# timestamps from datetime('now') (UTC), integer booleans,
# ON DELETE CASCADE off users. Not here: users.invited
# (v3 only) and the _migrations bookkeeping table
# (_run_migrations). push_tokens / notification_channels
# sit between two index groups — appended by later
# features, harmless. Keep this string byte-identical
# unless the schema really changes.
#
# Used by:
#   - init_db (above)
############################################################

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    email TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'student' CHECK(role IN ('student', 'teacher', 'admin', 'curator')),
    avatar_url TEXT,
    student_number TEXT,
    study_group TEXT,
    study_program TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS invitation_codes (
    id TEXT PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    role TEXT NOT NULL DEFAULT 'student' CHECK(role IN ('student', 'teacher', 'admin', 'curator')),
    created_by TEXT REFERENCES users(id),
    max_uses INTEGER NOT NULL DEFAULT 1,
    use_count INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS news_posts (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    summary TEXT,
    image_url TEXT,
    author_id TEXT REFERENCES users(id),
    author_name TEXT,
    source TEXT NOT NULL DEFAULT 'app' CHECK(source IN ('app', 'knf.vu.lt', 'vu.lt', 'faculty', 'user')),
    source_url TEXT UNIQUE,
    post_type TEXT NOT NULL DEFAULT 'article' CHECK(post_type IN ('article', 'social', 'announcement', 'poll', 'link')),
    is_public INTEGER NOT NULL DEFAULT 1,
    likes_count INTEGER NOT NULL DEFAULT 0,
    comments_count INTEGER NOT NULL DEFAULT 0,
    shares_count INTEGER NOT NULL DEFAULT 0,
    published_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS news_likes (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    post_id TEXT NOT NULL REFERENCES news_posts(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, post_id)
);

CREATE TABLE IF NOT EXISTS news_comments (
    id TEXT PRIMARY KEY,
    post_id TEXT NOT NULL REFERENCES news_posts(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS polls (
    id TEXT PRIMARY KEY,
    post_id TEXT NOT NULL REFERENCES news_posts(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    end_date TEXT,
    total_votes INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS poll_options (
    id TEXT PRIMARY KEY,
    poll_id TEXT NOT NULL REFERENCES polls(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    votes INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS poll_votes (
    user_id TEXT NOT NULL,
    poll_id TEXT NOT NULL REFERENCES polls(id) ON DELETE CASCADE,
    option_id TEXT NOT NULL REFERENCES poll_options(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, poll_id)
);

CREATE TABLE IF NOT EXISTS schedule_lessons (
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

CREATE TABLE IF NOT EXISTS scraper_runs (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running' CHECK(status IN ('running', 'completed', 'failed')),
    articles_found INTEGER NOT NULL DEFAULT 0,
    articles_new INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL DEFAULT 'direct' CHECK(type IN ('direct', 'group')),
    title TEXT,
    avatar_emoji TEXT,
    created_by TEXT REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS conversation_participants (
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    pinned INTEGER NOT NULL DEFAULT 0,
    last_read_at TEXT,
    joined_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (conversation_id, user_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    sender_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    text TEXT NOT NULL DEFAULT '',
    image_url TEXT,
    reply_to_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
    deleted_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS message_reactions (
    message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    emoji TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (message_id, user_id)
);

CREATE TABLE IF NOT EXISTS message_reads (
    message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    read_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (message_id, user_id)
);

CREATE TABLE IF NOT EXISTS friendships (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    friend_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, friend_id)
);

CREATE TABLE IF NOT EXISTS friend_requests (
    id TEXT PRIMARY KEY,
    from_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    to_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'accepted', 'rejected')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS faculty_info (
    id TEXT PRIMARY KEY,
    lang TEXT NOT NULL DEFAULT 'lt',
    section TEXT NOT NULL,
    data_json TEXT NOT NULL,
    scraped_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(lang, section)
);

CREATE INDEX IF NOT EXISTS idx_news_posts_published ON news_posts(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_posts_source ON news_posts(source);
CREATE INDEX IF NOT EXISTS idx_news_comments_post ON news_comments(post_id);
CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(token);
CREATE INDEX IF NOT EXISTS idx_invitation_codes_code ON invitation_codes(code);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_conversation_participants_user ON conversation_participants(user_id);
CREATE INDEX IF NOT EXISTS idx_message_reactions_message ON message_reactions(message_id);
CREATE INDEX IF NOT EXISTS idx_message_reads_message ON message_reads(message_id);
CREATE INDEX IF NOT EXISTS idx_message_reads_user ON message_reads(user_id);
CREATE INDEX IF NOT EXISTS idx_friendships_user ON friendships(user_id);
CREATE INDEX IF NOT EXISTS idx_friendships_friend ON friendships(friend_id);
CREATE TABLE IF NOT EXISTS push_tokens (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT 'unknown',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS notification_channels (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    channel TEXT NOT NULL CHECK(channel IN ('news', 'chat', 'schedule', 'admin')),
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, channel)
);

CREATE INDEX IF NOT EXISTS idx_friend_requests_to ON friend_requests(to_user_id, status);
CREATE INDEX IF NOT EXISTS idx_friend_requests_from ON friend_requests(from_user_id, status);
CREATE INDEX IF NOT EXISTS idx_push_tokens_user ON push_tokens(user_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_push_tokens_token ON push_tokens(token);
CREATE INDEX IF NOT EXISTS idx_notification_channels_user ON notification_channels(user_id);
"""








############################################################
# _migration_v8_add_active_column
############################################################
#
# Migration v8: users.active INTEGER NOT NULL DEFAULT 1.
# Admin deactivation used to lazily ALTER the table from
# inside the admin request and nothing ever checked the
# flag — a deactivated user could simply log in again. The
# column is now part of _SCHEMA and enforced by login and
# by get_current_user (auth/routes.py); admin/routes.py
# flips it and drops the user's sessions. On a fresh DB the
# ALTER fails with "duplicate column" and is swallowed.
# Defined after _SCHEMA rather than next to v7 — the
# _MIGRATIONS dict resolves it at call time, so placement
# is irrelevant.
#
# Used by:
#   - _run_migrations (above) — via the _MIGRATIONS dict
############################################################

def _migration_v8_add_active_column(conn):
    try:
        conn.execute("ALTER TABLE users ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        logger.info("  Added 'active' column to users table")
    except Exception:
        # The normal path on a fresh DB: _SCHEMA already
        # created the column, so the ALTER is a duplicate
        logger.info("  'active' column already exists, skipping")
    conn.commit()








############################################################
# _migration_v9_add_reply_and_delete
############################################################
#
# Migration v9: messages.reply_to_id (quoted replies —
# points at the quoted message, nulled if that row ever
# goes away) and messages.deleted_at ("unsend" — a soft
# delete: the row survives so replies keep their target,
# cursors keep their order, and every reader shows a
# placeholder instead of the content). Both columns are
# also in _SCHEMA, so on a fresh DB each ALTER fails with
# "duplicate column" and is swallowed. Defined after
# _SCHEMA like v8 — the _MIGRATIONS dict resolves it at
# call time.
#
# Used by:
#   - _run_migrations (above) — via the _MIGRATIONS dict
############################################################

def _migration_v9_add_reply_and_delete(conn):
    for statement in (
        "ALTER TABLE messages ADD COLUMN reply_to_id TEXT REFERENCES messages(id) ON DELETE SET NULL",
        "ALTER TABLE messages ADD COLUMN deleted_at TEXT",
    ):
        try:
            conn.execute(statement)
            logger.info("  %s", statement)
        except Exception:
            # The normal path on a fresh DB: _SCHEMA already
            # created the column, so the ALTER is a duplicate
            logger.info("  column already exists, skipping: %s", statement)
    conn.commit()
