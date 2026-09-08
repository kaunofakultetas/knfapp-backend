# -----------------------------------------------------------
#  [*] Memes — the self-hosted shared meme library
#
#  The faculty's own reaction collection: every student sees
#  one library, anyone signed in may push into it, and sending
#  one is an ordinary image message whose imageUrl points at
#  /api/memes/file/... — no third-party GIF service, no search
#  leaving this origin. Files live in MEMES_DIR (default
#  /data/memes), a SEPARATE folder from the per-user uploads:
#  library files are shared, never deleted by an unsend, and
#  the per-user storage quota does not count them.
#
#  Two kinds of file, two treatments:
#    - GIFs are stored AS SENT once the bytes prove the GIF
#      signature — a re-encode would flatten the animation;
#    - static images (jpg/png/webp) go through the SAME Pillow
#      re-encode the uploads route runs (EXIF stripped, pixels
#      capped, canonical format) — a meme is still a stranger's
#      file.
#  Both yield the ~14px micro preview the grid blurs.
#
#  Endpoints:
#    GET    /api/memes             — the library (q= filters title/tags)
#    POST   /api/memes             — push one (multipart, signed in)
#    DELETE /api/memes/<meme_id>   — pusher or admin removes it
#    GET    /api/memes/file/<name> — the bytes (public, cached)
#
#  Used by:
#    - mobile composer's meme tab (list, push, send)
#    - swagger/swagger.yaml documents the shapes
# -----------------------------------------------------------

import base64
import io
import logging
import os
import uuid

from flask import Blueprint, current_app, jsonify, request, send_from_directory
from PIL import Image
from werkzeug.utils import secure_filename

from app.auth.routes import rate_limit, require_auth
from app.database import get_db, utc_now_iso

memes_bp = Blueprint("memes", __name__)
logger = logging.getLogger(__name__)

# Animated files keep their bytes, so they get the bigger cap;
# static memes are re-encoded like any upload
GIF_MAX_BYTES = 8 * 1024 * 1024
IMAGE_MAX_BYTES = 5 * 1024 * 1024
TITLE_MAX = 80
TAGS_MAX = 200
PAGE_LIMIT = 60

# SQLite's lower() folds ASCII only — 'ačiū' typed against the
# title 'AČIŪ' finds NOTHING there. Both sides of the search go
# through this fold instead: lowercase + Lithuanian diacritics
# to their base letters, stored in the `search` column at write
# time and applied to the query at read time.
_FOLD = str.maketrans("ąčęėįšųūžĄČĘĖĮŠŲŪŽ", "aceeisuuzaceeisuuz")


def _fold(text):
    return (text or "").translate(_FOLD).lower()







############################################################
# _memes_dir
############################################################
#
# The library's folder, created on first use. A SEPARATE tree
# from UPLOAD_DIR on purpose: shared files with their own
# lifecycle (admin/pusher delete only), outside the per-user
# quota and the unsend cleanup.
#
# Used by:
#   - every route below
############################################################

def _memes_dir():
    path = current_app.config.get("MEMES_DIR") or os.environ.get("MEMES_DIR", "/data/memes")
    os.makedirs(path, exist_ok=True)
    return path







############################################################
# _meme_payload
############################################################
#
# One library row as the client sees it — the url is the
# RELATIVE serve path, resolved by the client like any stored
# image; preview is the ~14px micro copy for the grid's blur;
# animated says whether the file kept its frames.
#
# Used by:
#   - list_memes, push_meme (below)
############################################################

def _meme_payload(row):
    return {
        "id": row["id"],
        "url": f"/api/memes/file/{row['filename']}",
        "title": row["title"],
        "tags": row["tags"] or "",
        "width": row["width"],
        "height": row["height"],
        "preview": row["preview"],
        "animated": row["filename"].lower().endswith(".gif"),
        "addedBy": row["added_by"],
        "createdAt": row["created_at"],
    }







############################################################
# list_memes
############################################################
#
# GET /api/memes?q=&offset=
#
# The library, newest first, PAGE_LIMIT rows a page. `q`
# filters case-insensitively over title and tags — the search
# never leaves this origin.
#
# Used by:
#   - mobile composer's meme tab
############################################################

@memes_bp.route("", methods=["GET"])
@require_auth
def list_memes():
    query = (request.args.get("q") or "").strip()
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0

    db = get_db()
    try:
        words = [w for w in _fold(query).split() if w]
        if words:
            # Every word must appear somewhere in the folded
            # haystack — "kavos puodelis" is two matches, not one
            # literal substring
            where = " AND ".join("search LIKE ?" for _ in words)
            rows = db.execute(
                f"""SELECT * FROM memes
                   WHERE {where}
                   ORDER BY created_at DESC LIMIT ? OFFSET ?""",
                (*[f"%{w}%" for w in words], PAGE_LIMIT + 1, offset),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM memes ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (PAGE_LIMIT + 1, offset),
            ).fetchall()
        has_more = len(rows) > PAGE_LIMIT
        return jsonify({
            "memes": [_meme_payload(row) for row in rows[:PAGE_LIMIT]],
            "hasMore": has_more,
        }), 200
    finally:
        db.close()







############################################################
# push_meme
############################################################
#
# POST /api/memes  (multipart: file, title?, tags?)
#
# Any signed-in student grows the library for everyone. A GIF
# must open with the GIF signature and stays byte-identical
# (animation!); anything else must survive the uploads
# route's Pillow re-encode — the same bar every photo clears.
# The client's filename only donates the default title. The
# first frame yields the ~14px preview.
#
# Used by:
#   - mobile composer's meme tab — the "+" push flow
############################################################

@memes_bp.route("", methods=["POST"])
@require_auth
@rate_limit("meme_push", max_attempts=20)
def push_meme():
    # STEP 1: the file and its size
    # =============================
    if "file" not in request.files:
        return jsonify({"error": "No file provided", "code": "no_file"}), 400
    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "No file selected", "code": "no_file"}), 400

    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)
    if size == 0:
        return jsonify({"error": "Empty file", "code": "empty_file"}), 400

    blob = file.read()
    is_gif = blob.startswith((b"GIF87a", b"GIF89a"))
    cap = GIF_MAX_BYTES if is_gif else IMAGE_MAX_BYTES
    if size > cap:
        return jsonify({
            "error": f"File too large. Max {cap // (1024 * 1024)} MB",
            "code": "file_too_large",
        }), 400


    # STEP 2: a GIF is proven and kept; a static image is
    # re-encoded by the uploads gate (EXIF gone, pixels capped)
    # =========================================================
    if is_gif:
        ext = "gif"
        stored = blob
    else:
        from app.uploads.routes import _reencode_image

        ext, stored, rejection = _reencode_image(blob)
        if rejection or not stored:
            message, code = rejection or ("File content does not match an allowed image format", "bad_file_content")
            return jsonify({"error": message, "code": code}), 400

    try:
        with Image.open(io.BytesIO(stored)) as img:
            width, height = img.size
            first = img.convert("RGB")
            first.thumbnail((14, 14))
            buf = io.BytesIO()
            first.save(buf, format="JPEG", quality=60)
            preview = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return jsonify({"error": "File content does not match its extension", "code": "bad_file_content"}), 400


    # STEP 3: the title — given, or the filename's stem
    # =================================================
    title = (request.form.get("title") or "").strip()
    if not title:
        stem = (secure_filename(file.filename) or "meme").rsplit(".", 1)[0]
        title = stem.replace("_", " ").replace("-", " ").strip() or "Meme"
    if len(title) > TITLE_MAX:
        title = title[:TITLE_MAX]
    tags = (request.form.get("tags") or "").strip().lower()
    if len(tags) > TAGS_MAX:
        tags = tags[:TAGS_MAX]


    # STEP 4: the file under a fresh name, then the row
    # =================================================
    safe_name = secure_filename(f"{uuid.uuid4().hex}.{ext}")
    final_path = os.path.join(_memes_dir(), safe_name)
    part_path = f"{final_path}.part"
    try:
        with open(part_path, "wb") as handle:
            handle.write(stored)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(part_path, final_path)
    except OSError:
        logger.exception("Meme library write failed")
        try:
            os.unlink(part_path)
        except OSError:
            pass
        return jsonify({"error": "Storage unavailable", "code": "storage_unavailable"}), 507

    meme_id = str(uuid.uuid4())
    db = get_db()
    try:
        db.execute(
            """INSERT INTO memes (id, filename, title, tags, added_by, byte_size, width, height, preview, search, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (meme_id, safe_name, title, tags or None, request.user["id"], len(stored), width, height, preview,
             _fold(f"{title} {tags}"), utc_now_iso()),
        )
        db.commit()
        row = db.execute("SELECT * FROM memes WHERE id = ?", (meme_id,)).fetchone()
        return jsonify({"meme": _meme_payload(row)}), 201
    finally:
        db.close()







############################################################
# delete_meme
############################################################
#
# DELETE /api/memes/<meme_id>
#
# The pusher takes back their own; an admin curates anything.
# The file goes with the row — old messages referencing it
# lose their picture, deliberately: moderation must be able to
# remove a file completely.
#
# Used by:
#   - mobile meme tab (own rows), admin tooling
############################################################

@memes_bp.route("/<meme_id>", methods=["DELETE"])
@require_auth
def delete_meme(meme_id):
    db = get_db()
    try:
        row = db.execute("SELECT * FROM memes WHERE id = ?", (meme_id,)).fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404
        is_admin = request.user.get("role") == "admin"
        if not is_admin and row["added_by"] != request.user["id"]:
            return jsonify({"error": "Only the pusher or an admin can remove a meme"}), 403

        db.execute("DELETE FROM memes WHERE id = ?", (meme_id,))
        db.commit()
        try:
            os.unlink(os.path.join(_memes_dir(), row["filename"]))
        except OSError:
            pass
        return jsonify({"ok": True}), 200
    finally:
        db.close()







############################################################
# serve_meme
############################################################
#
# GET /api/memes/file/<name>
#
# The bytes, public and cacheable like the uploads route —
# a chat bubble must render without a token. The name is
# re-secured as belt and braces; send_from_directory refuses
# path tricks on its own.
#
# Used by:
#   - every chat bubble whose imageUrl points here
############################################################

@memes_bp.route("/file/<name>", methods=["GET"])
def serve_meme(name):
    safe_name = secure_filename(name)
    if not safe_name or safe_name != name:
        return jsonify({"error": "Not found"}), 404
    return send_from_directory(_memes_dir(), safe_name, max_age=86400 * 7)
