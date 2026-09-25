############################################################
#  [*] Memes API — the self-hosted shared library
#
#  The faculty's own reaction collection: one library for
#  everyone, anyone signed in may push into it, and sending
#  one is an ordinary image message whose url points at the
#  public serve route — no third-party image service, no
#  search leaving this origin. GIFs are stored AS SENT once
#  the bytes prove the signature (a re-encode would flatten
#  the animation); static images clear the SAME re-encode
#  gate every upload does.
#
#  Split into:
#
#    list_memes  — GET  /api/memes (?q= folded search)
#    push_meme   — POST /api/memes (multipart, signed in)
#    delete_meme — DELETE /api/memes/<meme_id>
#    serve_meme  — GET  /api/memes/file/<name> (public)
############################################################


import base64
import io
import logging
import mimetypes
import os
import re
import uuid


from PIL import Image
from django.conf import settings
from django.db import transaction
from django.http import FileResponse


from knfapp.common import ratelimit
from knfapp.common.http import clean_param, json_error, json_response, require_methods
from knfapp.common.timestamps import utc_now
from knfapp.memes.models import Meme
from knfapp.uploads.gates import reencode_image
from knfapp.users.auth import require_auth


logger = logging.getLogger(__name__)

# Animated files keep their bytes, so they get the bigger cap;
# static memes are re-encoded like any upload
GIF_MAX_BYTES = 8 * 1024 * 1024
IMAGE_MAX_BYTES = 5 * 1024 * 1024
TITLE_MAX = 80
TAGS_MAX = 200
PAGE_LIMIT = 60

# Stored names are a uuid4 hex plus the two extensions this
# module writes — the serve/delete gate
FILENAME_RE = re.compile(r"^[0-9a-f]{32}\.(gif|jpg|png|webp)$")

# lower() folds ASCII only — both sides of the search go
# through this fold instead
_FOLD = str.maketrans("ąčęėįšųūžĄČĘĖĮŠŲŪŽ", "aceeisuuzaceeisuuz")

_memes_dir_cache = None


def _fold(text):
    return (text or "").translate(_FOLD).lower()


def _memes_dir():
    global _memes_dir_cache
    if _memes_dir_cache is None:
        path = os.path.abspath(settings.MEMES_DIR)
        os.makedirs(path, exist_ok=True)
        _memes_dir_cache = path
    return _memes_dir_cache


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
        "addedBy": row["added_by_id"],
        "createdAt": row["created_at"],
    }


_ROW_FIELDS = ("id", "filename", "title", "tags", "width", "height", "preview", "added_by_id", "created_at")








############################################################
# list_memes
############################################################
#
# GET /api/memes?q=&offset= — newest first, PAGE_LIMIT rows
# a page. Every word of ?q must appear somewhere in the
# folded haystack — "kavos puodelis" is two matches, not
# one literal substring.
#
# Used by:
#   - the mobile composer's meme tab
############################################################

@require_methods("GET")
@require_auth
def list_memes(request):
    query = (clean_param(request.GET.get("q")) or "").strip()
    try:
        offset = max(0, int(request.GET.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0

    base = Meme.objects.all()
    for word in [w for w in _fold(query).split() if w]:
        base = base.filter(search__contains=word)

    rows = list(base.order_by("-created_at").values(*_ROW_FIELDS)[offset:offset + PAGE_LIMIT + 1])
    has_more = len(rows) > PAGE_LIMIT
    return json_response({
        "memes": [_meme_payload(row) for row in rows[:PAGE_LIMIT]],
        "hasMore": has_more,
    })








############################################################
# push_meme
############################################################
#
# POST /api/memes (multipart: file, title?, tags?). A GIF
# must open with the GIF signature and stays byte-identical;
# anything else must survive the uploads gate's re-encode.
# The client's filename only donates the default title; the
# first frame yields the ~14px preview the grid blurs. Size
# is refused as 413 file_too_large twice over: the 8 MB
# ceiling on the declared size BEFORE a byte of the body is
# read, the 5 MB static cap once the signature says the
# file is not a GIF.
#
# Used by:
#   - the mobile composer's meme tab — the push flow
############################################################

@require_methods("POST")
@require_auth
@ratelimit.per_user("meme_push", max_attempts=20)
def push_meme(request):
    # STEP 1: the file and its size
    # =============================
    file = request.FILES.get("file")
    if file is None or not file.name:
        return json_error("No file provided", 400, code="no_file")
    if file.size == 0:
        return json_error("Empty file", 400, code="empty_file")
    # The ceiling before the read: the kind is unknown until
    # the signature is sniffed, so the bigger cap gates here
    # and the per-kind one follows the sniff
    ceiling = max(GIF_MAX_BYTES, IMAGE_MAX_BYTES)
    if file.size > ceiling:
        return json_error(f"File too large. Max {ceiling // (1024 * 1024)} MB", 413, code="file_too_large")

    blob = file.read()
    is_gif = blob.startswith((b"GIF87a", b"GIF89a"))
    cap = GIF_MAX_BYTES if is_gif else IMAGE_MAX_BYTES
    if file.size > cap:
        return json_error(f"File too large. Max {cap // (1024 * 1024)} MB", 413, code="file_too_large")


    # STEP 2: a GIF is proven and kept; a static image clears
    # the uploads gate (EXIF gone, pixels capped)
    # =======================================================
    if is_gif:
        ext = "gif"
        stored = blob
    else:
        ext, stored, rejection = reencode_image(blob)
        if rejection or not stored:
            message, code = rejection or ("File content does not match an allowed image format", "bad_file_content")
            return json_error(message, 400, code=code)

    try:
        with Image.open(io.BytesIO(stored)) as img:
            width, height = img.size
            first = img.convert("RGB")
            first.thumbnail((14, 14))
            buf = io.BytesIO()
            first.save(buf, format="JPEG", quality=60)
            preview = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return json_error("File content does not match its extension", 400, code="bad_file_content")


    # STEP 3: the title — given, or the filename's stem
    # =================================================
    title = (request.POST.get("title") or "").strip()
    if not title:
        stem = os.path.basename(file.name or "meme").rsplit(".", 1)[0]
        title = stem.replace("_", " ").replace("-", " ").strip() or "Meme"
    if len(title) > TITLE_MAX:
        title = title[:TITLE_MAX]
    tags = (request.POST.get("tags") or "").strip().lower()
    if len(tags) > TAGS_MAX:
        tags = tags[:TAGS_MAX]


    # STEP 4: the file under a fresh name (.part, fsync,
    # rename), then the row
    # ==================================================
    safe_name = f"{uuid.uuid4().hex}.{ext}"
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
        return json_error("Storage unavailable", 507, code="storage_unavailable")

    meme_id = str(uuid.uuid4())
    Meme.objects.create(
        id=meme_id, filename=safe_name, title=title, tags=tags or None,
        added_by_id=request.user["id"], byte_size=len(stored), width=width, height=height,
        preview=preview, search=_fold(f"{title} {tags}"), created_at=utc_now(),
    )
    row = Meme.objects.filter(id=meme_id).values(*_ROW_FIELDS).first()
    return json_response({"meme": _meme_payload(row)}, status=201)








############################################################
# delete_meme / serve_meme
############################################################
#
# The pusher takes back their own; an admin curates
# anything — and the file goes with the row, deliberately:
# moderation must be able to remove a file completely, old
# messages referencing it lose their picture. The serve
# route is public and long-cached (a chat bubble renders
# without a token), gated to the exact names this module
# writes — and it touches no table, so it opts out of the
# request-wide transaction ATOMIC_REQUESTS would open around
# every one of those GETs (the uploads serve does the same).
#
# Used by:
#   - delete_meme: the mobile meme panel (app/(main)/chat-room
#     MemeLibrary, the viewer's own tiles) and the Vite
#     panel's Memes.jsx
#   - serve_meme: every chat bubble and meme tile whose url
#     points here
############################################################

@require_methods("DELETE")
@require_auth
def delete_meme(request, meme_id):
    row = Meme.objects.filter(id=meme_id).values("id", "filename", "added_by_id").first()
    if not row:
        return json_error("Not found", 404)
    is_admin = request.user.get("role") == "admin"
    if not is_admin and row["added_by_id"] != request.user["id"]:
        return json_error("Only the pusher or an admin can remove a meme", 403)

    Meme.objects.filter(id=meme_id).delete()
    try:
        os.unlink(os.path.join(_memes_dir(), row["filename"]))
    except OSError:
        pass
    return json_response({"ok": True})


@transaction.non_atomic_requests
@require_methods("GET")
def serve_meme(request, name):
    if not FILENAME_RE.match(name):
        return json_error("Not found", 404)

    directory = _memes_dir()
    file_path = os.path.join(directory, name)
    if not os.path.isfile(file_path):
        return json_error("Not found", 404)
    if os.path.realpath(file_path) != os.path.join(os.path.realpath(directory), name):
        return json_error("Not found", 404)

    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    response = FileResponse(open(file_path, "rb"), content_type=content_type)
    response["Cache-Control"] = f"public, max-age={86400 * 7}"
    return response
