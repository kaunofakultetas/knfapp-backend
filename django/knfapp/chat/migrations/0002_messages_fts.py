############################################################
#  [*] chat 0002 — the messages_fts search shadow table
#
#  An FTS5 external-content table over messages.text, kept
#  in sync by AFTER INSERT / DELETE / UPDATE OF text
#  triggers and backfilled with the 'rebuild' command. The
#  in-room search joins it by rowid; a build without FTS5
#  logs and degrades the search to its LIKE fallback — the
#  probe CREATE eats exactly that case, so the migrate can
#  never fail over a missing extension.
#  Skipped entirely off SQLite — the postgres move replaces
#  this with its own search plan alongside the raw SQL
#  rewrites.
############################################################


import logging

from django.db import migrations


logger = logging.getLogger(__name__)


def _create_fts(apps, schema_editor):
    if schema_editor.connection.vendor != "sqlite":
        logger.warning("messages_fts is SQLite-only — search will use the LIKE fallback")
        return

    cursor = schema_editor.connection.cursor()
    try:
        cursor.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts "
            "USING fts5(text, content='messages', content_rowid='rowid')"
        )
    except Exception as e:
        logger.warning("FTS5 unavailable (%s) — message search stays on its LIKE fallback", e)
        return

    # The external-content 'delete' command needs the OLD text,
    # hence the insert-style form in the triggers
    cursor.executescript("""
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
    cursor.execute("INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')")


def _drop_fts(apps, schema_editor):
    if schema_editor.connection.vendor != "sqlite":
        return
    cursor = schema_editor.connection.cursor()
    for trigger in ("messages_fts_ai", "messages_fts_ad", "messages_fts_au"):
        cursor.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    cursor.execute("DROP TABLE IF EXISTS messages_fts")


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(_create_fts, _drop_fts),
    ]
