############################################################
#  [*] Upload storage — the disk side
#
#  Where the bytes live and the operations that touch them.
#  Stored names are a uuid4 hex plus a canonical extension
#  — FILENAME_RE is the gate every entry point shares, so
#  nothing path-like can ever be served, written or
#  deleted, and the directory stays flat. Its extension
#  table is BUILT from the upload gates' own admit-lists
#  (plus the containers the image re-encode writes), so a
#  kind the upload route accepts can never be one this gate
#  refuses to serve or unlink.
#
#    upload_dir()             — resolved + created once per
#                               process
#    atomic_write(...)        — .part, fsync, rename: a full
#                               disk never leaves a
#                               truncated file being served
#                               as a valid image
#    owns_upload(user, url)   — the acceptance-side check
#                               for a client-supplied upload
#                               url: a registered file of
#                               the acting user's own
#    unlink_upload_file(name) — the raw unlink alone, for a
#                               caller that has already
#                               settled the ownership row
#    delete_upload(path, owner_id, admin=False)
#                             — the ONE sink through which
#                               a stored file leaves disk:
#                               owner-checked, reference-
#                               guarded, answers an outcome
#                               word
#    ensure_thumbnail(name)   — the one derivative size, made
#                               on first request (?s=thumb)
#    sweep_orphan_uploads()   — the reachability sweep for
#                               files nothing references
#
#  Ownership is decided HERE, not in the callers. The other
#  apps store whatever /api/uploads/ path a client handed
#  them (filenames are public — every avatar and feed
#  cover shows one), so a record pointing at somebody
#  else's file is an ordinary state, and the delete must
#  refuse to take that file with the record.
#
#  Orphans are NOT only a crash-window rarity (an earlier
#  version of this banner said so, and the claim was
#  false): a retried chat gallery re-uploads the photos an
#  aborted attempt already stored, a definitively refused
#  send never deletes the file it carried, and an
#  abandoned pick leaves its upload behind — every one
#  charged to the account's quota for good (KNF-118).
#  sweep_orphan_uploads is the reachability pass that
#  reclaims them; the daily maintenance command runs it.
############################################################


import io
import logging
import os
import re
from datetime import timedelta


from PIL import Image
from django.conf import settings
from django.db.models import Q, TextField
from django.db.models.functions import Cast


from knfapp.uploads.gates import (
    ALLOWED_AUDIO_EXTENSIONS,
    ALLOWED_DOC_EXTENSIONS,
    ALLOWED_VIDEO_EXTENSIONS,
    STORED_IMAGE_EXTENSIONS,
)
from knfapp.common.timestamps import utc_now
from knfapp.uploads.models import Upload


logger = logging.getLogger(__name__)

# Every extension this module may be asked to serve or unlink,
# from the SAME tuples the upload gates admit — a kind added in
# gates.py is admitted here by construction, never by a second
# hand-kept list (the voice-note extensions were once missing
# from this one: a stored .m4a answered 201 and then could
# neither be played nor deleted). "jpeg" stays for rows
# written before the re-encode settled on "jpg"
STORED_EXTENSIONS = (
    STORED_IMAGE_EXTENSIONS + ("jpeg",)
    + ALLOWED_DOC_EXTENSIONS + ALLOWED_VIDEO_EXTENSIONS + ALLOWED_AUDIO_EXTENSIONS
)
FILENAME_RE = re.compile(r"^[0-9a-f]{32}\.(" + "|".join(STORED_EXTENSIONS) + r")$")

UPLOAD_QUOTA_BYTES = 100 * 1024 * 1024   # stored bytes one account may hold
UPLOAD_RATE_MAX = 20                     # uploads per user per 5 min window

# The one derivative size: the longest edge of a ?s=thumb copy —
# the largest avatar the app draws (88 pt) at 3x density
THUMB_EDGE = 320
THUMB_DIRNAME = "thumbs"
# Only still photos get a derivative — the re-encode stores gif
# and webp only for ANIMATIONS, whose first frame is no thumbnail
THUMB_SOURCE_EXTENSIONS = ("jpg", "jpeg", "png")
THUMB_JPEG_QUALITY = 82

# An unreferenced upload younger than this is left alone: a chat
# send parked in an offline outbox still carries its url, and a
# photo picked for a post is uploaded before the post exists
ORPHAN_MIN_AGE = timedelta(days=7)

# Any run of hex inside a referencing column — a stored name's
# uuid part in a relative url, an absolute same-origin one, a
# JSON blob holding either. Every 32-char window of a run counts
# (see referenced_upload_names): over-matching can only keep an
# orphan, while under-matching would delete a live file
_HEX_RUN_RE = re.compile(r"[0-9a-f]{32,}")

# Resolved (and created) once per process by upload_dir()
_upload_dir = None


def upload_dir():
    global _upload_dir
    if _upload_dir is None:
        resolved = os.path.abspath(settings.UPLOAD_DIR)
        os.makedirs(resolved, exist_ok=True)
        _upload_dir = resolved
    return _upload_dir


def safe_upload_name(value):
    # Reduce whatever was stored to a bare name and admit only the
    # shape this module writes — the one gate serve/delete share
    name = (value or "").rsplit("/", 1)[-1]
    if not name or not FILENAME_RE.match(name):
        return None
    return name


def atomic_write(name, blob):
    return _write_atomically(os.path.join(upload_dir(), name), blob)


def _write_atomically(final_path, blob):
    part_path = f"{final_path}.part"
    try:
        with open(part_path, "wb") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(part_path, final_path)
        return True
    except OSError:
        logger.error("Upload write failed for %s", os.path.basename(final_path), exc_info=True)
        try:
            os.unlink(part_path)
        except OSError:
            pass
        return False








############################################################
# owns_upload
############################################################
#
# The acceptance-side check for a client-supplied upload
# url ("/api/uploads/<name>" or the bare name): True only
# when the name is a stored shape AND its ownership row
# belongs to the acting user. Upload.user is SET_NULL, so
# an ownerless row (an erased account's leftover) is
# nobody's — and a user id of None can never match it. A
# path-prefix test alone is not enough anywhere a record
# will later hand its url to delete_upload: the prefix is
# public knowledge, the row is not.
#
# Used by:
#   - news/api/views.py create_post — the cover
#   - social/api/views.py create_post / update_post — the
#     wall cover
#   - users/profile.py apply_profile_patch — the avatar
############################################################

def owns_upload(user_id, url):
    if not user_id or not isinstance(url, str):
        return False
    name = safe_upload_name(url)
    if not name:
        return False
    return Upload.objects.filter(filename=name, user_id=user_id).exists()








############################################################
# unlink_upload_file
############################################################
#
# The raw unlink alone. Takes the STORED NAME (never a
# path) and touches no row: the caller has already settled
# the ownership row — and, in erasure's case, is running
# after its transaction committed, where the file must go
# but nothing may raise. True when a file was actually
# removed, False when it was already gone; any other
# filesystem error is logged and swallowed, the survivor
# waits for the operator's hand. The name still passes the
# FILENAME_RE gate (the shape this module writes is the
# only one it will ever unlink), so a tampered row cannot
# point the unlink outside the directory — a name that
# fails the gate is False too.
#
# The ?s=thumb derivative goes with it, best-effort and
# regardless of the original's outcome: it is regrowable
# cache, and a thumbnail must never outlive its photo.
#
# Used by:
#   - delete_upload (below)
#   - sweep_orphan_uploads (below)
#   - users/erasure.py — the on-commit sweep of an erased
#     account's files
############################################################

def unlink_upload_file(name):
    name = safe_upload_name(name)
    if not name:
        return False
    try:
        os.unlink(thumbnail_path(name))
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("Could not unlink the thumbnail of %s", name, exc_info=True)
    try:
        os.unlink(os.path.join(upload_dir(), name))
        return True
    except FileNotFoundError:
        return False
    except OSError:
        logger.warning("Could not unlink upload %s", name, exc_info=True)
        return False








############################################################
# _referenced_elsewhere
############################################################
#
# The still-in-use guard: True when any live record still
# names the file — a user's avatar_url, a post's image_url,
# or one of a chat message's slots (image_url,
# attachment_url, and the JSON columns attachment_meta
# .thumbnailUrl, link_preview.imageUrl, gallery[].url).
# Substring match on the bare name, so both the relative
# and the absolute same-origin form the chat send accepts
# count. The JSON columns are CAST to text first: a
# JSONField `__contains` means JSON containment, not a
# substring, and the cast reads the same on SQLite (text
# already) and PostgreSQL (jsonb). A deployment without the
# chat tables counts as "not referenced" — the query is
# wrapped, never fatal.
#
# The caller has already blanked or deleted its OWN record
# (an unsent message's slots are NULLed, an expired one is
# gone, a replaced avatar is overwritten) before asking, so
# the record being deleted never holds its own file back.
#
# Used by:
#   - delete_upload (below) — skipped for admin=True
############################################################

def _referenced_elsewhere(name):
    from knfapp.users.models import User
    if User.objects.filter(avatar_url__contains=name).exists():
        return True

    try:
        from knfapp.news.models import NewsPost
        if NewsPost.objects.filter(image_url__contains=name).exists():
            return True
    except Exception:
        logger.info("Reference check skipped the news tables for %s", name)

    try:
        from knfapp.chat.models import Message
        if Message.objects.annotate(
            meta_text=Cast("attachment_meta", TextField()),
            preview_text=Cast("link_preview", TextField()),
            gallery_text=Cast("gallery", TextField()),
        ).filter(
            Q(image_url__contains=name) | Q(attachment_url__contains=name)
            | Q(meta_text__contains=name) | Q(preview_text__contains=name)
            | Q(gallery_text__contains=name)
        ).exists():
            return True
    except Exception:
        logger.info("Reference check skipped the chat tables for %s", name)

    return False








############################################################
# delete_upload
############################################################
#
# The one sink through which a stored file leaves disk.
# Takes the value the other apps stored, the id of the user
# the operation acts FOR (the sender of an unsent message,
# the author of a deleted post, the profile owner) and,
# for moderation, admin=True. Answers one word:
#
#   "removed"    — file unlinked and the ownership row gone
#   "missing"    — nothing to do: not a stored upload at all
#                  (a scraped http(s) image url, a foreign
#                  path) or the file was already gone — the
#                  row, if any, still goes so the quota
#                  frees. Callers treat this as success.
#   "forbidden"  — the row belongs to somebody else, or
#                  there is no row and the caller is not an
#                  admin (an unknown name and an orphaned
#                  file look the same from outside — the
#                  rule api/views.py delete_file answers 404
#                  with). Nothing is touched.
#   "referenced" — another live record still names the file
#                  (see _referenced_elsewhere): a forwarded
#                  copy of the same photo, a cover also used
#                  as an avatar. Nothing is touched; the
#                  last reference to go takes the file.
#
# admin=True bypasses both the owner check and the
# reference guard: moderation must be able to remove an
# abusive file even while records point at it. Never raises
# on a filesystem error — the caller is mid-delete/replace
# and must succeed regardless; a survivor waits for the
# operator's hand. The warning on a refusal logs the stored
# name only, never anything of the caller's.
#
# Used by:
#   - api/views.py delete_file — the wire route (403/404
#     decided there, 409 still_referenced from here)
#   - users/profile.py — the avatar a profile update
#     replaced, on the commit
#   - news/api/views.py delete_post, social/api/views.py
#     delete_post — the cover, as the author, on the commit
#   - chat/api/views.py delete_message / _sweep_expired —
#     unsent and expired message files, as the sender
############################################################

def delete_upload(path, owner_id, *, admin=False):
    # STEP 1: reduce to a stored name — anything else was never
    # ours to delete and reads as already gone
    # ========================================================
    if not isinstance(path, str):
        return "missing"
    name = safe_upload_name(path)
    if not name:
        return "missing"


    # STEP 2: the ownership row decides for everyone but an admin;
    # an ownerless row (SET_NULL after an erasure) is nobody's
    # ============================================================
    row = Upload.objects.filter(filename=name).values("user_id").first()
    if not admin:
        if row is None or row["user_id"] is None or row["user_id"] != owner_id:
            logger.warning("Refused to delete upload %s: not owned by the acting user", name)
            return "forbidden"


        # STEP 3: a file another live record still shows stays —
        # the last reference to go takes it
        # ======================================================
        if _referenced_elsewhere(name):
            return "referenced"


    # STEP 4: unlink, then the row — the quota frees even when the
    # file had already gone
    # ===========================================================
    removed = unlink_upload_file(name)
    Upload.objects.filter(filename=name).delete()
    return "removed" if removed else "missing"








############################################################
# thumbnail_path / ensure_thumbnail
############################################################
#
# The one derivative size (KNF-136): a still photo's
# THUMB_EDGE-px copy under UPLOAD_DIR/thumbs/<stored name>,
# made on the FIRST ?s=thumb request and served from disk
# ever after — no migration, no upload-time cost, old
# uploads included, and no size parameter a caller could
# turn into a CPU oracle (one fixed size, at most one
# decode per stored photo). ensure_thumbnail answers the
# derivative's path, or None when the original should be
# served instead: not a stored still photo, already inside
# the edge (the original IS the thumbnail), or any failure
# on the way — a derivative can never cost a viewer the
# picture.
#
# Server-made, so it counts against no quota; it leaves
# disk with its original through unlink_upload_file, and
# the thumbs/ folder is safe to wipe by hand — it regrows
# on demand.
#
# Used by:
#   - api/views.py serve_file — the ?s=thumb variant
#   - unlink_upload_file (above) — takes the derivative along
############################################################

def thumbnail_path(name):
    return os.path.join(upload_dir(), THUMB_DIRNAME, name)


def ensure_thumbnail(name):
    # STEP 1: only a stored still photo has a derivative, and one
    # already made is simply reused
    # ===========================================================
    name = safe_upload_name(name)
    if not name or name.rsplit(".", 1)[1] not in THUMB_SOURCE_EXTENSIONS:
        return None
    target = thumbnail_path(name)
    if os.path.isfile(target):
        return target


    # STEP 2: shrink in the source's own format — Pillow's
    # thumbnail() decodes a JPEG at a reduced scale directly, so
    # the full 2048 px buffer is never built for it
    # ==========================================================
    try:
        with Image.open(os.path.join(upload_dir(), name)) as img:
            if max(img.size) <= THUMB_EDGE:
                return None
            img.thumbnail((THUMB_EDGE, THUMB_EDGE))
            buffer = io.BytesIO()
            if name.endswith(".png"):
                img.save(buffer, format="PNG", optimize=True)
            else:
                img.convert("RGB").save(buffer, format="JPEG", quality=THUMB_JPEG_QUALITY,
                                        optimize=True, progressive=True)
    except Exception:
        logger.warning("Thumbnail of %s could not be made — serving the original", name, exc_info=True)
        return None


    # STEP 3: the atomic write — two first requests racing each
    # write the same bytes, and the rename makes either one whole
    # ===========================================================
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
    except OSError:
        logger.warning("Thumbnail folder unavailable — serving the original of %s", name, exc_info=True)
        return None
    return target if _write_atomically(target, buffer.getvalue()) else None








############################################################
# referenced_upload_names
############################################################
#
# The uuid part of every stored name any live record still
# shows, in ONE pass per table instead of one reference
# query per file: the same slots _referenced_elsewhere
# guards (an avatar, a post cover, a chat message's image/
# attachment and its JSON columns) plus the free text a
# pasted upload link can live in (post bodies, comments,
# message text) and the map's entity documents (room
# photos are upload references), scanned for hex runs so
# the relative, the absolute same-origin and the JSON-
# embedded forms all count — conservatively, every 32-char
# window of a longer run too. A table that cannot be read
# ABORTS the pass (raises): an unreadable reference set
# must never look like "nothing is referenced" to the
# sweep below.
#
# Used by:
#   - sweep_orphan_uploads (below)
############################################################

def referenced_upload_names():
    from knfapp.chat.models import Message
    from knfapp.news.models import NewsComment, NewsPost
    from knfapp.users.models import User
    from knfapp.wayfind.models import WfEntity

    names = set()

    def collect(values):
        for value in values:
            if not value:
                continue
            for run in _HEX_RUN_RE.findall(str(value)):
                names.update(run[i:i + 32] for i in range(len(run) - 31))

    collect(User.objects.exclude(avatar_url__isnull=True).values_list("avatar_url", flat=True))
    collect(NewsPost.objects.exclude(image_url__isnull=True).values_list("image_url", flat=True))
    collect(NewsPost.objects.values_list("content", flat=True).iterator())
    collect(NewsComment.objects.values_list("text", flat=True).iterator())
    for row in Message.objects.values_list("image_url", "attachment_url", "attachment_meta",
                                           "link_preview", "gallery", "text").iterator():
        collect(row)
    collect(WfEntity.objects.values_list("data", flat=True).iterator())
    return names








############################################################
# sweep_orphan_uploads
############################################################
#
#   sweep_orphan_uploads()              — delete the orphans
#   sweep_orphan_uploads(dry_run=True)  — only count them
#
# The reachability pass behind KNF-118: an upload row older
# than ORPHAN_MIN_AGE whose uuid no live record shows
# (referenced_upload_names) is an orphan — its file
# (and thumbnail) is unlinked and its row deleted, which is
# what frees the uploader's quota. Young rows are never
# touched (an offline chat outbox or an unfinished post may
# still hold the url). Answers (orphans found, bytes
# freed); a dry run touches nothing and reports what a real
# run would. One file's failure is logged and skipped.
#
# Used by:
#   - scraper/management/commands/maintenance.py — the
#     daily housekeeping tick
############################################################

def sweep_orphan_uploads(dry_run=False):
    cutoff = utc_now() - ORPHAN_MIN_AGE
    referenced = referenced_upload_names()

    orphans = [
        (filename, byte_size)
        for filename, byte_size in Upload.objects.filter(created_at__lt=cutoff)
        .values_list("filename", "byte_size").iterator()
        if filename.split(".", 1)[0] not in referenced
    ]
    freed = sum(byte_size or 0 for _, byte_size in orphans)
    if dry_run or not orphans:
        return len(orphans), freed

    for filename, _ in orphans:
        try:
            unlink_upload_file(filename)
            Upload.objects.filter(filename=filename).delete()
        except Exception:
            logger.exception("Orphan sweep could not remove %s", filename)
    logger.info("Orphan sweep removed %d unreferenced upload(s), %d bytes", len(orphans), freed)
    return len(orphans), freed
