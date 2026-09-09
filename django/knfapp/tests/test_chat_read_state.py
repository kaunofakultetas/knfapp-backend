############################################################
#  [*] Regression tests — chat read state, presence, people
#
#  The two read stores agreeing (the watermark that only
#  ever advances, the receipt cap, the shared socket+REST
#  budget), the relationship-gated presence oracle, the
#  people picker's exclusions and ranking, the socket rate
#  window, and the chat side of erasure and export.
############################################################


import json


from django.test import Client, TestCase


from knfapp.chat import events
from knfapp.chat.api.views import _apply_mark_read
from knfapp.chat.models import ConversationParticipant, Message, MessageRead
from knfapp.common import ratelimit
from knfapp.users import auth
from .utils import PASSWORD, bearer, create_message, create_room, create_user, naive_now


class ReadStateTestCase(TestCase):

    def setUp(self):
        ratelimit.reset()
        events.reset_socket_state()
        self.tomas = create_user(username="tomas")
        self.ona = create_user(username="ona")
        self.tomas_token = auth.mint_session(self.tomas.id)
        self.ona_token = auth.mint_session(self.ona.id)
        self.room = create_room([self.tomas, self.ona])
        self.client = Client()


class MarkReadTests(ReadStateTestCase):

    def test_both_stores_move_and_the_badges_clear(self):
        create_message(self.room, self.ona, minutes_ago=2)
        create_message(self.room, self.ona, minutes_ago=1)

        response = bearer(self.client.put, f"/api/chat/conversations/{self.room.id}/read", self.tomas_token)
        self.assertEqual(json.loads(response.content)["readCount"], 2)
        self.assertEqual(MessageRead.objects.filter(user_id=self.tomas.id).count(), 2)

        total = bearer(self.client.get, "/api/chat/unread-count", self.tomas_token)
        self.assertEqual(json.loads(total.content)["unreadCount"], 0)
        # A second call finds nothing new — no re-broadcast fodder
        response = bearer(self.client.put, f"/api/chat/conversations/{self.room.id}/read", self.tomas_token)
        self.assertEqual(json.loads(response.content)["readCount"], 0)

    def test_the_watermark_never_regresses(self):
        create_message(self.room, self.ona, minutes_ago=3)
        fresh = naive_now()
        stale = naive_now(minutes_ago=10)

        self.assertIsNotNone(_apply_mark_read(self.room.id, self.tomas.id, fresh))
        # The out-of-order twin commits later with an older `now`
        # — the advance-only guard lives in the UPDATE itself,
        # so the same statement holds on either engine
        _apply_mark_read(self.room.id, self.tomas.id, stale)
        row = ConversationParticipant.objects.get(conversation_id=self.room.id, user_id=self.tomas.id)
        self.assertEqual(row.last_read_at.replace(tzinfo=None), fresh)

    def test_a_non_member_writes_nothing_and_hears_403(self):
        outsider = create_user(username="pasalinis")
        outsider_token = auth.mint_session(outsider.id)
        response = bearer(self.client.put, f"/api/chat/conversations/{self.room.id}/read", outsider_token)
        self.assertEqual(response.status_code, 403)

    def test_rest_spends_the_socket_budget(self):
        # 10 per 10 s, shared — the REST path is not the free
        # bypass around the socket quota
        for _ in range(10):
            self.assertEqual(bearer(self.client.put,
                                    f"/api/chat/conversations/{self.room.id}/read",
                                    self.tomas_token).status_code, 200)
        response = bearer(self.client.put, f"/api/chat/conversations/{self.room.id}/read", self.tomas_token)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(json.loads(response.content)["code"], "rate_limited")


class SocketHandshakeTests(TestCase):

    def test_a_valid_token_joins_every_room_and_lands_in_presence(self):
        # The front door: an accepted handshake must join the
        # user's conv:* rooms and record presence. This runs the
        # REAL handler against a recording sio stub — a crash
        # inside its try-arm reads as a silent rejection that no
        # HTTP pin would ever notice
        user = create_user(username="tomas")
        other = create_user(username="ona")
        room = create_room([user, other])
        token = auth.mint_session(user.id)

        calls = {"handlers": {}, "rooms": [], "emits": []}

        class _Sio:
            def on(self, event, handler=None):
                calls["handlers"][event] = handler

            def enter_room(self, sid, room_name):
                calls["rooms"].append(room_name)

            def emit(self, event, payload=None, to=None, **kwargs):
                calls["emits"].append(event)

        events.reset_socket_state()
        try:
            events.register_socket_events(_Sio())
            accepted = calls["handlers"]["connect"]("sid-1", {}, {"token": token})
            self.assertNotEqual(accepted, False)
            self.assertIn(f"conv:{room.id}", calls["rooms"])
            self.assertEqual(events._connected_users.get("sid-1"), user.id)
            self.assertIn("connected", calls["emits"])
        finally:
            events.reset_socket_state()


class SocketRateTests(TestCase):

    def setUp(self):
        events.reset_socket_state()
        self.addCleanup(events.reset_socket_state)

    def test_the_window_fills_and_unlisted_events_pass_free(self):
        for _ in range(10):
            self.assertFalse(events._socket_rate_check("u1", "mark_read"))
        self.assertTrue(events._socket_rate_check("u1", "mark_read"))
        # Another user's budget is their own
        self.assertFalse(events._socket_rate_check("u2", "mark_read"))
        # An event with no entry in the table is unlimited
        for _ in range(50):
            self.assertFalse(events._socket_rate_check("u1", "nezinomas"))


class PresenceTests(ReadStateTestCase):

    def test_presence_is_relationship_gated(self):
        stranger = create_user(username="svetimas")
        # Both are "online" in this process
        events._connected_users["sid-ona"] = self.ona.id
        events._connected_users["sid-svetimas"] = stranger.id
        self.addCleanup(events.reset_socket_state)

        response = bearer(self.client.post, "/api/chat/online-status", self.tomas_token,
                          data=json.dumps({"userIds": [self.ona.id, stranger.id]}),
                          content_type="application/json")
        online = json.loads(response.content)["online"]
        # The roommate reads true; the stranger reads exactly like
        # a genuinely offline user — no free presence oracle
        self.assertTrue(online[self.ona.id])
        self.assertFalse(online[stranger.id])


class PeoplePickerTests(ReadStateTestCase):

    def test_exclusions_and_ranking(self):
        create_user(username="onute")          # display "Onute"
        create_user(username="miegantis", active=0)
        from knfapp.social.models import UserBlock
        blocked = create_user(username="onablok")
        UserBlock.objects.create(blocker=blocked, blocked=self.tomas, created_at=naive_now())

        response = json.loads(bearer(self.client.get, "/api/chat/users/search?q=on",
                                     self.tomas_token).content)
        names = [u["username"] for u in response["users"]]
        self.assertNotIn("miegantis", names)   # deactivated
        self.assertNotIn("onablok", names)     # a block pair, either direction
        self.assertNotIn("tomas", names)       # never the caller
        self.assertEqual(names[0], "ona")      # the shorter display name sorts first in its tier

        # The keystroke warm-up answers empty, no directory hit
        response = json.loads(bearer(self.client.get, "/api/chat/users/search?q=o",
                                     self.tomas_token).content)
        self.assertEqual(response["users"], [])

    def test_an_exact_username_hit_ranks_first(self):
        create_user(username="onas")  # display "Onas" — prefix tier
        response = json.loads(bearer(self.client.get, "/api/chat/users/search?q=ona",
                                     self.tomas_token).content)
        self.assertEqual(response["users"][0]["username"], "ona")


class ChatErasureParityTests(ReadStateTestCase):

    def test_erasure_now_reaches_the_chat_tables(self):
        # Erasure's chat pass: photo refs nulled, the reader
        # state hard-deleted, the messages kept for the room
        msg = create_message(self.room, self.tomas, text="Lieka",
                             image_url="/api/uploads/" + "e" * 32 + ".jpg")
        foreign = create_message(self.room, self.ona)
        MessageRead.objects.create(message=foreign, user=self.tomas, read_at=naive_now())

        response = bearer(self.client.delete, "/api/auth/me", self.tomas_token,
                          data=json.dumps({"password": PASSWORD}), content_type="application/json")
        self.assertEqual(response.status_code, 200)

        row = Message.objects.get(id=msg.id)
        self.assertEqual(row.text, "Lieka")          # shared history stays
        self.assertIsNone(row.image_url)             # the personal photo ref does not
        self.assertEqual(MessageRead.objects.filter(user_id=self.tomas.id).count(), 0)
        self.assertEqual(ConversationParticipant.objects.filter(user_id=self.tomas.id).count(), 0)

    def test_the_export_now_carries_the_chat_sections(self):
        create_message(self.room, self.tomas, text="Mano žinutė")
        response = bearer(self.client.get, "/api/auth/me/export", self.tomas_token)
        payload = json.loads(response.content)
        self.assertEqual([m["text"] for m in payload["messages"]], ["Mano žinutė"])
        self.assertEqual([c["id"] for c in payload["conversations"]], [self.room.id])
