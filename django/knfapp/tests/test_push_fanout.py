############################################################
#  [*] Regression tests — push fan-out targeting
#
#  Who a channel send actually reaches, with the Expo
#  transport patched out (send_push_batch is the seam —
#  nothing here touches the network): the opt-out model (a
#  missing notification_channels row means ENABLED, only an
#  explicit enabled=0 suppresses), the unknown-channel
#  refusal that would otherwise ignore every opt-out, the
#  language split routing the English copy (ONE batch when a
#  caller has no English copy at all, and a copy given for one
#  field only falling back per field — KNF-071), the distinct-
#  owner count behind stats["users"], the orphan-token
#  prune, the dead-device retirement that must never
#  recycle the connection from inside a transaction (an
#  ATOMIC_REQUESTS view would roll back behind its 201),
#  the token redaction that keeps a bearer credential out
#  of log lines, and the two retry policies of the Expo
#  transport — a send that timed out reading is never
#  replayed (Expo may have enqueued it; a replay is a
#  duplicate on every phone), a receipt query still is.
############################################################


import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock


import requests
from urllib3.connectionpool import HTTPConnectionPool
from urllib3.exceptions import ReadTimeoutError
from urllib3.util.retry import Retry

from django.db import connection
from django.test import SimpleTestCase, TestCase


from knfapp.notifications import push
from knfapp.notifications.models import NotificationChannel, PushToken
from knfapp.users import auth
from knfapp.users.models import Session
from .utils import create_user


def _token(user, name, language="lt", active=1):
    now = "2026-01-01T00:00:00"
    return PushToken.objects.create(id=str(uuid.uuid4()), user_id=user.id,
                                    token=f"ExponentPushToken[{name}]",
                                    language=language, active=active,
                                    created_at=now, updated_at=now)


class FanoutTargetingTests(TestCase):

    def setUp(self):
        self.calls = []

        def fake_batch(tokens, title, body, data=None, priority=None, ttl=None, stats=None):
            self.calls.append({"tokens": list(tokens), "title": title, "body": body,
                               "data": data, "priority": priority, "ttl": ttl})
            return len(tokens)

        self._real = push.send_push_batch
        push.send_push_batch = fake_batch
        self.addCleanup(lambda: setattr(push, "send_push_batch", self._real))

        self.reader = create_user(username="skaitytojas")
        self.optout = create_user(username="atsisakes")

    def _sent_tokens(self):
        return [t for call in self.calls for t in call["tokens"]]

    def test_only_an_explicit_disable_suppresses(self):
        _token(self.reader, "a")
        _token(self.optout, "b")
        # An explicit enabled=1 row and a missing row both mean ENABLED
        NotificationChannel.objects.create(user_id=self.reader.id, channel="news",
                                           enabled=1, updated_at="2026-01-01T00:00:00")
        NotificationChannel.objects.create(user_id=self.optout.id, channel="news",
                                           enabled=0, updated_at="2026-01-01T00:00:00")

        sent = push.notify_channel("news", "KNF naujienos", "Tekstas")
        self.assertEqual(sent, 1)
        self.assertEqual(self._sent_tokens(), ["ExponentPushToken[a]"])

    def test_the_optout_is_per_channel_and_inactive_tokens_stay_out(self):
        _token(self.optout, "b")
        _token(self.optout, "dead", active=0)
        NotificationChannel.objects.create(user_id=self.optout.id, channel="news",
                                           enabled=0, updated_at="2026-01-01T00:00:00")
        # The news opt-out does not silence the schedule channel
        self.assertEqual(push.notify_channel("schedule", "Tvarkaraštis", "Tekstas"), 1)
        self.assertEqual(self._sent_tokens(), ["ExponentPushToken[b]"])

    def test_an_unknown_channel_sends_to_nobody(self):
        # No opt-out rows exist for a bogus name — sending would
        # ignore every opt-out, so it must send nothing at all
        _token(self.reader, "a")
        self.assertEqual(push.notify_channel("marketing", "X", "Y"), 0)
        self.assertEqual(push.notify_channel_users("marketing", [self.reader.id], "X", "Y"), 0)
        self.assertEqual(self.calls, [])

    def test_the_language_split_routes_the_english_copy(self):
        _token(self.reader, "lt1", language="lt")
        _token(self.reader, "en1", language="en")
        stats = {}
        push.notify_channel("news", "Naujienos", "Tekstas",
                            title_en="News", body_en="Text", stats=stats)
        by_title = {c["title"]: c["tokens"] for c in self.calls}
        self.assertEqual(by_title["Naujienos"], ["ExponentPushToken[lt1]"])
        self.assertEqual(by_title["News"], ["ExponentPushToken[en1]"])
        # Two devices, ONE owner — reach is reported in people
        self.assertEqual(stats["users"], 1)

    def test_no_english_copy_is_one_batch_for_every_device(self):
        # A caller with nothing to translate (a human-typed
        # broadcast) — the split would send the same bytes in
        # two round-trips; one batch carries every device
        _token(self.reader, "lt1", language="lt")
        _token(self.reader, "en1", language="en")
        push.notify_channel("news", "Naujienos", "Tekstas")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(sorted(self.calls[0]["tokens"]), ["ExponentPushToken[en1]", "ExponentPushToken[lt1]"])
        self.assertEqual((self.calls[0]["title"], self.calls[0]["body"]), ("Naujienos", "Tekstas"))

    def test_english_copy_given_alone_falls_back_per_field(self):
        # The chat fan-out's shape: the title (a sender's name)
        # needs no translation, the composed body does
        _token(self.reader, "lt1", language="lt")
        _token(self.reader, "en1", language="en")
        push.notify_channel_users("chat", [self.reader.id], "Tomas", "Nauja žinutė", body_en="New message")
        by_token = {call["tokens"][0]: (call["title"], call["body"]) for call in self.calls}
        self.assertEqual(by_token["ExponentPushToken[lt1]"], ("Tomas", "Nauja žinutė"))
        self.assertEqual(by_token["ExponentPushToken[en1]"], ("Tomas", "New message"))

    def test_the_channel_stamp_rides_a_copy_of_the_callers_data(self):
        _token(self.reader, "a")
        data = {"type": "news"}
        push.notify_channel("news", "T", "B", data=data)
        self.assertEqual(self.calls[0]["data"]["channel"], "news")
        self.assertNotIn("channel", data)  # the caller's dict never grows one

    def test_chat_sends_ride_high_priority_with_a_short_ttl(self):
        _token(self.reader, "a")
        push.notify_channel_users("chat", [self.reader.id], "Žinutė", "Tekstas")
        self.assertEqual((self.calls[0]["priority"], self.calls[0]["ttl"]), ("high", 3600))
        push.notify_channel("news", "T", "B")
        self.assertEqual((self.calls[1]["priority"], self.calls[1]["ttl"]), (None, 86400))

    def test_exclude_user_id_keeps_the_author_quiet(self):
        _token(self.reader, "a")
        _token(self.optout, "b")
        push.notify_channel("news", "T", "B", exclude_user_id=self.reader.id)
        self.assertEqual(self._sent_tokens(), ["ExponentPushToken[b]"])


class TokenHygieneTests(TestCase):

    def _record_connection_recycling(self):
        # The in-memory SQLite connection ignores close(), so the
        # proof is the CALL itself — a recorder stands in for
        # close_old_connections
        calls = []
        real = push.close_old_connections
        push.close_old_connections = lambda *args, **kwargs: calls.append((args, kwargs))
        self.addCleanup(lambda: setattr(push, "close_old_connections", real))
        return calls

    def test_the_orphan_prune_spares_owners_with_a_live_session(self):
        held = create_user(username="prisijunges")
        auth.mint_session(held.id)
        _token(held, "kept")

        gone = create_user(username="isejes")
        Session.objects.create(id=str(uuid.uuid4()), user_id=gone.id, token="pasibaigusi",
                               created_at="2026-01-01T00:00:00+00:00",
                               expires_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat())
        _token(gone, "dropped")

        self.assertEqual(push.prune_orphan_push_tokens(), 1)
        self.assertEqual(list(PushToken.objects.values_list("token", flat=True)),
                         ["ExponentPushToken[kept]"])

    def test_dead_devices_are_retired_not_deleted(self):
        user = create_user(username="istrynes")
        _token(user, "uninstalled")
        self.assertEqual(push._deactivate_tokens(["ExponentPushToken[uninstalled]", None]), 1)
        row = PushToken.objects.get()
        self.assertEqual(row.active, 0)  # the next register flips it back

    def test_log_excerpts_never_carry_a_raw_token(self):
        excerpt = push._sanitize('Expo said: "ExponentPushToken[abc123]" is not registered\r\nnext line')
        self.assertNotIn("abc123", excerpt)
        self.assertIn("token:", excerpt)
        self.assertNotIn("\n", excerpt)  # a body cannot forge extra log lines

    def test_retiring_inside_a_transaction_never_touches_the_connection(self):
        calls = self._record_connection_recycling()
        user = create_user(username="istrynes")
        _token(user, "uninstalled")
        # A TestCase method runs inside an atomic block — the
        # same footing as an ATOMIC_REQUESTS view
        self.assertTrue(connection.in_atomic_block)

        self.assertEqual(push._deactivate_tokens(["ExponentPushToken[uninstalled]"]), 1)
        self.assertEqual(calls, [])
        self.assertEqual(PushToken.objects.get().active, 0)

    def test_the_whole_route_survives_a_dead_device_inside_a_transaction(self):
        calls = self._record_connection_recycling()
        # Expo's verdict for the slice: nothing accepted, every
        # device gone — the transport is the seam, nothing here
        # touches the network
        real_slice = push._send_slice
        push._send_slice = lambda part, deadline: (0, [m["to"] for m in part],
                                                   {"DeviceNotRegistered": len(part)})
        self.addCleanup(lambda: setattr(push, "_send_slice", real_slice))

        reader = create_user(username="skaitytojas")
        _token(reader, "dead")

        self.assertEqual(push.notify_channel("news", "T", "B"), 0)
        self.assertEqual(PushToken.objects.get().active, 0)
        self.assertEqual(calls, [])


class ExpoTransportTests(SimpleTestCase):

    def _attempts_reading(self, url):
        # Every attempt urllib3 makes lands in _make_request — the
        # one seam below its retry loop and above the socket. It
        # raises a read timeout each time (the bytes went out, no
        # answer came back), the sleeps between retries are patched
        # out, and no connection is ever opened
        attempts = []

        def timed_out(pool, conn, method, path, *args, **kwargs):
            attempts.append(path)
            raise ReadTimeoutError(pool, path, "Read timed out.")

        with mock.patch.object(HTTPConnectionPool, "_make_request", autospec=True, side_effect=timed_out), \
                mock.patch.object(Retry, "sleep"):
            with self.assertRaises(requests.exceptions.RequestException):
                push._SESSION.post(url, json=[], headers=push._EXPO_HEADERS, timeout=1)
        return len(attempts)

    def test_a_send_that_timed_out_reading_is_never_replayed(self):
        # One shared policy used to replay the slice up to four
        # times and then report it as failed — four deliveries per
        # phone, counted as none
        self.assertEqual(self._attempts_reading(push.EXPO_PUSH_URL), 1)

    def test_a_receipt_query_still_retries(self):
        # Idempotent: the first try plus the policy's three retries
        self.assertEqual(self._attempts_reading(push.EXPO_RECEIPTS_URL), 4)

    def test_the_two_policies_are_what_the_banner_claims(self):
        send = push._SESSION.get_adapter(push.EXPO_PUSH_URL).max_retries
        receipts = push._SESSION.get_adapter(push.EXPO_RECEIPTS_URL).max_retries
        self.assertIsNot(send, receipts)

        # send: connection errors only — no read, status or other
        # retry, and a 429/5xx is answered as it came
        self.assertEqual((send.connect, send.read, send.status, send.other), (2, 0, 0, 0))
        self.assertFalse(send.is_retry("POST", 503, has_retry_after=False))
        self.assertFalse(send.is_retry("POST", 429, has_retry_after=True))

        # receipts: the full policy, Retry-After honoured but bounded
        self.assertEqual(receipts.total, 3)
        self.assertTrue(receipts.is_retry("POST", 503, has_retry_after=False))
        self.assertTrue(receipts.is_retry("POST", 429, has_retry_after=True))
        self.assertLessEqual(receipts.parse_retry_after("3600"), push._RETRY_AFTER_MAX)
        self.assertLessEqual(push._RETRY_AFTER_MAX, 10)

        # The send may read for the 30 s the admin broadcast banner
        # promises; the receipt query folds sooner
        self.assertEqual(push._SEND_TIMEOUT, (5, 30))
        self.assertEqual(push._RECEIPT_TIMEOUT[1], 10)
