############################################################
#  [*] Regression tests — the poll lifecycle
#
#  Creation's stripped-then-counted options and one-per-post
#  rule, the end-date normalisation (edge-of-calendar stamps
#  earn a 400, not a 500), voting's cast/move/409 triangle
#  with recomputed tallies, and delete restoring the
#  post_type its source implies.
############################################################


from datetime import datetime, timedelta, timezone


from django.test import Client, TestCase


from knfapp.common import ratelimit
from knfapp.news.models import NewsPost, Poll, PollOption
from knfapp.users import auth
from .utils import bearer, create_post, create_user


class PollCreateTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.client = Client()
        self.author = create_user(username="autorius")
        self.token = auth.mint_session(self.author.id)
        self.post = create_post(author=self.author)

    def _create(self, token=None, **body):
        payload = {"title": "Kur švęsime?", "options": ["Kaune", "Vilniuje"], **body}
        return bearer(self.client.post, f"/api/news/{self.post.id}/poll", token or self.token,
                      data=payload, content_type="application/json")

    def test_options_are_stripped_before_the_count_check(self):
        response = self._create(options=["Kaune", "   ", ""])
        self.assertEqual(response.status_code, 400)

    def test_creation_flips_the_post_type_and_keeps_option_order(self):
        body = self._create(options=["Pirmas", "Antras", "Trečias"]).json()
        self.assertEqual([o["text"] for o in body["options"]], ["Pirmas", "Antras", "Trečias"])
        self.assertEqual(NewsPost.objects.get(id=self.post.id).post_type, "poll")
        self.assertIsNone(body["userVote"])

    def test_one_poll_per_post(self):
        self.assertEqual(self._create().status_code, 201)
        self.assertEqual(self._create().status_code, 409)

    def test_a_scraped_article_cannot_carry_a_poll(self):
        admin = create_user(username="vadovas", email="v@knf.vu.lt", role="admin")
        scraped = create_post(source_url="https://knf.vu.lt/n1")
        response = bearer(self.client.post, f"/api/news/{scraped.id}/poll", auth.mint_session(admin.id),
                          data={"title": "K?", "options": ["A", "B"]}, content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_end_date_is_normalised_and_the_calendar_edge_is_a_400(self):
        offset = self._create(end_date="2030-01-01T12:00:00+03:00").json()
        self.assertEqual(offset["endDate"], "2030-01-01T09:00:00+00:00")
        # Parses happily, overflows on the move to UTC — a 400,
        # never a 500
        self.post2 = create_post(author=self.author, title="Kitas")
        bad = bearer(self.client.post, f"/api/news/{self.post2.id}/poll", self.token,
                     data={"title": "K?", "options": ["A", "B"], "end_date": "0001-01-01T00:00:00+14:00"},
                     content_type="application/json")
        self.assertEqual(bad.status_code, 400)


class PollVoteTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.client = Client()
        self.author = create_user(username="autorius")
        self.voter = create_user(username="balsuotojas", email="b@knf.vu.lt")
        self.token = auth.mint_session(self.voter.id)
        self.post = create_post(author=self.author)
        bearer(self.client.post, f"/api/news/{self.post.id}/poll", auth.mint_session(self.author.id),
               data={"title": "Kur?", "options": ["Kaune", "Vilniuje"]}, content_type="application/json")
        self.options = list(PollOption.objects.order_by("position").values_list("id", flat=True))

    def _vote(self, option_id):
        return bearer(self.client.post, f"/api/news/{self.post.id}/poll/vote", self.token,
                      data={"option_id": option_id}, content_type="application/json")

    def test_cast_move_and_the_no_op_409(self):
        first = self._vote(self.options[0]).json()
        self.assertEqual(first["userVote"], self.options[0])
        self.assertEqual(first["totalVotes"], 1)

        moved = self._vote(self.options[1]).json()
        self.assertEqual(moved["userVote"], self.options[1])
        self.assertEqual(moved["totalVotes"], 1)          # a move is not a second vote
        self.assertEqual([o["votes"] for o in moved["options"]], [0, 1])

        self.assertEqual(self._vote(self.options[1]).status_code, 409)

    def test_a_foreign_option_is_a_plain_400(self):
        other = create_post(author=self.author, title="Kitas")
        bearer(self.client.post, f"/api/news/{other.id}/poll", auth.mint_session(self.author.id),
               data={"title": "K?", "options": ["X", "Y"]}, content_type="application/json")
        foreign = PollOption.objects.exclude(id__in=self.options).first().id
        self.assertEqual(self._vote(foreign).status_code, 400)

    def test_an_ended_poll_refuses_votes(self):
        Poll.objects.update(end_date=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
        self.assertEqual(self._vote(self.options[0]).status_code, 400)

    def test_an_unparseable_end_date_means_open(self):
        Poll.objects.update(end_date="rytoj po pietų")
        self.assertEqual(self._vote(self.options[0]).status_code, 200)


class PollDeleteTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.client = Client()
        self.author = create_user(username="autorius")
        self.token = auth.mint_session(self.author.id)

    def test_delete_restores_the_source_implied_type(self):
        post = create_post(author=self.author)
        bearer(self.client.post, f"/api/news/{post.id}/poll", self.token,
               data={"title": "K?", "options": ["A", "B"]}, content_type="application/json")
        response = bearer(self.client.delete, f"/api/news/{post.id}/poll", self.token)
        self.assertEqual((response.status_code, response.json()["postType"]), (200, "social"))
        self.assertEqual(NewsPost.objects.get(id=post.id).post_type, "social")
        self.assertEqual(Poll.objects.count(), 0)
