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
#    _seed_defaults   — admin with a generated (or
#                       ADMIN_PASSWORD env) password plus a
#                       generated bootstrap invitation code,
#                       each logged exactly once; ONLY when
#                       the database file did not yet exist
#    _run_migrations  — versioned one-shot migrations, each
#                       recorded in the _migrations table
#
#  Migration index (the _MIGRATIONS dict in _run_migrations):
#    v1  retired to a recorded no-op (was: HTML-escape
#        user text — destructive on any replay)
#    v2  retired to a recorded no-op (was: unescape until
#        stable — destructive on any replay)
#    v3  users.invited (guest vs invited trust level)
#    v4  faculty_info table (scraped contacts/programs)
#    v5  users.student_number / study_group / study_program
#    v6  push_tokens table (Expo push)
#    v7  notification_channels table (per-topic opt-in)
#    v8  users.active (admin deactivation)
#    v9  messages.reply_to_id / deleted_at (quoted replies,
#        unsend)
#    v10 messages.client_msg_id + unique send-nonce index
#        (idempotent chat sends)
#    v11 push_tokens.language (per-device push copy in the
#        app language)
#    v12 expire the legacy WELCOME-KNF-2026 bootstrap invite
#    v13 sessions.token stored as sha256 + expired-row purge
#    v14 reconcile denormalised news/poll counters from rows
#    v15 indexes on hot foreign-key child columns
#    v16 rebuild four tables: ON DELETE SET NULL for
#        created_by/author_id, FK on poll_votes.user_id
#    v17 normalise legacy space-form timestamps to ISO-T
#    v18 schedule_lessons dedupe + natural-key unique index
#    v19 scraper_runs(started_at) index + one-off prune
#    v20 messages_fts FTS5 shadow table + sync triggers
#    v21 unique pending (from,to) pair on friend_requests
#    v22 drop six indexes duplicating implicit PK/UNIQUE
#    v23 delete orphaned message_reads rows
#    v25 deleted_source_urls tombstones so the scrapers stop
#        resurrecting deleted articles
#    v26 dedupe polls per post, then UNIQUE polls(post_id)
#        + poll_options(poll_id) index
#    v35 canonicalise news_posts.source_url and drop the
#        duplicate articles that exposes
#    v36 scraper_runs(source, started_at DESC) index for the
#        per-source status block + close interrupted runs
#    v40 admin_audit table (trail of every privileged
#        action taken through app/admin/routes.py)
#    v43 uploads table (owner + byte size per stored file)
#        for the per-user quota, the DELETE route and the
#        orphan sweep
#    v46 push_tokens(active, user_id) index for the push
#        fan-out scan
#    v47 delete push_tokens rows that are not valid Expo
#        tokens (the old prefix-only intake check)
#    v49 demote planted multi-member 'direct' conversations
#        to 'group' (chat dedup audit)
#    v55 composite news_posts(author_id, source,
#        published_at DESC) index for the profile lists
#    v56 user_blocks + reports tables, users.chat_push_preview
#        (block/report abuse handling, content-free pushes)
#
#  Gotchas:
#    - _SCHEMA already carries the v3–v11 objects
#      (users.invited included), so on a fresh DB the
#      column ALTERs fail with "duplicate column" and are
#      deliberately skipped — any OTHER OperationalError
#      re-raises and aborts startup so the retry lands on
#      the next boot.
#    - A migration runs exactly once per DB file; editing
#      an applied one changes nothing until its _migrations
#      row is deleted.
#    - init_db's connection runs with foreign_keys=ON and
#      audits PRAGMA foreign_key_check after migrating.
############################################################


import hashlib
import logging
import os
import secrets
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt

_db_path = None
logger = logging.getLogger(__name__)








############################################################
# init_db
############################################################
#
# Boots the database file: refuses a blank path, runs the
# idempotent _SCHEMA script, seeds defaults on a genuinely
# brand-new FILE (os.path.exists is checked BEFORE connect,
# because sqlite3.connect itself creates the file — an
# existing file with an empty users table gets a loud
# warning, never a silent re-seed), then applies pending
# migrations with foreign_keys=ON, audits PRAGMA
# foreign_key_check, and sweeps expired session rows. Also
# pins the module-level _db_path that get_db() reads — but
# only once the path has passed the blank check, so a
# refused boot leaves get_db() pointed where it was.
# "database is locked" at any step is retried with backoff
# (5 attempts over ~30 s) before giving up; every terminal
# failure logs one CRITICAL line naming the DB path so the
# container's restart loop self-describes.
#
# Used by:
#   - app/__init__.py — create_app(), once, inside
#     app.app_context()
############################################################

def init_db(db_path):
    # STEP 1: refuse a blank path. A compose file carrying
    # `DB_PATH=` (set but empty) reaches here as "", and
    # sqlite3.connect("") opens a PRIVATE TEMPORARY database:
    # the whole boot would report success — schema, seeded
    # admin, every migration — and be destroyed on close,
    # leaving every request to 500 on "no such table"
    # =======================================================
    global _db_path

    if isinstance(db_path, str) and not db_path.strip():
        logger.critical("Database init FAILED: DB_PATH is blank — refusing to boot a throwaway database")
        raise ValueError("DB_PATH is blank")


    # STEP 2: remember the path for get_db(), and test for a
    # brand-new file BEFORE connect creates it
    # ======================================================
    _db_path = db_path

    fresh_file = not os.path.exists(db_path)


    # STEP 3: schema + seed + migrations + audits, retried on
    # "database is locked" — attempt N sleeps N*3 s, so five
    # attempts spread over ~30 s before the boot aborts
    # =======================================================
    attempts = 5
    for attempt in range(1, attempts + 1):
        conn = None
        try:
            # STEP 3.1: connect with FK enforcement ON — seeding
            # and migrations must obey the same rules as requests
            conn = sqlite3.connect(db_path, timeout=30)
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(_SCHEMA)
            conn.commit()

            # STEP 3.2: seed ONLY a brand-new file; an existing
            # file with zero users means the wrong DB_PATH or a
            # wiped volume — warn loudly instead of re-planting
            # a fresh admin over someone's data directory
            count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            if count == 0:
                if fresh_file:
                    _seed_defaults(conn)
                else:
                    logger.warning(
                        "Database %s already existed but has ZERO users — refusing to re-seed defaults; "
                        "check DB_PATH / the mounted volume", db_path,
                    )

            # STEP 3.3: pending data migrations, each recorded
            # in the _migrations table once it succeeds
            _run_migrations(conn)

            # STEP 3.4: FK audit — enforcement only guards new
            # writes, so surface any legacy orphans in the log
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            for v in violations:
                logger.warning("Foreign-key violation: table=%s rowid=%s references %s", v[0], v[1], v[2])

            # STEP 3.5: boot-time sweep of expired sessions (the
            # scheduler repeats this on its tick)
            sweep_expired_sessions(conn)
            conn.commit()
            return
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e).lower() and attempt < attempts:
                logger.warning(
                    "Database %s locked during init (attempt %d/%d) — retrying in %d s",
                    db_path, attempt, attempts, attempt * 3,
                )
                time.sleep(attempt * 3)
                continue
            logger.critical("Database init FAILED for %s: %s", db_path, e)
            raise
        except Exception as e:
            logger.critical("Database init FAILED for %s: %s", db_path, e)
            raise
        finally:
            if conn is not None:
                conn.close()








############################################################
# get_db
############################################################
#
# Opens a fresh connection to the file init_db() registered,
# rows as sqlite3.Row (dict-style access) — and refuses with
# a clear RuntimeError when init_db() has not run yet
# (sqlite3.connect(None) would raise an opaque TypeError).
# The guard tests FALSINESS, not None: an empty path would
# otherwise sail past it and hand back a private temporary
# database with no tables in it, 500ing every request.
# Four PRAGMAs on every open: journal_mode=WAL is persistent
# in the file so the repeat is harmless; foreign_keys=ON is
# PER-CONNECTION and must be set here or ON DELETE CASCADE
# silently stops working; synchronous=NORMAL is the standard
# WAL pairing (fsync on checkpoint, not on every commit);
# busy_timeout=30000 makes writers WAIT up to 30 s on a
# locked database instead of raising OperationalError into
# a 500 after SQLite's 5 s default. connect() itself takes
# timeout=15 as well, because the first PRAGMA runs BEFORE
# busy_timeout is set and would otherwise fall back to those
# same 5 seconds. Callers own the connection — the house
# pattern is db = get_db(); try: ... finally: db.close().
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
    if not _db_path:
        raise RuntimeError("init_db() has not been called with a usable database path")

    conn = sqlite3.connect(_db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn








############################################################
# utc_now_iso
############################################################
#
# The house timestamp: timezone-aware UTC in ISO-8601 T-form
# ("2026-08-29T12:34:56.789012+00:00"). Python-side INSERTs
# and UPDATEs should stamp with this instead of letting a
# column's datetime('now') DEFAULT fire — the DEFAULT writes
# space-form text that sorts wrong against T-form under
# SQLite's string comparison. Migration v17 normalised the
# legacy space-form rows; the SQL DEFAULTs remain only as
# never-firing backstops.
#
# Used by:
#   - init_db / sweep_expired_sessions /
#     _migration_v12_expire_bootstrap_invite /
#     _migration_v13_hash_session_tokens (this file)
#   - news/routes.py — the explicit created_at stamps on the
#     comment and poll-vote INSERTs
############################################################

def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()








############################################################
# sweep_expired_sessions
############################################################
#
# Deletes every sessions row whose expires_at is in the
# past. Before this sweep the only cleanup was the lazy
# per-token purge in auth's get_current_user, so rows whose
# tokens were never presented again piled up forever. All
# expires_at values are Python isoformat T-form UTC (the
# INSERTs always supply them; v17 normalised any stragglers),
# so the plain string comparison is correct. The caller owns
# the connection AND the commit.
#
# Used by:
#   - init_db (above) — once per boot
#   - the scheduler tick (app/__init__.py) — periodic re-run
############################################################

def sweep_expired_sessions(conn):
    swept = conn.execute("DELETE FROM sessions WHERE expires_at < ?", (utc_now_iso(),)).rowcount
    if swept:
        logger.info("Swept %d expired session row(s)", swept)








############################################################
# _seed_defaults
############################################################
#
# First-boot fixtures so the app is usable straight away:
#   - user admin — password from the ADMIN_PASSWORD env var
#     when set, otherwise a cryptographically random secret
#     (secrets.token_urlsafe); never a fixed string
#   - a generated bootstrap invitation code ("KNF-" + 16
#     random hex chars), role student, 100 uses, valid 365
#     days from seeding
# The generated password and code are logged EXACTLY ONCE,
# here — read the first-boot log or the account is
# unreachable. No demo timetable any more: the schedule
# scraper fills schedule_lessons within its first tick, and
# the old 11-lesson "2025-pavasaris" fixture only polluted
# the semester filter.
#
# Used by:
#   - init_db (above) — only when the database file did not
#     exist before this boot
############################################################

def _seed_defaults(conn):
    # STEP 1: the admin account — env-provided or generated
    # password, bcrypt-hashed; invited=1 (the seeded admin is
    # the anchor of the invite tree, not a guest)
    # ======================================================
    admin_id = str(uuid.uuid4())
    admin_password = os.environ.get("ADMIN_PASSWORD") or secrets.token_urlsafe(16)
    pw_hash = bcrypt.hashpw(admin_password.encode(), bcrypt.gensalt()).decode()

    conn.execute(
        "INSERT INTO users (id, username, email, display_name, password_hash, role, invited) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (admin_id, "admin", "admin@knf.vu.lt", "Administratorius", pw_hash, "admin", 1),
    )


    # STEP 2: a reusable bootstrap invitation code (max_uses
    # 100) that the admin "created", expiring a year from now
    # ======================================================
    invite_id = str(uuid.uuid4())
    code = "KNF-" + secrets.token_hex(8).upper()
    expires = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
    conn.execute(
        "INSERT INTO invitation_codes (id, code, role, created_by, max_uses, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
        (invite_id, code, "student", admin_id, 100, expires),
    )

    conn.commit()


    # STEP 3: the one and only print of the generated secrets
    # — an env-set password is acknowledged, never echoed
    # =======================================================
    if os.environ.get("ADMIN_PASSWORD"):
        logger.warning(
            "FIRST BOOT: seeded user 'admin' with the password from ADMIN_PASSWORD; bootstrap invitation code: %s",
            code,
        )
    else:
        logger.warning(
            "FIRST BOOT: seeded user 'admin' with generated password: %s — shown ONCE, store it now; "
            "bootstrap invitation code: %s",
            admin_password, code,
        )








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
# a new _migration_vN function plus a dict entry; there is
# no version constant to bump — the dict IS the registry.
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
        10: ("Add client_msg_id to messages for idempotent sends", _migration_v10_add_client_msg_id),
        11: ("Add language to push_tokens for per-device push copy", _migration_v11_add_push_language),
        12: ("Expire the legacy WELCOME-KNF-2026 bootstrap invitation code", _migration_v12_expire_bootstrap_invite),
        13: ("Store session tokens as sha256 and purge expired rows", _migration_v13_hash_session_tokens),
        14: ("Reconcile denormalised news/poll counters from their rows", _migration_v14_reconcile_counters),
        15: ("Index hot foreign-key child columns", _migration_v15_add_fk_indexes),
        16: ("Rebuild tables for ON DELETE actions and poll_votes user FK", _migration_v16_rebuild_fk_actions),
        17: ("Normalise legacy space-form timestamps to ISO-T", _migration_v17_normalize_timestamps),
        18: ("Dedupe schedule_lessons and add its natural-key indexes", _migration_v18_schedule_lessons_indexes),
        19: ("Index scraper_runs.started_at and prune ancient runs", _migration_v19_scraper_runs_index),
        20: ("Add messages_fts full-text shadow table with sync triggers", _migration_v20_messages_fts),
        21: ("Unique pending (from,to) pair on friend_requests", _migration_v21_friend_requests_pending_unique),
        22: ("Drop indexes duplicating implicit PK/UNIQUE indexes", _migration_v22_drop_duplicate_indexes),
        23: ("Delete orphaned message_reads rows", _migration_v23_delete_orphan_message_reads),
        25: ("Add deleted_source_urls tombstones for scraper dedupe", _migration_v25_deleted_source_urls),
        26: ("Dedupe polls per post and index the poll foreign keys", _migration_v26_poll_indexes),
        35: ("Canonicalise news_posts.source_url and drop the duplicates it exposes", _migration_v35_canonical_source_urls),
        36: ("Index scraper_runs(source, started_at) and close interrupted runs", _migration_v36_scraper_runs_source_index),
        40: ("Add admin_audit table for the privileged-action trail", _migration_v40_admin_audit),
        43: ("Add uploads table for file ownership, quota and orphan GC", _migration_v43_add_uploads_table),
        46: ("Index push_tokens(active, user_id) for the push fan-out", _migration_v46_push_tokens_active_index),
        47: ("Delete push_tokens rows that are not valid Expo tokens", _migration_v47_purge_invalid_push_tokens),
        49: ("Demote planted multi-member 'direct' conversations to 'group'", _migration_v49_direct_room_audit),
        55: ("Composite author/source/date index on news_posts", _migration_v55_news_posts_author_index),
        56: ("Add user_blocks + reports tables and users.chat_push_preview", _migration_v56_blocks_reports_preview),
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
        try:
            fn(conn)
        except Exception as e:
            # One self-describing line for the restart loop —
            # the version row is NOT written, so the next boot
            # retries this exact migration
            logger.critical("Data migration v%d (%s) FAILED on %s: %s", version, desc, _db_path, e)
            raise
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
# Migration v1: RETIRED to a recorded no-op. It used to
# HTML-escape every user-generated text column, truncate
# oversized titles/content and NULL non-http(s) avatar_urls
# — including the app's own /api/uploads avatars. That pass
# was superseded by v2 (output-escaping made stored escapes
# wrong) and has long been applied in production, but the
# body was DESTRUCTIVE on any replay: a rebuilt _migrations
# table would truncate content and wipe avatars again. The
# function and its dict entry stay so the version chain and
# numbering never shift; the body deliberately does nothing.
#
# Used by:
#   - _run_migrations (above) — via the _MIGRATIONS dict
############################################################

def _migration_v1_xss_cleanup(conn):
    # STEP 1: nothing — only the version row is recorded (by
    # _run_migrations), keeping the chain intact
    # ======================================================
    logger.info("  v1 is retired — recorded as a no-op")








############################################################
# _migration_v2_unescape_double_escapes
############################################################
#
# Migration v2: RETIRED to a recorded no-op. It used to
# html.unescape() every text column repeatedly until stable,
# reversing v1's escaping plus the layers the old
# before_request middleware piled on. That historical
# cleanup is complete and applied in production — but the
# until-stable loop is DESTRUCTIVE on any replay: on a
# database with a rebuilt _migrations table it would now
# destroy text where a user genuinely typed "&amp;". The
# function and its dict entry stay so the version chain and
# numbering never shift; the body deliberately does nothing.
#
# Used by:
#   - _run_migrations (above) — via the _MIGRATIONS dict
############################################################

def _migration_v2_unescape_double_escapes(conn):
    # STEP 1: nothing — only the version row is recorded (by
    # _run_migrations), keeping the chain intact
    # ======================================================
    logger.info("  v2 is retired — recorded as a no-op")








############################################################
# _migration_v3_add_invited_column
############################################################
#
# Migration v3: users.invited INTEGER NOT NULL DEFAULT 1 —
# 1 when the account was registered with an invitation code
# (higher trust), 0 for guest sign-ups. The column is now
# ALSO in _SCHEMA — with DEFAULT 0, because a fresh row is
# a guest until registration says otherwise — while this
# ALTER keeps DEFAULT 1 on purpose: every user that existed
# before v3 came through an invitation code. On a fresh DB
# the ALTER hits "duplicate column" and is skipped; any
# OTHER OperationalError (locked/full/readonly) re-raises
# so startup aborts and retries next boot.
#
# Used by:
#   - _run_migrations (above) — via the _MIGRATIONS dict
############################################################

def _migration_v3_add_invited_column(conn):
    try:
        conn.execute("ALTER TABLE users ADD COLUMN invited INTEGER NOT NULL DEFAULT 1")
        logger.info("  Added 'invited' column to users table (existing users marked invited=1)")
    except sqlite3.OperationalError as e:
        # Only the "duplicate column" skip is legitimate — a
        # locked/full/readonly database must abort startup
        if "duplicate column" not in str(e).lower():
            raise
        logger.warning("  'invited' column already exists, skipping")
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
        except sqlite3.OperationalError as e:
            # Only the "duplicate column" skip is legitimate — a
            # locked/full/readonly database must abort startup
            if "duplicate column" not in str(e).lower():
                raise
            logger.warning("  '%s' column already exists, skipping", col)
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
# ON DELETE CASCADE off users. Not here: the _migrations
# bookkeeping table (_run_migrations) and the
# migration-only indexes/FTS objects (v10, v15, v18–v21 —
# their columns/features may postdate this script on old
# files). users.invited carries DEFAULT 0 here vs v3's
# DEFAULT 1 — see the v3 banner. No index that merely
# duplicates an implicit PK/UNIQUE index (v22 dropped
# them). push_tokens / notification_channels sit between
# two index groups — appended by later features, harmless.
# Keep this string byte-identical unless the schema really
# changes.
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
    invited INTEGER NOT NULL DEFAULT 0,
    avatar_url TEXT,
    student_number TEXT,
    study_group TEXT,
    study_program TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    chat_push_preview INTEGER NOT NULL DEFAULT 1,
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
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    poll_id TEXT NOT NULL REFERENCES polls(id) ON DELETE CASCADE,
    option_id TEXT NOT NULL REFERENCES poll_options(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, poll_id)
);

CREATE TABLE IF NOT EXISTS deleted_source_urls (
    source_url TEXT PRIMARY KEY,
    deleted_by TEXT REFERENCES users(id) ON DELETE SET NULL,
    deleted_at TEXT NOT NULL DEFAULT (datetime('now'))
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
    client_msg_id TEXT,
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

CREATE TABLE IF NOT EXISTS user_blocks (
    blocker_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    blocked_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (blocker_id, blocked_id)
);

CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    reporter_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    target_type TEXT NOT NULL CHECK(target_type IN ('user', 'post', 'message')),
    target_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'resolved')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS faculty_info (
    id TEXT PRIMARY KEY,
    lang TEXT NOT NULL DEFAULT 'lt',
    section TEXT NOT NULL,
    data_json TEXT NOT NULL,
    scraped_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(lang, section)
);

CREATE INDEX IF NOT EXISTS idx_user_blocks_blocked ON user_blocks(blocked_id);
CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_posts_published ON news_posts(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_posts_source ON news_posts(source);
CREATE INDEX IF NOT EXISTS idx_news_comments_post ON news_comments(post_id);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_conversation_participants_user ON conversation_participants(user_id);
CREATE INDEX IF NOT EXISTS idx_message_reads_user ON message_reads(user_id);
CREATE INDEX IF NOT EXISTS idx_friendships_friend ON friendships(friend_id);
CREATE TABLE IF NOT EXISTS push_tokens (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT 'unknown',
    language TEXT NOT NULL DEFAULT 'lt',
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

CREATE TABLE IF NOT EXISTS uploads (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL UNIQUE,
    user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    byte_size INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_uploads_user ON uploads(user_id);

CREATE TABLE IF NOT EXISTS admin_audit (
    id TEXT PRIMARY KEY,
    actor_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    target TEXT,
    payload TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_admin_audit_created ON admin_audit(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_admin_audit_actor ON admin_audit(actor_id);
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
    except sqlite3.OperationalError as e:
        # The normal path on a fresh DB: _SCHEMA already
        # created the column, so the ALTER is a duplicate —
        # anything else (locked/full/readonly) must re-raise
        if "duplicate column" not in str(e).lower():
            raise
        logger.warning("  'active' column already exists, skipping")
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
        except sqlite3.OperationalError as e:
            # The normal path on a fresh DB: _SCHEMA already
            # created the column, so the ALTER is a duplicate —
            # anything else (locked/full/readonly) must re-raise
            if "duplicate column" not in str(e).lower():
                raise
            logger.warning("  column already exists, skipping: %s", statement)
    conn.commit()








############################################################
# _migration_v10_add_client_msg_id
############################################################
#
# Migration v10: messages.client_msg_id — the sender's
# optimistic-send nonce — plus the UNIQUE index on
# (conversation_id, sender_id, client_msg_id) that makes a
# chat send idempotent: a retried POST carrying a nonce
# already committed hits the index, and send_message
# (chat/routes.py) answers with the existing row instead
# of inserting a duplicate. NULL nonces (old rows, clients
# not sending one) never collide — SQLite treats each NULL
# in a unique index as distinct. The COLUMN is also in
# _SCHEMA's CREATE TABLE, so on a fresh DB the ALTER fails
# with "duplicate column" and is swallowed; the INDEX lives
# ONLY here — putting it in _SCHEMA would crash init_db on
# a pre-v10 DB, where the schema script runs before this
# migration adds the column. Defined after _SCHEMA like
# v8/v9 — the _MIGRATIONS dict resolves it at call time.
#
# Used by:
#   - _run_migrations (above) — via the _MIGRATIONS dict
############################################################

def _migration_v10_add_client_msg_id(conn):
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN client_msg_id TEXT")
        logger.info("  Added 'client_msg_id' column to messages table")
    except sqlite3.OperationalError as e:
        # The normal path on a fresh DB: _SCHEMA already
        # created the column, so the ALTER is a duplicate —
        # anything else (locked/full/readonly) must re-raise
        if "duplicate column" not in str(e).lower():
            raise
        logger.warning("  'client_msg_id' column already exists, skipping")

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_client_msg ON messages(conversation_id, sender_id, client_msg_id)"
    )
    conn.commit()








############################################################
# _migration_v11_add_push_language
############################################################
#
# Migration v11: push_tokens.language TEXT NOT NULL DEFAULT
# 'lt' — the app language the device registered with ('lt'
# or 'en'), written by POST /api/notifications/register and
# read by the push senders (notifications/push.py) so
# scraper notification copy arrives in the user's language.
# Existing rows default to 'lt', the app default; the device
# re-registers on every app start and on a language switch,
# so the column self-corrects. The column is also in
# _SCHEMA, so on a fresh DB the ALTER fails with "duplicate
# column" and is swallowed. Defined after _SCHEMA like
# v8–v10 — the _MIGRATIONS dict resolves it at call time.
#
# Used by:
#   - _run_migrations (above) — via the _MIGRATIONS dict
############################################################

def _migration_v11_add_push_language(conn):
    try:
        conn.execute("ALTER TABLE push_tokens ADD COLUMN language TEXT NOT NULL DEFAULT 'lt'")
        logger.info("  Added 'language' column to push_tokens table")
    except sqlite3.OperationalError as e:
        # The normal path on a fresh DB: _SCHEMA already
        # created the column, so the ALTER is a duplicate —
        # anything else (locked/full/readonly) must re-raise
        if "duplicate column" not in str(e).lower():
            raise
        logger.warning("  'language' column already exists, skipping")
    conn.commit()








############################################################
# _migration_v12_expire_bootstrap_invite
############################################################
#
# Migration v12: kill the hard-coded WELCOME-KNF-2026
# bootstrap invitation code on every deployed database. The
# literal shipped in git and was seeded with 100 uses and a
# year of validity, so anyone reading the repo could
# register; _seed_defaults now generates a random code, and
# this sets the legacy row's expires_at to NOW so it fails
# the register route's expiry check from this boot on.
# Idempotent: an already-expired row matches nothing.
#
# Used by:
#   - _run_migrations (above) — via the _MIGRATIONS dict
############################################################

def _migration_v12_expire_bootstrap_invite(conn):
    # STEP 1: expire, never delete — users.invited history
    # stays explainable by the surviving row
    # ====================================================
    now = utc_now_iso()
    expired = conn.execute(
        "UPDATE invitation_codes SET expires_at = ? WHERE code = 'WELCOME-KNF-2026' AND expires_at > ?",
        (now, now),
    ).rowcount
    if expired:
        logger.info("  Expired the legacy WELCOME-KNF-2026 invitation code")
    conn.commit()








############################################################
# _migration_v13_hash_session_tokens
############################################################
#
# Migration v13: sessions.token becomes sha256(raw) hex —
# a database leak then no longer hands out ready-to-use
# bearer tokens. The client keeps receiving (and sending)
# the RAW token; auth/routes.py and chat/events.py hash the
# presented value before the lookup, so the wire contract
# is untouched. Rows minted before this migration hold raw
# uuid4 (36 chars) and are rewritten in place — those
# sessions keep working because the lookup now hashes what
# the client presents. Expired rows are purged first.
# Idempotent: a 64-char lowercase-hex token is already a
# digest and is left alone.
#
# Used by:
#   - _run_migrations (above) — via the _MIGRATIONS dict
############################################################

def _migration_v13_hash_session_tokens(conn):
    # STEP 1: expired rows die unhashed — nothing can present
    # them again anyway
    # ======================================================
    purged = conn.execute("DELETE FROM sessions WHERE expires_at < ?", (utc_now_iso(),)).rowcount
    if purged:
        logger.info("  Purged %d expired session row(s)", purged)


    # STEP 2: rewrite every raw token to its sha256 hex; the
    # hex-shape test keeps a re-run from double-hashing
    # ======================================================
    rewritten = 0
    for row in conn.execute("SELECT id, token FROM sessions").fetchall():
        token = row[1]
        if len(token) == 64 and all(c in "0123456789abcdef" for c in token):
            continue
        conn.execute(
            "UPDATE sessions SET token = ? WHERE id = ?",
            (hashlib.sha256(token.encode()).hexdigest(), row[0]),
        )
        rewritten += 1
    if rewritten:
        logger.info("  Hashed %d live session token(s)", rewritten)
    conn.commit()








############################################################
# _migration_v14_reconcile_counters
############################################################
#
# Migration v14: one-off reconciliation of every
# denormalised counter from the rows that are the truth —
# news_posts.likes_count / comments_count from news_likes /
# news_comments, poll_options.votes and polls.total_votes
# from poll_votes. Years of non-transactional bump/decrement
# code left production rows drifted (deleted comments never
# decremented, double-bumps on races); the like toggle
# already recomputes from rows on every flip, and the news
# routes now use the same idiom for comments and polls, so
# this migration puts the stored numbers back on the truth
# once. Idempotent: recomputing twice lands on the same
# values.
#
# Used by:
#   - _run_migrations (above) — via the _MIGRATIONS dict
############################################################

def _migration_v14_reconcile_counters(conn):
    # STEP 1: post counters from their child tables
    # =============================================
    conn.execute("""
        UPDATE news_posts SET
            likes_count = (SELECT COUNT(*) FROM news_likes WHERE news_likes.post_id = news_posts.id),
            comments_count = (SELECT COUNT(*) FROM news_comments WHERE news_comments.post_id = news_posts.id)
    """)


    # STEP 2: poll counters from poll_votes
    # =====================================
    conn.execute(
        "UPDATE poll_options SET votes = (SELECT COUNT(*) FROM poll_votes WHERE poll_votes.option_id = poll_options.id)"
    )
    conn.execute(
        "UPDATE polls SET total_votes = (SELECT COUNT(*) FROM poll_votes WHERE poll_votes.poll_id = polls.id)"
    )
    conn.commit()
    logger.info("  Reconciled news like/comment counters and poll vote counters")








############################################################
# _migration_v15_add_fk_indexes
############################################################
#
# Migration v15: indexes on the hot foreign-key child
# columns that had none — without them every user/post/
# message delete (and every ON DELETE CASCADE/SET NULL walk)
# degrades to a full table scan of each child table. Lives
# ONLY here, not in _SCHEMA, per the v10 lesson: the schema
# script runs before migrations on old files. The three
# created_by/author_id indexes are re-created by v16 after
# its table rebuilds; IF NOT EXISTS keeps every re-run
# silent.
#
# Used by:
#   - _run_migrations (above) — via the _MIGRATIONS dict
############################################################

def _migration_v15_add_fk_indexes(conn):
    # STEP 1: the union of missing FK child indexes
    # =============================================
    for statement in (
        "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_news_likes_post ON news_likes(post_id)",
        "CREATE INDEX IF NOT EXISTS idx_news_comments_user ON news_comments(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender_id)",
        "CREATE INDEX IF NOT EXISTS idx_messages_reply_to ON messages(reply_to_id)",
        "CREATE INDEX IF NOT EXISTS idx_news_posts_author ON news_posts(author_id)",
        "CREATE INDEX IF NOT EXISTS idx_message_reactions_user ON message_reactions(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_conversations_created_by ON conversations(created_by)",
        "CREATE INDEX IF NOT EXISTS idx_invitation_codes_created_by ON invitation_codes(created_by)",
    ):
        conn.execute(statement)
    conn.commit()
    logger.info("  Created the missing foreign-key child indexes")








############################################################
# _migration_v16_rebuild_fk_actions
############################################################
#
# Migration v16: the classic SQLite table rebuild (new
# table → copy → drop → rename) for the FK declarations
# ALTER TABLE cannot change:
#   - invitation_codes.created_by, news_posts.author_id,
#     conversations.created_by → ON DELETE SET NULL (all
#     nullable, every reader tolerates NULL; without an
#     action a user delete is impossible on an FK-enforcing
#     connection)
#   - poll_votes.user_id → REFERENCES users(id) ON DELETE
#     CASCADE (it had NO foreign key at all, so votes would
#     outlive their voter)
# Each rebuild is skipped when PRAGMA foreign_key_list
# already shows the wanted action — on a fresh DB _SCHEMA
# now declares all four correctly, making this a no-op.
# foreign_keys is toggled OFF around the drop/rename dance
# (and back ON in finally); the PRAGMA only bites outside a
# transaction, hence the commit first. Indexes dropped with
# the old tables are re-created immediately.
#
# Used by:
#   - _run_migrations (above) — via the _MIGRATIONS dict
############################################################

def _migration_v16_rebuild_fk_actions(conn):
    # STEP 1: read the declared ON DELETE action of one FK —
    # foreign_key_list rows are (id, seq, table, from, to,
    # on_update, on_delete, match)
    # =====================================================
    def _on_delete(table, column):
        for fk in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall():
            if fk[3] == column:
                return fk[6]
        return None


    # STEP 2: rebuild specs — (table, column, wanted action,
    # CREATE for the replacement, column list, indexes to
    # re-create after the rename)
    # ======================================================
    rebuilds = (
        ("invitation_codes", "created_by", "SET NULL",
         """CREATE TABLE invitation_codes_new (
                id TEXT PRIMARY KEY,
                code TEXT UNIQUE NOT NULL,
                role TEXT NOT NULL DEFAULT 'student' CHECK(role IN ('student', 'teacher', 'admin', 'curator')),
                created_by TEXT REFERENCES users(id) ON DELETE SET NULL,
                max_uses INTEGER NOT NULL DEFAULT 1,
                use_count INTEGER NOT NULL DEFAULT 0,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )""",
         "id, code, role, created_by, max_uses, use_count, expires_at, created_at",
         ("CREATE INDEX IF NOT EXISTS idx_invitation_codes_created_by ON invitation_codes(created_by)",)),

        ("news_posts", "author_id", "SET NULL",
         """CREATE TABLE news_posts_new (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                summary TEXT,
                image_url TEXT,
                author_id TEXT REFERENCES users(id) ON DELETE SET NULL,
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
            )""",
         "id, title, content, summary, image_url, author_id, author_name, source, source_url, post_type, "
         "is_public, likes_count, comments_count, shares_count, published_at, created_at, updated_at",
         ("CREATE INDEX IF NOT EXISTS idx_news_posts_published ON news_posts(published_at DESC)",
          "CREATE INDEX IF NOT EXISTS idx_news_posts_source ON news_posts(source)",
          "CREATE INDEX IF NOT EXISTS idx_news_posts_author ON news_posts(author_id)")),

        ("conversations", "created_by", "SET NULL",
         """CREATE TABLE conversations_new (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL DEFAULT 'direct' CHECK(type IN ('direct', 'group')),
                title TEXT,
                avatar_emoji TEXT,
                created_by TEXT REFERENCES users(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )""",
         "id, type, title, avatar_emoji, created_by, created_at, updated_at",
         ("CREATE INDEX IF NOT EXISTS idx_conversations_created_by ON conversations(created_by)",)),

        ("poll_votes", "user_id", "CASCADE",
         """CREATE TABLE poll_votes_new (
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                poll_id TEXT NOT NULL REFERENCES polls(id) ON DELETE CASCADE,
                option_id TEXT NOT NULL REFERENCES poll_options(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (user_id, poll_id)
            )""",
         "user_id, poll_id, option_id, created_at",
         ()),
    )

    needed = [spec for spec in rebuilds if _on_delete(spec[0], spec[1]) != spec[2]]
    if not needed:
        logger.info("  All FK actions already correct — nothing to rebuild")
        return


    # STEP 3: the drop/rename dance with FK enforcement off;
    # a leftover *_new from an aborted earlier run is cleared
    # first so the retry can succeed
    # ======================================================
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        for table, column, wanted, create_sql, cols, post_statements in needed:
            conn.execute(f"DROP TABLE IF EXISTS {table}_new")
            conn.execute(create_sql)
            conn.execute(f"INSERT INTO {table}_new ({cols}) SELECT {cols} FROM {table}")
            conn.execute(f"DROP TABLE {table}")
            conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
            for statement in post_statements:
                conn.execute(statement)
            logger.info("  Rebuilt %s — %s now ON DELETE %s", table, column, wanted)
        conn.commit()
    finally:
        conn.execute("PRAGMA foreign_keys=ON")








############################################################
# _migration_v17_normalize_timestamps
############################################################
#
# Migration v17: one text shape for timestamps. Python-side
# writes stamp isoformat ("2026-08-29T12:00:00+00:00") while
# firing SQL DEFAULTs wrote datetime('now') space-form
# ("2026-08-29 12:00:00") into the SAME columns — and under
# SQLite's string comparison every space-form value sorts
# before every T-form value of the same day, breaking ORDER
# BY and expiry checks. Every space-form row becomes T-form
# here (' ' → 'T' — the GLOB only matches the legacy shape,
# so a re-run touches nothing); routes now always stamp
# explicitly via utc_now_iso(), leaving the DEFAULTs as
# never-firing backstops.
#
# Used by:
#   - _run_migrations (above) — via the _MIGRATIONS dict
############################################################

def _migration_v17_normalize_timestamps(conn):
    # STEP 1: every (table, column) that carries timestamp
    # text — including nullable ones; NULLs fail the GLOB
    # ====================================================
    columns = (
        ("users", "created_at"), ("users", "updated_at"),
        ("invitation_codes", "expires_at"), ("invitation_codes", "created_at"),
        ("sessions", "created_at"), ("sessions", "expires_at"),
        ("news_posts", "published_at"), ("news_posts", "created_at"), ("news_posts", "updated_at"),
        ("news_likes", "created_at"),
        ("news_comments", "created_at"),
        ("polls", "end_date"), ("polls", "created_at"),
        ("poll_votes", "created_at"),
        ("schedule_lessons", "created_at"),
        ("scraper_runs", "started_at"), ("scraper_runs", "finished_at"),
        ("conversations", "created_at"), ("conversations", "updated_at"),
        ("conversation_participants", "last_read_at"), ("conversation_participants", "joined_at"),
        ("messages", "created_at"), ("messages", "deleted_at"),
        ("message_reactions", "created_at"),
        ("message_reads", "read_at"),
        ("friendships", "created_at"),
        ("friend_requests", "created_at"), ("friend_requests", "updated_at"),
        ("faculty_info", "scraped_at"),
        ("push_tokens", "created_at"), ("push_tokens", "updated_at"),
        ("notification_channels", "updated_at"),
    )


    # STEP 2: space → T, only on values in the legacy shape;
    # the f-string names come from the literal tuple above,
    # never from input
    # =====================================================
    total = 0
    for table, column in columns:
        total += conn.execute(
            f"UPDATE {table} SET {column} = REPLACE({column}, ' ', 'T') "
            f"WHERE {column} GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] *'"
        ).rowcount
    conn.commit()
    logger.info("  Normalised %d space-form timestamp value(s) to ISO-T", total)








############################################################
# _migration_v18_schedule_lessons_indexes
############################################################
#
# Migration v18: schedule_lessons gets the indexes its only
# writer and readers always needed — the scraper de-duped
# with a full-row SELECT per candidate lesson (a full table
# scan each), and the day view filters on (semester,
# group_name, day_of_week) with nothing to stand on.
# Existing duplicates are removed first (keeping the oldest
# row of each natural-key group) so the UNIQUE index can
# build; the scraper now INSERT OR IGNOREs against it.
# Migration-only per the v10 lesson — never in _SCHEMA.
#
# Used by:
#   - _run_migrations (above) — via the _MIGRATIONS dict
############################################################

def _migration_v18_schedule_lessons_indexes(conn):
    # STEP 1: dedupe on the natural key, oldest row wins
    # ==================================================
    removed = conn.execute("""
        DELETE FROM schedule_lessons WHERE rowid NOT IN (
            SELECT MIN(rowid) FROM schedule_lessons
            GROUP BY semester, group_name, day_of_week, time_start, time_end, title, teacher, room
        )
    """).rowcount
    if removed:
        logger.info("  Removed %d duplicate schedule_lessons row(s)", removed)


    # STEP 2: the natural-key UNIQUE index plus the filter
    # index the schedule views read through
    # ====================================================
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_schedule_lessons_natural "
        "ON schedule_lessons(semester, group_name, day_of_week, time_start, time_end, title, teacher, room)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_schedule_lessons_filter "
        "ON schedule_lessons(semester, group_name, day_of_week)"
    )
    conn.commit()








############################################################
# _migration_v19_scraper_runs_index
############################################################
#
# Migration v19: scraper_runs grew without bound (a row per
# scheduled run, forever) and the status endpoint's ORDER BY
# started_at DESC had no index. One index — started_at
# leading, because the status query has no source filter —
# plus a one-off prune of runs older than 30 days; the
# scrapers repeat the prune at the end of every scheduled
# run. The cutoff is computed in Python so it compares
# correctly against v17's T-form timestamps.
#
# Used by:
#   - _run_migrations (above) — via the _MIGRATIONS dict
############################################################

def _migration_v19_scraper_runs_index(conn):
    # STEP 1: the status query's index
    # ===============================
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scraper_runs_started ON scraper_runs(started_at DESC)")


    # STEP 2: one-off prune of ancient runs
    # =====================================
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    pruned = conn.execute("DELETE FROM scraper_runs WHERE started_at < ?", (cutoff,)).rowcount
    if pruned:
        logger.info("  Pruned %d scraper_runs row(s) older than 30 days", pruned)
    conn.commit()








############################################################
# _migration_v20_messages_fts
############################################################
#
# Migration v20: messages_fts — an FTS5 shadow table over
# messages.text (external content, keyed on messages.rowid)
# kept in sync by AFTER INSERT / DELETE / UPDATE OF text
# triggers, then backfilled with the 'rebuild' command. The
# in-room search joins it by rowid and keeps filtering
# deleted_at on messages itself. Created HERE and not in
# _SCHEMA on purpose (beyond the v10 lesson): a SQLite
# built without FTS5 must degrade to the LIKE fallback, not
# crash the boot — the probe CREATE below eats exactly that
# case and leaves the search on LIKE.
#
# Used by:
#   - _run_migrations (above) — via the _MIGRATIONS dict
############################################################

def _migration_v20_messages_fts(conn):
    # STEP 1: probe-create — an FTS5-less build logs and
    # keeps the LIKE search, everything else proceeds
    # ==================================================
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts "
            "USING fts5(text, content='messages', content_rowid='rowid')"
        )
    except sqlite3.OperationalError as e:
        logger.warning("  FTS5 unavailable (%s) — message search stays on its LIKE fallback", e)
        return


    # STEP 2: sync triggers — the external-content 'delete'
    # command needs the OLD text, hence the insert-style form
    # ======================================================
    conn.executescript("""
        CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
        END;
        CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
        END;
        CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE OF text ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
            INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
        END;
    """)


    # STEP 3: backfill — 'rebuild' repopulates the whole
    # index from the content table, so re-runs are safe
    # ==================================================
    conn.execute("INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')")
    conn.commit()
    logger.info("  Created messages_fts with sync triggers and backfilled it")








############################################################
# _migration_v21_friend_requests_pending_unique
############################################################
#
# Migration v21: at most ONE pending friend request per
# (from_user_id, to_user_id) — the send route's check-then-
# insert could race itself into duplicates, which then made
# accept fire twice. A partial UNIQUE index closes the race
# at the database (the route catches the IntegrityError
# into its existing 409); accepted/rejected history rows
# stay unconstrained. Existing pending duplicates are
# collapsed to the oldest first so the index can build.
#
# Used by:
#   - _run_migrations (above) — via the _MIGRATIONS dict
############################################################

def _migration_v21_friend_requests_pending_unique(conn):
    # STEP 1: collapse existing pending duplicates, oldest
    # row wins
    # ====================================================
    removed = conn.execute("""
        DELETE FROM friend_requests WHERE status = 'pending' AND rowid NOT IN (
            SELECT MIN(rowid) FROM friend_requests WHERE status = 'pending'
            GROUP BY from_user_id, to_user_id
        )
    """).rowcount
    if removed:
        logger.info("  Removed %d duplicate pending friend request(s)", removed)


    # STEP 2: the partial unique index
    # ================================
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_friend_requests_pending "
        "ON friend_requests(from_user_id, to_user_id) WHERE status = 'pending'"
    )
    conn.commit()








############################################################
# _migration_v22_drop_duplicate_indexes
############################################################
#
# Migration v22: drop the six indexes that merely duplicate
# an index SQLite already maintains implicitly — the UNIQUE
# column indexes on sessions.token and invitation_codes.code
# and the left-prefix of four composite PRIMARY KEYs. Each
# was pure write amplification: every INSERT paid for an
# index no query plan would prefer. The CREATE lines are
# gone from _SCHEMA in the same change, so nothing
# resurrects them on the next boot.
#
# Used by:
#   - _run_migrations (above) — via the _MIGRATIONS dict
############################################################

def _migration_v22_drop_duplicate_indexes(conn):
    # STEP 1: DROP IF EXISTS each — fresh DBs never had them
    # ======================================================
    for name in (
        "idx_sessions_token",
        "idx_invitation_codes_code",
        "idx_message_reactions_message",
        "idx_message_reads_message",
        "idx_friendships_user",
        "idx_notification_channels_user",
    ):
        conn.execute(f"DROP INDEX IF EXISTS {name}")
    conn.commit()
    logger.info("  Dropped the six duplicate indexes")








############################################################
# _migration_v23_delete_orphan_message_reads
############################################################
#
# Migration v23: delete message_reads rows whose message or
# user no longer resolves. They were minted while init_db
# (and some early code paths) ran with foreign_keys OFF, so
# the CASCADEs that should have removed them never fired;
# production is known to hold at least one such orphan.
# init_db now runs PRAGMA foreign_key_check after every
# migration pass, so a regression shows up in the boot log
# instead of silently accumulating. Idempotent: orphans can
# only be deleted once.
#
# NOT EXISTS, never NOT IN: `x NOT IN (subquery)` evaluates
# to NULL — never true — the moment the subquery yields one
# NULL, so a single NULL messages.id (TEXT PRIMARY KEY does
# not imply NOT NULL, and this migration exists precisely to
# clean up rows written while foreign_keys was OFF) would
# silently disable the whole sweep.
#
# Used by:
#   - _run_migrations (above) — via the _MIGRATIONS dict
############################################################

def _migration_v23_delete_orphan_message_reads(conn):
    # STEP 1: both legs of the composite key must resolve
    # ===================================================
    removed = conn.execute("""
        DELETE FROM message_reads
        WHERE NOT EXISTS (SELECT 1 FROM messages WHERE messages.id = message_reads.message_id)
           OR NOT EXISTS (SELECT 1 FROM users WHERE users.id = message_reads.user_id)
    """).rowcount
    if removed:
        logger.info("  Deleted %d orphaned message_reads row(s)", removed)
    conn.commit()








############################################################
# _migration_v25_deleted_source_urls
############################################################
#
# Migration v25: the deleted_source_urls tombstone table.
# Both scrapers dedupe on source_url against news_posts
# ALONE, so deleting a scraped article achieved nothing —
# the next tick (20 minutes at most) re-inserted it, and an
# admin removing an article had to keep removing it. The
# tombstone survives the post row: news/routes.py delete_post
# writes the URL here, and the scrapers' dedupe checks skip
# any URL present. deleted_by is nullable and ON DELETE SET
# NULL, so removing the admin who deleted an article does not
# resurrect it.
#
# A brand-new TABLE, so it lives in BOTH _SCHEMA and here
# (each guarded IF NOT EXISTS) — a fresh DB gets it from the
# schema script, an existing file from this migration.
#
# Used by:
#   - _run_migrations (above) — via the _MIGRATIONS dict
############################################################

def _migration_v25_deleted_source_urls(conn):
    # STEP 1: the table, identical to the _SCHEMA copy
    # ===============================================
    conn.execute("""
        CREATE TABLE IF NOT EXISTS deleted_source_urls (
            source_url TEXT PRIMARY KEY,
            deleted_by TEXT REFERENCES users(id) ON DELETE SET NULL,
            deleted_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    logger.info("  deleted_source_urls tombstone table ready")








############################################################
# _migration_v26_poll_indexes
############################################################
#
# Migration v26: one poll per post, enforced by the database.
# create_poll was check-then-act, so two concurrent calls
# could hang two polls on one post — and polls(post_id) had
# no index at all, which made every poll read and every post
# delete scan the whole table (poll_options(poll_id) was in
# the same state).
#
# Duplicates are removed FIRST, keeping the lowest rowid per
# post_id — the poll that was created first, whose votes are
# the ones people cast — and the orphan sweeps afterwards
# cover rows that outlived their parent while foreign_keys
# was off. Only then can the unique index be built. Both
# indexes live ONLY here, never in _SCHEMA: that script runs
# BEFORE migrations on an old file, and a unique index over
# not-yet-deduped rows would crash-loop the container (the
# lesson written into the v10 banner). Idempotent — a re-run
# finds nothing to dedupe and both indexes already present.
#
# Used by:
#   - _run_migrations (above) — via the _MIGRATIONS dict
############################################################

def _migration_v26_poll_indexes(conn):
    # STEP 1: keep the OLDEST poll per post; the FK cascades
    # take its options and votes with the rest
    # ======================================================
    duplicates = conn.execute("""
        DELETE FROM polls
        WHERE rowid NOT IN (SELECT MIN(rowid) FROM polls GROUP BY post_id)
    """).rowcount
    if duplicates:
        logger.info("  Deleted %d duplicate poll(s) — one poll per post from now on", duplicates)


    # STEP 2: anything the cascade could not reach, because it
    # was written while foreign_keys was off
    # ========================================================
    conn.execute("DELETE FROM poll_votes WHERE poll_id NOT IN (SELECT id FROM polls)")
    conn.execute("DELETE FROM poll_options WHERE poll_id NOT IN (SELECT id FROM polls)")
    conn.execute("DELETE FROM poll_votes WHERE option_id NOT IN (SELECT id FROM poll_options)")


    # STEP 3: the constraint the route now relies on, plus the
    # missing foreign-key child index
    # ========================================================
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_polls_post ON polls(post_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_poll_options_poll ON poll_options(poll_id)")
    conn.commit()
    logger.info("  Indexed polls(post_id) UNIQUE and poll_options(poll_id)")








############################################################
# _migration_v35_canonical_source_urls
############################################################
#
# Migration v35: news_posts.source_url in the canonical shape
# the scrapers now write — https, no "www.", no fragment, no
# utm_*/fbclid tracking parameters, no trailing slash. The
# dedup key used to be the URL exactly as the listing page
# spelled it, so the same article behind two spellings (or
# reached through a redirect) was stored twice, and without
# this pass every vu.lt row stored as "https://www.vu.lt/…"
# would look unseen to the new key and be imported a second
# time.
#
# The oldest row of each canonical URL is kept and the later
# ones deleted; their likes and comments go with them
# through ON DELETE CASCADE. The whole scan is planned before
# a single write, because source_url is UNIQUE and the
# canonical spelling a kept row takes is exactly what one of
# its own duplicates may still be holding — rewriting inside
# the scan hit that constraint and crash-looped the boot.
# Idempotent: a second run finds every URL already canonical
# and nothing to delete.
# normalise_url is imported from the scraper package HERE
# rather than at module level so this file keeps no
# import-time dependency on it — and so the migration and
# the scrapers can never drift into two different keys.
#
# Used by:
#   - _run_migrations (above) — via the _MIGRATIONS dict
############################################################

def _migration_v35_canonical_source_urls(conn):
    # STEP 1: the scrapers' own canonicaliser
    # =======================================
    from app.scraper.common import normalise_url

    rows = conn.execute(
        "SELECT id, source_url FROM news_posts WHERE source_url IS NOT NULL ORDER BY rowid"
    ).fetchall()


    # STEP 2: plan first, write later. First sighting of each
    # canonical URL wins; the later rows are the duplicates the
    # old key let through
    # =======================================================
    kept = {}
    doomed = []

    for post_id, source_url in rows:
        canonical = normalise_url(source_url)

        if canonical in kept:
            doomed.append(post_id)
            continue

        kept[canonical] = (post_id, source_url)


    # STEP 3: the duplicates go BEFORE the rewrites — source_url
    # is UNIQUE, and the canonical spelling an older row is
    # about to take is exactly what a later duplicate may still
    # be holding
    # ==========================================================
    for post_id in doomed:
        conn.execute("DELETE FROM news_posts WHERE id = ?", (post_id,))

    updated = 0
    for canonical, (post_id, source_url) in kept.items():
        if canonical != source_url:
            conn.execute(
                "UPDATE news_posts SET source_url = ? WHERE id = ?",
                (canonical, post_id),
            )
            updated += 1

    removed = len(doomed)
    conn.commit()
    logger.info("  Canonicalised %d source_url value(s), removed %d duplicate article(s)",
                updated, removed)








############################################################
# _migration_v49_direct_room_audit
############################################################
#
# Migration v49: any 'direct' conversation holding MORE
# than two participants becomes a 'group'. The old
# create_conversation neither rejected a multi-member
# 'direct' nor counted members in its dedup query, so a
# planted 3-person "direct" room could be answered as two
# other people's DM; the route now enforces exactly two
# members (chat/routes.py) and the dedup only matches
# count-2 rooms — this is the one-time audit of the rows
# minted before that fix. One-member direct rooms (the
# counterpart left) are LEGITIMATE and keep their type.
# Demoted rooms get the fallback title "Grupė" when they
# have none, because a group with a NULL title is a state
# the mobile Messages-tab search cannot digest. Idempotent:
# a second run finds nothing to demote.
#
# Used by:
#   - _run_migrations (above) — via the _MIGRATIONS dict
############################################################

def _migration_v49_direct_room_audit(conn):
    # STEP 1: demote and title in one UPDATE — only rooms
    # with three or more members qualify
    # ===================================================
    demoted = conn.execute("""
        UPDATE conversations
        SET type = 'group',
            title = COALESCE(NULLIF(title, ''), 'Grupė')
        WHERE type = 'direct' AND id IN (
            SELECT conversation_id FROM conversation_participants
            GROUP BY conversation_id HAVING COUNT(*) > 2
        )
    """).rowcount
    if demoted:
        logger.info("  Demoted %d multi-member 'direct' conversation(s) to 'group'", demoted)
    conn.commit()








############################################################
# _migration_v46_push_tokens_active_index
############################################################
#
# Migration v46: the index the push fan-out has always
# wanted. notify_channel scans push_tokens for active = 1 and
# then checks each row's owner against notification_channels,
# so every broadcast walked the whole table; the composite
# (active, user_id) answers the filter and feeds the opt-out
# check its user id straight from the index, and
# notify_channel_users' user_id IN (...) uses it too.
#
# An index on an EXISTING table, so it lives ONLY here and
# never in _SCHEMA — the schema script runs before migrations
# on old files (the v10 banner tells that story).
# IF NOT EXISTS keeps a re-run silent.
#
# Used by:
#   - _run_migrations (above) — via the _MIGRATIONS dict
############################################################

def _migration_v46_push_tokens_active_index(conn):
    # STEP 1: filter column first, then the join column
    # =================================================
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_push_tokens_active "
        "ON push_tokens(active, user_id)"
    )
    conn.commit()
    logger.info("  Created the push_tokens(active, user_id) index")








############################################################
# _migration_v47_purge_invalid_push_tokens
############################################################
#
# Migration v47: delete every push_tokens row whose token is
# not a real Expo token. Intake used to check the
# "ExponentPushToken[" PREFIX only — no closing bracket, no
# character set — so control characters, markup and
# SQL-looking payloads could be stored and then quoted back
# into log lines by Expo's error responses. The route now
# validates the whole grammar (notifications/routes.py,
# _TOKEN_RE); this is the one-time cleanup of what got in
# before it.
#
# The test runs in PYTHON, with the same expression the route
# uses (notifications/routes.py, _TOKEN_RE) — SQLITE's GLOB
# cannot express "ten to sixty-four characters from this
# alphabet", and a looser pattern here would leave exactly
# the rows the route now refuses. The table holds one row per
# device, so reading it whole costs nothing. A deleted row
# costs its device nothing either: the app re-registers on
# every start.
#
# Used by:
#   - _run_migrations (above) — via the _MIGRATIONS dict
############################################################

def _migration_v47_purge_invalid_push_tokens(conn):
    # mid-function import: re is used nowhere else in this file
    import re

    # STEP 1: everything the intake grammar would refuse today
    # ========================================================
    # init_db's connection is a PLAIN sqlite3 connection — no
    # row_factory, so rows are tuples here and NOT sqlite3.Row;
    # unpack positionally, never by column name
    valid = re.compile(r"ExponentPushToken\[[A-Za-z0-9_-]{10,64}\]")
    doomed = [
        row_id
        for row_id, token in conn.execute("SELECT id, token FROM push_tokens").fetchall()
        if not isinstance(token, str) or not valid.fullmatch(token)
    ]


    # STEP 2: delete in chunks — well under SQLite's 999
    # variables per statement
    # =================================================
    for i in range(0, len(doomed), 400):
        part = doomed[i : i + 400]
        placeholders = ",".join("?" * len(part))
        conn.execute(f"DELETE FROM push_tokens WHERE id IN ({placeholders})", part)

    if doomed:
        logger.info("  Deleted %d malformed push_tokens row(s)", len(doomed))
    conn.commit()








############################################################
# _migration_v43_add_uploads_table
############################################################
#
# Migration v43: the uploads table — one row per stored
# file, naming its owner and its byte size. Until it landed
# an uploaded file had no owner record at all: no per-user
# quota was countable, no DELETE could be authorised, and
# nothing could tell an orphan from a live avatar. A brand
# new TABLE, so it lives in BOTH _SCHEMA and here (each
# guarded IF NOT EXISTS) — the columns of an EXISTING table
# would belong here only, per the v10 banner.
#
# user_id is ON DELETE SET NULL rather than CASCADE: when an
# account goes, its FILES must still be findable by the
# orphan sweep, and a row whose filename survived is exactly
# what lets the sweep delete them. Files written before this
# migration have no row — they are ownerless, admin-only for
# the DELETE route, and swept once nothing references them.
#
# Used by:
#   - _run_migrations (above) — via the _MIGRATIONS dict
############################################################

def _migration_v43_add_uploads_table(conn):
    # STEP 1: the table and its owner index, both idempotent
    # ======================================================
    conn.execute("""
        CREATE TABLE IF NOT EXISTS uploads (
            id TEXT PRIMARY KEY,
            filename TEXT NOT NULL UNIQUE,
            user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
            byte_size INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_uploads_user ON uploads(user_id)")
    conn.commit()








############################################################
# _migration_v55_news_posts_author_index
############################################################
#
# Migration v55: the index every profile path wanted.
# social/routes.py filters news_posts on author_id AND
# source IN ('user','faculty') and then sorts by
# published_at — three times per profile view (the post
# count, the page, the total) — while the only index on the
# column was v15's single-column idx_news_posts_author, so
# each of those queries still walked and re-sorted the
# author's whole history. The composite covers the filter
# and the ORDER BY together.
#
# An index on an EXISTING table, so it lives ONLY here and
# never in _SCHEMA — the schema script runs before
# migrations on old files (the v10 banner tells that story).
# IF NOT EXISTS keeps a re-run silent.
#
# Used by:
#   - _run_migrations (above) — via the _MIGRATIONS dict
############################################################

def _migration_v55_news_posts_author_index(conn):
    # STEP 1: filter columns first, then the sort column
    # ==================================================
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_news_posts_author_source "
        "ON news_posts(author_id, source, published_at DESC)"
    )
    conn.commit()
    logger.info("  Created the composite news_posts(author_id, source, published_at) index")








############################################################
# _migration_v40_admin_audit
############################################################
#
# Migration v40: admin_audit, the trail app/admin/routes.py
# had none of. Minting and revoking invitation codes,
# changing a role, deactivating an account and firing a
# broadcast all left the module silently — the only evidence
# a role grant ever happened was the granted role itself.
# Every mutating admin handler now writes one row here
# INSIDE its own transaction, so the trail cannot record an
# action that was rolled back, nor miss one that committed.
#
# actor_id is ON DELETE SET NULL (the v16 convention): a
# deleted admin must not take their history with them.
# payload is JSON text — whatever the action needs, e.g.
# {"from": "student", "to": "admin"}. Nothing reads the
# table from the API yet; it is read through DbGate.
#
# A brand-new TABLE, so it lives in BOTH _SCHEMA and this
# migration, each guarded by IF NOT EXISTS — unlike a column
# on an existing table, which may only ever appear here (the
# v10 banner tells that story).
#
# Used by:
#   - _run_migrations (above) — via the _MIGRATIONS dict
############################################################

def _migration_v40_admin_audit(conn):
    # STEP 1: the table, then the two indexes its readers
    # want — newest-first browsing and "everything actor X
    # ever did"
    # ==================================================
    conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_audit (
            id TEXT PRIMARY KEY,
            actor_id TEXT REFERENCES users(id) ON DELETE SET NULL,
            action TEXT NOT NULL,
            target TEXT,
            payload TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_admin_audit_created ON admin_audit(created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_admin_audit_actor ON admin_audit(actor_id)")
    conn.commit()
    logger.info("  admin_audit table ready")








############################################################
# _migration_v36_scraper_runs_source_index
############################################################
#
# Migration v36: GET /api/scraper/status stopped being "the
# last 20 rows, all sources mixed" — it now answers the
# question that matters, "when did EACH source last run,
# last succeed and last fail", which is three
# source-filtered lookups per source. v19's index leads with
# started_at alone, so every one of them scanned. This one
# leads with source.
#
# The same pass closes the rows a killed process left at
# 'running': nothing ever reconciled them, so a scrape
# interrupted months ago still showed as in flight.
# scraper/scheduler.py repeats the reconciliation at every
# start and once a day; this is the one-off for the rows
# already in the file.
#
# An index on an EXISTING table, so it lives ONLY here and
# never in _SCHEMA — the schema script runs before
# migrations on old files (the v10 banner tells that story).
#
# Used by:
#   - _run_migrations (above) — via the _MIGRATIONS dict
############################################################

def _migration_v36_scraper_runs_source_index(conn):
    # STEP 1: the per-source status queries' index
    # ============================================
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_scraper_runs_source_started "
        "ON scraper_runs(source, started_at DESC)"
    )


    # STEP 2: one-off reconciliation of the abandoned runs
    # ====================================================
    closed = conn.execute(
        """UPDATE scraper_runs
           SET status = 'failed', error_message = 'interrupted', finished_at = ?
           WHERE status = 'running'""",
        (datetime.now(timezone.utc).isoformat(),),
    ).rowcount
    conn.commit()
    if closed:
        logger.info("  Closed %d scraper run(s) left 'running' by an interrupted process", closed)








############################################################
# _migration_v56_blocks_reports_preview
############################################################
#
# Migration v56: the abuse-handling gap — nothing in the
# backend let a user refuse contact (any account could open
# a conversation with any other and push-notify abuse at
# will), nothing recorded a complaint, and every chat push
# shipped the message text to Expo with no way to opt out.
#
#   user_blocks — one row per (blocker, blocked) pair;
#     consulted by chat create_conversation / send_message /
#     the push fan-out / user search and by the friend
#     request route
#   reports — user-filed complaints (user/post/message),
#     listed and resolved in the admin panel
#   users.chat_push_preview — 1 ships the first 100 chars
#     to Expo as before, 0 sends the content-free body
#
# Tables and column are idempotent (IF NOT EXISTS / caught
# duplicate-column), the _SCHEMA pattern for new tables.
#
# Used by:
#   - _run_migrations (above) — via the _MIGRATIONS dict
############################################################

def _migration_v56_blocks_reports_preview(conn):
    # STEP 1: the block pairs and their reverse-lookup index
    # ======================================================
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_blocks (
            blocker_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            blocked_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (blocker_id, blocked_id)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_blocks_blocked ON user_blocks(blocked_id)"
    )


    # STEP 2: the reports ledger and the admin list's index
    # =====================================================
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id TEXT PRIMARY KEY,
            reporter_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            target_type TEXT NOT NULL CHECK(target_type IN ('user', 'post', 'message')),
            target_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'resolved')),
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status, created_at DESC)"
    )


    # STEP 3: the per-user push-preview flag, default on (the
    # behaviour every account had before the setting existed)
    # =======================================================
    try:
        conn.execute(
            "ALTER TABLE users ADD COLUMN chat_push_preview INTEGER NOT NULL DEFAULT 1"
        )
        logger.info("  Added 'chat_push_preview' column to users table")
    except sqlite3.OperationalError as e:
        # The normal path on a fresh DB: _SCHEMA already
        # created the column, so the ALTER is a duplicate —
        # anything else (locked/full/readonly) must re-raise
        if "duplicate column" not in str(e).lower():
            raise
        logger.warning("  'chat_push_preview' column already exists, skipping")
    conn.commit()
    logger.info("  user_blocks + reports tables ready")
