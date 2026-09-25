############################################################
#  [*] Regression tests — the socket handlers past the handshake
#
#  The handshake pair is pinned in test_chat_read_state.py
#  and test_socket_teardown.py; these take the five events
#  that follow it — join_conversation, leave_conversation,
#  typing, stop_typing, mark_read — and the read-receipt
#  targeting underneath mark_read, all through the REAL
#  handlers bound to a recording sio stub. Before this file
#  no test ever fetched those handlers: a membership gate
#  answering True for everyone, or receipt targeting that
#  always fell back to the room, left the suite green.
#
#  The last block pins the connection hygiene on the way
#  out of a handler: recycled on an idle packet thread, left
#  alone inside an atomic block (the harness, any in-request
#  caller) — the unguarded close is what made the PostgreSQL
#  pass answer "the connection is closed" from eight tests
#  the SQLite pass never noticed.
############################################################


import json
from types import SimpleNamespace
from unittest.mock import patch


from django.db import connection
from django.test import TestCase


from knfapp.chat import events
from knfapp.chat.models import MessageRead
from knfapp.common import ratelimit
from knfapp.users import auth
from .utils import bearer, create_message, create_room, create_user


class _Sio:
    # test_socket_teardown.py's harness, grown for the room and
    # fan-out events: the handlers by name, every room join and
    # leave with its sid, every emit with its addressing (the
    # room or sid in `to`, the actor left out via skip_sid),
    # the sids it was asked to cut
    def __init__(self):
        self.handlers = {}
        self.joined = []
        self.left = []
        self.emits = []
        self.cut = []

    def on(self, event, handler=None):
        self.handlers[event] = handler

    def enter_room(self, sid, room_name):
        self.joined.append((sid, room_name))

    def leave_room(self, sid, room_name):
        self.left.append((sid, room_name))

    def emit(self, event, payload=None, to=None, skip_sid=None, **kwargs):
        self.emits.append((event, payload, to, skip_sid))

    def disconnect(self, sid):
        self.cut.append(sid)

    def sent(self, event):
        # The emits of one event, in order
        return [e for e in self.emits if e[0] == event]


class HandlerTestCase(TestCase):

    # Presence as the handshake would have filed it: tomas on
    # two devices, ona on one, an outsider who shares no room —
    # planted directly, the handshake has its own tests
    def setUp(self):
        ratelimit.reset()
        events.reset_socket_state()
        self.addCleanup(events.reset_socket_state)

        self.tomas = create_user(username="tomas")
        self.ona = create_user(username="ona")
        self.outsider = create_user(username="pasalinis")
        self.room = create_room([self.tomas, self.ona])
        self.conv = f"conv:{self.room.id}"

        self.sio = _Sio()
        events.register_socket_events(self.sio)
        self._plant("sid-tomas", self.tomas)
        self._plant("sid-tomas-tab", self.tomas)
        self._plant("sid-ona", self.ona)
        self._plant("sid-pasalinis", self.outsider)

    def _plant(self, sid, user):
        events._connected_users[sid] = user.id
        events._connected_names[sid] = user.display_name

    def _fire(self, event, sid, data=None):
        # What python-socketio does with a client packet: the
        # bound (guarded) handler, sid first, then the payload
        return self.sio.handlers[event](sid, data)








############################################################
# join_conversation / leave_conversation
############################################################

class RoomTests(HandlerTestCase):

    def test_a_member_enters_the_room(self):
        self._fire("join_conversation", "sid-tomas", {"conversationId": self.room.id})
        self.assertEqual(self.sio.joined, [("sid-tomas", self.conv)])
        self.assertEqual(self.sio.emits, [])

    def test_a_non_member_is_dropped_without_a_word(self):
        # The refusal is silence — no room, no ack, no error
        # event: knowing a conversation id must not be enough to
        # read its live traffic
        self._fire("join_conversation", "sid-pasalinis", {"conversationId": self.room.id})
        self.assertEqual(self.sio.joined, [])
        self.assertEqual(self.sio.emits, [])

    def test_the_membership_gate_is_what_keeps_the_outsider_out(self):
        # The mutation the audit ran — _is_member answering True
        # for everyone — walks the outsider straight in, so the
        # case above stands or falls with the gate
        with patch.object(events, "_is_member", return_value=True):
            self._fire("join_conversation", "sid-pasalinis", {"conversationId": self.room.id})
        self.assertEqual(self.sio.joined, [("sid-pasalinis", self.conv)])

    def test_leave_leaves_the_room_membership_unchecked(self):
        self._fire("leave_conversation", "sid-tomas", {"conversationId": self.room.id})
        # Leaving a room the socket was never in is socketio's
        # own no-op, so the outsider's leave goes through as well
        self._fire("leave_conversation", "sid-pasalinis", {"conversationId": self.room.id})
        self.assertEqual(self.sio.left, [("sid-tomas", self.conv), ("sid-pasalinis", self.conv)])
        self.assertEqual(self.sio.emits, [])

    def test_an_unknown_sid_and_a_junk_payload_do_nothing(self):
        for data in (None, [], "x", {}, {"conversationId": ""}, {"conversationId": 7}):
            self._fire("join_conversation", "sid-tomas", data)
            self._fire("leave_conversation", "sid-tomas", data)
        # A sid the handshake never recorded is not a user
        self._fire("join_conversation", "sid-niekas", {"conversationId": self.room.id})
        self._fire("leave_conversation", "sid-niekas", {"conversationId": self.room.id})
        self.assertEqual((self.sio.joined, self.sio.left, self.sio.emits), ([], [], []))

    def test_the_join_budget_is_ten_per_window(self):
        for _ in range(10):
            self._fire("join_conversation", "sid-tomas", {"conversationId": self.room.id})
        self._fire("join_conversation", "sid-tomas", {"conversationId": self.room.id})
        self.assertEqual(len(self.sio.joined), 10)

    def test_the_guard_turns_a_crash_into_an_error_event(self):
        # An UNEXPECTED failure inside a handler must neither
        # escape to the packet thread nor pass in silence
        with patch.object(events, "_is_member", side_effect=RuntimeError("boom")), \
                self.assertLogs(events.logger, level="ERROR") as logged:
            self._fire("join_conversation", "sid-tomas", {"conversationId": self.room.id})
        self.assertEqual(self.sio.emits, [("error", {"message": "Internal error"}, "sid-tomas", None)])
        self.assertEqual(self.sio.joined, [])
        # ...and the log names the event and the sid
        self.assertIn("event=join_conversation sid=sid-tomas", logged.output[0])








############################################################
# typing / stop_typing
############################################################

class TypingTests(HandlerTestCase):

    def test_typing_reaches_the_room_and_skips_the_typist(self):
        self._fire("typing", "sid-tomas", {"conversationId": self.room.id})
        self.assertEqual(self.sio.emits, [(
            "user_typing",
            {"conversationId": self.room.id, "userId": self.tomas.id, "displayName": "Tomas"},
            self.conv,
            "sid-tomas",
        )])

    def test_stop_typing_likewise_without_the_name(self):
        self._fire("stop_typing", "sid-tomas", {"conversationId": self.room.id})
        self.assertEqual(self.sio.emits, [(
            "user_stop_typing",
            {"conversationId": self.room.id, "userId": self.tomas.id},
            self.conv,
            "sid-tomas",
        )])

    def test_a_non_member_plants_no_indicator(self):
        self._fire("typing", "sid-pasalinis", {"conversationId": self.room.id})
        self._fire("stop_typing", "sid-pasalinis", {"conversationId": self.room.id})
        self.assertEqual(self.sio.emits, [])
        # ...and the gate is what keeps them out
        with patch.object(events, "_is_member", return_value=True):
            self._fire("typing", "sid-pasalinis", {"conversationId": self.room.id})
        self.assertEqual([e[0] for e in self.sio.emits], ["user_typing"])

    def test_the_name_is_the_handshakes_not_the_tables(self):
        # Cached at the handshake: a rename mid-session shows the
        # old name until that socket reconnects
        events._connected_names["sid-tomas"] = "Senas vardas"
        self._fire("typing", "sid-tomas", {"conversationId": self.room.id})
        self.assertEqual(self.sio.emits[0][1]["displayName"], "Senas vardas")

    def test_the_window_is_twenty_per_user_per_event(self):
        for _ in range(20):
            self._fire("typing", "sid-tomas", {"conversationId": self.room.id})
        self._fire("typing", "sid-tomas", {"conversationId": self.room.id})
        self.assertEqual(len(self.sio.emits), 20)
        # stop_typing keeps a budget of its own
        self._fire("stop_typing", "sid-tomas", {"conversationId": self.room.id})
        self.assertEqual(len(self.sio.emits), 21)








############################################################
# mark_read — the socket twin of PUT …/read
############################################################

class MarkReadTests(HandlerTestCase):

    def test_receipts_land_and_the_event_targets_sender_and_reader(self):
        first = create_message(self.room, self.ona, minutes_ago=2)
        second = create_message(self.room, self.ona, minutes_ago=1)

        self._fire("mark_read", "sid-tomas", {"conversationId": self.room.id})

        self.assertEqual(MessageRead.objects.filter(user_id=self.tomas.id).count(), 2)
        sent = self.sio.sent("messages_read")
        # ona (the sender) and BOTH of tomas's devices — never
        # the outsider, never the room
        self.assertEqual(sorted(e[2] for e in sent), ["sid-ona", "sid-tomas", "sid-tomas-tab"])
        payload = {"conversationId": self.room.id, "readerId": self.tomas.id,
                   "messageIds": [second.id, first.id]}
        self.assertTrue(all(e[1] == payload for e in sent))

        # Nothing new the second time — no re-broadcast
        self._fire("mark_read", "sid-tomas", {"conversationId": self.room.id})
        self.assertEqual(len(self.sio.sent("messages_read")), 3)

    def test_a_non_member_is_dropped_where_rest_answers_403(self):
        create_message(self.room, self.ona)
        self._fire("mark_read", "sid-pasalinis", {"conversationId": self.room.id})
        self.assertEqual(MessageRead.objects.filter(user_id=self.outsider.id).count(), 0)
        self.assertEqual(self.sio.emits, [])

    def test_the_socket_spends_the_budget_rest_shares(self):
        # 10 per 10 s, ONE budget for both transports: ten socket
        # events fill it and the REST twin's first call is 429
        for _ in range(10):
            self._fire("mark_read", "sid-tomas", {"conversationId": self.room.id})
        token = auth.mint_session(self.tomas.id)
        response = bearer(self.client.put, f"/api/chat/conversations/{self.room.id}/read", token)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(json.loads(response.content)["code"], "rate_limited")

    def test_rest_spends_the_budget_the_socket_reads(self):
        # ...and the other way round: after ten REST calls a
        # socket event with a genuinely unread message is dropped
        # — no receipt, no fan-out
        token = auth.mint_session(self.tomas.id)
        for _ in range(10):
            self.assertEqual(bearer(self.client.put, f"/api/chat/conversations/{self.room.id}/read",
                                    token).status_code, 200)
        create_message(self.room, self.ona)
        self._fire("mark_read", "sid-tomas", {"conversationId": self.room.id})
        self.assertEqual(MessageRead.objects.filter(user_id=self.tomas.id).count(), 0)
        self.assertEqual(self.sio.emits, [])








############################################################
# _read_receipt_sids / emit_read_receipt
############################################################

def _explode(**kwargs):
    raise RuntimeError("boom")


# A Message stand-in whose manager cannot query — the DB-error
# arm of _read_receipt_sids without touching the real manager
_BrokenMessage = SimpleNamespace(objects=SimpleNamespace(filter=_explode))


class ReadReceiptTargetingTests(TestCase):

    # A four-way room: tomas reads, ona and rasa wrote, jonas is
    # a member who wrote nothing in the batch; presence holds
    # tomas twice, the others once, plus an outsider
    def setUp(self):
        events.reset_socket_state()
        self.addCleanup(events.reset_socket_state)
        self.tomas = create_user(username="tomas")
        self.ona = create_user(username="ona")
        self.rasa = create_user(username="rasa")
        self.jonas = create_user(username="jonas")
        outsider = create_user(username="pasalinis")
        self.room = create_room([self.tomas, self.ona, self.rasa, self.jonas],
                                conv_type="group", title="Grupė")
        self.from_ona = create_message(self.room, self.ona, minutes_ago=3)
        self.from_rasa = create_message(self.room, self.rasa, minutes_ago=2)
        for sid, user in (("sid-tomas", self.tomas), ("sid-tomas-tab", self.tomas),
                          ("sid-ona", self.ona), ("sid-rasa", self.rasa),
                          ("sid-jonas", self.jonas), ("sid-pasalinis", outsider)):
            events._connected_users[sid] = user.id
        self.sio = _Sio()

    def test_the_senders_and_every_device_of_the_reader(self):
        sids = events._read_receipt_sids(self.tomas.id, [self.from_ona.id, self.from_rasa.id])
        self.assertEqual(sorted(sids), ["sid-ona", "sid-rasa", "sid-tomas", "sid-tomas-tab"])
        # A member who sent nothing in the batch is not woken —
        # the whole point of targeting over the room
        sids = events._read_receipt_sids(self.tomas.id, [self.from_ona.id])
        self.assertEqual(sorted(sids), ["sid-ona", "sid-tomas", "sid-tomas-tab"])

    def test_a_sender_without_a_socket_is_simply_absent(self):
        events._connected_users.pop("sid-ona")
        sids = events._read_receipt_sids(self.tomas.id, [self.from_ona.id])
        self.assertEqual(sorted(sids), ["sid-tomas", "sid-tomas-tab"])

    def test_the_three_fallback_conditions_answer_none(self):
        # An empty batch
        self.assertIsNone(events._read_receipt_sids(self.tomas.id, []))
        # Past 900 ids (one IN list under SQLite's variable cap):
        # 900 still targets — unknown ids resolve to nobody but
        # the reader — and 901 is the room
        self.assertEqual(sorted(events._read_receipt_sids(self.tomas.id, ["x"] * 900)),
                         ["sid-tomas", "sid-tomas-tab"])
        self.assertIsNone(events._read_receipt_sids(self.tomas.id, ["x"] * 901))
        # A database error on the sender lookup — logged, never raised
        with patch.object(events, "Message", _BrokenMessage), \
                self.assertLogs(events.logger, level="ERROR"):
            self.assertIsNone(events._read_receipt_sids(self.tomas.id, [self.from_ona.id]))

    def test_emit_targets_the_sids_and_only_falls_back_to_the_room(self):
        ids = [self.from_ona.id]
        payload = {"conversationId": self.room.id, "readerId": self.tomas.id, "messageIds": ids}
        events.emit_read_receipt(self.sio, self.room.id, self.tomas.id, ids)
        # Each sid is its own room in Socket.IO — one emit per
        # target, none to conv:<id>
        self.assertEqual(sorted(e[2] for e in self.sio.emits), ["sid-ona", "sid-tomas", "sid-tomas-tab"])
        self.assertTrue(all(e[:2] == ("messages_read", payload) for e in self.sio.emits))

        # The audit's other mutation — targeting that always
        # answers None — would send every receipt to the room;
        # only a documented fallback may
        self.sio.emits.clear()
        with patch.object(events, "Message", _BrokenMessage), \
                self.assertLogs(events.logger, level="ERROR"):
            events.emit_read_receipt(self.sio, self.room.id, self.tomas.id, ids)
        self.assertEqual(self.sio.emits, [("messages_read", payload, f"conv:{self.room.id}", None)])








############################################################
# The connection on the way out of a handler
############################################################

class ConnectionHygieneTests(HandlerTestCase):

    def _record_recycling(self):
        # The in-memory SQLite connection ignores close(), so the
        # proof is the CALL itself — a recorder stands in for
        # close_old_connections (test_push_fanout.py does the
        # same for the push helper)
        calls = []
        real = events.close_old_connections
        events.close_old_connections = lambda *args, **kwargs: calls.append(args)
        self.addCleanup(lambda: setattr(events, "close_old_connections", real))
        return calls

    def _walk_the_three_sites(self):
        # The handshake's finally arm, a guarded handler, the
        # disconnect edge
        token = auth.mint_session(self.ona.id)
        self.sio.handlers["connect"]("sid-ona-2", {}, {"token": token})
        self._fire("join_conversation", "sid-tomas", {"conversationId": self.room.id})
        self._fire("disconnect", "sid-tomas-tab")

    def test_inside_a_transaction_the_connection_is_left_alone(self):
        calls = self._record_recycling()
        # A TestCase runs inside an atomic block — the footing of
        # any in-request caller, and where the unguarded close
        # was what the PostgreSQL pass tripped over
        self.assertTrue(connection.in_atomic_block)
        self._walk_the_three_sites()
        self.assertEqual(calls, [])
        # ...and the transaction is still alive to prove it
        self.assertEqual(MessageRead.objects.count(), 0)

    def test_on_an_idle_packet_thread_the_connection_is_recycled(self):
        calls = self._record_recycling()
        # The production footing: autocommit, no transaction
        with patch.object(events, "connection", SimpleNamespace(in_atomic_block=False)):
            self._walk_the_three_sites()
        self.assertEqual(len(calls), 3)
