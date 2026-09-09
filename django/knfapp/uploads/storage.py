############################################################
#  [*] Upload storage — the disk side
#
#  Where the bytes live and the two operations that touch
#  them. Stored names are a uuid4 hex plus a canonical
#  extension — FILENAME_RE is the gate every entry point
#  shares, so nothing path-like can ever be served, written
#  or deleted, and the directory stays flat.
#
#    upload_dir()        — resolved + created once per process
#    atomic_write(...)   — .part, fsync, rename: a full disk
#                          never leaves a truncated file
#                          being served as a valid image
#    delete_upload(path) — the shared cleanup helper the
#                          other apps call with whatever
#                          they stored ("/api/uploads/<name>"
#                          or the bare name)
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


from knfapp.uploads.models import Upload


logger = logging.getLogger(__name__)

FILENAME_RE = re.compile(r"^[0-9a-f]{32}\.(jpg|jpeg|png|gif|webp|pdf|docx|xlsx|pptx|zip|txt|mp4|mov|m4v|webm)$")

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
# delete_upload
############################################################
#
# Takes the value the OTHER apps stored and removes both
# the file and its ownership row. True when a file was
# actually unlinked; False for a value that is not a stored
# upload (a scraped http(s) image url, a foreign path) or a
# file already gone. Never raises on a filesystem error —
# the caller is mid-delete/replace and must succeed
# regardless; a survivor waits for the operator's hand.
#
# Used by:
#   - api/views.py delete_file
#   - users/api/auth_views.py — the avatar a profile update
#     replaced
#   - news/chat — post covers, unsent and expired message
#     files
############################################################

def delete_upload(path):
    if not isinstance(path, str):
        return False
    name = safe_upload_name(path)
    if not name:
        return False

    removed = False
    try:
        os.unlink(os.path.join(upload_dir(), name))
        removed = True
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("Could not unlink upload %s", name, exc_info=True)

    # The ownership row goes regardless, so the quota frees up
    Upload.objects.filter(filename=name).delete()
    return removed
