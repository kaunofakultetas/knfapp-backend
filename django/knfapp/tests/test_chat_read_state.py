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

    def test_a_bad_token_is_refused_with_reason_unauthorized(self):
        # The reason string is the client's ONLY way to tell a
        # dead session from a capacity refusal — the mobile app
        # shows "session expired" for 'unauthorized' alone
        from socketio.exceptions import ConnectionRefusedError

        calls = {"handlers": {}}

        class _Sio:
            def on(self, event, handler=None):
                calls["handlers"][event] = handler

            def enter_room(self, sid, room_name):
                pass

            def emit(self, event, payload=None, to=None, **kwargs):
                pass

        events.reset_socket_state()
        try:
            events.register_socket_events(_Sio())
            with self.assertRaises(ConnectionRefusedError) as caught:
                calls["handlers"]["connect"]("sid-x", {}, {"token": "netikras"})
            self.assertEqual(caught.exception.error_args.get("message"), "unauthorized")
        finally:
            events.reset_socket_state()

    def test_past_the_user_cap_the_oldest_socket_is_evicted_and_the_newcomer_admitted(self):
        # Newest wins: five slots full (mostly zombies in real
        # life) must NOT read as a dead session to a user who just
        # logged in — the oldest sid goes, the fresh handshake
        # lands. The evicted sid is also force-disconnected so a
        # LIVE old device notices and reconnects.
        user = create_user(username="tomas")
        token = auth.mint_session(user.id)

        calls = {"handlers": {}, "disconnected": []}

        class _Sio:
            def on(self, event, handler=None):
                calls["handlers"][event] = handler

            def enter_room(self, sid, room_name):
                pass

            def emit(self, event, payload=None, to=None, **kwargs):
                pass

            def disconnect(self, sid):
                calls["disconnected"].append(sid)

        events.reset_socket_state()
        try:
            events.register_socket_events(_Sio())
            for i in range(events._MAX_SOCKETS_PER_USER):
                events._connected_users[f"sid-{i}"] = user.id
                events._connected_names[f"sid-{i}"] = "Tomas"

            accepted = calls["handlers"]["connect"]("sid-new", {}, {"token": token})

            self.assertNotEqual(accepted, False)
            self.assertEqual(calls["disconnected"], ["sid-0"])
            self.assertNotIn("sid-0", events._connected_users)
            self.assertEqual(events._connected_users.get("sid-new"), user.id)
            # The cap still holds: five sockets, newcomer included
            self.assertEqual(
                sum(1 for uid in events._connected_users.values() if uid == user.id),
                events._MAX_SOCKETS_PER_USER,
            )
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

    def test_an_offline_roommate_reads_false(self):
        # The gate passing must not imply presence: ona shares
        # the room but holds no socket. Without this case the
        # stranger test alone conflates "gated" with "offline"
        response = bearer(self.client.post, "/api/chat/online-status", self.tomas_token,
                          data=json.dumps({"userIds": [self.ona.id]}),
                          content_type="application/json")
        self.assertFalse(json.loads(response.content)["online"][self.ona.id])

    def test_disconnect_takes_one_socket_never_the_whole_user(self):
        # Two devices, one closes: the user must stay online
        # until the LAST sid leaves the table — asserted through
        # the endpoint, with the REAL disconnect handler
        handlers = {}

        class _Sio:
            def on(self, event, handler=None):
                handlers[event] = handler

            def enter_room(self, sid, room_name):
                pass

            def emit(self, event, payload=None, to=None, **kwargs):
                pass

        events.register_socket_events(_Sio())
        self.addCleanup(events.reset_socket_state)
        events._connected_users["sid-phone"] = self.ona.id
        events._connected_users["sid-tab"] = self.ona.id

        def ona_online():
            response = bearer(self.client.post, "/api/chat/online-status", self.tomas_token,
                              data=json.dumps({"userIds": [self.ona.id]}),
                              content_type="application/json")
            return json.loads(response.content)["online"][self.ona.id]

        handlers["disconnect"]("sid-phone")
        self.assertTrue(ona_online())
        handlers["disconnect"]("sid-tab")
        self.assertFalse(ona_online())
        # A sid the table never held is a no-op, not an error
        handlers["disconnect"]("sid-nezinomas")
        # The disconnect edge stamped the last-seen column
        self.ona.refresh_from_db()
        self.assertIsNotNone(self.ona.last_active_at)

    def test_last_seen_rides_the_same_gate(self):
        # ona (roommate) carries a stamp, the stranger carries
        # one too — only the roommate's is revealed; a stamped
        # stranger reads null exactly like a never-seen account
        stranger = create_user(username="svetimas")
        from knfapp.common.timestamps import utc_now
        from knfapp.users.models import User
        stamp = utc_now()
        User.objects.filter(id__in=[self.ona.id, stranger.id]).update(last_active_at=stamp)

        response = bearer(self.client.post, "/api/chat/online-status", self.tomas_token,
                          data=json.dumps({"userIds": [self.ona.id, stranger.id]}),
                          content_type="application/json")
        body = json.loads(response.content)
        self.assertEqual(body["lastSeen"][self.ona.id], stamp.isoformat())
        self.assertIsNone(body["lastSeen"][stranger.id])

    def test_a_never_connected_roommate_reads_null_last_seen(self):
        response = bearer(self.client.post, "/api/chat/online-status", self.tomas_token,
                          data=json.dumps({"userIds": [self.ona.id]}),
                          content_type="application/json")
        self.assertIsNone(json.loads(response.content)["lastSeen"][self.ona.id])

    def test_input_hygiene_drops_junk_and_truncates_at_200(self):
        # A non-array body is the one shape that earns a 400
        response = bearer(self.client.post, "/api/chat/online-status", self.tomas_token,
                          data=json.dumps({"userIds": "ne-sarasas"}),
                          content_type="application/json")
        self.assertEqual(response.status_code, 400)

        # Non-string ids are dropped FIRST, then the list is cut
        # to 200 — ona rides in position 201 and must fall off,
        # and the strangers that remain all read false
        ids = [7, None] + [f"id-{i}" for i in range(200)] + [self.ona.id]
        response = bearer(self.client.post, "/api/chat/online-status", self.tomas_token,
                          data=json.dumps({"userIds": ids}),
                          content_type="application/json")
        online = json.loads(response.content)["online"]
        self.assertEqual(len(online), 200)
        self.assertNotIn(self.ona.id, online)
        self.assertFalse(any(online.values()))


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
