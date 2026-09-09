############################################################
#  [*] Regression tests — the schema hardening decisions
#
#  What the hardened schema promises: the TTL sweep
#  commits under a live reply (reply_to is a loose ref — a
#  real FK would brick the room), the NULL-subject activity
#  rows are unique and re-recording refreshes instead of
#  crashing, username/email uniqueness binds case-
#  insensitively at the database, the enum CHECKs on
#  messages.kind and push_tokens.platform refuse garbage,
#  the self-pair CHECKs refuse self-friendship and
#  self-blocks, the schedule natural-key columns refuse the
#  NULL that would dodge dedup, and a poll's wire total is
#  the sum of its options — there is no stored twin to
#  drift.
############################################################


import uuid


from django.db import IntegrityError, connection, transaction
from django.test import Client, TestCase, TransactionTestCase


from knfapp.chat import events
from knfapp.chat.models import Message
from knfapp.common.timestamps import utc_now_iso
from knfapp.notifications.models import PushToken
from knfapp.social.activity import record_activity
from knfapp.social.models import Activity, Friendship
from knfapp.users import auth
from .utils import bearer, create_message, create_room, create_user, naive_now


class SweepUnderLiveReplyTests(TransactionTestCase):

    # TransactionTestCase on purpose: the harness transaction
    # of a plain TestCase defers FK checks past the assertion
    # window — this pin must exercise the sweep the way
    # production runs it, committing on autocommit

    def setUp(self):
        events.reset_socket_state()
        self.user = create_user(username="tomas")
        self.other = create_user(username="ona")
        self.token = auth.mint_session(self.user.id)
        self.client = Client()

    def test_an_expired_quoted_message_sweeps_out_under_its_live_reply(self):
        room = create_room([self.user, self.other], message_ttl_seconds=60)
        quoted = create_message(room, self.other, text="nyks", minutes_ago=10,
                                expires_at=naive_now(minutes_ago=5))
        reply = create_message(room, self.user, text="atsakymas", reply_to=quoted)

        # The read triggers the sweep; a real FK on reply_to_id
        # would fail exactly this commit
        response = bearer(self.client.get, f"/api/chat/conversations/{room.id}/messages", self.token)
        self.assertEqual(response.status_code, 200)

        self.assertFalse(Message.objects.filter(id=quoted.id).exists())
        reply.refresh_from_db()
        self.assertEqual(reply.reply_to_id, quoted.id)   # the dangling loose ref, by design

        # The survivor renders — its quote shapes as the ghost
        payload = response.json()
        kept = [m for m in payload["messages"] if m["id"] == reply.id]
        self.assertEqual(len(kept), 1)


class ActivityUniquenessTests(TestCase):

    def setUp(self):
        self.user = create_user(username="tomas")
        self.actor = create_user(username="ona")

    def test_null_subject_rows_are_unique_at_the_database(self):
        fields = dict(user_id=self.user.id, kind="connect_accept", actor_id=self.actor.id,
                      subject_id=None, created_at=utc_now_iso())
        Activity.objects.create(id=str(uuid.uuid4()), **fields)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Activity.objects.create(id=str(uuid.uuid4()), **fields)

    def test_re_recording_refreshes_the_row_instead_of_crashing(self):
        record_activity(self.user.id, "connect_accept", self.actor.id)
        first = Activity.objects.get()
        record_activity(self.user.id, "connect_accept", self.actor.id)
        # One row, same key — refreshed, never re-minted into a
        # phantom twin the partial unique would refuse
        second = Activity.objects.get()
        self.assertEqual(first.id, second.id)


class CaseInsensitiveUniquenessTests(TestCase):

    def test_a_case_variant_twin_is_refused_by_the_database(self):
        create_user(username="Tomas", email="tomas@knf.vu.lt")
        for username, email in (("tomas", "kitas@knf.vu.lt"), ("kitas", "TOMAS@knf.vu.lt")):
            with self.assertRaises(IntegrityError), transaction.atomic():
                create_user(username=username, email=email)


class EnumCheckTests(TestCase):

    def setUp(self):
        self.user = create_user(username="tomas")

    def test_message_kind_refuses_values_outside_the_set(self):
        room = create_room([self.user])
        with self.assertRaises(IntegrityError), transaction.atomic():
            create_message(room, self.user, kind="hologram")

    def test_push_platform_refuses_values_outside_the_set(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            PushToken.objects.create(id=str(uuid.uuid4()), user_id=self.user.id,
                                     token="t-1", platform="toaster",
                                     created_at=utc_now_iso(), updated_at=utc_now_iso())

    def test_self_pairs_are_refused(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Friendship.objects.create(user_id=self.user.id, friend_id=self.user.id,
                                      created_at=utc_now_iso())


class ScheduleNaturalKeyTests(TestCase):

    def test_the_key_columns_refuse_null_and_dedup_stays_total(self):
        # A raw INSERT with NULL teacher — the shape that would
        # dodge the natural key — dies at the constraint
        with self.assertRaises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO schedule_lessons
                       (id, title, teacher, room, time_start, time_end, day_of_week,
                        group_name, semester, created_at)
                       VALUES (%s, %s, NULL, '', '10:00', '11:30', 0, 'G-1', '2026-R', %s)""",
                    (str(uuid.uuid4()), "Programavimas", utc_now_iso()),
                )

        # With '' enforced, the scraper's conflict-ignoring
        # insert (the portable ON CONFLICT spelling) dedups
        # the twin
        with connection.cursor() as cursor:
            for _ in range(2):
                cursor.execute(
                    """INSERT INTO schedule_lessons
                       (id, title, teacher, room, time_start, time_end, day_of_week,
                        group_name, semester, created_at)
                       VALUES (%s, %s, '', '', '10:00', '11:30', 0, 'G-1', '2026-R', %s)
                       ON CONFLICT (semester, group_name, day_of_week, time_start,
                                    time_end, title, teacher, room) DO NOTHING""",
                    (str(uuid.uuid4()), "Programavimas", utc_now_iso()),
                )
            cursor.execute("SELECT COUNT(*) FROM schedule_lessons")
            self.assertEqual(cursor.fetchone()[0], 1)


class PollTotalTests(TestCase):

    def test_the_wire_total_is_the_sum_of_the_options(self):
        from knfapp.news.core import poll_shape
        shaped = poll_shape(
            {"id": "p", "post_id": "n", "title": "Kada?", "end_date": None, "created_at": utc_now_iso()},
            [{"id": "o1", "text": "Rytoj", "votes": 2}, {"id": "o2", "text": "Poryt", "votes": 3}],
            None,
        )
        self.assertEqual(shaped["totalVotes"], 5)
        # There is no stored column to drift from this sum
        with connection.cursor() as cursor:
            cursor.execute("SELECT name FROM pragma_table_info('polls') WHERE name = 'total_votes'")
            self.assertIsNone(cursor.fetchone())
