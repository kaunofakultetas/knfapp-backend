############################################################
#  [*] Regression tests — who sees which post
#
#  news/core.can_view_post as a table (the block rows
#  included), then the one rule the wire must keep: a
#  hidden post and a missing post answer the SAME 404 on
#  every per-post route, so existence never leaks. The
#  feed's three viewer classes (guest / member / staff)
#  each pin their slice; the block is enforced on every
#  read and write both apps serve; and a private faculty
#  draft is the staff's to proof-read, never a friend's.
############################################################


from django.test import Client, TestCase


from knfapp.common import ratelimit
from knfapp.common.timestamps import utc_now_iso
from knfapp.news.core import can_view_post
from knfapp.news.models import NewsPost
from knfapp.social.models import Activity, UserBlock
from knfapp.users import auth
from .utils import bearer, befriend, create_post, create_user


def _block_row(blocker, blocked):
    # The row POST /api/social/blocks writes — for the predicate
    # table, which never goes through a route
    UserBlock.objects.create(blocker=blocker, blocked=blocked, created_at=utc_now_iso())


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

    def test_the_block_rows_of_the_table(self):
        blocker = create_user(username="blokuotojas", email="b@knf.vu.lt")
        _block_row(self.author, self.stranger)      # the author blocked the stranger
        _block_row(blocker, self.author)            # and was blocked by somebody else
        _block_row(self.teacher, self.stranger)     # a teacher blocked the stranger
        _block_row(self.stranger, self.admin)       # the stranger blocked the admin

        public_wall = _gate(create_post(author=self.author, is_public=1))
        private_wall = _gate(create_post(author=self.author, is_public=0))
        public_faculty = _gate(create_post(author=self.teacher, source="faculty",
                                           post_type="announcement", is_public=1))
        private_faculty = _gate(create_post(author=self.teacher, source="faculty",
                                            post_type="announcement", is_public=0))
        stranger_wall = _gate(create_post(author=self.stranger, is_public=1))

        cases = [
            (public_wall, self._user(self.stranger), False),   # blocked BY the author: even public
            (public_wall, self._user(blocker), False),         # the blocker's own block, the other way
            (stranger_wall, self._user(self.author), False),   # and the author never sees the blocked's wall
            (public_wall, self._user(self.friend), True),      # a block is a pair, not a shroud
            (public_faculty, self._user(self.stranger), True),  # official word stays readable
            (private_faculty, self._user(self.stranger), False),  # the unchanged draft rule
            (public_wall, None, True),                         # guests are nobody's blocked
            (stranger_wall, self._user(self.admin), True),     # admin keeps the bypass
            (private_wall, self._user(self.admin), True),
        ]
        for row, user, expected in cases:
            self.assertEqual(can_view_post(row, user), expected, (row["source"], user))

        # The caller's own block set short-circuits the query
        self.assertFalse(can_view_post(public_wall, self._user(self.stranger), blocked={self.author.id}))
        self.assertTrue(can_view_post(public_wall, self._user(self.stranger), blocked=set()))


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


class BlockEnforcementTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.client = Client()
        self.victim = create_user(username="auka")
        self.harasser = create_user(username="priekabiautojas", email="p@knf.vu.lt")
        self.victim_token = auth.mint_session(self.victim.id)
        self.harasser_token = auth.mint_session(self.harasser.id)
        self.victim_post = create_post(author=self.victim, title="Aukos įrašas")
        self.harasser_post = create_post(author=self.harasser, title="Priekabiautojo įrašas")
        # Through the route — the friendship severing rides along
        self._block(self.victim_token, self.harasser.id)

    def _block(self, token, user_id):
        response = bearer(self.client.post, "/api/social/blocks", token,
                          data={"user_id": user_id}, content_type="application/json")
        self.assertEqual(response.status_code, 200)

    def _titles(self, path, token):
        response = bearer(self.client.get, path, token)
        self.assertEqual(response.status_code, 200, path)
        return {p["title"] for p in response.json()["posts"]}

    def _like(self, post, token):
        return bearer(self.client.post, f"/api/news/{post.id}/like", token, content_type="application/json")

    def _comment(self, post, token, text="Ei tu"):
        return bearer(self.client.post, f"/api/news/{post.id}/comments", token,
                      data={"text": text}, content_type="application/json")

    def test_the_harasser_cannot_like_or_comment_on_the_victims_wall(self):
        self.assertEqual(self._like(self.victim_post, self.harasser_token).status_code, 404)
        self.assertEqual(self._comment(self.victim_post, self.harasser_token).status_code, 404)
        # Nothing reached the victim's activity list, nothing counted
        self.assertEqual(Activity.objects.filter(user=self.victim).count(), 0)
        row = NewsPost.objects.get(id=self.victim_post.id)
        self.assertEqual((row.likes_count, row.comments_count), (0, 0))
        # Nor the other way round
        self.assertEqual(self._like(self.harasser_post, self.victim_token).status_code, 404)

    def test_the_harasser_reads_the_victims_profile_and_wall_as_missing(self):
        profile = bearer(self.client.get, f"/api/social/profile/{self.victim.id}", self.harasser_token)
        self.assertEqual(profile.status_code, 404)
        wall = bearer(self.client.get, f"/api/social/posts?user_id={self.victim.id}", self.harasser_token)
        self.assertEqual(wall.status_code, 404)
        # The same body an unknown id gets — a block is not a fact
        # the blocked party is told
        missing = bearer(self.client.get, f"/api/social/profile/{'0' * 32}", self.harasser_token)
        self.assertEqual(profile.json(), missing.json())
        self.assertEqual(bearer(self.client.get, f"/api/news/{self.victim_post.id}",
                                self.harasser_token).status_code, 404)

    def test_the_blocker_keeps_the_shell_with_the_wall_hidden(self):
        # blockedByMe is the client's only unblock affordance — the
        # profile stays, the wall rows do not
        profile = bearer(self.client.get, f"/api/social/profile/{self.harasser.id}", self.victim_token)
        self.assertEqual(profile.status_code, 200)
        self.assertEqual((profile.json()["blockedByMe"], profile.json()["postCount"]), (True, 0))
        wall = bearer(self.client.get, f"/api/social/posts?user_id={self.harasser.id}", self.victim_token)
        self.assertEqual((wall.status_code, wall.json()["total"]), (200, 0))

    def test_the_community_feed_drops_both_sides_wall_posts(self):
        self.assertNotIn("Priekabiautojo įrašas", self._titles("/api/social/feed", self.victim_token))
        self.assertNotIn("Aukos įrašas", self._titles("/api/social/feed", self.harasser_token))
        # A third party still sees both — a block is a pair
        other = create_user(username="kitas", email="k@knf.vu.lt")
        self.assertEqual(self._titles("/api/social/feed", auth.mint_session(other.id)),
                         {"Aukos įrašas", "Priekabiautojo įrašas"})

    def test_the_news_feed_drops_the_blocked_wall_posts_on_its_own_gate(self):
        # The block route severs the friendship, and that alone keeps
        # a non-friend's wall out of /api/news — so the feed's OWN
        # gate is proven with the friendship re-written beside the
        # block
        befriend(self.victim, self.harasser)
        self.assertNotIn("Priekabiautojo įrašas", self._titles("/api/news", self.victim_token))
        self.assertNotIn("Aukos įrašas", self._titles("/api/news", self.harasser_token))
        # Each still sees their own
        self.assertIn("Aukos įrašas", self._titles("/api/news", self.victim_token))

    def test_official_rows_stay_readable_but_not_engageable(self):
        teacher = create_user(username="destytojas", email="d@knf.vu.lt", role="teacher")
        announcement = create_post(author=teacher, source="faculty", post_type="announcement",
                                   title="Skelbimas")
        self._block(auth.mint_session(teacher.id), self.harasser.id)

        self.assertIn("Skelbimas", self._titles("/api/news", self.harasser_token))
        self.assertEqual(bearer(self.client.get, f"/api/news/{announcement.id}",
                                self.harasser_token).status_code, 200)
        self.assertEqual(self._like(announcement, self.harasser_token).status_code, 404)
        self.assertEqual(self._comment(announcement, self.harasser_token).status_code, 404)
        self.assertEqual(Activity.objects.filter(user=teacher).count(), 0)

    def test_the_thread_omits_the_blocked_partys_comments(self):
        teacher = create_user(username="destytojas", email="d@knf.vu.lt", role="teacher")
        announcement = create_post(author=teacher, source="faculty", post_type="announcement")
        self.assertEqual(self._comment(announcement, self.victim_token, "Aukos žodis").status_code, 201)
        self.assertEqual(self._comment(announcement, self.harasser_token, "Priekabė").status_code, 201)

        def thread(token=None):
            kwargs = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
            body = self.client.get(f"/api/news/{announcement.id}/comments", **kwargs).json()
            return {c["text"] for c in body["comments"]}, body["total"]

        self.assertEqual(thread(self.victim_token), ({"Aukos žodis"}, 1))
        self.assertEqual(thread(self.harasser_token), ({"Priekabė"}, 1))
        self.assertEqual(thread(), ({"Aukos žodis", "Priekabė"}, 2))


class PrivateDraftWallTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.client = Client()
        self.teacher = create_user(username="destytojas", role="teacher")
        self.student = create_user(username="studentas", email="s@knf.vu.lt")
        self.curator = create_user(username="kuratorius", email="k@knf.vu.lt", role="curator")
        befriend(self.teacher, self.student)
        self.draft = create_post(author=self.teacher, source="faculty", post_type="announcement",
                                 title="Juodraštis", is_public=0)

    def _wall(self, token):
        return bearer(self.client.get, f"/api/social/posts?user_id={self.teacher.id}", token).json()

    def _post_count(self, token):
        return bearer(self.client.get, f"/api/social/profile/{self.teacher.id}", token).json()["postCount"]

    def _post_status(self, token):
        return bearer(self.client.get, f"/api/news/{self.draft.id}", token).status_code

    def test_a_student_friend_never_sees_the_draft(self):
        token = auth.mint_session(self.student.id)
        self.assertEqual(self._wall(token)["total"], 0)
        self.assertEqual(self._post_count(token), 0)
        self.assertEqual(self._post_status(token), 404)

    def test_a_curator_proof_reads_it_without_being_a_friend(self):
        token = auth.mint_session(self.curator.id)
        self.assertEqual(self._wall(token)["total"], 1)
        self.assertEqual(self._post_count(token), 1)
        self.assertEqual(self._post_status(token), 200)

    def test_the_author_sees_their_own(self):
        token = auth.mint_session(self.teacher.id)
        self.assertEqual(self._wall(token)["total"], 1)
        self.assertEqual(self._post_count(token), 1)

    def test_friendship_still_unlocks_the_private_wall_post_only(self):
        create_post(author=self.teacher, title="Privati siena", is_public=0)
        token = auth.mint_session(self.student.id)
        self.assertEqual({p["title"] for p in self._wall(token)["posts"]}, {"Privati siena"})
        self.assertEqual(self._post_count(token), 1)
