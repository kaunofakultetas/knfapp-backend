############################################################
#  [*] Regression tests — the schema's own guarantees
#
#  The database facts the routes silently lean on: the
#  query-path indexes exist (the push fan-out's, the feed's,
#  the badge counts', the queues' — a dropped index breaks
#  no test and slows every user), the disappearing-messages
#  sweep index is PARTIAL (indexing every ordinary message
#  would tax each send for nothing), the connection
#  enforces foreign keys (the erasure and unsend paths
#  depend on that discipline), and models.py and the
#  hand-kept migrations agree — runTests.sh runs
#  `makemigrations --check` before the suite, but the
#  everyday `docker exec … manage.py test` loop skips that
#  script, so the same check runs HERE as a test.
#
#  Runs on both engines (TEST_DATABASE_URL, see settings):
#  the index/unique facts come from Django's introspection
#  where it answers, and from the engine's own catalog for
#  the one thing it does not expose — whether an index is
#  partial.
############################################################


from django.core.management import call_command
from django.db import connection
from django.test import TestCase


EXPECTED_INDEXES = {
    "push_tokens": {"idx_push_tokens_active"},
    "news_posts": {"idx_news_posts_published", "idx_news_posts_source"},
    "activity": {"idx_activity_unread", "idx_activity_user", "activity_unique_null_subject"},
    "reports": {"idx_reports_status"},
    "memes": {"idx_memes_created"},
    "messages": {"idx_messages_expires", "idx_messages_conversation"},
    "schedule_events": {"idx_schedule_events_date", "idx_schedule_events_sem"},
    "schedule_groups": {"idx_schedule_groups_name"},
    "uploads": {"idx_uploads_user", "idx_uploads_created"},
    "users": {"users_username_ci", "users_email_ci"},
}

# Uniques declared as table CONSTRAINTs are backed by
# autoindexes — pinned by their COLUMNS, not a name
EXPECTED_UNIQUES = {
    "messages": ("conversation_id", "sender_id", "client_msg_id"),
    "schedule_events": ("date", "time_start", "time_end", "title",
                        "lecture_type", "room"),
}


class SchemaGuaranteeTests(TestCase):

    def _indexes(self, table):
        # {name: {"unique", "partial"}} — vendor-split on purpose:
        # the partial flag is not in the introspection API, and
        # these two engines are the only ones this stack runs on
        with connection.cursor() as cursor:
            if connection.vendor == "sqlite":
                cursor.execute(f'PRAGMA index_list("{table}")')
                return {row[1]: {"unique": bool(row[2]), "partial": bool(row[4])}
                        for row in cursor.fetchall()}
            cursor.execute(
                "SELECT c.relname, i.indisunique, i.indpred IS NOT NULL "
                "FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid "
                "JOIN pg_class t ON t.oid = i.indrelid "
                "WHERE t.relname = %s", [table])
            return {row[0]: {"unique": bool(row[1]), "partial": bool(row[2])}
                    for row in cursor.fetchall()}

    def test_the_query_path_indexes_exist(self):
        for table, expected in EXPECTED_INDEXES.items():
            have = self._indexes(table)
            missing = expected - set(have)
            self.assertFalse(missing, f"{table} is missing {missing}")

    def _unique_columns(self, table):
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(cursor, table)
        return {tuple(c["columns"]) for c in constraints.values() if c["unique"]}

    def test_the_expiry_index_is_partial_and_the_unique_keys_hold(self):
        messages = self._indexes("messages")
        self.assertTrue(messages["idx_messages_expires"]["partial"])
        for table, columns in EXPECTED_UNIQUES.items():
            self.assertIn(columns, self._unique_columns(table),
                          f"{table} lost its unique key over {columns}")

    def test_foreign_keys_are_enforced_on_this_connection(self):
        # The relation is DECLARED on both engines; SQLite enforces
        # it only when the per-connection pragma is on (PostgreSQL
        # always does — deferred to commit)
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(cursor, "messages")
            declared = {tuple(c["columns"]): c["foreign_key"]
                        for c in constraints.values() if c["foreign_key"]}
            self.assertEqual(declared.get(("conversation_id",)), ("conversations", "id"))
            if connection.vendor == "sqlite":
                cursor.execute("PRAGMA foreign_keys")
                self.assertEqual(cursor.fetchone()[0], 1)

    def test_models_and_migrations_are_in_step(self):
        # makemigrations --check exits 1 (SystemExit) when a model
        # change has no migration yet; a clean tree returns quietly
        try:
            call_command("makemigrations", check=True, dry_run=True, verbosity=0)
        except SystemExit:
            self.fail("models.py and migrations have drifted — run makemigrations")
