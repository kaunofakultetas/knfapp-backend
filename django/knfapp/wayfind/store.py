############################################################
#  [*] Wayfind store — the content-addressed file store
#
#  The two disk primitives api/views.py, api/captures.py and
#  stitch.py share: resolving a UPLOAD_DIR/wayfind/<kind>
#  directory and the atomic writes. Resolved per call rather
#  than cached, so override_settings hands every test its
#  own directory.
############################################################


import logging
import os

from django.conf import settings


logger = logging.getLogger(__name__)


def store_dir(kind):
    # UPLOAD_DIR/wayfind/<kind>, created on first use
    resolved = os.path.join(os.path.abspath(settings.UPLOAD_DIR), "wayfind", kind)
    os.makedirs(resolved, exist_ok=True)
    return resolved


def write_once(directory, name, blob) -> bool:
    # Content-addressed atomic write: the bytes go to
    # <name>.part, are fsynced and os.replace-d into place; a
    # file already there IS the same bytes (its name is their
    # hash), so nothing is rewritten. True when the file
    # exists afterwards
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


def write_replace(directory, name, blob) -> bool:
    # The mutable cousin of write_once, for files NAMED BY
    # ROLE rather than by content — a capture's <targetId>.jpg,
    # where a re-shot frame must replace the old one. Same
    # .part + fsync + os.replace dance, but an existing file
    # is overwritten instead of trusted
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
