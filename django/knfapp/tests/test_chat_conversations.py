############################################################
#  [*] Regression tests — chat conversations
#
#  The list's naming and counting rules (a direct chat is
#  named after the counterpart, never a stored title; the
#  row badge agrees with what the reader can still read),
#  the create gates (a group needs a title, a direct chat
#  strips an attacker-chosen one, blocked pairs answer one
#  flat 403), the two-member dedup that a planted
#  multi-member 'direct' row must never satisfy, and the
#  leave's ghost-reader purge.
############################################################


import json


from django.test import Client, TestCase


from knfapp.chat.models import Conversation, ConversationParticipant, Message, MessageRead, MessageReaction
from knfapp.common import ratelimit
from knfapp.users import auth
from .utils import bearer, create_message, create_room, create_user, naive_now


class ChatTestCase(TestCase):

    def setUp(self):
        ratelimit.reset()
        from knfapp.chat import events
        events.reset_socket_state()
        self.tomas = create_user(username="tomas")
        self.ona = create_user(username="ona")
        self.tomas_token = auth.mint_session(self.tomas.id)
        self.ona_token = auth.mint_session(self.ona.id)
        self.client = Client()

    def _post(self, path, token, body):
        return bearer(self.client.post, path, token,
                      data=json.dumps(body), content_type="application/json")


class ConversationListTests(ChatTestCase):

    def test_a_direct_chat_is_named_after_the_counterpart(self):
        create_room([self.tomas, self.ona])
        response = bearer(self.client.get, "/api/chat/conversations", self.tomas_token)
        row = json.loads(response.content)["conversations"][0]
        self.assertEqual(row["title"], "Ona")
        # …and the same room reads "Tomas" from the other side
        response = bearer(self.client.get, "/api/chat/conversations", self.ona_token)
        self.assertEqual(json.loads(response.content)["conversations"][0]["title"], "Tomas")

    def test_the_row_badge_counts_only_what_the_reader_can_still_read(self):
        room = create_room([self.tomas, self.ona])
        create_message(room, self.ona, minutes_ago=3)
        create_message(room, self.ona, minutes_ago=2, deleted_at=naive_now(2))  # unsent
        create_message(room, self.tomas, minutes_ago=1)  # own — never unread

        response = bearer(self.client.get, "/api/chat/conversations", self.tomas_token)
        row = json.loads(response.content)["conversations"][0]
        self.assertEqual(row["unreadCount"], 1)
        # The tab badge total agrees with the row badges
        total = bearer(self.client.get, "/api/chat/unread-count", self.tomas_token)
        self.assertEqual(json.loads(total.content)["unreadCount"], 1)
        # The unsent row still previews as a placeholder, not stale text
        self.assertTrue(row["lastMessage"]["deleted"] is False)  # newest is Tomas's own


class CreateConversationTests(ChatTestCase):

    def test_the_direct_dedup_answers_200_with_the_existing_room(self):
        first = self._post("/api/chat/conversations", self.tomas_token,
                           {"participantIds": [self.ona.id]})
        self.assertEqual(first.status_code, 201)
        conv_id = json.loads(first.content)["conversationId"]

        again = self._post("/api/chat/conversations", self.ona_token,
                           {"participantIds": [self.tomas.id]})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(json.loads(again.content)["conversationId"], conv_id)

    def test_a_left_dm_is_never_reused_by_a_recreate(self):
        first = self._post("/api/chat/conversations", self.tomas_token,
                           {"participantIds": [self.ona.id]})
        conv_id = json.loads(first.content)["conversationId"]
        bearer(self.client.delete, f"/api/chat/conversations/{conv_id}", self.ona_token)

        # The abandoned room dropped its pair claim — either
        # member's recreate starts a FRESH room, never a 200
        # onto a conversation the leaver already walked out of
        again = self._post("/api/chat/conversations", self.ona_token,
                           {"participantIds": [self.tomas.id]})
        self.assertEqual(again.status_code, 201)
        self.assertNotEqual(json.loads(again.content)["conversationId"], conv_id)

    def test_a_planted_multi_member_direct_is_never_reused_as_a_dm(self):
        third = create_user(username="trecias")
        planted = create_room([self.tomas, self.ona, third], conv_type="direct")
        response = self._post("/api/chat/conversations", self.tomas_token,
                              {"participantIds": [self.ona.id]})
        self.assertEqual(response.status_code, 201)
        self.assertNotEqual(json.loads(response.content)["conversationId"], planted.id)

    def test_a_direct_chat_stores_no_attacker_chosen_title(self):
        response = self._post("/api/chat/conversations", self.tomas_token,
                              {"participantIds": [self.ona.id], "title": "Dekanas"})
        conv_id = json.loads(response.content)["conversationId"]
        self.assertIsNone(Conversation.objects.get(id=conv_id).title)

    def test_the_create_gates(self):
        cases = (
            ({"participantIds": [], "type": "group", "title": "X"}, 400),
            ({"participantIds": [self.ona.id], "type": "group"}, 400),        # no title
            ({"participantIds": [self.ona.id], "type": "group", "title": "  "}, 400),
            ({"participantIds": [self.tomas.id]}, 400),                       # self only
            ({"participantIds": [self.ona.id, "nera-tokio"]}, 400),           # unknown id
            ({"participantIds": [self.ona.id], "type": "bogus"}, 400),
        )
        for body, expected in cases:
            self.assertEqual(self._post("/api/chat/conversations", self.tomas_token, body).status_code,
                             expected, body)
        # A deactivated account cannot be dragged into a room
        sleeper = create_user(username="miegantis", active=0)
        response = self._post("/api/chat/conversations", self.tomas_token,
                              {"participantIds": [sleeper.id]})
        self.assertEqual(response.status_code, 400)

    def test_a_blocked_pair_answers_one_flat_403(self):
        import uuid
        from knfapp.social.models import UserBlock
        UserBlock.objects.create(blocker=self.ona, blocked=self.tomas, created_at=naive_now())
        response = self._post("/api/chat/conversations", self.tomas_token,
                              {"participantIds": [self.ona.id]})
        self.assertEqual(response.status_code, 403)
        # The body must not say who blocked whom
        self.assertNotIn("block", json.loads(response.content)["error"].lower())

    def test_a_group_opens_with_its_own_narration(self):
        response = self._post("/api/chat/conversations", self.tomas_token,
                              {"participantIds": [self.ona.id], "type": "group", "title": "Kursas"})
        conv_id = json.loads(response.content)["conversationId"]
        system = Message.objects.get(conversation_id=conv_id)
        self.assertEqual(system.kind, "system")
        self.assertIn("sukūrė grupę", system.text)


class LeaveConversationTests(ChatTestCase):

    def test_the_leaver_takes_their_ghost_read_state_along(self):
        third = create_user(username="trecias")
        room = create_room([self.tomas, self.ona, third], conv_type="group", title="Kursas")
        msg = create_message(room, self.ona)
        MessageRead.objects.create(message=msg, user=self.tomas, read_at=naive_now())
        MessageReaction.objects.create(message=msg, user=self.tomas, emoji="❤️", created_at=naive_now())

        response = bearer(self.client.delete, f"/api/chat/conversations/{room.id}", self.tomas_token)
        self.assertEqual(response.status_code, 200)
        # The remaining members' read/status math counts no ghost
        self.assertEqual(MessageRead.objects.filter(user_id=self.tomas.id).count(), 0)
        self.assertEqual(MessageReaction.objects.filter(user_id=self.tomas.id).count(), 0)
        # …the history stays, narrated
        self.assertTrue(Message.objects.filter(conversation_id=room.id, kind="system",
                                               text__contains="paliko pokalbį").exists())

    def test_the_last_leaver_purges_the_room_entirely(self):
        room = create_room([self.tomas, self.ona])
        create_message(room, self.ona)
        bearer(self.client.delete, f"/api/chat/conversations/{room.id}", self.ona_token)
        bearer(self.client.delete, f"/api/chat/conversations/{room.id}", self.tomas_token)
        self.assertEqual(Conversation.objects.filter(id=room.id).count(), 0)
        self.assertEqual(Message.objects.filter(conversation_id=room.id).count(), 0)

    def test_the_gates(self):
        room = create_room([self.tomas, self.ona])
        outsider = create_user(username="pasalinis")
        outsider_token = auth.mint_session(outsider.id)
        self.assertEqual(bearer(self.client.delete, f"/api/chat/conversations/{room.id}",
                                outsider_token).status_code, 403)
        self.assertEqual(bearer(self.client.delete, "/api/chat/conversations/nera",
                                self.tomas_token).status_code, 404)


class TogglePinTests(ChatTestCase):

    def test_the_pin_is_per_user_and_sorts_first(self):
        room_a = create_room([self.tomas, self.ona])
        room_b = create_room([self.tomas, create_user(username="kitas")])
        # room_b is newer activity, but Tomas pins room_a
        Conversation.objects.filter(id=room_b.id).update(updated_at=naive_now())
        response = bearer(self.client.put, f"/api/chat/conversations/{room_a.id}/pin", self.tomas_token)
        self.assertTrue(json.loads(response.content)["pinned"])

        rows = json.loads(bearer(self.client.get, "/api/chat/conversations", self.tomas_token).content)["conversations"]
        self.assertEqual(rows[0]["id"], room_a.id)
        # Ona's view is untouched — pins are the caller's own
        self.assertEqual(ConversationParticipant.objects.get(
            conversation_id=room_a.id, user_id=self.ona.id).pinned, 0)

    def test_a_non_member_gets_403_from_the_atomic_flip(self):
        room = create_room([self.ona, create_user(username="kitas")])
        response = bearer(self.client.put, f"/api/chat/conversations/{room.id}/pin", self.tomas_token)
        self.assertEqual(response.status_code, 403)
