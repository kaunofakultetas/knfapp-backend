############################################################
#  [*] Regression tests — walls, blocks, reports, activity
#
#  The wall CRUD's ownership-404 rule and the faculty pair,
#  the cover rule (a registered upload of the caller's own
#  on create and on a changed edit — 400 upload_not_owned
#  otherwise, the stored cover sent back unchanged a no-op),
#  blocking's severing side effects and idempotent taps,
#  the report whitelist, and the activity list's keyset
#  paging with its read/unread pair.
############################################################


import shutil
import tempfile
import uuid


from django.test import Client, TestCase


from knfapp.common import ratelimit
from knfapp.common.timestamps import utc_now_iso
from knfapp.social.activity import record_activity
from knfapp.social.models import Activity, FriendRequest, Friendship, UserBlock
from knfapp.uploads import storage
from knfapp.uploads.models import Upload
from knfapp.users import auth
from .utils import bearer, befriend, create_post, create_user, register_upload


class WallCrudTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.client = Client()
        self.author = create_user(username="autorius")
        self.token = auth.mint_session(self.author.id)

    def test_someone_elses_post_reads_as_missing_never_403(self):
        other = create_user(username="kitas", email="k@knf.vu.lt")
        post = create_post(author=other)
        response = bearer(self.client.put, f"/api/social/posts/{post.id}", self.token,
                          data={"content": "perrašyta"}, content_type="application/json")
        self.assertEqual(response.status_code, 404)

    def test_staff_manage_their_faculty_posts_here_too(self):
        teacher = create_user(username="destytojas", email="t@knf.vu.lt", role="teacher")
        token = auth.mint_session(teacher.id)
        post = create_post(author=teacher, source="faculty", post_type="announcement")
        self.assertEqual(bearer(self.client.put, f"/api/social/posts/{post.id}", token,
                                data={"title": "Naujas pavadinimas"},
                                content_type="application/json").status_code, 200)
        self.assertEqual(bearer(self.client.delete, f"/api/social/posts/{post.id}", token).status_code, 200)

    def test_an_edit_never_rerankes_the_feed(self):
        post = create_post(author=self.author, published_at="2026-01-01T10:00:00+00:00")
        bearer(self.client.put, f"/api/social/posts/{post.id}", self.token,
               data={"content": "naujas turinys"}, content_type="application/json")
        post.refresh_from_db()
        self.assertEqual(post.published_at.isoformat(), "2026-01-01T10:00:00+00:00")

    def test_a_cover_must_be_the_callers_own_registered_upload_on_create_and_edit(self):
        tmp = tempfile.mkdtemp(prefix="knfapp-wall-")
        storage._upload_dir = tmp
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        self.addCleanup(lambda: setattr(storage, "_upload_dir", None))
        other = create_user(username="kitas", email="k@knf.vu.lt")
        foreign = register_upload(tmp, other)
        own = register_upload(tmp, self.author)

        def create(image_url):
            return bearer(self.client.post, "/api/social/posts", self.token,
                          data={"content": "Įrašas", "image_url": image_url},
                          content_type="application/json")

        refused = create(f"/api/uploads/{foreign}")
        self.assertEqual((refused.status_code, refused.json()["code"]), (400, "upload_not_owned"))
        created = create(f"/api/uploads/{own}")
        self.assertEqual(created.status_code, 201)
        post_id = created.json()["id"]

        def edit(image_url):
            return bearer(self.client.put, f"/api/social/posts/{post_id}", self.token,
                          data={"image_url": image_url}, content_type="application/json")

        refused = edit(f"/api/uploads/{foreign}")
        self.assertEqual((refused.status_code, refused.json()["code"]), (400, "upload_not_owned"))
        # The stored cover sent back unchanged is a no-op — even
        # once its ledger row is gone
        Upload.objects.filter(filename=own).delete()
        self.assertEqual(edit(f"/api/uploads/{own}").status_code, 200)
        # A NEW own upload passes
        self.assertEqual(edit(f"/api/uploads/{register_upload(tmp, self.author)}").status_code, 200)

    def test_own_posts_list_shows_private_only_to_self_and_friends(self):
        create_post(author=self.author, title="Privati", is_public=0)
        guest = self.client.get(f"/api/social/posts?user_id={self.author.id}").json()
        self.assertEqual(guest["total"], 0)
        mine = bearer(self.client.get, f"/api/social/posts?user_id={self.author.id}", self.token).json()
        self.assertEqual(mine["total"], 1)


class BlockTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.client = Client()
        self.a = create_user(username="tomas")
        self.b = create_user(username="migle", email="m@knf.vu.lt")
        self.token = auth.mint_session(self.a.id)

    def test_blocking_severs_friendship_and_pending_requests(self):
        befriend(self.a, self.b)
        FriendRequest.objects.create(id=str(uuid.uuid4()), from_user=self.b, to_user=self.a,
                                     created_at=utc_now_iso(), updated_at=utc_now_iso())
        response = bearer(self.client.post, "/api/social/blocks", self.token,
                          data={"user_id": self.b.id}, content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Friendship.objects.count(), 0)
        self.assertEqual(FriendRequest.objects.filter(status="pending").count(), 0)
        self.assertTrue(UserBlock.objects.filter(blocker_id=self.a.id, blocked_id=self.b.id).exists())

    def test_repeat_blocks_and_unknown_unblocks_are_200s(self):
        for _ in range(2):
            self.assertEqual(bearer(self.client.post, "/api/social/blocks", self.token,
                                    data={"user_id": self.b.id},
                                    content_type="application/json").status_code, 200)
        self.assertEqual(bearer(self.client.delete, f"/api/social/blocks/{self.b.id}",
                                self.token).status_code, 200)
        self.assertEqual(bearer(self.client.delete, f"/api/social/blocks/{self.b.id}",
                                self.token).status_code, 200)

    def test_the_block_list_keeps_deactivated_accounts(self):
        bearer(self.client.post, "/api/social/blocks", self.token,
               data={"user_id": self.b.id}, content_type="application/json")
        self.b.active = 0
        self.b.save(update_fields=["active"])
        body = bearer(self.client.get, "/api/social/blocks", self.token).json()
        self.assertEqual(len(body["blocked"]), 1)


class ReportTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.client = Client()
        self.reporter = create_user(username="pranesejas")
        self.token = auth.mint_session(self.reporter.id)

    def _report(self, **body):
        return bearer(self.client.post, "/api/social/reports", self.token,
                      data=body, content_type="application/json")

    def test_the_target_must_exist_in_its_own_table(self):
        target = create_user(username="kitas", email="k@knf.vu.lt")
        ok = self._report(target_type="user", target_id=target.id, reason="įžeidinėja")
        self.assertEqual(ok.status_code, 201)
        self.assertEqual(self._report(target_type="post", target_id="0" * 32,
                                      reason="netinka").status_code, 404)
        self.assertEqual(self._report(target_type="įrašas", target_id="x",
                                      reason="netinka").status_code, 400)


class ActivityTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.client = Client()
        self.user = create_user(username="tomas")
        self.actor = create_user(username="migle", email="m@knf.vu.lt")
        self.token = auth.mint_session(self.user.id)

    def test_keyset_paging_never_duplicates_or_drops(self):
        for i in range(35):
            record_activity(self.user.id, "like", self.actor.id, f"post-{i}", None)

        first = bearer(self.client.get, "/api/social/activity", self.token).json()
        self.assertEqual((len(first["notifications"]), first["hasMore"]), (30, True))

        second = bearer(self.client.get, f"/api/social/activity?cursor={first['cursor']}", self.token).json()
        self.assertEqual((len(second["notifications"]), second["hasMore"]), (5, False))
        ids = [n["id"] for n in first["notifications"]] + [n["id"] for n in second["notifications"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_a_malformed_cursor_reads_as_the_first_page(self):
        record_activity(self.user.id, "like", self.actor.id, "post-1", None)
        body = bearer(self.client.get, "/api/social/activity?cursor=sugadintas", self.token).json()
        self.assertEqual(len(body["notifications"]), 1)

    def test_read_all_and_the_badge_probe(self):
        record_activity(self.user.id, "like", self.actor.id, "post-1", None)
        self.assertEqual(bearer(self.client.get, "/api/social/activity/unread",
                                self.token).json()["count"], 1)
        bearer(self.client.post, "/api/social/activity/read", self.token, content_type="application/json")
        self.assertEqual(bearer(self.client.get, "/api/social/activity/unread",
                                self.token).json()["count"], 0)
        self.assertEqual(Activity.objects.filter(read=0).count(), 0)
