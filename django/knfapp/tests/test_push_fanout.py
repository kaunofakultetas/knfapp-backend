############################################################
#  [*] Regression tests — push fan-out targeting
#
#  Who a channel send actually reaches, with the Expo
#  transport patched out (send_push_batch is the seam —
#  nothing here touches the network): the opt-out model (a
#  missing notification_channels row means ENABLED, only an
#  explicit enabled=0 suppresses), the unknown-channel
#  refusal that would otherwise ignore every opt-out, the
#  language split routing the English copy, the distinct-
#  owner count behind stats["users"], the orphan-token
#  prune, and the token redaction that keeps a bearer
#  credential out of log lines.
############################################################


import uuid
from datetime import datetime, timedelta, timezone


from django.test import TestCase


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
