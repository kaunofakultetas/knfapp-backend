"""Database initialization and helpers."""

import logging
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt

_db_path = None
logger = logging.getLogger(__name__)


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
