############################################################
#  [*] Regression tests — the cutover row copy
#
#  The copy_production_data command against a handmade
#  source file carrying every known production quirk: a
#  dangling quote reference, NULL-teacher timetable
#  duplicates, poll options without a position column, and
#  counters that drifted from their child rows. The copy
#  must land the rows, heal exactly those quirks, rebuild
#  the search shadow — and REFUSE to run over garbage (an
#  enum outside the target's CHECK) or over a non-empty
#  target, because silently dropping bad rows is worse than
#  stopping.
############################################################


import io
import sqlite3


from django.core.management import CommandError, call_command
from django.test import TestCase


from knfapp.chat.models import Message
from knfapp.news.models import NewsPost, Poll, PollOption
from knfapp.schedule.models import ScheduleLesson
from knfapp.users.models import User


AWARE = "2026-09-01T10:00:00+00:00"
NAIVE = "2026-09-01T10:00:00"


def build_source(path, bad_role=False):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT, email TEXT, display_name TEXT,
            password_hash TEXT, role TEXT, invited INTEGER, avatar_url TEXT, student_number TEXT,
            study_group TEXT, study_program TEXT, active INTEGER, chat_push_preview INTEGER,
            created_at TEXT, updated_at TEXT);
        CREATE TABLE news_posts (id TEXT PRIMARY KEY, title TEXT, content TEXT, summary TEXT,
            image_url TEXT, author_id TEXT, author_name TEXT, source TEXT, source_url TEXT,
            post_type TEXT, is_public INTEGER, likes_count INTEGER, comments_count INTEGER,
            shares_count INTEGER, published_at TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE news_likes (post_id TEXT, user_id TEXT, created_at TEXT);
        CREATE TABLE polls (id TEXT PRIMARY KEY, post_id TEXT, title TEXT, end_date TEXT,
            total_votes INTEGER, created_at TEXT);
        CREATE TABLE poll_options (id TEXT PRIMARY KEY, poll_id TEXT, text TEXT, votes INTEGER);
        CREATE TABLE poll_votes (poll_id TEXT, option_id TEXT, user_id TEXT, created_at TEXT);
        CREATE TABLE conversations (id TEXT PRIMARY KEY, type TEXT, title TEXT, avatar_emoji TEXT,
            message_ttl_seconds INTEGER, created_by TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE conversation_participants (conversation_id TEXT, user_id TEXT, pinned INTEGER,
            last_read_at TEXT, joined_at TEXT);
        CREATE TABLE messages (id TEXT PRIMARY KEY, conversation_id TEXT, sender_id TEXT,
            text TEXT, image_url TEXT, reply_to_id TEXT, deleted_at TEXT, client_msg_id TEXT,
            kind TEXT, edited_at TEXT, attachment_url TEXT, attachment_name TEXT,
            attachment_size INTEGER, attachment_mime TEXT, attachment_meta TEXT,
            link_preview TEXT, gallery TEXT, pinned_at TEXT, pinned_by TEXT,
            forwarded INTEGER, expires_at TEXT, created_at TEXT);
        CREATE TABLE schedule_lessons (id TEXT PRIMARY KEY, title TEXT, teacher TEXT, room TEXT,
            time_start TEXT, time_end TEXT, day_of_week INTEGER, group_name TEXT,
            semester TEXT, created_at TEXT);
    """)

    def user(uid, name, role="student"):
        conn.execute("INSERT INTO users VALUES (?,?,?,?,?,?,1,NULL,NULL,NULL,NULL,1,1,?,?)",
                     (uid, name, f"{name}@knf.vu.lt", name.capitalize(), "$2b$x", role, AWARE, AWARE))

    user("u1", "tomas")
    user("u2", "ona", role="root" if bad_role else "admin")

    # A post whose counters DRIFTED: one real like, counter says 7
    conn.execute("INSERT INTO news_posts VALUES ('p1','T','C','S',NULL,'u1','Tomas','user',NULL,"
                 "'social',1,7,3,2,?,?,?)", (NAIVE, AWARE, AWARE))
    conn.execute("INSERT INTO news_likes VALUES ('p1','u2',?)", (AWARE,))

    # A poll in stored option order, with drifted vote counters
    conn.execute("INSERT INTO polls VALUES ('poll1','p1','Kada?',NULL,9,?)", (AWARE,))
    for oid, text in (("o1", "Rytoj"), ("o2", "Poryt"), ("o3", "Niekada")):
        conn.execute("INSERT INTO poll_options VALUES (?,?,?,5)", (oid, "poll1", text))
    conn.execute("INSERT INTO poll_votes VALUES ('poll1','o2','u1',?)", (AWARE,))
    conn.execute("INSERT INTO poll_votes VALUES ('poll1','o2','u2',?)", (AWARE,))

    # A room with a healthy quote and a DANGLING one
    conn.execute("INSERT INTO conversations VALUES ('c1','direct',NULL,NULL,NULL,'u1',?,?)",
                 (NAIVE, NAIVE))
    for uid in ("u1", "u2"):
        conn.execute("INSERT INTO conversation_participants VALUES ('c1',?,0,NULL,?)", (uid, NAIVE))
    conn.execute("INSERT INTO messages (id, conversation_id, sender_id, text, kind, forwarded, created_at)"
                 " VALUES ('m1','c1','u1','Labas','text',0,?)", (NAIVE,))
    conn.execute("INSERT INTO messages (id, conversation_id, sender_id, text, reply_to_id, kind, forwarded, created_at)"
                 " VALUES ('m2','c1','u2','Atsakau','m1','text',0,?)", (NAIVE,))
    conn.execute("INSERT INTO messages (id, conversation_id, sender_id, text, reply_to_id, kind, forwarded, created_at)"
                 " VALUES ('m3','c1','u1','Vaiduokliui','m-dinges','text',0,?)", (NAIVE,))

    # The same lesson three times: once clean, once with NULLs
    # that normalise into a duplicate, once genuinely distinct
    lesson = ("Programavimas", "10:00", "11:30", 0, "ISKS-1", "2026-R")
    conn.execute("INSERT INTO schedule_lessons VALUES ('l1',?, 'A. P.','302',?,?,?,?,?,?)",
                 (lesson[0], *lesson[1:], NAIVE))
    conn.execute("INSERT INTO schedule_lessons VALUES ('l2',?, NULL,NULL,?,?,?,?,?,?)",
                 (lesson[0], *lesson[1:], NAIVE))
    conn.execute("INSERT INTO schedule_lessons VALUES ('l3',?, NULL,NULL,?,?,?,?,?,?)",
                 (lesson[0], *lesson[1:], NAIVE))

    conn.commit()
    conn.close()


class CopyProductionDataTests(TestCase):

    def _source(self, **kwargs):
        import tempfile
        handle = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        handle.close()
        build_source(handle.name, **kwargs)
        self.addCleanup(lambda: __import__("os").unlink(handle.name))
        return handle.name

    def test_the_copy_lands_and_heals_every_known_quirk(self):
        out = io.StringIO()
        call_command("copy_production_data", source=self._source(), stdout=out)

        # The rows landed
        self.assertEqual(User.objects.count(), 2)
        self.assertEqual(Message.objects.count(), 3)

        # The healthy quote survived, the dangling one is NULL
        self.assertEqual(Message.objects.get(id="m2").reply_to_id, "m1")
        self.assertIsNone(Message.objects.get(id="m3").reply_to_id)

        # Counters re-derived from child rows, not trusted
        post = NewsPost.objects.get(id="p1")
        self.assertEqual((post.likes_count, post.comments_count), (1, 0))
        self.assertEqual(post.shares_count, 2)  # no child table — copied as counted
        self.assertEqual(Poll.objects.get(id="poll1").total_votes, 2)
        self.assertEqual([(o.id, o.votes) for o in PollOption.objects.order_by("position")],
                         [("o1", 0), ("o2", 2), ("o3", 0)])

        # The NULL-teacher duplicates collapsed to one healed row
        lessons = list(ScheduleLesson.objects.order_by("id"))
        self.assertEqual(len(lessons), 2)
        self.assertEqual({(l.teacher, l.room) for l in lessons}, {("A. P.", "302"), ("", "")})

        # The gates the command promised
        report = out.getvalue()
        self.assertIn("1 dangling quote ref(s) NULLed", report)
        self.assertIn("1 duplicate(s) dropped", report)
        self.assertIn("foreign keys clean", report)

    def test_garbage_enums_abort_before_the_first_write(self):
        with self.assertRaises(CommandError):
            call_command("copy_production_data", source=self._source(bad_role=True),
                         stdout=io.StringIO(), stderr=io.StringIO())
        self.assertEqual(User.objects.count(), 0)  # nothing landed

    def test_a_nonempty_target_is_refused_without_the_flag(self):
        from .utils import create_user
        create_user(username="jau")
        with self.assertRaises(CommandError):
            call_command("copy_production_data", source=self._source(), stdout=io.StringIO())
