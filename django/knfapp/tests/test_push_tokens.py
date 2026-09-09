############################################################
#  [*] Regression tests — push tokens and channel switches
#
#  The token grammar gate, the one-statement upsert's three
#  meanings (insert / reactivate / takeover), the per-user
#  fleet cap, owner-scoped removal, the opt-out channel
#  model with its validate-before-write batch, and the
#  chat-preview privacy flag.
############################################################


import uuid


from django.test import Client, TestCase


from knfapp.common import ratelimit
from knfapp.common.timestamps import utc_now_iso
from knfapp.notifications.models import NotificationChannel, PushToken
from knfapp.users import auth
from .utils import bearer, create_user


def _token(seed="abcdefgh0123"):
    return f"ExponentPushToken[{seed}]"


class RegisterTokenTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.client = Client()
        self.user = create_user()
        self.token = auth.mint_session(self.user.id)

    def _register(self, push_token, session=None, **body):
        return bearer(self.client.post, "/api/notifications/register", session or self.token,
                      data={"token": push_token, **body}, content_type="application/json")

    def test_the_whole_grammar_is_the_gate(self):
        for bad in ("ExponentPushToken[", "ExponentPushToken[has space]",
                    "ExponentPushToken[<script>]", "ExponentPushToken[short]", "kitoks"):
            self.assertEqual(self._register(bad).status_code, 400, bad)

    def test_fresh_is_201_and_own_repeat_is_200_with_the_same_id(self):
        first = self._register(_token())
        self.assertEqual(first.status_code, 201)
        token_id = first.json()["tokenId"]
        repeat = self._register(_token(), platform="ios", language="en")
        self.assertEqual((repeat.status_code, repeat.json()["tokenId"]), (200, token_id))
        row = PushToken.objects.get(token=_token())
        self.assertEqual((row.platform, row.language), ("ios", "en"))

    def test_a_takeover_reassigns_and_reactivates(self):
        self._register(_token())
        PushToken.objects.filter(token=_token()).update(active=0)
        other = create_user(username="kitas", email="k@knf.vu.lt")
        response = self._register(_token(), session=auth.mint_session(other.id))
        self.assertEqual(response.status_code, 201)   # not "own" — the device changed hands
        row = PushToken.objects.get(token=_token())
        self.assertEqual((row.user_id, row.active), (other.id, 1))

    def test_the_fleet_cap_drops_the_stalest_rows(self):
        for i in range(12):
            PushToken.objects.create(id=str(uuid.uuid4()), user=self.user,
                                     token=_token(f"senas{i:07d}"), created_at=utc_now_iso(),
                                     updated_at=f"2026-01-{i + 1:02d}T00:00:00")
        self._register(_token("naujausias1"))
        self.assertEqual(PushToken.objects.filter(user=self.user).count(), 10)
        # The newest survivors include the fresh row; the oldest went
        self.assertFalse(PushToken.objects.filter(token=_token("senas0000000")).exists())

    def test_unregister_is_owner_scoped(self):
        self._register(_token())
        stranger = create_user(username="kitas", email="k@knf.vu.lt")
        foreign = bearer(self.client.delete, "/api/notifications/register",
                         auth.mint_session(stranger.id),
                         data={"token": _token()}, content_type="application/json")
        self.assertEqual(foreign.status_code, 404)
        own = bearer(self.client.delete, "/api/notifications/register", self.token,
                     data={"token": _token()}, content_type="application/json")
        self.assertEqual(own.status_code, 200)
        # No grammar gate on the way out — a stored row of any
        # shape must be removable by its owner
        PushToken.objects.create(id=str(uuid.uuid4()), user=self.user, token="senas-formatas",
                                 created_at=utc_now_iso(), updated_at=utc_now_iso())
        legacy = bearer(self.client.delete, "/api/notifications/register", self.token,
                        data={"token": "senas-formatas"}, content_type="application/json")
        self.assertEqual(legacy.status_code, 200)


class ChannelTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.client = Client()
        self.user = create_user()
        self.token = auth.mint_session(self.user.id)

    def test_missing_rows_read_as_enabled(self):
        body = bearer(self.client.get, "/api/notifications/channels", self.token).json()
        self.assertEqual(body["channels"], {"news": True, "chat": True, "schedule": True, "admin": True})

    def test_a_partial_put_answers_the_full_state(self):
        response = bearer(self.client.put, "/api/notifications/channels", self.token,
                          data={"channels": {"chat": False}}, content_type="application/json")
        self.assertEqual(response.json()["channels"],
                         {"news": True, "chat": False, "schedule": True, "admin": True})
        self.assertEqual(NotificationChannel.objects.count(), 1)

    def test_the_batch_validates_before_the_first_write(self):
        response = bearer(self.client.put, "/api/notifications/channels", self.token,
                          data={"channels": {"chat": False, "netikras": True}},
                          content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("netikras", response.json()["error"])
        # Nothing half-applied
        self.assertEqual(NotificationChannel.objects.count(), 0)

        typed = bearer(self.client.put, "/api/notifications/channels", self.token,
                       data={"channels": {"chat": 1}}, content_type="application/json")
        self.assertEqual(typed.status_code, 400)
        self.assertIn("int", typed.json()["error"])

    def test_chat_preview_round_trip(self):
        self.assertTrue(bearer(self.client.get, "/api/notifications/chat-preview",
                               self.token).json()["enabled"])
        response = bearer(self.client.put, "/api/notifications/chat-preview", self.token,
                          data={"enabled": False}, content_type="application/json")
        self.assertEqual(response.json(), {"enabled": False})
        self.assertFalse(bearer(self.client.get, "/api/notifications/chat-preview",
                                self.token).json()["enabled"])
        typed = bearer(self.client.put, "/api/notifications/chat-preview", self.token,
                       data={"enabled": "ne"}, content_type="application/json")
        self.assertEqual(typed.status_code, 400)
