############################################################
#  [*] Regression tests — control bytes in query parameters
#
#  Every text ?param reader wraps its request.GET.get in
#  clean_param (common/http.py), so a %00 in a query string
#  never reaches the driver: PostgreSQL's text type refuses
#  NUL with a DataError — an unauthenticated 500 on the
#  three public routes below — and SQLite binds the needle
#  NUL-terminated, which collapses a LIKE pattern to a bare
#  '%'. The module is engine-blind on purpose: the same
#  assertions hold on in-memory SQLite and on the
#  TEST_DATABASE_URL PostgreSQL pass, and only the latter
#  could see the 500 the wrap removed. One case per reader
#  named in clean_param's banner; the chat searches keep
#  the guarantee their old ad-hoc guards existed for — an
#  embedded NUL never pages the whole room or directory.
############################################################


import json
import uuid


from django.test import Client, TestCase, override_settings


from knfapp.common import ratelimit
from knfapp.common.timestamps import utc_now
from knfapp.schedule.models import ScheduleEvent, ScheduleEventGroup, ScheduleGroup
from knfapp.users import auth
from .utils import bearer, create_message, create_post, create_room, create_user


NUL = "\x00"


def _lesson(semester="2025-R", group="IS-1", teacher="J. Jonaitis"):
    stamp = utc_now()
    event = ScheduleEvent.objects.create(
        id=str(uuid.uuid4()), title="Programavimas", teacher=teacher, room="301",
        date=stamp.date(), time_start="09:00", time_end="10:30",
        semester=semester, last_seen_at=stamp, created_at=stamp,
    )
    group_row, _ = ScheduleGroup.objects.get_or_create(
        slug=f"slug-{group}", defaults={"group_name": group, "last_seen_at": stamp})
    ScheduleEventGroup.objects.create(event=event, group=group_row, last_seen_at=stamp)
    return event


class PublicRouteTests(TestCase):
    # The unauthenticated readers — on the pristine tree each
    # of the three schedule/news routes was a PostgreSQL 500
    # from a bare %00

    def setUp(self):
        self.client = Client()
        _lesson()

    def test_news_q_source_and_before(self):
        create_post(title="Labas rytas")
        create_post(title="Kitas")
        self.assertEqual(self.client.get("/api/news", {"q": NUL}).status_code, 200)
        # An embedded NUL is dropped and what is left is the needle
        titles = [p["title"] for p in self.client.get("/api/news", {"q": f"La{NUL}bas"}).json()["posts"]]
        self.assertEqual(titles, ["Labas rytas"])
        # ?source is whitelisted and ?before is a parsed stamp — the
        # cleaned value fails each gate with a 400, never a 500
        self.assertEqual(self.client.get("/api/news", {"source": NUL}).status_code, 400)
        self.assertEqual(self.client.get("/api/news", {"before": NUL}).status_code, 400)

    def test_schedule_events_group_teacher_semester_from_to(self):
        for name in ("group", "teacher", "semester"):
            with self.subTest(param=name):
                response = self.client.get("/api/schedule/events", {name: NUL})
                self.assertEqual(response.status_code, 200, response.content)
        # from/to are regex-gated dates: the cleaned value is empty, a 400
        for name in ("from", "to"):
            with self.subTest(param=name):
                self.assertEqual(self.client.get("/api/schedule/events", {name: NUL}).status_code, 400)

    def test_schedule_filters_semester(self):
        response = self.client.get("/api/schedule/filters", {"semester": NUL})
        self.assertEqual(response.status_code, 200, response.content)

    def test_schedule_week_group_semester_and_day(self):
        response = self.client.get("/api/schedule", {"group": NUL, "semester": NUL})
        self.assertEqual(response.status_code, 200, response.content)
        # day is digits-only: the cleaned value is empty, a 400
        self.assertEqual(self.client.get("/api/schedule", {"day": NUL}).status_code, 400)

    def test_social_public_readers(self):
        # user_id names an account: cleaned to nothing it is the
        # documented 400, never a lookup on a control byte
        self.assertEqual(self.client.get("/api/social/posts", {"user_id": NUL}).status_code, 400)
        self.assertEqual(self.client.get("/api/social/feed", {"before": NUL}).status_code, 400)

    def test_info_lang_and_section(self):
        # lang falls back to 'lt'; a section cleaned to nothing
        # means the whole handbook, not an unknown-section 400
        response = self.client.get("/api/info", {"lang": NUL, "section": NUL})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["lang"], "lt")


class MemberRouteTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        from knfapp.chat import events
        events.reset_socket_state()
        self.client = Client()
        self.tomas = create_user(username="tomas")
        self.ona = create_user(username="ona")
        self.token = auth.mint_session(self.tomas.id)
        self.room = create_room([self.tomas, self.ona])

    def _get(self, path, params=None):
        return bearer(self.client.get, path, self.token, data=params or {})

    def test_chat_message_search_strips_the_nul_and_never_pages_the_whole_room(self):
        create_message(self.room, self.ona, text="Slaptas tekstas")
        create_message(self.room, self.ona, text="Kitas tekstas")
        path = f"/api/chat/conversations/{self.room.id}/messages/search"
        response = self._get(path, {"q": f"Slap{NUL}tas"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual([m["text"] for m in response.json()["messages"]], ["Slaptas tekstas"])
        self.assertEqual(response.json()["total"], 1)
        # A NUL alone is blank once cleaned — the documented 400
        self.assertEqual(self._get(path, {"q": NUL}).status_code, 400)

    def test_chat_user_search_strips_the_nul_and_never_pages_the_directory(self):
        create_user(username="petras")
        response = self._get("/api/chat/users/search", {"q": f"on{NUL}a"})
        self.assertEqual(response.status_code, 200, response.content)
        # 'ona' alone — a needle collapsed to '%' would list petras too
        self.assertEqual([u["username"] for u in response.json()["users"]], ["ona"])
        # A NUL alone is under the 2-char floor once cleaned
        self.assertEqual(self._get("/api/chat/users/search", {"q": NUL}).json(), {"users": []})

    def test_chat_page_cursors_and_change_feed(self):
        create_message(self.room, self.ona, text="Labas")
        path = f"/api/chat/conversations/{self.room.id}/messages"
        for params in ({"before": NUL}, {"before_id": NUL}, {"after": NUL}, {"after_id": NUL}):
            with self.subTest(params=params):
                response = self._get(path, params)
                self.assertEqual(response.status_code, 200, response.content)
        # ?around names a message id — cleaned to nothing it is no
        # anchor at all (the plain newest page), never a lookup of
        # a control byte
        self.assertEqual(self._get(path, {"around": NUL}).status_code, 200)
        changes = f"/api/chat/conversations/{self.room.id}/changes"
        self.assertEqual(self._get(changes, {"since": NUL}).status_code, 400)

    def test_memes_q(self):
        response = self._get("/api/memes", {"q": NUL})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["memes"], [])

    def test_social_cursor_and_direction(self):
        # The id half of the keyset cursor, cleaned to nothing,
        # reads as no cursor — the first page, never a bind of NUL
        response = self._get("/api/social/activity", {"cursor": f"2026-01-01T00:00:00+00:00|{NUL}"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._get("/api/social/friends/requests", {"direction": NUL}).status_code, 400)


SECRET = "test-internal-secret"


@override_settings(ASSISTANT_INTERNAL_SECRET=SECRET)
class StaffAndInternalRouteTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.client = Client()
        self.admin = create_user(username="vadovas", role="admin")
        self.token = auth.mint_session(self.admin.id)

    def _get(self, path, params=None):
        return bearer(self.client.get, path, self.token, data=params or {})

    def test_admin_reports_status(self):
        self.assertEqual(self._get("/api/admin/reports", {"status": NUL}).status_code, 400)

    def test_scraper_status_source_and_status(self):
        response = self._get("/api/scraper/status", {"source": NUL, "status": NUL})
        self.assertEqual(response.status_code, 200, response.content)

    def test_assistant_review_rating(self):
        response = self._get("/api/admin/assistant/threads", {"rating": NUL})
        self.assertEqual(response.status_code, 200, response.content)

    def test_assistant_internal_user_id(self):
        response = self.client.get("/internal/assistant/threads/list", {"user_id": NUL},
                                   HTTP_X_INTERNAL_SECRET=SECRET)
        self.assertEqual(response.status_code, 400, response.content)
        response = self.client.get(f"/internal/assistant/threads/{uuid.uuid4()}/messages", {"user_id": NUL},
                                   HTTP_X_INTERNAL_SECRET=SECRET)
        self.assertEqual(response.status_code, 404, response.content)

    def test_wayfind_capture_building_id(self):
        # buildingId prefixes the capture's composite id — cleaned
        # to nothing it is the unscoped lookup, and no such capture
        response = self._get("/api/wayfind/captures/cap-0000-0001", {"buildingId": NUL})
        self.assertEqual(response.status_code, 404, response.content)
