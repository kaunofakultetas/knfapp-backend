############################################################
#  [*] Regression tests — the wayfind stitcher
#
#  The capture-to-panorama contract: a 12-frame ring at
#  hfov 60 composes into a 2048-wide pano CROPPED to the
#  band the frames covered, so the stored image, the
#  wf_panoramas row and the capture's pano block all say the
#  same thing (a full turn by that band, centred where the
#  band is — KNF-025: the old whole-sphere canvas labelled
#  with the captured arc rendered squashed by 180/vfov);
#  the measured arc rides in the report as capturedHfovDeg /
#  capturedVfovDeg. Its CENTRE COLUMN is the finish
#  body's centreYawDeg — falling back, without a body, to
#  the frame with the earliest updated_at, so a re-shot
#  first target hands the fallback centre to the next
#  frame (the anchor the alignment tool depends on). The
#  pano lands in the content-hash store with heading_source
#  'auto' and the frames directory is deleted; a failure
#  lands in the report with its reason and keeps the
#  frames. Driven synchronously through stitch_capture —
#  no worker thread runs under the test harness.
############################################################


import io
import json
import os


from PIL import Image


from knfapp.common.db import q1
from knfapp.wayfind.stitch import compose_panorama, stitch_capture
from .utils import bearer
from .test_wayfind_captures import COLORS, CaptureTestCase, RING, frame_bytes


class StitchEndToEndTests(CaptureTestCase):

    def _ring_capture(self, finish_body=None, reshoot_first=False):
        self._capture()
        for i in range(12):
            self._put_frame("cap-0000-0001", f"r0-{i}", i * 30.0, i)
        if reshoot_first:
            # Re-shooting the first target stamps a fresh
            # updated_at — the fallback-centre ordering pin
            self._put_frame("cap-0000-0001", "r0-0", 0.0, 0)
        response = self._finish(body=finish_body)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(stitch_capture("b1:cap-0000-0001"))
        return json.loads(bearer(self.client.get, "/api/wayfind/captures/cap-0000-0001",
                                 self.token).content)

    def test_a_full_ring_stitches_to_a_wrapped_panorama(self):
        body = self._ring_capture()
        self.assertEqual((body["status"], body["progressPct"]), ("done", 100))

        coverage = body["report"]["coverage"]
        # 12 frames 30 degrees apart at 60-degree hfov wrap the yaw
        self.assertEqual((coverage["hfovDeg"], coverage["capturedHfovDeg"]), (360.0, 360.0))
        # 480x640 portrait at hfov 60 gives vfov 2*atan(tan(30)*4/3) ~ 75
        self.assertTrue(70 <= coverage["vfovDeg"] <= 82, coverage)
        self.assertEqual(coverage["capturedVfovDeg"], coverage["vfovDeg"])
        self.assertLessEqual(abs(coverage["vOffsetDeg"]), 3)
        # No finish body sent — the centre falls back to the
        # earliest-uploaded frame's yaw, r0-0 here
        self.assertEqual(coverage["centreYawDeg"], 0.0)

        # The stored image IS the band: its rows are exactly the
        # vfov's share of the 1024-row sphere, and every place
        # that describes it says the same coverage
        pano = body["pano"]
        band_rows = round(coverage["vfovDeg"] / 180.0 * 1024)
        self.assertEqual((pano["width"], pano["height"]), (2048, band_rows))
        self.assertEqual((pano["hfovDeg"], pano["vfovDeg"], pano["vOffsetDeg"]),
                         (360.0, coverage["vfovDeg"], coverage["vOffsetDeg"]))
        served = self.client.get(pano["url"])
        self.assertEqual(served.status_code, 200)
        image = Image.open(io.BytesIO(b"".join(served.streaming_content)))
        self.assertEqual(image.size, (2048, band_rows))
        # 360 degrees across 2048 columns and vfov down the rows:
        # the image's own aspect agrees with its geometry
        self.assertAlmostEqual(image.width / image.height, 360.0 / coverage["vfovDeg"], delta=0.05)

        # heading_source 'auto' on the stored pano row, its
        # geometry the image's, and the frames directory is gone
        row = q1("SELECT heading_source, hfov_deg, vfov_deg FROM wf_panoramas WHERE id = %s", (pano["id"],))
        self.assertEqual((row["heading_source"], row["hfov_deg"], row["vfov_deg"]), ("auto", 360.0, coverage["vfovDeg"]))
        frames_path = os.path.join(self.tmp, "wayfind", "captures", "b1_cap-0000-0001")
        self.assertFalse(os.path.isdir(frames_path))

    def test_the_finish_body_centres_the_stitch(self):
        # The client names its first ACCEPTED frame's yaw — 90,
        # which is r0-3, NOT the first frame to reach the server
        body = self._ring_capture(finish_body={"centreYawDeg": 90.0})
        self.assertEqual(body["report"]["coverage"]["centreYawDeg"], 90.0)
        self.assertEqual(body["pano"]["centreYawDeg"], 90.0)

        # The canvas centre column really carries the yaw-90
        # frame (COLORS[3], a yellow), not the first upload's red
        served = self.client.get(body["pano"]["url"])
        image = Image.open(io.BytesIO(b"".join(served.streaming_content)))
        r, g, b = image.getpixel((1024, image.height // 2))
        self.assertTrue(r > 170 and g > 130 and b < 110, (r, g, b))

    def test_a_reshot_first_target_hands_the_fallback_centre_on(self):
        # Without a finish body the earliest SURVIVING upload is
        # r0-1 (yaw 30) — the documented fallback ordering
        body = self._ring_capture(reshoot_first=True)
        self.assertEqual(body["report"]["coverage"]["centreYawDeg"], 30.0)
        self.assertEqual(body["pano"]["centreYawDeg"], 30.0)

    def test_a_stitch_failure_lands_in_the_report(self):
        self._capture()
        for i in range(8):
            self._put_frame("cap-0000-0001", f"r0-{i}", i * 30.0, i)

        # Pull the frame files out from under the stitcher — a
        # dead-disk stand-in; the rows remain, the files do not
        frames_path = os.path.join(self.tmp, "wayfind", "captures", "b1_cap-0000-0001")
        for name in os.listdir(frames_path):
            os.unlink(os.path.join(frames_path, name))
        self.assertEqual(self._finish().status_code, 200)

        self.assertFalse(stitch_capture("b1:cap-0000-0001"))
        body = json.loads(bearer(self.client.get, "/api/wayfind/captures/cap-0000-0001",
                                 self.token).content)
        self.assertEqual(body["status"], "failed")
        self.assertIn("readable frames", body["report"]["reason"])


class ComposeTests(CaptureTestCase):

    def test_a_partial_arc_measures_its_own_coverage(self):
        import numpy

        # Three frames spanning yaw 0..60 at hfov 60: the covered
        # arc is ~120 degrees, nowhere near a wrap
        frames = [
            {"image": Image.new("RGB", (320, 320), COLORS[i]), "yawDeg": i * 30.0, "pitchDeg": 0.0, "rollDeg": 0.0}
            for i in range(3)
        ]
        canvas, coverage = compose_panorama(frames, 60.0)

        # The arc is REPORTED as captured, while the canvas stays a
        # full turn (fill beyond the arc) — so hfovDeg, the image's
        # own extent, is 360. (This test once asserted hfovDeg ~120
        # for this 2048-column canvas: the very mislabel KNF-025
        # names, which squashed the photo onto a 120° mesh)
        self.assertTrue(100 <= coverage["capturedHfovDeg"] <= 140, coverage)
        self.assertEqual(coverage["hfovDeg"], 360.0)
        # Square frames at hfov 60 cover a ~60 degree band on the
        # horizon — and the canvas is cut to exactly that band
        self.assertTrue(50 <= coverage["vfovDeg"] <= 75, coverage)
        self.assertEqual(canvas.shape, (round(coverage["vfovDeg"] / 180.0 * 1024), 2048, 3))
        self.assertEqual(canvas.dtype, numpy.uint8)
        self.assertLessEqual(abs(coverage["vOffsetDeg"]), 3)
        self.assertEqual(coverage["centreYawDeg"], 0.0)

        # The centre column carries the FIRST frame (red), not
        # the fill and not a later frame
        r, g, b = canvas[canvas.shape[0] // 2, 1024]
        self.assertTrue(int(r) > 150 and int(r) > int(g) + 50 and int(r) > int(b) + 50, (r, g, b))

    def test_a_band_above_the_horizon_keeps_its_offset(self):
        # Frames pitched 30 degrees up: the cropped band sits above
        # the horizon and says so — vOffsetDeg is what lets the
        # stage hang it there instead of on the horizon
        frames = [
            {"image": Image.new("RGB", (320, 320), COLORS[i]), "yawDeg": i * 30.0, "pitchDeg": 30.0, "rollDeg": 0.0}
            for i in range(12)
        ]
        canvas, coverage = compose_panorama(frames, 60.0)
        self.assertTrue(25 <= coverage["vOffsetDeg"] <= 35, coverage)
        self.assertEqual(canvas.shape[0], round(coverage["vfovDeg"] / 180.0 * 1024))

    def test_compose_centres_on_the_requested_yaw(self):
        # Same three frames, but the caller asks for yaw 30 — the
        # finish body's centreYawDeg overrides the frame order
        frames = [
            {"image": Image.new("RGB", (320, 320), COLORS[i]), "yawDeg": i * 30.0, "pitchDeg": 0.0, "rollDeg": 0.0}
            for i in range(3)
        ]
        canvas, coverage = compose_panorama(frames, 60.0, centre_yaw_deg=30.0)

        self.assertEqual(coverage["centreYawDeg"], 30.0)
        # The centre column carries the yaw-30 frame (green), not
        # the first-listed red one
        r, g, b = canvas[canvas.shape[0] // 2, 1024]
        self.assertTrue(int(g) > 120 and int(g) > int(r) + 40 and int(g) > int(b) + 40, (r, g, b))
