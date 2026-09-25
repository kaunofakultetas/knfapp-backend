############################################################
#  [*] Regression tests — chat link previews
#
#  The unfurler's two storage and network promises. The SSRF
#  gate and the connection must agree: a hop connects to the
#  very address the gate vetted (URL rewritten to the IP, the
#  hostname kept in Host / SNI / the certificate check), so a
#  rebinding DNS answer can never slip loopback in between —
#  and a private answer, first hop or redirect, is refused.
#  The card's picture is an upload of the sender's: counted
#  against their quota (at the ceiling the card keeps its
#  text only) and stored once per (sender, source image), so
#  the same link posted again reuses the stored file.
############################################################


import io
import os
import shutil
import socket
import tempfile
from unittest.mock import patch


import requests
from PIL import Image
from django.test import TestCase
from requests.structures import CaseInsensitiveDict


from knfapp.chat import linkpreview
from knfapp.chat.models import Message
from knfapp.uploads import storage
from knfapp.uploads.models import Upload
from knfapp.common.timestamps import utc_now_iso
from .utils import create_message, create_room, create_user


PUBLIC_IP = "93.184.216.34"


def _answers(*ips):
    # getaddrinfo's shape: (family, type, proto, canonname, sockaddr)
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 80)) for ip in ips]


def _response(status=200, content_type="text/html", body=b"", location=None):
    resp = requests.Response()
    resp.status_code = status
    headers = {"Content-Type": content_type}
    if location:
        headers["Location"] = location
    resp.headers = CaseInsensitiveDict(headers)
    resp.raw = io.BytesIO(body)
    return resp


def _png():
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (123, 0, 63)).save(buf, format="PNG")
    return buf.getvalue()








############################################################
# PinnedFetchTests
############################################################
#
# KNF-059: one resolution per hop, and the connection goes
# to exactly that answer.
############################################################

class PinnedFetchTests(TestCase):

    def _fetch(self, url, resolver, responses):
        sent = []

        def fake_send(adapter, request, **kwargs):
            sent.append((adapter, request))
            return responses.pop(0)

        with patch.object(linkpreview.socket, "getaddrinfo", side_effect=resolver) as lookups, \
                patch.object(requests.adapters.HTTPAdapter, "send", autospec=True, side_effect=fake_send):
            result = linkpreview._fetch_bounded(url, 1024, (1, 1), "text/html")
        return result, sent, lookups

    def test_a_rebinding_answer_never_reaches_the_connection(self):
        # The resolver's second answer is loopback — the fetch
        # must never ask it a second time for the same hop
        answers = [_answers(PUBLIC_IP), _answers("127.0.0.1")]
        result, sent, lookups = self._fetch(
            "https://rebind.attacker.test/page?x=1",
            lambda *args, **kwargs: answers.pop(0),
            [_response(body=b"<html></html>")],
        )
        self.assertEqual(lookups.call_count, 1)
        adapter, request = sent[0]
        self.assertTrue(request.url.startswith(f"https://{PUBLIC_IP}/page?x=1"))
        self.assertEqual(request.headers["Host"], "rebind.attacker.test")
        # TLS keeps the hostname for SNI and the certificate check
        self.assertIsInstance(adapter, linkpreview._PinnedHTTPSAdapter)
        self.assertEqual(adapter.poolmanager.connection_pool_kw["server_hostname"], "rebind.attacker.test")
        self.assertEqual(adapter.poolmanager.connection_pool_kw["assert_hostname"], "rebind.attacker.test")
        # The answer names the site by its hostname URL, never the IP
        self.assertEqual(result[0], "https://rebind.attacker.test/page?x=1")

    def test_a_private_answer_is_refused_before_any_connection(self):
        for private in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "192.168.1.1"):
            result, sent, _lookups = self._fetch(
                "http://internal.test/", lambda *args, **kwargs: _answers(private), [],
            )
            self.assertIsNone(result, private)
            self.assertEqual(sent, [], private)

    def test_a_redirect_into_a_private_address_is_refused(self):
        answers = [_answers(PUBLIC_IP), _answers("127.0.0.1")]
        result, sent, _lookups = self._fetch(
            "http://public.test/",
            lambda *args, **kwargs: answers.pop(0),
            [_response(status=302, location="http://admin.internal/")],
        )
        self.assertIsNone(result)
        self.assertEqual(len(sent), 1)

    def test_a_plain_http_hop_is_pinned_by_url_and_host_header(self):
        result, sent, _lookups = self._fetch(
            "http://example.test:80/a", lambda *args, **kwargs: _answers(PUBLIC_IP),
            [_response(body=b"ok")],
        )
        _adapter, request = sent[0]
        self.assertEqual(request.url, f"http://{PUBLIC_IP}/a")
        self.assertEqual(request.headers["Host"], "example.test")
        self.assertEqual(result[1], b"ok")

    def test_ports_and_schemes_outside_the_rule_are_refused(self):
        for url in ("http://example.test:8080/", "ftp://example.test/", "file:///etc/passwd"):
            result, sent, _lookups = self._fetch(url, lambda *args, **kwargs: _answers(PUBLIC_IP), [])
            self.assertIsNone(result, url)
            self.assertEqual(sent, [], url)








############################################################
# StoredImageTests
############################################################
#
# KNF-058: the card's picture obeys the sender's quota and is
# stored once per (sender, source image).
############################################################

class StoredImageTests(TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="knfapp-unfurl-")
        storage._upload_dir = self.tmp
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.addCleanup(lambda: setattr(storage, "_upload_dir", None))
        self.tomas = create_user(username="tomas")
        self.ona = create_user(username="ona")

    def _store(self, sender, image_url="https://example.test/og.png"):
        fetches = []

        def fake_fetch(url, max_bytes, timeout, accept):
            fetches.append(url)
            return url, _png(), "image/png"

        with patch.object(linkpreview, "_fetch_bounded", side_effect=fake_fetch):
            stored = linkpreview._store_image(image_url, sender.id)
        return stored, fetches

    def test_an_account_at_its_quota_gets_the_card_without_a_picture(self):
        Upload.objects.create(id="quota-row", filename="f" * 32 + ".jpg", user_id=self.tomas.id,
                              byte_size=storage.UPLOAD_QUOTA_BYTES, created_at=utc_now_iso())
        stored, _fetches = self._store(self.tomas)
        self.assertIsNone(stored)
        self.assertEqual(Upload.objects.filter(user_id=self.tomas.id).count(), 1)
        self.assertEqual(os.listdir(self.tmp), [])

    def test_the_same_picture_is_stored_once_and_reused(self):
        first, fetches_first = self._store(self.tomas)
        second, fetches_second = self._store(self.tomas)
        self.assertEqual(first[0], second[0])
        self.assertTrue(first[1].startswith("data:image/jpeg;base64,"))
        self.assertEqual(second[1], first[1])
        self.assertEqual(len(fetches_first), 1)
        self.assertEqual(fetches_second, [])
        self.assertEqual(Upload.objects.filter(user_id=self.tomas.id).count(), 1)
        self.assertEqual(len(os.listdir(self.tmp)), 1)
        # The stored name is one the uploads gate admits
        self.assertIsNotNone(storage.safe_upload_name(first[0]))

    def test_each_sender_owns_their_own_copy(self):
        tomas_copy, _ = self._store(self.tomas)
        ona_copy, _ = self._store(self.ona)
        self.assertNotEqual(tomas_copy[0], ona_copy[0])
        self.assertTrue(Upload.objects.filter(user_id=self.ona.id, filename=ona_copy[0].rsplit("/", 1)[-1]).exists())

    def test_a_row_that_outlived_its_file_is_replaced(self):
        first, _ = self._store(self.tomas)
        os.unlink(os.path.join(self.tmp, first[0].rsplit("/", 1)[-1]))
        again, fetches = self._store(self.tomas)
        self.assertEqual(len(fetches), 1)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, again[0].rsplit("/", 1)[-1])))
        self.assertEqual(Upload.objects.filter(user_id=self.tomas.id).count(), 1)

    def test_two_messages_with_one_link_share_one_file_until_the_last_unsend(self):
        room = create_room([self.tomas, self.ona])
        first = create_message(room, self.tomas, text="https://example.test")
        second = create_message(room, self.tomas, text="vėl https://example.test")
        page = b'<html><head><meta property="og:title" content="Pavyzdys">' \
               b'<meta property="og:image" content="/og.png"></head></html>'

        def fake_fetch(url, max_bytes, timeout, accept):
            if url.endswith("/og.png"):
                return url, _png(), "image/png"
            return url, page, "text/html"

        class _Sio:
            def emit(self, *args, **kwargs):
                pass

        with patch.object(linkpreview, "_fetch_bounded", side_effect=fake_fetch), \
                patch.object(linkpreview.connection, "close"):
            linkpreview.unfurl_message(_Sio(), room.id, first.id, "https://example.test", self.tomas.id)
            linkpreview.unfurl_message(_Sio(), room.id, second.id, "https://example.test", self.tomas.id)
        cards = [Message.objects.get(id=m.id).link_preview for m in (first, second)]
        self.assertEqual(cards[0]["imageUrl"], cards[1]["imageUrl"])
        self.assertEqual(cards[0]["title"], "Pavyzdys")
        self.assertEqual(len(os.listdir(self.tmp)), 1)
