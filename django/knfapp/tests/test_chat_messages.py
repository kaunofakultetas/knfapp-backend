############################################################
#  [*] Regression tests — chat messages
#
#  The send gates (the beacon guard on imageUrl in both its
#  falsy-non-string and foreign-host shapes, cross-room
#  quotes, and the two stable codes the mobile engine
#  triages on — text_too_long, quote_not_found), the
#  idempotent replay that keeps a retry from
#  becoming a duplicate, the unsend's blanking + blob
#  cleanup + silent repeat (and the cleanup's owner rule:
#  a forwarded copy of somebody else's photo never takes
#  their file, a photo still shown elsewhere waits for its
#  last message — on unsend and on the expiry sweep alike),
#  the edit rules, the reaction
#  allowlist with its replace-not-accumulate write, the
#  composite paging cursor that equal stamps cannot defeat,
#  the own-message status ladder, disappearing messages (the
#  TTL stamp, the sweep, and the three surfaces that read
#  WITHOUT a sweep — the preview, the pin banner, the search
#  — keeping a lapsed row out on their own), and the search
#  escapes.
############################################################


import json
import os
import shutil
import tempfile
import uuid


from django.test import Client, TestCase


from knfapp.chat.models import Message, MessageReaction
from knfapp.common import ratelimit
from knfapp.uploads import storage
from knfapp.uploads.models import Upload
from knfapp.users import auth
from knfapp.common.timestamps import utc_now_iso
from knfapp.common.timestamps import as_naive_utc
from .utils import bearer, create_message, create_room, create_user, naive_now, register_upload


class ChatMessageTestCase(TestCase):

    def setUp(self):
        ratelimit.reset()
        from knfapp.chat import events
        events.reset_socket_state()
        self.tomas = create_user(username="tomas")
        self.ona = create_user(username="ona")
        self.tomas_token = auth.mint_session(self.tomas.id)
        self.ona_token = auth.mint_session(self.ona.id)
        self.room = create_room([self.tomas, self.ona])
        self.client = Client()

    def _send(self, token=None, room=None, **body):
        return bearer(self.client.post,
                      f"/api/chat/conversations/{(room or self.room).id}/messages",
                      token or self.tomas_token,
                      data=json.dumps(body), content_type="application/json")


class SendGateTests(ChatMessageTestCase):

    def test_the_image_beacon_guard_in_both_its_shapes(self):
        # A falsy non-string must not skip validation whole
        for bad in ([], {}, 0, False):
            self.assertEqual(self._send(text="x", imageUrl=bad).status_code, 400, repr(bad))
        # A foreign host must never reach a reader's client
        self.assertEqual(self._send(imageUrl="https://evil.example/a.jpg").status_code, 400)
        # The two own-origin families pass
        self.assertEqual(self._send(imageUrl="/api/uploads/" + "a" * 32 + ".jpg").status_code, 201)
        self.assertEqual(self._send(imageUrl="/api/memes/file/memas.jpg").status_code, 201)

    def test_an_empty_message_and_an_outsider_are_refused(self):
        self.assertEqual(self._send().status_code, 400)
        outsider = create_user(username="pasalinis")
        outsider_token = auth.mint_session(outsider.id)
        self.assertEqual(self._send(token=outsider_token, text="x").status_code, 403)

    def test_a_quote_must_live_in_this_very_conversation(self):
        other_room = create_room([self.tomas, create_user(username="kitas")])
        foreign = create_message(other_room, self.tomas)
        response = self._send(text="atsakymas", replyToId=foreign.id)
        self.assertEqual((response.status_code, response.json()["code"]), (400, "quote_not_found"))
        self.assertEqual(self._send(text="x", replyToId="  ").status_code, 400)

        own = create_message(self.room, self.ona, text="Klausimas")
        response = self._send(text="Atsakymas", replyToId=own.id)
        self.assertEqual(response.status_code, 201)
        quote = json.loads(response.content)["message"]["replyTo"]
        self.assertEqual((quote["senderName"], quote["text"]), ("Ona", "Klausimas"))

    def test_the_length_cap_and_the_quote_miss_carry_their_slugs(self):
        # The mobile engine triages on serverCode: exactly these two
        # names mean "too long" and "the quoted message is gone"
        response = self._send(text="a" * 5001)
        self.assertEqual((response.status_code, response.json()["code"]), (400, "text_too_long"))
        response = self._send(text="x", replyToId=str(uuid.uuid4()))
        self.assertEqual((response.status_code, response.json()["code"]), (400, "quote_not_found"))
        # The same cap, the same name, on an edit
        own = create_message(self.room, self.tomas)
        response = bearer(self.client.put, f"/api/chat/conversations/{self.room.id}/messages/{own.id}",
                          self.tomas_token, data=json.dumps({"text": "a" * 5001}),
                          content_type="application/json")
        self.assertEqual((response.status_code, response.json()["code"]), (400, "text_too_long"))

    def test_a_gallery_rides_alone(self):
        item = {"url": "/api/uploads/" + "b" * 32 + ".jpg"}
        self.assertEqual(self._send(gallery=[item]).status_code, 400)  # 1 photo is no gallery
        self.assertEqual(self._send(gallery=[item, item], imageUrl="/api/uploads/" + "c" * 32 + ".jpg").status_code, 400)
        self.assertEqual(self._send(gallery=[item, dict(item, width=100)]).status_code, 201)

    def test_the_direct_send_between_a_blocked_pair_is_refused(self):
        from knfapp.social.models import UserBlock
        UserBlock.objects.create(blocker=self.ona, blocked=self.tomas, created_at=naive_now())
        self.assertEqual(self._send(text="labas").status_code, 403)


class IdempotentSendTests(ChatMessageTestCase):

    def test_a_retry_answers_the_committed_row_not_a_duplicate(self):
        first = self._send(text="Vienintelė", client_msg_id="nonce-1")
        self.assertEqual(first.status_code, 201)
        msg_id = json.loads(first.content)["message"]["id"]

        again = self._send(text="Vienintelė", client_msg_id="nonce-1")
        self.assertEqual(again.status_code, 200)
        self.assertEqual(json.loads(again.content)["message"]["id"], msg_id)
        self.assertEqual(Message.objects.filter(conversation_id=self.room.id, kind="text").count(), 1)

    def test_the_sender_never_counts_their_own_message_as_unread(self):
        self._send(text="Mano")
        total = bearer(self.client.get, "/api/chat/unread-count", self.tomas_token)
        self.assertEqual(json.loads(total.content)["unreadCount"], 0)
        # …while the other side counts one
        total = bearer(self.client.get, "/api/chat/unread-count", self.ona_token)
        self.assertEqual(json.loads(total.content)["unreadCount"], 1)


class UnsendTests(ChatMessageTestCase):

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="knfapp-chat-")
        storage._upload_dir = self.tmp
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.addCleanup(lambda: setattr(storage, "_upload_dir", None))

    def test_the_unsend_blanks_drops_reactions_and_removes_the_blob(self):
        filename = f"{uuid.uuid4().hex}.jpg"
        open(os.path.join(self.tmp, filename), "wb").write(b"bytes")
        Upload.objects.create(id=str(uuid.uuid4()), filename=filename, user_id=self.tomas.id,
                              byte_size=5, created_at=utc_now_iso())

        sent = self._send(text="Nuotrauka", imageUrl=f"/api/uploads/{filename}")
        msg_id = json.loads(sent.content)["message"]["id"]
        MessageReaction.objects.create(message_id=msg_id, user_id=self.ona.id,
                                       emoji="❤️", created_at=naive_now())

        response = bearer(self.client.delete,
                          f"/api/chat/conversations/{self.room.id}/messages/{msg_id}", self.tomas_token)
        self.assertEqual(response.status_code, 200)

        row = Message.objects.get(id=msg_id)
        self.assertEqual((row.text, row.image_url), ("", None))
        self.assertIsNotNone(row.deleted_at)
        self.assertEqual(MessageReaction.objects.filter(message_id=msg_id).count(), 0)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, filename)))
        # A repeat unsend is a silent 200 — never a second broadcast
        self.assertEqual(bearer(self.client.delete,
                                f"/api/chat/conversations/{self.room.id}/messages/{msg_id}",
                                self.tomas_token).status_code, 200)

    def test_only_the_sender_can_unsend(self):
        msg = create_message(self.room, self.tomas)
        response = bearer(self.client.delete,
                          f"/api/chat/conversations/{self.room.id}/messages/{msg.id}", self.ona_token)
        self.assertEqual(response.status_code, 403)

    def _unsend(self, msg_id, token=None):
        return bearer(self.client.delete,
                      f"/api/chat/conversations/{self.room.id}/messages/{msg_id}",
                      token or self.tomas_token)

    def test_unsending_someone_elses_photo_keeps_their_file_and_row(self):
        # Ona's registered photo, sent by Tomas (a member — the send
        # accepts any local path, a forward would carry exactly
        # this) and unsent by him: her file and her row survive
        filename = register_upload(self.tmp, self.ona)
        sent = self._send(text="Svetima", imageUrl=f"/api/uploads/{filename}")
        self.assertEqual(sent.status_code, 201)
        msg_id = json.loads(sent.content)["message"]["id"]

        self.assertEqual(self._unsend(msg_id).status_code, 200)
        self.assertIsNotNone(Message.objects.get(id=msg_id).deleted_at)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, filename)))
        self.assertTrue(Upload.objects.filter(filename=filename, user_id=self.ona.id).exists())

    def test_a_photo_sent_twice_goes_with_its_last_message(self):
        # The forward shape: the same own upload in two messages —
        # the first unsend keeps the file, the second takes it
        filename = register_upload(self.tmp, self.tomas)
        first = json.loads(self._send(text="Pirma", imageUrl=f"/api/uploads/{filename}").content)["message"]["id"]
        second = json.loads(self._send(text="Antra", imageUrl=f"/api/uploads/{filename}").content)["message"]["id"]

        self.assertEqual(self._unsend(first).status_code, 200)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, filename)))
        self.assertTrue(Upload.objects.filter(filename=filename).exists())

        self.assertEqual(self._unsend(second).status_code, 200)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, filename)))
        self.assertFalse(Upload.objects.filter(filename=filename).exists())

    def test_the_expiry_sweep_takes_the_senders_own_photo_and_keeps_a_foreign_one(self):
        own = register_upload(self.tmp, self.ona)
        foreign = register_upload(self.tmp, self.tomas)
        mine = create_message(self.room, self.ona, text="Nyksta", image_url=f"/api/uploads/{own}",
                              expires_at=naive_now(minutes_ago=1))
        theirs = create_message(self.room, self.ona, text="Nyksta svetima", image_url=f"/api/uploads/{foreign}",
                                expires_at=naive_now(minutes_ago=1))

        # The page read triggers the sweep; the files go on the commit
        with self.captureOnCommitCallbacks(execute=True):
            page = bearer(self.client.get, f"/api/chat/conversations/{self.room.id}/messages", self.tomas_token)
            self.assertEqual(page.status_code, 200)

        self.assertEqual(Message.objects.filter(id__in=[mine.id, theirs.id]).count(), 0)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, own)))
        self.assertFalse(Upload.objects.filter(filename=own).exists())
        self.assertTrue(os.path.exists(os.path.join(self.tmp, foreign)))
        self.assertTrue(Upload.objects.filter(filename=foreign, user_id=self.tomas.id).exists())


class EditTests(ChatMessageTestCase):

    def _edit(self, msg_id, text, token=None):
        return bearer(self.client.put,
                      f"/api/chat/conversations/{self.room.id}/messages/{msg_id}",
                      token or self.tomas_token,
                      data=json.dumps({"text": text}), content_type="application/json")

    def test_the_edit_rules(self):
        msg = create_message(self.room, self.tomas, text="Pirmas")
        self.assertEqual(self._edit(msg.id, "Antras").status_code, 200)
        self.assertIsNotNone(Message.objects.get(id=msg.id).edited_at)

        self.assertEqual(self._edit(msg.id, "Svetimas", token=self.ona_token).status_code, 403)

        unsent = create_message(self.room, self.tomas, deleted_at=naive_now())
        self.assertEqual(self._edit(unsent.id, "Prikeltas").status_code, 409)

        file_row = create_message(self.room, self.tomas, kind="file",
                                  attachment_url="/api/uploads/" + "d" * 32 + ".pdf",
                                  attachment_name="d.pdf")
        self.assertEqual(self._edit(file_row.id, "Pavadinimas").status_code, 400)

    def test_the_change_feed_carries_the_edit(self):
        # The naive wire shape the app sends (no offset — a "+00:00"
        # in a bare query string would decode as a space anyway)
        cursor = as_naive_utc(naive_now()).isoformat()
        msg = create_message(self.room, self.tomas, text="Pirmas")
        self._edit(msg.id, "Antras")
        response = bearer(self.client.get,
                          f"/api/chat/conversations/{self.room.id}/changes?since={cursor}",
                          self.ona_token)
        changed = json.loads(response.content)["messages"]
        self.assertEqual([m["text"] for m in changed], ["Antras"])
        self.assertEqual(bearer(self.client.get,
                                f"/api/chat/conversations/{self.room.id}/changes?since=rytoj",
                                self.ona_token).status_code, 400)


class ReactionTests(ChatMessageTestCase):

    def _react(self, msg_id, emoji, token=None):
        return bearer(self.client.post,
                      f"/api/chat/conversations/{self.room.id}/messages/{msg_id}/react",
                      token or self.ona_token,
                      data=json.dumps({"emoji": emoji}), content_type="application/json")

    def test_one_emoji_per_user_replace_never_accumulate(self):
        msg = create_message(self.room, self.tomas)
        self.assertEqual(self._react(msg.id, "❤️").status_code, 200)
        response = self._react(msg.id, "\U0001F44D")
        reactions = json.loads(response.content)["reactions"]
        self.assertEqual(len(reactions), 1)
        self.assertEqual(reactions[0]["emoji"], "\U0001F44D")
        # The broadcast shape carries no bySelf — many clients read it
        self.assertNotIn("bySelf", reactions[0])
        # …while the page shape does
        page = json.loads(bearer(self.client.get,
                                 f"/api/chat/conversations/{self.room.id}/messages",
                                 self.ona_token).content)
        self.assertTrue(page["messages"][-1]["reactions"][0]["bySelf"])

    def test_the_allowlist_and_the_unsent_gate(self):
        msg = create_message(self.room, self.tomas)
        self.assertEqual(self._react(msg.id, "\U0001F921").status_code, 400)  # not in the picker
        unsent = create_message(self.room, self.tomas, deleted_at=naive_now())
        self.assertEqual(self._react(unsent.id, "❤️").status_code, 404)


class PagingTests(ChatMessageTestCase):

    def test_equal_stamps_cannot_defeat_the_composite_cursor(self):
        stamp = naive_now(minutes_ago=5)
        ids = sorted(str(uuid.uuid4()) for _ in range(3))
        for mid in ids:
            create_message(self.room, self.ona, text=f"m-{mid[:4]}", id=mid, created_at=stamp)

        first = json.loads(bearer(self.client.get,
                                  f"/api/chat/conversations/{self.room.id}/messages?limit=2",
                                  self.tomas_token).content)
        self.assertTrue(first["hasMore"])
        oldest_on_page = first["messages"][0]
        second = json.loads(bearer(
            self.client.get,
            f"/api/chat/conversations/{self.room.id}/messages?limit=2"
            f"&before={oldest_on_page['createdAt']}&before_id={oldest_on_page['id']}",
            self.tomas_token).content)
        walked = {m["id"] for m in first["messages"]} | {m["id"] for m in second["messages"]}
        self.assertEqual(walked, set(ids))  # nobody skipped, nobody repeated
        self.assertFalse(second["hasMore"])  # exact, not "page happened to be full"

    def test_the_own_status_ladder(self):
        third = create_user(username="trecias")
        room = create_room([self.tomas, self.ona, third], conv_type="group", title="Kursas")
        sent = self._send(room=room, text="Visiems")
        msg_id = json.loads(sent.content)["message"]["id"]

        def own_status():
            page = json.loads(bearer(self.client.get,
                                     f"/api/chat/conversations/{room.id}/messages",
                                     self.tomas_token).content)
            return next(m["status"] for m in page["messages"] if m["id"] == msg_id)

        self.assertEqual(own_status(), "sent")
        bearer(self.client.put, f"/api/chat/conversations/{room.id}/read", self.ona_token)
        self.assertEqual(own_status(), "delivered")
        third_token = auth.mint_session(third.id)
        bearer(self.client.put, f"/api/chat/conversations/{room.id}/read", third_token)
        self.assertEqual(own_status(), "read")


class DisappearingTests(ChatMessageTestCase):

    def test_the_ttl_stamps_at_send_time_and_the_sweep_collects(self):
        response = bearer(self.client.put, f"/api/chat/conversations/{self.room.id}/ttl",
                          self.tomas_token,
                          data=json.dumps({"seconds": 60}), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        # The room narrated the change
        self.assertTrue(Message.objects.filter(conversation_id=self.room.id, kind="system",
                                               text__contains="įjungė nykstančias").exists())

        sent = self._send(text="Nyksta")
        self.assertIsNotNone(json.loads(sent.content)["message"]["expiresAt"])

        # An already-overdue row leaves before the next page is read
        expired = create_message(self.room, self.ona, text="Pradingęs",
                                 expires_at=naive_now(minutes_ago=1))
        page = json.loads(bearer(self.client.get,
                                 f"/api/chat/conversations/{self.room.id}/messages",
                                 self.tomas_token).content)
        self.assertNotIn("Pradingęs", [m["text"] for m in page["messages"]])
        self.assertEqual(Message.objects.filter(id=expired.id).count(), 0)

    def test_the_preview_falls_back_to_the_previous_live_message(self):
        # The conversation list never sweeps — its seek must skip
        # the lapsed row INSIDE the LIMIT 1, so the preview is the
        # older live message, not the lapsed body and not blank
        create_message(self.room, self.ona, text="Senas gyvas", minutes_ago=5)
        create_message(self.room, self.ona, text="Naujas dingęs", minutes_ago=2,
                       expires_at=naive_now(minutes_ago=1))
        rows = json.loads(bearer(self.client.get, "/api/chat/conversations",
                                 self.tomas_token).content)["conversations"]
        self.assertEqual(rows[0]["lastMessage"]["text"], "Senas gyvas")

    def test_an_expired_pin_leaves_the_banner_at_once(self):
        # The pin banner is read without a sweep too — the
        # predicate alone keeps a lapsed pin out
        create_message(self.room, self.ona, text="Prisegtas gyvas", minutes_ago=3,
                       pinned_at=naive_now(), pinned_by=self.ona.id)
        create_message(self.room, self.ona, text="Prisegtas dingęs", minutes_ago=2,
                       pinned_at=naive_now(), pinned_by=self.ona.id,
                       expires_at=naive_now(minutes_ago=1))
        pins = json.loads(bearer(self.client.get, f"/api/chat/conversations/{self.room.id}/pins",
                                 self.tomas_token).content)["pins"]
        self.assertEqual([p["text"] for p in pins], ["Prisegtas gyvas"])

    def test_search_never_surfaces_a_lapsed_row_and_a_hit_carries_its_deadline(self):
        create_message(self.room, self.ona, text="Slaptas gyvas", minutes_ago=3,
                       expires_at=naive_now(minutes_ago=-60))
        gone = create_message(self.room, self.ona, text="Slaptas dingęs", minutes_ago=2,
                              expires_at=naive_now(minutes_ago=1))
        response = json.loads(bearer(self.client.get,
                                     f"/api/chat/conversations/{self.room.id}/messages/search?q=Slaptas",
                                     self.tomas_token).content)
        self.assertEqual([m["text"] for m in response["messages"]], ["Slaptas gyvas"])
        self.assertEqual(response["total"], 1)
        # The hit ships its deadline, so the client can drop it the
        # moment it lapses on screen
        self.assertIsNotNone(response["messages"][0]["expiresAt"])
        # ...and the search swept the room on its way in
        self.assertEqual(Message.objects.filter(id=gone.id).count(), 0)

    def test_the_ttl_bounds(self):
        for seconds in (30, 40_000_000, True):
            response = bearer(self.client.put, f"/api/chat/conversations/{self.room.id}/ttl",
                              self.tomas_token,
                              data=json.dumps({"seconds": seconds}), content_type="application/json")
            self.assertEqual(response.status_code, 400, seconds)


class SearchTests(ChatMessageTestCase):

    def test_like_wildcards_match_literally(self):
        # The search needle's % must match the CHARACTER, never
        # become a wildcard that also answers the 100x row
        create_message(self.room, self.ona, text="Pasiekta 100% tikslo")
        create_message(self.room, self.ona, text="Pasiekta 100x tikslo")
        response = json.loads(bearer(self.client.get,
                                     f"/api/chat/conversations/{self.room.id}/messages/search?q=100%25",
                                     self.tomas_token).content)
        self.assertEqual([m["text"] for m in response["messages"]], ["Pasiekta 100% tikslo"])

    def test_a_nul_needle_is_never_the_whole_room(self):
        # clean_param drops the control bytes before the length is
        # judged: a NUL-only needle is the blank-q 400, never the
        # bare '%' a NUL-terminated bind would have made of it
        create_message(self.room, self.ona, text="Slaptas tekstas")
        response = bearer(self.client.get,
                          f"/api/chat/conversations/{self.room.id}/messages/search?q=%00%00",
                          self.tomas_token)
        self.assertEqual(response.status_code, 400)

    def test_unsent_rows_never_surface(self):
        create_message(self.room, self.ona, text="Randamas")
        create_message(self.room, self.ona, text="Randamas dingęs", deleted_at=naive_now())
        response = json.loads(bearer(self.client.get,
                                     f"/api/chat/conversations/{self.room.id}/messages/search?q=Randamas",
                                     self.tomas_token).content)
        self.assertEqual(response["total"], 1)
