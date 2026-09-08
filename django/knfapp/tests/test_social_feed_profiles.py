############################################################
#  [*] Regression tests — the community feed and profiles
#
#  The feed's viewer slices and its truncation flag, the
#  recency window, the profile's viewer-dependent counts and
#  friendshipStatus, and the block flag the client keys the
#  unblock affordance on.
############################################################


from datetime import datetime, timedelta, timezone


from django.test import Client, TestCase


from knfapp.common import ratelimit
from knfapp.news.core import SUMMARY_LENGTH
from knfapp.users import auth
from .utils import bearer, befriend, create_post, create_user


class SocialFeedTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.client = Client()
        self.author = create_user(username="autorius")
        self.friend = create_user(username="draugas", email="d@knf.vu.lt")
        befriend(self.author, self.friend)

    def _titles(self, token=None):
        kwargs = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        return {p["title"] for p in self.client.get("/api/social/feed", **kwargs).json()["posts"]}

    def test_walls_only_and_the_viewer_slices(self):
        create_post(title="Straipsnis")                                        # scraped — never here
        create_post(author=self.author, title="Vieša siena")
        create_post(author=self.author, title="Privati siena", is_public=0)

        self.assertEqual(self._titles(), {"Vieša siena"})
        token = auth.mint_session(self.friend.id)
        self.assertEqual(self._titles(token), {"Vieša siena", "Privati siena"})

    def test_a_deactivated_authors_posts_are_nobodys(self):
        ghost = create_user(username="dinges", email="g@knf.vu.lt", active=0)
        create_post(author=ghost, title="Palikta")
        self.assertEqual(self._titles(), set())

    def test_the_recency_window_bounds_the_ranked_wall(self):
        create_post(author=self.author, title="Šviežias")
        create_post(author=self.author, title="Senas",
                    published_at=(datetime.now(timezone.utc) - timedelta(days=200)).isoformat())
        self.assertEqual(self._titles(), {"Šviežias"})

    def test_list_bodies_are_trimmed_with_the_flag(self):
        create_post(author=self.author, title="Ilgas", content="ą" * (SUMMARY_LENGTH + 50))
        post = self.client.get("/api/social/feed").json()["posts"][0]
        self.assertTrue(post["truncated"])
        self.assertEqual(len(post["content"]), SUMMARY_LENGTH)
        # The author rides as the CURRENT display name + avatar
        self.assertEqual(post["author"], self.author.display_name)


class ProfileTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.client = Client()
        self.owner = create_user(username="autorius")
        self.viewer = create_user(username="lankytojas", email="l@knf.vu.lt")
        self.viewer_token = auth.mint_session(self.viewer.id)

    def _profile(self, token=None):
        kwargs = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        return self.client.get(f"/api/social/profile/{self.owner.id}", **kwargs).json()

    def test_post_count_follows_the_viewers_visibility(self):
        create_post(author=self.owner, title="Vieša")
        create_post(author=self.owner, title="Privati", is_public=0)
        create_post(author=self.owner, source="faculty", post_type="announcement", title="Skelbimas")

        self.assertEqual(self._profile()["postCount"], 2)          # guest: public user+faculty
        befriend(self.owner, self.viewer)
        self.assertEqual(self._profile(self.viewer_token)["postCount"], 3)

    def test_friendship_status_speaks_from_the_viewers_side(self):
        self.assertEqual(self._profile(self.viewer_token)["friendshipStatus"], "none")

        bearer(self.client.post, "/api/social/friends/request", self.viewer_token,
               data={"user_id": self.owner.id}, content_type="application/json")
        self.assertEqual(self._profile(self.viewer_token)["friendshipStatus"], "request_sent")

        # And from the owner's side the same row reads received
        owner_token = auth.mint_session(self.owner.id)
        viewed = self.client.get(f"/api/social/profile/{self.viewer.id}",
                                 HTTP_AUTHORIZATION=f"Bearer {owner_token}").json()
        self.assertEqual(viewed["friendshipStatus"], "request_received")

    def test_blocked_by_me_keys_the_unblock_affordance(self):
        bearer(self.client.post, "/api/social/blocks", self.viewer_token,
               data={"user_id": self.owner.id}, content_type="application/json")
        self.assertTrue(self._profile(self.viewer_token)["blockedByMe"])

    def test_a_deactivated_profile_is_gone_except_to_admins(self):
        ghost = create_user(username="dinges", email="g@knf.vu.lt", active=0)
        self.assertEqual(self.client.get(f"/api/social/profile/{ghost.id}").status_code, 404)
        admin = create_user(username="vadovas", email="v@knf.vu.lt", role="admin")
        response = self.client.get(f"/api/social/profile/{ghost.id}",
                                   HTTP_AUTHORIZATION=f"Bearer {auth.mint_session(admin.id)}")
        self.assertEqual(response.status_code, 200)
