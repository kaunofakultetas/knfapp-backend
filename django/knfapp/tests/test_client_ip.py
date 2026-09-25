############################################################
#  [*] Regression tests — client_ip counts trusted hops
#
#  The rate-limit identity behind two proxies. The host's
#  TLS terminator stamps the client, this stack's ingress
#  appends its own peer (the outer proxy), so Django reads
#  "<client>, <outer proxy>": the TRUSTED_PROXY_HOPS (1)
#  rightmost entries are infrastructure and the client is
#  the entry just left of them. A chain no longer than the
#  trusted tail did not come through the outer proxy and
#  its first entry is the peer; no header at all is
#  REMOTE_ADDR. The last case is end-to-end: two forged
#  clients behind the same proxy hop must land in
#  DIFFERENT validate-code buckets — under the old
#  last-hop rule they shared one, because the last hop was
#  the proxy every request has in common.
############################################################


import json


from django.test import Client, RequestFactory, TestCase, override_settings


from knfapp.common import ratelimit
from knfapp.common.http import client_ip


def _request(forwarded=None, remote="10.0.0.5"):
    meta = {"REMOTE_ADDR": remote}
    if forwarded is not None:
        meta["HTTP_X_FORWARDED_FOR"] = forwarded
    return RequestFactory().get("/api/health", **meta)


@override_settings(TRUSTED_PROXY_HOPS=1)
class ClientIpTests(TestCase):

    def test_the_entry_left_of_the_trusted_hop_is_the_client(self):
        self.assertEqual(client_ip(_request("203.0.113.9, 127.0.0.1")), "203.0.113.9")

    def test_a_chain_no_longer_than_the_trusted_tail_yields_its_first_entry(self):
        # A direct hit on the published port: the ingress stamped
        # the one entry there is, and it IS the peer
        self.assertEqual(client_ip(_request("172.18.0.1")), "172.18.0.1")

    def test_no_header_falls_back_to_remote_addr(self):
        self.assertEqual(client_ip(_request(remote="10.0.0.5")), "10.0.0.5")
        self.assertEqual(client_ip(_request(remote="")), "unknown")

    @override_settings(TRUSTED_PROXY_HOPS=2)
    def test_two_trusted_hops_count_one_further_left(self):
        self.assertEqual(client_ip(_request("198.51.100.7, 203.0.113.9, 127.0.0.1")), "198.51.100.7")

    def test_whitespace_and_empty_segments_are_ignored(self):
        self.assertEqual(client_ip(_request(" 203.0.113.9 , , 127.0.0.1 , ")), "203.0.113.9")
        self.assertEqual(client_ip(_request(" , ")), "10.0.0.5")


@override_settings(TRUSTED_PROXY_HOPS=1)
class ForwardedClientsBucketSeparatelyTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.client = Client()

    def _validate(self, forged_client):
        return self.client.post("/api/auth/validate-code", data=json.dumps({"code": "NERA"}),
                                content_type="application/json",
                                HTTP_X_FORWARDED_FOR=f"{forged_client}, 127.0.0.1")

    def test_two_clients_behind_one_proxy_hop_spend_separate_budgets(self):
        # STEP 1: fill the first client's bucket — bounded, so a
        # changed budget cannot turn this into an endless loop
        # =====================================================
        for _ in range(200):
            if self._validate("203.0.113.9").status_code == 429:
                break
        else:
            self.fail("the validate-code bucket never filled")
        self.assertEqual(self._validate("203.0.113.9").status_code, 429)


        # STEP 2: the second client, same proxy hop — a fresh
        # bucket, not the first client's 429
        # ===================================================
        self.assertEqual(self._validate("203.0.113.10").status_code, 200)
