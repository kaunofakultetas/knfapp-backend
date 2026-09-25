############################################################
#  [*] Regression tests — the upload routes over the wire
#
#  Store → serve → delete against a throwaway upload
#  directory: the url shape the app persists, the ownership
#  row behind the quota (and the users-row lock the quota
#  sum runs under), the owner-or-admin delete rule,
#  the 409 for a file a record still shows, the serve
#  route's refusal to be a filesystem probe, and the full
#  round trip of EVERY kind the gates admit — a voice note
#  once stored fine and then could neither be fetched nor
#  deleted. Plus the storage sink's own contract —
#  delete_upload's four outcomes, decided by the ownership
#  row and the still-referenced guard, not by the caller.
############################################################


import io
import os
import shutil
import tempfile
import uuid


from PIL import Image
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext


from knfapp.common import ratelimit
from knfapp.common.timestamps import utc_now_iso
from knfapp.uploads import storage
from knfapp.uploads.models import Upload
from knfapp.users import auth
from knfapp.users.models import User
from .utils import bearer, create_message, create_room, create_user, register_upload


def _jpg_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (120, 0, 60)).save(buf, format="JPEG")
    buf.seek(0)
    buf.name = "nuotrauka.jpg"
    return buf


def _blob(name, data):
    buf = io.BytesIO(data)
    buf.name = name
    return buf


class UploadFlowTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.tmp = tempfile.mkdtemp(prefix="knfapp-uploads-")
        # The module caches the resolved dir per process — point it
        # at the throwaway one for the duration of each test
        storage._upload_dir = self.tmp
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.addCleanup(lambda: setattr(storage, "_upload_dir", None))

        self.user = create_user()
        self.token = auth.mint_session(self.user.id)
        self.client = Client()

    def _upload(self, token=None, **extra):
        return self.client.post("/api/uploads", data={"file": _jpg_bytes(), **extra},
                                HTTP_AUTHORIZATION=f"Bearer {token or self.token}")


    def test_a_photo_lands_reencoded_under_a_uuid_name(self):
        response = self._upload()
        self.assertEqual(response.status_code, 201)
        body = response.json()
        # The relative url the client persists, and the row the
        # quota counts
        self.assertRegex(body["url"], r"^/api/uploads/[0-9a-f]{32}\.jpg$")
        self.assertEqual(body["mime"], "image/jpeg")
        self.assertEqual((body["width"], body["height"]), (8, 8))
        self.assertTrue(body["preview"].startswith("data:image/jpeg;base64,"))
        row = Upload.objects.get(filename=body["filename"])
        self.assertEqual(row.user_id, self.user.id)
        self.assertEqual(row.byte_size, body["size"])

        served = self.client.get(body["url"])
        self.assertEqual(served.status_code, 200)
        # Browser cache only — chat photos ride this public route.
        # A year and immutable, not the old 24 h: the bytes behind
        # a uuid name never change, and the old window came with no
        # validator, so a lapsed copy could only be re-downloaded
        # whole (KNF-188; test_uploads_serving pins the 304 path)
        self.assertEqual(served["Cache-Control"], "private, max-age=31536000, immutable")

    def test_anonymous_uploads_are_refused(self):
        response = self.client.post("/api/uploads", data={"file": _jpg_bytes()})
        self.assertEqual(response.status_code, 401)

    def test_garbage_bytes_answer_the_slug_not_a_500(self):
        blob = io.BytesIO(b"tikrai ne paveikslas")
        blob.name = "photo.jpg"
        response = self.client.post("/api/uploads", data={"file": blob},
                                    HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.assertEqual((response.status_code, response.json()["code"]), (400, "bad_file_content"))

    def test_a_text_document_is_stored_as_sent(self):
        blob = io.BytesIO("Tvarkaraštis rytoj.".encode())
        blob.name = "planas.txt"
        response = self.client.post("/api/uploads", data={"file": blob, "kind": "file"},
                                    HTTP_AUTHORIZATION=f"Bearer {self.token}")
        body = response.json()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(body["mime"], "text/plain")
        self.assertEqual(body["name"], "planas.txt")
        served = self.client.get(body["url"])
        self.assertEqual(b"".join(served.streaming_content).decode(), "Tvarkaraštis rytoj.")

    def test_the_quota_answers_413_before_writing(self):
        Upload.objects.create(id=str(uuid.uuid4()), filename=f"{uuid.uuid4().hex}.jpg",
                              user=self.user, byte_size=storage.UPLOAD_QUOTA_BYTES,
                              created_at=utc_now_iso())
        response = self._upload()
        self.assertEqual((response.status_code, response.json()["code"]), (413, "quota_exceeded"))
        # Refused before the bytes hit the directory or a row lands
        self.assertEqual(os.listdir(self.tmp), [])
        self.assertEqual(Upload.objects.filter(user_id=self.user.id).count(), 1)

    def test_the_quota_check_holds_the_account_row_first(self):
        # The quota sum runs under SELECT … FOR UPDATE on the caller's
        # users row, so overlapping uploads queue per account instead
        # of each seeing the whole remaining budget. SQLite has no row
        # locks and Django drops the FOR UPDATE clause there, so the
        # literal clause cannot be asserted — what is pinned is the
        # statement and its place: the one-column read of the users
        # row lands before the byte_size aggregate, and the uploads
        # INSERT comes after both (413 before writing)
        with CaptureQueriesContext(connection) as captured:
            response = self._upload()
        self.assertEqual(response.status_code, 201)
        sql = [q["sql"] for q in captured.captured_queries]
        lock = [i for i, s in enumerate(sql) if s.startswith('SELECT "users"."id" AS "id" FROM "users" WHERE "users"."id" = ')]
        total = [i for i, s in enumerate(sql) if 'SUM("uploads"."byte_size")' in s]
        insert = [i for i, s in enumerate(sql) if s.startswith('INSERT INTO "uploads"')]
        self.assertEqual((len(lock), len(total), len(insert)), (1, 1, 1), sql)
        self.assertLess(lock[0], total[0])
        self.assertLess(total[0], insert[0])

    def test_every_kind_round_trips_store_serve_delete(self):
        # One signature-only body per kind (the gates read the
        # header, nothing decodes a video): 201 → GET 200 with the
        # canonical mime → DELETE 200 → gone from disk, row and
        # route. The serve/delete name gate must admit every
        # extension the upload gate stores
        ftyp = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
        cases = [
            ("image", _jpg_bytes(), "image/jpeg"),
            ("file", _blob("planas.txt", "Tvarkaraštis rytoj.".encode()), "text/plain"),
            ("video", _blob("filmas.mp4", ftyp), "video/mp4"),
            ("audio", _blob("balsas.m4a", ftyp), "audio/mp4"),
        ]
        for kind, blob, mime in cases:
            with self.subTest(kind=kind):
                response = self.client.post("/api/uploads", data={"file": blob, "kind": kind},
                                            HTTP_AUTHORIZATION=f"Bearer {self.token}")
                self.assertEqual(response.status_code, 201, response.content)
                body = response.json()
                self.assertEqual(body["mime"], mime)

                served = self.client.get(body["url"])
                # A refusal is a JSON body worth quoting; a 200 streams
                self.assertEqual(served.status_code, 200, b"" if served.streaming else served.content)
                self.assertEqual(served["Content-Type"], mime)

                self.assertEqual(bearer(self.client.delete, body["url"], self.token).status_code, 200)
                self.assertEqual(self.client.get(body["url"]).status_code, 404)
                self.assertFalse(Upload.objects.filter(filename=body["filename"]).exists())
                self.assertFalse(os.path.exists(os.path.join(self.tmp, body["filename"])))

    def test_serve_refuses_names_it_could_not_have_written(self):
        for name in ("../knfapp.sqlite3", "..%2Fdb", "x.jpg", "a" * 32 + ".exe"):
            response = self.client.get(f"/api/uploads/{name}")
            self.assertIn(response.status_code, (400, 404), name)

    def test_delete_is_owner_or_admin_only(self):
        uploaded = self._upload().json()["filename"]

        stranger = create_user(username="kitas", email="kitas@knf.vu.lt")
        stranger_token = auth.mint_session(stranger.id)
        admin = create_user(username="vadovas", email="vadovas@knf.vu.lt", role="admin")
        admin_token = auth.mint_session(admin.id)

        self.assertEqual(bearer(self.client.delete, f"/api/uploads/{uploaded}", stranger_token).status_code, 403)
        self.assertEqual(bearer(self.client.delete, f"/api/uploads/{uploaded}", self.token).status_code, 200)
        self.assertEqual(Upload.objects.filter(filename=uploaded).count(), 0)

        # An unknown name and someone else's ownerless file look the
        # same from outside — admins get through
        second = self._upload().json()["filename"]
        self.assertEqual(bearer(self.client.delete, f"/api/uploads/{second}", admin_token).status_code, 200)
        self.assertEqual(bearer(self.client.delete, "/api/uploads/" + "0" * 32 + ".jpg", stranger_token).status_code, 404)

    def test_a_file_still_in_use_answers_409_unless_an_admin_asks(self):
        uploaded = self._upload().json()["filename"]
        User.objects.filter(id=self.user.id).update(avatar_url=f"/api/uploads/{uploaded}")

        # The owner's own avatar still shows it — the row and the
        # bytes stay
        response = bearer(self.client.delete, f"/api/uploads/{uploaded}", self.token)
        self.assertEqual((response.status_code, response.json()["code"]), (409, "still_referenced"))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, uploaded)))
        self.assertTrue(Upload.objects.filter(filename=uploaded).exists())

        # Moderation pulls it from under the avatar
        admin = create_user(username="vadovas", email="vadovas@knf.vu.lt", role="admin")
        self.assertEqual(bearer(self.client.delete, f"/api/uploads/{uploaded}",
                                auth.mint_session(admin.id)).status_code, 200)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, uploaded)))
        self.assertFalse(Upload.objects.filter(filename=uploaded).exists())


class DeleteUploadSinkTests(TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="knfapp-sink-")
        storage._upload_dir = self.tmp
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.addCleanup(lambda: setattr(storage, "_upload_dir", None))
        self.owner = create_user()
        self.other = create_user(username="kitas", email="kitas@knf.vu.lt")

    def _on_disk(self, name):
        return os.path.exists(os.path.join(self.tmp, name))

    def test_an_own_file_goes_with_its_row(self):
        name = register_upload(self.tmp, self.owner)
        self.assertEqual(storage.delete_upload(f"/api/uploads/{name}", self.owner.id), "removed")
        self.assertFalse(self._on_disk(name))
        self.assertFalse(Upload.objects.filter(filename=name).exists())

    def test_a_foreign_file_is_forbidden_and_untouched(self):
        name = register_upload(self.tmp, self.other)
        self.assertEqual(storage.delete_upload(f"/api/uploads/{name}", self.owner.id), "forbidden")
        self.assertTrue(self._on_disk(name))
        self.assertTrue(Upload.objects.filter(filename=name, user_id=self.other.id).exists())

    def test_a_rowless_file_is_forbidden_for_a_user_and_removed_for_an_admin(self):
        name = f"{uuid.uuid4().hex}.jpg"
        open(os.path.join(self.tmp, name), "wb").write(b"x")
        self.assertEqual(storage.delete_upload(name, self.owner.id), "forbidden")
        self.assertTrue(self._on_disk(name))
        self.assertEqual(storage.delete_upload(name, self.owner.id, admin=True), "removed")
        self.assertFalse(self._on_disk(name))

    def test_an_ownerless_row_is_nobodys(self):
        # What an erasure's SET_NULL leaves — not even a caller
        # with no id of its own may match it
        name = register_upload(self.tmp, None)
        self.assertEqual(storage.delete_upload(name, self.owner.id), "forbidden")
        self.assertEqual(storage.delete_upload(name, None), "forbidden")
        self.assertTrue(self._on_disk(name))

    def test_a_file_another_users_avatar_shows_stays_unless_an_admin_asks(self):
        name = register_upload(self.tmp, self.owner)
        User.objects.filter(id=self.other.id).update(avatar_url=f"/api/uploads/{name}")
        self.assertEqual(storage.delete_upload(name, self.owner.id), "referenced")
        self.assertTrue(self._on_disk(name))
        self.assertTrue(Upload.objects.filter(filename=name).exists())

        self.assertEqual(storage.delete_upload(name, self.owner.id, admin=True), "removed")
        self.assertFalse(self._on_disk(name))
        self.assertFalse(Upload.objects.filter(filename=name).exists())

    def test_the_guard_reads_the_chat_json_columns_too(self):
        # A gallery item and a link card live inside JSON — the
        # substring match must see through the cast
        room = create_room([self.owner, self.other])
        in_gallery = register_upload(self.tmp, self.owner)
        in_card = register_upload(self.tmp, self.owner)
        create_message(room, self.other, gallery=[{"url": f"/api/uploads/{in_gallery}"}])
        create_message(room, self.other, link_preview={"imageUrl": f"/api/uploads/{in_card}"})
        self.assertEqual(storage.delete_upload(in_gallery, self.owner.id), "referenced")
        self.assertEqual(storage.delete_upload(in_card, self.owner.id), "referenced")

    def test_a_value_that_is_not_a_stored_upload_reads_as_missing(self):
        # A scraped cover, a foreign path, a non-string — nothing
        # of ours to delete, and never an error for the caller
        self.assertEqual(storage.delete_upload("https://knf.vu.lt/naujiena.jpg", self.owner.id), "missing")
        self.assertEqual(storage.delete_upload("/api/memes/file/memas.jpg", self.owner.id), "missing")
        self.assertEqual(storage.delete_upload(None, self.owner.id), "missing")
        # A row whose file already went still frees the quota
        name = register_upload(self.tmp, self.owner)
        os.unlink(os.path.join(self.tmp, name))
        self.assertEqual(storage.delete_upload(name, self.owner.id), "missing")
        self.assertFalse(Upload.objects.filter(filename=name).exists())
