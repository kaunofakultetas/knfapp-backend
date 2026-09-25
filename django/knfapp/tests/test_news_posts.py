############################################################
#  [*] Regression tests — creating and deleting posts
#
#  The role→source rule (there is no role GATE — staff
#  publish as the faculty, members to their wall), the
#  typed-input 400s that must never be 500s, the own-uploads
#  image rule (the path shape AND the ledger row: somebody
#  else's registered file is 400 upload_not_owned), the
#  'news' push a public faculty post hands off on the commit
#  (a daemon thread — never the request's own transaction,
#  and nothing for a member's or a private post), and
#  delete's tombstone — the row that keeps the scrapers from
#  resurrecting an admin-deleted article — plus delete's
#  cover cleanup, which acts as the AUTHOR and so never takes
#  a foreign file planted in the column.
############################################################


import os
import shutil
import tempfile


from django.test import Client, TestCase


from knfapp.common import ratelimit
from knfapp.news import core
from knfapp.news.api import views
from knfapp.news.models import DeletedSourceUrl, NewsComment, NewsLike, NewsPost
from knfapp.uploads import storage
from knfapp.uploads.models import Upload
from knfapp.users import auth
from .utils import bearer, create_post, create_user, register_upload


def _throwaway_upload_dir(case):
    tmp = tempfile.mkdtemp(prefix="knfapp-news-")
    storage._upload_dir = tmp
    case.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
    case.addCleanup(lambda: setattr(storage, "_upload_dir", None))
    return tmp


class CreatePostTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.client = Client()

    def _create(self, role="student", **body):
        self._seq = getattr(self, "_seq", 0) + 1
        user = create_user(username=f"u{role}{self._seq}", email=f"{role}{self._seq}@knf.vu.lt", role=role)
        token = auth.mint_session(user.id)
        payload = {"content": "Sveiki visi!", **body}
        return bearer(self.client.post, "/api/news", token, data=payload,
                      content_type="application/json")

    def test_the_role_picks_the_source_never_a_gate(self):
        member = self._create().json()
        self.assertEqual((member["source"], member["postType"]), ("user", "social"))
        teacher = self._create(role="teacher").json()
        self.assertEqual((teacher["source"], teacher["postType"]), ("faculty", "announcement"))

    def test_the_201_is_the_reread_wire_shape(self):
        body = self._create().json()
        self.assertFalse(body["liked"])
        self.assertIsNone(body["sourceUrl"])
        self.assertEqual(body["title"], "Sveiki visi!"[:80])
        self.assertTrue(body["isPublic"])

    def test_the_cover_must_be_the_callers_own_registered_upload(self):
        tmp = _throwaway_upload_dir(self)
        author = create_user(username="autorius")
        other = create_user(username="kitas", email="kitas@knf.vu.lt")
        foreign = register_upload(tmp, other)
        own = register_upload(tmp, author)
        token = auth.mint_session(author.id)

        refused = bearer(self.client.post, "/api/news", token,
                         data={"content": "Sveiki", "image_url": f"/api/uploads/{foreign}"},
                         content_type="application/json")
        self.assertEqual((refused.status_code, refused.json()["code"]), (400, "upload_not_owned"))

        accepted = bearer(self.client.post, "/api/news", token,
                          data={"content": "Sveiki", "image_url": f"/api/uploads/{own}"},
                          content_type="application/json")
        self.assertEqual(accepted.status_code, 201)
        self.assertEqual(accepted.json()["imageUrl"], f"/api/uploads/{own}")

    def test_typed_inputs_answer_400_never_500(self):
        cases = [
            {"content": 123},
            {"content": "x", "title": ["sąrašas"]},
            {"content": "x", "post_type": "poll"},          # no poll cards without a poll
            {"content": "x", "post_type": "keistas"},
            {"content": "x", "is_public": "false"},         # the truthy-string trap
            {"content": "x", "image_url": 0},
            {"content": "x", "image_url": "https://evil.example/x.jpg"},
        ]
        for body in cases:
            self.assertEqual(self._create(**body).status_code, 400, body)

    def test_a_public_faculty_post_hands_its_push_to_the_commit(self):
        # The seam: no real thread in a test — capture the spawn
        spawned = []
        real = views._spawn_news_push
        views._spawn_news_push = lambda *args: spawned.append(args)
        self.addCleanup(lambda: setattr(views, "_spawn_news_push", real))

        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            response = self._create(role="teacher", title="Posėdis", content="Rytoj 10:00")
            self.assertEqual(response.status_code, 201)
            self.assertEqual(spawned, [])  # nothing leaves before the commit
        body = response.json()
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(spawned, [(
            "Posėdis", "Rytoj 10:00"[:core.SUMMARY_LENGTH],
            {"type": "news", "source": "faculty", "postId": body["id"]},
            NewsPost.objects.get(id=body["id"]).author_id,
        )])

        # A member's post and a private faculty post ring nobody
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self._create().status_code, 201)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self._create(role="teacher", is_public=False).status_code, 201)
        self.assertEqual(len(spawned), 1)


class DeletePostTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.client = Client()
        self.author = create_user(username="autorius")
        self.token = auth.mint_session(self.author.id)

    def test_author_or_admin_only(self):
        post = create_post(author=self.author)
        stranger = create_user(username="svetimas", email="svetimas@knf.vu.lt")
        self.assertEqual(bearer(self.client.delete, f"/api/news/{post.id}",
                                auth.mint_session(stranger.id)).status_code, 403)
        self.assertEqual(bearer(self.client.delete, f"/api/news/{post.id}", self.token).status_code, 200)
        self.assertEqual(NewsPost.objects.filter(id=post.id).count(), 0)

    def test_a_scraped_delete_writes_the_tombstone(self):
        admin = create_user(username="vadovas", email="vadovas@knf.vu.lt", role="admin")
        post = create_post(source_url="https://knf.vu.lt/naujiena-1")
        response = bearer(self.client.delete, f"/api/news/{post.id}", auth.mint_session(admin.id))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(DeletedSourceUrl.objects.filter(source_url="https://knf.vu.lt/naujiena-1").exists())

    def test_the_delete_never_takes_a_foreign_file_planted_in_the_cover(self):
        # Written straight into the column, past the acceptance
        # check — the sink still refuses it as not the author's
        tmp = _throwaway_upload_dir(self)
        other = create_user(username="kitas", email="kitas@knf.vu.lt")
        foreign = register_upload(tmp, other)
        post = create_post(author=self.author, image_url=f"/api/uploads/{foreign}")

        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(bearer(self.client.delete, f"/api/news/{post.id}", self.token).status_code, 200)

        self.assertEqual(NewsPost.objects.filter(id=post.id).count(), 0)
        self.assertTrue(os.path.exists(os.path.join(tmp, foreign)))
        self.assertTrue(Upload.objects.filter(filename=foreign, user_id=other.id).exists())

    def test_the_authors_own_cover_goes_with_the_post_whoever_deletes_it(self):
        tmp = _throwaway_upload_dir(self)
        own = register_upload(tmp, self.author)
        post = create_post(author=self.author, image_url=f"/api/uploads/{own}")
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(bearer(self.client.delete, f"/api/news/{post.id}", self.token).status_code, 200)
        self.assertFalse(os.path.exists(os.path.join(tmp, own)))
        self.assertFalse(Upload.objects.filter(filename=own).exists())

        # An admin's delete acts for the author too — the cover is
        # the author's, and it goes
        admin = create_user(username="vadovas", email="vadovas@knf.vu.lt", role="admin")
        own2 = register_upload(tmp, self.author)
        post2 = create_post(author=self.author, image_url=f"/api/uploads/{own2}")
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(bearer(self.client.delete, f"/api/news/{post2.id}",
                                    auth.mint_session(admin.id)).status_code, 200)
        self.assertFalse(os.path.exists(os.path.join(tmp, own2)))

    def test_dependants_die_with_the_post(self):
        from .utils import PASSWORD  # noqa: F401 — parity import guard
        post = create_post(author=self.author)
        liker = create_user(username="draugas", email="d@knf.vu.lt")
        bearer(self.client.post, f"/api/news/{post.id}/like", auth.mint_session(liker.id),
               content_type="application/json")
        bearer(self.client.post, f"/api/news/{post.id}/comments", auth.mint_session(liker.id),
               data={"text": "Puiku"}, content_type="application/json")

        bearer(self.client.delete, f"/api/news/{post.id}", self.token)
        self.assertEqual(NewsLike.objects.count(), 0)
        self.assertEqual(NewsComment.objects.count(), 0)
