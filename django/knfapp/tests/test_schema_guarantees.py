############################################################
#  [*] Regression tests — the schema's own guarantees
#
#  The database facts the routes silently lean on: the
#  query-path indexes exist (the push fan-out's, the feed's,
#  the badge counts', the queues' — a dropped index breaks
#  no test and slows every user), the disappearing-messages
#  sweep index is PARTIAL (indexing every ordinary message
#  would tax each send for nothing), and the connection
#  enforces foreign keys (the erasure and unsend paths
#  depend on that discipline).
############################################################


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
        with connection.cursor() as cursor:
            cursor.execute(f'PRAGMA index_list("{table}")')
            return {row[1]: {"unique": bool(row[2]), "partial": bool(row[4])}
                    for row in cursor.fetchall()}

    def test_the_query_path_indexes_exist(self):
        for table, expected in EXPECTED_INDEXES.items():
            have = self._indexes(table)
            missing = expected - set(have)
            self.assertFalse(missing, f"{table} is missing {missing}")

    def _unique_columns(self, table):
        found = set()
        with connection.cursor() as cursor:
            cursor.execute(f'PRAGMA index_list("{table}")')
            for _seq, name, unique, _origin, _partial in cursor.fetchall():
                if not unique:
                    continue
                cursor.execute(f'PRAGMA index_info("{name}")')
                found.add(tuple(row[2] for row in cursor.fetchall()))
        return found

    def test_the_expiry_index_is_partial_and_the_unique_keys_hold(self):
        messages = self._indexes("messages")
        self.assertTrue(messages["idx_messages_expires"]["partial"])
        for table, columns in EXPECTED_UNIQUES.items():
            self.assertIn(columns, self._unique_columns(table),
                          f"{table} lost its unique key over {columns}")

    def test_foreign_keys_are_enforced_on_this_connection(self):
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA foreign_keys")
            self.assertEqual(cursor.fetchone()[0], 1)
