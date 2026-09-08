############################################################
#  [*] Wayfind store — the content-addressed file store
#
#  The two disk primitives routes.py and stitch.py share:
#  resolving a UPLOAD_DIR/wayfind/<kind> directory and the
#  atomic write-once for content-addressed files. They moved
#  out of routes.py when the capture stitcher (stitch.py)
#  needed to store its composed panorama through the exact
#  same path as the upload route — one store, one naming
#  rule, whoever writes.
############################################################


import logging
import os

from flask import current_app


logger = logging.getLogger(__name__)









############################################################
# store_dir
############################################################
#
# UPLOAD_DIR/wayfind/<kind>, created on first use. Resolved
# per call rather than cached: the tests hand every app a
# different directory. Needs an app context (request or
# app.app_context()) for current_app.
#
# Used by:
#   - app/wayfind/routes.py — upload_panorama, serve_panorama,
#     upload_plan, serve_plan (as _store_dir)
#   - app/wayfind/captures.py — the frame files
#   - app/wayfind/stitch.py — storing the composed panorama
############################################################

def store_dir(kind: str) -> str:
    resolved = os.path.join(os.path.abspath(current_app.config["UPLOAD_DIR"]), "wayfind", kind)
    os.makedirs(resolved, exist_ok=True)
    return resolved









############################################################
# write_once
############################################################
#
# Content-addressed atomic write: the bytes go to <name>.part,
# are fsynced and os.replace-d into place; a file already
# there IS the same bytes (its name is their hash), so nothing
# is rewritten. Answers True when the file exists afterwards.
#
# Used by:
#   - app/wayfind/routes.py — upload_panorama, upload_plan
#     (as _write_once)
#   - app/wayfind/stitch.py — storing the composed panorama
############################################################

def write_once(directory: str, name: str, blob: bytes) -> bool:

    final_path = os.path.join(directory, name)
    if os.path.exists(final_path):
        return True

    part_path = f"{final_path}.part"
    try:
        with open(part_path, "wb") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(part_path, final_path)
        return True
    except OSError:
        logger.error("Wayfind write failed for %s", name, exc_info=True)
        try:
            os.unlink(part_path)
        except OSError:
            pass
        return False









############################################################
# write_replace
############################################################
#
# The mutable cousin of write_once, for files NAMED BY ROLE
# rather than by content — a capture's <targetId>.jpg, where
# a re-shot frame must replace the old one. Same .part +
# fsync + os.replace dance, but an existing file is
# overwritten instead of trusted.
#
# Used by:
#   - app/wayfind/captures.py — upload_capture_frame
############################################################

def write_replace(directory: str, name: str, blob: bytes) -> bool:

    final_path = os.path.join(directory, name)
    part_path = f"{final_path}.part"
    try:
        with open(part_path, "wb") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(part_path, final_path)
        return True
    except OSError:
        logger.error("Wayfind write failed for %s", name, exc_info=True)
        try:
            os.unlink(part_path)
        except OSError:
            pass
        return False
