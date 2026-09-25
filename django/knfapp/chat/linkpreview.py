############################################################
#  [*] chat/linkpreview.py — link unfurling for messages
#
#  A message whose text carries a URL gets a small card: the
#  page's title, description, site name and a picture —
#  fetched by the SERVER, never by the phones, so a link
#  never beacons every reader's address to a stranger's
#  host, and the picture is stored as one of our own uploads
#  (re-encoded through the photo pipeline: no metadata,
#  bounded pixels) instead of a hot-link.
#
#  Runs OFF the request thread after the message is
#  committed (a daemon thread from send_message): the send
#  answers at once with linkPreview: null, and the room
#  hears a 'message_updated' broadcast when the card is
#  ready. Every failure is logged and swallowed — a preview
#  never owes anybody an error.
#
#  Fetch discipline (the SSRF rules):
#    - http(s) only, ports 80/443 only, the host must
#      resolve to a PUBLIC address (loopback, private,
#      link-local, multicast, reserved refused), checked
#      again on every redirect (at most 3), under our own
#      user agent;
#    - the connection goes to THE ADDRESS THE GATE VETTED.
#      The host is resolved once per hop and the socket is
#      opened to that IP (Host header, TLS SNI and the
#      certificate check keep the hostname) — a second,
#      independent resolution inside the HTTP client was a
#      DNS-rebinding window: public for the gate, 127.0.0.1
#      for the connect;
#    - the page: 4 s connect / 6 s read, the first 512 KB
#      only, text/html only; the picture: 3 MB cap, image/*
#      only, then the uploads re-encode gate (decode, bound,
#      strip).
#
#  Storage discipline: the card's picture is an upload of
#  the SENDER's, so it counts against their 100 MB quota
#  like any upload (an account at the ceiling gets the text
#  card without a picture), and it is stored once per
#  (sender, source image) — the name is derived from both,
#  so posting the same link again reuses the stored file
#  instead of writing another copy.
#
#  Split into:
#
#    find_url            — the first http(s) URL in a text
#    _public_address     — the DNS/IP gate → the vetted IP
#    _PinnedHTTPSAdapter — TLS to the vetted IP, hostname kept
#    _fetch_bounded      — a capped GET with redirect re-checks
#    extract_preview     — HTML → {url, title, description,
#                          siteName, image}
#    _tiny_preview       — the ~14px blur of a stored picture
#    _store_image        — the picture as an own upload
#    unfurl_message      — the background task
############################################################


import hashlib
import ipaddress
import logging
import re
import socket
import uuid
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter

from django.db import IntegrityError, connection

from knfapp.chat.models import Message

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
# The ONE address a hop may connect to, or None. The host is
# resolved once; if it resolves to nothing, or ANY answer is
# private/loopback/link-local/multicast/reserved/non-global,
# the hop is refused. Otherwise the first vetted answer is
# returned — and _fetch_bounded connects to exactly that IP,
# so there is no second resolution a rebinding DNS server
# could answer differently. A port other than 80/443, a
# scheme other than http(s) or an unencodable host name is
# refused too.
#
# Used by:
#   - _fetch_bounded (below), per hop
############################################################

def _public_address(url):
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    if port not in (None, 80, 443):
        return None
    try:
        infos = socket.getaddrinfo(parts.hostname, port or (443 if parts.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        return None
    if not infos:
        return None
    vetted = None
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            return None
        if (address.is_private or address.is_loopback or address.is_link_local or address.is_multicast
                or address.is_reserved or address.is_unspecified or not address.is_global):
            return None
        if vetted is None:
            vetted = address
    return vetted








############################################################
# _PinnedHTTPSAdapter
############################################################
#
# The HTTPS half of the pin: the request URL carries the
# vetted IP, so without help TLS would send the IP as SNI and
# check the certificate against it. The adapter hands
# urllib3 the real hostname for both (server_hostname for
# SNI, assert_hostname for the certificate match), so a
# pinned connection verifies exactly like a normal one.
# Mounted per hop on a throwaway session — the pool key
# carries the hostname, so no pooled connection is ever
# reused for a different host. Plain HTTP needs no adapter:
# the Host header alone names the site.
#
# Used by:
#   - _fetch_bounded (below) — https hops
############################################################

class _PinnedHTTPSAdapter(HTTPAdapter):

    def __init__(self, hostname, **kwargs):
        self._hostname = hostname
        super().__init__(**kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        pool_kwargs["server_hostname"] = self._hostname
        pool_kwargs["assert_hostname"] = self._hostname
        super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)








############################################################
# _fetch_bounded
############################################################
#
# GET with redirects followed by hand (each hop
# re-validated), the body read as a capped stream. Every hop
# connects to the address _public_address vetted for it —
# the URL is rewritten to that IP, the Host header (and, for
# HTTPS, SNI and the certificate check through
# _PinnedHTTPSAdapter) keep the hostname, and the session
# ignores environment proxies so nothing can resolve the
# name a second time. Redirect targets resolve against the
# hostname URL, never the IP one. Returns (final_url, bytes,
# content_type) or None.
#
# Used by:
#   - unfurl_message / _store_image (below)
############################################################

def _fetch_bounded(url, max_bytes, timeout, accept):
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        address = _public_address(current)
        if address is None:
            logger.info("Link preview refused (not a public address): %s", current)
            return None

        # The pinned request: the vetted IP in the URL, the
        # site's name everywhere the site must see it
        parts = urlsplit(current)
        try:
            host = parts.hostname.encode("idna").decode("ascii")
        except UnicodeError:
            return None
        default_port = 443 if parts.scheme == "https" else 80
        ip_host = f"[{address}]" if address.version == 6 else str(address)
        port_suffix = f":{parts.port}" if parts.port and parts.port != default_port else ""
        target = urlunsplit((parts.scheme, ip_host + port_suffix, parts.path or "/", parts.query, ""))
        session = requests.Session()
        session.trust_env = False
        if parts.scheme == "https":
            session.mount("https://", _PinnedHTTPSAdapter(host))
        try:
            resp = session.get(
                target, timeout=timeout, stream=True, allow_redirects=False,
                headers={"Host": host + port_suffix, "User-Agent": USER_AGENT, "Accept": accept,
                         "Accept-Language": "lt,en;q=0.8"},
            )
        except requests.RequestException as e:
            session.close()
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
            session.close()
    return None








############################################################
# extract_preview
############################################################
#
# The page's Open Graph / Twitter / plain tags, in that
# order of trust, trimmed to card size. `image` is the
# absolute URL of the picture to fetch — not yet ours.
#
# Used by:
#   - unfurl_message (below), tests
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
# _tiny_preview
############################################################
#
# The ~14px micro copy a card blurs while the real picture
# downloads (the same trick the upload route plays for
# photos), as a data URI — or None when the bytes will not
# decode.
#
# Used by:
#   - _store_image (below) — a fresh store and a reuse alike
############################################################

def _tiny_preview(encoded):
    import base64
    import io

    from PIL import Image

    try:
        with Image.open(io.BytesIO(encoded)) as stored:
            tiny = stored.convert("RGB")
            tiny.thumbnail((14, 14))
            tiny_buf = io.BytesIO()
            tiny.save(tiny_buf, format="JPEG", quality=60)
            return "data:image/jpeg;base64," + base64.b64encode(tiny_buf.getvalue()).decode("ascii")
    except Exception:
        return None








############################################################
# _store_image
############################################################
#
# The card's picture as an own upload of the sender: fetched
# under the same gate, bounded, re-encoded through the photo
# pipeline, written atomically, recorded in uploads —
# counted against the sender's storage quota exactly like
# the upload route counts it (an account at the ceiling
# gets the card WITHOUT a picture, never a picture past its
# budget). The stored name is a digest of (sender, source
# image URL) plus the re-encoder's extension — the shape
# storage.FILENAME_RE admits — so the same link posted again
# finds its stored picture and reuses it: no second fetch,
# no second file, no second charge. The reuse keeps working
# for as long as any message shows the file; the unsend /
# expiry cleanup removes it with the last one (the sink's
# reference guard). Returns (relative url, tiny preview) or
# None.
#
# Used by:
#   - unfurl_message (below)
############################################################

def _store_image(image_url, sender_id):
    # STEP 1: the stored copy of this sender's earlier card with
    # the same picture — reused when its row AND file survive
    # ========================================================
    import os

    from django.db.models import Sum

    from knfapp.common.timestamps import utc_now
    from knfapp.uploads.gates import reencode_image
    from knfapp.uploads.models import Upload
    from knfapp.uploads.storage import UPLOAD_QUOTA_BYTES, atomic_write, upload_dir

    digest = hashlib.sha256(f"{sender_id}\n{image_url}".encode("utf-8")).hexdigest()[:32]
    existing = Upload.objects.filter(user_id=sender_id, filename__startswith=f"{digest}.") \
        .values_list("filename", flat=True).first()
    if existing:
        try:
            with open(os.path.join(upload_dir(), existing), "rb") as handle:
                return f"/api/uploads/{existing}", _tiny_preview(handle.read())
        except OSError:
            # The row outlived its file (a crash window): drop the
            # stale row and store afresh below — quota and all
            Upload.objects.filter(filename=existing, user_id=sender_id).delete()


    # STEP 2: fetch and re-encode through the photo pipeline
    # ======================================================
    fetched = _fetch_bounded(image_url, IMAGE_MAX_BYTES, PAGE_TIMEOUT, "image/*")
    if not fetched:
        return None
    _final, blob, content_type = fetched
    if not content_type.startswith("image/") or not blob:
        return None
    ext, encoded, rejection = reencode_image(blob)
    if rejection or not encoded:
        return None


    # STEP 3: the sender's quota, the same sum the upload route
    # checks — over the ceiling the card keeps its text only
    # ======================================================
    used = Upload.objects.filter(user_id=sender_id).aggregate(total=Sum("byte_size"))["total"] or 0
    if used + len(encoded) > UPLOAD_QUOTA_BYTES:
        logger.info("Link preview image skipped: sender %s is at the storage quota", sender_id)
        return None


    # STEP 4: the file, then its ownership row — a racing twin
    # of the same unfurl lands on the SAME name and row
    # ==========================================================
    safe_name = f"{digest}.{ext}"
    if not atomic_write(safe_name, encoded):
        return None
    try:
        Upload.objects.create(
            id=str(uuid.uuid4()), filename=safe_name, user_id=sender_id,
            byte_size=len(encoded), created_at=utc_now(),
        )
    except IntegrityError:
        # The twin recorded it first — same name, same bytes
        pass
    return f"/api/uploads/{safe_name}", _tiny_preview(encoded)








############################################################
# unfurl_message
############################################################
#
# The background task: fetch the page, build the card, store
# the picture, write link_preview on the row (unless the row
# was unsent meanwhile) and tell the room. Closes the
# thread's DB connection on the way out — the daemon thread
# dies right after.
#
# Used by:
#   - api/views.py send_message — via the _spawn seam
############################################################

def unfurl_message(sio, conv_id, msg_id, url, sender_id):
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

        # One conditional UPDATE — the card lands only on a row
        # that still exists and was not unsent meanwhile, with no
        # check-then-write window between the two
        changed = Message.objects.filter(
            id=msg_id, conversation_id=conv_id, deleted_at__isnull=True,
        ).update(link_preview=preview)
        if not changed:
            return

        from knfapp.chat.events import emit_message_updated
        emit_message_updated(sio, conv_id, msg_id, {"linkPreview": preview})
    except Exception:
        logger.exception("Link preview failed for message %s", msg_id)
    finally:
        connection.close()
