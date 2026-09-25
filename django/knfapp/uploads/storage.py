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
#
#  Ownership is decided HERE, not in the callers. The other
#  apps store whatever /api/uploads/ path a client handed
#  them (filenames are public — every avatar and feed
#  cover shows one), so a record pointing at somebody
#  else's file is an ordinary state, and the delete must
#  refuse to take that file with the record.
#
#  There is deliberately no orphan-file sweep: every
#  deletion path removes its own files, so an orphan only
#  ever comes from a crash window — rare enough for an
#  operator to clean by hand.
############################################################


import logging
import os
import re


from django.conf import settings
from django.db.models import Q, TextField
from django.db.models.functions import Cast


from knfapp.uploads.gates import (
    ALLOWED_AUDIO_EXTENSIONS,
    ALLOWED_DOC_EXTENSIONS,
    ALLOWED_VIDEO_EXTENSIONS,
    STORED_IMAGE_EXTENSIONS,
)
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
    final_path = os.path.join(upload_dir(), name)
    part_path = f"{final_path}.part"
    try:
        with open(part_path, "wb") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(part_path, final_path)
        return True
    except OSError:
        logger.error("Upload write failed for %s", name, exc_info=True)
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
# Used by:
#   - delete_upload (below)
#   - users/erasure.py — the on-commit sweep of an erased
#     account's files
############################################################

def unlink_upload_file(name):
    name = safe_upload_name(name)
    if not name:
        return False
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
