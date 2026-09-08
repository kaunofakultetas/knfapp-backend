############################################################
#  [*] Wayfind captures — guided panorama capture intake
#
#  Where a phone's guided 360 capture session lands. The
#  admin walks a target plan (the kit's CaptureHud), the app
#  shoots one JPEG per target and uploads each with the pose
#  the tracker measured (yawDeg clockwise from above, pitchDeg
#  positive up, rollDeg positive = tilted clockwise from
#  upright portrait); when the admin finishes, the capture is
#  queued and stitch.py's worker composes the frames into one
#  equirectangular panorama stored through the same
#  content-hash store the direct panorama upload uses.
#
#  A capture id is a CLIENT-generated uuid, stored scoped as
#  '<building_id>:<client id>' exactly like wf_ops — the same
#  client id works on a second building, and a replayed
#  create applies once. The frame URLs carry only the bare
#  id: the scoped row is found by suffix, and on the (uuid-
#  unlikely) collision of one bare id across two buildings
#  the caller disambiguates with a buildingId form/query
#  field — 409 'ambiguous' says so.
#
#  Frames are named by ROLE (<targetId>.jpg under
#  UPLOAD_DIR/wayfind/captures/<building>_<id>/), not by
#  content: a re-shot target REPLACES its file and its
#  wf_capture_frames row, so the PUT is idempotent per
#  target. Every accepted frame goes through the panorama
#  upload's re-encode gate, capped at 2048 px on the long
#  edge. After finish the capture is read-only — a late
#  frame answers 409.
#
#  Tables: wf_captures, wf_capture_frames (migration v65).
#  Every route is in swagger/swagger.yaml. Registered on the
#  wayfind blueprint from routes.py (register_capture_routes).
#
#    POST /api/wayfind/buildings/<id>/captures            — open a capture session (admin/curator)
#    PUT  /api/wayfind/captures/<id>/frames/<targetId>    — store one frame + its pose (admin/curator)
#    POST /api/wayfind/captures/<id>/finish               — queue the stitch (admin/curator)
#    GET  /api/wayfind/captures/<id>                      — status / report / the finished pano (admin/curator)
############################################################


import io
import json
import os
import re

from flask import jsonify, request
from PIL import Image, ImageOps

from app.auth.routes import rate_limit, require_role
from app.database import get_db, utc_now_iso
from app.uploads.routes import MAX_IMAGE_PIXELS as BOMB_GUARD_PIXELS
from app.wayfind.store import store_dir, write_replace


# Same pair as routes.py's EDITOR_ROLES — kept local because
# this module must not import routes.py at its top (routes.py
# imports THIS module at its bottom to register the routes)
EDITOR_ROLES = ("admin", "curator")

CAPTURE_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}\Z")
TARGET_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,32}\Z")

# A capture frame is a single phone photo: the pano viewer
# never needs its native width, so the 2048 px cap of the
# generic upload path applies (the 8192 cap is for whole
# panoramas only)
FRAME_MAX_EDGE = 2048

# The real decode ceiling. uploads/routes.py installs its
# 30 MP MAX_IMAGE_PIXELS as Pillow's PROCESS-WIDE bomb guard,
# and Pillow refuses to open anything past TWICE that (between
# 1x and 2x it only warns) — so 60 MP is the largest frame
# this route can ever decode, and the DecompressionBombError
# catch below is what actually delivers the 413. Derived, not
# copied, so the constant cannot drift from the guard; the
# explicit width*height comparison stays as the belt for the
# day the guard moves.
FRAME_MAX_PIXELS = 2 * BOMB_GUARD_PIXELS

# The 44-target 'full' plan plus headroom; anything past this
# is a malformed client, not a bigger sphere
MAX_TARGETS = 64

# Fewer frames than one wall row's worth cannot cover enough
# of the sphere to be worth stitching
MIN_FINISH_FRAMES = 8









############################################################
# register_capture_routes
############################################################
#
# Hangs the four capture routes on the wayfind blueprint.
# Registration lives in a function (called from routes.py's
# bottom) instead of a blueprint import here, so the module
# import order stays acyclic: routes.py finishes defining its
# helpers, then pulls this module in.
#
# Used by:
#   - app/wayfind/routes.py — once, at module bottom
############################################################

def register_capture_routes(bp):
    bp.add_url_rule("/buildings/<building_id>/captures", view_func=create_capture, methods=["POST"])
    bp.add_url_rule("/captures/<capture_id>/frames/<target_id>", view_func=upload_capture_frame, methods=["PUT"])
    bp.add_url_rule("/captures/<capture_id>/finish", view_func=finish_capture, methods=["POST"])
    bp.add_url_rule("/captures/<capture_id>", view_func=get_capture, methods=["GET"])









############################################################
# frames_dir
############################################################
#
# The directory one capture's frame files live in:
# UPLOAD_DIR/wayfind/captures/<building>_<client id>. The
# scoped row id's ':' becomes '_' — collision-free, since
# neither id regex admits an underscore — so the name is
# filesystem-safe everywhere.
#
# Used by:
#   - upload_capture_frame (below)
#   - app/wayfind/stitch.py — reading the frames, deleting
#     the directory after a successful stitch
############################################################

def frames_dir(scoped_id: str) -> str:
    return os.path.join(store_dir("captures"), scoped_id.replace(":", "_"))









############################################################
# create_capture
############################################################
#
# POST /api/wayfind/buildings/<building_id>/captures
#
# {"id": "<client uuid>", "nodeId"?: "n1", "mode": "full"|"walls",
#  "frameHfovDeg"?: 60, "targets": [{"id": "r0-0", "yawDeg": 0,
#  "pitchDeg": 0}, …]}
#
# Opens one capture session in status 'uploading' and answers
# 201 {id, status}. The target list is the CLIENT's plan
# (wayfindengine's planTargets) — stored verbatim so the
# stitcher and the GET report against what the phone actually
# aimed at, whatever plan version shot it. A replayed create
# for an id this building has seen answers 200 with the row
# as it stands instead of 409 — the sync outbox retries
# blind after a dropped connection.
#
# Used by:
#   - the app's guided-capture screen through wayfindsync's
#     upload queue
############################################################

@require_role(*EDITOR_ROLES)
@rate_limit("wayfind_captures", max_attempts=60)
def create_capture(building_id):

    # Imported here, not at module top: routes.py imports this
    # module at its bottom, so a top-level import would cycle
    from app.wayfind.routes import ENTITY_ID_RE, _load_building

    building = _load_building(building_id)
    if building is None:
        return jsonify({"error": "Unknown building", "code": "not_found"}), 404


    # STEP 1: the shape — id, mode, hfov, and every target
    # ====================================================
    body = request.get_json(silent=True) or {}
    capture_id = body.get("id")
    if not isinstance(capture_id, str) or not CAPTURE_ID_RE.match(capture_id):
        return jsonify({"error": "id must be 8-64 letters, digits or dashes", "code": "bad_id"}), 400
    mode = body.get("mode")
    if mode not in ("full", "walls"):
        return jsonify({"error": "mode must be 'full' or 'walls'", "code": "bad_mode"}), 400
    hfov = body.get("frameHfovDeg", 60)
    if not isinstance(hfov, (int, float)) or hfov != hfov or not (10 <= hfov <= 170):
        return jsonify({"error": "frameHfovDeg must be a number between 10 and 170", "code": "bad_hfov"}), 400
    node_id = body.get("nodeId")
    if node_id is not None and not (isinstance(node_id, str) and ENTITY_ID_RE.match(node_id)):
        return jsonify({"error": "nodeId must be a short id", "code": "bad_node"}), 400

    targets = body.get("targets")
    if not isinstance(targets, list) or not (1 <= len(targets) <= MAX_TARGETS):
        return jsonify({"error": f"targets must list 1-{MAX_TARGETS} targets", "code": "bad_targets"}), 400
    seen_ids = set()
    clean_targets = []
    for target in targets:
        if not isinstance(target, dict) or not isinstance(target.get("id"), str) or not TARGET_ID_RE.match(target["id"]):
            return jsonify({"error": "every target needs a short id", "code": "bad_targets"}), 400
        if target["id"] in seen_ids:
            return jsonify({"error": f"duplicate target id {target['id']}", "code": "bad_targets"}), 400
        yaw = target.get("yawDeg")
        pitch = target.get("pitchDeg")
        if not isinstance(yaw, (int, float)) or not isinstance(pitch, (int, float)) or yaw != yaw or pitch != pitch or not (-90 <= pitch <= 90):
            return jsonify({"error": "every target needs yawDeg and pitchDeg (pitch within ±90)", "code": "bad_targets"}), 400
        seen_ids.add(target["id"])
        clean_targets.append({"id": target["id"], "yawDeg": float(yaw), "pitchDeg": float(pitch)})


    # STEP 2: one row, scoped like wf_ops; a replay answers
    # the existing row instead of a 409 the outbox would
    # treat as a rejection
    # =====================================================
    db = get_db()
    scoped = f"{building_id}:{capture_id}"
    existing = db.execute("SELECT status FROM wf_captures WHERE id = ?", (scoped,)).fetchone()
    if existing is not None:
        return jsonify({"id": capture_id, "status": existing["status"]}), 200

    now = utc_now_iso()
    db.execute(
        """
        INSERT INTO wf_captures (id, building_id, node_id, mode, frame_hfov_deg, targets, expected,
                                 status, progress_pct, report, pano_id, created_by, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'uploading', 0, NULL, NULL, ?, ?, ?)
        """,
        (scoped, building_id, node_id, mode, float(hfov), json.dumps(clean_targets, ensure_ascii=False),
         len(clean_targets), request.user["id"], now, now),
    )
    db.commit()

    return jsonify({"id": capture_id, "status": "uploading"}), 201









############################################################
# _resolve_capture
############################################################
#
# The wf_captures row for a bare client id, or (None, an
# error response pair). The row id is scoped
# '<building>:<id>', so the lookup is by suffix; an optional
# buildingId (form field or query arg — the frame PUT is
# multipart, the others are not) makes it exact. Two
# buildings holding the same bare id without a buildingId is
# a 409 'ambiguous' — practically unreachable with client
# uuids, but never guessed at.
#
# Used by:
#   - upload_capture_frame / finish_capture / get_capture
#     (below)
############################################################

def _resolve_capture(capture_id):

    if not CAPTURE_ID_RE.match(capture_id or ""):
        return None, (jsonify({"error": "Unknown capture", "code": "not_found"}), 404)

    db = get_db()
    building_id = request.form.get("buildingId") or request.args.get("buildingId")
    if building_id:
        row = db.execute("SELECT * FROM wf_captures WHERE id = ?", (f"{building_id}:{capture_id}",)).fetchone()
        if row is None:
            return None, (jsonify({"error": "Unknown capture", "code": "not_found"}), 404)
        return row, None

    # Neither id regex admits ':', so '%:<id>' matches exactly
    # the rows whose bare id IS capture_id
    rows = db.execute("SELECT * FROM wf_captures WHERE id LIKE ?", (f"%:{capture_id}",)).fetchall()
    if not rows:
        return None, (jsonify({"error": "Unknown capture", "code": "not_found"}), 404)
    if len(rows) > 1:
        return None, (jsonify({"error": "This capture id exists in several buildings — pass buildingId", "code": "ambiguous"}), 409)
    return rows[0], None









############################################################
# upload_capture_frame
############################################################
#
# PUT /api/wayfind/captures/<capture_id>/frames/<target_id>
#
# Multipart: file (the photo), yawDeg, pitchDeg, rollDeg (the
# tracker's pose at the shutter — P1 conventions), optional
# buildingId. Idempotent per target: a re-upload replaces the
# file and the row. The picture goes through the panorama
# path's re-encode gate (verify, EXIF-upright, RGB, JPEG q85)
# but capped at 2048 px on the long edge, and the write is
# atomic (.part + replace) so the stitcher can never read
# half a frame. A frame past FRAME_MAX_PIXELS (60 MP —
# Pillow's hard bomb stop) answers 413 too_large. Answers
# {stored, expected}. 409 once the capture left 'uploading'.
#
# Used by:
#   - wayfindsync's upload queue — UploadItem kind 'frame'
#     through SyncTransport.uploadFrame
############################################################

@require_role(*EDITOR_ROLES)
@rate_limit("wayfind_frames", max_attempts=600)
def upload_capture_frame(capture_id, target_id):

    capture, error = _resolve_capture(capture_id)
    if error:
        return error
    if capture["status"] != "uploading":
        return jsonify({"error": "This capture is closed — frames are only accepted before finish", "code": "not_uploading"}), 409


    # STEP 1: the target must be on this capture's plan, and
    # the pose must be three real numbers
    # ======================================================
    targets = json.loads(capture["targets"])
    if not any(target["id"] == target_id for target in targets):
        return jsonify({"error": "Unknown target for this capture", "code": "unknown_target"}), 404
    yaw = _form_float("yawDeg")
    pitch = _form_float("pitchDeg")
    roll = _form_float("rollDeg")
    if yaw is None or pitch is None or roll is None or not (-90 <= pitch <= 90):
        return jsonify({"error": "yawDeg, pitchDeg and rollDeg are required numbers (pitch within ±90)", "code": "bad_pose"}), 400
    upload = request.files.get("file")
    if upload is None:
        return jsonify({"error": "file is required", "code": "no_file"}), 400


    # STEP 2: decode, verify and re-encode — the same gate as
    # upload_panorama, at the 2048 px frame cap. Pillow raises
    # DecompressionBombError inside open() for anything past
    # FRAME_MAX_PIXELS, so the oversize refusal is the catch,
    # not the comparison
    # =======================================================
    raw = upload.read()
    try:
        probe = Image.open(io.BytesIO(raw))
        probe.verify()
        image = Image.open(io.BytesIO(raw))
        if image.width * image.height > FRAME_MAX_PIXELS:
            return jsonify({"error": f"Image too large. Max {FRAME_MAX_PIXELS // (1000 * 1000)} megapixels", "code": "too_large"}), 413
        image = ImageOps.exif_transpose(image)
        image = image.convert("RGB")
        if max(image.width, image.height) > FRAME_MAX_EDGE:
            scale = FRAME_MAX_EDGE / max(image.width, image.height)
            image = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.LANCZOS)
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=85, optimize=True, progressive=True)
        blob = out.getvalue()
        width, height = image.width, image.height
    except Image.DecompressionBombError:
        return jsonify({"error": f"Image too large. Max {FRAME_MAX_PIXELS // (1000 * 1000)} megapixels", "code": "too_large"}), 413
    except Exception:
        return jsonify({"error": "Not a readable image", "code": "bad_image"}), 400


    # STEP 3: replace the file, upsert the row, count what is
    # stored
    # =======================================================
    directory = frames_dir(capture["id"])
    os.makedirs(directory, exist_ok=True)
    if not write_replace(directory, f"{target_id}.jpg", blob):
        return jsonify({"error": "The frame could not be stored", "code": "write_failed"}), 500

    db = get_db()
    db.execute(
        """
        INSERT OR REPLACE INTO wf_capture_frames (capture_id, target_id, yaw_deg, pitch_deg, roll_deg, bytes, width, height, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (capture["id"], target_id, yaw, pitch, roll, len(blob), width, height, utc_now_iso()),
    )
    db.execute("UPDATE wf_captures SET updated_at = ? WHERE id = ?", (utc_now_iso(), capture["id"]))
    db.commit()

    stored = db.execute("SELECT COUNT(*) AS n FROM wf_capture_frames WHERE capture_id = ?", (capture["id"],)).fetchone()["n"]
    return jsonify({"stored": stored, "expected": capture["expected"]})









############################################################
# finish_capture
############################################################
#
# POST /api/wayfind/captures/<capture_id>/finish
#
# Optional JSON body: {"centreYawDeg": <number>} — the yaw of
# the session's chronologically FIRST ACCEPTED frame (the
# manifest's firstYawDeg). The stitcher centres the panorama
# on it; parked inside the report JSON until then, because
# wf_captures grows no column for one number. Without it the
# stitcher falls back to the frame with the earliest
# updated_at.
#
# Closes the intake and hands the capture to the stitch
# worker: 409 unless the capture is still 'uploading', 400
# bad_centre when the body's centreYawDeg is not a finite
# number, 422 with the counts when fewer than
# MIN_FINISH_FRAMES frames arrived (the phone should re-drain
# its queue first). On success the row flips to 'queued' and
# the worker is nudged so the stitch starts inside a second
# instead of at the next 3 s poll.
#
# Used by:
#   - the app's guided-capture screen, once every frame's
#     upload has drained
############################################################

@require_role(*EDITOR_ROLES)
@rate_limit("wayfind_captures", max_attempts=60)
def finish_capture(capture_id):

    capture, error = _resolve_capture(capture_id)
    if error:
        return error
    if capture["status"] != "uploading":
        return jsonify({"error": f"This capture is already {capture['status']}", "code": "not_uploading"}), 409


    # STEP 1: the optional body — the first accepted frame's
    # yaw, kept for the stitcher inside the report JSON
    # ======================================================
    body = request.get_json(silent=True) or {}
    centre_yaw = body.get("centreYawDeg")
    if centre_yaw is not None and (
        not isinstance(centre_yaw, (int, float)) or centre_yaw != centre_yaw
        or centre_yaw in (float("inf"), float("-inf"))
    ):
        return jsonify({"error": "centreYawDeg must be a finite number", "code": "bad_centre"}), 400


    # STEP 2: gate on the frame count, then flip to 'queued'
    # and wake the worker
    # ======================================================
    db = get_db()
    frames = db.execute("SELECT COUNT(*) AS n FROM wf_capture_frames WHERE capture_id = ?", (capture["id"],)).fetchone()["n"]
    if frames < MIN_FINISH_FRAMES:
        return jsonify({
            "error": f"At least {MIN_FINISH_FRAMES} frames are needed to stitch — {frames} arrived",
            "code": "too_few_frames",
            "frames": frames,
            "required": MIN_FINISH_FRAMES,
        }), 422

    report = json.dumps({"centreYawDeg": float(centre_yaw)}, ensure_ascii=False) if centre_yaw is not None else None
    db.execute(
        "UPDATE wf_captures SET status = 'queued', report = ?, updated_at = ? WHERE id = ?",
        (report, utc_now_iso(), capture["id"]),
    )
    db.commit()

    # Imported lazily for the same cycle reason as above —
    # stitch.py imports frames_dir from this module
    from app.wayfind.stitch import nudge_stitch_worker

    nudge_stitch_worker()
    return jsonify({"status": "queued"})









############################################################
# get_capture
############################################################
#
# GET /api/wayfind/captures/<capture_id>[?buildingId=…]
#
# The capture as it stands: status, the frame count against
# the plan, the stitch report once one exists (frames used,
# coverage, timing — or the failure reason; between finish
# and done it may carry just the parked centreYawDeg), and on
# 'done' the panorama block the admin screens need to open
# the alignment tool: id, url, size, coverage and the centre
# column's yaw (the finish body's centreYawDeg when the
# client sent one, else the earliest-uploaded frame's yaw).
#
# Used by:
#   - the app's guided-capture screen — polled while the
#     stitch runs, then to jump into alignment
############################################################

@require_role(*EDITOR_ROLES)
def get_capture(capture_id):

    capture, error = _resolve_capture(capture_id)
    if error:
        return error

    db = get_db()
    frames = db.execute("SELECT COUNT(*) AS n FROM wf_capture_frames WHERE capture_id = ?", (capture["id"],)).fetchone()["n"]
    payload = {
        "id": capture["id"].split(":", 1)[1],
        "status": capture["status"],
        "frames": frames,
        "expected": capture["expected"],
        "progressPct": capture["progress_pct"],
    }

    report = json.loads(capture["report"]) if capture["report"] else None
    if report is not None:
        payload["report"] = report

    if capture["pano_id"]:
        pano = db.execute("SELECT * FROM wf_panoramas WHERE id = ?", (capture["pano_id"],)).fetchone()
        if pano is not None:
            coverage = (report or {}).get("coverage") or {}
            payload["pano"] = {
                "id": pano["id"],
                "url": f"/api/wayfind/panoramas/{pano['id']}.jpg",
                "width": pano["width"],
                "height": pano["height"],
                "hfovDeg": pano["hfov_deg"],
                "vfovDeg": pano["vfov_deg"],
                "centreYawDeg": coverage.get("centreYawDeg"),
            }

    return jsonify(payload)









############################################################
# _form_float
############################################################
#
# One REQUIRED multipart form field as a finite float, or
# None — the frame PUT has no optional pose fields, so unlike
# routes.py's _float_field there is no default to fall back
# on and absent and malformed answer the same refusal.
#
# Used by:
#   - upload_capture_frame (above)
############################################################

def _form_float(name):
    value = request.form.get(name)
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if parsed == parsed and parsed not in (float("inf"), float("-inf")) else None
