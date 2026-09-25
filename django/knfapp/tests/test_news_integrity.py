############################################################
#  [*] Regression tests — the news writers' integrity rules
#
#  The like as a SET (a client queue coalesces a tap burst
#  to "in flight + final intent", and two flips of a toggle
#  undo each other — the final intent must land), the one
#  lock order (every writer locks the post row before it
#  touches a child row), the author-liveness rule (a
#  deactivated account's wall post is gone from /api/news
#  exactly as it is from the social app — reads, writes,
#  the feed and its ETag), the list-page body cut the phone
#  opts into, the poll that cannot be born closed, a reader's
#  tallies that agree with the thread they open (a block
#  hides the other side's comments from both), and an
#  untitled post's title cut at a word, never mid-word.
############################################################


from datetime import datetime, timedelta, timezone
from unittest import mock


from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext


from knfapp.common import ratelimit
from knfapp.news import core
from knfapp.common.timestamps import utc_now_iso
from knfapp.news.models import NewsComment, NewsLike, NewsPost, PollOption, PollVote
from knfapp.social.models import Activity, UserBlock
from knfapp.users import auth
from knfapp.users.models import User
from .utils import bearer, befriend, create_post, create_user


def _news_updates(queries):
    # The news_posts UPDATEs a request issued — a no-op set must
    # issue none (no recount, no stamp, no feed-tag churn)
    return [q["sql"] for q in queries if q["sql"].startswith('UPDATE "news_posts"')]








class LikeSetTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.client = Client()
        self.author = create_user(username="autorius")
        self.liker = create_user(username="draugas", email="d@knf.vu.lt")
        self.token = auth.mint_session(self.liker.id)
        self.post = create_post(author=self.author)

    def _set(self, body=None):
        kwargs = {"content_type": "application/json"}
        if body is not None:
            kwargs["data"] = body
        return bearer(self.client.post, f"/api/news/{self.post.id}/like", self.token, **kwargs)

    def test_the_coalesced_burst_lands_the_final_intent(self):
        # Three taps on an unliked post (true, false, true) reach the
        # wire as the in-flight task and the final intent — two
        # "like" requests. Two FLIPS would land unliked; two SETS land
        # exactly what the reader asked for last
        first = self._set({"liked": True}).json()
        second = self._set({"liked": True}).json()
        self.assertEqual((first["liked"], second["liked"]), (True, True))
        self.assertEqual(second["likes"], 1)
        self.assertEqual(NewsLike.objects.filter(post_id=self.post.id).count(), 1)

    def test_a_set_that_already_holds_writes_nothing(self):
        self._set({"liked": True})
        with CaptureQueriesContext(connection) as captured:
            again = self._set({"liked": True})
        self.assertEqual(again.json(), {"liked": True, "likes": 1})
        self.assertEqual(_news_updates(captured.captured_queries), [])
        # One gesture, one notification — a repeat never re-rings it
        self.assertEqual(Activity.objects.filter(user=self.author, kind="like").count(), 1)

        with CaptureQueriesContext(connection) as captured:
            none = bearer(self.client.post, f"/api/news/{create_post(author=self.author).id}/like",
                          self.token, data={"liked": False}, content_type="application/json")
        self.assertEqual(none.json(), {"liked": False, "likes": 0})
        self.assertEqual(_news_updates(captured.captured_queries), [])

    def test_unset_takes_the_like_and_its_notification_back(self):
        self._set({"liked": True})
        off = self._set({"liked": False}).json()
        self.assertEqual(off, {"liked": False, "likes": 0})
        self.assertEqual(Activity.objects.count(), 0)

    def test_a_non_boolean_target_is_a_400(self):
        for body in ({"liked": "yes"}, {"liked": 1}, {"liked": None, "x": 1}):
            response = self._set(body)
            if body.get("liked") is None:
                # An explicit null is "no target" — the legacy flip
                self.assertEqual(response.status_code, 200, body)
            else:
                self.assertEqual(response.status_code, 400, body)

    def test_no_body_keeps_the_legacy_flip(self):
        self.assertEqual(self._set().json()["liked"], True)
        self.assertEqual(self._set().json()["liked"], False)








class LockOrderTests(TestCase):

    # Every writer locks the post row BEFORE it touches a child row:
    # the spy runs inside core.lock_post and records what the child
    # tables held at that instant — a lock taken after the write
    # would see the write already there

    def setUp(self):
        ratelimit.reset()
        self.client = Client()
        self.author = create_user(username="autorius")
        self.reader = create_user(username="skaitytojas", email="s@knf.vu.lt")
        self.author_token = auth.mint_session(self.author.id)
        self.token = auth.mint_session(self.reader.id)
        self.post = create_post(author=self.author)
        self.seen = []

    def _spy(self, probe):
        real = core.lock_post

        def spy(post_id):
            self.seen.append(probe())
            return real(post_id)
        return spy

    def test_the_like_and_the_comment_lock_before_their_insert(self):
        with mock.patch.object(core, "lock_post", self._spy(
                lambda: NewsLike.objects.filter(post_id=self.post.id).count())):
            bearer(self.client.post, f"/api/news/{self.post.id}/like", self.token,
                   data={"liked": True}, content_type="application/json")
        self.assertEqual(self.seen, [0])

        self.seen.clear()
        with mock.patch.object(core, "lock_post", self._spy(
                lambda: NewsComment.objects.filter(post_id=self.post.id).count())):
            bearer(self.client.post, f"/api/news/{self.post.id}/comments", self.token,
                   data={"text": "Puiku"}, content_type="application/json")
        self.assertEqual(self.seen, [0])

    def test_the_comment_delete_and_the_vote_lock_first(self):
        created = bearer(self.client.post, f"/api/news/{self.post.id}/comments", self.token,
                         data={"text": "Puiku"}, content_type="application/json").json()
        with mock.patch.object(core, "lock_post", self._spy(
                lambda: NewsComment.objects.filter(post_id=self.post.id).count())):
            bearer(self.client.delete, f"/api/news/{self.post.id}/comments/{created['id']}", self.token)
        self.assertEqual(self.seen, [1])

        self.seen.clear()
        bearer(self.client.post, f"/api/news/{self.post.id}/poll", self.author_token,
               data={"title": "Kur?", "options": ["Kaune", "Vilniuje"]}, content_type="application/json")
        option = PollOption.objects.order_by("position").values_list("id", flat=True).first()
        with mock.patch.object(core, "lock_post", self._spy(lambda: PollVote.objects.count())):
            bearer(self.client.post, f"/api/news/{self.post.id}/poll/vote", self.token,
                   data={"option_id": option}, content_type="application/json")
        self.assertEqual(self.seen, [0])

    def test_both_post_deletes_lock_before_the_children_go(self):
        NewsLike.objects.create(user=self.reader, post=self.post, created_at=datetime.now(timezone.utc))
        with mock.patch.object(core, "lock_post", self._spy(
                lambda: NewsLike.objects.filter(post_id=self.post.id).count())):
            bearer(self.client.delete, f"/api/news/{self.post.id}", self.author_token)
        self.assertEqual(self.seen, [1])
        self.assertFalse(NewsPost.objects.filter(id=self.post.id).exists())

        # The wall delete binds the name at import — patched there
        wall = create_post(author=self.author, title="Siena")
        NewsLike.objects.create(user=self.reader, post=wall, created_at=datetime.now(timezone.utc))
        self.seen.clear()
        with mock.patch("knfapp.social.api.views.lock_post", self._spy(
                lambda: NewsLike.objects.filter(post_id=wall.id).count())):
            bearer(self.client.delete, f"/api/social/posts/{wall.id}", self.author_token)
        self.assertEqual(self.seen, [1])
        self.assertFalse(NewsPost.objects.filter(id=wall.id).exists())








class DeactivatedAuthorTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.client = Client()
        self.author = create_user(username="autorius")
        self.friend = create_user(username="draugas", email="d@knf.vu.lt")
        self.admin = create_user(username="vadovas", email="v@knf.vu.lt", role="admin")
        befriend(self.author, self.friend)
        self.friend_token = auth.mint_session(self.friend.id)
        self.post = create_post(author=self.author, title="Neapykantos kalba")

    def _deactivate(self, active=False):
        User.objects.filter(id=self.author.id).update(active=active, updated_at=datetime.now(timezone.utc))

    def _feed_titles(self, token):
        return [p["title"] for p in bearer(self.client.get, "/api/news", token).json()["posts"]]

    def test_every_per_post_route_answers_404_once_the_author_is_gone(self):
        self._deactivate()
        guest_paths = [
            ("get", f"/api/news/{self.post.id}", {}),
            ("get", f"/api/news/{self.post.id}/comments", {}),
            ("post", f"/api/news/{self.post.id}/share", {"content_type": "application/json"}),
        ]
        for method, path, kwargs in guest_paths:
            self.assertEqual(getattr(self.client, method)(path, **kwargs).status_code, 404, path)
        for method, path, kwargs in guest_paths + [
            ("post", f"/api/news/{self.post.id}/like", {"content_type": "application/json"}),
            ("post", f"/api/news/{self.post.id}/comments",
             {"data": {"text": "Vis dar čia?"}, "content_type": "application/json"}),
        ]:
            response = bearer(getattr(self.client, method), path, self.friend_token, **kwargs)
            self.assertEqual(response.status_code, 404, path)

    def test_the_feed_drops_the_post_and_its_tag_moves(self):
        first = bearer(self.client.get, "/api/news", self.friend_token)
        self.assertIn("Neapykantos kalba", [p["title"] for p in first.json()["posts"]])

        self._deactivate()
        # Deactivation writes the users row, never news_posts — the
        # old tag must not answer 304 for a page that held the post
        again = bearer(self.client.get, "/api/news", self.friend_token, HTTP_IF_NONE_MATCH=first["ETag"])
        self.assertEqual(again.status_code, 200)
        self.assertNotIn("Neapykantos kalba", [p["title"] for p in again.json()["posts"]])

        self._deactivate(active=True)
        self.assertIn("Neapykantos kalba", self._feed_titles(self.friend_token))

    def test_an_admin_keeps_the_moderation_view_and_official_word_stays(self):
        self._deactivate()
        admin_token = auth.mint_session(self.admin.id)
        self.assertEqual(bearer(self.client.get, f"/api/news/{self.post.id}", admin_token).status_code, 200)

        teacher = create_user(username="destytojas", email="t@knf.vu.lt", role="teacher")
        notice = create_post(author=teacher, source="faculty", post_type="announcement", title="Tvarkaraštis")
        User.objects.filter(id=teacher.id).update(active=False)
        self.assertEqual(self.client.get(f"/api/news/{notice.id}").status_code, 200)
        self.assertIn("Tvarkaraštis", self._feed_titles(self.friend_token))

    def test_the_gate_rule_itself(self):
        row = {"id": "p", "author_id": self.author.id, "source": "user", "is_public": 1}
        self.assertTrue(core.can_view_post({**row, "author__active": True}, None))
        self.assertFalse(core.can_view_post({**row, "author__active": False}, None))
        self.assertFalse(core.can_view_post({**row, "author__active": False},
                                            {"id": self.friend.id, "role": "student"}))
        self.assertTrue(core.can_view_post({**row, "author__active": False},
                                           {"id": self.admin.id, "role": "admin"}))
        # A row that did not join the author places no restriction
        self.assertTrue(core.can_view_post(row, None))








class FeedTruncationTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.client = Client()
        self.long = create_post(title="Ilgas", content="Ž" * 500)
        self.short = create_post(title="Trumpas", content="Trumpai.")

    def _posts(self, query=""):
        return {p["title"]: p for p in self.client.get(f"/api/news{query}").json()["posts"]}

    def test_the_phone_opts_into_cut_bodies_and_the_flag_tells_them_apart(self):
        cut = self._posts("?truncate=1")
        self.assertEqual(len(cut["Ilgas"]["content"]), core.SUMMARY_LENGTH)
        self.assertTrue(cut["Ilgas"]["truncated"])
        self.assertEqual(cut["Trumpas"]["content"], "Trumpai.")
        self.assertFalse(cut["Trumpas"]["truncated"])

    def test_without_the_flag_every_body_travels_whole(self):
        # The admin panel's list renders bodies — it never asks
        whole = self._posts()
        self.assertEqual(len(whole["Ilgas"]["content"]), 500)
        self.assertFalse(whole["Ilgas"]["truncated"])
        self.assertEqual(len(self._posts("?truncate=0")["Ilgas"]["content"]), 500)
        # The article route always serves the whole body
        article = self.client.get(f"/api/news/{self.long.id}").json()
        self.assertEqual((len(article["content"]), article["truncated"]), (500, False))

    def test_the_flag_is_part_of_the_tag(self):
        cut = self.client.get("/api/news?truncate=1")
        whole = self.client.get("/api/news")
        self.assertNotEqual(cut["ETag"], whole["ETag"])
        self.assertEqual(self.client.get("/api/news?truncate=1", HTTP_IF_NONE_MATCH=cut["ETag"]).status_code, 304)








class PollEndDateTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.client = Client()
        self.author = create_user(username="autorius")
        self.token = auth.mint_session(self.author.id)
        self.post = create_post(author=self.author)

    def _create(self, end_date):
        return bearer(self.client.post, f"/api/news/{self.post.id}/poll", self.token,
                      data={"title": "Kada?", "options": ["Rytoj", "Poryt"], "end_date": end_date},
                      content_type="application/json")

    def test_a_past_end_date_is_refused_with_its_code(self):
        for stamp in ("2020-01-01T00:00:00+00:00",
                      (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()):
            response = self._create(stamp)
            self.assertEqual(response.status_code, 400, stamp)
            self.assertEqual(response.json()["code"], "end_date_in_the_past")
        # Nothing was born: the post is still a plain post
        self.assertEqual(NewsPost.objects.get(id=self.post.id).post_type, "social")

    def test_a_future_end_date_is_accepted(self):
        response = self._create((datetime.now(timezone.utc) + timedelta(days=2)).isoformat())
        self.assertEqual(response.status_code, 201)








class BlockedTalliesTests(TestCase):

    # The thread drops a blocked pair's comments (and its total
    # follows); every counter this reader is shown must agree

    def setUp(self):
        ratelimit.reset()
        self.client = Client()
        self.me = create_user(username="skaitytojas")
        self.author = create_user(username="autorius", email="a@knf.vu.lt")
        self.troll = create_user(username="piktas", email="p@knf.vu.lt")
        self.others = [create_user(username=f"kitas{i}", email=f"k{i}@knf.vu.lt") for i in range(3)]
        befriend(self.me, self.author)
        self.token = auth.mint_session(self.me.id)
        self.post = create_post(author=self.author, title="Siena")
        for commenter in self.others + [self.troll]:
            bearer(self.client.post, f"/api/news/{self.post.id}/comments", auth.mint_session(commenter.id),
                   data={"text": f"nuo {commenter.username}"}, content_type="application/json")
            bearer(self.client.post, f"/api/news/{self.post.id}/like", auth.mint_session(commenter.id),
                   data={"liked": True}, content_type="application/json")
        UserBlock.objects.create(blocker=self.me, blocked=self.troll, created_at=utc_now_iso())

    def _card(self, path):
        return next(p for p in bearer(self.client.get, path, self.token).json()["posts"] if p["id"] == self.post.id)

    def test_every_counter_agrees_with_the_thread(self):
        thread = bearer(self.client.get, f"/api/news/{self.post.id}/comments", self.token).json()
        self.assertEqual(thread["total"], 3)

        self.assertEqual((self._card("/api/news")["comments"], self._card("/api/news")["likes"]), (3, 3))
        self.assertEqual(self._card("/api/social/feed")["comments"], 3)
        self.assertEqual(self._card(f"/api/social/posts?user_id={self.author.id}")["comments"], 3)
        detail = bearer(self.client.get, f"/api/news/{self.post.id}", self.token).json()
        self.assertEqual((detail["comments"], detail["likes"]), (3, 3))

        # The stored counters stay the global truth — a reader with
        # no block sees all four
        stranger = auth.mint_session(self.others[0].id)
        self.assertEqual(bearer(self.client.get, f"/api/news/{self.post.id}", stranger).json()["comments"], 4)

    def test_a_delete_answers_the_count_the_thread_shows(self):
        mine = bearer(self.client.post, f"/api/news/{self.post.id}/comments", self.token,
                      data={"text": "mano"}, content_type="application/json").json()
        response = bearer(self.client.delete, f"/api/news/{self.post.id}/comments/{mine['id']}", self.token)
        self.assertEqual(response.json()["comments"], 3)








class DerivedTitleTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.client = Client()
        self.token = auth.mint_session(create_user(username="rasytojas").id)

    def _title(self, content, path="/api/news"):
        return bearer(self.client.post, path, self.token, data={"content": content},
                      content_type="application/json").json()["title"]

    def test_a_long_first_line_is_cut_at_a_word_with_an_ellipsis(self):
        text = ("Pirmoji semestro savaitė praėjo labai greitai, o bendrabutyje jau visi "
                "geria kavą ir ruošiasi pirmiesiems atsiskaitymams")
        title = self._title(text)
        self.assertTrue(title.endswith("…"), title)
        self.assertLessEqual(len(title), core.DERIVED_TITLE_LENGTH)
        # Every word before the ellipsis is whole — the body starts with it
        self.assertTrue(text.startswith(title[:-1]), title)
        self.assertEqual(text[len(title) - 1], " ")

    def test_a_short_post_and_a_multi_line_post_keep_their_first_line_whole(self):
        self.assertEqual(self._title("Gal kas žinote, iki kada šiandien dirba biblioteka? 📚🙏"),
                         "Gal kas žinote, iki kada šiandien dirba biblioteka? 📚🙏")
        self.assertEqual(self._title("Ieškau komandos\nhakatonui savaitgalį", path="/api/social/posts"),
                         "Ieškau komandos")

    def test_one_unbroken_run_is_hard_cut(self):
        title = self._title("https://" + "a" * 120)
        self.assertEqual(len(title), core.DERIVED_TITLE_LENGTH)
        self.assertTrue(title.endswith("…"))
