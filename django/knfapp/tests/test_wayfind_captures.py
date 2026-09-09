############################################################
#  [*] Regression tests — wayfind guided captures
#
#  The guided-capture contract, proved through the real
#  routes: a capture id is scoped per building like op ids
#  (the same client uuid opens a second building's session,
#  the bare id resolves while unique, a genuinely ambiguous
#  bare id is a 409 naming buildingId — never a guess); a
#  frame PUT is idempotent per target and multipart-parsed
#  by hand (Django fills nothing for PUT); a picture past
#  60 MP (Pillow's hard decompression-bomb stop, twice the
#  process-wide 30 MP guard) answers 413 too_large for
#  frames and direct panoramas alike; finish gates on 8
#  frames, on status and on its optional body. The SVG plan
#  store cuts scripts before hashing.
############################################################


import io
import json
import shutil
import tempfile


from django.test import Client, TestCase, override_settings
from django.test.client import BOUNDARY, MULTIPART_CONTENT, encode_multipart
from PIL import Image


from knfapp.common import ratelimit
from knfapp.users import auth
from .utils import bearer, create_user


RING = [{"id": f"r0-{i}", "yawDeg": i * 30.0, "pitchDeg": 0.0} for i in range(12)]

COLORS = [(220, 40, 40), (40, 180, 60), (50, 90, 220), (230, 190, 40), (170, 60, 200), (40, 200, 200),
          (240, 120, 30), (120, 120, 120), (90, 40, 20), (200, 220, 240), (20, 60, 30), (240, 60, 140)]


def frame_bytes(index, size=(480, 640)):
    image = Image.new("RGB", size, COLORS[index % 12])
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=90)
    return out.getvalue()


def bomb_bytes():
    # 96 MP of 1-bit black — tiny on the wire, far past the
    # 60 MP stop Pillow enforces (twice the process-wide guard)
    image = Image.new("1", (12000, 8000))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _file(blob, name="f.jpg"):
    handle = io.BytesIO(blob)
    handle.name = name
    return handle


class CaptureTestCase(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.tmp = tempfile.mkdtemp(prefix="knfapp-wayfind-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        overridden = override_settings(UPLOAD_DIR=self.tmp)
        overridden.enable()
        self.addCleanup(overridden.disable)

        self.admin = create_user(username="vadovas", role="admin")
        self.token = auth.mint_session(self.admin.id)
        self.client = Client()
        self._building("b1")

    def _building(self, building_id):
        response = bearer(self.client.post, "/api/wayfind/buildings", self.token,
                          data=json.dumps({"id": building_id, "name": "B"}),
                          content_type="application/json")
        self.assertEqual(response.status_code, 201, response.content)

    def _capture(self, building_id="b1", capture_id="cap-0000-0001", targets=RING, expect=201):
        response = bearer(self.client.post, f"/api/wayfind/buildings/{building_id}/captures", self.token,
                          data=json.dumps({"id": capture_id, "mode": "walls",
                                           "frameHfovDeg": 60, "targets": targets}),
                          content_type="application/json")
        self.assertEqual(response.status_code, expect, response.content)
        return response

    def _put_frame(self, capture_id, target_id, yaw, index, blob=None, extra=None):
        fields = {"yawDeg": str(yaw), "pitchDeg": "0", "rollDeg": "0",
                  "file": _file(blob if blob is not None else frame_bytes(index))}
        if extra:
            fields.update(extra)
        payload = encode_multipart(BOUNDARY, fields)
        return bearer(self.client.put, f"/api/wayfind/captures/{capture_id}/frames/{target_id}",
                      self.token, data=payload, content_type=MULTIPART_CONTENT)

    def _finish(self, capture_id="cap-0000-0001", body=None):
        return bearer(self.client.post, f"/api/wayfind/captures/{capture_id}/finish", self.token,
                      data=json.dumps(body or {}), content_type="application/json")


class CreateCaptureTests(CaptureTestCase):

    def test_create_refuses_bad_shapes(self):
        bad = [
            {"id": "x", "mode": "walls", "targets": RING},                      # id too short
            {"id": "cap-0000-0001", "mode": "spiral", "targets": RING},         # bad mode
            {"id": "cap-0000-0001", "mode": "full", "targets": []},             # no targets
            {"id": "cap-0000-0001", "mode": "full", "targets": [{"id": "t"}]},  # target without pose
            {"id": "cap-0000-0001", "mode": "full", "frameHfovDeg": 500, "targets": RING},
        ]
        for body in bad:
            response = bearer(self.client.post, "/api/wayfind/buildings/b1/captures", self.token,
                              data=json.dumps(body), content_type="application/json")
            self.assertEqual(response.status_code, 400, body)

        response = bearer(self.client.post, "/api/wayfind/buildings/nope/captures", self.token,
                          data=json.dumps({"id": "cap-0000-0001", "mode": "full", "targets": RING}),
                          content_type="application/json")
        self.assertEqual(response.status_code, 404)

    def test_create_is_idempotent_and_scoped_per_building(self):
        self._building("b2")
        self._capture("b1")

        # A replayed create answers the row as it stands, not
        # 409 — the outbox retries blind after a dropped
        # connection
        replay = self._capture("b1", expect=200)
        self.assertEqual(json.loads(replay.content), {"id": "cap-0000-0001", "status": "uploading"})

        # The SAME client id opens a session on the second building
        self._capture("b2", expect=201)

        # Now the bare id is ambiguous without buildingId, exact with it
        response = bearer(self.client.get, "/api/wayfind/captures/cap-0000-0001", self.token)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(json.loads(response.content)["code"], "ambiguous")
        response = bearer(self.client.get, "/api/wayfind/captures/cap-0000-0001?buildingId=b2", self.token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)["expected"], 12)


class FrameUploadTests(CaptureTestCase):

    def setUp(self):
        super().setUp()
        self._capture()

    def test_a_frame_put_is_idempotent_per_target(self):
        response = self._put_frame("cap-0000-0001", "r0-0", 0.0, 0)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(json.loads(response.content), {"stored": 1, "expected": 12})

        # A re-shot target replaces its frame — the count stays
        response = self._put_frame("cap-0000-0001", "r0-0", 2.5, 1)
        self.assertEqual(json.loads(response.content), {"stored": 1, "expected": 12})

    def test_the_frame_refusals(self):
        # A target the plan never listed
        response = self._put_frame("cap-0000-0001", "r0-99", 0.0, 0)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(json.loads(response.content)["code"], "unknown_target")

        # A pose that is not three numbers
        response = self._put_frame("cap-0000-0001", "r0-0", 0.0, 0, extra={"yawDeg": "up"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.content)["code"], "bad_pose")

        # Not an image
        response = self._put_frame("cap-0000-0001", "r0-0", 0.0, 0, blob=b"not a jpeg")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.content)["code"], "bad_image")

        # An unknown capture id
        response = self._put_frame("cap-9999-9999", "r0-0", 0.0, 0)
        self.assertEqual(response.status_code, 404)

    def test_a_bomb_sized_frame_answers_413_and_stores_nothing(self):
        # Pillow raises DecompressionBombError inside open() for
        # this one — the promised 413 too_large, not a misleading
        # 400 bad_image
        response = self._put_frame("cap-0000-0001", "r0-0", 0.0, 0, blob=bomb_bytes())
        self.assertEqual(response.status_code, 413)
        body = json.loads(response.content)
        self.assertEqual(body["code"], "too_large")
        self.assertIn("60 megapixels", body["error"])

        status = bearer(self.client.get, "/api/wayfind/captures/cap-0000-0001", self.token)
        self.assertEqual(json.loads(status.content)["frames"], 0)

    def test_a_bomb_sized_panorama_keeps_the_same_promise(self):
        response = bearer(self.client.post, "/api/wayfind/buildings/b1/panoramas", self.token,
                          data={"file": _file(bomb_bytes(), "p.png")})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(json.loads(response.content)["code"], "too_large")


class FinishTests(CaptureTestCase):

    def setUp(self):
        super().setUp()
        self._capture()

    def test_finish_gates_and_closes_the_intake(self):
        for i in range(3):
            self._put_frame("cap-0000-0001", f"r0-{i}", i * 30.0, i)

        # Fewer than 8 frames cannot stitch
        response = self._finish()
        self.assertEqual(response.status_code, 422)
        body = json.loads(response.content)
        self.assertEqual((body["code"], body["frames"], body["required"]), ("too_few_frames", 3, 8))

        for i in range(3, 12):
            self._put_frame("cap-0000-0001", f"r0-{i}", i * 30.0, i)
        response = self._finish()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content), {"status": "queued"})

        # Closed: a late frame and a second finish both answer 409
        response = self._put_frame("cap-0000-0001", "r0-0", 0.0, 0)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(json.loads(response.content)["code"], "not_uploading")
        self.assertEqual(self._finish().status_code, 409)

    def test_finish_refuses_a_centre_that_is_not_a_finite_number(self):
        response = self._finish(body={"centreYawDeg": "north"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.content)["code"], "bad_centre")

        # The refusal left the capture open
        status = bearer(self.client.get, "/api/wayfind/captures/cap-0000-0001", self.token)
        self.assertEqual(json.loads(status.content)["status"], "uploading")

    def test_capture_routes_need_an_editor_role(self):
        student = create_user(username="studentas")
        student_token = auth.mint_session(student.id)
        response = bearer(self.client.post, "/api/wayfind/buildings/b1/captures", student_token,
                          data=json.dumps({"id": "cap-0000-0002", "mode": "full", "targets": RING}),
                          content_type="application/json")
        self.assertEqual(response.status_code, 403)
        response = bearer(self.client.get, "/api/wayfind/captures/cap-0000-0001", student_token)
        self.assertEqual(response.status_code, 403)


class PlanUploadTests(CaptureTestCase):

    def test_the_svg_is_sanitised_before_it_is_hashed(self):
        svg = ('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script>'
               '<rect onclick="steal()" width="10" height="10"/>'
               '<a href="javascript:run()">x</a></svg>')
        response = bearer(self.client.post, "/api/wayfind/buildings/b1/plans", self.token,
                          data={"file": _file(svg.encode(), "p.svg")})
        self.assertEqual(response.status_code, 201, response.content)
        url = json.loads(response.content)["url"]

        served = self.client.get(url)
        self.assertEqual(served.status_code, 200)
        text = b"".join(served.streaming_content).decode()
        self.assertNotIn("<script", text)
        self.assertNotIn("onclick", text)
        self.assertNotIn("javascript:", text)
        self.assertEqual(served["Cache-Control"], "public, max-age=31536000, immutable")

    def test_a_stored_name_off_the_grammar_is_404_before_the_disk(self):
        for name in ("../secret.svg", "abc.svg", "a" * 64 + ".png"):
            self.assertEqual(self.client.get(f"/api/wayfind/plans/{name}").status_code, 404)
