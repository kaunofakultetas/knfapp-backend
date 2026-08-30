# -----------------------------------------------------------
#  [*] Tests — the admin broadcast slice, exhaustively
#      (app/admin/routes.py)
#
#  Everything the broadcast push is made of, branch by branch:
#
#    send_admin_notification  POST /api/admin/notifications
#    broadcast_job_status     GET  /api/admin/notifications/<id>
#    _run_broadcast           the background fan-out
#    _set_broadcast_job       the in-process job registry write
#    _broadcast_job           the registry read
#    _fanout_counts           whatever push handed back
#    _get_socketio            the deferred SocketIO lookup
#
#  What this file proves that the broader admin files do not:
#
#    - every guard in the route answers BEFORE anything is
#      registered, audited or scheduled — a refused body leaves
#      no job, no admin_audit row and no background task, and
#      the guards fire in their written order (a 201-character
#      title with a 1001-character body is a title error).
#    - the boundaries are counted the way the route says:
#      CHARACTERS for title/body (200 / 1000, "ą" and an emoji
#      each counting as one, trimmed first) and UTF-8 BYTES for
#      the `data` payload (3072), so 1600 Lithuanian characters
#      are refused while 3072 ASCII ones are not.
#    - what goes ON THE WIRE is not what goes to Expo: the JSON
#      provider escapes the 202 and the status body, while
#      notify_channel and the audit trail get the caller's raw
#      text (bodies are posted as raw bytes here — TESTPLAN
#      rule 10).
#    - _fanout_counts reads every shape push can hand back and
#      RAISES on the shapes it cannot — which _run_broadcast
#      catches with the fan-out itself, so an unreadable result
#      finishes the job as failed instead of stranding it in
#      "running".
#    - the registry is a bounded LRU with a real eviction race:
#      a job evicted while its fan-out is in flight stays
#      evicted, because the done-write updates and never
#      re-creates (_update_broadcast_job) — a half record with
#      no title or createdAt was outside the shape the 202
#      promised.
#    - the job id is not a capability — a second admin reads
#      another admin's job, a student is refused with 403 rather
#      than the 404 an unknown id gets, and neither route leaks
#      a job's existence to a non-admin.
# -----------------------------------------------------------

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

import pytest
import time_machine

import app as app_package
from app.admin import routes as admin_routes
from app.auth.routes import _rate_limit_store


NOTIFICATIONS = "/api/admin/notifications"

# The wire shape the 202 and the status body both carry
JOB_FIELDS = {"jobId", "status", "sent", "failed", "title", "createdAt", "finishedAt", "message", "distinctUsers"}




# -----------------------------------------------------------
# _fresh_state
# -----------------------------------------------------------
#
# The job registry and auth's rate-limit store are both
# PROCESS-wide and outlive the `app` fixture, so a job minted
# here would still be visible to the next test and this file's
# request count would ride on top of every earlier file's
# global per-IP budget.
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _fresh_state():
    admin_routes._broadcast_jobs.clear()
    _rate_limit_store.clear()
    yield
    admin_routes._broadcast_jobs.clear()
    _rate_limit_store.clear()




# -----------------------------------------------------------
# _InlineSocketIO / inline_socketio / queued_socketio
# -----------------------------------------------------------
#
# The route hands the fan-out to
# socketio.start_background_task, which is a real thread in
# production. Two stand-ins:
#
#   inline_socketio — runs the target INSIDE the request, so a
#                     job's whole lifecycle is observable with
#                     no sleeping and no race
#   queued_socketio — records the call and runs NOTHING, for
#                     the tests that only care what was
#                     scheduled or must drive the task later
#
# `on_schedule` fires before the target runs, which is how the
# tests look at the registry at the instant the task starts.
# -----------------------------------------------------------

class _InlineSocketIO:
    def __init__(self, run=True):
        self.tasks = []
        self.run = run
        self.on_schedule = None
        self.raises = None

    def start_background_task(self, target, *args, **kwargs):
        self.tasks.append((target, args, kwargs))
        if self.on_schedule is not None:
            self.on_schedule(*args)
        if self.raises is not None:
            raise self.raises
        if self.run:
            target(*args, **kwargs)
        return None


@pytest.fixture
def inline_socketio(monkeypatch):
    fake = _InlineSocketIO(run=True)
    monkeypatch.setattr(admin_routes, "_get_socketio", lambda: fake)
    return fake


@pytest.fixture
def queued_socketio(monkeypatch):
    fake = _InlineSocketIO(run=False)
    monkeypatch.setattr(admin_routes, "_get_socketio", lambda: fake)
    return fake




# -----------------------------------------------------------
# fake_notify
# -----------------------------------------------------------
#
# notify_channel with no Expo and no network behind it. Set
# `.result` to an int, a tuple, a dict or an Exception (raised
# instead of returned) and read `.calls` afterwards; `.during`
# is called while the fan-out is mid-flight, which is the only
# moment "running" is on the record.
# -----------------------------------------------------------

@pytest.fixture
def fake_notify(monkeypatch):
    from app.notifications import push as push_module

    def _notify(channel, title, body, data=None, **kwargs):
        _notify.calls.append({"channel": channel, "title": title, "body": body,
                              "data": data, "kwargs": kwargs})
        if _notify.during is not None:
            _notify.during()
        if isinstance(_notify.result, BaseException):
            raise _notify.result
        return _notify.result

    _notify.calls = []
    _notify.result = 0
    _notify.during = None
    monkeypatch.setattr(push_module, "notify_channel", _notify)
    return _notify




# -----------------------------------------------------------
# _post / _post_raw
# -----------------------------------------------------------
#
# _post is the smallest valid broadcast, overridable field by
# field. _post_raw puts EXACTLY the given object on the wire
# with the stdlib serialiser — the test client's `json=` kwarg
# runs through the app's html-escaping JSON provider
# (TESTPLAN rule 10), which would quietly pre-escape any body
# whose markup is the point.
# -----------------------------------------------------------

def _post(client, headers, **body):
    payload = {"title": "Pranešimas", "body": "Turinys"}
    payload.update(body)
    return client.post(NOTIFICATIONS, headers=headers, json=payload)


def _post_raw(client, headers, payload, content_type="application/json"):
    raw = json.dumps(payload, ensure_ascii=False).encode() if not isinstance(payload, bytes) else payload
    return client.post(NOTIFICATIONS, data=raw,
                       headers={**headers, "Content-Type": content_type})




# -----------------------------------------------------------
# _padded_data
# -----------------------------------------------------------
#
# A `data` object that serialises to EXACTLY `size` bytes once
# the route has stamped the type marker on it — the only way
# to sit ON the _BROADCAST_DATA_MAX boundary. `fill` picks the
# padding character, so the same helper builds the ASCII and
# the multi-byte Lithuanian case; an odd byte left over by a
# multi-byte fill is taken up by one ASCII character.
# -----------------------------------------------------------

def _padded_data(size, fill="x"):
    empty = json.dumps({"pad": "", "type": "admin_announcement"}, ensure_ascii=False).encode()
    room = size - len(empty)
    width = len(fill.encode())
    assert room >= 0, "the marker alone is already past the requested size"
    padded = {"pad": fill * (room // width) + "x" * (room % width)}
    assert len(json.dumps({**padded, "type": "admin_announcement"},
                          ensure_ascii=False).encode()) == size
    return padded




# -----------------------------------------------------------
# _audit_rows / _job_id
# -----------------------------------------------------------

def _audit_rows(db):
    return db.execute(
        "SELECT * FROM admin_audit WHERE action = 'notification.broadcast' ORDER BY created_at"
    ).fetchall()


def _job_id(response):
    return response.get_json()["jobId"]




# ===========================================================
# _get_socketio
# ===========================================================

def test_the_socketio_lookup_hands_back_the_instance_bound_on_the_package(app):
    from app import socketio

    assert admin_routes._get_socketio() is socketio


def test_the_socketio_lookup_is_deferred_to_call_time(monkeypatch):
    # The import lives INSIDE the helper as the cycle guard, so
    # rebinding the package attribute changes what comes back —
    # proof the name is not captured at import time
    sentinel = object()
    monkeypatch.setattr(app_package, "socketio", sentinel)

    assert admin_routes._get_socketio() is sentinel


def test_the_socketio_lookup_needs_no_app_or_request_context():
    assert admin_routes._get_socketio() is not None


def test_the_route_schedules_through_the_real_lookup(client, admin, monkeypatch, fake_notify):
    # Everything else here patches _get_socketio itself; this one
    # leaves the helper alone and swaps the bound instance, so the
    # route's own call is the one under test
    _, headers = admin
    recorder = _InlineSocketIO(run=False)
    monkeypatch.setattr(app_package, "socketio", recorder)

    response = _post(client, headers)

    assert response.status_code == 202
    assert [task[0] for task in recorder.tasks] == [admin_routes._run_broadcast]




# ===========================================================
# _fanout_counts — every shape push may hand back
# ===========================================================

@pytest.mark.parametrize("result,expected", [
    # the bare accepted-ticket count push returns today
    (0, (0, 0)),
    (7, (7, 0)),
    (10 ** 12, (10 ** 12, 0)),
    (-3, (-3, 0)),
    (None, (0, 0)),
    (False, (0, 0)),
    (True, (1, 0)),
    (3.9, (3, 0)),
    (-3.9, (-3, 0)),
    ("", (0, 0)),
    ("12", (12, 0)),
    ((), (0, 0)),
    # the (sent, failed) pair push is growing
    ((4, 2), (4, 2)),
    ((0, 0), (0, 0)),
    (("4", "2"), (4, 2)),
    ((4.9, 2.9), (4, 2)),
    ((True, False), (1, 0)),
    ((-1, -2), (-1, -2)),
    # the dict shape
    ({}, (0, 0)),
    ({"sent": 9, "failed": 1}, (9, 1)),
    ({"sent": 3}, (3, 0)),
    ({"failed": 2}, (0, 2)),
    ({"sent": "5", "failed": "6"}, (5, 6)),
    ({"sent": 2.7}, (2, 0)),
    ({"sent": True, "failed": False}, (1, 0)),
    ({"sent": 1, "failed": 1, "kita": "ignoruojama"}, (1, 1)),
])
def test_the_fanout_counts_read_every_result_shape(result, expected):
    assert admin_routes._fanout_counts(result) == expected


def test_the_fanout_counts_read_a_two_field_namedtuple_as_the_pair():
    from collections import namedtuple

    pair = namedtuple("Pair", "sent failed")

    assert admin_routes._fanout_counts(pair(6, 1)) == (6, 1)


def test_the_fanout_counts_read_a_dict_subclass_like_a_dict():
    from collections import OrderedDict

    assert admin_routes._fanout_counts(OrderedDict(sent=2, failed=3)) == (2, 3)


@pytest.mark.parametrize("result,error", [
    ((5,), TypeError),               # a 1-tuple is not the pair
    ((1, 2, 3), TypeError),          # nor is a 3-tuple
    ([4, 2], TypeError),             # a LIST is not a tuple
    ({"sent": None}, TypeError),
    ({"sent": "abc"}, ValueError),
    ({"failed": []}, TypeError),
    (object(), TypeError),
    ("septyni", ValueError),
])
def test_a_result_shape_the_fanout_counts_cannot_read_raises(result, error):
    with pytest.raises(error):
        admin_routes._fanout_counts(result)


def test_the_fanout_counts_never_clamp_a_negative_pair():
    # Documented as-is: nothing here invents a floor, so a future
    # push.py returning -1 shows -1 rather than a quiet 0
    assert admin_routes._fanout_counts((-5, -7)) == (-5, -7)




# ===========================================================
# _set_broadcast_job / _broadcast_job — the registry
# ===========================================================

def test_a_new_job_record_carries_its_own_id():
    record = admin_routes._set_broadcast_job("darbas", status="queued")

    assert record == {"jobId": "darbas", "status": "queued"}


def test_a_job_can_be_created_with_no_fields_at_all():
    assert admin_routes._set_broadcast_job("tuscias") == {"jobId": "tuscias"}
    assert admin_routes._broadcast_job("tuscias") == {"jobId": "tuscias"}


def test_an_update_merges_instead_of_replacing():
    admin_routes._set_broadcast_job("darbas", status="queued", title="Tema", sent=0)
    admin_routes._set_broadcast_job("darbas", status="done", sent=4)

    assert admin_routes._broadcast_job("darbas") == {"jobId": "darbas", "status": "done",
                                                     "title": "Tema", "sent": 4}


def test_a_field_set_to_none_is_stored_as_none():
    admin_routes._set_broadcast_job("darbas", finishedAt=None)

    assert admin_routes._broadcast_job("darbas")["finishedAt"] is None


def test_the_registry_key_wins_over_a_jobid_in_the_fields():
    # Nothing in the module does this — pinned so the lookup key
    # can never be mistaken for the record's own field
    admin_routes._set_broadcast_job("raktas", jobId="kitas")

    assert admin_routes._broadcast_job("raktas") == {"jobId": "kitas"}
    assert admin_routes._broadcast_job("kitas") is None


def test_the_record_handed_out_by_a_write_is_a_copy():
    handed_out = admin_routes._set_broadcast_job("darbas", status="queued")
    handed_out["status"] = "sugadinta"
    handed_out["naujas"] = True

    assert admin_routes._broadcast_job("darbas") == {"jobId": "darbas", "status": "queued"}


def test_the_record_handed_out_by_a_read_is_a_copy():
    admin_routes._set_broadcast_job("darbas", status="queued")

    handed_out = admin_routes._broadcast_job("darbas")
    handed_out["status"] = "sugadinta"

    assert admin_routes._broadcast_job("darbas")["status"] == "queued"


def test_the_copy_is_shallow_so_a_nested_field_is_shared():
    # The registry only ever holds scalars today; pinned because a
    # nested field would NOT be protected by the copy
    admin_routes._set_broadcast_job("darbas", payload={"a": 1})

    admin_routes._broadcast_job("darbas")["payload"]["a"] = 2

    assert admin_routes._broadcast_job("darbas")["payload"] == {"a": 2}


@pytest.mark.parametrize("job_id", ["", "0", "ą-ž", "x" * 10000, 7, None, ("a", "b")])
def test_any_hashable_id_can_key_a_job(job_id):
    admin_routes._set_broadcast_job(job_id, status="queued")

    assert admin_routes._broadcast_job(job_id)["status"] == "queued"


@pytest.mark.parametrize("job_id", ["nera-tokio", "", None, 0])
def test_an_unknown_job_id_reads_as_none(job_id):
    assert admin_routes._broadcast_job(job_id) is None


def test_the_registry_holds_exactly_the_cap_without_evicting():
    for index in range(admin_routes._BROADCAST_JOBS_MAX):
        admin_routes._set_broadcast_job(f"darbas-{index}", status="queued")

    assert len(admin_routes._broadcast_jobs) == admin_routes._BROADCAST_JOBS_MAX
    assert admin_routes._broadcast_job("darbas-0") is not None


def test_one_job_past_the_cap_evicts_exactly_the_oldest():
    for index in range(admin_routes._BROADCAST_JOBS_MAX + 1):
        admin_routes._set_broadcast_job(f"darbas-{index}", status="queued")

    assert len(admin_routes._broadcast_jobs) == admin_routes._BROADCAST_JOBS_MAX
    assert admin_routes._broadcast_job("darbas-0") is None
    assert admin_routes._broadcast_job("darbas-1") is not None
    assert admin_routes._broadcast_job(f"darbas-{admin_routes._BROADCAST_JOBS_MAX}") is not None


def test_the_survivors_are_the_newest_in_insertion_order():
    for index in range(admin_routes._BROADCAST_JOBS_MAX + 5):
        admin_routes._set_broadcast_job(f"darbas-{index}", status="queued")

    assert list(admin_routes._broadcast_jobs) == [
        f"darbas-{index}" for index in range(5, admin_routes._BROADCAST_JOBS_MAX + 5)
    ]


def test_an_update_moves_a_job_out_of_the_eviction_line():
    for index in range(admin_routes._BROADCAST_JOBS_MAX):
        admin_routes._set_broadcast_job(f"darbas-{index}", status="queued")
    admin_routes._set_broadcast_job("darbas-0", status="running")
    admin_routes._set_broadcast_job("naujas", status="queued")

    assert admin_routes._broadcast_job("darbas-0")["status"] == "running"
    assert admin_routes._broadcast_job("darbas-1") is None


def test_a_read_does_not_move_a_job_out_of_the_eviction_line():
    for index in range(admin_routes._BROADCAST_JOBS_MAX):
        admin_routes._set_broadcast_job(f"darbas-{index}", status="queued")
    admin_routes._broadcast_job("darbas-0")
    admin_routes._set_broadcast_job("naujas", status="queued")

    assert admin_routes._broadcast_job("darbas-0") is None


def test_a_shrunken_cap_evicts_every_extra_record_in_one_write(monkeypatch):
    # The eviction is a WHILE, not an IF: one write past a cap that
    # moved has to drain the whole overflow, not one record of it
    for index in range(10):
        admin_routes._set_broadcast_job(f"darbas-{index}", status="queued")
    monkeypatch.setattr(admin_routes, "_BROADCAST_JOBS_MAX", 3)

    admin_routes._set_broadcast_job("naujas", status="queued")

    assert list(admin_routes._broadcast_jobs) == ["darbas-8", "darbas-9", "naujas"]


def test_a_zero_cap_keeps_nothing_yet_still_hands_the_record_back(monkeypatch):
    monkeypatch.setattr(admin_routes, "_BROADCAST_JOBS_MAX", 0)

    record = admin_routes._set_broadcast_job("darbas", status="queued")

    assert record == {"jobId": "darbas", "status": "queued"}
    assert admin_routes._broadcast_jobs == {}
    assert admin_routes._broadcast_job("darbas") is None


def test_concurrent_writers_lose_no_job(monkeypatch):
    monkeypatch.setattr(admin_routes, "_BROADCAST_JOBS_MAX", 10000)
    ready = threading.Barrier(8)

    def _write(worker):
        ready.wait()
        for index in range(25):
            admin_routes._set_broadcast_job(f"darbas-{worker}-{index}", status="queued")

    threads = [threading.Thread(target=_write, args=(worker,)) for worker in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(admin_routes._broadcast_jobs) == 200


def test_concurrent_writers_on_one_job_lose_no_field():
    ready = threading.Barrier(8)

    def _write(worker):
        ready.wait()
        for index in range(25):
            admin_routes._set_broadcast_job("darbas", **{f"laukas-{worker}-{index}": worker})

    threads = [threading.Thread(target=_write, args=(worker,)) for worker in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    record = admin_routes._broadcast_job("darbas")
    assert len(record) == 201  # 200 fields + jobId
    assert len(admin_routes._broadcast_jobs) == 1




# ===========================================================
# _run_broadcast — the background fan-out
# ===========================================================

def test_the_fan_out_marks_the_job_running_before_it_calls_push(fake_notify):
    seen = {}
    admin_routes._set_broadcast_job("darbas", status="queued", title="Tema")
    fake_notify.during = lambda: seen.update(admin_routes._broadcast_job("darbas"))

    admin_routes._run_broadcast("darbas", "Tema", "Tekstas", {"type": "admin_announcement"})

    assert seen["status"] == "running"
    assert seen["title"] == "Tema"
    assert admin_routes._broadcast_job("darbas")["status"] == "done"


def test_the_fan_out_creates_the_record_for_a_job_it_never_saw(fake_notify):
    fake_notify.result = 3

    admin_routes._run_broadcast("nezinomas", "Tema", "Tekstas", {})

    record = admin_routes._broadcast_job("nezinomas")
    assert record["status"] == "done"
    assert record["sent"] == 3
    assert "title" not in record


def test_the_fan_out_goes_out_on_the_admin_channel_with_the_payload(fake_notify):
    payload = {"type": "admin_announcement", "postId": "7", "deep": {"a": [1, 2]}}

    admin_routes._run_broadcast("darbas", "Antraštė", "Tekstas", payload)

    # `stats` is the dict notify_channel fills with failed/users
    # — the route hands a fresh one in on every broadcast
    assert fake_notify.calls == [{"channel": "admin", "title": "Antraštė", "body": "Tekstas",
                                  "data": payload, "kwargs": {"stats": {}}}]


def test_the_fan_out_keeps_the_fields_the_request_put_on_the_job(fake_notify):
    fake_notify.result = (2, 1)
    admin_routes._set_broadcast_job("darbas", status="queued", title="Tema",
                                    createdAt="2026-01-01T00:00:00+00:00", sent=0, failed=0)

    admin_routes._run_broadcast("darbas", "Tema", "Tekstas", {})

    record = admin_routes._broadcast_job("darbas")
    assert record["title"] == "Tema"
    assert record["createdAt"] == "2026-01-01T00:00:00+00:00"
    assert (record["sent"], record["failed"]) == (2, 1)


def test_a_finished_fan_out_says_how_many_tickets_expo_accepted(fake_notify):
    fake_notify.result = 12

    admin_routes._run_broadcast("darbas", "T", "B", {})

    record = admin_routes._broadcast_job("darbas")
    assert record["message"] == "Accepted by Expo for 12 device token(s) across 0 user(s)"
    assert record["finishedAt"] is not None


def test_the_finished_record_reports_distinct_users_from_the_fan_out_stats(monkeypatch):
    # Tickets are devices, not people: the owner count comes out
    # of the stats dict notify_channel fills, next to failed
    from app.notifications import push as push_module

    def _notify(channel, title, body, data=None, stats=None, **kwargs):
        stats["users"] = 5
        stats["failed"] = 2
        return 9

    monkeypatch.setattr(push_module, "notify_channel", _notify)

    admin_routes._run_broadcast("darbas", "T", "B", {})

    record = admin_routes._broadcast_job("darbas")
    assert (record["sent"], record["failed"], record["distinctUsers"]) == (9, 2, 5)
    assert record["message"] == "Accepted by Expo for 9 device token(s) across 5 user(s)"


@pytest.mark.parametrize("result,sent,failed", [
    (0, 0, 0),
    (10 ** 9, 10 ** 9, 0),
    ((0, 40), 0, 40),
    ({"sent": 1, "failed": 0}, 1, 0),
])
def test_the_finished_record_reports_whatever_push_counted(fake_notify, result, sent, failed):
    fake_notify.result = result

    admin_routes._run_broadcast("darbas", "T", "B", {})

    record = admin_routes._broadcast_job("darbas")
    assert (record["sent"], record["failed"]) == (sent, failed)
    assert record["message"] == f"Accepted by Expo for {sent} device token(s) across 0 user(s)"


@pytest.mark.parametrize("error", [
    RuntimeError("Expo unreachable"),
    ValueError("bloga reikšmė"),
    sqlite3.OperationalError("database is locked"),
    TypeError("blogas tipas"),
    MemoryError(),
])
def test_any_exception_from_push_is_recorded_as_failed_and_never_raised(fake_notify, error):
    fake_notify.result = error

    admin_routes._run_broadcast("darbas", "T", "B", {})

    record = admin_routes._broadcast_job("darbas")
    assert record["status"] == "failed"
    assert record["message"] == "Broadcast failed — see the server log"
    assert record["finishedAt"] is not None


def test_a_failed_fan_out_leaves_the_counts_the_request_seeded(fake_notify):
    fake_notify.result = RuntimeError("Expo unreachable")
    admin_routes._set_broadcast_job("darbas", status="queued", sent=0, failed=0, title="Tema")

    admin_routes._run_broadcast("darbas", "Tema", "B", {})

    record = admin_routes._broadcast_job("darbas")
    assert (record["sent"], record["failed"], record["title"]) == (0, 0, "Tema")


def test_a_baseexception_from_push_is_not_swallowed(fake_notify):
    # `except Exception` deliberately does not catch a shutdown:
    # the job stays "running" and the interpreter keeps unwinding
    fake_notify.result = KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        admin_routes._run_broadcast("darbas", "T", "B", {})

    assert admin_routes._broadcast_job("darbas")["status"] == "running"


def test_a_failed_fan_out_is_logged_with_its_job_id(fake_notify, caplog):
    fake_notify.result = RuntimeError("Expo unreachable")

    with caplog.at_level("ERROR", logger="app.admin.routes"):
        admin_routes._run_broadcast("darbas-42", "T", "B", {})

    assert "darbas-42" in caplog.text
    assert "Expo unreachable" in caplog.text


def test_a_finished_fan_out_is_logged_with_both_counts(fake_notify, caplog):
    fake_notify.result = (5, 2)

    with caplog.at_level("INFO", logger="app.admin.routes"):
        admin_routes._run_broadcast("darbas-7", "T", "B", {})

    assert "Admin broadcast darbas-7: 5 accepted, 2 failed" in caplog.text


def test_the_fan_out_resolves_notify_channel_at_call_time(monkeypatch):
    # The import sits inside the function, so a module attribute
    # swapped after startup is the one that runs
    from app.notifications import push as push_module

    monkeypatch.setattr(push_module, "notify_channel", lambda *a, **k: 99)

    admin_routes._run_broadcast("darbas", "T", "B", {})

    assert admin_routes._broadcast_job("darbas")["sent"] == 99


def test_the_fan_out_needs_no_app_or_request_context(app):
    # The REAL notify_channel, no tokens registered and no request
    # anywhere: it must open its own connection and finish
    admin_routes._run_broadcast("darbas", "T", "B", {"type": "admin_announcement"})

    record = admin_routes._broadcast_job("darbas")
    assert record["status"] == "done"
    assert record["sent"] == 0


def test_rerunning_a_job_overwrites_its_result(fake_notify):
    fake_notify.result = 1
    with time_machine.travel("2026-03-01T10:00:00+00:00", tick=False):
        admin_routes._run_broadcast("darbas", "T", "B", {})
        first = admin_routes._broadcast_job("darbas")

    fake_notify.result = 8
    with time_machine.travel("2026-03-01T11:00:00+00:00", tick=False):
        admin_routes._run_broadcast("darbas", "T", "B", {})

    second = admin_routes._broadcast_job("darbas")
    assert (first["sent"], second["sent"]) == (1, 8)
    assert first["finishedAt"] != second["finishedAt"]
    assert second["finishedAt"].startswith("2026-03-01T11:00:00")


def test_a_job_evicted_mid_flight_is_not_resurrected_by_the_done_write(fake_notify, monkeypatch):
    # The one real race the registry has: the request's record is
    # pushed out by newer broadcasts while the fan-out is in
    # flight. The done-write used to put a HALF record back — one
    # without the title and createdAt the 202 promised — so it now
    # updates only, and the job stays forgotten
    monkeypatch.setattr(admin_routes, "_BROADCAST_JOBS_MAX", 3)
    admin_routes._set_broadcast_job("darbas", status="queued", title="Tema", createdAt="anksciau")

    def _evict():
        for index in range(3):
            admin_routes._set_broadcast_job(f"kitas-{index}", status="queued")

    fake_notify.during = _evict
    fake_notify.result = 2
    admin_routes._run_broadcast("darbas", "Tema", "B", {})

    assert admin_routes._broadcast_job("darbas") is None
    assert list(admin_routes._broadcast_jobs) == ["kitas-0", "kitas-1", "kitas-2"]


@pytest.mark.parametrize("result", [(1, 2, 3), (5,), [4, 2], {"sent": None}, {"sent": "abc"},
                                     object(), "septyni"])
def test_an_unreadable_push_result_marks_the_job_failed(fake_notify, result):
    # _fanout_counts runs INSIDE the try, so every shape it
    # refuses finishes the job instead of escaping the background
    # task and leaving it "running" for good
    fake_notify.result = result

    admin_routes._run_broadcast("darbas", "T", "B", {})

    record = admin_routes._broadcast_job("darbas")
    assert record["status"] == "failed"
    assert record["message"] == "Broadcast failed — see the server log"
    assert record["finishedAt"] is not None




# ===========================================================
# POST /api/admin/notifications — the guards
# ===========================================================

def test_a_broadcast_with_no_body_at_all_is_refused(client, admin, queued_socketio):
    _, headers = admin

    response = client.post(NOTIFICATIONS, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON object body required"
    assert queued_socketio.tasks == []


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded",
                                          "text/json-not-really"])
def test_a_broadcast_outside_a_json_content_type_is_refused(client, admin, queued_socketio, content_type):
    _, headers = admin

    response = _post_raw(client, headers, {"title": "T", "body": "B"}, content_type=content_type)

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON object body required"


def test_a_broadcast_on_a_vendor_json_content_type_is_accepted(client, admin, queued_socketio):
    # The before_request hook and Flask both go by "json" in the
    # mimetype, so +json suffixes parse like application/json
    _, headers = admin

    response = _post_raw(client, headers, {"title": "T", "body": "B"},
                         content_type="application/vnd.knf+json")

    assert response.status_code == 202


def test_a_json_null_body_is_refused_by_the_route(client, admin, queued_socketio):
    _, headers = admin

    response = _post_raw(client, headers, b"null")

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON object body required"


def test_an_empty_json_body_is_refused_by_the_route(client, admin, queued_socketio):
    _, headers = admin

    response = _post_raw(client, headers, b"")

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON object body required"


@pytest.mark.parametrize("body", [b"[1, 2]", b'"tekstas"', b"7", b"true"])
def test_a_non_object_json_body_is_refused_before_the_route(client, admin, queued_socketio, body):
    _, headers = admin

    response = _post_raw(client, headers, body)

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body must be an object"
    assert queued_socketio.tasks == []


@pytest.mark.parametrize("value", [True, False, 0, 1.5, [], {}, [1]])
def test_a_non_string_title_is_refused(client, admin, queued_socketio, value):
    _, headers = admin

    response = _post(client, headers, title=value)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Title and body must be strings"


@pytest.mark.parametrize("value", [True, False, 0, 1.5, [], {}, None])
def test_a_non_string_body_is_refused(client, admin, queued_socketio, value):
    _, headers = admin

    response = _post(client, headers, body=value)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Title and body must be strings"


def test_the_string_check_beats_the_emptiness_check(client, admin, queued_socketio):
    _, headers = admin

    response = client.post(NOTIFICATIONS, headers=headers, json={"title": 1, "body": 2})

    assert response.get_json()["error"] == "Title and body must be strings"


@pytest.mark.parametrize("title,body", [
    ("", "Turinys"), ("Tema", ""), (" ", "Turinys"), ("Tema", "\t\n\r "),
    ("\xa0", "Turinys"),          # a no-break space IS whitespace to str.strip
    ("\u2003", "Turinys"),        # so is an em space
])
def test_a_title_or_body_that_trims_to_nothing_is_refused(client, admin, queued_socketio, title, body):
    _, headers = admin

    response = _post(client, headers, title=title, body=body)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Title and body are required"


def test_a_zero_width_space_is_a_character_not_whitespace(client, admin, queued_socketio):
    # str.strip does not touch U+200B, so this is a one-character
    # title and the route accepts it — pinned as the boundary of
    # "trimmed before the checks"
    _, headers = admin

    response = _post(client, headers, title="\u200b")

    assert response.status_code == 202


def test_a_title_of_null_bytes_is_stripped_to_nothing_and_refused(client, admin, queued_socketio):
    # The before_request hook removes NUL from every string, so
    # what reaches the route is an empty title
    _, headers = admin

    response = _post_raw(client, headers, {"title": "\x00\x00", "body": "Turinys"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Title and body are required"


def test_a_null_byte_inside_a_title_is_stripped_before_the_fan_out(client, admin, inline_socketio,
                                                                    fake_notify):
    _, headers = admin

    response = _post_raw(client, headers, {"title": "Te\x00ma", "body": "Turinys"})

    assert response.status_code == 202
    assert fake_notify.calls[0]["title"] == "Tema"


@pytest.mark.parametrize("missing", [{"body": "Turinys"}, {"title": "Tema"}, {}])
def test_a_missing_title_or_body_reads_as_empty(client, admin, queued_socketio, missing):
    _, headers = admin

    response = client.post(NOTIFICATIONS, headers=headers, json=missing)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Title and body are required"


def test_an_uppercase_json_content_type_is_still_json(client, admin, queued_socketio):
    _, headers = admin

    response = _post_raw(client, headers, {"title": "T", "body": "B"},
                         content_type="APPLICATION/JSON")

    assert response.status_code == 202


def test_a_repeated_key_keeps_the_last_value(client, admin, queued_socketio):
    # json.loads' own rule, pinned because a repeated title is the
    # cheapest way to try to smuggle a second one past the checks
    _, headers = admin

    response = _post_raw(client, headers, b'{"title": "t' + b't' * 250 + b'", "title": "Tema",'
                                          b' "body": "Turinys"}')

    assert response.status_code == 202
    assert response.get_json()["title"] == "Tema"


def test_unknown_body_keys_are_ignored(client, admin, inline_socketio, fake_notify):
    # Nothing outside title / body / data is read — in particular
    # `channel`, which would otherwise aim a broadcast at a channel
    # nobody opted out of
    _, headers = admin

    response = _post_raw(client, headers, {"title": "T", "body": "B", "channel": "news",
                                           "exclude_user_id": "kazkas", "nesamone": True})

    assert response.status_code == 202
    assert fake_notify.calls[0]["channel"] == "admin"
    assert fake_notify.calls[0]["kwargs"] == {"stats": {}}


def test_the_caller_cannot_dictate_the_job_record(client, admin, queued_socketio):
    _, headers = admin

    job = _post_raw(client, headers, {
        "title": "Tema", "body": "Turinys", "jobId": "mano-darbas", "status": "done",
        "sent": 999, "failed": -1, "finishedAt": "2000-01-01T00:00:00+00:00",
        "message": "kitas tekstas",
    }).get_json()

    assert job["jobId"] != "mano-darbas"
    assert (job["status"], job["sent"], job["failed"]) == ("queued", 0, 0)
    assert job["finishedAt"] is None
    assert job["message"] == "Broadcast accepted for delivery on the admin channel"




# ===========================================================
# POST /api/admin/notifications — the length boundaries
# ===========================================================

@pytest.mark.parametrize("title", ["t", "t" * 199, "t" * 200, "ą" * 200, "😀" * 200])
def test_a_title_up_to_two_hundred_characters_is_accepted(client, admin, queued_socketio, title):
    _, headers = admin

    response = _post_raw(client, headers, {"title": title, "body": "Turinys"})

    assert response.status_code == 202


@pytest.mark.parametrize("title", ["t" * 201, "ą" * 201, "😀" * 201, "t" * 5000])
def test_a_title_past_two_hundred_characters_is_refused(client, admin, queued_socketio, title):
    _, headers = admin

    response = _post_raw(client, headers, {"title": title, "body": "Turinys"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Title must be at most 200 characters"


def test_a_two_hundred_character_title_survives_its_surrounding_whitespace(client, admin,
                                                                           queued_socketio):
    _, headers = admin

    response = _post_raw(client, headers, {"title": "\n\t  " + "t" * 200 + "  \r\n", "body": "B"})

    assert response.status_code == 202
    assert response.get_json()["title"] == "t" * 200


@pytest.mark.parametrize("body", ["b", "b" * 999, "b" * 1000, "ą" * 1000])
def test_a_body_up_to_a_thousand_characters_is_accepted(client, admin, queued_socketio, body):
    _, headers = admin

    response = _post_raw(client, headers, {"title": "Tema", "body": body})

    assert response.status_code == 202


@pytest.mark.parametrize("body", ["b" * 1001, "ą" * 1001])
def test_a_body_past_a_thousand_characters_is_refused(client, admin, queued_socketio, body):
    _, headers = admin

    response = _post_raw(client, headers, {"title": "Tema", "body": body})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Body must be at most 1000 characters"


def test_the_title_length_is_checked_before_the_body_length(client, admin, queued_socketio):
    _, headers = admin

    response = _post_raw(client, headers, {"title": "t" * 201, "body": "b" * 1001})

    assert response.get_json()["error"] == "Title must be at most 200 characters"


def test_an_empty_title_beats_an_oversized_body(client, admin, queued_socketio):
    _, headers = admin

    response = _post_raw(client, headers, {"title": "   ", "body": "b" * 1001})

    assert response.get_json()["error"] == "Title and body are required"


def test_a_body_of_a_million_characters_is_refused_rather_than_sent(client, admin, queued_socketio):
    _, headers = admin

    response = _post_raw(client, headers, {"title": "Tema", "body": "b" * 1_000_000})

    assert response.status_code == 400
    assert queued_socketio.tasks == []


def test_a_body_over_the_app_wide_size_ceiling_never_reaches_the_route(client, admin, queued_socketio):
    _, headers = admin
    oversized = b'{"title": "T", "body": "' + b"b" * (7 * 1024 * 1024) + b'"}'

    response = client.post(NOTIFICATIONS, data=oversized,
                           headers={**headers, "Content-Type": "application/json"})

    assert response.status_code == 413
    assert queued_socketio.tasks == []




# ===========================================================
# POST /api/admin/notifications — the data payload
# ===========================================================

@pytest.mark.parametrize("value", ["tekstas", "", 5, 0, 1.5, ["a"], [], True, False])
def test_a_data_payload_that_is_not_an_object_is_refused(client, admin, queued_socketio, value):
    _, headers = admin

    response = _post(client, headers, data=value)

    assert response.status_code == 400
    assert response.get_json()["error"] == "data must be an object"


def test_an_empty_data_object_carries_only_the_marker(client, admin, inline_socketio, fake_notify):
    _, headers = admin

    _post(client, headers, data={})

    assert fake_notify.calls[0]["data"] == {"type": "admin_announcement"}


@pytest.mark.parametrize("supplied", [
    {"type": "chat_message"}, {"type": None}, {"type": 7}, {"type": {"nested": True}},
])
def test_the_announcement_marker_replaces_any_caller_type(client, admin, inline_socketio,
                                                           fake_notify, supplied):
    _, headers = admin

    _post(client, headers, data=supplied)

    assert fake_notify.calls[0]["data"]["type"] == "admin_announcement"


def test_the_caller_payload_rides_along_untouched(client, admin, inline_socketio, fake_notify):
    _, headers = admin
    payload = {"postId": "7", "nested": {"list": [1, 2, {"gilu": True}]}, "skaicius": 3.5,
               "tuscia": None, "vėliava": False}

    _post_raw(client, headers, {"title": "T", "body": "B", "data": payload})

    assert fake_notify.calls[0]["data"] == {**payload, "type": "admin_announcement"}


def test_a_payload_exactly_at_the_ceiling_is_accepted(client, admin, queued_socketio):
    _, headers = admin

    response = _post(client, headers, data=_padded_data(admin_routes._BROADCAST_DATA_MAX))

    assert response.status_code == 202


def test_a_payload_one_byte_past_the_ceiling_is_refused(client, admin, queued_socketio):
    _, headers = admin

    response = _post(client, headers, data=_padded_data(admin_routes._BROADCAST_DATA_MAX + 1))

    assert response.status_code == 400
    assert response.get_json()["error"] == "data must serialise to at most 3072 bytes"
    assert queued_socketio.tasks == []


def test_the_ceiling_counts_utf8_bytes_not_characters(client, admin, queued_socketio):
    # 1600 Lithuanian characters are 3200 bytes: refused, although
    # the same count of ASCII would sail through
    _, headers = admin

    response = _post_raw(client, headers, {"title": "T", "body": "B", "data": {"pad": "ą" * 1600}})

    assert response.status_code == 400
    assert response.get_json()["error"] == "data must serialise to at most 3072 bytes"


def test_a_multibyte_payload_exactly_at_the_ceiling_is_accepted(client, admin, queued_socketio):
    _, headers = admin

    response = _post_raw(client, headers, {
        "title": "T", "body": "B",
        "data": _padded_data(admin_routes._BROADCAST_DATA_MAX, fill="ą"),
    })

    assert response.status_code == 202


def test_the_marker_counts_towards_the_ceiling(client, admin, queued_socketio):
    # The payload the caller sent fits; the payload the route
    # BUILDS does not, and it is the built one that is measured
    _, headers = admin
    marker_bytes = len(json.dumps({"type": "admin_announcement"}).encode()) - 2

    just_under = _padded_data(admin_routes._BROADCAST_DATA_MAX)
    just_under["pad"] += "x" * marker_bytes

    assert len(json.dumps({"pad": just_under["pad"]}).encode()) <= admin_routes._BROADCAST_DATA_MAX
    assert _post(client, headers, data=just_under).status_code == 400


def test_a_deeply_nested_payload_is_refused_before_the_route(client, admin, queued_socketio):
    _, headers = admin
    nested = {"gilu": True}
    for _ in range(40):
        nested = {"gilu": nested}

    response = _post_raw(client, headers, {"title": "T", "body": "B", "data": nested})

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON nesting too deep"
    assert queued_socketio.tasks == []


def test_a_modestly_nested_payload_reaches_expo_intact(client, admin, inline_socketio, fake_notify):
    _, headers = admin
    nested = {"gilu": True}
    for _ in range(8):
        nested = {"gilu": nested}

    _post_raw(client, headers, {"title": "T", "body": "B", "data": nested})

    assert fake_notify.calls[0]["data"] == {**nested, "type": "admin_announcement"}




# ===========================================================
# POST /api/admin/notifications — what the 202 promises
# ===========================================================

@pytest.mark.contract
def test_the_accepted_job_carries_exactly_the_documented_fields(client, admin, queued_socketio):
    _, headers = admin

    response = _post(client, headers, title="Dėmesio", body="Rytoj nevyks paskaitos")

    assert response.status_code == 202
    job = response.get_json()
    assert set(job) == JOB_FIELDS
    assert job["status"] == "queued"
    assert (job["sent"], job["failed"]) == (0, 0)
    assert job["title"] == "Dėmesio"
    assert job["finishedAt"] is None
    assert job["message"] == "Broadcast accepted for delivery on the admin channel"
    assert uuid.UUID(job["jobId"]).version == 4


def test_the_accepted_job_is_stamped_with_the_current_time(client, admin, queued_socketio):
    _, headers = admin

    with time_machine.travel("2026-05-04T08:30:00+00:00", tick=False):
        job = _post(client, headers).get_json()

    assert job["createdAt"] == "2026-05-04T08:30:00+00:00"


def test_the_broadcast_answers_no_store_like_every_admin_body(client, admin, queued_socketio):
    _, headers = admin

    response = _post(client, headers)

    assert "no-store" in response.headers.get("Cache-Control", "")
    assert response.headers["Content-Type"].startswith("application/json")


def test_the_job_is_in_the_registry_before_the_task_is_scheduled(client, admin, queued_socketio):
    seen = {}
    queued_socketio.on_schedule = lambda job_id, *rest: seen.update(
        admin_routes._broadcast_job(job_id) or {})
    _, headers = admin

    job = _post(client, headers).get_json()

    assert seen["jobId"] == job["jobId"]
    assert seen["status"] == "queued"


def test_the_task_is_handed_the_job_id_title_body_and_payload(client, admin, queued_socketio):
    _, headers = admin

    job = _post_raw(client, headers, {"title": "  Tema  ", "body": "  Tekstas  ",
                                      "data": {"postId": "9"}}).get_json()

    target, args, kwargs = queued_socketio.tasks[0]
    assert target is admin_routes._run_broadcast
    assert args == (job["jobId"], "Tema", "Tekstas", {"postId": "9", "type": "admin_announcement"})
    assert kwargs == {}


def test_two_identical_broadcasts_are_two_separate_jobs(client, admin, queued_socketio, db):
    _, headers = admin

    first = _job_id(_post(client, headers))
    second = _job_id(_post(client, headers))

    assert first != second
    assert len(queued_socketio.tasks) == 2
    assert {row["target"] for row in _audit_rows(db)} == {first, second}


def test_a_broadcast_writes_one_audit_row_naming_the_admin(client, admin, queued_socketio, db):
    admin_user, headers = admin

    job_id = _job_id(_post_raw(client, headers, {"title": "Svarbu & greitai", "body": "B"}))

    rows = _audit_rows(db)
    assert len(rows) == 1
    assert rows[0]["actor_id"] == admin_user["id"]
    assert rows[0]["target"] == job_id
    # The trail keeps the admin's RAW text — escaping happens on
    # the way out of the API, never on the way into the database
    assert json.loads(rows[0]["payload"]) == {"title": "Svarbu & greitai"}


@pytest.mark.parametrize("payload", [
    {"title": "", "body": "B"},
    {"title": "T", "body": ""},
    {"title": "t" * 201, "body": "B"},
    {"title": "T", "body": "b" * 1001},
    {"title": "T", "body": "B", "data": "ne objektas"},
    {"title": 5, "body": "B"},
])
def test_a_refused_broadcast_leaves_no_job_no_audit_row_and_no_task(client, admin, queued_socketio,
                                                                     db, payload):
    _, headers = admin

    response = client.post(NOTIFICATIONS, headers=headers, json=payload)

    assert response.status_code == 400
    assert queued_socketio.tasks == []
    assert _audit_rows(db) == []
    assert admin_routes._broadcast_jobs == {}


def test_a_broadcast_still_answers_when_the_audit_table_is_gone(client, admin, queued_socketio, db):
    # _write_audit swallows a missing table on purpose: the trail
    # must never be able to fail the action it records
    _, headers = admin
    db.execute("DROP TABLE admin_audit")
    db.commit()

    response = _post(client, headers)

    assert response.status_code == 202
    assert len(queued_socketio.tasks) == 1


def test_a_scheduling_failure_leaves_the_audited_job_queued(client, admin, app, monkeypatch, db):
    # Defensive path: start_background_task never raises in
    # production, but if it did the row is already committed and
    # the job must not be half-invented
    _, headers = admin
    app.config["PROPAGATE_EXCEPTIONS"] = False
    exploding = _InlineSocketIO(run=False)
    exploding.raises = RuntimeError("no eventlet hub")
    monkeypatch.setattr(admin_routes, "_get_socketio", lambda: exploding)

    response = _post(client, headers)

    assert response.status_code == 500
    assert response.get_json()["error"] == "Internal server error"
    assert len(_audit_rows(db)) == 1
    assert [job["status"] for job in admin_routes._broadcast_jobs.values()] == ["queued"]


def test_the_expo_payload_and_the_wire_body_differ_by_escaping(client, admin, inline_socketio,
                                                                fake_notify):
    # TESTPLAN rule 10: posted as raw bytes, so the markup is the
    # admin's own. Expo must get it verbatim while the JSON
    # provider escapes the copy the caller reads back
    _, headers = admin
    raw_title = 'Rytoj <b>nėra</b> "paskaitų" & kava'

    response = _post_raw(client, headers, {"title": raw_title, "body": "A & B"})

    assert fake_notify.calls[0]["title"] == raw_title
    assert fake_notify.calls[0]["body"] == "A & B"
    assert response.get_json()["title"] == ("Rytoj &lt;b&gt;nėra&lt;/b&gt; "
                                            "&quot;paskaitų&quot; &amp; kava")


def test_the_accepted_body_is_the_copy_taken_before_the_fan_out(client, admin, inline_socketio,
                                                                 fake_notify):
    _, headers = admin
    fake_notify.result = 9

    job = _post(client, headers).get_json()

    assert (job["status"], job["sent"], job["finishedAt"]) == ("queued", 0, None)
    assert admin_routes._broadcast_job(job["jobId"])["status"] == "done"


def test_one_broadcast_past_the_registry_cap_forgets_the_oldest_job(client, admin, queued_socketio,
                                                                     monkeypatch):
    # The cap, reached the way an admin reaches it — with the real
    # 50 this is 51 POSTs proving the same thing 51 times slower
    monkeypatch.setattr(admin_routes, "_BROADCAST_JOBS_MAX", 3)
    _, headers = admin

    ids = [_job_id(_post(client, headers)) for _ in range(4)]

    assert client.get(f"{NOTIFICATIONS}/{ids[0]}", headers=headers).status_code == 404
    assert client.get(f"{NOTIFICATIONS}/{ids[3]}", headers=headers).status_code == 200




# ===========================================================
# GET /api/admin/notifications/<job_id>
# ===========================================================

@pytest.mark.contract
def test_a_finished_job_reads_back_with_its_counts(client, admin, inline_socketio, fake_notify):
    _, headers = admin
    fake_notify.result = (11, 2)

    job_id = _job_id(_post(client, headers, title="Dėmesio"))
    response = client.get(f"{NOTIFICATIONS}/{job_id}", headers=headers)

    assert response.status_code == 200
    body = response.get_json()
    assert set(body) == JOB_FIELDS
    assert body["status"] == "done"
    assert (body["sent"], body["failed"]) == (11, 2)
    assert body["title"] == "Dėmesio"
    assert body["message"] == "Accepted by Expo for 11 device token(s) across 0 user(s)"
    assert body["finishedAt"] is not None
    assert "no-store" in response.headers.get("Cache-Control", "")


def test_a_queued_job_reads_back_unchanged(client, admin, queued_socketio):
    _, headers = admin

    job = _post(client, headers).get_json()

    assert client.get(f"{NOTIFICATIONS}/{job['jobId']}", headers=headers).get_json() == job


def test_a_failed_job_reads_back_as_failed(client, admin, inline_socketio, fake_notify):
    _, headers = admin
    fake_notify.result = RuntimeError("Expo unreachable")

    job_id = _job_id(_post(client, headers))

    body = client.get(f"{NOTIFICATIONS}/{job_id}", headers=headers).get_json()
    assert body["status"] == "failed"
    assert body["message"] == "Broadcast failed — see the server log"


def test_a_status_body_is_escaped_like_every_other_json_body(client, admin, queued_socketio):
    _, headers = admin
    job_id = _job_id(_post_raw(client, headers, {"title": "A & <b>B</b>", "body": "B"}))

    body = client.get(f"{NOTIFICATIONS}/{job_id}", headers=headers).get_json()

    assert body["title"] == "A &amp; &lt;b&gt;B&lt;/b&gt;"
    assert admin_routes._broadcast_job(job_id)["title"] == "A & <b>B</b>"


@pytest.mark.parametrize("job_id", ["nera-tokio", "00000000-0000-0000-0000-000000000000",
                                    "x" * 2000, "ą-ž", "%2E%2E", "null", "0"])
def test_an_unknown_job_id_is_a_404_with_the_house_message(client, admin, job_id):
    _, headers = admin

    response = client.get(f"{NOTIFICATIONS}/{job_id}", headers=headers)

    assert response.status_code == 404
    assert response.get_json()["error"] == "Broadcast job not found"


def test_a_job_id_with_a_slash_never_reaches_the_route(client, admin, queued_socketio):
    # <job_id> is a single path segment, so this is Flask's own 404
    _, headers = admin
    job_id = _job_id(_post(client, headers))

    response = client.get(f"{NOTIFICATIONS}/{job_id}/extra", headers=headers)

    assert response.status_code == 404
    assert response.get_json()["error"] == "Not found"


def test_a_trailing_slash_on_a_job_id_is_a_404(client, admin, queued_socketio):
    _, headers = admin
    job_id = _job_id(_post(client, headers))

    assert client.get(f"{NOTIFICATIONS}/{job_id}/", headers=headers).status_code == 404


def test_the_collection_path_has_no_get(client, admin):
    _, headers = admin

    response = client.get(NOTIFICATIONS, headers=headers)

    assert response.status_code == 405
    assert response.get_json()["error"] == "Method not allowed"


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_a_job_can_only_be_read(client, admin, queued_socketio, method):
    _, headers = admin
    job_id = _job_id(_post(client, headers))

    response = getattr(client, method)(f"{NOTIFICATIONS}/{job_id}", headers=headers)

    assert response.status_code == 405


def test_a_job_survives_being_read_twice(client, admin, inline_socketio, fake_notify):
    _, headers = admin
    fake_notify.result = 4

    job_id = _job_id(_post(client, headers))

    first = client.get(f"{NOTIFICATIONS}/{job_id}", headers=headers).get_json()
    second = client.get(f"{NOTIFICATIONS}/{job_id}", headers=headers).get_json()
    assert first == second


def test_reading_a_job_does_not_save_it_from_eviction(client, admin, queued_socketio, monkeypatch):
    monkeypatch.setattr(admin_routes, "_BROADCAST_JOBS_MAX", 2)
    _, headers = admin
    first = _job_id(_post(client, headers))

    client.get(f"{NOTIFICATIONS}/{first}", headers=headers)
    _post(client, headers)
    _post(client, headers)

    assert client.get(f"{NOTIFICATIONS}/{first}", headers=headers).status_code == 404




# ===========================================================
# require_role — the broadcast routes are admin-only
# ===========================================================

def test_an_anonymous_caller_cannot_broadcast(client, queued_socketio):
    response = client.post(NOTIFICATIONS, json={"title": "T", "body": "B"})

    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required"
    assert queued_socketio.tasks == []


@pytest.mark.parametrize("role", ["student", "teacher", "curator"])
def test_no_role_below_admin_can_broadcast(client, make_user, auth_headers, queued_socketio, role):
    headers = auth_headers(make_user(role=role))

    response = client.post(NOTIFICATIONS, headers=headers, json={"title": "T", "body": "B"})

    assert response.status_code == 403
    assert response.get_json()["error"] == "Insufficient permissions"
    assert admin_routes._broadcast_jobs == {}


@pytest.mark.parametrize("role", ["student", "teacher", "curator"])
def test_an_existing_job_is_not_readable_below_admin(client, admin, make_user, auth_headers,
                                                      queued_socketio, role):
    # 403, never the 404 an unknown id gets — the gate answers
    # before the registry is consulted, so a job id leaks nothing
    _, admin_headers = admin
    job_id = _job_id(_post(client, admin_headers))
    headers = auth_headers(make_user(role=role))

    response = client.get(f"{NOTIFICATIONS}/{job_id}", headers=headers)

    assert response.status_code == 403
    assert response.get_json()["error"] == "Insufficient permissions"


def test_an_anonymous_caller_cannot_read_a_job(client, admin, queued_socketio):
    _, headers = admin
    job_id = _job_id(_post(client, headers))

    assert client.get(f"{NOTIFICATIONS}/{job_id}").status_code == 401


def test_a_job_is_not_owned_by_the_admin_who_started_it(client, admin, make_user, auth_headers,
                                                         queued_socketio):
    # The registry is process-wide, not per-actor: any admin may
    # follow any broadcast
    _, headers = admin
    job_id = _job_id(_post(client, headers))
    other_headers = auth_headers(make_user(role="admin"))

    response = client.get(f"{NOTIFICATIONS}/{job_id}", headers=other_headers)

    assert response.status_code == 200
    assert response.get_json()["jobId"] == job_id


def test_a_deactivated_admin_can_no_longer_broadcast(client, admin, make_user, auth_headers,
                                                      queued_socketio):
    _, headers = admin
    other = make_user(role="admin")
    other_headers = auth_headers(other)
    client.patch(f"/api/admin/users/{other['id']}", headers=headers, json={"active": False})

    response = client.post(NOTIFICATIONS, headers=other_headers, json={"title": "T", "body": "B"})

    assert response.status_code == 401
    assert queued_socketio.tasks == []


def test_an_admin_demoted_mid_session_can_no_longer_broadcast(client, admin, make_user,
                                                               auth_headers, queued_socketio):
    _, headers = admin
    other = make_user(role="admin")
    other_headers = auth_headers(other)
    assert client.post(NOTIFICATIONS, headers=other_headers,
                       json={"title": "T", "body": "B"}).status_code == 202

    client.patch(f"/api/admin/users/{other['id']}", headers=headers, json={"role": "student"})

    assert client.post(NOTIFICATIONS, headers=other_headers,
                       json={"title": "T", "body": "B"}).status_code == 403


# The uuid is a FIXED literal, never uuid4(): a value generated
# at collection time gives every xdist worker a different test
# id, and the run aborts with "different tests were collected"
@pytest.mark.parametrize("header", ["Bearer nesamone", "Bearer ", "Basic abc", "nesamone",
                                    "bearer 9c1e7a02-4b6d-4c3e-9f18-0a5b6c7d8e9f"])
def test_a_broken_authorization_header_cannot_broadcast(client, queued_socketio, header):
    response = client.post(NOTIFICATIONS, headers={"Authorization": header},
                           json={"title": "T", "body": "B"})

    assert response.status_code == 401
    assert queued_socketio.tasks == []


def test_the_gate_runs_before_the_body_is_looked_at(client, actor, queued_socketio):
    # A student with a body that would also fail validation gets
    # the 403, not a 400 that would confirm the body was read
    _, headers = actor

    response = client.post(NOTIFICATIONS, headers=headers, json={"title": 5})

    assert response.status_code == 403




# ===========================================================
# The whole broadcast, end to end
# ===========================================================

def test_a_broadcast_reaches_every_registered_device_once(client, admin, db, make_user,
                                                           inline_socketio, monkeypatch):
    # The REAL notify_channel over three real push_tokens rows,
    # with only the Expo HTTP call replaced — the fan-out picks
    # the tokens, the job records what came back
    from app.notifications import push as push_module

    _, headers = admin
    # The users first, on their own connections: an uncommitted
    # INSERT here would hold the write lock make_user needs
    users = [make_user() for _ in range(3)]
    for user in users:
        db.execute("INSERT INTO push_tokens (id, user_id, token, platform) VALUES (?, ?, ?, ?)",
                   (str(uuid.uuid4()), user["id"], f"ExponentPushToken[{uuid.uuid4().hex[:22]}]", "ios"))
    db.commit()

    batches = []

    def _send(tokens, title, body, data=None, **kwargs):
        batches.append(list(tokens))
        return len(tokens)

    monkeypatch.setattr(push_module, "send_push_batch", _send)

    job_id = _job_id(_post_raw(client, headers, {"title": "Dėmesio", "body": "Rytoj nevyks paskaitos"}))

    status = client.get(f"{NOTIFICATIONS}/{job_id}", headers=headers).get_json()
    assert status["status"] == "done"
    assert status["sent"] == 3
    assert sum(len(batch) for batch in batches) == 3


def test_the_job_timeline_runs_queued_then_running_then_done(client, admin, inline_socketio,
                                                              fake_notify):
    _, headers = admin
    timeline = []
    scheduled = []

    def _at_schedule(job_id, *rest):
        scheduled.append(job_id)
        timeline.append(admin_routes._broadcast_job(job_id)["status"])

    inline_socketio.on_schedule = _at_schedule
    fake_notify.during = lambda: timeline.append(admin_routes._broadcast_job(scheduled[0])["status"])

    job = _post(client, headers).get_json()
    timeline.append(admin_routes._broadcast_job(job["jobId"])["status"])

    assert job["status"] == "queued"
    assert timeline == ["queued", "running", "done"]


def test_the_finished_stamp_lands_after_the_created_stamp(client, admin, queued_socketio,
                                                           fake_notify):
    _, headers = admin

    with time_machine.travel("2026-06-01T09:00:00+00:00", tick=False):
        job = _post(client, headers).get_json()

    target, args, _ = queued_socketio.tasks[0]
    with time_machine.travel("2026-06-01T09:04:00+00:00", tick=False):
        target(*args)

    finished = admin_routes._broadcast_job(job["jobId"])["finishedAt"]
    assert datetime.fromisoformat(finished) > datetime.fromisoformat(job["createdAt"])
    assert datetime.fromisoformat(finished) == datetime(2026, 6, 1, 9, 4, tzinfo=timezone.utc)
