# -----------------------------------------------------------
#  [*] Tests — wayfind guided captures (app/wayfind/captures
#      + stitch)
#
#  What the phase 2 capture contract promises, proved through
#  the real routes:
#
#    - a capture id is scoped per building like op ids: the
#      same client uuid opens a second building's session,
#      the bare id resolves while unique, and a genuinely
#      ambiguous bare id is a 409 naming buildingId — never
#      a guess.
#    - a frame PUT is idempotent per target (a re-shot frame
#      replaces), refuses unknown targets, poses that are not
#      three numbers, and any frame once the capture left
#      'uploading'; a picture past 60 MP (Pillow's hard
#      decompression-bomb stop, twice the 30 MP guard
#      uploads/routes.py installs) answers 413 too_large —
#      for frames and direct panorama uploads alike.
#    - finish gates on 8 frames (422 with the counts), on
#      status (409 on a second finish), and on its optional
#      body (400 bad_centre for a non-finite centreYawDeg).
#    - the stitcher composes a 12-frame ring into a 2048x1024
#      pano whose coverage says hfov 360 (the ring wraps),
#      whose vfov matches the frame geometry, and whose
#      CENTRE COLUMN is the finish body's centreYawDeg (the
#      client's first ACCEPTED frame) — falling back, without
#      a body, to the frame with the earliest updated_at, so
#      a re-shot first target hands the fallback centre to
#      the next frame. The anchor the alignment tool depends
#      on. The pano lands in the content-hash store with
#      heading_source 'auto' and the frames directory is
#      deleted.
#
#  The stitch tests need numpy (pinned in requirements.txt);
#  in a tests image built before that pin they skip instead
#  of failing the suite.
# -----------------------------------------------------------

import io
import json
import os
import time

import pytest
from PIL import Image, ImageDraw




# -----------------------------------------------------------
# helpers — a building, a capture, a synthetic frame
# -----------------------------------------------------------
#
# Frames are distinct solid colours with a drawn pattern so
# the stitch has real structure; poses sit on the r0 ring.
#
# Used by:
#   - every test below
# -----------------------------------------------------------

RING = [{"id": f"r0-{i}", "yawDeg": i * 30.0, "pitchDeg": 0.0} for i in range(12)]

COLORS = [(220, 40, 40), (40, 180, 60), (50, 90, 220), (230, 190, 40), (170, 60, 200), (40, 200, 200),
          (240, 120, 30), (120, 120, 120), (90, 40, 20), (200, 220, 240), (20, 60, 30), (240, 60, 140)]


def _building(client, headers, building_id="b1"):
    response = client.post("/api/wayfind/buildings", json={"id": building_id, "name": "B"}, headers=headers)
    assert response.status_code == 201, response.get_json()
    return building_id


def _capture(client, headers, building_id="b1", capture_id="cap-0000-0001", targets=RING):
    response = client.post(
        f"/api/wayfind/buildings/{building_id}/captures",
        json={"id": capture_id, "mode": "walls", "frameHfovDeg": 60, "targets": targets},
        headers=headers,
    )
    assert response.status_code == 201, response.get_json()
    return capture_id


def _frame_bytes(index, size=(480, 640)):
    image = Image.new("RGB", size, COLORS[index % 12])
    draw = ImageDraw.Draw(image)
    for k in range(index + 1):
        draw.rectangle([20 + k * 30, 40, 40 + k * 30, 120], fill=(255, 255, 255))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=90)
    return out.getvalue()


def _bomb_bytes():
    # 96 MP of 1-bit black — tiny on the wire, far past the
    # 60 MP stop Pillow enforces (twice the 30 MP guard
    # uploads/routes.py installs process-wide)
    image = Image.new("1", (12000, 8000))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _put_frame(client, headers, capture_id, target_id, yaw, index, pose_extra=None):
    data = {"yawDeg": str(yaw), "pitchDeg": "0", "rollDeg": "0",
            "file": (io.BytesIO(_frame_bytes(index)), "f.jpg")}
    if pose_extra:
        data.update(pose_extra)
    return client.put(f"/api/wayfind/captures/{capture_id}/frames/{target_id}",
                      data=data, content_type="multipart/form-data", headers=headers)


def _stitch_now(app, db, scoped_id):
    # The background worker (started by create_app) may claim
    # the queued row first; drive the stitch directly when it
    # has not, then wait out whoever runs it
    from app.wayfind.stitch import stitch_capture

    deadline = time.time() + 60
    while time.time() < deadline:
        row = db.execute("SELECT status FROM wf_captures WHERE id = ?", (scoped_id,)).fetchone()
        if row["status"] in ("done", "failed"):
            return row["status"]
        if row["status"] == "queued":
            with app.app_context():
                stitch_capture(scoped_id)
        else:
            time.sleep(0.2)
    raise AssertionError("stitch did not finish in time")




# -----------------------------------------------------------
# create — shape refusals and the per-building id scope
# -----------------------------------------------------------

def test_create_refuses_bad_shapes(client, admin):
    _, headers = admin
    _building(client, headers)

    bad = [
        {"id": "x", "mode": "walls", "targets": RING},                       # id too short
        {"id": "cap-0000-0001", "mode": "spiral", "targets": RING},          # bad mode
        {"id": "cap-0000-0001", "mode": "full", "targets": []},              # no targets
        {"id": "cap-0000-0001", "mode": "full", "targets": [{"id": "t"}]},   # target without pose
        {"id": "cap-0000-0001", "mode": "full", "frameHfovDeg": 500, "targets": RING},
    ]
    for body in bad:
        response = client.post("/api/wayfind/buildings/b1/captures", json=body, headers=headers)
        assert response.status_code == 400, (body, response.get_json())

    response = client.post("/api/wayfind/buildings/nope/captures",
                           json={"id": "cap-0000-0001", "mode": "full", "targets": RING}, headers=headers)
    assert response.status_code == 404


def test_create_is_idempotent_and_scoped_per_building(client, admin):
    _, headers = admin
    _building(client, headers, "b1")
    _building(client, headers, "b2")
    _capture(client, headers, "b1")

    # A replayed create answers the row as it stands, not 409 —
    # the outbox retries blind after a dropped connection
    response = client.post("/api/wayfind/buildings/b1/captures",
                           json={"id": "cap-0000-0001", "mode": "walls", "targets": RING}, headers=headers)
    assert response.status_code == 200
    assert response.get_json() == {"id": "cap-0000-0001", "status": "uploading"}

    # The SAME client id opens a session on the second building
    response = client.post("/api/wayfind/buildings/b2/captures",
                           json={"id": "cap-0000-0001", "mode": "full", "targets": RING}, headers=headers)
    assert response.status_code == 201

    # Now the bare id is ambiguous without buildingId, exact with it
    response = client.get("/api/wayfind/captures/cap-0000-0001", headers=headers)
    assert response.status_code == 409
    assert response.get_json()["code"] == "ambiguous"
    response = client.get("/api/wayfind/captures/cap-0000-0001?buildingId=b2", headers=headers)
    assert response.status_code == 200
    assert response.get_json()["expected"] == 12




# -----------------------------------------------------------
# frames — idempotent per target, gated on the plan and status
# -----------------------------------------------------------

def test_frame_upload_is_idempotent_per_target(client, admin):
    _, headers = admin
    _building(client, headers)
    _capture(client, headers)

    response = _put_frame(client, headers, "cap-0000-0001", "r0-0", 0.0, 0)
    assert response.status_code == 200
    assert response.get_json() == {"stored": 1, "expected": 12}

    # A re-shot target replaces its frame — the count stays
    response = _put_frame(client, headers, "cap-0000-0001", "r0-0", 2.5, 1)
    assert response.status_code == 200
    assert response.get_json() == {"stored": 1, "expected": 12}


def test_frame_upload_refusals(client, admin, db):
    _, headers = admin
    _building(client, headers)
    _capture(client, headers)

    # A target the plan never listed
    response = _put_frame(client, headers, "cap-0000-0001", "r0-99", 0.0, 0)
    assert response.status_code == 404
    assert response.get_json()["code"] == "unknown_target"

    # A pose that is not three numbers
    response = client.put("/api/wayfind/captures/cap-0000-0001/frames/r0-0",
                          data={"yawDeg": "up", "pitchDeg": "0", "rollDeg": "0",
                                "file": (io.BytesIO(_frame_bytes(0)), "f.jpg")},
                          content_type="multipart/form-data", headers=headers)
    assert response.status_code == 400
    assert response.get_json()["code"] == "bad_pose"

    # Not an image
    response = client.put("/api/wayfind/captures/cap-0000-0001/frames/r0-0",
                          data={"yawDeg": "0", "pitchDeg": "0", "rollDeg": "0",
                                "file": (io.BytesIO(b"not a jpeg"), "f.jpg")},
                          content_type="multipart/form-data", headers=headers)
    assert response.status_code == 400
    assert response.get_json()["code"] == "bad_image"

    # An unknown capture id
    response = _put_frame(client, headers, "cap-9999-9999", "r0-0", 0.0, 0)
    assert response.status_code == 404


def test_bomb_sized_frame_answers_413(client, admin):
    _, headers = admin
    _building(client, headers)
    _capture(client, headers)

    # Pillow raises DecompressionBombError inside open() for
    # this one — the promised 413 too_large, not a misleading
    # 400 bad_image
    response = client.put("/api/wayfind/captures/cap-0000-0001/frames/r0-0",
                          data={"yawDeg": "0", "pitchDeg": "0", "rollDeg": "0",
                                "file": (io.BytesIO(_bomb_bytes()), "f.png")},
                          content_type="multipart/form-data", headers=headers)
    assert response.status_code == 413
    body = response.get_json()
    assert body["code"] == "too_large"
    assert "60 megapixels" in body["error"]

    # Nothing was stored
    body = client.get("/api/wayfind/captures/cap-0000-0001", headers=headers).get_json()
    assert body["frames"] == 0


def test_bomb_sized_panorama_answers_413(client, admin):
    _, headers = admin
    _building(client, headers)

    # The direct panorama upload keeps the same promise
    response = client.post("/api/wayfind/buildings/b1/panoramas",
                           data={"file": (io.BytesIO(_bomb_bytes()), "p.png")},
                           content_type="multipart/form-data", headers=headers)
    assert response.status_code == 413
    body = response.get_json()
    assert body["code"] == "too_large"
    assert "60 megapixels" in body["error"]


def test_finish_gates_and_closes_the_intake(client, admin):
    _, headers = admin
    _building(client, headers)
    _capture(client, headers)
    for i in range(3):
        _put_frame(client, headers, "cap-0000-0001", f"r0-{i}", i * 30.0, i)

    # Fewer than 8 frames cannot stitch
    response = client.post("/api/wayfind/captures/cap-0000-0001/finish", headers=headers)
    assert response.status_code == 422
    body = response.get_json()
    assert body["code"] == "too_few_frames" and body["frames"] == 3 and body["required"] == 8

    for i in range(3, 12):
        _put_frame(client, headers, "cap-0000-0001", f"r0-{i}", i * 30.0, i)
    response = client.post("/api/wayfind/captures/cap-0000-0001/finish", headers=headers)
    assert response.status_code == 200
    assert response.get_json() == {"status": "queued"}

    # Closed: a late frame and a second finish both answer 409
    response = _put_frame(client, headers, "cap-0000-0001", "r0-0", 0.0, 0)
    assert response.status_code == 409
    assert response.get_json()["code"] == "not_uploading"
    response = client.post("/api/wayfind/captures/cap-0000-0001/finish", headers=headers)
    assert response.status_code == 409


def test_finish_refuses_a_centre_that_is_not_a_finite_number(client, admin):
    _, headers = admin
    _building(client, headers)
    _capture(client, headers)

    response = client.post("/api/wayfind/captures/cap-0000-0001/finish",
                           json={"centreYawDeg": "north"}, headers=headers)
    assert response.status_code == 400
    assert response.get_json()["code"] == "bad_centre"

    # The refusal left the capture open
    body = client.get("/api/wayfind/captures/cap-0000-0001", headers=headers).get_json()
    assert body["status"] == "uploading"


def test_capture_routes_need_an_editor_role(client, admin, actor):
    _, admin_headers = admin
    _, student_headers = actor
    _building(client, admin_headers)

    response = client.post("/api/wayfind/buildings/b1/captures",
                           json={"id": "cap-0000-0001", "mode": "full", "targets": RING},
                           headers=student_headers)
    assert response.status_code == 403
    response = client.get("/api/wayfind/captures/cap-0000-0001", headers=student_headers)
    assert response.status_code == 403




# -----------------------------------------------------------
# the stitch — ring to panorama, coverage, centre column
# -----------------------------------------------------------

@pytest.mark.slow
def test_full_ring_stitches_to_a_wrapped_panorama(app, client, admin, db):
    pytest.importorskip("numpy")
    _, headers = admin
    _building(client, headers)
    _capture(client, headers)
    for i in range(12):
        _put_frame(client, headers, "cap-0000-0001", f"r0-{i}", i * 30.0, i)
    response = client.post("/api/wayfind/captures/cap-0000-0001/finish", headers=headers)
    assert response.status_code == 200

    assert _stitch_now(app, db, "b1:cap-0000-0001") == "done"

    body = client.get("/api/wayfind/captures/cap-0000-0001", headers=headers).get_json()
    assert body["status"] == "done" and body["progressPct"] == 100
    coverage = body["report"]["coverage"]
    # 12 frames 30 degrees apart at 60-degree hfov wrap the yaw
    assert coverage["hfovDeg"] == 360.0
    # 480x640 portrait at hfov 60 gives vfov 2*atan(tan(30)*4/3) ~ 75
    assert 70 <= coverage["vfovDeg"] <= 82
    assert abs(coverage["vOffsetDeg"]) <= 3
    # No finish body sent, so the centre falls back to the
    # earliest-uploaded frame's yaw — r0-0 here
    assert coverage["centreYawDeg"] == 0.0

    pano = body["pano"]
    assert pano["width"] == 2048 and pano["height"] == 1024
    assert pano["centreYawDeg"] == 0.0
    served = client.get(pano["url"])
    assert served.status_code == 200
    image = Image.open(io.BytesIO(served.data))
    assert image.size == (2048, 1024)

    # heading_source 'auto' on the stored pano row, and the
    # frames directory is gone
    row = db.execute("SELECT heading_source, hfov_deg FROM wf_panoramas WHERE id = ?", (pano["id"],)).fetchone()
    assert row["heading_source"] == "auto" and row["hfov_deg"] == 360.0
    frames_dir = os.path.join(app.config["UPLOAD_DIR"], "wayfind", "captures", "b1_cap-0000-0001")
    assert not os.path.isdir(frames_dir)


@pytest.mark.slow
def test_finish_centre_yaw_centres_the_stitch(app, client, admin, db):
    pytest.importorskip("numpy")
    _, headers = admin
    _building(client, headers)
    _capture(client, headers)
    for i in range(12):
        _put_frame(client, headers, "cap-0000-0001", f"r0-{i}", i * 30.0, i)

    # The client names its first ACCEPTED frame's yaw — 90,
    # which is r0-3, NOT the first frame to reach the server
    response = client.post("/api/wayfind/captures/cap-0000-0001/finish",
                           json={"centreYawDeg": 90.0}, headers=headers)
    assert response.status_code == 200

    assert _stitch_now(app, db, "b1:cap-0000-0001") == "done"
    body = client.get("/api/wayfind/captures/cap-0000-0001", headers=headers).get_json()
    assert body["report"]["coverage"]["centreYawDeg"] == 90.0
    assert body["pano"]["centreYawDeg"] == 90.0

    # The canvas centre column really carries the yaw-90
    # frame (COLORS[3], a yellow), not the first upload's red
    served = client.get(body["pano"]["url"])
    image = Image.open(io.BytesIO(served.data))
    r, g, b = image.getpixel((1024, 512))
    assert r > 170 and g > 130 and b < 110


@pytest.mark.slow
def test_reshot_first_target_falls_back_to_the_earliest_upload(app, client, admin, db):
    pytest.importorskip("numpy")
    _, headers = admin
    _building(client, headers)
    _capture(client, headers)
    for i in range(12):
        _put_frame(client, headers, "cap-0000-0001", f"r0-{i}", i * 30.0, i)

    # Re-shooting the first target stamps a fresh updated_at,
    # so without a finish body the earliest surviving upload
    # is r0-1 (yaw 30) — the documented fallback ordering
    response = _put_frame(client, headers, "cap-0000-0001", "r0-0", 0.0, 0)
    assert response.status_code == 200
    response = client.post("/api/wayfind/captures/cap-0000-0001/finish", headers=headers)
    assert response.status_code == 200

    assert _stitch_now(app, db, "b1:cap-0000-0001") == "done"
    body = client.get("/api/wayfind/captures/cap-0000-0001", headers=headers).get_json()
    assert body["report"]["coverage"]["centreYawDeg"] == 30.0
    assert body["pano"]["centreYawDeg"] == 30.0


@pytest.mark.slow
def test_stitch_failure_lands_in_the_report(app, client, admin, db):
    pytest.importorskip("numpy")
    _, headers = admin
    _building(client, headers)
    _capture(client, headers)
    for i in range(8):
        _put_frame(client, headers, "cap-0000-0001", f"r0-{i}", i * 30.0, i)

    # Pull the frame files out from under the stitcher — a
    # dead-disk stand-in; the rows remain, the files do not
    frames_dir = os.path.join(app.config["UPLOAD_DIR"], "wayfind", "captures", "b1_cap-0000-0001")
    for name in os.listdir(frames_dir):
        os.unlink(os.path.join(frames_dir, name))
    response = client.post("/api/wayfind/captures/cap-0000-0001/finish", headers=headers)
    assert response.status_code == 200

    assert _stitch_now(app, db, "b1:cap-0000-0001") == "failed"
    body = client.get("/api/wayfind/captures/cap-0000-0001", headers=headers).get_json()
    assert body["status"] == "failed"
    assert "readable frames" in body["report"]["reason"]




# -----------------------------------------------------------
# compose_panorama — the pure math, no routes involved
# -----------------------------------------------------------

@pytest.mark.slow
def test_partial_arc_measures_its_own_coverage():
    numpy = pytest.importorskip("numpy")
    from app.wayfind.stitch import compose_panorama

    # Three frames spanning yaw 0..60 at hfov 60: the covered
    # arc is ~120 degrees, nowhere near a wrap
    frames = [
        {"image": Image.new("RGB", (320, 320), COLORS[i]), "yawDeg": i * 30.0, "pitchDeg": 0.0, "rollDeg": 0.0}
        for i in range(3)
    ]
    canvas, coverage = compose_panorama(frames, 60.0)

    assert canvas.shape == (1024, 2048, 3) and canvas.dtype == numpy.uint8
    assert 100 <= coverage["hfovDeg"] <= 140
    # Square frames at hfov 60 cover a ~60 degree band on the horizon
    assert 50 <= coverage["vfovDeg"] <= 75
    assert abs(coverage["vOffsetDeg"]) <= 3
    assert coverage["centreYawDeg"] == 0.0

    # The centre column carries the FIRST frame (red), not the
    # fill and not a later frame
    r, g, b = canvas[512, 1024]
    assert int(r) > 150 and int(r) > int(g) + 50 and int(r) > int(b) + 50


@pytest.mark.slow
def test_compose_centres_on_the_requested_yaw():
    pytest.importorskip("numpy")
    from app.wayfind.stitch import compose_panorama

    # Same three frames, but the caller asks for yaw 30 — the
    # finish body's centreYawDeg overrides the frame order
    frames = [
        {"image": Image.new("RGB", (320, 320), COLORS[i]), "yawDeg": i * 30.0, "pitchDeg": 0.0, "rollDeg": 0.0}
        for i in range(3)
    ]
    canvas, coverage = compose_panorama(frames, 60.0, centre_yaw_deg=30.0)

    assert coverage["centreYawDeg"] == 30.0
    # The centre column carries the yaw-30 frame (green), not
    # the first-listed red one
    r, g, b = canvas[512, 1024]
    assert int(g) > 120 and int(g) > int(r) + 40 and int(g) > int(b) + 40
