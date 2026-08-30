# -----------------------------------------------------------
#  [*] Tests — push.py's send path, exhaustively
#
#  The six functions that stand between this backend and
#  Expo's push service, driven through every arm they have:
#
#    _build_session          — the shared transport
#    _sanitize               — the log-safety filter
#    _pace_slice             — the process-wide rate gate
#    send_push_notification  — one message, one ticket
#    _send_slice             — one POST of at most 100
#    send_push_batch         — the sliced fan-out
#
#  Line and branch coverage of this module were already whole
#  when this file was written, so nothing here is aimed at an
#  unexecuted line. What it aims at is everything the line
#  count cannot see:
#
#    - BOUNDARIES: 0, 1, the cap, one past the cap, empty,
#      None, the wrong type, and something far too big — the
#      slice cap of 100, the fan-out worker cap of 3, the
#      pace gate's exact interval, _sanitize's limit at 0, 1,
#      exactly-the-limit and negative
#    - EXACTNESS: only status 200 is a success, only the
#      literal "ok"/"error"/"DeviceNotRegistered" spellings
#      are the verdicts they look like, only a truthy
#      "errors" array is a rejection
#    - CONTAINMENT: a malformed ticket is charged to itself
#      and the rest of the slice still lands; a slice that
#      fails costs only its own hundred; nothing raises into
#      the caller and nothing reaches the database that has
#      no business there
#    - HYGIENE: no raw token in a log line, and no upstream
#      string able to forge one
#
#  No packet leaves the process (every exp.host call goes
#  through `responses`, and the container has no network) and
#  no test waits on a real clock (`time_machine` for the
#  deadline, a stub clock for the pace gate).
# -----------------------------------------------------------

import json
import logging
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone

import pytest
import requests
import responses
import time_machine
from requests.adapters import HTTPAdapter

from app.notifications import push


LOG_NAME = "app.notifications.push"

# Where the clock-exact tests travel. Under tick=False
# time.monotonic() stops dead, so a deadline captured here is
# bit-for-bit the value the module reads back
FROZEN = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)




# -----------------------------------------------------------
# _isolated_module
# -----------------------------------------------------------
#
# push.py keeps two pieces of process-wide state — the pace
# gate's last-slice stamp and the in-memory receipt queue.
# Both are wiped around every test here, and the pace interval
# is switched off unless a test asks for it.
#
# The wipe matters: a test that travels in time or stubs the
# clock leaves a stamp behind, and the next slice — here or in
# another agent's file — would block under the pace lock for
# as long as that stamp is in the future.
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_module(monkeypatch):
    _wipe_module_state()
    monkeypatch.setattr(push, "_SLICE_INTERVAL", 0.0)
    yield
    _wipe_module_state()


def _wipe_module_state():
    push._last_slice_at = 0.0
    with push._receipt_lock:
        push._receipt_queue.clear()




# -----------------------------------------------------------
# Tokens and Expo fakes
# -----------------------------------------------------------
#
# _tok pads the opaque part so the strings also satisfy the
# intake grammar notifications/routes.py enforces — a
# realistic token is what makes the redaction assertions mean
# something.
#
# _accepts_all answers whatever it is handed with a matching
# row of "ok" tickets, so one test can send a single message
# and the next can send three hundred without arranging a body
# per slice.
# -----------------------------------------------------------

def _tok(name):
    return f"ExponentPushToken[{name}-aaaaaaaaaa]"


def _accepts_all():

    def _reply(request):
        payload = json.loads(request.body)
        if isinstance(payload, list):
            data = [{"status": "ok", "id": f"ticket:{msg.get('to')}"} for msg in payload]
        else:
            data = {"status": "ok", "id": f"ticket:{payload.get('to')}"}
        return 200, {}, json.dumps({"data": data})

    responses.add_callback(responses.POST, push.EXPO_PUSH_URL,
                           callback=_reply, content_type="application/json")


def _replies(body=None, status=200, text=None):
    if text is not None:
        responses.add(responses.POST, push.EXPO_PUSH_URL, body=text, status=status,
                      content_type="text/html")
    else:
        responses.add(responses.POST, push.EXPO_PUSH_URL, json=body, status=status)


def _raises(exc):
    responses.add(responses.POST, push.EXPO_PUSH_URL, body=exc)


def _bodies():
    return [json.loads(call.request.body) for call in responses.calls
            if call.request.url == push.EXPO_PUSH_URL]


def _messages():
    out = []
    for body in _bodies():
        out.extend(body if isinstance(body, list) else [body])
    return out


def _live():
    return time.monotonic() + 60


def _lapsed():
    return time.monotonic() - 1


def _seed_token(db, user_id, token, language="lt", active=1):
    db.execute(
        "INSERT INTO push_tokens (id, user_id, token, platform, language, active)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), user_id, token, "ios", language, active),
    )
    db.commit()
    return token


def _row(db, token):
    return db.execute("SELECT * FROM push_tokens WHERE token = ?", (token,)).fetchone()


def _boom(*args, **kwargs):
    raise sqlite3.OperationalError("unable to open database file")




# -----------------------------------------------------------
# no_backoff
# -----------------------------------------------------------
#
# `responses` drives the mounted adapter's real Retry policy,
# which is what makes a retry test mean anything — but the
# shipped backoff waits 0 s, then 2 s, then 4 s. This swaps in
# the same policy with the waiting removed, so the retry COUNT
# under test is still the shipped one.
#
# Used by:
#   - the retry tests; every other test answers with a status
#     outside the forcelist, which is never retried
# -----------------------------------------------------------

@pytest.fixture
def no_backoff(monkeypatch):
    adapter = push._SESSION.get_adapter("https://exp.host")
    monkeypatch.setattr(adapter, "max_retries", adapter.max_retries.new(backoff_factor=0))




# -----------------------------------------------------------
# clock
# -----------------------------------------------------------
#
# A stub monotonic clock plus a stub sleep, for the pace gate
# alone. Real seconds are never spent: sleeping just advances
# the stub, which is exactly what the gate's own arithmetic
# assumes. Every value used with it is a whole or half second
# so the float subtraction inside _pace_slice is exact and the
# boundary tests cannot drift.
#
# Used by:
#   - the _pace_slice tests (which make no HTTP call, so
#     nothing else in the process reads this clock)
# -----------------------------------------------------------

class _Clock:

    def __init__(self, now=1000.0):
        self.now = now
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    stub = _Clock()
    monkeypatch.setattr(push.time, "monotonic", stub.monotonic)
    monkeypatch.setattr(push.time, "sleep", stub.sleep)
    return stub




# -----------------------------------------------------------
# Fan-out stubs
# -----------------------------------------------------------
#
# _pool_recorder stands in for ThreadPoolExecutor: it records
# the max_workers the fan-out asked for and then maps the
# slices SERIALLY, so a multi-slice test is deterministic and
# `responses` answers in registration order.
#
# _forbidden_pool is its opposite — proof that a lone slice
# never builds a pool for one HTTP call.
#
# _slice_recorder replaces _send_slice itself, for the tests
# about what the fan-out hands each slice (the batch it got,
# the deadline it shares).
# -----------------------------------------------------------

def _pool_recorder(record):

    class _Pool:

        def __init__(self, max_workers=None):
            record.append(max_workers)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def map(self, fn, items):
            return [fn(item) for item in items]

    return _Pool


def _forbidden_pool(*args, **kwargs):
    raise AssertionError("a single slice must not build a thread pool")


def _slice_recorder(record, verdict=None):

    def _fake(part, deadline):
        record.append({"batch": list(part), "deadline": deadline})
        return verdict(part) if verdict else (len(part), [], {})

    return _fake








# -----------------------------------------------------------
# _build_session — the one transport every Expo call shares
# -----------------------------------------------------------

def test_building_a_session_hands_back_a_new_one_every_time():
    first = push._build_session()
    second = push._build_session()

    assert first is not second
    assert first is not push._SESSION


def test_a_built_session_is_a_requests_session_with_a_mounted_http_adapter():
    session = push._build_session()

    assert isinstance(session, requests.Session)
    assert isinstance(session.get_adapter("https://exp.host"), HTTPAdapter)


def test_only_the_https_scheme_gets_the_retrying_adapter():
    session = push._build_session()

    assert session.get_adapter("https://exp.host").max_retries.total == 3
    # Plain http keeps the stock adapter requests mounts, which
    # retries nothing — Expo is https-only, so that is fine
    assert session.get_adapter("http://exp.host").max_retries.total == 0


def test_the_retry_policy_is_three_attempts_with_a_growing_backoff():
    retry = push._build_session().get_adapter("https://exp.host").max_retries

    assert retry.total == 3
    assert retry.backoff_factor == 1


def test_only_the_rate_limit_and_the_server_errors_are_retried():
    retry = push._build_session().get_adapter("https://exp.host").max_retries

    assert set(retry.status_forcelist) == {429, 500, 502, 503, 504}
    for wasted in (400, 401, 403, 404, 418, 451):
        assert wasted not in retry.status_forcelist


def test_post_is_listed_because_urllib3_retries_no_other_method_here():
    retry = push._build_session().get_adapter("https://exp.host").max_retries

    assert set(retry.allowed_methods) == {"POST"}
    assert "GET" not in retry.allowed_methods


def test_a_retry_after_header_is_parsed_rather_than_ignored():
    retry = push._build_session().get_adapter("https://exp.host").max_retries

    assert retry.respect_retry_after_header is True
    assert retry.parse_retry_after("7") == 7


def test_the_module_session_carries_exactly_the_policy_this_factory_builds():
    shipped = push._SESSION.get_adapter("https://exp.host").max_retries
    fresh = push._build_session().get_adapter("https://exp.host").max_retries

    assert (shipped.total, shipped.backoff_factor) == (fresh.total, fresh.backoff_factor)
    assert set(shipped.status_forcelist) == set(fresh.status_forcelist)
    assert set(shipped.allowed_methods) == set(fresh.allowed_methods)


def test_a_built_session_carries_no_expo_credentials():
    # The Expo project keeps enhanced push security OFF; a
    # credential here would be rejected upstream
    session = push._build_session()

    assert session.auth is None
    assert "Authorization" not in session.headers


@responses.activate
def test_a_built_session_retries_a_rate_limited_post_until_the_total_runs_out():
    session = push._build_session()
    adapter = session.get_adapter("https://exp.host")
    adapter.max_retries = adapter.max_retries.new(backoff_factor=0)
    _replies({}, status=429)

    with pytest.raises(requests.exceptions.RetryError):
        session.post(push.EXPO_PUSH_URL, json={}, timeout=1)

    # The first attempt plus the three the policy allows
    assert len(responses.calls) == 4


@responses.activate
def test_a_built_session_does_not_retry_a_status_outside_the_forcelist():
    session = push._build_session()
    adapter = session.get_adapter("https://exp.host")
    adapter.max_retries = adapter.max_retries.new(backoff_factor=0)
    _replies(text="nope", status=400)

    assert session.post(push.EXPO_PUSH_URL, json={}, timeout=1).status_code == 400
    assert len(responses.calls) == 1


@responses.activate
def test_a_built_session_passes_a_two_hundred_straight_through():
    session = push._build_session()
    _replies({"data": []})

    assert session.post(push.EXPO_PUSH_URL, json={}, timeout=1).json() == {"data": []}
    assert len(responses.calls) == 1








# -----------------------------------------------------------
# _sanitize — nothing upstream reaches a log line unfiltered
# -----------------------------------------------------------

def test_sanitizing_folds_a_newline_so_upstream_text_cannot_forge_a_log_line():
    out = push._sanitize("accepted\nERROR everything is on fire")

    assert "\n" not in out
    assert out == "accepted ERROR everything is on fire"


def test_sanitizing_folds_a_carriage_return_and_a_crlf_pair_separately():
    assert push._sanitize("a\rb") == "a b"
    # Each character is replaced on its own, so CRLF becomes two
    # spaces rather than one
    assert push._sanitize("a\r\nb") == "a  b"


def test_sanitizing_leaves_a_tab_alone_because_a_tab_starts_no_log_line():
    assert push._sanitize("a\tb") == "a\tb"


def test_sanitizing_redacts_the_token_expo_quotes_back_at_us():
    token = _tok("quoted")

    out = push._sanitize(f'"{token}" is not a registered device')

    assert token not in out
    assert f"token:{push.token_digest(token)}" in out


def test_sanitizing_redacts_the_short_expo_spelling_too():
    out = push._sanitize("ExpoPushToken[shortform-aaaa]")

    assert "shortform" not in out
    assert out.startswith("token:")


def test_sanitizing_redacts_a_token_with_nothing_between_the_brackets():
    out = push._sanitize("ExponentPushToken[]")

    assert out == f"token:{push.token_digest('ExponentPushToken[]')}"


def test_the_redaction_is_case_sensitive_so_a_lookalike_is_left_as_it_came():
    # Not a token Expo ever emits — pinned so a future pattern
    # change is a deliberate one
    assert push._sanitize("exponentpushtoken[abc]") == "exponentpushtoken[abc]"


def test_an_unterminated_bracket_is_not_a_token_and_is_left_alone():
    assert push._sanitize("ExponentPushToken[abc") == "ExponentPushToken[abc"


def test_a_bracket_broken_by_a_newline_is_not_a_token_and_only_the_newline_folds():
    # The character class excludes CR/LF on purpose: a real Expo
    # token can never span two lines
    assert push._sanitize("ExponentPushToken[ab\ncd]") == "ExponentPushToken[ab cd]"


def test_every_token_in_the_string_is_redacted_not_just_the_first():
    one, two = _tok("first"), _tok("second")

    out = push._sanitize(f"{one} and {two}")

    assert one not in out and two not in out
    assert out == f"token:{push.token_digest(one)} and token:{push.token_digest(two)}"


def test_the_same_device_redacts_to_the_same_digest_and_two_devices_do_not():
    one, two = _tok("same"), _tok("other")

    assert push._sanitize(f"{one} {one}") == f"token:{push.token_digest(one)} " \
                                             f"token:{push.token_digest(one)}"
    assert push.token_digest(one) != push.token_digest(two)


def test_a_redacted_token_is_fourteen_characters_of_digest_and_nothing_else():
    out = push._sanitize(_tok("exact"))

    assert len(out) == len("token:") + 8
    assert out[6:].isalnum()


def test_the_redaction_happens_before_the_cut_so_a_tail_token_cannot_half_leak():
    token = _tok("tail")

    out = push._sanitize("x" * 190 + token)

    assert token not in out
    assert "token:" in out
    assert len(out) == 200


def test_the_default_limit_is_two_hundred_characters():
    assert len(push._sanitize("x" * 500)) == 200


def test_text_shorter_than_the_limit_comes_back_whole():
    assert push._sanitize("x" * 199) == "x" * 199


def test_text_exactly_at_the_limit_comes_back_whole():
    assert push._sanitize("x" * 200) == "x" * 200


def test_one_character_past_the_limit_is_cut_to_the_limit():
    assert push._sanitize("x" * 201) == "x" * 200


def test_a_limit_of_one_keeps_a_single_character():
    assert push._sanitize("abcdef", 1) == "a"


def test_a_limit_of_zero_keeps_nothing():
    assert push._sanitize("abcdef", 0) == ""


def test_a_negative_limit_drops_from_the_end():
    # Plain slicing, pinned so nobody "fixes" it into a crash
    assert push._sanitize("abcdef", -2) == "abcd"


def test_a_limit_larger_than_the_text_is_harmless():
    assert push._sanitize("abc", 10_000) == "abc"


def test_an_enormous_upstream_body_is_cut_down_to_the_limit():
    out = push._sanitize("y" * 100_000, 500)

    assert len(out) == 500


def test_an_empty_string_sanitizes_to_an_empty_string():
    assert push._sanitize("") == ""


@pytest.mark.parametrize("value, expected", [
    (None, "None"),
    (0, "0"),
    (12345, "12345"),
    (True, "True"),
    (3.5, "3.5"),
    ([], "[]"),
    ({}, "{}"),
])
def test_anything_that_is_not_a_string_is_stringified_first(value, expected):
    assert push._sanitize(value) == expected


def test_the_error_array_expo_sends_is_stringified_and_still_redacted():
    # The shape _send_slice hands it: payload["errors"], a list
    # of dicts that can quote the offending token
    token = _tok("inlist")

    out = push._sanitize([{"code": "PUSH_TOO_MANY_NOTIFICATIONS", "message": token}])

    assert token not in out
    assert "PUSH_TOO_MANY_NOTIFICATIONS" in out
    assert f"token:{push.token_digest(token)}" in out


def test_a_token_inside_bytes_is_redacted_after_the_stringification():
    out = push._sanitize(b"ExponentPushToken[inbytes-aaaa]")

    assert "inbytes" not in out
    assert "token:" in out


def test_sanitizing_always_returns_a_string():
    for value in (None, 1, [1], {"a": 1}, "text"):
        assert isinstance(push._sanitize(value), str)


def test_ordinary_upstream_text_survives_untouched():
    assert push._sanitize("Expo push service unavailable") == "Expo push service unavailable"








# -----------------------------------------------------------
# _pace_slice — the process-wide gate under Expo's ceiling
# -----------------------------------------------------------

def test_the_first_slice_of_a_quiet_process_waits_for_nothing(clock, monkeypatch):
    monkeypatch.setattr(push, "_SLICE_INTERVAL", 1.0)
    push._last_slice_at = 0.0

    push._pace_slice()

    assert clock.slept == []


def test_a_slice_that_follows_at_once_waits_out_the_whole_interval(clock, monkeypatch):
    monkeypatch.setattr(push, "_SLICE_INTERVAL", 1.0)
    push._last_slice_at = 1000.0

    push._pace_slice()

    assert clock.slept == [1.0]


def test_a_slice_half_an_interval_later_waits_out_only_the_remainder(clock, monkeypatch):
    monkeypatch.setattr(push, "_SLICE_INTERVAL", 1.0)
    push._last_slice_at = 999.5

    push._pace_slice()

    assert clock.slept == [0.5]


def test_exactly_one_interval_later_is_late_enough_to_skip_the_wait(clock, monkeypatch):
    # The boundary of `if wait > 0` — zero is not a wait
    monkeypatch.setattr(push, "_SLICE_INTERVAL", 1.0)
    push._last_slice_at = 999.0

    push._pace_slice()

    assert clock.slept == []


def test_long_past_the_interval_nothing_waits_at_all(clock, monkeypatch):
    monkeypatch.setattr(push, "_SLICE_INTERVAL", 1.0)
    push._last_slice_at = 900.0

    push._pace_slice()

    assert clock.slept == []


def test_an_interval_of_zero_never_waits(clock, monkeypatch):
    monkeypatch.setattr(push, "_SLICE_INTERVAL", 0.0)
    push._last_slice_at = 1000.0

    push._pace_slice()

    assert clock.slept == []


def test_the_stamp_is_taken_after_the_wait_so_waiters_do_not_all_wait_in_full(clock, monkeypatch):
    monkeypatch.setattr(push, "_SLICE_INTERVAL", 1.0)
    push._last_slice_at = 1000.0

    push._pace_slice()

    # 1000 + the 1 s just slept — a second waiter measures from
    # here, not from where the first one started
    assert push._last_slice_at == 1001.0


def test_a_slice_that_did_not_wait_still_stamps_the_moment_it_left(clock, monkeypatch):
    monkeypatch.setattr(push, "_SLICE_INTERVAL", 1.0)
    push._last_slice_at = 900.0

    push._pace_slice()

    assert push._last_slice_at == 1000.0


def test_a_stamp_left_in_the_future_makes_the_gate_wait_out_the_whole_gap(clock, monkeypatch):
    # Why every test file resets _last_slice_at: a time-travelled
    # test that leaves the stamp ahead parks the next slice for
    # the difference
    monkeypatch.setattr(push, "_SLICE_INTERVAL", 1.0)
    push._last_slice_at = 1100.0

    push._pace_slice()

    assert clock.slept == [101.0]


def test_pacing_reports_nothing_back_to_its_caller(clock, monkeypatch):
    monkeypatch.setattr(push, "_SLICE_INTERVAL", 0.0)

    assert push._pace_slice() is None


def test_the_wait_happens_while_the_gate_is_held_so_the_pool_serialises(monkeypatch):
    held = []
    monkeypatch.setattr(push, "_SLICE_INTERVAL", 5.0)
    monkeypatch.setattr(push.time, "sleep", lambda s: held.append(push._pace_lock.locked()))
    push._last_slice_at = time.monotonic()

    push._pace_slice()

    assert held == [True]


def test_the_gate_is_released_once_the_slice_is_through():
    push._pace_slice()

    assert push._pace_lock.locked() is False


def test_the_gate_is_one_process_wide_lock():
    assert isinstance(push._pace_lock, type(threading.Lock()))








# -----------------------------------------------------------
# send_push_notification — one message, one ticket, one bool
# -----------------------------------------------------------

@responses.activate
def test_a_lone_send_carries_exactly_the_five_fields_expo_needs():
    _accepts_all()

    assert push.send_push_notification(_tok("bare"), "Sveiki", "Naujiena") is True

    message = _messages()[0]
    assert set(message) == {"to", "title", "body", "sound", "channelId"}
    assert message["sound"] == "default"
    assert message["channelId"] == "default"


@responses.activate
def test_a_lone_send_posts_one_object_not_an_array():
    _accepts_all()
    push.send_push_notification(_tok("object"), "T", "B")

    assert isinstance(_bodies()[0], dict)


@responses.activate
def test_lithuanian_text_reaches_expo_unescaped():
    # Nothing on this path runs the app's html-escaping JSON
    # provider — the phone must show the characters as typed
    _accepts_all()
    push.send_push_notification(_tok("lt"), "Sveiki, čia KNF", "Ačiū & <3")

    message = _messages()[0]
    assert message["title"] == "Sveiki, čia KNF"
    assert message["body"] == "Ačiū & <3"


@responses.activate
@pytest.mark.parametrize("data", [None, {}, 0, "", []])
def test_a_falsy_data_object_is_never_stamped_on_the_message(data):
    _accepts_all()
    push.send_push_notification(_tok("nodata"), "T", "B", data=data)

    assert "data" not in _messages()[0]


@responses.activate
def test_a_data_object_that_is_not_a_dict_still_rides_as_it_came():
    # The guard is truthiness, not a type check
    _accepts_all()
    push.send_push_notification(_tok("listdata"), "T", "B", data=["news"])

    assert _messages()[0]["data"] == ["news"]


@responses.activate
def test_a_deeply_nested_data_object_survives_the_round_trip():
    _accepts_all()
    payload = {"type": "chat_message", "conversationId": "c-1",
               "meta": {"unread": [1, 2, 3], "nested": {"deep": True}}}

    push.send_push_notification(_tok("nested"), "T", "B", data=payload)

    assert _messages()[0]["data"] == payload


@responses.activate
@pytest.mark.parametrize("badge", [0, 1, -1, 999_999])
def test_any_badge_count_including_zero_rides_the_message(badge):
    _accepts_all()
    push.send_push_notification(_tok("badge"), "T", "B", badge=badge)

    assert _messages()[0]["badge"] == badge


@responses.activate
def test_a_badge_of_false_rides_because_only_none_is_omitted():
    # `badge is not None`, not truthiness
    _accepts_all()
    push.send_push_notification(_tok("falsebadge"), "T", "B", badge=False)

    assert _messages()[0]["badge"] is False


@responses.activate
def test_a_missing_badge_leaves_the_key_off_entirely():
    _accepts_all()
    push.send_push_notification(_tok("nobadge"), "T", "B", badge=None)

    assert "badge" not in _messages()[0]


@responses.activate
def test_an_empty_token_is_still_posted_because_filtering_is_the_callers_job():
    _accepts_all()

    assert push.send_push_notification("", "T", "B") is True
    assert _messages()[0]["to"] == ""


@responses.activate
def test_a_title_and_body_of_none_ride_as_nulls():
    _accepts_all()
    push.send_push_notification(_tok("nulls"), None, None)

    message = _messages()[0]
    assert message["title"] is None
    assert message["body"] is None


@responses.activate
@pytest.mark.parametrize("status", [201, 202, 400, 401, 403, 404, 418, 451])
def test_only_a_two_hundred_is_a_send(status, caplog):
    _replies(text="whatever", status=status)

    with caplog.at_level(logging.WARNING, logger=LOG_NAME):
        assert push.send_push_notification(_tok("status"), "T", "B") is False

    assert f"Expo push HTTP {status}" in caplog.text


@responses.activate
def test_a_no_content_answer_is_not_a_send(caplog):
    # 204 carries no body at all, so it can only be read as the
    # status it is
    _replies(text="", status=204)

    with caplog.at_level(logging.WARNING, logger=LOG_NAME):
        assert push.send_push_notification(_tok("nocontent"), "T", "B") is False

    assert "Expo push HTTP 204" in caplog.text


@responses.activate
def test_the_status_is_read_before_the_body_so_an_html_page_is_not_a_parse_failure(caplog):
    _replies(text="<html><h1>502 Bad Gateway</h1></html>", status=400)

    with caplog.at_level(logging.WARNING, logger=LOG_NAME):
        assert push.send_push_notification(_tok("htmlpage"), "T", "B") is False

    assert "Expo push HTTP 400" in caplog.text
    assert "non-JSON" not in caplog.text


@responses.activate
def test_an_upstream_error_page_quoting_a_token_is_redacted_in_the_log(caplog):
    token = _tok("leaky")
    _replies(text=f"invalid token {token}", status=400)

    with caplog.at_level(logging.WARNING, logger=LOG_NAME):
        push.send_push_notification(token, "T", "B")

    assert token not in caplog.text
    assert push.token_digest(token) in caplog.text


@responses.activate
def test_an_upstream_error_page_cannot_forge_a_second_log_line(caplog):
    _replies(text="bad\nCRITICAL everything is fine", status=400)

    with caplog.at_level(logging.WARNING, logger=LOG_NAME):
        push.send_push_notification(_tok("forge"), "T", "B")

    assert len(caplog.records) == 1
    assert "\n" not in caplog.records[0].getMessage()


@responses.activate
def test_a_two_hundred_with_an_empty_body_is_not_a_send(caplog):
    _replies(text="", status=200)

    with caplog.at_level(logging.WARNING, logger=LOG_NAME):
        assert push.send_push_notification(_tok("emptybody"), "T", "B") is False

    assert "non-JSON" in caplog.text


@responses.activate
@pytest.mark.parametrize("body", ["null", "42", '"a string"', "[1, 2, 3]"])
def test_a_two_hundred_whose_json_is_not_an_envelope_is_not_a_send(body, caplog):
    responses.add(responses.POST, push.EXPO_PUSH_URL, body=body, status=200,
                  content_type="application/json")

    with caplog.at_level(logging.WARNING, logger=LOG_NAME):
        assert push.send_push_notification(_tok("shape"), "T", "B") is False

    assert "unexpected body shape" in caplog.text


@responses.activate
@pytest.mark.parametrize("ticket", [None, "ok", ["ok"], 1, True])
def test_a_data_field_that_is_not_an_object_is_not_a_ticket(ticket):
    _replies({"data": ticket})

    assert push.send_push_notification(_tok("notdict"), "T", "B") is False


@responses.activate
def test_an_envelope_with_no_data_key_at_all_is_not_a_send():
    _replies({"errors": [{"code": "PUSH_TOO_MANY_EXPERIENCE_IDS"}]})

    assert push.send_push_notification(_tok("nodata"), "T", "B") is False


@responses.activate
def test_a_ticket_with_no_status_is_taken_as_accepted():
    # Expo always stamps one; this pins what happens if it ever
    # stops — an accepted ticket, parked for the receipt poll,
    # where the receipt is the real verdict anyway
    _replies({"data": {"id": "ticket-nostatus"}})

    assert push.send_push_notification(_tok("nostatus"), "T", "B") is True
    assert push._receipt_queue[0][0] == "ticket-nostatus"


@responses.activate
def test_the_error_verdict_is_the_exact_lowercase_spelling():
    # "Error" is not "error", so this counts as accepted — pinned
    # because a looser check would swallow real verdicts
    _replies({"data": {"status": "Error", "details": {"error": "MessageTooBig"}}})

    assert push.send_push_notification(_tok("case"), "T", "B") is True


@responses.activate
def test_an_accepted_ticket_is_parked_with_the_device_it_belongs_to():
    _accepts_all()
    token = _tok("park")

    push.send_push_notification(token, "T", "B")

    assert len(push._receipt_queue) == 1
    assert push._receipt_queue[0][:2] == (f"ticket:{token}", token)


@responses.activate
@pytest.mark.parametrize("ticket_id", [None, "", 0])
def test_an_accepted_ticket_without_a_usable_id_parks_nothing(ticket_id):
    _replies({"data": {"status": "ok", "id": ticket_id}})

    assert push.send_push_notification(_tok("noid"), "T", "B") is True
    assert len(push._receipt_queue) == 0


@responses.activate
@pytest.mark.parametrize("body", [
    {"data": {"status": "error", "details": {"error": "MessageTooBig"}, "id": "t-1"}},
    {"data": ["not a ticket"]},
    {"errors": [{"code": "x"}]},
])
def test_no_failure_path_parks_a_receipt(body):
    _replies(body)

    push.send_push_notification(_tok("noreceipt"), "T", "B")

    assert len(push._receipt_queue) == 0


@responses.activate
def test_a_device_not_registered_ticket_retires_that_device_and_no_other(app, db, make_user):
    user = make_user()
    gone = _seed_token(db, user["id"], _tok("gone"))
    alive = _seed_token(db, user["id"], _tok("alive"))
    _replies({"data": {"status": "error", "details": {"error": "DeviceNotRegistered"}}})

    assert push.send_push_notification(gone, "T", "B") is False

    assert _row(db, gone)["active"] == 0
    assert _row(db, alive)["active"] == 1


@responses.activate
def test_retiring_a_device_twice_is_harmless(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _tok("twice"))
    _replies({"data": {"status": "error", "details": {"error": "DeviceNotRegistered"}}})
    _replies({"data": {"status": "error", "details": {"error": "DeviceNotRegistered"}}})

    assert push.send_push_notification(token, "T", "B") is False
    assert push.send_push_notification(token, "T", "B") is False

    assert _row(db, token)["active"] == 0


@responses.activate
def test_a_dead_device_verdict_for_a_token_nobody_registered_changes_nothing(app, db):
    _replies({"data": {"status": "error", "details": {"error": "DeviceNotRegistered"}}})

    assert push.send_push_notification(_tok("stranger"), "T", "B") is False

    assert db.execute("SELECT COUNT(*) c FROM push_tokens").fetchone()["c"] == 0


@responses.activate
def test_the_dead_device_check_is_the_exact_spelling(app, db, make_user, caplog):
    user = make_user()
    token = _seed_token(db, user["id"], _tok("nearly"))
    _replies({"data": {"status": "error", "details": {"error": "devicenotregistered"}}})

    with caplog.at_level(logging.WARNING, logger=LOG_NAME):
        assert push.send_push_notification(token, "T", "B") is False

    assert _row(db, token)["active"] == 1
    assert "devicenotregistered" in caplog.text


@responses.activate
@pytest.mark.parametrize("details", [None, {}, "DeviceNotRegistered", ["DeviceNotRegistered"], 7])
def test_an_error_ticket_whose_details_are_unusable_names_no_error_type(details, caplog):
    _replies({"data": {"status": "error", "details": details}})

    with caplog.at_level(logging.WARNING, logger=LOG_NAME):
        assert push.send_push_notification(_tok("nodetail"), "T", "B") is False

    assert "Expo push error None" in caplog.text


@responses.activate
def test_an_error_ticket_message_that_is_not_a_string_is_still_logged():
    _replies({"data": {"status": "error", "details": {"error": "MessageTooBig"},
                       "message": {"nested": "object"}}})

    assert push.send_push_notification(_tok("weirdmsg"), "T", "B") is False


@responses.activate
def test_an_error_ticket_with_no_message_logs_an_empty_excerpt(caplog):
    _replies({"data": {"status": "error", "details": {"error": "InvalidCredentials"}}})

    with caplog.at_level(logging.WARNING, logger=LOG_NAME):
        push.send_push_notification(_tok("nomsg"), "T", "B")

    assert "InvalidCredentials" in caplog.text


@responses.activate
def test_an_error_ticket_quoting_the_token_never_logs_it_raw(caplog):
    token = _tok("quotedback")
    _replies({"data": {"status": "error", "details": {"error": "InvalidCredentials"},
                       "message": f"{token} is not valid"}})

    with caplog.at_level(logging.WARNING, logger=LOG_NAME):
        push.send_push_notification(token, "T", "B")

    assert token not in caplog.text
    assert push.token_digest(token) in caplog.text


@responses.activate
@pytest.mark.parametrize("exc", [
    requests.exceptions.ConnectionError("no route to exp.host"),
    requests.exceptions.ReadTimeout("expo took too long"),
    requests.exceptions.TooManyRedirects("in circles"),
    requests.exceptions.SSLError("bad certificate"),
])
def test_any_transport_failure_is_swallowed_and_reported_as_a_miss(exc, caplog):
    _raises(exc)

    with caplog.at_level(logging.ERROR, logger=LOG_NAME):
        assert push.send_push_notification(_tok("down"), "T", "B") is False

    assert "Failed to send push notification" in caplog.text
    assert caplog.records[-1].exc_info is not None


def test_a_session_that_raises_something_unexpected_is_swallowed_too(monkeypatch):

    class _Angry:

        def post(self, *args, **kwargs):
            raise RuntimeError("the session itself is broken")

    monkeypatch.setattr(push, "_SESSION", _Angry())

    assert push.send_push_notification(_tok("angry"), "T", "B") is False


@responses.activate
def test_a_server_error_that_clears_on_a_retry_is_still_a_send(no_backoff):
    _replies(text="<html>500</html>", status=500)
    _replies({"data": {"status": "ok", "id": "ticket-recovered"}})

    assert push.send_push_notification(_tok("recovers"), "T", "B") is True
    assert len(responses.calls) == 2


@responses.activate
def test_a_server_error_that_never_clears_gives_up_after_the_retries(no_backoff):
    _replies(text="<html>503</html>", status=503)

    assert push.send_push_notification(_tok("never"), "T", "B") is False
    assert len(responses.calls) == 4


@responses.activate
def test_a_ticket_error_other_than_a_dead_device_opens_no_database_connection(monkeypatch):
    monkeypatch.setattr(push, "get_db", _boom)
    _replies({"data": {"status": "error", "details": {"error": "MessageRateExceeded"}}})

    assert push.send_push_notification(_tok("nodb"), "T", "B") is False


@responses.activate
def test_a_dead_database_cannot_turn_a_dead_device_into_a_crash(monkeypatch, caplog):
    monkeypatch.setattr(push, "get_db", _boom)
    _replies({"data": {"status": "error", "details": {"error": "DeviceNotRegistered"}}})

    with caplog.at_level(logging.ERROR, logger=LOG_NAME):
        assert push.send_push_notification(_tok("dbgone"), "T", "B") is False

    assert "Failed to deactivate tokens" in caplog.text


@responses.activate
def test_a_lone_send_makes_exactly_one_request():
    _accepts_all()
    push.send_push_notification(_tok("once"), "T", "B")

    assert len(responses.calls) == 1








# -----------------------------------------------------------
# _send_slice — one POST of at most a hundred, no database
# -----------------------------------------------------------

@responses.activate
def test_a_deadline_that_is_exactly_now_abandons_the_slice(caplog):
    _accepts_all()

    with caplog.at_level(logging.WARNING, logger=LOG_NAME):
        with time_machine.travel(FROZEN, tick=False):
            verdict = push._send_slice([{"to": _tok("edge")}], time.monotonic())

    assert verdict == (0, [], {"Abandoned": 1})
    assert len(responses.calls) == 0
    assert "deadline reached" in caplog.text


@responses.activate
def test_a_deadline_a_hair_away_still_lets_the_slice_go():
    _accepts_all()

    with time_machine.travel(FROZEN, tick=False):
        sent, dead, errors = push._send_slice([{"to": _tok("hair")}], time.monotonic() + 0.001)

    assert (sent, dead, errors) == (1, [], {})
    assert len(responses.calls) == 1


@responses.activate
def test_an_abandoned_slice_charges_every_message_it_was_holding():
    batch = [{"to": _tok(f"ab{i}")} for i in range(37)]

    assert push._send_slice(batch, _lapsed()) == (0, [], {"Abandoned": 37})


def test_an_abandoned_empty_slice_charges_nothing_but_still_says_abandoned():
    assert push._send_slice([], _lapsed()) == (0, [], {"Abandoned": 0})


def test_an_abandoned_slice_does_not_even_reach_the_pace_gate(monkeypatch):
    paced = []
    monkeypatch.setattr(push, "_pace_slice", lambda: paced.append(True))

    push._send_slice([{"to": _tok("nopace")}], _lapsed())

    assert paced == []


@responses.activate
def test_a_live_slice_paces_itself_before_it_posts(monkeypatch):
    order = []
    monkeypatch.setattr(push, "_pace_slice", lambda: order.append("paced"))
    responses.add_callback(responses.POST, push.EXPO_PUSH_URL,
                           callback=lambda r: order.append("posted") or (200, {}, '{"data": []}'))

    push._send_slice([{"to": _tok("order")}], _live())

    assert order[:2] == ["paced", "posted"]


@responses.activate
def test_an_empty_slice_with_a_live_deadline_posts_an_empty_array():
    _replies({"data": []})

    assert push._send_slice([], _live()) == (0, [], {})
    assert _bodies() == [[]]


@responses.activate
@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 418, 451])
def test_a_non_two_hundred_costs_the_whole_slice_and_names_the_status(status, caplog):
    batch = [{"to": _tok(f"http{i}")} for i in range(3)]
    _replies(text="upstream said no", status=status)

    with caplog.at_level(logging.WARNING, logger=LOG_NAME):
        assert push._send_slice(batch, _live()) == (0, [], {f"HTTP {status}": 3})

    assert f"Expo batch push HTTP {status}" in caplog.text


@responses.activate
def test_a_non_two_hundred_body_quoting_a_token_is_redacted(caplog):
    token = _tok("httpleak")
    _replies(text=f"bad token {token}", status=400)

    with caplog.at_level(logging.WARNING, logger=LOG_NAME):
        push._send_slice([{"to": token}], _live())

    assert token not in caplog.text


@responses.activate
@pytest.mark.parametrize("exc", [
    requests.exceptions.ConnectionError("no route"),
    requests.exceptions.ReadTimeout("too slow"),
    requests.exceptions.ChunkedEncodingError("truncated"),
])
def test_a_request_that_never_completed_costs_the_whole_slice(exc):
    _raises(exc)
    batch = [{"to": _tok(f"fail{i}")} for i in range(5)]

    assert push._send_slice(batch, _live()) == (0, [], {"RequestFailed": 5})


@responses.activate
def test_a_two_hundred_that_is_not_json_is_a_request_failure_not_a_shape_failure():
    _replies(text="<html>hello</html>", status=200)

    assert push._send_slice([{"to": _tok("notjson")}], _live()) == (0, [], {"RequestFailed": 1})


@responses.activate
def test_a_rate_limit_that_survives_every_retry_is_a_request_failure(no_backoff):
    _replies({}, status=429)

    assert push._send_slice([{"to": _tok("ratelimited")}], _live()) == (0, [], {"RequestFailed": 1})
    assert len(responses.calls) == 4


@responses.activate
def test_a_top_level_errors_array_rejects_the_whole_slice(caplog):
    batch = [{"to": _tok(f"rej{i}")} for i in range(4)]
    _replies({"errors": [{"code": "PUSH_TOO_MANY_EXPERIENCE_IDS", "message": "mixed projects"}]})

    with caplog.at_level(logging.WARNING, logger=LOG_NAME):
        assert push._send_slice(batch, _live()) == (0, [], {"Rejected": 4})

    assert "rejected" in caplog.text
    assert "PUSH_TOO_MANY_EXPERIENCE_IDS" in caplog.text


@responses.activate
def test_a_rejection_quoting_a_token_never_logs_it_raw(caplog):
    token = _tok("rejleak")
    _replies({"errors": [{"code": "PUSH_TOO_MANY_NOTIFICATIONS", "message": token}]})

    with caplog.at_level(logging.WARNING, logger=LOG_NAME):
        push._send_slice([{"to": token}], _live())

    assert token not in caplog.text
    assert push.token_digest(token) in caplog.text


@responses.activate
@pytest.mark.parametrize("errors", [None, [], {}, "", 0, False])
def test_a_falsy_errors_field_is_no_rejection_at_all(errors):
    # The guard is truthiness: an empty array is Expo saying
    # "nothing went wrong", and the tickets still count
    _replies({"errors": errors, "data": [{"status": "ok", "id": "t-1"}]})

    assert push._send_slice([{"to": _tok("okanyway")}], _live())[0] == 1


@responses.activate
def test_a_rejection_wins_over_any_tickets_in_the_same_body():
    _replies({"errors": [{"code": "PUSH_TOO_MANY_NOTIFICATIONS"}],
              "data": [{"status": "ok", "id": "t-ignored"}]})

    assert push._send_slice([{"to": _tok("both")}], _live()) == (0, [], {"Rejected": 1})
    assert len(push._receipt_queue) == 0


@responses.activate
@pytest.mark.parametrize("body", [
    [{"status": "ok"}],
    "a bare string",
    42,
    None,
])
def test_a_body_that_is_not_an_envelope_is_malformed(body, caplog):
    responses.add(responses.POST, push.EXPO_PUSH_URL, body=json.dumps(body), status=200,
                  content_type="application/json")

    with caplog.at_level(logging.WARNING, logger=LOG_NAME):
        assert push._send_slice([{"to": _tok("env")}], _live()) == (0, [], {"Malformed": 1})

    assert "unexpected body shape" in caplog.text


@responses.activate
@pytest.mark.parametrize("results", [None, {}, "ok", 3, {"0": {"status": "ok"}}])
def test_a_data_field_that_is_not_a_list_is_malformed(results):
    batch = [{"to": _tok(f"nl{i}")} for i in range(2)]
    _replies({"data": results})

    assert push._send_slice(batch, _live()) == (0, [], {"Malformed": 2})


@responses.activate
def test_an_envelope_with_no_data_key_is_malformed():
    _replies({"status": "ok"})

    assert push._send_slice([{"to": _tok("nodata")}], _live()) == (0, [], {"Malformed": 1})


@responses.activate
def test_an_empty_ticket_list_for_a_full_slice_counts_nothing_and_charges_nothing():
    batch = [{"to": _tok(f"silent{i}")} for i in range(3)]
    _replies({"data": []})

    assert push._send_slice(batch, _live()) == (0, [], {})


@responses.activate
def test_fewer_tickets_than_messages_counts_only_what_came_back():
    batch = [{"to": _tok(f"short{i}")} for i in range(3)]
    _replies({"data": [{"status": "ok", "id": "t-0"}]})

    assert push._send_slice(batch, _live()) == (1, [], {})
    assert len(push._receipt_queue) == 1


@responses.activate
def test_more_tickets_than_messages_ignores_the_tail_and_says_so(caplog):
    batch = [{"to": _tok("one")}]
    _replies({"data": [{"status": "ok", "id": "t-0"},
                       {"status": "ok", "id": "t-extra"},
                       {"status": "ok", "id": "t-extra-2"}]})

    with caplog.at_level(logging.WARNING, logger=LOG_NAME):
        assert push._send_slice(batch, _live()) == (1, [], {})

    assert "ignoring the tail" in caplog.text
    assert len(push._receipt_queue) == 1


@responses.activate
def test_exactly_as_many_tickets_as_messages_counts_them_all():
    batch = [{"to": _tok(f"pair{i}")} for i in range(3)]
    _replies({"data": [{"status": "ok", "id": f"t-{i}"} for i in range(3)]})

    assert push._send_slice(batch, _live())[0] == 3


@responses.activate
@pytest.mark.parametrize("ticket", ["ok", None, 7, ["ok"], True])
def test_a_ticket_that_is_not_an_object_is_charged_as_malformed(ticket):
    _replies({"data": [ticket]})

    assert push._send_slice([{"to": _tok("badticket")}], _live()) == (0, [], {"Malformed": 1})


@responses.activate
def test_one_malformed_ticket_does_not_abandon_the_rest_of_the_slice():
    batch = [{"to": _tok("a")}, {"to": _tok("b")}, {"to": _tok("c")}]
    _replies({"data": ["nonsense",
                       {"status": "ok", "id": "t-b"},
                       {"status": "error", "details": {"error": "DeviceNotRegistered"}}]})

    sent, dead, errors = push._send_slice(batch, _live())

    assert sent == 1
    assert dead == [_tok("c")]
    assert errors == {"Malformed": 1, "DeviceNotRegistered": 1}


@responses.activate
def test_a_verdict_is_mapped_to_its_own_device_by_position():
    batch = [{"to": _tok("first")}, {"to": _tok("middle")}, {"to": _tok("last")}]
    _replies({"data": [{"status": "ok", "id": "t-first"},
                       {"status": "error", "details": {"error": "DeviceNotRegistered"}},
                       {"status": "ok", "id": "t-last"}]})

    sent, dead, errors = push._send_slice(batch, _live())

    assert (sent, dead) == (2, [_tok("middle")])
    assert [entry[1] for entry in push._receipt_queue] == [_tok("first"), _tok("last")]


@responses.activate
def test_two_dead_devices_in_one_slice_come_back_in_the_order_they_were_sent():
    batch = [{"to": _tok("d1")}, {"to": _tok("alive")}, {"to": _tok("d2")}]
    dead_ticket = {"status": "error", "details": {"error": "DeviceNotRegistered"}}
    _replies({"data": [dead_ticket, {"status": "ok", "id": "t"}, dead_ticket]})

    sent, dead, errors = push._send_slice(batch, _live())

    assert dead == [_tok("d1"), _tok("d2")]
    assert errors == {"DeviceNotRegistered": 2}


@responses.activate
def test_the_same_device_twice_in_one_slice_is_reported_twice():
    # The slice counts verdicts; the caller's single UPDATE is
    # what makes the retirement idempotent
    token = _tok("dup")
    dead_ticket = {"status": "error", "details": {"error": "DeviceNotRegistered"}}
    _replies({"data": [dead_ticket, dead_ticket]})

    sent, dead, errors = push._send_slice([{"to": token}, {"to": token}], _live())

    assert dead == [token, token]
    assert errors == {"DeviceNotRegistered": 2}


@responses.activate
@pytest.mark.parametrize("details", [None, {}, {"error": ""}, {"error": None}, "boom", [1], 0])
def test_an_error_ticket_with_no_usable_code_is_tallied_as_unknown(details):
    _replies({"data": [{"status": "error", "details": details}]})

    assert push._send_slice([{"to": _tok("unknown")}], _live()) == (0, [], {"Unknown": 1})


@responses.activate
def test_the_same_error_code_accumulates_across_the_slice():
    batch = [{"to": _tok(f"big{i}")} for i in range(3)]
    ticket = {"status": "error", "details": {"error": "MessageTooBig"}}
    _replies({"data": [ticket, ticket, ticket]})

    assert push._send_slice(batch, _live()) == (0, [], {"MessageTooBig": 3})


@responses.activate
def test_different_error_codes_are_tallied_apart(caplog):
    batch = [{"to": _tok(f"mix{i}")} for i in range(3)]
    _replies({"data": [{"status": "error", "details": {"error": "MessageTooBig"}},
                       {"status": "error", "details": {"error": "MessageRateExceeded"}},
                       {"status": "error", "details": {"error": "MessageTooBig"}}]})

    with caplog.at_level(logging.WARNING, logger=LOG_NAME):
        sent, dead, errors = push._send_slice(batch, _live())

    assert errors == {"MessageTooBig": 2, "MessageRateExceeded": 1}
    assert dead == []


@responses.activate
def test_an_accepted_ticket_without_an_id_counts_but_parks_nothing():
    _replies({"data": [{"status": "ok"}, {"status": "ok", "id": "t-1"}]})

    sent, dead, errors = push._send_slice([{"to": _tok("noid")}, {"to": _tok("withid")}], _live())

    assert sent == 2
    assert [entry[0] for entry in push._receipt_queue] == ["t-1"]


@responses.activate
def test_the_ok_verdict_is_the_exact_lowercase_spelling():
    # "OK" is not "ok": it falls through to the error tally as an
    # unnamed code rather than being counted as a send
    _replies({"data": [{"status": "OK", "id": "t-1"}]})

    assert push._send_slice([{"to": _tok("caps")}], _live()) == (0, [], {"Unknown": 1})


@responses.activate
def test_a_message_with_no_recipient_is_charged_to_itself_and_the_rest_still_land():
    batch = [{"title": "no recipient"}, {"to": _tok("fine")}]
    _replies({"data": [{"status": "ok", "id": "t-0"}, {"status": "ok", "id": "t-1"}]})

    sent, dead, errors = push._send_slice(batch, _live())

    assert sent == 1
    assert errors == {"Malformed": 1}
    assert [entry[1] for entry in push._receipt_queue] == [_tok("fine")]


@responses.activate
def test_a_recipient_that_cannot_be_digested_is_charged_twice_over(caplog):
    # The code is tallied before the log line, and the log line
    # is what raises — so the ticket lands in both buckets
    _replies({"data": [{"status": "error", "details": {"error": "MessageTooBig"}}]})

    with caplog.at_level(logging.ERROR, logger=LOG_NAME):
        sent, dead, errors = push._send_slice([{"to": 12345}], _live())

    assert errors == {"MessageTooBig": 1, "Malformed": 1}
    assert "Failed to read an Expo ticket" in caplog.text


@responses.activate
def test_a_dead_device_with_an_undigestible_name_still_comes_back_dead():
    # The DeviceNotRegistered arm never digests, so nothing raises
    _replies({"data": [{"status": "error", "details": {"error": "DeviceNotRegistered"}}]})

    assert push._send_slice([{"to": 999}], _live()) == (0, [999], {"DeviceNotRegistered": 1})


@responses.activate
def test_a_ticket_error_message_quoting_a_token_never_logs_it_raw(caplog):
    token = _tok("ticketleak")
    _replies({"data": [{"status": "error", "details": {"error": "MessageTooBig"},
                        "message": f"payload for {token} was too large"}]})

    with caplog.at_level(logging.WARNING, logger=LOG_NAME):
        push._send_slice([{"to": token}], _live())

    assert token not in caplog.text
    assert push.token_digest(token) in caplog.text


@responses.activate
def test_a_ticket_error_message_cannot_forge_a_log_line(caplog):
    _replies({"data": [{"status": "error", "details": {"error": "MessageTooBig"},
                        "message": "too big\nCRITICAL all devices lost"}]})

    with caplog.at_level(logging.WARNING, logger=LOG_NAME):
        push._send_slice([{"to": _tok("forge")}], _live())

    assert all("\n" not in record.getMessage() for record in caplog.records)


@responses.activate
def test_a_slice_never_opens_a_database_connection_even_for_a_dead_device(monkeypatch):
    monkeypatch.setattr(push, "get_db", _boom)
    _replies({"data": [{"status": "error", "details": {"error": "DeviceNotRegistered"}}]})

    assert push._send_slice([{"to": _tok("nodb")}], _live()) == (0, [_tok("nodb")],
                                                                 {"DeviceNotRegistered": 1})


@responses.activate
def test_a_slice_leaves_the_batch_it_was_handed_untouched():
    batch = [{"to": _tok("keep"), "title": "T"}]
    before = json.dumps(batch)
    _accepts_all()

    push._send_slice(batch, _live())

    assert json.dumps(batch) == before


@responses.activate
def test_a_slice_always_answers_with_the_same_three_shapes():
    _accepts_all()

    verdict = push._send_slice([{"to": _tok("shape")}], _live())

    assert isinstance(verdict, tuple) and len(verdict) == 3
    assert isinstance(verdict[0], int)
    assert isinstance(verdict[1], list)
    assert isinstance(verdict[2], dict)


@responses.activate
def test_a_full_hundred_message_slice_goes_out_as_one_request():
    _accepts_all()
    batch = [{"to": _tok(f"full{i:03d}")} for i in range(push._SEND_SLICE)]

    assert push._send_slice(batch, _live())[0] == 100
    assert len(responses.calls) == 1








# -----------------------------------------------------------
# send_push_batch — the sliced, paced, bounded fan-out
# -----------------------------------------------------------

@responses.activate
@pytest.mark.parametrize("tokens", [None, [], (), ""])
def test_nothing_to_send_to_makes_no_request_at_all(tokens):
    assert push.send_push_batch(tokens, "T", "B") == 0
    assert len(responses.calls) == 0


@responses.activate
def test_nothing_to_send_to_leaves_a_stats_dict_untouched():
    stats = {}

    assert push.send_push_batch([], "T", "B", stats=stats) == 0
    assert stats == {}


@responses.activate
def test_one_token_is_one_message_in_one_request(monkeypatch):
    monkeypatch.setattr(push, "ThreadPoolExecutor", _forbidden_pool)
    _accepts_all()

    assert push.send_push_batch([_tok("solo")], "Sveiki", "Naujiena") == 1

    body = _bodies()[0]
    assert isinstance(body, list) and len(body) == 1
    assert body[0]["to"] == _tok("solo")


@responses.activate
def test_every_message_of_a_batch_differs_only_in_its_recipient():
    _accepts_all()
    tokens = [_tok(f"same{i}") for i in range(4)]

    push.send_push_batch(tokens, "Titulas", "Tekstas", data={"type": "news"})

    messages = _messages()
    assert sorted(m["to"] for m in messages) == sorted(tokens)
    for message in messages:
        assert message["title"] == "Titulas"
        assert message["body"] == "Tekstas"
        assert message["data"] == {"type": "news"}
        assert message["sound"] == "default"
        assert message["channelId"] == "default"


@responses.activate
def test_a_batch_carries_the_five_fields_and_nothing_more_when_nothing_optional_is_given():
    _accepts_all()

    push.send_push_batch([_tok("plain")], "T", "B")

    assert set(_messages()[0]) == {"to", "title", "body", "sound", "channelId"}


@responses.activate
@pytest.mark.parametrize("data", [None, {}, 0, "", []])
def test_a_falsy_data_object_is_stamped_on_no_message(data):
    _accepts_all()

    push.send_push_batch([_tok("nodata")], "T", "B", data=data)

    assert "data" not in _messages()[0]


@responses.activate
@pytest.mark.parametrize("priority", [None, "", 0])
def test_a_falsy_priority_is_left_off_the_message(priority):
    _accepts_all()

    push.send_push_batch([_tok("nopri")], "T", "B", priority=priority)

    assert "priority" not in _messages()[0]


@responses.activate
@pytest.mark.parametrize("priority", ["high", "normal", "default"])
def test_a_priority_rides_every_message_as_given(priority):
    _accepts_all()

    push.send_push_batch([_tok("pri"), _tok("pri2")], "T", "B", priority=priority)

    assert {m["priority"] for m in _messages()} == {priority}


@responses.activate
@pytest.mark.parametrize("ttl", [0, 1, 3600, 86400, -5])
def test_any_ttl_including_zero_rides_the_message(ttl):
    _accepts_all()

    push.send_push_batch([_tok("ttl")], "T", "B", ttl=ttl)

    assert _messages()[0]["ttl"] == ttl


@responses.activate
def test_a_ttl_of_none_is_left_off_entirely():
    _accepts_all()

    push.send_push_batch([_tok("nottl")], "T", "B", ttl=None)

    assert "ttl" not in _messages()[0]


@responses.activate
def test_the_callers_data_object_comes_back_exactly_as_it_went_in():
    _accepts_all()
    data = {"type": "news"}

    push.send_push_batch([_tok("nomutate")], "T", "B", data=data)

    assert data == {"type": "news"}


@responses.activate
def test_a_bare_string_is_iterated_character_by_character(monkeypatch):
    # Not a supported call — pinned because it fails silently
    # rather than loudly: `tokens` is a list of tokens, and a
    # single string is a sequence of one-character "tokens"
    monkeypatch.setattr(push, "ThreadPoolExecutor", _forbidden_pool)
    _accepts_all()

    push.send_push_batch("abcde", "T", "B")

    assert [m["to"] for m in _messages()] == ["a", "b", "c", "d", "e"]


@responses.activate
def test_empty_and_none_recipients_are_posted_because_filtering_is_the_callers_job():
    _accepts_all()

    assert push.send_push_batch([None, "", _tok("real")], "T", "B") == 3
    assert [m["to"] for m in _messages()] == [None, "", _tok("real")]


@responses.activate
def test_exactly_the_slice_cap_is_a_single_inline_request(monkeypatch):
    monkeypatch.setattr(push, "ThreadPoolExecutor", _forbidden_pool)
    _accepts_all()
    tokens = [_tok(f"cap{i:03d}") for i in range(100)]

    assert push.send_push_batch(tokens, "T", "B") == 100
    assert len(responses.calls) == 1


@responses.activate
def test_one_token_past_the_cap_splits_into_two_slices(monkeypatch):
    workers = []
    monkeypatch.setattr(push, "ThreadPoolExecutor", _pool_recorder(workers))
    _accepts_all()
    tokens = [_tok(f"over{i:03d}") for i in range(101)]

    assert push.send_push_batch(tokens, "T", "B") == 101

    assert len(responses.calls) == 2
    assert [len(body) for body in _bodies()] == [100, 1]
    assert workers == [2]


@responses.activate
def test_every_token_of_a_split_batch_is_addressed_exactly_once(monkeypatch):
    monkeypatch.setattr(push, "ThreadPoolExecutor", _pool_recorder([]))
    _accepts_all()
    tokens = [_tok(f"fan{i:03d}") for i in range(250)]

    assert push.send_push_batch(tokens, "T", "B") == 250

    assert [len(body) for body in _bodies()] == [100, 100, 50]
    assert sorted(m["to"] for m in _messages()) == sorted(tokens)


@pytest.mark.parametrize("count, expected_workers", [
    (101, 2),
    (200, 2),
    (201, 3),
    (300, 3),
    (301, 3),
    (1000, 3),
])
def test_the_pool_never_grows_past_three_workers(count, expected_workers, monkeypatch):
    workers = []
    monkeypatch.setattr(push, "ThreadPoolExecutor", _pool_recorder(workers))
    monkeypatch.setattr(push, "_send_slice", _slice_recorder([]))

    push.send_push_batch([_tok(f"w{i:04d}") for i in range(count)], "T", "B")

    assert workers == [expected_workers]
    assert expected_workers <= push._FANOUT_WORKERS


def test_every_slice_of_a_fan_out_shares_one_deadline(monkeypatch):
    calls = []
    monkeypatch.setattr(push, "ThreadPoolExecutor", _pool_recorder([]))
    monkeypatch.setattr(push, "_send_slice", _slice_recorder(calls))

    push.send_push_batch([_tok(f"dl{i:03d}") for i in range(250)], "T", "B")

    assert len({call["deadline"] for call in calls}) == 1


def test_the_shared_deadline_is_the_fan_out_budget_from_now(monkeypatch):
    calls = []
    monkeypatch.setattr(push, "_send_slice", _slice_recorder(calls))

    with time_machine.travel(FROZEN, tick=False):
        push.send_push_batch([_tok("budget")], "T", "B")
        assert calls[0]["deadline"] == time.monotonic() + push._FANOUT_DEADLINE


def test_the_fan_out_budget_is_bounded_so_a_scheduler_tick_cannot_hang():
    assert 0 < push._FANOUT_DEADLINE <= 300


def test_the_slices_carry_the_messages_in_order(monkeypatch):
    calls = []
    monkeypatch.setattr(push, "ThreadPoolExecutor", _pool_recorder([]))
    monkeypatch.setattr(push, "_send_slice", _slice_recorder(calls))
    tokens = [_tok(f"ord{i:03d}") for i in range(150)]

    push.send_push_batch(tokens, "T", "B")

    assert [len(call["batch"]) for call in calls] == [100, 50]
    assert [m["to"] for call in calls for m in call["batch"]] == tokens


@responses.activate
def test_a_batch_counts_accepted_tickets_not_messages_posted():
    _replies({"data": [{"status": "ok", "id": "t-0"},
                       {"status": "error", "details": {"error": "MessageTooBig"}},
                       {"status": "ok", "id": "t-2"}]})

    assert push.send_push_batch([_tok("a"), _tok("b"), _tok("c")], "T", "B") == 2


@responses.activate
def test_a_batch_answers_with_a_plain_integer():
    _accepts_all()

    assert push.send_push_batch([_tok("int")], "T", "B") is not True
    assert isinstance(push.send_push_batch([_tok("int")], "T", "B"), int)


@responses.activate
def test_a_batch_that_expo_never_answered_reports_nothing_sent():
    _raises(requests.exceptions.ConnectionError("no route"))

    assert push.send_push_batch([_tok("down")], "T", "B") == 0


@responses.activate
def test_a_failing_slice_costs_only_itself(monkeypatch):
    monkeypatch.setattr(push, "ThreadPoolExecutor", _pool_recorder([]))
    _replies(text="no", status=400)
    _accepts_all()
    tokens = [_tok(f"half{i:03d}") for i in range(150)]

    assert push.send_push_batch(tokens, "T", "B") == 50


@responses.activate
def test_the_error_tallies_of_every_slice_are_merged(monkeypatch):
    stats = {}
    monkeypatch.setattr(push, "ThreadPoolExecutor", _pool_recorder([]))
    ticket = {"status": "error", "details": {"error": "MessageTooBig"}}
    _replies({"data": [ticket] * 100})
    _replies({"data": [ticket] * 50})
    tokens = [_tok(f"merge{i:03d}") for i in range(150)]

    assert push.send_push_batch(tokens, "T", "B", stats=stats) == 0
    assert stats["errors"] == {"MessageTooBig": 150}


@responses.activate
def test_a_stats_dict_gets_sent_failed_and_an_error_tally():
    stats = {}
    _replies({"data": [{"status": "ok", "id": "t-0"},
                       {"status": "error", "details": {"error": "MessageRateExceeded"}}]})

    push.send_push_batch([_tok("s1"), _tok("s2")], "T", "B", stats=stats)

    assert stats == {"sent": 1, "failed": 1, "errors": {"MessageRateExceeded": 1}}


@responses.activate
def test_a_clean_batch_still_leaves_an_empty_error_tally_behind():
    stats = {}
    _accepts_all()

    push.send_push_batch([_tok("clean")], "T", "B", stats=stats)

    assert stats == {"sent": 1, "failed": 0, "errors": {}}


@responses.activate
def test_a_second_batch_adds_to_the_same_stats_dict():
    stats = {"sent": 5, "failed": 2, "errors": {"MessageTooBig": 1}}
    _accepts_all()

    push.send_push_batch([_tok("more1"), _tok("more2")], "T", "B", stats=stats)

    assert stats == {"sent": 7, "failed": 2, "errors": {"MessageTooBig": 1}}


@responses.activate
def test_a_repeated_error_code_adds_to_the_count_already_in_the_stats():
    stats = {"errors": {"MessageTooBig": 4}}
    _replies({"data": [{"status": "error", "details": {"error": "MessageTooBig"}}]})

    push.send_push_batch([_tok("again")], "T", "B", stats=stats)

    assert stats["errors"] == {"MessageTooBig": 5}


@responses.activate
def test_the_failed_count_is_every_token_that_did_not_come_back_accepted():
    stats = {}
    _replies({"data": [{"status": "ok", "id": "t-0"}]})

    push.send_push_batch([_tok("f1"), _tok("f2"), _tok("f3")], "T", "B", stats=stats)

    # Two of the three were never ticketed at all — the slice
    # charges them nothing, the batch counts them as failed
    assert (stats["sent"], stats["failed"]) == (1, 2)
    assert stats["errors"] == {}


@responses.activate
def test_the_same_device_twice_counts_as_two_messages_in_the_stats():
    stats = {}
    token = _tok("twice")
    _accepts_all()

    assert push.send_push_batch([token, token], "T", "B", stats=stats) == 2
    assert (stats["sent"], stats["failed"]) == (2, 0)


@responses.activate
def test_a_batch_with_no_stats_dict_still_reports_its_number():
    _accepts_all()

    assert push.send_push_batch([_tok("nostats")], "T", "B", stats=None) == 1


@responses.activate
def test_the_stats_sent_count_matches_the_returned_number():
    stats = {}
    _accepts_all()

    returned = push.send_push_batch([_tok(f"match{i}") for i in range(3)], "T", "B", stats=stats)

    assert returned == stats["sent"]


@responses.activate
def test_every_dead_device_of_the_whole_fan_out_is_retired_in_one_update(monkeypatch):
    retired = []
    monkeypatch.setattr(push, "ThreadPoolExecutor", _pool_recorder([]))
    monkeypatch.setattr(push, "_deactivate_tokens", lambda tokens: retired.append(list(tokens)))
    dead = {"status": "error", "details": {"error": "DeviceNotRegistered"}}
    _replies({"data": [dead] * 100})
    _replies({"data": [dead] * 20})
    tokens = [_tok(f"dead{i:03d}") for i in range(120)]

    push.send_push_batch(tokens, "T", "B")

    assert len(retired) == 1
    assert sorted(retired[0]) == sorted(tokens)


@responses.activate
def test_a_batch_with_no_dead_device_opens_no_database_connection(monkeypatch):
    monkeypatch.setattr(push, "get_db", _boom)
    _accepts_all()

    assert push.send_push_batch([_tok("nodb")], "T", "B") == 1


@responses.activate
def test_a_dead_device_really_is_retired_in_the_table(app, db, make_user):
    user = make_user()
    gone = _seed_token(db, user["id"], _tok("batchgone"))
    alive = _seed_token(db, user["id"], _tok("batchalive"))
    _replies({"data": [{"status": "error", "details": {"error": "DeviceNotRegistered"}},
                       {"status": "ok", "id": "t-1"}]})

    assert push.send_push_batch([gone, alive], "T", "B") == 1

    assert _row(db, gone)["active"] == 0
    assert _row(db, alive)["active"] == 1


@responses.activate
def test_the_same_dead_device_reported_twice_moves_one_row(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _tok("dupdead"))
    dead = {"status": "error", "details": {"error": "DeviceNotRegistered"}}
    _replies({"data": [dead, dead]})

    push.send_push_batch([token, token], "T", "B")

    assert db.execute("SELECT COUNT(*) c FROM push_tokens WHERE active = 0").fetchone()["c"] == 1


@responses.activate
def test_a_batch_reports_its_error_tally_once_and_in_order(caplog):
    _replies({"data": [{"status": "error", "details": {"error": "MessageTooBig"}},
                       {"status": "error", "details": {"error": "DeviceNotRegistered"}},
                       {"status": "error", "details": {"error": "MessageTooBig"}}]})

    with caplog.at_level(logging.WARNING, logger=LOG_NAME):
        push.send_push_batch([_tok("t1"), _tok("t2"), _tok("t3")], "T", "B")

    tally = [record.getMessage() for record in caplog.records
             if record.getMessage().startswith("Push batch errors")]
    assert tally == ["Push batch errors: DeviceNotRegistered=1, MessageTooBig=2"]


@responses.activate
def test_a_clean_batch_logs_no_error_tally(caplog):
    _accepts_all()

    with caplog.at_level(logging.WARNING, logger=LOG_NAME):
        push.send_push_batch([_tok("quiet")], "T", "B")

    assert "Push batch errors" not in caplog.text


@responses.activate
def test_a_batch_always_says_how_many_of_how_many_expo_accepted(caplog):
    _replies({"data": [{"status": "ok", "id": "t-0"},
                       {"status": "error", "details": {"error": "MessageTooBig"}}]})

    with caplog.at_level(logging.INFO, logger=LOG_NAME):
        push.send_push_batch([_tok("i1"), _tok("i2")], "T", "B")

    assert "Push batch: 1/2 accepted by Expo" in caplog.text


@responses.activate
def test_a_batch_past_the_fan_out_deadline_sends_nothing_and_charges_everything(monkeypatch):
    stats = {}
    monkeypatch.setattr(push, "_FANOUT_DEADLINE", -1)
    tokens = [_tok(f"late{i}") for i in range(5)]

    assert push.send_push_batch(tokens, "T", "B", stats=stats) == 0

    assert len(responses.calls) == 0
    assert stats == {"sent": 0, "failed": 5, "errors": {"Abandoned": 5}}


@responses.activate
def test_a_whole_fan_out_past_the_deadline_abandons_every_slice(monkeypatch):
    monkeypatch.setattr(push, "ThreadPoolExecutor", _pool_recorder([]))
    monkeypatch.setattr(push, "_FANOUT_DEADLINE", -1)
    stats = {}

    assert push.send_push_batch([_tok(f"gone{i:03d}") for i in range(250)], "T", "B",
                                stats=stats) == 0

    assert stats["errors"] == {"Abandoned": 250}


@responses.activate
def test_a_thousand_devices_fan_out_in_ten_slices(monkeypatch):
    calls = []
    monkeypatch.setattr(push, "ThreadPoolExecutor", _pool_recorder([]))
    monkeypatch.setattr(push, "_send_slice", _slice_recorder(calls))

    assert push.send_push_batch([_tok(f"k{i:04d}") for i in range(1000)], "T", "B") == 1000

    assert [len(call["batch"]) for call in calls] == [100] * 10


@responses.activate
def test_a_real_pool_fan_out_still_addresses_every_device_once():
    # The one test that lets the shipped ThreadPoolExecutor run:
    # the slices really do go out concurrently, and the merge on
    # the calling thread still sees all of them
    _accepts_all()
    tokens = [_tok(f"pool{i:03d}") for i in range(250)]

    assert push.send_push_batch(tokens, "T", "B") == 250
    assert sorted(m["to"] for m in _messages()) == sorted(tokens)
