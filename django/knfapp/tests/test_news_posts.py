############################################################
#  [*] Regression tests — creating and deleting posts
#
#  The role→source rule (there is no role GATE — staff
#  publish as the faculty, members to their wall), the
#  typed-input 400s that must never be 500s, the own-uploads
#  image rule, and delete's tombstone — the row that keeps
#  the scrapers from resurrecting an admin-deleted article.
############################################################


from django.test import Client, TestCase


from knfapp.common import ratelimit
from knfapp.news.models import DeletedSourceUrl, NewsComment, NewsLike, NewsPost
from knfapp.users import auth
from .utils import bearer, create_post, create_user


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
