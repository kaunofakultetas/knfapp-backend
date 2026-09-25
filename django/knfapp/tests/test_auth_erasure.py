############################################################
#  [*] Regression tests — self-service erasure and export
#
#  DELETE /api/auth/me is password-confirmed on the change-
#  password failure budget, refuses to take the last active
#  admin down, and runs the shared erasure: the users row
#  survives anonymised (bcrypt-shaped unreachable hash, the
#  Lithuanian marker), authored posts tombstone, fed
#  counters decrement while the rows still exist, uploads
#  leave the disk only once the erasure commits (a write
#  that fails after them rolls the whole account back with
#  the files still on disk), comments stay (they pick the
#  marker up at read time). The chat side scrubs the richer
#  media columns, renames the frozen system narrations,
#  purges the activity feed both ways, and takes any room
#  the departure emptied down whole. The export answers
#  every stored section — devices with a digest instead of
#  the live push credential, sessions as bare stamps — an
#  empty one as [] — never a 500.
############################################################


import json
import os
import shutil
import tempfile
import uuid
from unittest.mock import patch


from django.test import Client, TestCase


from knfapp.chat.models import Conversation, Message
from knfapp.common import ratelimit
from knfapp.common.timestamps import utc_now_iso
from knfapp.news.models import NewsComment, NewsLike, NewsPost
from knfapp.notifications.models import PushToken
from knfapp.social.activity import record_activity
from knfapp.social.models import Activity, Friendship
from knfapp.uploads import storage
from knfapp.uploads.models import Upload
from knfapp.users import auth, erasure
from knfapp.users.models import Session, User
from .utils import PASSWORD, bearer, befriend, create_message, create_post, create_room, create_user


class DeleteMeTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.user = create_user(username="tomas")
        self.token = auth.mint_session(self.user.id)
        self.client = Client()

    def _delete(self, password=PASSWORD, token=None):
        return bearer(self.client.delete, "/api/auth/me", token or self.token,
                      data=json.dumps({"password": password}), content_type="application/json")

    def test_a_wrong_password_spends_the_change_password_budget(self):
        response = self._delete(password="neteisingas")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.content)["code"], "invalid_credentials")
        self.assertTrue(User.objects.get(id=self.user.id).active)
        # Same bucket as change-password: the failures pool
        for _ in range(9):
            self._delete(password="neteisingas")
        self.assertEqual(self._delete().status_code, 429)

    def test_the_erasure_inventory(self):
        tmp = tempfile.mkdtemp(prefix="knfapp-erasure-")
        storage._upload_dir = tmp
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        self.addCleanup(lambda: setattr(storage, "_upload_dir", None))

        # A file on disk, a tombstonable post, a like feeding a
        # counter, a comment that must SURVIVE, a friendship
        filename = f"{uuid.uuid4().hex}.jpg"  # the shape storage writes — the RE gate admits no other
        open(os.path.join(tmp, filename), "wb").write(b"bytes")
        Upload.objects.create(id=str(uuid.uuid4()), filename=filename, user_id=self.user.id,
                              byte_size=5, created_at=utc_now_iso())
        own_post = create_post(author=self.user, source="user", post_type="social",
                               image_url=f"/api/uploads/{filename}")
        other = create_user(username="kitas")
        their_post = create_post(author=other, source="user", post_type="social", likes_count=1)
        NewsLike.objects.create(post_id=their_post.id, user_id=self.user.id, created_at=utc_now_iso())
        NewsComment.objects.create(id=str(uuid.uuid4()), post_id=their_post.id, user_id=self.user.id,
                                   text="Sveikinu", created_at=utc_now_iso())
        befriend(self.user, other)

        # The file goes in the on-commit sweep — executed here,
        # since a TestCase never really commits
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self._delete().status_code, 200)

        row = User.objects.get(id=self.user.id)
        self.assertEqual(row.display_name, "Ištrintas naudotojas")
        self.assertEqual(row.username, f"deleted-{self.user.id}")
        self.assertEqual(row.active, 0)
        self.assertTrue(row.password_hash.startswith("$2"))  # bcrypt-shaped, never '!'
        self.assertIsNone(row.avatar_url)

        self.assertFalse(os.path.exists(os.path.join(tmp, filename)))
        own_post.refresh_from_db()
        self.assertIsNone(own_post.image_url)
        self.assertEqual(own_post.author_name, "Ištrintas naudotojas")

        their_post.refresh_from_db()
        self.assertEqual(their_post.likes_count, 0)
        self.assertEqual(NewsLike.objects.filter(user_id=self.user.id).count(), 0)
        self.assertEqual(NewsComment.objects.filter(user_id=self.user.id).count(), 1)
        self.assertEqual(Friendship.objects.count(), 0)
        self.assertEqual(Session.objects.filter(user_id=self.user.id).count(), 0)
        self.assertEqual(bearer(self.client.get, "/api/auth/me", self.token).status_code, 401)

    def test_a_failed_erasure_keeps_the_files_on_disk(self):
        tmp = tempfile.mkdtemp(prefix="knfapp-erasure-")
        storage._upload_dir = tmp
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        self.addCleanup(lambda: setattr(storage, "_upload_dir", None))

        filename = f"{uuid.uuid4().hex}.jpg"
        open(os.path.join(tmp, filename), "wb").write(b"bytes")
        Upload.objects.create(id=str(uuid.uuid4()), filename=filename, user_id=self.user.id,
                              byte_size=5, created_at=utc_now_iso())

        # hashpw is the erasure's STEP 4 — the rows are already
        # gone when it blows up. The request rolls back to its
        # savepoint, and Django drops the on-commit sweep that
        # was registered inside it, so nothing runs here
        with patch.object(erasure.bcrypt, "hashpw", side_effect=RuntimeError("disk on fire")):
            with self.assertRaises(RuntimeError):
                with self.captureOnCommitCallbacks(execute=True):
                    self._delete()

        # A whole account, not one pointing at 404s
        self.assertTrue(os.path.exists(os.path.join(tmp, filename)))
        self.assertEqual(Upload.objects.filter(user_id=self.user.id).count(), 1)
        self.assertTrue(User.objects.get(id=self.user.id).active)

    def test_the_chat_side_scrubs_media_names_activity_and_orphan_rooms(self):
        other = create_user(username="kitas")

        # A living room: a system narration opening with the
        # actor's name, and a message carrying the richer media
        # columns the unsend path clears
        room = create_room([self.user, other], conv_type="group", title="Komanda")
        narration = create_message(room, self.user, text="Tomas sukūrė grupę „Komanda“", kind="system")
        media = create_message(room, self.user, text="štai failas",
                               attachment_url="/api/uploads/x.pdf", attachment_name="CV Tomas.pdf",
                               attachment_size=9, attachment_mime="application/pdf",
                               attachment_meta={"thumbnailUrl": "/api/uploads/t.jpg"},
                               gallery=[{"url": "/api/uploads/g.jpg"}],
                               link_preview={"imageUrl": "/api/uploads/l.jpg"})

        # A room the departure will EMPTY — its history must
        # not survive a member nobody can ever be again
        orphan = create_room([self.user])
        create_message(orphan, self.user, text="vienas kambaryje")

        # Their private feed and their gesture in another feed
        record_activity(self.user.id, "like", other.id, subject_id="p1", subject_preview="x")
        record_activity(other.id, "like", self.user.id, subject_id="p2", subject_preview="y")

        self.assertEqual(self._delete().status_code, 200)

        # The narration wears the tombstone name; the media
        # columns are gone with the files they pointed at
        narration.refresh_from_db()
        self.assertEqual(narration.text, "Ištrintas naudotojas sukūrė grupę „Komanda“")
        media.refresh_from_db()
        for column in ("attachment_url", "attachment_name", "attachment_size",
                       "attachment_mime", "attachment_meta", "gallery", "link_preview"):
            self.assertIsNone(getattr(media, column), column)

        # The emptied room is purged whole; the living room stays
        self.assertFalse(Conversation.objects.filter(id=orphan.id).exists())
        self.assertFalse(Message.objects.filter(conversation_id=orphan.id).exists())
        self.assertTrue(Conversation.objects.filter(id=room.id).exists())

        # Both activity directions are gone — the feed was
        # theirs, the gesture rows advertised deleted gestures
        self.assertEqual(Activity.objects.count(), 0)

    def test_the_last_active_admin_cannot_delete_themselves(self):
        admin = create_user(username="vadovas", role="admin")
        token = auth.mint_session(admin.id)
        response = self._delete(token=token)
        self.assertEqual(response.status_code, 400)
        self.assertIn("last active admin", json.loads(response.content)["error"])
        # A second active admin unblocks the same request
        create_user(username="pavaduotojas", role="admin")
        self.assertEqual(self._delete(token=token).status_code, 200)


class ExportMeTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.user = create_user(username="tomas")
        self.token = auth.mint_session(self.user.id)
        self.client = Client()

    def _export(self):
        return bearer(self.client.get, "/api/auth/me/export", self.token)

    def test_the_export_carries_every_section_and_no_hash(self):
        create_post(author=self.user, source="user", post_type="social")
        PushToken.objects.create(id=str(uuid.uuid4()), user_id=self.user.id,
                                 token="ExponentPushToken[slaptas]", platform="ios",
                                 created_at=utc_now_iso(), updated_at=utc_now_iso())
        response = self._export()
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)

        for section in ("profile", "posts", "comments", "messages", "conversations", "likes",
                        "pollVotes", "friends", "friendRequests", "blocks", "reports",
                        "notificationChannels", "uploads", "pushTokens", "sessions",
                        "activity", "reactions", "memes"):
            self.assertIn(section, payload)

        self.assertEqual(payload["profile"]["username"], "tomas")
        self.assertNotIn("password_hash", payload["profile"])
        self.assertEqual(len(payload["posts"]), 1)
        # A user with no chat rows gets empty sections, not nulls
        self.assertEqual(payload["messages"], [])
        self.assertEqual(payload["conversations"], [])

        # The device section masks the credential: an 8-hex
        # digest rides, the raw token never does
        device = payload["pushTokens"][0]
        self.assertNotIn("token", device)
        self.assertRegex(device["tokenDigest"], r"^[0-9a-f]{8}$")
        # Session rows carry only their stamps — never the hash
        self.assertEqual(set(payload["sessions"][0]), {"created_at", "expires_at"})

    def test_the_export_budget_is_five_per_window(self):
        for _ in range(5):
            self.assertEqual(self._export().status_code, 200)
        response = self._export()
        self.assertEqual(response.status_code, 429)
        self.assertTrue(response.headers["Retry-After"])
