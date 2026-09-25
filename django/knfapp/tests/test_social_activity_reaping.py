############################################################
#  [*] Regression tests — activity rows follow their subject
#
#  Activity keys its subject by a bare id (no foreign key),
#  so nothing cascades: every delete path that removes what
#  a notification points at must reap it by hand. Pinned
#  here: a block wipes the pair's rows both ways (the
#  pending ask whose request is gone, the old accept, the
#  likes and comment excerpts), both post deletes take the
#  like and comment rows with the post, and a comment
#  delete re-points the author's row at the commenter's
#  newest SURVIVING comment — or drops it — so a moderated
#  insult never lives on verbatim in its victim's list. And a
#  block made before the reap existed still hides the pair's
#  rows on read — in the list and in the badge count.
############################################################


from datetime import timedelta


from django.test import Client, TestCase


from knfapp.common import ratelimit
from knfapp.common.timestamps import utc_now
from knfapp.social.activity import record_activity
from knfapp.common.timestamps import utc_now_iso
from knfapp.social.models import Activity, UserBlock
from knfapp.users import auth
from .utils import bearer, befriend, create_post, create_user








class BlockReapsThePairTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.client = Client()
        self.me = create_user(username="auka")
        self.them = create_user(username="priekabiautojas", email="p@knf.vu.lt")
        self.bystander = create_user(username="pasalinis", email="b@knf.vu.lt")
        self.token = auth.mint_session(self.me.id)

    def test_the_pairs_rows_go_both_ways_and_nobody_elses(self):
        # Their pending ask on my list, a like and a comment of
        # theirs on my post, and my old accept on THEIR list
        request = bearer(self.client.post, "/api/social/friends/request",
                         auth.mint_session(self.them.id), data={"user_id": self.me.id},
                         content_type="application/json").json()
        post = create_post(author=self.me)
        record_activity(self.me.id, "like", self.them.id, post.id, "Naujiena")
        record_activity(self.me.id, "comment", self.them.id, post.id, "idiotas")
        record_activity(self.them.id, "connect_accept", self.me.id)
        record_activity(self.me.id, "like", self.bystander.id, post.id, "Naujiena")
        self.assertEqual(Activity.objects.filter(subject_id=request["id"]).count(), 1)

        blocked = bearer(self.client.post, "/api/social/blocks", self.token,
                         data={"user_id": self.them.id}, content_type="application/json")
        self.assertEqual(blocked.status_code, 200)

        self.assertFalse(Activity.objects.filter(user=self.me, actor=self.them).exists())
        self.assertFalse(Activity.objects.filter(user=self.them, actor=self.me).exists())
        # A block is a pair, not a shroud
        self.assertTrue(Activity.objects.filter(user=self.me, actor=self.bystander).exists())

        # And the list the victim reads no longer carries the ask
        rows = bearer(self.client.get, "/api/social/activity", self.token).json()["notifications"]
        self.assertEqual([r["actor"]["id"] for r in rows], [self.bystander.id])








class PostDeleteReapsTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.client = Client()
        self.author = create_user(username="autorius")
        self.fan = create_user(username="gerbejas", email="g@knf.vu.lt")
        befriend(self.author, self.fan)
        self.author_token = auth.mint_session(self.author.id)
        self.fan_token = auth.mint_session(self.fan.id)

    def _engage(self, post):
        bearer(self.client.post, f"/api/news/{post.id}/like", self.fan_token,
               data={"liked": True}, content_type="application/json")
        bearer(self.client.post, f"/api/news/{post.id}/comments", self.fan_token,
               data={"text": "Puikus įrašas"}, content_type="application/json")
        self.assertEqual(Activity.objects.filter(subject_id=post.id).count(), 2)

    def test_the_news_delete_takes_the_notifications_with_the_post(self):
        post, other = create_post(author=self.author), create_post(author=self.author, title="Kitas")
        self._engage(post)
        self._engage(other)
        self.assertEqual(bearer(self.client.delete, f"/api/news/{post.id}", self.author_token).status_code, 200)
        self.assertEqual(Activity.objects.filter(subject_id=post.id).count(), 0)
        self.assertEqual(Activity.objects.filter(subject_id=other.id).count(), 2)

    def test_the_wall_delete_the_app_uses_does_too(self):
        post = create_post(author=self.author)
        self._engage(post)
        response = bearer(self.client.delete, f"/api/social/posts/{post.id}", self.author_token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Activity.objects.filter(subject_id=post.id).count(), 0)








class CommentDeleteRepointsTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.client = Client()
        self.author = create_user(username="autorius")
        self.harasser = create_user(username="piktas", email="p@knf.vu.lt")
        self.author_token = auth.mint_session(self.author.id)
        self.harasser_token = auth.mint_session(self.harasser.id)
        self.post = create_post(author=self.author)

    def _comment(self, text):
        return bearer(self.client.post, f"/api/news/{self.post.id}/comments", self.harasser_token,
                      data={"text": text}, content_type="application/json").json()

    def _row(self):
        return Activity.objects.filter(user=self.author, kind="comment", actor=self.harasser).first()

    def test_the_excerpt_follows_the_newest_survivor_then_the_row_goes(self):
        polite = self._comment("Įdomu")
        insult = self._comment("idiotas")
        self.assertEqual(self._row().subject_preview, "idiotas")
        # The author has read it — re-pointing must not re-ring it
        Activity.objects.filter(user=self.author).update(read=True)

        self.assertEqual(bearer(self.client.delete, f"/api/news/{self.post.id}/comments/{insult['id']}",
                                self.author_token).status_code, 200)
        row = self._row()
        self.assertEqual(row.subject_preview, "Įdomu")
        self.assertTrue(row.read)

        bearer(self.client.delete, f"/api/news/{self.post.id}/comments/{polite['id']}", self.harasser_token)
        self.assertIsNone(self._row())

    def test_deleting_an_older_comment_keeps_the_newest_excerpt(self):
        older = self._comment("Pirmas")
        self._comment("Antras")
        # Pin a gap between the two stamps so "newest" is unambiguous
        from knfapp.news.models import NewsComment
        NewsComment.objects.filter(id=older["id"]).update(created_at=utc_now() - timedelta(minutes=5))

        bearer(self.client.delete, f"/api/news/{self.post.id}/comments/{older['id']}", self.author_token)
        self.assertEqual(self._row().subject_preview, "Antras")








class BlockedActorReadFilterTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.client = Client()
        self.me = create_user(username="auka")
        self.troll = create_user(username="piktas", email="p@knf.vu.lt")
        self.friend = create_user(username="draugas", email="d@knf.vu.lt")
        self.token = auth.mint_session(self.me.id)
        post = create_post(author=self.me)
        record_activity(self.me.id, "like", self.troll.id, post.id, "Naujiena")
        record_activity(self.me.id, "comment", self.troll.id, post.id, "idiotas")
        record_activity(self.me.id, "like", self.friend.id, post.id, "Naujiena")
        # A block written straight to the table — the rows above
        # predate any reap, as a block made before it existed
        UserBlock.objects.create(blocker=self.me, blocked=self.troll, created_at=utc_now_iso())

    def test_the_list_and_the_badge_hide_the_blocked_actor(self):
        rows = bearer(self.client.get, "/api/social/activity", self.token).json()["notifications"]
        self.assertEqual([r["actor"]["id"] for r in rows], [self.friend.id])
        self.assertEqual(bearer(self.client.get, "/api/social/activity/unread", self.token).json()["count"], 1)
