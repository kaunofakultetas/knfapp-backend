############################################################
#  [*] Regression tests — the chat wire's second pass
#
#  The change feed shipping REAL receipts, reactions and the
#  own-message status ladder (it once hard-coded "read", []
#  and [], so a resync flipped an unread own message to the
#  read tick and wiped reactions off the screen), both change
#  cursors stamped BEFORE the rows are read (a write that
#  commits mid-read must land in the next feed, never between
#  the two), system rows carrying their event so every client
#  words them in its own language (the Lithuanian prose is
#  only the fallback), the last member's purge taking the
#  room's files with it, and the chat push wording every
#  line the backend composes in English too.
############################################################


import json
import os
import shutil
import tempfile
from datetime import timedelta
from unittest.mock import patch


from django.test import Client, TestCase


from knfapp.chat import events
from knfapp.chat.api import views
from knfapp.chat.models import Conversation, Message, MessageReaction, MessageRead
from knfapp.common import ratelimit
from knfapp.common.timestamps import as_naive_utc, utc_now
from knfapp.uploads import storage
from knfapp.uploads.models import Upload
from knfapp.users import auth
from .utils import bearer, create_message, create_room, create_user, naive_now, register_upload


class ChatWireTestCase(TestCase):

    def setUp(self):
        ratelimit.reset()
        events.reset_socket_state()
        self.tomas = create_user(username="tomas")
        self.ona = create_user(username="ona")
        self.tomas_token = auth.mint_session(self.tomas.id)
        self.ona_token = auth.mint_session(self.ona.id)
        self.room = create_room([self.tomas, self.ona])
        self.client = Client()

    def _changes(self, since, token=None, room=None):
        return bearer(self.client.get,
                      f"/api/chat/conversations/{(room or self.room).id}/changes?since={since}",
                      token or self.tomas_token)

    def _page(self, token=None, room=None):
        return bearer(self.client.get,
                      f"/api/chat/conversations/{(room or self.room).id}/messages",
                      token or self.tomas_token)








############################################################
# ChangeFeedShapeTests
############################################################
#
# KNF-057 / KNF-093: an edited row in the feed must describe
# the truth the history page describes — nobody read it, so
# the sender's own row stays "sent"; a real receipt and a
# real reaction come back as they are.
############################################################

class ChangeFeedShapeTests(ChatWireTestCase):

    def test_an_unread_edited_own_message_stays_sent(self):
        since = as_naive_utc(naive_now(minutes_ago=5)).isoformat()
        msg = create_message(self.room, self.tomas, text="labas", minutes_ago=2)
        MessageRead.objects.create(message_id=msg.id, user_id=self.tomas.id, read_at=naive_now(2))
        Message.objects.filter(id=msg.id).update(text="labas!", edited_at=utc_now())

        row = json.loads(self._changes(since).content)["messages"][0]
        self.assertEqual(row["text"], "labas!")
        self.assertEqual(row["status"], "sent")
        self.assertEqual(row["readBy"], [self.tomas.id])

    def test_real_receipts_and_reactions_survive_the_feed(self):
        since = as_naive_utc(naive_now(minutes_ago=5)).isoformat()
        msg = create_message(self.room, self.tomas, text="labas", minutes_ago=2)
        MessageRead.objects.create(message_id=msg.id, user_id=self.ona.id, read_at=naive_now(1))
        MessageReaction.objects.create(message_id=msg.id, user_id=self.ona.id, emoji="\U0001F44D",
                                       created_at=naive_now(1))
        Message.objects.filter(id=msg.id).update(text="labas!", edited_at=utc_now())

        # The sender's view: read by the only other member
        mine = json.loads(self._changes(since).content)["messages"][0]
        self.assertEqual(mine["status"], "read")
        self.assertEqual(mine["readBy"], [self.ona.id])
        self.assertEqual(mine["reactions"], [{"emoji": "\U0001F44D", "count": 1, "bySelf": False,
                                              "byUserIds": [self.ona.id]}])

        # The reader's view: bySelf is theirs, the status fixed
        theirs = json.loads(self._changes(since, token=self.ona_token).content)["messages"][0]
        self.assertTrue(theirs["reactions"][0]["bySelf"])
        self.assertFalse(theirs["isOwn"])
        self.assertEqual(theirs["status"], "read")

    def test_an_unsent_row_ships_blank_and_flagged(self):
        since = as_naive_utc(naive_now(minutes_ago=5)).isoformat()
        msg = create_message(self.room, self.tomas, text="slaptas", minutes_ago=2)
        Message.objects.filter(id=msg.id).update(text="", deleted_at=utc_now())
        row = json.loads(self._changes(since).content)["messages"][0]
        self.assertTrue(row["deleted"])
        self.assertEqual(row["text"], "")








############################################################
# CursorTests
############################################################
#
# KNF-115: the cursor is read BEFORE the page and backdated,
# so an edit stamped just before the cursor but committed
# after the page's SELECT still reaches the next feed.
############################################################

class CursorTests(ChatWireTestCase):

    def test_the_page_cursor_is_stamped_before_the_read_and_backdated(self):
        frozen = utc_now()
        with patch.object(views, "utc_now", return_value=frozen):
            body = json.loads(self._page().content)
        cursor = body["cursor"]
        expected = as_naive_utc(frozen - views._CHANGES_CURSOR_SLACK).isoformat()
        self.assertEqual(cursor, expected)

    def test_an_edit_committed_mid_read_reaches_the_next_feed(self):
        msg = create_message(self.room, self.ona, text="originalus tekstas", minutes_ago=3)
        read_started = utc_now()
        with patch.object(views, "utc_now", return_value=read_started):
            cursor = json.loads(self._page().content)["cursor"]
        # The racing edit: stamped a moment BEFORE the page's
        # clock read, committed after its rows were served
        Message.objects.filter(id=msg.id).update(
            text="PAKEISTA", edited_at=read_started - timedelta(milliseconds=5),
        )
        changed = json.loads(self._changes(cursor).content)["messages"]
        self.assertEqual([m["text"] for m in changed], ["PAKEISTA"])

    def test_a_repeat_from_the_new_cursor_may_redeliver_but_never_invents(self):
        since = as_naive_utc(naive_now(minutes_ago=5)).isoformat()
        msg = create_message(self.room, self.ona, text="a", minutes_ago=3)
        Message.objects.filter(id=msg.id).update(text="b", edited_at=utc_now())
        first = json.loads(self._changes(since).content)
        second = json.loads(self._changes(first["cursor"]).content)
        # Inside the slack the same change may come back —
        # idempotent for the client — but nothing new appears
        self.assertTrue({m["id"] for m in second["messages"]} <= {msg.id})








############################################################
# SystemEventTests
############################################################
#
# KNF-126: every narrated room event carries a code plus
# parameters the client words itself; the prose stays as the
# fallback. The media frame never leaks onto a system row.
############################################################

class SystemEventTests(ChatWireTestCase):

    def test_a_group_opens_with_its_event(self):
        response = bearer(self.client.post, "/api/chat/conversations", self.tomas_token,
                          data=json.dumps({"participantIds": [self.ona.id], "type": "group",
                                           "title": "  Programų sistemos  "}),
                          content_type="application/json")
        conv_id = json.loads(response.content)["conversationId"]
        room = Conversation.objects.get(id=conv_id)
        page = json.loads(self._page(room=room).content)
        line = page["messages"][0]
        self.assertEqual(line["kind"], "system")
        self.assertEqual(line["system"], {"event": "group_created", "title": "Programų sistemos"})
        self.assertIsNone(line["media"])
        self.assertIn("sukūrė grupę", line["text"])

        # The conversation list previews it through the event too
        rows = json.loads(bearer(self.client.get, "/api/chat/conversations", self.ona_token).content)
        preview = [c for c in rows["conversations"] if c["id"] == conv_id][0]["lastMessage"]
        self.assertEqual(preview["system"], {"event": "group_created", "title": "Programų sistemos"})

    def test_the_ttl_narration_carries_seconds_not_a_worded_window(self):
        response = bearer(self.client.put, f"/api/chat/conversations/{self.room.id}/ttl", self.tomas_token,
                          data=json.dumps({"seconds": 3600}), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        bearer(self.client.put, f"/api/chat/conversations/{self.room.id}/ttl", self.tomas_token,
               data=json.dumps({"seconds": 0}), content_type="application/json")
        events_seen = [m["system"] for m in json.loads(self._page().content)["messages"] if m["kind"] == "system"]
        self.assertEqual(events_seen, [{"event": "ttl_on", "seconds": 3600}, {"event": "ttl_off"}])

    def test_setting_the_window_the_room_already_has_narrates_nothing(self):
        # Re-picking the active row once posted a second "turned
        # it off" line for every member and bumped the room
        put = lambda seconds: bearer(self.client.put, f"/api/chat/conversations/{self.room.id}/ttl", self.tomas_token,
                                     data=json.dumps({"seconds": seconds}), content_type="application/json")
        off_again = put(0)
        self.assertEqual((off_again.status_code, json.loads(off_again.content)), (200, {"messageTtlSeconds": None}))
        self.assertFalse(Message.objects.filter(conversation_id=self.room.id, kind="system").exists())
        put(3600)
        again = put(3600)
        self.assertEqual(json.loads(again.content), {"messageTtlSeconds": 3600})
        events_seen = [m["system"] for m in json.loads(self._page().content)["messages"] if m["kind"] == "system"]
        self.assertEqual(events_seen, [{"event": "ttl_on", "seconds": 3600}])

    def test_a_leave_is_narrated_with_its_event(self):
        vida = create_user(username="vida")
        group = create_room([self.tomas, self.ona, vida], conv_type="group", title="Trise")
        leave = bearer(self.client.delete, f"/api/chat/conversations/{group.id}", auth.mint_session(vida.id))
        self.assertEqual(leave.status_code, 200)
        line = json.loads(self._page(room=group).content)["messages"][-1]
        self.assertEqual((line["kind"], line["system"]), ("system", {"event": "left"}))

    def test_a_new_group_opens_with_nothing_unread_for_anyone(self):
        # The creator's opening line once carried a LATER stamp than
        # the members' fresh watermarks: every invitee started at 1
        response = bearer(self.client.post, "/api/chat/conversations", self.tomas_token,
                          data=json.dumps({"participantIds": [self.ona.id], "type": "group", "title": "Nauja"}),
                          content_type="application/json")
        conv_id = json.loads(response.content)["conversationId"]
        rows = json.loads(bearer(self.client.get, "/api/chat/conversations", self.ona_token).content)
        row = [c for c in rows["conversations"] if c["id"] == conv_id][0]
        self.assertEqual(row["unreadCount"], 0)
        total = json.loads(bearer(self.client.get, "/api/chat/unread-count", self.ona_token).content)
        self.assertEqual(total["unreadCount"], 0)
        line = Message.objects.get(conversation_id=conv_id, kind="system")
        from knfapp.chat.models import ConversationParticipant
        watermark = ConversationParticipant.objects.get(conversation_id=conv_id, user_id=self.ona.id).last_read_at
        self.assertEqual(line.created_at, watermark)

    def test_a_system_line_is_never_unread(self):
        # Somebody else toggling the timer narrates the room — not
        # a message for anybody to read
        bearer(self.client.put, f"/api/chat/conversations/{self.room.id}/ttl", self.tomas_token,
               data=json.dumps({"seconds": 3600}), content_type="application/json")
        create_message(self.room, self.tomas, text="tikra žinutė")
        rows = json.loads(bearer(self.client.get, "/api/chat/conversations", self.ona_token).content)
        self.assertEqual(rows["conversations"][0]["unreadCount"], 1)
        total = json.loads(bearer(self.client.get, "/api/chat/unread-count", self.ona_token).content)
        self.assertEqual(total["unreadCount"], 1)

    def test_an_ordinary_row_carries_no_event(self):
        create_message(self.room, self.ona, text="labas")
        row = json.loads(self._page().content)["messages"][0]
        self.assertIsNone(row["system"])

    def test_a_legacy_prose_row_falls_back_to_its_text(self):
        # Written before events existed: no meta, the prose only
        create_message(self.room, self.ona, text="Ona paliko pokalbį", kind="system")
        row = json.loads(self._page().content)["messages"][0]
        self.assertIsNone(row["system"])
        self.assertEqual(row["text"], "Ona paliko pokalbį")








############################################################
# LastMemberPurgeTests
############################################################
#
# KNF-055: the last leave destroys the room AND the files its
# messages held — each as its sender, so a forwarded copy of
# somebody else's photo (still theirs) survives.
############################################################

class LastMemberPurgeTests(ChatWireTestCase):

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="knfapp-purge-")
        storage._upload_dir = self.tmp
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.addCleanup(lambda: setattr(storage, "_upload_dir", None))

    def _leave(self, token):
        return bearer(self.client.delete, f"/api/chat/conversations/{self.room.id}", token)

    def test_the_purge_takes_every_slot_as_its_sender(self):
        photo = register_upload(self.tmp, self.tomas)
        pdf = register_upload(self.tmp, self.ona, ext="pdf")
        poster = register_upload(self.tmp, self.tomas)
        album = register_upload(self.tmp, self.ona)
        foreign = register_upload(self.tmp, create_user(username="kitas"))
        create_message(self.room, self.tomas, image_url=f"/api/uploads/{photo}")
        create_message(self.room, self.ona, kind="file", attachment_url=f"/api/uploads/{pdf}",
                       attachment_name="a.pdf")
        create_message(self.room, self.tomas, kind="video", attachment_url=f"/api/uploads/{poster}",
                       attachment_name="v.mp4", attachment_meta={"thumbnailUrl": f"/api/uploads/{poster}"})
        create_message(self.room, self.ona, kind="image",
                       gallery=[{"url": f"/api/uploads/{album}"}, {"url": f"/api/uploads/{photo}"}])
        # A forward of a third person's photo: never Tomas's to delete
        create_message(self.room, self.tomas, image_url=f"/api/uploads/{foreign}")

        self.assertEqual(self._leave(self.ona_token).status_code, 200)
        # Somebody is still in: nothing may go yet
        for name in (photo, pdf, poster, album, foreign):
            self.assertTrue(os.path.exists(os.path.join(self.tmp, name)), name)

        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self._leave(self.tomas_token).status_code, 200)

        self.assertFalse(Conversation.objects.filter(id=self.room.id).exists())
        for name in (photo, pdf, poster, album):
            self.assertFalse(os.path.exists(os.path.join(self.tmp, name)), name)
            self.assertFalse(Upload.objects.filter(filename=name).exists(), name)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, foreign)))
        self.assertTrue(Upload.objects.filter(filename=foreign).exists())

    def test_a_file_another_room_still_shows_survives_the_purge(self):
        photo = register_upload(self.tmp, self.tomas)
        create_message(self.room, self.tomas, image_url=f"/api/uploads/{photo}")
        other = create_room([self.tomas, create_user(username="vida")])
        create_message(other, self.tomas, image_url=f"/api/uploads/{photo}")

        self._leave(self.ona_token)
        with self.captureOnCommitCallbacks(execute=True):
            self._leave(self.tomas_token)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, photo)))








############################################################
# PushLanguageTests
############################################################
#
# KNF-071: every push line the backend words itself — the
# media marker, the content-free body, the mention title —
# rides with its English copy for devices registered as
# 'en'; the sender's own text needs none.
############################################################

class PushLanguageTests(ChatWireTestCase):

    def _send(self, **body):
        spawned = []
        with patch.object(views, "_spawn", side_effect=lambda target, *args: spawned.append(args)):
            response = bearer(self.client.post, f"/api/chat/conversations/{self.room.id}/messages",
                              self.tomas_token, data=json.dumps(body), content_type="application/json")
        self.assertEqual(response.status_code, 201)
        return [args for args in spawned if len(args) >= 4 and isinstance(args[0], list)]

    def test_a_voice_note_is_worded_in_both_languages(self):
        pushes = self._send(kind="audio", attachment={"url": "/api/uploads/" + "a" * 32 + ".m4a",
                                                        "name": "v.m4a", "size": 3, "mime": "audio/mp4"})
        _ids, _title, body, data, _title_en, body_en = pushes[0]
        self.assertEqual((body, body_en, data["preview"]), ("Balso žinutė", "Voice message", "audio"))

    def test_a_photo_and_a_file_carry_english_markers(self):
        photo = self._send(imageUrl="/api/uploads/" + "b" * 32 + ".jpg")[0]
        self.assertEqual((photo[2], photo[5]), ("Nuotrauka", "Photo"))
        doc = self._send(attachment={"url": "/api/uploads/" + "c" * 32 + ".pdf", "name": "a.pdf",
                                     "size": 3, "mime": "application/pdf"})[0]
        self.assertEqual((doc[2], doc[5]), ("Failas", "File"))

    def test_the_senders_own_text_needs_no_translation(self):
        pushes = self._send(text="Labas, kaip sekasi?")
        self.assertEqual(pushes[0][2], "Labas, kaip sekasi?")
        self.assertIsNone(pushes[0][5])

    def test_the_content_free_body_and_the_mention_title_have_english_copies(self):
        from knfapp.users.models import User
        User.objects.filter(id=self.ona.id).update(chat_push_preview=0)
        pushes = self._send(text=f"@{self.ona.display_name} žiūrėk")
        _ids, title, body, _data, title_en, body_en = pushes[0]
        self.assertIn("paminėjo jus", title)
        self.assertIn("mentioned you", title_en)
        self.assertEqual((body, body_en), ("Nauja žinutė", "New message"))
