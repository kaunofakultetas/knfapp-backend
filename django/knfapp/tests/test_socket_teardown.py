############################################################
#  [*] Regression tests — socket teardown is per SESSION
#
#  A chat socket authenticates once, at the handshake, and
#  the revocation routes cut it afterwards. These pin the
#  SCOPE of that cut: the handshake files the session hash
#  beside the sid, a single-device logout cuts only the
#  presented session's sockets, a password change cuts every
#  session's but the caller's, logout-all cuts the account,
#  and another user's socket is never touched. Before the
#  hash was filed every route cut every socket of the
#  account, so one phone signing out silenced the tablet's
#  chat until it was backgrounded or the network blinked.
############################################################


from unittest.mock import patch


from django.test import TestCase


from knfapp.chat import events
from knfapp.chat import socket as socket_layer
from knfapp.common import ratelimit
from knfapp.users import auth
from .utils import PASSWORD, bearer, create_user


class _Sio:
    # The handshake harness: records the handlers, swallows the
    # room joins and emits, records the sids it was asked to cut
    def __init__(self):
        self.handlers = {}
        self.cut = []

    def on(self, event, handler=None):
        self.handlers[event] = handler

    def enter_room(self, sid, room_name):
        pass

    def emit(self, event, payload=None, to=None, **kwargs):
        pass

    def disconnect(self, sid):
        self.cut.append(sid)








############################################################
# The handshake files the session beside the sid
############################################################

class HandshakeSessionTests(TestCase):

    def setUp(self):
        events.reset_socket_state()
        self.addCleanup(events.reset_socket_state)
        self.user = create_user(username="tomas")
        self.token = auth.mint_session(self.user.id)

    def test_authenticate_socket_answers_the_user_and_the_session_hash(self):
        resolved = events._authenticate_socket({}, {"token": self.token})
        self.assertIsNotNone(resolved)
        user, session_hash = resolved
        self.assertEqual(user["id"], self.user.id)
        self.assertNotIn("password_hash", user)
        # The digest the sessions row stores — never the raw token
        self.assertEqual(session_hash, auth.hash_token(self.token))
        self.assertNotEqual(session_hash, self.token)

    def test_the_query_string_fallback_answers_the_same_pair(self):
        by_auth = events._authenticate_socket({}, {"token": self.token})
        by_query = events._authenticate_socket({"QUERY_STRING": f"token={self.token}"}, None)
        self.assertEqual(by_query, by_auth)

    def test_a_bad_token_is_none_not_a_pair(self):
        self.assertIsNone(events._authenticate_socket({}, {"token": "netikras"}))
        self.assertIsNone(events._authenticate_socket({}, {}))

    def test_connect_records_the_session_hash_and_disconnect_forgets_it(self):
        sio = _Sio()
        events.register_socket_events(sio)

        accepted = sio.handlers["connect"]("sid-1", {}, {"token": self.token})
        self.assertNotEqual(accepted, False)
        self.assertEqual(events._connected_users.get("sid-1"), self.user.id)
        self.assertEqual(events._connected_sessions.get("sid-1"), auth.hash_token(self.token))

        sio.handlers["disconnect"]("sid-1")
        self.assertNotIn("sid-1", events._connected_users)
        self.assertNotIn("sid-1", events._connected_sessions)

    def test_an_evicted_socket_loses_its_session_row_too(self):
        sio = _Sio()
        events.register_socket_events(sio)
        for i in range(events._MAX_SOCKETS_PER_USER):
            events._connected_users[f"sid-{i}"] = self.user.id
            events._connected_names[f"sid-{i}"] = "Tomas"
            events._connected_sessions[f"sid-{i}"] = "senas-hash"

        sio.handlers["connect"]("sid-new", {}, {"token": self.token})

        self.assertEqual(sio.cut, ["sid-0"])
        self.assertNotIn("sid-0", events._connected_sessions)
        self.assertEqual(events._connected_sessions.get("sid-new"), auth.hash_token(self.token))








############################################################
# The revocation routes, through the real URLconf
############################################################

class SocketTeardownScopeTests(TestCase):

    # Presence as handle_connect would have filed it: the user's
    # phone (two sids on one session — a zombie beside the live
    # one), the same user's tablet on a second session, and a
    # control sid of another user
    def setUp(self):
        ratelimit.reset()
        events.reset_socket_state()
        self.addCleanup(events.reset_socket_state)

        self.user = create_user(username="tomas")
        self.stranger = create_user(username="rasa")
        self.phone = auth.mint_session(self.user.id)
        self.tablet = auth.mint_session(self.user.id)
        self.rasa = auth.mint_session(self.stranger.id)

        self._plant("sid-phone", self.user, self.phone)
        self._plant("sid-phone-zombie", self.user, self.phone)
        self._plant("sid-tablet", self.user, self.tablet)
        self._plant("sid-rasa", self.stranger, self.rasa)

        self.cut = []
        patcher = patch.object(socket_layer.sio, "disconnect", side_effect=self.cut.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _plant(self, sid, user, token):
        events._connected_users[sid] = user.id
        events._connected_names[sid] = user.display_name
        events._connected_sessions[sid] = auth.hash_token(token)

    def _post(self, path, token, **body):
        return bearer(self.client.post, path, token, data=body or None, content_type="application/json")


    # ---- logout ----------------------------------------

    def test_logout_cuts_only_the_presented_sessions_sockets(self):
        response = self._post("/api/auth/logout", self.phone)
        self.assertEqual(response.status_code, 200)

        self.assertEqual(sorted(self.cut), ["sid-phone", "sid-phone-zombie"])
        self.assertEqual(set(events._connected_users), {"sid-tablet", "sid-rasa"})
        # The tablet's session is as alive as its socket
        self.assertEqual(bearer(self.client.get, "/api/auth/me", self.tablet).status_code, 200)


    # ---- change-password -------------------------------

    def test_change_password_cuts_the_other_sessions_and_keeps_the_callers(self):
        response = self._post("/api/auth/change-password", self.phone,
                              old_password=PASSWORD, new_password="nauja-slapta-2026")
        self.assertEqual(response.status_code, 200)

        self.assertEqual(self.cut, ["sid-tablet"])
        self.assertEqual(set(events._connected_users), {"sid-phone", "sid-phone-zombie", "sid-rasa"})
        # The rotating device keeps its session — and its socket
        self.assertEqual(bearer(self.client.get, "/api/auth/me", self.phone).status_code, 200)
        self.assertEqual(bearer(self.client.get, "/api/auth/me", self.tablet).status_code, 401)


    # ---- logout-all ------------------------------------

    def test_logout_all_cuts_every_socket_of_the_account_and_nobody_elses(self):
        response = self._post("/api/auth/logout-all", self.phone)
        self.assertEqual(response.status_code, 200)

        self.assertEqual(sorted(self.cut), ["sid-phone", "sid-phone-zombie", "sid-tablet"])
        self.assertEqual(set(events._connected_users), {"sid-rasa"})


    # ---- the helper's scopes, directly -----------------

    def test_an_unrecorded_sid_survives_the_single_device_cut_and_falls_to_the_revocation(self):
        # A sid with no filed session proves nothing: only_session
        # cuts what is KNOWN to be that session, except_session
        # keeps what is KNOWN to be the caller's
        events._connected_users["sid-ghost"] = self.user.id
        events._connected_names["sid-ghost"] = "Tomas"

        closed = events.disconnect_user_sockets(self.user.id, only_session=auth.hash_token(self.phone))
        self.assertEqual(closed, 2)
        self.assertIn("sid-ghost", events._connected_users)

        closed = events.disconnect_user_sockets(self.user.id, except_session=auth.hash_token(self.tablet))
        self.assertEqual(closed, 1)
        self.assertEqual(self.cut[-1], "sid-ghost")
        self.assertEqual(set(events._connected_users), {"sid-tablet", "sid-rasa"})
