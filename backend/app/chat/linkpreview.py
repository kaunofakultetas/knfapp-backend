############################################################
# chat/linkpreview.py — link unfurling for chat messages
############################################################
#
# A message whose text carries a URL gets a small card: the
# page's title, description, site name and a picture — fetched
# by the SERVER, never by the phones, so a link never beacons
# every reader's address to a stranger's host, and the picture
# is stored as one of our own uploads (re-encoded through the
# photo pipeline: no metadata, bounded pixels) instead of a
# hot-link.
#
# Runs OFF the request thread after the message is committed
# (socketio.start_background_task from send_message): the send
# answers at once with linkPreview: null, and the room hears a
# 'message_updated' broadcast when the card is ready. Every
# failure is logged and swallowed — a preview never owes
# anybody an error.
#
# Fetch discipline (the SSRF rules):
#   - http(s) only, ports 80/443 only, the host must resolve to
#     a PUBLIC address (loopback, private, link-local, multicast,
#     reserved and IPv6 unique-local ranges refused), checked
#     again on every redirect (at most 3), and the request
#     carries our own user agent;
#   - the page: 4 s connect / 6 s read, the first 512 KB only,
#     text/html only; the picture: 3 MB cap, image/* only, then
#     _reencode_image (which decodes, bounds and strips it).
#
# Split into:
#
#   find_url          — the first http(s) URL in a text
#   _public_address   — the DNS/IP gate
#   _fetch_bounded    — a capped GET with redirect re-checks
#   extract_preview   — HTML → {url, title, description, siteName, image}
#   _store_image      — the picture as an own upload
#   unfurl_message    — the background task (fetch, store, update, emit)
#
# Used by:
#   - chat/routes.py — send_message starts unfurl_message
############################################################

import io
import ipaddress
import logging
import os
import re
import socket
import uuid
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from app.database import get_db, utc_now_iso

logger = logging.getLogger(__name__)

USER_AGENT = "KNFAPP/1.0 (+https://knfapp.knf-hosting.lt; link preview)"
PAGE_TIMEOUT = (4, 6)
PAGE_MAX_BYTES = 512 * 1024
IMAGE_MAX_BYTES = 3 * 1024 * 1024
MAX_REDIRECTS = 3

# The first http(s) URL — a scheme is required (a bare host in
# chat is not worth a network round-trip)
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def find_url(text):
    if not text:
        return None
    match = _URL_RE.search(text)
    if not match:
        return None
    # Trailing punctuation belongs to the sentence, not the link
    return match.group(0).rstrip(".,;:!?)”’'\"")







############################################################
# _public_address
############################################################
#
# True when every address the host resolves to is public. A
# host that resolves to nothing, or to any private/loopback/
# link-local/multicast/reserved address, is refused — the DNS
# answer is what the fetch would actually connect to.
#
# Used by:
#   - _fetch_bounded (below), per hop
############################################################

def _public_address(url):
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    if parts.port not in (None, 80, 443):
        return False
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (address.is_private or address.is_loopback or address.is_link_local or address.is_multicast
                or address.is_reserved or address.is_unspecified or not address.is_global):
            return False
    return True







############################################################
# _fetch_bounded
############################################################
#
# GET with redirects followed by hand (each hop re-validated),
# the body read as a capped stream. Returns (final_url, bytes,
# content_type) or None.
#
# Used by:
#   - extract_preview / _store_image (below)
############################################################

def _fetch_bounded(url, max_bytes, timeout, accept):
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        if not _public_address(current):
            logger.info("Link preview refused (not a public address): %s", current)
            return None
        try:
            resp = requests.get(
                current, timeout=timeout, stream=True, allow_redirects=False,
                headers={"User-Agent": USER_AGENT, "Accept": accept, "Accept-Language": "lt,en;q=0.8"},
            )
        except requests.RequestException as e:
            logger.info("Link preview fetch failed for %s: %s", current, e)
            return None
        try:
            if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                current = urljoin(current, resp.headers["Location"])
                continue
            if resp.status_code != 200:
                return None
            content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            chunks = []
            size = 0
            for chunk in resp.iter_content(chunk_size=16 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    break
                chunks.append(chunk)
            return current, b"".join(chunks), content_type
        finally:
            resp.close()
    return None







############################################################
# extract_preview
############################################################
#
# The page's Open Graph / Twitter / plain tags, in that order
# of trust, trimmed to card size. `image` is the absolute URL
# of the picture to fetch — not yet ours.
#
# Used by:
#   - unfurl_message (below)
#   - tests
############################################################

def extract_preview(html, page_url):
    soup = BeautifulSoup(html, "lxml")

    def meta(*names):
        for name in names:
            tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
            if tag and tag.get("content"):
                return tag["content"].strip()
        return ""

    title = meta("og:title", "twitter:title") or (soup.title.get_text(strip=True) if soup.title else "")
    description = meta("og:description", "twitter:description", "description")
    site_name = meta("og:site_name") or (urlsplit(page_url).hostname or "")
    image = meta("og:image", "og:image:url", "twitter:image")
    if image:
        image = urljoin(page_url, image)
    if not title and not description:
        return None
    return {
        "url": page_url,
        "title": title[:200],
        "description": description[:300],
        "siteName": site_name[:100],
        "image": image or None,
    }







############################################################
# _store_image
############################################################
#
# The card's picture as an own upload of the sender: fetched
# under the same gate, bounded, re-encoded through the photo
# pipeline, written atomically, recorded in uploads. Returns
# the relative url or None.
#
# Used by:
#   - unfurl_message (below)
############################################################

def _store_image(image_url, sender_id):
    import base64
    import io

    from PIL import Image
    from werkzeug.utils import secure_filename
    from app.uploads.routes import _get_upload_dir, _reencode_image

    fetched = _fetch_bounded(image_url, IMAGE_MAX_BYTES, PAGE_TIMEOUT, "image/*")
    if not fetched:
        return None
    _final, blob, content_type = fetched
    if not content_type.startswith("image/") or not blob:
        return None
    ext, encoded, rejection = _reencode_image(blob)
    if rejection or not encoded:
        return None

    # The ~14px micro copy the card blurs while the real picture
    # downloads (same trick the upload route plays for photos)
    tiny_preview = None
    try:
        with Image.open(io.BytesIO(encoded)) as stored:
            tiny = stored.convert("RGB")
            tiny.thumbnail((14, 14))
            tiny_buf = io.BytesIO()
            tiny.save(tiny_buf, format="JPEG", quality=60)
            tiny_preview = "data:image/jpeg;base64," + base64.b64encode(tiny_buf.getvalue()).decode("ascii")
    except Exception:
        tiny_preview = None

    safe_name = secure_filename(f"{uuid.uuid4().hex}.{ext}")
    final_path = os.path.join(_get_upload_dir(), safe_name)
    part_path = f"{final_path}.part"
    try:
        with open(part_path, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(part_path, final_path)
    except OSError:
        logger.warning("Link preview image write failed", exc_info=True)
        try:
            os.unlink(part_path)
        except OSError:
            pass
        return None

    db = get_db()
    try:
        db.execute(
            "INSERT INTO uploads (id, filename, user_id, byte_size, created_at) VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), safe_name, sender_id, len(encoded), utc_now_iso()),
        )
        db.commit()
    finally:
        db.close()
    return f"/api/uploads/{safe_name}", tiny_preview







############################################################
# unfurl_message
############################################################
#
# The background task: fetch the page, build the card, store
# the picture, write link_preview on the row (unless the row
# was unsent meanwhile) and tell the room.
#
# Used by:
#   - chat/routes.py send_message — socketio.start_background_task
############################################################

def unfurl_message(socketio, conv_id, msg_id, url, sender_id):
    try:
        fetched = _fetch_bounded(url, PAGE_MAX_BYTES, PAGE_TIMEOUT, "text/html,application/xhtml+xml")
        if not fetched:
            return
        final_url, body, content_type = fetched
        if content_type not in ("text/html", "application/xhtml+xml"):
            return
        preview = extract_preview(body.decode("utf-8", errors="replace"), final_url)
        if not preview:
            return
        image_url = preview.pop("image", None)
        stored = _store_image(image_url, sender_id) if image_url else None
        preview["imageUrl"], preview["imagePreview"] = stored if stored else (None, None)

        import json
        db = get_db()
        try:
            row = db.execute("SELECT deleted_at FROM messages WHERE id = ? AND conversation_id = ?", (msg_id, conv_id)).fetchone()
            if not row or row["deleted_at"] is not None:
                return
            db.execute("UPDATE messages SET link_preview = ? WHERE id = ?", (json.dumps(preview), msg_id))
            db.commit()
        finally:
            db.close()

        from app.chat.events import emit_message_updated
        emit_message_updated(socketio, conv_id, msg_id, {"linkPreview": preview})
    except Exception:
        logger.exception("Link preview failed for message %s", msg_id)
