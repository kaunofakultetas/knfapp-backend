"""Database initialization and helpers."""

import html
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt

_db_path = None
logger = logging.getLogger(__name__)

# Migration version — bump this to re-run migrations
_CURRENT_MIGRATION_VERSION = 2


def init_db(db_path):
    """Initialize the database with schema and seed data."""
    global _db_path
    _db_path = db_path

    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    conn.commit()

    # Seed default admin user if no users exist
    count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if count == 0:
        _seed_defaults(conn)

    # Run one-time data migrations (XSS cleanup, oversized data, etc.)
    _run_migrations(conn)

    conn.close()


def get_db():
    """Get a database connection."""
    conn = sqlite3.connect(_db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _seed_defaults(conn):
    """Create default admin user and initial invitation code."""
    admin_id = str(uuid.uuid4())
    pw_hash = bcrypt.hashpw(b"admin123", bcrypt.gensalt()).decode()

    conn.execute(
        "INSERT INTO users (id, username, email, display_name, password_hash, role) VALUES (?, ?, ?, ?, ?, ?)",
        (admin_id, "admin", "admin@knf.vu.lt", "Administratorius", pw_hash, "admin"),
    )

    # Create a reusable invitation code for testing
    invite_id = str(uuid.uuid4())
    code = "WELCOME-KNF-2026"
    expires = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
    conn.execute(
        "INSERT INTO invitation_codes (id, code, role, created_by, max_uses, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
        (invite_id, code, "student", admin_id, 100, expires),
    )

    # Seed schedule data
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


def _run_migrations(conn):
    """Run one-time data migrations, tracked by version number."""
    # Ensure migrations table exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()

    _MIGRATIONS = {
        1: ("XSS payload cleanup + oversized data trim", _migration_v1_xss_cleanup),
        2: ("Reverse double-escaped HTML entities (input-escaping removed)", _migration_v2_unescape_double_escapes),
    }

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


def _migration_v1_xss_cleanup(conn):
    """Migration v1: HTML-escape all user-generated text columns and
    clean up oversized records.

    Targets:
    - users.display_name
    - news_posts.title, content, summary, author_name
    - news_comments.text
    - messages.text
    - conversations.title
    - polls.title, poll_options.text

    Also truncates oversized data (e.g. post f162b474 with 100k-char title).
    """
    MAX_TITLE_LEN = 200
    MAX_CONTENT_LEN = 10000

    def _escape_column(table, column, id_column="id"):
        """HTML-escape all non-NULL values in a text column."""
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

    # ── Users: display_name ──
    _escape_column("users", "display_name")

    # ── News posts: title, content, summary, author_name ──
    _escape_column("news_posts", "title")
    _escape_column("news_posts", "content")
    _escape_column("news_posts", "summary")
    _escape_column("news_posts", "author_name")

    # Truncate oversized titles (e.g. post f162b474 with 100k chars)
    oversized = conn.execute(
        "SELECT id, title FROM news_posts WHERE LENGTH(title) > ?",
        (MAX_TITLE_LEN,),
    ).fetchall()
    for row in oversized:
        truncated = row[1][:MAX_TITLE_LEN]
        conn.execute("UPDATE news_posts SET title = ? WHERE id = ?", (truncated, row[0]))
        logger.info("  Truncated oversized title on post %s (was %d chars)", row[0], len(row[1]))

    # Truncate oversized content
    oversized_content = conn.execute(
        "SELECT id, content FROM news_posts WHERE LENGTH(content) > ?",
        (MAX_CONTENT_LEN,),
    ).fetchall()
    for row in oversized_content:
        truncated = row[1][:MAX_CONTENT_LEN]
        conn.execute("UPDATE news_posts SET content = ? WHERE id = ?", (truncated, row[0]))
        logger.info("  Truncated oversized content on post %s (was %d chars)", row[0], len(row[1]))

    # ── Comments: text ──
    _escape_column("news_comments", "text")

    # ── Chat messages: text ──
    _escape_column("messages", "text")

    # ── Conversations: title ──
    _escape_column("conversations", "title")

    # ── Polls: title ──
    _escape_column("polls", "title")

    # ── Poll options: text ──
    _escape_column("poll_options", "text")

    # ── Validate and clear bad avatar_url values ──
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


def _migration_v2_unescape_double_escapes(conn):
    """Migration v2: Reverse accumulated double-escaping from v1 + input-time
    html.escape().

    The old before_request middleware ran html.escape() on every write, so
    round-trip edits accumulated escape layers:
        & -> &amp; -> &amp;amp; -> &amp;amp;amp; ...

    Now that escaping happens on OUTPUT only, stored data must be raw text.
    This migration repeatedly unescapes until stable, restoring original text.

    Scraper content (source IN ('knf.vu.lt', 'vu.lt')) is also unescaped
    because the scraper already stores raw text and v1 escaped it.
    """

    def _unescape_column(table, column, id_column="id"):
        """html.unescape all non-NULL values in a text column until stable."""
        rows = conn.execute(
            f"SELECT {id_column}, {column} FROM {table} WHERE {column} IS NOT NULL"
        ).fetchall()
        updated = 0
        for row in rows:
            current = row[1]
            # Repeatedly unescape until the string stops changing
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

    # Users
    _unescape_column("users", "display_name")

    # News posts (all sources, including scraper)
    _unescape_column("news_posts", "title")
    _unescape_column("news_posts", "content")
    _unescape_column("news_posts", "summary")
    _unescape_column("news_posts", "author_name")

    # Comments
    _unescape_column("news_comments", "text")

    # Chat
    _unescape_column("messages", "text")
    _unescape_column("conversations", "title")

    # Polls
    _unescape_column("polls", "title")
    _unescape_column("poll_options", "text")

    conn.commit()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
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
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS message_reactions (
    message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    emoji TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
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

CREATE INDEX IF NOT EXISTS idx_news_posts_published ON news_posts(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_posts_source ON news_posts(source);
CREATE INDEX IF NOT EXISTS idx_news_comments_post ON news_comments(post_id);
CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(token);
CREATE INDEX IF NOT EXISTS idx_invitation_codes_code ON invitation_codes(code);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_conversation_participants_user ON conversation_participants(user_id);
CREATE INDEX IF NOT EXISTS idx_message_reactions_message ON message_reactions(message_id);
CREATE INDEX IF NOT EXISTS idx_friendships_user ON friendships(user_id);
CREATE INDEX IF NOT EXISTS idx_friendships_friend ON friendships(friend_id);
CREATE INDEX IF NOT EXISTS idx_friend_requests_to ON friend_requests(to_user_id, status);
CREATE INDEX IF NOT EXISTS idx_friend_requests_from ON friend_requests(from_user_id, status);
"""
