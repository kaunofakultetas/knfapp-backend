############################################################
#  [*] Regression tests — the feed's paging and cache
#
#  The ?before window pin (a scraper insert mid-paging must
#  not shift the OFFSET window), the parameter 400s, and
#  the ETag cycle: a 304 on the unchanged feed, a fresh tag
#  the moment a like lands (the counters term of the
#  watermark — stamps alone would lie), and the
#  private-vs-public cache scope.
############################################################


from datetime import datetime, timedelta, timezone


from django.test import Client, TestCase


from knfapp.common import ratelimit
from knfapp.users import auth
from .utils import bearer, create_post, create_user


class FeedPagingTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.client = Client()

    def test_parameter_garbage_is_a_400(self):
        for query in ("?page=0", "?page=abc", "?per_page=0", "?per_page=999", "?source=blogas", "?before=vakar"):
            self.assertEqual(self.client.get(f"/api/news{query}").status_code, 400, query)

    def test_before_pins_the_window_against_later_inserts(self):
        create_post(title="Sena", published_at=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat())
        pin = datetime.now(timezone.utc) - timedelta(hours=1)
        create_post(title="Nauja", published_at=datetime.now(timezone.utc).isoformat())

        body = self.client.get(f"/api/news?before={pin.isoformat()}").json()
        titles = {p["title"] for p in body["posts"]}
        self.assertEqual(titles, {"Sena"})

    def test_has_more_speaks_for_the_filtered_total(self):
        for i in range(3):
            create_post(title=f"P{i}")
        body = self.client.get("/api/news?per_page=2").json()
        self.assertEqual((body["total"], body["hasMore"], len(body["posts"])), (3, True, 2))


class FeedCacheTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.client = Client()
        self.user = create_user()
        self.token = auth.mint_session(self.user.id)
        self.post = create_post(title="Straipsnis")

    def test_the_etag_cycle_and_the_like_that_breaks_it(self):
        first = self.client.get("/api/news")
        tag = first["ETag"]
        self.assertTrue(tag.startswith('W/"'))

        unchanged = self.client.get("/api/news", HTTP_IF_NONE_MATCH=tag)
        self.assertEqual(unchanged.status_code, 304)

        # A like changes the feed's bytes without moving any stamp —
        # the watermark's counter term must break the 304
        bearer(self.client.post, f"/api/news/{self.post.id}/like", self.token,
               content_type="application/json")
        changed = self.client.get("/api/news", HTTP_IF_NONE_MATCH=tag)
        self.assertEqual(changed.status_code, 200)
        self.assertNotEqual(changed["ETag"], tag)

    def test_cache_scope_is_the_viewers(self):
        guest = self.client.get("/api/news")
        self.assertIn("public", guest["Cache-Control"])
        member = bearer(self.client.get, "/api/news", self.token)
        self.assertIn("private", member["Cache-Control"])

    def test_guest_and_member_tags_never_collide(self):
        guest = self.client.get("/api/news")["ETag"]
        member = bearer(self.client.get, "/api/news", self.token)["ETag"]
        self.assertNotEqual(guest, member)
