############################################################
#  [*] Regression tests — the push receipt stage
#
#  Stage two of Expo delivery, with the transport patched
#  out (nothing here touches the network) and the store
#  pointed at a throwaway file per test:
#
#    - KNF-074: tickets a process queues live in the shared
#      store, so a cron command that fans out and exits
#      leaves them for the next pass — the server's watcher
#      or `manage.py poll_push_receipts` — instead of taking
#      them to the grave 899 s before the first poll;
#    - KNF-073: an id Expo has no verdict for yet, and every
#      id of a slice whose request failed, goes back for
#      another round (three at most) instead of being thrown
#      away; a DeviceNotRegistered receipt retires the token;
#    - the store degrades, never fails: a corrupt file reads
#      as empty, an unusable path falls back to memory, and
#      a send never breaks over its receipts.
############################################################


import json
import os
import shutil
import tempfile
import uuid
from io import StringIO
from unittest import mock


from django.core.management import call_command
from django.test import TestCase, override_settings


from knfapp.notifications import push
from knfapp.notifications.models import PushToken
from .utils import create_user


def _token(user, name):
    now = "2026-01-01T00:00:00"
    return PushToken.objects.create(id=str(uuid.uuid4()), user_id=user.id,
                                    token=f"ExponentPushToken[{name}]", language="lt",
                                    active=1, created_at=now, updated_at=now)


class _FakeResponse:

    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        return self._body


class ReceiptStageTests(TestCase):

    def setUp(self):
        # A store of this test's own — the live server's file
        # in the container's /tmp is never touched
        self.dir = tempfile.mkdtemp(prefix="knf-receipts-")
        self.store = os.path.join(self.dir, "receipts.json")
        settings_override = override_settings(PUSH_RECEIPT_STORE=self.store)
        settings_override.enable()
        self.addCleanup(settings_override.disable)
        self.addCleanup(shutil.rmtree, self.dir, True)

        # No real watcher threads: the arming is recorded, the
        # 15-minute sleep never starts. Only push's own module
        # reference is swapped — the process-wide threading
        # module stays untouched
        self.armed = []
        fake_threading = mock.Mock()
        fake_threading.Thread.side_effect = lambda **kw: mock.Mock(start=lambda: self.armed.append(kw["name"]))
        thread_patch = mock.patch.object(push, "threading", fake_threading)
        thread_patch.start()
        self.addCleanup(thread_patch.stop)
        push._receipt_watcher_alive = False
        self.addCleanup(setattr, push, "_receipt_watcher_alive", False)

        # A controllable wall clock for the 15-minute delay
        self.now = 1_000_000.0
        clock_patch = mock.patch.object(push.time, "time", side_effect=lambda: self.now)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)

        # The receipts endpoint, scripted per test; every call
        # records the ids it asked about
        self.asked = []
        self.answers = []

        def fake_post(url, json=None, headers=None, timeout=None):
            self.assertEqual(url, push.EXPO_RECEIPTS_URL)
            self.asked.append(list(json["ids"]))
            answer = self.answers.pop(0) if self.answers else _FakeResponse(200, {"data": {}})
            if isinstance(answer, Exception):
                raise answer
            return answer

        post_patch = mock.patch.object(push._SESSION, "post", side_effect=fake_post)
        post_patch.start()
        self.addCleanup(post_patch.stop)

    def _stored(self):
        with open(self.store, encoding="utf-8") as fh:
            return json.load(fh)

    def _queue(self, *ids):
        push._queue_receipts([(ticket_id, f"ExponentPushToken[{ticket_id}]") for ticket_id in ids])

    def _later(self, seconds=push._RECEIPT_DELAY):
        self.now += seconds

    def test_a_queued_ticket_lands_in_the_shared_store_and_arms_one_watcher(self):
        self._queue("T1", "T2")
        self._queue("T3")
        self.assertEqual([row[0] for row in self._stored()], ["T1", "T2", "T3"])
        self.assertEqual([row[3] for row in self._stored()], [0, 0, 0])
        self.assertEqual(self.armed, ["expo-receipt-watcher"])
        self.assertEqual(push.pending_push_receipts(), 3)

    def test_a_cron_process_exiting_leaves_its_tickets_for_the_next_pass(self):
        # The cron fan-out queues and exits: its watcher thread
        # dies with it (modelled by forgetting it was armed)
        self._queue("C1", "C2")
        push._receipt_watcher_alive = False

        # Too early — nothing is asked, nothing is lost
        self.assertEqual(push.poll_push_receipts(), 0)
        self.assertEqual(self.asked, [])
        self.assertEqual(push.pending_push_receipts(), 2)

        # Fifteen minutes on, ANOTHER process's pass asks for both
        self._later()
        self.answers = [_FakeResponse(200, {"data": {"C1": {"status": "ok"}, "C2": {"status": "ok"}}})]
        self.assertEqual(push.poll_push_receipts(), 2)
        self.assertEqual(self.asked, [["C1", "C2"]])
        self.assertEqual(push.pending_push_receipts(), 0)

    def test_ids_expo_has_no_verdict_for_ride_again_up_to_three_rounds(self):
        self._queue("T1", "T2", "T3")
        self._later()

        # Round 1: Expo answers for T1 only — T2 and T3 are still
        # in flight to APNs/FCM and must not be forgotten
        self.answers = [_FakeResponse(200, {"data": {"T1": {"status": "ok"}}})]
        self.assertEqual(push.poll_push_receipts(), 1)
        self.assertEqual(sorted((row[0], row[3]) for row in self._stored()), [("T2", 1), ("T3", 1)])

        # Round 2: still nothing for them; round 3: the last try
        self.assertEqual(push.poll_push_receipts(), 0)
        self.assertEqual(sorted((row[0], row[3]) for row in self._stored()), [("T2", 2), ("T3", 2)])
        self.assertEqual(push.poll_push_receipts(), 0)
        self.assertEqual(self._stored(), [])
        self.assertEqual(self.asked, [["T1", "T2", "T3"], ["T2", "T3"], ["T2", "T3"]])

    def test_a_failed_receipts_request_keeps_its_whole_slice(self):
        self._queue("A1", "A2", "A3")
        self._later()

        self.answers = [push.requests.ConnectionError("down")]
        self.assertEqual(push.poll_push_receipts(), 0)
        self.assertEqual(sorted(row[0] for row in self._stored()), ["A1", "A2", "A3"])

        # A non-200 keeps them too
        self.answers = [_FakeResponse(503, {"errors": ["busy"]})]
        self.assertEqual(push.poll_push_receipts(), 0)
        self.assertEqual(sorted((row[0], row[3]) for row in self._stored()), [("A1", 2), ("A2", 2), ("A3", 2)])

        # ...and the next healthy pass reads them
        self.answers = [_FakeResponse(200, {"data": {tid: {"status": "ok"} for tid in ("A1", "A2", "A3")}})]
        self.assertEqual(push.poll_push_receipts(), 3)
        self.assertEqual(self._stored(), [])

    def test_a_device_not_registered_receipt_retires_the_token(self):
        user = create_user(username="istrynes")
        _token(user, "D1")
        self._queue("D1")
        self._later()

        self.answers = [_FakeResponse(200, {"data": {"D1": {"status": "error", "message": "gone",
                                                            "details": {"error": "DeviceNotRegistered"}}}})]
        self.assertEqual(push.poll_push_receipts(), 1)
        self.assertEqual(PushToken.objects.get().active, 0)

    def test_a_ticket_older_than_expos_retention_is_dropped_unasked(self):
        self._queue("OLD")
        self._later(push._RECEIPT_MAX_AGE + 1)
        self.assertEqual(push.poll_push_receipts(), 0)
        self.assertEqual(self.asked, [])
        self.assertEqual(self._stored(), [])

    def test_a_slice_send_parks_its_accepted_tickets_in_one_write(self):
        batch = [{"to": f"ExponentPushToken[S{i}]"} for i in range(3)]
        tickets = {"data": [{"status": "ok", "id": "S0"}, {"status": "error", "details": {"error": "MessageTooBig"}},
                            {"status": "ok", "id": "S2"}]}
        with mock.patch.object(push._SESSION, "post", return_value=_FakeResponse(200, tickets)), \
                mock.patch.object(push, "_pace_slice"), \
                mock.patch.object(push, "_write_receipt_store", wraps=push._write_receipt_store) as writes:
            sent, dead, errors = push._send_slice(batch, deadline=float("inf"))

        self.assertEqual((sent, dead, errors), (2, [], {"MessageTooBig": 1}))
        self.assertEqual([row[0] for row in self._stored()], ["S0", "S2"])
        self.assertEqual(writes.call_count, 1)

    def test_a_corrupt_store_reads_as_empty_and_heals(self):
        with open(self.store, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(push.pending_push_receipts(), 0)
        self._queue("H1")
        self.assertEqual([row[0] for row in self._stored()], ["H1"])

    def test_an_unusable_store_falls_back_to_memory_and_never_fails_a_send(self):
        push._receipt_memory.clear()
        self.addCleanup(push._receipt_memory.clear)
        self.addCleanup(setattr, push, "_receipt_store_failed", False)
        with override_settings(PUSH_RECEIPT_STORE=os.path.join(self.dir, "missing", "dir", "receipts.json")):
            self._queue("M1", "M2")
            self.assertEqual(push.pending_push_receipts(), 2)
            self._later()
            self.answers = [_FakeResponse(200, {"data": {"M1": {"status": "ok"}, "M2": {"status": "ok"}}})]
            self.assertEqual(push.poll_push_receipts(), 2)
            self.assertEqual(push.pending_push_receipts(), 0)

    def test_the_management_command_runs_the_pass_and_reports(self):
        self._queue("K1", "K2")
        self._later()
        self.answers = [_FakeResponse(200, {"data": {"K1": {"status": "ok"}}})]

        out = StringIO()
        call_command("poll_push_receipts", stdout=out)

        self.assertEqual(out.getvalue().strip(), "Push receipts: 1 checked, 1 pending")
        self.assertEqual([(row[0], row[3]) for row in self._stored()], [("K2", 1)])
