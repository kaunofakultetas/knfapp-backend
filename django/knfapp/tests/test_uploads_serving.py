############################################################
#  [*] Regression tests — serving, thumbnails, orphan sweep
#
#  The read side of the upload store and its lifecycle:
#    - a served file runs no SQL at all (the dispatcher is
#      non-atomic — every image GET used to BEGIN/COMMIT
#      around nothing, KNF-135) and carries a year-long
#      immutable Cache-Control plus ETag/Last-Modified, so
#      a lapsed copy revalidates to a 304 (KNF-188);
#    - ?s=thumb serves ONE small derivative of a still
#      photo, made on first request, and falls back to the
#      original for everything else (KNF-136) — and the
#      derivative leaves disk with its photo;
#    - the orphan sweep reclaims old uploads nothing
#      references and never touches a referenced or a
#      young one (KNF-118).
############################################################


import io
import os
import shutil
import tempfile
from datetime import timedelta


from PIL import Image
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.utils.http import http_date


from knfapp.common import ratelimit
from knfapp.common.timestamps import utc_now
from knfapp.uploads import storage
from knfapp.uploads.models import Upload
from knfapp.users import auth
from knfapp.users.models import User
from .utils import bearer, create_message, create_post, create_room, create_user, register_upload


def _photo(width, height, fmt="JPEG", name="nuotrauka.jpg", mode="RGB"):
    buf = io.BytesIO()
    Image.new(mode, (width, height), (120, 0, 60) if mode == "RGB" else (120, 0, 60, 128)).save(buf, format=fmt)
    buf.seek(0)
    buf.name = name
    return buf


def _body(response):
    return b"".join(response.streaming_content) if response.streaming else response.content








############################################################
# ServeCachingTests
############################################################
#
# KNF-135 + KNF-188 over the wire.
############################################################

class ServeCachingTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.tmp = tempfile.mkdtemp(prefix="knfapp-serve-")
        storage._upload_dir = self.tmp
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.addCleanup(lambda: setattr(storage, "_upload_dir", None))
        self.user = create_user()
        self.token = auth.mint_session(self.user.id)
        self.client = Client()

    def _store(self, blob):
        response = self.client.post("/api/uploads", data={"file": blob},
                                    HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()["url"]


    def test_a_served_file_runs_no_sql_at_all(self):
        url = self._store(_photo(8, 8))
        # Inside the test's own transaction an atomic view would
        # still show up as SAVEPOINT/RELEASE pairs — a non-atomic
        # one issues nothing
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url)
            _body(response)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([q["sql"] for q in ctx.captured_queries], [])

    def test_a_delete_through_the_same_route_still_commits(self):
        url = self._store(_photo(8, 8))
        self.assertEqual(bearer(self.client.delete, url, self.token).status_code, 200)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertFalse(Upload.objects.exists())

    def test_the_answer_is_immutable_with_both_validators(self):
        url = self._store(_photo(8, 8))
        response = self.client.get(url)
        _body(response)
        self.assertEqual(response["Cache-Control"], "private, max-age=31536000, immutable")
        self.assertRegex(response["ETag"], r'^"[0-9a-f]{32}-o-[0-9a-f]+-[0-9a-f]+"$')
        self.assertTrue(response["Last-Modified"].endswith("GMT"))

    def test_a_current_copy_revalidates_to_a_304(self):
        url = self._store(_photo(8, 8))
        first = self.client.get(url)
        _body(first)
        etag = first["ETag"]

        by_tag = self.client.get(url, HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(by_tag.status_code, 304)
        self.assertEqual(by_tag.content, b"")
        self.assertEqual(by_tag["ETag"], etag)
        self.assertEqual(by_tag["Cache-Control"], "private, max-age=31536000, immutable")

        # The old route could not answer this with anything but a
        # full 200 — no validator existed to compare against
        future = http_date((utc_now() + timedelta(days=365)).timestamp())
        by_date = self.client.get(url, HTTP_IF_MODIFIED_SINCE=future)
        self.assertEqual(by_date.status_code, 304)

        stale = self.client.get(url, HTTP_IF_NONE_MATCH='"not-this-one"')
        self.assertEqual(stale.status_code, 200)
        self.assertEqual(_body(stale)[:3], b"\xff\xd8\xff")

    def test_a_range_answer_carries_the_same_cache_headers(self):
        url = self._store(_photo(8, 8))
        response = self.client.get(url, HTTP_RANGE="bytes=0-9")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(len(_body(response)), 10)
        self.assertEqual(response["Cache-Control"], "private, max-age=31536000, immutable")
        self.assertIn("ETag", response)








############################################################
# ThumbnailTests
############################################################
#
# KNF-136: one derivative size for still photos.
############################################################

class ThumbnailTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.tmp = tempfile.mkdtemp(prefix="knfapp-thumb-")
        storage._upload_dir = self.tmp
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.addCleanup(lambda: setattr(storage, "_upload_dir", None))
        self.user = create_user()
        self.token = auth.mint_session(self.user.id)
        self.client = Client()

    def _store(self, blob, kind=None):
        data = {"file": blob}
        if kind:
            data["kind"] = kind
        response = self.client.post("/api/uploads", data=data, HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()


    def test_a_big_photo_serves_a_small_derivative_made_once(self):
        stored = self._store(_photo(1200, 900))
        response = self.client.get(stored["url"] + "?s=thumb")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/jpeg")
        with Image.open(io.BytesIO(_body(response))) as thumb:
            self.assertEqual(thumb.size, (320, 240))

        derivative = os.path.join(self.tmp, "thumbs", stored["filename"])
        self.assertTrue(os.path.isfile(derivative))
        made_at = os.stat(derivative).st_mtime_ns

        # Served from disk the second time — not made again — under
        # a validator of its own
        again = self.client.get(stored["url"] + "?s=thumb")
        _body(again)
        self.assertEqual(os.stat(derivative).st_mtime_ns, made_at)
        self.assertIn('-t-', again["ETag"])
        original = self.client.get(stored["url"])
        _body(original)
        self.assertNotEqual(again["ETag"], original["ETag"])

    def test_a_transparent_photo_keeps_its_format_and_alpha(self):
        stored = self._store(_photo(800, 400, fmt="PNG", name="logo.png", mode="RGBA"))
        response = self.client.get(stored["url"] + "?s=thumb")
        self.assertEqual(response["Content-Type"], "image/png")
        with Image.open(io.BytesIO(_body(response))) as thumb:
            self.assertEqual((thumb.size, thumb.mode), ((320, 160), "RGBA"))

    def test_everything_else_serves_the_original(self):
        # Any other ?s value, a photo already that small, and a
        # document — the original bytes, and no derivative on disk
        big = self._store(_photo(1200, 900))
        other = self.client.get(big["url"] + "?s=4096")
        with Image.open(io.BytesIO(_body(other))) as image:
            self.assertEqual(image.size, (1200, 900))

        small = self._store(_photo(200, 100))
        tiny = self.client.get(small["url"] + "?s=thumb")
        with Image.open(io.BytesIO(_body(tiny))) as image:
            self.assertEqual(image.size, (200, 100))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "thumbs", small["filename"])))

        text = io.BytesIO("Tvarkaraštis rytoj.".encode())
        text.name = "planas.txt"
        doc = self._store(text, kind="file")
        served = self.client.get(doc["url"] + "?s=thumb")
        self.assertEqual(served["Content-Type"], "text/plain")
        self.assertEqual(_body(served), "Tvarkaraštis rytoj.".encode())

    def test_the_derivative_leaves_disk_with_its_photo(self):
        stored = self._store(_photo(1200, 900))
        _body(self.client.get(stored["url"] + "?s=thumb"))
        derivative = os.path.join(self.tmp, "thumbs", stored["filename"])
        self.assertTrue(os.path.isfile(derivative))

        self.assertEqual(bearer(self.client.delete, stored["url"], self.token).status_code, 200)
        self.assertFalse(os.path.exists(derivative))
        self.assertEqual(self.client.get(stored["url"] + "?s=thumb").status_code, 404)








############################################################
# OrphanSweepTests
############################################################
#
# KNF-118: reclaim what nothing references — and only that.
############################################################

class OrphanSweepTests(TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="knfapp-orphans-")
        storage._upload_dir = self.tmp
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.addCleanup(lambda: setattr(storage, "_upload_dir", None))
        self.owner = create_user()
        self.other = create_user(username="kitas", email="kitas@knf.vu.lt")

    def _old(self, name, days=8):
        Upload.objects.filter(filename=name).update(created_at=utc_now() - timedelta(days=days))
        return name

    def _on_disk(self, name):
        return os.path.exists(os.path.join(self.tmp, name))


    def test_an_old_unreferenced_upload_goes_and_frees_the_quota(self):
        orphan = self._old(register_upload(self.tmp, self.owner, blob=b"x" * 500))
        os.makedirs(os.path.join(self.tmp, "thumbs"))
        open(os.path.join(self.tmp, "thumbs", orphan), "wb").write(b"t")

        self.assertEqual(storage.sweep_orphan_uploads(), (1, 500))
        self.assertFalse(self._on_disk(orphan))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "thumbs", orphan)))
        self.assertFalse(Upload.objects.filter(filename=orphan).exists())

    def test_every_kind_of_reference_keeps_its_file(self):
        room = create_room([self.owner, self.other])
        avatar = self._old(register_upload(self.tmp, self.owner))
        cover = self._old(register_upload(self.tmp, self.owner))
        photo = self._old(register_upload(self.tmp, self.owner))
        document = self._old(register_upload(self.tmp, self.owner, ext="pdf"))
        poster = self._old(register_upload(self.tmp, self.owner))
        in_gallery = self._old(register_upload(self.tmp, self.owner))
        in_card = self._old(register_upload(self.tmp, self.owner))

        User.objects.filter(id=self.owner.id).update(avatar_url=f"/api/uploads/{avatar}")
        create_post(author=self.owner, image_url=f"/api/uploads/{cover}")
        # The absolute same-origin form the chat send accepts counts too
        create_message(room, self.owner, image_url=f"https://knf.vu.lt/api/uploads/{photo}")
        create_message(room, self.owner, attachment_url=f"/api/uploads/{document}",
                       attachment_meta={"thumbnailUrl": f"/api/uploads/{poster}"})
        create_message(room, self.other, gallery=[{"url": f"/api/uploads/{in_gallery}"}])
        create_message(room, self.other, link_preview={"imageUrl": f"/api/uploads/{in_card}"})

        self.assertEqual(storage.sweep_orphan_uploads(), (0, 0))
        for name in (avatar, cover, photo, document, poster, in_gallery, in_card):
            self.assertTrue(self._on_disk(name), name)
            self.assertTrue(Upload.objects.filter(filename=name).exists(), name)

    def test_a_link_in_free_text_or_map_data_keeps_its_file(self):
        # A pasted upload link in a post body, a comment or a
        # message, and a room photo in the map's entity data,
        # are references too — the sweep must never read them
        # as nothing
        from knfapp.news.models import NewsComment
        from knfapp.wayfind.models import WfBuilding, WfEntity

        room = create_room([self.owner, self.other])
        in_body = self._old(register_upload(self.tmp, self.owner))
        in_comment = self._old(register_upload(self.tmp, self.owner))
        in_text = self._old(register_upload(self.tmp, self.owner))
        on_map = self._old(register_upload(self.tmp, self.owner))

        post = create_post(author=self.owner, content=f"Nuotrauka: https://knf.vu.lt/api/uploads/{in_body}")
        NewsComment.objects.create(id="c1", post_id=post.id, user=self.other,
                                   text=f"Štai /api/uploads/{in_comment}", created_at=utc_now())
        create_message(room, self.other, text=f"Žiūrėk /api/uploads/{in_text}")
        WfBuilding.objects.create(id="knf", name="KNF", created_at=utc_now(), updated_at=utc_now())
        WfEntity.objects.create(building_id="knf", kind="room", id="r1", revision=1, updated_at=utc_now(),
                                data={"name": "101", "photos": [f"/api/uploads/{on_map}"]})

        self.assertEqual(storage.sweep_orphan_uploads(), (0, 0))
        for name in (in_body, in_comment, in_text, on_map):
            self.assertTrue(self._on_disk(name), name)

    def test_a_young_unreferenced_upload_is_left_alone(self):
        # An offline outbox or an unfinished post may still hold it
        young = register_upload(self.tmp, self.owner)
        self.assertEqual(storage.sweep_orphan_uploads(), (0, 0))
        self.assertTrue(self._on_disk(young))

    def test_a_dry_run_counts_and_touches_nothing(self):
        orphan = self._old(register_upload(self.tmp, None, blob=b"x" * 300))
        self.assertEqual(storage.sweep_orphan_uploads(dry_run=True), (1, 300))
        self.assertTrue(self._on_disk(orphan))
        self.assertTrue(Upload.objects.filter(filename=orphan).exists())
