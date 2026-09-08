############################################################
#  [*] copy_production_data — the cutover row copy
#
#  Copies every row of the production SQLite file into the
#  freshly migrated database this process points at, healing
#  the known data quirks on the way:
#
#    - dangling messages.reply_to_id references (quotes of
#      rows deleted under unenforced foreign keys) are
#      NULLed — the target enforces the constraint
#    - schedule_lessons teacher/room NULLs become '' and the
#      duplicates that NULL-distinctness allowed are dropped
#    - poll_options gains its position column, numbered in
#      the source's stored order per poll
#    - the denormalised counters (post likes/comments, poll
#      totals and option votes) are RECOMPUTED from child
#      rows instead of trusted
#    - the messages_fts search shadow is rebuilt after the
#      bulk insert
#
#  Discipline: a PRE-FLIGHT pass validates what a blind copy
#  would trip over (enum values outside the target's CHECK
#  constraints, NULLs in columns the target requires) and
#  ABORTS with a report — bad source data is fixed at the
#  source, never silently dropped. The copy itself runs in
#  ONE transaction: any failure leaves the target exactly as
#  it was. A POST-FLIGHT pass compares per-table row counts
#  (expected deltas from the healings called out) and runs
#  PRAGMA foreign_key_check.
#
#  Usage, against the fresh database:
#
#    python3 manage.py migrate --noinput
#    python3 manage.py copy_production_data --source /data/live.sqlite3
#
#  The target must be EMPTY (no users) unless
#  --allow-nonempty is passed. The source is opened
#  read-only; it is never written.
############################################################


import sqlite3

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from knfapp.chat.models import CONVERSATION_TYPES
from knfapp.news.models import POST_TYPES, SOURCES
from knfapp.scraper.models import RUN_STATUSES
from knfapp.social.models import ACTIVITY_KINDS, REPORT_STATUSES, REPORT_TARGET_TYPES, REQUEST_STATUSES
from knfapp.users.models import ROLES
from knfapp.wayfind.models import CAPTURE_MODES, CAPTURE_STATUSES, ENTITY_KINDS

# Parents strictly before children — the target checks every
# foreign key per statement
TABLE_ORDER = (
    "users",
    "invitation_codes",
    "sessions",
    "push_tokens",
    "notification_channels",
    "uploads",
    "news_posts",
    "news_likes",
    "news_comments",
    "polls",
    "poll_options",
    "poll_votes",
    "friendships",
    "friend_requests",
    "user_blocks",
    "reports",
    "activity",
    "deleted_source_urls",
    "conversations",
    "conversation_participants",
    "messages",
    "message_reads",
    "message_reactions",
    "schedule_lessons",
    "faculty_info",
    "memes",
    "admin_audit",
    "scraper_runs",
    "wf_buildings",
    "wf_entities",
    "wf_ops",
    "wf_versions",
    "wf_panoramas",
    "wf_plans",
    "wf_captures",
    "wf_capture_frames",
)

# (table, column, allowed values) — the target's CHECK
# constraints; a source value outside its set would abort the
# copy mid-table, so it aborts the pre-flight instead
ENUM_CHECKS = (
    ("users", "role", ROLES),
    ("news_posts", "source", SOURCES),
    ("news_posts", "post_type", POST_TYPES),
    ("conversations", "type", CONVERSATION_TYPES),
    ("friend_requests", "status", REQUEST_STATUSES),
    ("reports", "target_type", REPORT_TARGET_TYPES),
    ("reports", "status", REPORT_STATUSES),
    ("activity", "kind", ACTIVITY_KINDS),
    ("scraper_runs", "status", RUN_STATUSES),
    ("wf_entities", "kind", ENTITY_KINDS),
    ("wf_captures", "mode", CAPTURE_MODES),
    ("wf_captures", "status", CAPTURE_STATUSES),
    ("wf_ops", "status", ("applied", "rejected")),
)

BATCH = 1000

NATURAL_LESSON_KEY = ("semester", "group_name", "day_of_week", "time_start", "time_end",
                      "title", "teacher", "room")


class Command(BaseCommand):
    help = "Copy the production SQLite database into the fresh migrated one, healing known quirks"

    def add_arguments(self, parser):
        parser.add_argument("--source", required=True, help="path to the production SQLite file")
        parser.add_argument("--allow-nonempty", action="store_true",
                            help="copy even though the target already holds users")

    def handle(self, *args, **options):
        source = sqlite3.connect(f"file:{options['source']}?mode=ro", uri=True)
        source.row_factory = sqlite3.Row

        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM users")
            if cursor.fetchone()[0] and not options["allow_nonempty"]:
                raise CommandError("The target already holds users — pass --allow-nonempty to copy anyway")

        present = {row[0] for row in source.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
        tables = [t for t in TABLE_ORDER if t in present]
        skipped = [t for t in TABLE_ORDER if t not in present]
        for table in skipped:
            self.stdout.write(f"source has no {table} — skipped")

        self._preflight(source, tables)

        healed = {"reply_refs": 0, "lesson_dupes": 0, "lesson_nulls": 0}
        copied = {}
        with transaction.atomic():
            for table in tables:
                if table == "messages":
                    copied[table] = self._copy_messages(source, healed)
                elif table == "poll_options":
                    copied[table] = self._copy_poll_options(source)
                elif table == "schedule_lessons":
                    copied[table] = self._copy_schedule_lessons(source, healed)
                else:
                    copied[table] = self._copy_table(source, table)

            self._recompute_counters()
            self._rebuild_fts()

        self._postflight(source, tables, copied, healed)

    ############################################################
    # pre-flight — abort BEFORE the first write
    ############################################################

    def _preflight(self, source, tables):
        problems = []

        for table, column, allowed in ENUM_CHECKS:
            if table not in tables:
                continue
            marks = ",".join("?" * len(allowed))
            rows = source.execute(
                f"SELECT {column}, COUNT(*) FROM {table}"
                f" WHERE {column} NOT IN ({marks}) GROUP BY {column}",
                list(allowed),
            ).fetchall()
            for value, count in rows:
                problems.append(f"{table}.{column}: {count} row(s) hold {value!r} "
                                f"(outside the target's CHECK)")

        # NULLs where the target insists on a value
        for table in tables:
            target_cols = self._target_columns(table)
            source_cols = {r[1] for r in source.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, notnull in target_cols:
                if not notnull:
                    continue
                if name not in source_cols:
                    # the one column the copy itself fills
                    if not (table == "poll_options" and name == "position"):
                        problems.append(f"{table}.{name}: required by the target, absent in the source")
                    continue
                # The two healed columns may hold NULLs in the source
                if table == "schedule_lessons" and name in ("teacher", "room"):
                    continue
                count = source.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {name} IS NULL").fetchone()[0]
                if count:
                    problems.append(f"{table}.{name}: {count} NULL row(s), target requires a value")

        if problems:
            for p in problems:
                self.stderr.write("PRE-FLIGHT: " + p)
            raise CommandError(f"{len(problems)} pre-flight problem(s) — fix the source first")

        self.stdout.write("pre-flight clean")

    ############################################################
    # the copy primitives
    ############################################################

    def _target_columns(self, table):
        with connection.cursor() as cursor:
            cursor.execute(f"PRAGMA table_info({table})")
            return [(r[1], bool(r[3])) for r in cursor.fetchall()]

    def _shared_columns(self, source, table):
        source_cols = [r[1] for r in source.execute(f"PRAGMA table_info({table})").fetchall()]
        target_cols = {name for name, _ in self._target_columns(table)}
        return [c for c in source_cols if c in target_cols]

    def _insert_rows(self, table, columns, rows):
        if not rows:
            return 0
        marks = ",".join(["%s"] * len(columns))
        cols = ",".join(columns)
        with connection.cursor() as cursor:
            cursor.executemany(f"INSERT INTO {table} ({cols}) VALUES ({marks})", rows)
        return len(rows)

    def _copy_table(self, source, table, order_by=""):
        columns = self._shared_columns(source, table)
        total = 0
        cursor = source.execute(f"SELECT {','.join(columns)} FROM {table}{order_by}")
        while True:
            rows = cursor.fetchmany(BATCH)
            if not rows:
                break
            total += self._insert_rows(table, columns, [tuple(r) for r in rows])
        self.stdout.write(f"{table}: {total} row(s)")
        return total

    ############################################################
    # the healed tables
    ############################################################

    def _copy_messages(self, source, healed):
        # Two passes: rows land with reply_to_id NULL, then the
        # references whose target really exists are restored —
        # a quote of a vanished row stays NULL instead of
        # violating the enforced foreign key
        columns = self._shared_columns(source, "messages")
        reply_idx = columns.index("reply_to_id")
        id_idx = columns.index("id")
        total = 0
        refs = []
        cursor = source.execute(f"SELECT {','.join(columns)} FROM messages ORDER BY created_at, id")
        while True:
            rows = cursor.fetchmany(BATCH)
            if not rows:
                break
            batch = []
            for row in rows:
                values = list(row)
                if values[reply_idx] is not None:
                    refs.append((values[id_idx], values[reply_idx]))
                    values[reply_idx] = None
                batch.append(tuple(values))
            total += self._insert_rows("messages", columns, batch)

        restored = 0
        with connection.cursor() as cursor:
            for msg_id, reply_to in refs:
                cursor.execute(
                    "UPDATE messages SET reply_to_id = %s"
                    " WHERE id = %s AND EXISTS (SELECT 1 FROM messages WHERE id = %s)",
                    (reply_to, msg_id, reply_to),
                )
                restored += cursor.rowcount
        healed["reply_refs"] = len(refs) - restored
        self.stdout.write(f"messages: {total} row(s), {healed['reply_refs']} dangling quote ref(s) NULLed")
        return total

    def _copy_poll_options(self, source):
        # The target's position column numbers the source's
        # stored order (rowid) per poll
        columns = self._shared_columns(source, "poll_options") + ["position"]
        rows = source.execute(
            f"SELECT {','.join(columns[:-1])}, poll_id AS _p FROM poll_options ORDER BY poll_id, rowid"
        ).fetchall()
        batch = []
        position = 0
        last_poll = None
        for row in rows:
            values = list(row)[:-1]
            poll_id = row["_p"]
            position = position + 1 if poll_id == last_poll else 0
            last_poll = poll_id
            batch.append((*values, position))
        total = self._insert_rows("poll_options", columns, batch)
        self.stdout.write(f"poll_options: {total} row(s), position backfilled")
        return total

    def _copy_schedule_lessons(self, source, healed):
        # NULL teacher/room becomes '' (the natural key counts
        # NULLs as distinct, so those rows could duplicate), and
        # exact duplicates under the normalised key are dropped
        columns = self._shared_columns(source, "schedule_lessons")
        key_idx = [columns.index(k) for k in NATURAL_LESSON_KEY]
        fix_idx = [columns.index(k) for k in ("teacher", "room")]
        seen = set()
        batch = []
        total = 0
        for row in source.execute(f"SELECT {','.join(columns)} FROM schedule_lessons ORDER BY rowid"):
            values = list(row)
            for i in fix_idx:
                if values[i] is None:
                    values[i] = ""
                    healed["lesson_nulls"] += 1
            key = tuple(values[i] for i in key_idx)
            if key in seen:
                healed["lesson_dupes"] += 1
                continue
            seen.add(key)
            batch.append(tuple(values))
        total = self._insert_rows("schedule_lessons", columns, batch)
        self.stdout.write(f"schedule_lessons: {total} row(s), "
                          f"{healed['lesson_nulls']} NULL(s) normalised, "
                          f"{healed['lesson_dupes']} duplicate(s) dropped")
        return total

    ############################################################
    # healing that runs on the copied target
    ############################################################

    def _recompute_counters(self):
        # The denormalised counters are re-derived from child
        # rows — copied drift heals here (shares_count has no
        # child table and is copied as counted)
        with connection.cursor() as cursor:
            cursor.execute("""
                UPDATE news_posts SET likes_count =
                    (SELECT COUNT(*) FROM news_likes WHERE news_likes.post_id = news_posts.id)
            """)
            cursor.execute("""
                UPDATE news_posts SET comments_count =
                    (SELECT COUNT(*) FROM news_comments WHERE news_comments.post_id = news_posts.id)
            """)
            cursor.execute("""
                UPDATE poll_options SET votes =
                    (SELECT COUNT(*) FROM poll_votes WHERE poll_votes.option_id = poll_options.id)
            """)
            cursor.execute("""
                UPDATE polls SET total_votes =
                    (SELECT COUNT(*) FROM poll_votes WHERE poll_votes.poll_id = polls.id)
            """)
        self.stdout.write("counters recomputed from child rows")

    def _rebuild_fts(self):
        with connection.cursor() as cursor:
            try:
                cursor.execute("INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')")
                self.stdout.write("messages_fts rebuilt")
            except Exception:
                self.stdout.write("messages_fts absent — search stays on its LIKE fallback")

    ############################################################
    # post-flight — the copy proves itself
    ############################################################

    def _postflight(self, source, tables, copied, healed):
        mismatches = []
        for table in tables:
            source_count = source.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            expected = source_count
            if table == "schedule_lessons":
                expected -= healed["lesson_dupes"]
            if copied.get(table, 0) != expected:
                mismatches.append(f"{table}: source {source_count}, copied {copied.get(table, 0)}")

        with connection.cursor() as cursor:
            cursor.execute("PRAGMA foreign_key_check")
            fk_rows = cursor.fetchall()

        if mismatches or fk_rows:
            for m in mismatches:
                self.stderr.write("POST-FLIGHT: " + m)
            if fk_rows:
                self.stderr.write(f"POST-FLIGHT: {len(fk_rows)} foreign_key_check violation(s)")
            raise CommandError("post-flight failed — the transaction was committed, inspect before serving")

        self.stdout.write(self.style.SUCCESS(
            f"copy complete: {sum(copied.values())} row(s) across {len(tables)} table(s), "
            f"foreign keys clean"))
