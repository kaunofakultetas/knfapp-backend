############################################################
#  [*] Regression tests — the feed's ETag seed and Vary
#
#  The seed rule (every input to the visibility filter is
#  an input to the tag — a demotion, a block), the
#  fingerprint that moves on EVERY engagement and poll
#  write (a like moved between two posts leaves every sum
#  where it was; a poll attached or voted touches no
#  counter), and the cache headers: every cacheable API
#  answer varies on Authorization, the signed-in feed as
#  private, no-cache.
############################################################


from django.db.models import Sum
from django.test import Client, TestCase


from knfapp.common import ratelimit
from knfapp.common.timestamps import utc_now_iso
from knfapp.news.models import NewsPost
from knfapp.social.models import UserBlock
from knfapp.users import auth
from knfapp.users.models import User
from .utils import bearer, befriend, create_post, create_user


def _titles(response):
    return [p["title"] for p in response.json()["posts"]]


class FeedSeedTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.client = Client()

    def _feed(self, token=None, tag=None):
        kwargs = {}
        if token:
            kwargs["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        if tag:
            kwargs["HTTP_IF_NONE_MATCH"] = tag
        return self.client.get("/api/news", **kwargs)

    def test_a_demotion_moves_the_tag(self):
        curator = create_user(username="kuratorius", role="curator")
        teacher = create_user(username="destytojas", email="d@knf.vu.lt", role="teacher")
        create_post(author=curator, source="faculty", post_type="announcement",
                    title="Juodraštis", is_public=0)
        token = auth.mint_session(teacher.id)

        first = self._feed(token)
        self.assertIn("Juodraštis", _titles(first))
        self.assertEqual(self._feed(token, first["ETag"]).status_code, 304)

        # The role is read per request — the demotion is live at once,
        # and the old tag must not answer for a body that held the draft
        User.objects.filter(id=teacher.id).update(role="student")
        again = self._feed(token, first["ETag"])
        self.assertEqual(again.status_code, 200)
        self.assertNotIn("Juodraštis", _titles(again))

    def test_a_block_moves_the_tag(self):
        viewer = create_user(username="skaitytojas")
        friend = create_user(username="draugas", email="d@knf.vu.lt")
        befriend(viewer, friend)
        create_post(author=friend, title="Draugo siena")
        token = auth.mint_session(viewer.id)

        first = self._feed(token)
        self.assertIn("Draugo siena", _titles(first))

        # The row beside an INTACT friendship: the block route would
        # sever it and move the friend-set term too, which is not
        # what is under test — the block set is a seed input of its own
        UserBlock.objects.create(blocker=viewer, blocked=friend, created_at=utc_now_iso())
        again = self._feed(token, first["ETag"])
        self.assertEqual(again.status_code, 200)
        self.assertNotIn("Draugo siena", _titles(again))


class FeedFingerprintTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.client = Client()
        self.reader = create_user(username="skaitytojas")
        self.token = auth.mint_session(self.reader.id)

    def _feed(self, tag=None):
        kwargs = {"HTTP_AUTHORIZATION": f"Bearer {self.token}"}
        if tag:
            kwargs["HTTP_IF_NONE_MATCH"] = tag
        return self.client.get("/api/news", **kwargs)

    def _like(self, post):
        return bearer(self.client.post, f"/api/news/{post.id}/like", self.token,
                      content_type="application/json")

    def test_a_like_moved_between_two_posts_moves_the_tag(self):
        a = create_post(title="A")
        b = create_post(title="B")
        self._like(a)
        first = self._feed()
        self.assertEqual(_titles(first)[0], "A")
        self.assertEqual({p["title"]: p["liked"] for p in first.json()["posts"]}, {"A": True, "B": False})

        # Unlike A, like B: the summed counters land exactly where
        # they were, the bytes and the ranking did not
        self._like(a)
        self._like(b)
        self.assertEqual(NewsPost.objects.aggregate(n=Sum("likes_count"))["n"], 1)

        again = self._feed(first["ETag"])
        self.assertEqual(again.status_code, 200)
        self.assertEqual(_titles(again)[0], "B")
        self.assertEqual({p["title"]: p["liked"] for p in again.json()["posts"]}, {"A": False, "B": True})

    def test_a_poll_attached_and_a_vote_move_the_tag(self):
        teacher = create_user(username="destytojas", email="d@knf.vu.lt", role="teacher")
        self.token = auth.mint_session(teacher.id)
        post = create_post(author=teacher, source="faculty", post_type="announcement", title="Apklausa")

        first = self._feed()
        self.assertEqual(self._feed(first["ETag"]).status_code, 304)

        created = bearer(self.client.post, f"/api/news/{post.id}/poll", self.token,
                         data={"title": "Kur?", "options": ["Kaune", "Vilniuje"]},
                         content_type="application/json")
        self.assertEqual(created.status_code, 201)
        with_poll = self._feed(first["ETag"])
        self.assertEqual(with_poll.status_code, 200)
        card = with_poll.json()["posts"][0]
        self.assertEqual((card["postType"], card["poll"]["totalVotes"]), ("poll", 0))

        option_id = created.json()["options"][0]["id"]
        voted = bearer(self.client.post, f"/api/news/{post.id}/poll/vote", self.token,
                       data={"option_id": option_id}, content_type="application/json")
        self.assertEqual(voted.status_code, 200)
        after_vote = self._feed(with_poll["ETag"])
        self.assertEqual(after_vote.status_code, 200)
        card = after_vote.json()["posts"][0]
        self.assertEqual((card["poll"]["totalVotes"], card["poll"]["userVote"]), (1, option_id))


class VaryTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.client = Client()
        self.user = create_user()
        self.token = auth.mint_session(self.user.id)
        create_post(title="Straipsnis")

    def test_the_feed_varies_on_authorization_and_the_member_arm_is_no_cache(self):
        guest = self.client.get("/api/news")
        self.assertIn("Authorization", guest["Vary"])
        self.assertIn("Accept-Encoding", guest["Vary"])
        self.assertEqual(guest["Cache-Control"], "public, max-age=60")

        member = bearer(self.client.get, "/api/news", self.token)
        self.assertIn("Authorization", member["Vary"])
        self.assertEqual(member["Cache-Control"], "private, no-cache")

        # The 304 arm carries the same headers
        cached = bearer(self.client.get, "/api/news", self.token, HTTP_IF_NONE_MATCH=member["ETag"])
        self.assertEqual(cached.status_code, 304)
        self.assertIn("Authorization", cached["Vary"])
        self.assertEqual(cached["Cache-Control"], "private, no-cache")

    def test_the_public_routes_vary_on_authorization_too(self):
        for path in ("/api/schedule/filters", "/api/schedule/events", "/api/info"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn("Authorization", response["Vary"], path)
            self.assertIn("Accept-Encoding", response["Vary"], path)
            self.assertIn("public", response["Cache-Control"], path)
