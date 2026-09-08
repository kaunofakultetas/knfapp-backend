############################################################
#  [*] Regression tests — who sees which post
#
#  news/core.can_view_post as a table, then the one rule
#  the wire must keep: a hidden post and a missing post
#  answer the SAME 404 on every per-post route, so
#  existence never leaks. The feed's three viewer classes
#  (guest / member / staff) each pin their slice.
############################################################


from django.test import Client, TestCase


from knfapp.common import ratelimit
from knfapp.news.core import can_view_post
from knfapp.users import auth
from .utils import bearer, befriend, create_post, create_user


def _gate(post):
    return {"id": post.id, "author_id": post.author_id, "source": post.source, "is_public": post.is_public}


class CanViewPostTests(TestCase):

    def setUp(self):
        self.author = create_user(username="autorius")
        self.friend = create_user(username="draugas", email="draugas@knf.vu.lt")
        self.stranger = create_user(username="svetimas", email="svetimas@knf.vu.lt")
        self.teacher = create_user(username="destytojas", email="destytojas@knf.vu.lt", role="teacher")
        self.admin = create_user(username="vadovas", email="vadovas@knf.vu.lt", role="admin")
        befriend(self.author, self.friend)

    def _user(self, u):
        return {"id": u.id, "role": u.role}

    def test_the_visibility_table(self):
        public_wall = _gate(create_post(author=self.author, is_public=1))
        private_wall = _gate(create_post(author=self.author, is_public=0))
        private_faculty = _gate(create_post(author=self.teacher, source="faculty",
                                            post_type="announcement", is_public=0))

        cases = [
            (public_wall, None, True),                       # public is public
            (private_wall, None, False),                     # guests never see private
            (private_wall, self._user(self.author), True),   # the author always does
            (private_wall, self._user(self.friend), True),   # a friend sees the wall
            (private_wall, self._user(self.stranger), False),
            (private_wall, self._user(self.teacher), False), # staff ≠ friends on walls
            (private_wall, self._user(self.admin), True),
            (private_faculty, self._user(self.stranger), False),
            (private_faculty, self._user(self.teacher), True),  # staff proof-read drafts
        ]
        for row, user, expected in cases:
            self.assertEqual(can_view_post(row, user), expected, (row["source"], user))


class HiddenEqualsMissingTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.author = create_user(username="autorius")
        self.stranger = create_user(username="svetimas", email="svetimas@knf.vu.lt")
        self.token = auth.mint_session(self.stranger.id)
        self.hidden = create_post(author=self.author, is_public=0)
        self.client = Client()

    def test_every_per_post_route_answers_the_same_404(self):
        hidden, missing = self.hidden.id, "0" * 32
        for path, method, kwargs in [
            ("/api/news/{}", "get", {}),
            ("/api/news/{}/comments", "get", {}),
            ("/api/news/{}/poll", "get", {}),
            ("/api/news/{}/like", "post", {"content_type": "application/json"}),
            ("/api/news/{}/share", "post", {"content_type": "application/json"}),
        ]:
            call = getattr(self.client, method)
            hidden_response = bearer(call, path.format(hidden), self.token, **kwargs)
            missing_response = bearer(call, path.format(missing), self.token, **kwargs)
            self.assertEqual(hidden_response.status_code, 404, path)
            self.assertEqual(hidden_response.json(), missing_response.json(), path)


class FeedSlicesTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.author = create_user(username="autorius")
        self.friend = create_user(username="draugas", email="draugas@knf.vu.lt")
        self.stranger = create_user(username="svetimas", email="svetimas@knf.vu.lt")
        befriend(self.author, self.friend)

        self.article = create_post(title="Straipsnis")                                  # scraped, public
        self.public_wall = create_post(author=self.author, title="Vieša siena")
        self.private_wall = create_post(author=self.author, title="Privati siena", is_public=0)
        self.draft = create_post(author=self.author, source="faculty", post_type="announcement",
                                 title="Juodraštis", is_public=0)
        self.client = Client()

    def _titles(self, token=None):
        kwargs = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        body = self.client.get("/api/news", **kwargs).json()
        return {p["title"] for p in body["posts"]}

    def test_guests_get_public_non_wall_rows_only(self):
        self.assertEqual(self._titles(), {"Straipsnis"})

    def test_a_friend_sees_the_wall_private_posts_included(self):
        token = auth.mint_session(self.friend.id)
        self.assertEqual(self._titles(token), {"Straipsnis", "Vieša siena", "Privati siena"})

    def test_a_stranger_sees_no_wall_posts_here_at_all(self):
        # Public walls of NON-friends live in /api/social/feed,
        # never in the news feed
        token = auth.mint_session(self.stranger.id)
        self.assertEqual(self._titles(token), {"Straipsnis"})

    def test_the_author_sees_their_own_draft_a_member_does_not(self):
        author_token = auth.mint_session(self.author.id)
        self.assertIn("Juodraštis", self._titles(author_token))
        stranger_token = auth.mint_session(self.stranger.id)
        self.assertNotIn("Juodraštis", self._titles(stranger_token))
