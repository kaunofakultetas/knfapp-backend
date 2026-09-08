############################################################
#  [*] Wayfind stitch — the capture-to-panorama worker
#
#  Turns a finished guided capture (captures.py) into one
#  equirectangular panorama. ONE daemon thread per process,
#  started from create_app (unlike the scraper scheduler it
#  touches nothing but the local database and disk — no
#  external traffic, no pushes — so building an app may start
#  it; WAYFIND_STITCH_ENABLED=0 turns it off). The thread
#  polls every 3 s for the oldest 'queued' capture, claims it
#  by flipping the row to 'stitching' (the UPDATE's WHERE
#  status='queued' is the lock), stitches, and writes the
#  outcome back: 'done' with a pano_id and a report, or
#  'failed' with the reason. finish_capture nudges the poll
#  so a stitch starts inside a second.
#
#  The composite is ORIENTATION-ONLY: no feature matching, no
#  bundle adjustment — the tracker's pose per frame (yawDeg
#  clockwise from above, pitchDeg positive up, rollDeg
#  positive = tilted clockwise from upright portrait) is
#  trusted as-is. Each frame is a pinhole camera at
#  frame_hfov_deg; the canvas (2048x1024) is inverse-mapped
#  per frame — for the canvas rows inside the frame's
#  footprint, every canvas direction is rotated into the
#  frame and sampled bilinearly — with a cosine-falloff
#  weight from the frame centre as the feather. Everything is
#  vectorised numpy; 36 frames at 1280 px stitch in seconds.
#  numpy is imported INSIDE the compose call, so a container
#  built before the numpy pin still boots and only an actual
#  stitch fails.
#
#  Frame conventions (world axes): +Z is yaw 0, +X is yaw 90
#  (clockwise from above), +Y is up; camera-to-world is
#  R = Ry(yaw) @ Rx(-pitch) @ Rz(-roll). The canvas columns
#  are rotated after accumulation so the CENTRE column is the
#  yaw the finish body named (centreYawDeg — the client's
#  first ACCEPTED frame), falling back to the earliest-
#  updated frame's yaw when no body was sent — the phase 2
#  contract the alignment tool builds on; the pano row is
#  stored with heading_source 'auto'.
#
#  The finished JPEG goes through the same content-hash store
#  as a direct panorama upload (store.py), gets a
#  wf_panoramas row, and the capture's frames directory is
#  deleted.
############################################################


import hashlib
import io
import json
import logging
import math
import os
import shutil
import threading
import time

from PIL import Image

from app.database import get_db, utc_now_iso
from app.wayfind.captures import frames_dir
from app.wayfind.store import store_dir, write_once


logger = logging.getLogger(__name__)

# Set on the first start_stitch_worker call and cleared by
# stop_stitch_worker — the module-level singleton IS the
# re-entry guard, exactly like the scraper scheduler's
_worker = None
_stop = None
_wake = None

# How long the worker sleeps between queue polls when nothing
# nudges it; finish_capture's nudge cuts the wait short
POLL_SECONDS = 3.0

CANVAS_W = 2048
CANVAS_H = 1024

# Frames are downscaled to this width before composing — at a
# 2048-wide canvas anything sharper is wasted work
FRAME_COMPOSE_WIDTH = 1280

# The dark neutral the uncovered sphere is filled with —
# near-black, slightly blue, so holes read as "not captured"
# rather than as content
FILL_RGB = (26.0, 28.0, 32.0)

# A canvas pixel with less accumulated weight than this
# counts as uncovered — both for the fill and for the
# coverage measurement
COVER_WEIGHT = 1e-3

# A canvas ROW counts as covered when at least this fraction
# of its columns is — a couple of stray feathered pixels must
# not stretch the measured vfov
ROW_COVER_FRACTION = 0.02









############################################################
# StitchError
############################################################
#
# A stitch failure with a reason fit for the capture's
# report — raised by the compose path, caught by
# stitch_capture, which stores the message and flips the row
# to 'failed'.
#
# Used by:
#   - _run_stitch / compose_panorama (below)
############################################################

class StitchError(Exception):
    pass









############################################################
# start_stitch_worker
############################################################
#
# Idempotent per process: a second call returns at once.
# Checks the WAYFIND_STITCH_ENABLED gate (default on),
# re-queues captures a killed process left at 'stitching'
# (the previous process is known to be gone, so they are
# orphans, not work in flight), then starts the daemon poll
# thread. Answers True when a worker is running after the
# call.
#
# Used by:
#   - app/__init__.py — create_app, after the blueprints
############################################################

def start_stitch_worker(app):
    global _worker, _stop, _wake

    raw = os.environ.get("WAYFIND_STITCH_ENABLED")
    if raw is not None and raw.strip().lower() in ("0", "false", "no", "off"):
        logger.info("WAYFIND_STITCH_ENABLED is off — no stitch worker in this process")
        return False

    if _worker is not None:
        return True

    # Bookkeeping first — a failure here must never cost us
    # the worker
    try:
        with app.app_context():
            _requeue_orphans()
    except Exception:
        logger.exception("Could not re-queue orphaned stitches at start")

    _stop = threading.Event()
    _wake = threading.Event()
    _worker = threading.Thread(target=_worker_loop, args=(app, _stop, _wake), daemon=True, name="wayfind-stitch")
    _worker.start()
    logger.info("Wayfind stitch worker started (poll every %.0f s)", POLL_SECONDS)
    return True









############################################################
# stop_stitch_worker
############################################################
#
# Signals the poll thread to exit and clears the singleton so
# a later start builds a fresh one. Never joins: a stitch in
# flight is not worth blocking an exit for — its row stays at
# 'stitching' and the next start re-queues it. Safe to call
# when nothing is running.
#
# Used by:
#   - nothing calls this at the moment — it mirrors
#     stop_scraper_scheduler for tests and future shutdown
#     paths
############################################################

def stop_stitch_worker():
    global _worker

    if _stop is not None:
        _stop.set()
    if _wake is not None:
        _wake.set()
    if _worker is not None:
        _worker = None
        logger.info("Wayfind stitch worker stopped")









############################################################
# nudge_stitch_worker
############################################################
#
# Wakes the poll thread early so a freshly queued capture
# starts stitching now instead of at the next 3 s tick. A
# no-op when no worker runs (the poll finds the row later, or
# the smoke drives stitch_capture directly).
#
# Used by:
#   - app/wayfind/captures.py — finish_capture
############################################################

def nudge_stitch_worker():
    if _wake is not None:
        _wake.set()









############################################################
# _worker_loop
############################################################
#
# The daemon thread's body: wait out the poll interval (or a
# nudge), then drain the queue one capture at a time —
# single worker, one stitch in flight ever, exactly what a
# single-writer SQLite deployment wants. Every pass is
# wrapped so one broken capture cannot kill the thread.
#
# Used by:
#   - start_stitch_worker (above) — as the thread target
############################################################

def _worker_loop(app, stop, wake):
    while not stop.is_set():
        wake.wait(POLL_SECONDS)
        wake.clear()
        if stop.is_set():
            break

        try:
            with app.app_context():
                while not stop.is_set():
                    capture_id = _claim_next()
                    if capture_id is None:
                        break
                    stitch_capture(capture_id)
        except Exception:
            logger.exception("Stitch worker pass failed")









############################################################
# _claim_next
############################################################
#
# The oldest 'queued' capture, flipped to 'stitching' — the
# conditional UPDATE is the claim, so a second process (or a
# smoke driving stitch_capture directly) can never grab the
# same row. Answers the scoped capture id, or None when the
# queue is empty.
#
# Used by:
#   - _worker_loop (above)
############################################################

def _claim_next():
    db = get_db()
    try:
        row = db.execute("SELECT id FROM wf_captures WHERE status = 'queued' ORDER BY updated_at, id LIMIT 1").fetchone()
        if row is None:
            return None
        claimed = db.execute(
            "UPDATE wf_captures SET status = 'stitching', progress_pct = 0, updated_at = ? WHERE id = ? AND status = 'queued'",
            (utc_now_iso(), row["id"]),
        ).rowcount
        db.commit()
        return row["id"] if claimed else None
    finally:
        db.close()









############################################################
# _requeue_orphans
############################################################
#
# Flips every 'stitching' row back to 'queued'. A row only
# sits there between a claim and its outcome, so at process
# start any such row belongs to a process that died mid-
# stitch — its frames are still on disk (they are deleted
# only after 'done'), so the stitch simply runs again.
#
# Used by:
#   - start_stitch_worker (above) — before the first poll
############################################################

def _requeue_orphans():
    db = get_db()
    try:
        requeued = db.execute(
            "UPDATE wf_captures SET status = 'queued', updated_at = ? WHERE status = 'stitching'",
            (utc_now_iso(),),
        ).rowcount
        db.commit()
        if requeued:
            logger.warning("Re-queued %d stitch(es) left 'stitching' by a killed process", requeued)
    finally:
        db.close()









############################################################
# stitch_capture
############################################################
#
# One capture, end to end: claim (when still 'queued' — a
# direct call from a test needs no worker), load the frames,
# compose, store the panorama through the content-hash store,
# write the wf_panoramas row (heading_source 'auto'), stamp
# the capture 'done' with its report, and delete the frames
# directory. Any failure lands as status 'failed' with the
# reason in the report — the frames stay for a retry. Needs
# an app context (store_dir reads current_app). Answers True
# when the capture reached 'done'.
#
# Used by:
#   - _worker_loop (above)
#   - the container smoke / tests — driven synchronously
############################################################

def stitch_capture(capture_id):
    # STEP 1: load and claim — a row someone else already
    # took (or finished) is left alone
    # ===================================================
    db = get_db()
    try:
        capture = db.execute("SELECT * FROM wf_captures WHERE id = ?", (capture_id,)).fetchone()
        if capture is None or capture["status"] not in ("queued", "stitching"):
            return False
        if capture["status"] == "queued":
            claimed = db.execute(
                "UPDATE wf_captures SET status = 'stitching', progress_pct = 0, updated_at = ? WHERE id = ? AND status = 'queued'",
                (utc_now_iso(), capture_id),
            ).rowcount
            db.commit()
            if not claimed:
                return False


        # STEP 2: the stitch itself, fenced — the reason of any
        # failure becomes the report
        # =====================================================
        try:
            _run_stitch(db, capture)
            return True
        except Exception as exc:
            logger.exception("Stitch failed for %s", capture_id)
            reason = str(exc) if isinstance(exc, StitchError) else f"internal: {exc}"
            db.execute(
                "UPDATE wf_captures SET status = 'failed', report = ?, updated_at = ? WHERE id = ?",
                (json.dumps({"reason": reason}, ensure_ascii=False), utc_now_iso(), capture_id),
            )
            db.commit()
            return False
    finally:
        db.close()









############################################################
# _run_stitch
############################################################
#
# The happy path of one stitch. The canvas centre column is
# the finish body's centreYawDeg when the client sent one —
# finish_capture parks it inside the report JSON, read back
# here before the report is rewritten. Without it the centre
# falls back to the frame with the EARLIEST updated_at, which
# is NOT necessarily the first accepted frame: a re-upload
# stamps a fresh updated_at, so a re-shot first target hands
# the fallback centre to the next frame, and the retry queue
# can deliver frames out of accept order. Clients that care
# send the body; readers must always use the reported
# coverage.centreYawDeg, which matches the canvas either way.
#
# Used by:
#   - stitch_capture (above)
############################################################

def _run_stitch(db, capture):
    # STEP 1: the frames — pose rows joined to their files,
    # loaded and downscaled for composing; the parked centre
    # yaw read out before the report is rewritten
    # =====================================================
    started = time.time()
    requested_centre = (json.loads(capture["report"]) or {}).get("centreYawDeg") if capture["report"] else None
    rows = db.execute(
        "SELECT * FROM wf_capture_frames WHERE capture_id = ? ORDER BY updated_at, target_id",
        (capture["id"],),
    ).fetchall()
    directory = frames_dir(capture["id"])

    frames = []
    for row in rows:
        path = os.path.join(directory, f"{row['target_id']}.jpg")
        try:
            image = Image.open(path).convert("RGB")
        except Exception:
            logger.warning("Frame %s of %s is unreadable — skipped", row["target_id"], capture["id"])
            continue
        if image.width > FRAME_COMPOSE_WIDTH:
            scale = FRAME_COMPOSE_WIDTH / image.width
            image = image.resize((FRAME_COMPOSE_WIDTH, max(1, round(image.height * scale))), Image.LANCZOS)
        frames.append({
            "image": image,
            "yawDeg": row["yaw_deg"],
            "pitchDeg": row["pitch_deg"],
            "rollDeg": row["roll_deg"],
        })
    if len(frames) < 2:
        raise StitchError("fewer than 2 readable frames on disk")


    # STEP 2: compose, with progress written back per frame so
    # the phone's poll shows a moving bar
    # ========================================================
    def on_progress(done, total):
        db.execute(
            "UPDATE wf_captures SET progress_pct = ? WHERE id = ?",
            (int(done * 90 / total), capture["id"]),
        )
        db.commit()

    canvas, coverage = compose_panorama(frames, capture["frame_hfov_deg"], on_progress, centre_yaw_deg=requested_centre)


    # STEP 3: encode and store through the content-hash store
    # — byte-identical to what upload_panorama would keep
    # =======================================================
    image = Image.fromarray(canvas, "RGB")
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=85, optimize=True, progressive=True)
    blob = out.getvalue()
    digest = hashlib.sha256(blob).hexdigest()
    if not write_once(store_dir("panoramas"), f"{digest}.jpg", blob):
        raise StitchError("the panorama could not be written")

    db.execute(
        """
        INSERT OR IGNORE INTO wf_panoramas (id, building_id, node_id, width, height, bytes, hfov_deg, vfov_deg, heading_raw_deg, heading_source, uploaded_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 'auto', ?, ?)
        """,
        (digest, capture["building_id"], capture["node_id"], image.width, image.height, len(blob),
         coverage["hfovDeg"], coverage["vfovDeg"], capture["created_by"], utc_now_iso()),
    )


    # STEP 4: the report, the 'done' stamp, and the frames
    # directory gone
    # ====================================================
    report = {
        "framesUsed": len(frames),
        "framesExpected": capture["expected"],
        "coverage": coverage,
        "timingMs": int((time.time() - started) * 1000),
    }
    db.execute(
        "UPDATE wf_captures SET status = 'done', progress_pct = 100, report = ?, pano_id = ?, updated_at = ? WHERE id = ?",
        (json.dumps(report, ensure_ascii=False), digest, utc_now_iso(), capture["id"]),
    )
    db.commit()
    shutil.rmtree(directory, ignore_errors=True)
    logger.info("Stitched %s: %d frames -> %s in %d ms", capture["id"], len(frames), digest, report["timingMs"])









############################################################
# compose_panorama
############################################################
#
# The pure composite: frames ({image: PIL RGB, yawDeg,
# pitchDeg, rollDeg}) + the shared frame hfov + an optional
# centre_yaw_deg (the finish body's request; with None the
# frames are ORDERED and the first one's yaw becomes the
# centre column) -> (a 2048x1024 uint8 RGB canvas, the
# coverage dict {hfovDeg, vfovDeg, vOffsetDeg, centreYawDeg,
# coveredPct}).
#
# Per frame the mapping is INVERSE: the canvas rows inside
# the frame's angular footprint are turned into world
# directions, rotated into the camera (world = R cam with
# R = Ry(yaw) @ Rx(-pitch) @ Rz(-roll), so cam = D @ R for
# row-vector directions), projected through the pinhole, and
# the frame is sampled bilinearly where the projection lands
# inside it. The weight is a separable raised-sine falloff
# from the frame centre (zero at the edges), so overlapping
# frames feather into each other instead of seaming.
# Accumulation is weighted-sum / weight; pixels no frame
# reached are filled with FILL_RGB.
#
# Coverage is measured BEFORE the centre rotation (a column
# roll changes no coverage): hfov is 360 when every column
# saw a frame, else the covered arc (360 minus the largest
# circular column gap); vfov and vOffset come from the
# covered row band. All numpy — no per-pixel Python — and
# numpy is imported here, lazily, so the module (imported by
# create_app's start hook) never needs it at boot.
#
# Used by:
#   - _run_stitch (above)
#   - the container smoke / tests — directly, on synthetic
#     frames
############################################################

def compose_panorama(frames, frame_hfov_deg, on_progress=None, centre_yaw_deg=None):
    import numpy as np

    # STEP 1: the canvas direction grid — column c is yaw
    # (c+0.5)/W*360, row r is pitch 90-(r+0.5)/H*180; world
    # axes per the file header
    # =====================================================
    acc = np.zeros((CANVAS_H, CANVAS_W, 3), np.float32)
    wsum = np.zeros((CANVAS_H, CANVAS_W), np.float32)

    lam = np.deg2rad((np.arange(CANVAS_W) + 0.5) / CANVAS_W * 360.0)
    phi_deg = 90.0 - (np.arange(CANVAS_H) + 0.5) / CANVAS_H * 180.0
    phi = np.deg2rad(phi_deg)
    sin_l, cos_l = np.sin(lam), np.cos(lam)
    sin_p, cos_p = np.sin(phi), np.cos(phi)

    half_h = math.tan(math.radians(frame_hfov_deg) / 2.0)


    # STEP 2: accumulate every frame into its footprint rows
    # ======================================================
    for index, frame in enumerate(frames):
        img = np.asarray(frame["image"], np.float32)
        fh, fw = img.shape[0], img.shape[1]
        half_v = half_h * fh / fw

        # Camera-to-world rotation from the pose
        yaw = math.radians(frame["yawDeg"])
        pitch = math.radians(frame["pitchDeg"])
        roll = math.radians(frame["rollDeg"])
        ry = np.array([[math.cos(yaw), 0, math.sin(yaw)], [0, 1, 0], [-math.sin(yaw), 0, math.cos(yaw)]])
        rx = np.array([[1, 0, 0], [0, math.cos(-pitch), -math.sin(-pitch)], [0, math.sin(-pitch), math.cos(-pitch)]])
        rz = np.array([[math.cos(-roll), -math.sin(-roll), 0], [math.sin(-roll), math.cos(-roll), 0], [0, 0, 1]])
        rot = (ry @ rx @ rz).astype(np.float32)

        # Only the canvas rows the frame can possibly touch —
        # its angular radius (the half-diagonal) around its
        # pitch, with a small margin for the row grid
        half_diag = math.degrees(math.atan(half_h * math.sqrt(1.0 + (fh / fw) ** 2)))
        touched = np.flatnonzero(np.abs(phi_deg - frame["pitchDeg"]) <= half_diag + 2.0)
        if touched.size == 0:
            continue
        r0, r1 = int(touched[0]), int(touched[-1]) + 1
        rows = r1 - r0

        # World directions of the slice, as (n, 3) row vectors
        cp = cos_p[r0:r1][:, None]
        dirs = np.stack([
            np.broadcast_to(sin_l[None, :], (rows, CANVAS_W)) * cp,
            np.broadcast_to(sin_p[r0:r1][:, None], (rows, CANVAS_W)),
            np.broadcast_to(cos_l[None, :], (rows, CANVAS_W)) * cp,
        ], axis=-1).reshape(-1, 3)

        # Into the camera and through the pinhole; z <= 0 is
        # behind the camera
        cam = dirs @ rot
        z = cam[:, 2]
        z_safe = np.where(z > 1e-6, z, 1.0)
        u = ((cam[:, 0] / z_safe) / half_h + 1.0) * 0.5 * (fw - 1)
        v = (1.0 - (cam[:, 1] / z_safe) / half_v) * 0.5 * (fh - 1)
        inside = (z > 1e-6) & (u >= 0) & (u <= fw - 1) & (v >= 0) & (v <= fh - 1)
        idx = np.flatnonzero(inside)
        if idx.size == 0:
            continue
        u, v = u[idx], v[idx]

        # Bilinear sample
        u0 = np.floor(u).astype(np.int32)
        v0 = np.floor(v).astype(np.int32)
        u1 = np.minimum(u0 + 1, fw - 1)
        v1 = np.minimum(v0 + 1, fh - 1)
        fu = (u - u0)[:, None]
        fv = (v - v0)[:, None]
        top = img[v0, u0] * (1.0 - fu) + img[v0, u1] * fu
        bottom = img[v1, u0] * (1.0 - fu) + img[v1, u1] * fu
        rgb = top * (1.0 - fv) + bottom * fv

        # The feather: a separable raised sine, 1 at the frame
        # centre, 0 at its edges
        weight = (np.sin(np.pi * (u + 0.5) / fw) * np.sin(np.pi * (v + 0.5) / fh)).astype(np.float32)

        acc_slice = acc[r0:r1].reshape(-1, 3)
        wsum_slice = wsum[r0:r1].reshape(-1)
        acc_slice[idx] += rgb * weight[:, None]
        wsum_slice[idx] += weight

        if on_progress is not None:
            on_progress(index + 1, len(frames))


    # STEP 3: normalise, fill the uncovered sphere, measure
    # coverage
    # =====================================================
    covered = wsum > COVER_WEIGHT
    canvas = acc / np.maximum(wsum, 1e-6)[:, :, None]
    canvas = np.where(covered[:, :, None], canvas, np.array(FILL_RGB, np.float32))

    col_covered = covered.any(axis=0)
    cols = np.flatnonzero(col_covered)
    if cols.size == 0:
        raise StitchError("the frames covered no part of the sphere")
    if bool(col_covered.all()):
        hfov = 360.0
    else:
        gaps = np.diff(cols) - 1
        wrap_gap = int(cols[0]) + (CANVAS_W - 1 - int(cols[-1]))
        largest_gap = max(int(gaps.max(initial=0)), wrap_gap)
        hfov = round(360.0 - largest_gap * 360.0 / CANVAS_W, 2)

    band = np.flatnonzero(covered.mean(axis=1) > ROW_COVER_FRACTION)
    if band.size == 0:
        band = np.flatnonzero(covered.any(axis=1))
    top_row, bottom_row = int(band[0]), int(band[-1])
    vfov = round((bottom_row - top_row + 1) * 180.0 / CANVAS_H, 2)
    v_offset = round(90.0 - (top_row + bottom_row + 1) / 2.0 * 180.0 / CANVAS_H, 2)


    # STEP 4: rotate the columns so the requested yaw — or,
    # without one, the first frame's — is the centre column,
    # the alignment tool's anchor
    # ======================================================
    centre_yaw = (frames[0]["yawDeg"] if centre_yaw_deg is None else float(centre_yaw_deg)) % 360.0
    shift = int(round(CANVAS_W / 2.0 - centre_yaw / 360.0 * CANVAS_W))
    canvas = np.roll(canvas, shift, axis=1)

    coverage = {
        "hfovDeg": hfov,
        "vfovDeg": vfov,
        "vOffsetDeg": v_offset,
        "centreYawDeg": round(centre_yaw, 2),
        "coveredPct": round(float(covered.mean()) * 100.0, 1),
    }
    return np.clip(canvas, 0.0, 255.0).astype(np.uint8), coverage
