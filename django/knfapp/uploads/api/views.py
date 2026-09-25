############################################################
#  [*] Uploads API — store, serve, delete
#
#  One POST that accepts a photo (re-encoded), a document,
#  a video or a voice note; a public GET serving the flat
#  directory with browser-only caching (plus one thumbnail
#  size for still photos); an owner-or-admin DELETE. Every
#  rejection carries the machine `code` beside the human
#  `error`, exactly the slugs the mobile app translates.
#
#  Split into:
#
#    upload_file — POST   /api/uploads
#    delete_file — DELETE /api/uploads/<filename>
#    serve_file  — GET    /api/uploads/<filename>
############################################################


import base64
import io
import logging
import mimetypes
import os
import re
import uuid


from PIL import Image
from django.db import models, transaction
from django.http import FileResponse, HttpResponse
from django.utils.cache import get_conditional_response
from django.utils.http import http_date, quote_etag


from knfapp.common import ratelimit
from knfapp.common.http import clean_param, json_error, json_response, require_methods
from knfapp.common.timestamps import utc_now
from knfapp.uploads import gates
from knfapp.uploads.models import Upload
from knfapp.uploads.storage import (
    THUMB_DIRNAME,
    UPLOAD_QUOTA_BYTES,
    UPLOAD_RATE_MAX,
    atomic_write,
    delete_upload,
    ensure_thumbnail,
    safe_upload_name,
    upload_dir,
)
from knfapp.users.auth import require_auth
from knfapp.users.models import User


logger = logging.getLogger(__name__)








############################################################
# upload_file
############################################################
#
# POST /api/uploads — multipart/form-data with one "file"
# field (+ optional kind=file/video/audio; default image) →
# 201 {"url": "/api/uploads/<uuid4 hex>.<ext>", "filename",
# "name", "size", "mime", "width", "height", "preview"}.
# The client's filename is thrown away entirely — the
# stored extension comes from the byte gates, so the bytes
# on disk always match the name they are served under, and
# names are never enumerable. Photos additionally answer
# their stored pixel size and a ~14px data-URI micro copy
# (the blurry placeholder the chat bubble shows while the
# real bytes download).
#
# Twenty uploads per user per 5 minutes and 100 MB stored
# per account bound what one account can do to the volume;
# the quota check holds the account's users row (SELECT …
# FOR UPDATE) for the rest of the request, so overlapping
# uploads queue per account instead of each seeing the
# whole remaining budget.
#
# Used by:
#   - services/api/uploads.ts uploadImageApi (avatar, post
#     image, chat photo/video/voice/file)
############################################################

@require_methods("POST")
@require_auth
@ratelimit.per_user("upload", max_attempts=UPLOAD_RATE_MAX)
def upload_file(request):
    # STEP 1: the file out of the multipart body
    # ==========================================
    file = request.FILES.get("file")
    if file is None or not file.name:
        return json_error("No file provided", 400, code="no_file")

    upload_kind = (
        clean_param(request.POST.get("kind"))
        or clean_param(request.GET.get("kind"))
        or "image"
    )
    size_cap = gates.VIDEO_MAX_SIZE if upload_kind == "video" else gates.MAX_FILE_SIZE
    if file.size > size_cap:
        return json_error(f"File too large. Max {size_cap // (1024 * 1024)} MB", 400, code="file_too_large")
    if file.size == 0:
        return json_error("Empty file", 400, code="empty_file")

    blob = file.read()


    # STEP 2: the kind picks its gate — documents/videos/audio are
    # stored as sent once their bytes prove the claimed type; a
    # photo goes through the signature sniff and the re-encode
    # ============================================================
    width = height = None
    preview = None
    if upload_kind == "file":
        ext, rejection = gates.accept_document(file.name, blob)
        if rejection:
            return json_error(rejection[0], 400, code=rejection[1])
    elif upload_kind == "video":
        ext, rejection = gates.accept_video(file.name, blob)
        if rejection:
            return json_error(rejection[0], 400, code=rejection[1])
    elif upload_kind == "audio":
        ext, rejection = gates.accept_audio(file.name, blob)
        if rejection:
            return json_error(rejection[0], 400, code=rejection[1])
    else:
        if not gates.sniff_image_format(blob):
            if not gates.allowed_image_name(file.name):
                return json_error(f"File type not allowed. Use: {', '.join(gates.ALLOWED_EXTENSIONS)}",
                                  400, code="bad_file_type")
            return json_error("File content does not match an allowed image format", 400, code="bad_file_content")

        ext, blob, rejection = gates.reencode_image(blob)
        if rejection:
            return json_error(rejection[0], 400, code=rejection[1])

        # The stored pixel size plus the micro copy — the client lays
        # a photo bubble out at its final proportions before the bytes
        # arrive, and upscales the blur while they download
        try:
            with Image.open(io.BytesIO(blob)) as stored:
                width, height = stored.size
                tiny = stored.convert("RGB")
                tiny.thumbnail((14, 14))
                tiny_buf = io.BytesIO()
                tiny.save(tiny_buf, format="JPEG", quality=60)
                preview = "data:image/jpeg;base64," + base64.b64encode(tiny_buf.getvalue()).decode("ascii")
        except Exception:
            width = height = None
            preview = None


    # STEP 3: the per-account storage quota, counted from the rows
    # under the account's own row lock. SELECT … FOR UPDATE on the
    # users row (a row that always exists — no table of its own)
    # serialises overlapping uploads per account: on PostgreSQL the
    # lock is held until the request's transaction commits
    # (ATOMIC_REQUESTS), which is exactly the sum + compare + write
    # + INSERT below, so a sibling upload waits at this line and
    # then counts the row this one inserted. Without it each of two
    # overlapping uploads sees the whole remaining budget — a
    # sibling's uncommitted row is invisible under READ COMMITTED
    # (which is also why a conditional INSERT would not close it).
    # SQLite has no row locks: Django drops the FOR UPDATE and the
    # statement is a plain one-column read
    # =============================================================
    user_id = request.user["id"]
    User.objects.select_for_update().filter(id=user_id).values_list("id", flat=True).first()
    used = Upload.objects.filter(user_id=user_id).aggregate(total=models.Sum("byte_size"))["total"] or 0
    if used + len(blob) > UPLOAD_QUOTA_BYTES:
        logger.warning("Upload quota reached by user %s (%d bytes stored)", user_id, used)
        return json_error(f"Storage quota reached. Max {UPLOAD_QUOTA_BYTES // (1024 * 1024)} MB per account",
                          413, code="quota_exceeded")


    # STEP 4: atomic write, then the ownership row (same request
    # transaction — a failed INSERT cannot leave an unowned file
    # that the quota would never count)
    # ==========================================================
    stored_name = f"{uuid.uuid4().hex}.{ext}"
    if not atomic_write(stored_name, blob):
        return json_error("Storage is unavailable, try again later", 507, code="storage_unavailable")

    Upload.objects.create(
        id=str(uuid.uuid4()), filename=stored_name, user_id=user_id,
        byte_size=len(blob), created_at=utc_now(),
    )

    return json_response({
        "url": f"/api/uploads/{stored_name}",
        "filename": stored_name,
        # What a document message carries as its attachment — the
        # name the sender chose, the byte size, the canonical mime
        "name": os.path.basename(file.name) or stored_name,
        "size": len(blob),
        "mime": gates.MIME_BY_EXT.get(ext) or f"image/{'jpeg' if ext == 'jpg' else ext}",
        # Photos only — None for documents, videos and audio
        "width": width,
        "height": height,
        "preview": preview,
    }, status=201)








############################################################
# delete_file
############################################################
#
# DELETE /api/uploads/<filename> — the uploader or an admin
# drops a stored file. A name with no ownership row is a
# 404 for everyone but an admin — an unknown name and an
# orphaned file look the same from outside. The 403/404
# checks stay HERE (they are the wire contract), and the
# same rule runs again inside storage.delete_upload, which
# also refuses a file another live record still shows — a
# post cover, an avatar, a chat photo — with 409
# still_referenced; an admin's delete skips that guard, so
# moderation can pull an abusive file from under its
# records. An already-missing file is still a 200 — the
# row goes and the caller got what it asked for — but a
# file that SURVIVES the unlink answers 500 delete_failed
# rather than lying: this is the erasure and moderation
# path. Runs in its own transaction: the /api/uploads/<name>
# dispatcher is non-atomic for the GET's sake (see
# serve_file), and a delete must still commit as one unit.
#
# Used by:
#   - admin/moderation tooling and manual erasure requests
############################################################

@transaction.atomic
@require_auth
def delete_file(request, filename):
    safe_name = safe_upload_name(filename)
    if not safe_name:
        return json_error("Invalid filename", 400, code="bad_filename")

    row = Upload.objects.filter(filename=safe_name).values("user_id").first()
    is_admin = request.user.get("role") == "admin"

    if row is None and not is_admin:
        return json_error("File not found", 404)
    if row is not None and not is_admin and row["user_id"] != request.user["id"]:
        return json_error("Only the owner can delete this file", 403)

    outcome = delete_upload(safe_name, request.user["id"], admin=is_admin)
    if outcome == "referenced":
        return json_error("The file is still used by a post, an avatar or a message", 409,
                          code="still_referenced")
    if outcome == "forbidden":
        # Unreachable after the checks above — kept so the sink's
        # verdict can never be answered as a success
        return json_error("Only the owner can delete this file", 403)

    if outcome == "removed":
        try:
            survived = os.path.lexists(os.path.join(upload_dir(), safe_name))
        except OSError:
            survived = True
        if survived:
            logger.error("Delete left %s on disk — there is no sweep, collect it by hand", safe_name)
            return json_error("The file could not be removed, try again later", 500, code="delete_failed")

    return json_response({"ok": True})








############################################################
# serve_file
############################################################
#
# GET /api/uploads/<filename> — public on purpose (avatars
# and post/chat images render for anonymous viewers too).
# The layers that keep it inside the flat directory: the
# URL converter refuses slashes, safe_upload_name admits
# ONLY the uuid-hex names this app writes, and a realpath
# check makes sure the bytes actually live where the name
# says — a symlink planted in the volume must not turn this
# unauthenticated route into an arbitrary file read.
#
# Caching: a stored name is a uuid written once, so its
# bytes can never change — a year of BROWSER caching,
# `immutable` (Cache-Control stays private: chat photos
# share this route, and a shared proxy cache has no
# business holding them), plus a strong ETag and a
# Last-Modified off one os.stat, so a client whose copy
# lapsed revalidates with a 304 instead of re-downloading
# the whole file. The old 24 h window with no validator
# could only ever re-transfer everything.
#
# ?s=thumb asks for the one derivative size (storage.
# ensure_thumbnail, made on first request): a 40 pt avatar
# no longer downloads the 2048 px original. Any other value
# — and a file with no derivative (a document, an
# animation, a photo already that small) — serves the
# original. The derivative passes the same realpath check.
#
# Never touches the database, and the dispatcher that
# routes it is marked non-atomic — under ATOMIC_REQUESTS
# every image GET used to open a connection for a BEGIN/
# COMMIT around no query at all (KNF-135).
#
# Byte ranges are honoured (single range only): the mobile
# video player refuses a file it cannot seek — iOS AVPlayer
# probes with Range and needs a 206 back, which the old
# Flask send_file gave it and FileResponse alone does not.
# A malformed Range falls back to the full 200 per spec; an
# unsatisfiable one answers 416.
#
# Used by:
#   - services/api/client.ts getUploadUrl — every rendered
#     avatar and post/chat image, and the chat video/audio
#     players (the Range consumers)
############################################################

# "bytes=start-end", either side optional but not both
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")

# Stored bytes never change under a name — a year, immutable
_FILE_CACHE_CONTROL = "private, max-age=31536000, immutable"


# A file opened at an offset that stops read() at the range
# end — what FileResponse streams for a 206
class _RangeReader:
    def __init__(self, handle, remaining):
        self._handle = handle
        self._remaining = remaining

    def read(self, size=-1):
        if self._remaining <= 0:
            return b""
        if size is None or size < 0 or size > self._remaining:
            size = self._remaining
        chunk = self._handle.read(size)
        self._remaining -= len(chunk)
        return chunk

    def close(self):
        self._handle.close()


# The cache headers every served answer carries — the 200,
# the 206 and the 304 alike
def _stamp_file_cache(response, etag, last_modified):
    response["Cache-Control"] = _FILE_CACHE_CONTROL
    response["ETag"] = etag
    response["Last-Modified"] = http_date(last_modified)
    return response


@transaction.non_atomic_requests
def serve_file(request, filename):
    safe_name = safe_upload_name(filename)
    if not safe_name:
        return json_error("Invalid filename", 400, code="bad_filename")

    directory = upload_dir()
    file_path = os.path.join(directory, safe_name)
    if not os.path.isfile(file_path):
        return json_error("File not found", 404)

    if os.path.realpath(file_path) != os.path.join(os.path.realpath(directory), safe_name):
        logger.warning("Refused %s: it resolves outside the upload directory", safe_name)
        return json_error("File not found", 404)

    # The gates' own mime table first — mimetypes knows no .m4a
    # or .m4v, and a voice note served as application/octet-stream
    # is one the mobile player will not open
    ext = safe_name.rsplit(".", 1)[1]
    content_type = (gates.MIME_BY_EXT.get(ext) or mimetypes.guess_type(safe_name)[0]
                    or "application/octet-stream")


    # STEP 1: the variant — ?s=thumb swaps in the derivative when
    # one exists (or can be made); everything else is the original
    # =============================================================
    variant = "o"
    if clean_param(request.GET.get("s")) == "thumb":
        thumb = ensure_thumbnail(safe_name)
        if thumb and os.path.realpath(thumb) == os.path.join(
                os.path.realpath(directory), THUMB_DIRNAME, safe_name):
            file_path = thumb
            variant = "t"


    # STEP 2: one stat for the size and both validators; a
    # request whose copy is still current gets its 304 here
    # (a failed If-Match gets its 412)
    # =====================================================
    stat = os.stat(file_path)
    size = stat.st_size
    last_modified = int(stat.st_mtime)
    etag = quote_etag(f"{safe_name.split('.', 1)[0]}-{variant}-{size:x}-{last_modified:x}")

    template = _stamp_file_cache(HttpResponse(), etag, last_modified)
    conditional = get_conditional_response(request, etag=etag, last_modified=last_modified,
                                           response=template)
    if conditional is not template:
        return conditional


    # STEP 3: a valid single range answers 206 with just that
    # window; suffix form ("bytes=-N") means the last N bytes
    # =======================================================
    match = _RANGE_RE.match(request.headers.get("Range", "").strip())
    if match and (match.group(1) or match.group(2)) and size > 0:
        if match.group(1):
            start = int(match.group(1))
            end = min(int(match.group(2)), size - 1) if match.group(2) else size - 1
        else:
            start = max(size - int(match.group(2)), 0)
            end = size - 1

        if start >= size or start > end:
            response = json_error("Range not satisfiable", 416)
            response["Content-Range"] = f"bytes */{size}"
            return response

        handle = open(file_path, "rb")
        handle.seek(start)
        response = FileResponse(_RangeReader(handle, end - start + 1),
                                content_type=content_type, status=206)
        response["Content-Range"] = f"bytes {start}-{end}/{size}"
        response["Content-Length"] = str(end - start + 1)
        response["Accept-Ranges"] = "bytes"
        return _stamp_file_cache(response, etag, last_modified)


    # STEP 4: no (or malformed) range — the whole file, with
    # Accept-Ranges advertising that seeking works here
    # ======================================================
    response = FileResponse(open(file_path, "rb"), content_type=content_type)
    response["Accept-Ranges"] = "bytes"
    return _stamp_file_cache(response, etag, last_modified)
