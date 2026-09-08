############################################################
#  [*] Regression tests — likes, shares, comments
#
#  The counter law (recomputed from child rows, never ±1 —
#  the drift that forced a production reset), the activity
#  rows that ride the same transaction (a withdrawn like
#  takes its row back), the guest share, and the comment
#  thread's deleted-user rendering.
############################################################


from django.test import Client, TestCase


from knfapp.common import ratelimit
from knfapp.news.models import NewsPost
from knfapp.social.models import Activity
from knfapp.users import auth
from .utils import bearer, create_post, create_user


class LikeTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.client = Client()
        self.author = create_user(username="autorius")
        self.liker = create_user(username="draugas", email="d@knf.vu.lt")
        self.token = auth.mint_session(self.liker.id)
        self.post = create_post(author=self.author)

    def _toggle(self):
        return bearer(self.client.post, f"/api/news/{self.post.id}/like", self.token,
                      content_type="application/json").json()

    def test_the_toggle_recounts_and_the_activity_row_follows(self):
        on = self._toggle()
        self.assertEqual((on["liked"], on["likes"]), (True, 1))
        self.assertEqual(NewsPost.objects.get(id=self.post.id).likes_count, 1)
        self.assertEqual(Activity.objects.filter(user=self.author, kind="like").count(), 1)

        off = self._toggle()
        self.assertEqual((off["liked"], off["likes"]), (False, 0))
        # The list never advertises a withdrawn gesture
        self.assertEqual(Activity.objects.count(), 0)

    def test_a_drifted_counter_heals_on_the_next_toggle(self):
        NewsPost.objects.filter(id=self.post.id).update(likes_count=41)
        self.assertEqual(self._toggle()["likes"], 1)

    def test_a_self_like_writes_no_activity(self):
        own = create_post(author=self.liker)
        bearer(self.client.post, f"/api/news/{own.id}/like", self.token, content_type="application/json")
        self.assertEqual(Activity.objects.count(), 0)


class ShareTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.client = Client()

    def test_guests_share_but_only_what_they_can_see(self):
        public = create_post()
        hidden = create_post(author=create_user(username="a"), is_public=0)
        ok = self.client.post(f"/api/news/{public.id}/share", content_type="application/json")
        self.assertEqual((ok.status_code, ok.json()["shares"]), (200, 1))
        self.assertEqual(self.client.post(f"/api/news/{hidden.id}/share",
                                          content_type="application/json").status_code, 404)


class CommentTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.client = Client()
        self.author = create_user(username="autorius")
        self.commenter = create_user(username="draugas", email="d@knf.vu.lt")
        self.token = auth.mint_session(self.commenter.id)
        self.post = create_post(author=self.author)

    def _add(self, text="Puiki žinia!"):
        return bearer(self.client.post, f"/api/news/{self.post.id}/comments", self.token,
                      data={"text": text}, content_type="application/json")

    def test_add_recounts_and_answers_one_clock(self):
        body = self._add().json()
        self.assertEqual(NewsPost.objects.get(id=self.post.id).comments_count, 1)
        # The 201's time IS the stored created_at — the list refetch
        # must not make the comment jump in time
        listed = self.client.get(f"/api/news/{self.post.id}/comments").json()["comments"][0]
        self.assertEqual(listed["time"], body["time"])
        self.assertEqual(Activity.objects.filter(kind="comment").count(), 1)

    def test_the_delete_rule_author_owner_admin(self):
        comment_id = self._add().json()["id"]
        stranger = create_user(username="svetimas", email="s@knf.vu.lt")
        path = f"/api/news/{self.post.id}/comments/{comment_id}"
        self.assertEqual(bearer(self.client.delete, path, auth.mint_session(stranger.id)).status_code, 403)
        # The post's author may moderate their thread
        response = bearer(self.client.delete, path, auth.mint_session(self.author.id))
        self.assertEqual((response.status_code, response.json()["comments"]), (200, 0))

    def test_a_comment_from_another_thread_is_unreachable(self):
        other = create_post(author=self.author, title="Kita")
        comment_id = self._add().json()["id"]
        self.assertEqual(bearer(self.client.delete, f"/api/news/{other.id}/comments/{comment_id}",
                                auth.mint_session(self.author.id)).status_code, 404)

    def test_an_orphaned_comment_still_renders_and_counts(self):
        self._add()
        self.commenter.delete()
        body = self.client.get(f"/api/news/{self.post.id}/comments").json()
        # The CASCADE takes the comment with the user — but a row
        # orphaned some OTHER way must not break paging, so the
        # LEFT-join fallback stays pinned at the shape level
        self.assertEqual(body["total"], len(body["comments"]))
