# -----------------------------------------------------------
#  [*] Tests — notifications/push.py, the Expo fan-out
#
#  The only path from this backend to a phone, proved without
#  a single packet leaving the process: every exp.host call is
#  driven through `responses`, and every clock the module
#  reads (the paced slice gate, the 15-minute receipt window,
#  session expiry) is driven through `time_machine`. The test
#  container has no network at all, so a regression that
#  reaches Expo for real fails here by construction.
#
#  What this module proves:
#
#    - BATCHING: one message per token, sliced at Expo's cap
#      of 100 — 100 tokens are one request, 101 are two, and a
#      broadcast fans out through the pool with every token
#      addressed exactly once
#    - LANGUAGE: push_tokens.language routes the copy —
#      'en' devices get title_en/body_en, 'lt' AND anything
#      else ride the Lithuanian batch, and a missing
#      translation falls back to it in ONE request
#    - OPT-OUT: the model is opt-out, so a missing
#      notification_channels row means enabled and only an
#      explicit enabled=0 for THAT channel silences a user;
#      an unknown channel name (which has no opt-out rows at
#      all, and would therefore mean "send to everyone")
#      sends nothing
#    - THE SKIP: notify_channel's exclude_user_id leaves the
#      one device out — the shape the callers use to skip the
#      author of a post, and chat's own online-user skip,
#      which hands notify_channel_users only the recipients
#      that have no socket in the room
#    - DEAD DEVICES: a DeviceNotRegistered verdict from a
#      ticket OR, fifteen minutes later, from a receipt
#      retires the row (active=0, never deleted, so the next
#      register revives it) — in ONE update for a whole batch
#    - RECEIPTS: tickets younger than the delay stay queued,
#      the rest are traded in slices of 300, and the operator
#      failures (InvalidCredentials, MessageRateExceeded) are
#      shouted about rather than swallowed
#    - CONTAINMENT: a timeout, a connection error, an HTML
#      error page, a JSON array where an object belongs, a
#      ticket that is not an object, a missing recipient, a
#      dead database — none of them raise into the caller and
#      none of them abandon the rest of the fan-out
#    - HYGIENE: no log line in this module may carry a raw
#      push token; it is a bearer credential for that phone
# -----------------------------------------------------------

import json
import logging
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests
import responses
import time_machine

from app.notifications import push


LOGGER_NAME = "app.notifications.push"

# The instant every clock-sensitive test travels to. Inside a
# time_machine.travel, time.monotonic() moves with the
# traveller, which is what makes the 15-minute receipt window
# testable without a real second passing
FROZEN = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)

# Read once at import, BEFORE the autouse fixture switches
# pacing off — the ceiling test asserts against the shipped
# value, not the test one
SHIPPED_SLICE_INTERVAL = push._SLICE_INTERVAL




# -----------------------------------------------------------
# _isolated_push_module
# -----------------------------------------------------------
#
# push.py carries process-wide state: the paced gate's
# last-slice stamp and the in-memory receipt queue. Both are
# reset around EVERY test here, and pacing is switched off
# unless a test asks for it.
#
# The reset is not tidiness. A test that travels in time moves
# time.monotonic() forward by decades; leaving that value in
# _last_slice_at would make the very next slice — in this file
# or in another agent's chat test — block under the pace lock
# for years.
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_push_module(monkeypatch):
    _reset_push_state()
    monkeypatch.setattr(push, "_SLICE_INTERVAL", 0.0)
    yield
    _reset_push_state()


def _reset_push_state():
    push._last_slice_at = 0.0
    with push._receipt_lock:
        push._receipt_queue.clear()




# -----------------------------------------------------------
# Token / row helpers
# -----------------------------------------------------------
#
# _expo_token pads the opaque part so the strings also satisfy
# the intake grammar notifications/routes.py enforces
# (ExponentPushToken[10..64 of A-Za-z0-9_-]) — these tests
# insert rows directly, but a realistic token is what makes
# the redaction assertions mean something.
# -----------------------------------------------------------

def _expo_token(name):
    return f"ExponentPushToken[{name}-aaaaaaaaaa]"


def _seed_token(db, user_id, token, language="lt", active=1, platform="ios"):
    db.execute(
        "INSERT INTO push_tokens (id, user_id, token, platform, language, active)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), user_id, token, platform, language, active),
    )
    db.commit()
    return token


def _set_channel(db, user_id, channel, enabled):
    db.execute(
        "INSERT OR REPLACE INTO notification_channels (user_id, channel, enabled) VALUES (?, ?, ?)",
        (user_id, channel, enabled),
    )
    db.commit()


def _seed_session(db, user_id, expires_at):
    db.execute(
        "INSERT INTO sessions (id, user_id, token, expires_at) VALUES (?, ?, ?, ?)",
        (str(uuid.uuid4()), user_id, uuid.uuid4().hex, expires_at),
    )
    db.commit()


def _token_row(db, token):
    return db.execute("SELECT * FROM push_tokens WHERE token = ?", (token,)).fetchone()




# -----------------------------------------------------------
# _bulk_users_with_tokens
# -----------------------------------------------------------
#
# Hundreds of users in two executemany calls — the only way to
# reach the _ID_CHUNK boundary (400 recipient ids per query)
# and the _SEND_SLICE boundary (100 messages per POST) with
# real rows instead of a monkeypatched constant.
#
# Used by:
#   - the chunking and fan-out tests below
# -----------------------------------------------------------

def _bulk_users_with_tokens(db, count, prefix, language="lt"):
    users = []
    user_rows = []
    token_rows = []

    for i in range(count):
        user_id = f"{prefix}-user-{i:04d}"
        token = _expo_token(f"{prefix}{i:04d}")
        user_rows.append((user_id, f"{prefix}{i:04d}", f"{prefix}{i:04d}@knf.vu.lt",
                          f"Bulk {i}", "x", "student", 1, 1))
        token_rows.append((f"{prefix}-tok-{i:04d}", user_id, token, "ios", language, 1))
        users.append((user_id, token))

    db.executemany(
        "INSERT INTO users (id, username, email, display_name, password_hash, role, active, invited)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", user_rows)
    db.executemany(
        "INSERT INTO push_tokens (id, user_id, token, platform, language, active)"
        " VALUES (?, ?, ?, ?, ?, ?)", token_rows)
    db.commit()
    return users




# -----------------------------------------------------------
# Expo fakes
# -----------------------------------------------------------
#
# _expo_accepts_all answers whatever it is handed with a
# matching row of "ok" tickets, so a test can send one message
# or two hundred and fifty without arranging a body for each
# slice. The ticket id embeds the recipient, which is what
# lets the receipt tests tie a verdict back to a device.
# -----------------------------------------------------------

def _expo_accepts_all():

    def _reply(request):
        payload = json.loads(request.body)
        if isinstance(payload, list):
            data = [{"status": "ok", "id": f"ticket:{msg.get('to')}"} for msg in payload]
        else:
            data = {"status": "ok", "id": f"ticket:{payload.get('to')}"}
        return 200, {}, json.dumps({"data": data})

    responses.add_callback(responses.POST, push.EXPO_PUSH_URL,
                           callback=_reply, content_type="application/json")


def _expo_replies(body=None, status=200, text=None):
    if text is not None:
        responses.add(responses.POST, push.EXPO_PUSH_URL, body=text, status=status,
                      content_type="text/html")
    else:
        responses.add(responses.POST, push.EXPO_PUSH_URL, json=body, status=status)


def _expo_raises(exc):
    responses.add(responses.POST, push.EXPO_PUSH_URL, body=exc)


def _expo_receipts(body=None, status=200, text=None, exc=None):
    if exc is not None:
        responses.add(responses.POST, push.EXPO_RECEIPTS_URL, body=exc)
    elif text is not None:
        responses.add(responses.POST, push.EXPO_RECEIPTS_URL, body=text, status=status,
                      content_type="text/html")
    else:
        responses.add(responses.POST, push.EXPO_RECEIPTS_URL, json=body, status=status)


def _push_bodies():
    return [json.loads(call.request.body) for call in responses.calls
            if call.request.url == push.EXPO_PUSH_URL]


def _all_messages():
    out = []
    for body in _push_bodies():
        out.extend(body if isinstance(body, list) else [body])
    return out


def _sent_tokens():
    return sorted(msg["to"] for msg in _all_messages())


def _receipt_bodies():
    return [json.loads(call.request.body) for call in responses.calls
            if call.request.url == push.EXPO_RECEIPTS_URL]


def _future():
    return time.monotonic() + 60


def _past():
    return time.monotonic() - 1




# -----------------------------------------------------------
# _explode
# -----------------------------------------------------------
#
# Stands in for get_db where the point is that the function
# under test must NOT reach the database — or must survive it
# being unreachable.
#
# Used by:
#   - the "no database of its own" and "database is gone"
#     tests below
# -----------------------------------------------------------

def _explode(*args, **kwargs):
    raise sqlite3.OperationalError("unable to open database file")




# -----------------------------------------------------------
# no_backoff
# -----------------------------------------------------------
#
# `responses` drives the mounted adapter's real Retry policy,
# which is what makes the retry tests below meaningful — but
# the shipped backoff waits 0 s, then 2 s, then 4 s between
# attempts. This swaps the policy for the same one with the
# waiting removed, so the retry COUNT under test is still the
# shipped one.
#
# Used by:
#   - the retry tests below; every other test uses a status
#     outside the forcelist, which is never retried
# -----------------------------------------------------------

@pytest.fixture
def no_backoff(monkeypatch):
    adapter = push._SESSION.get_adapter("https://exp.host")
    monkeypatch.setattr(adapter, "max_retries", adapter.max_retries.new(backoff_factor=0))




# -----------------------------------------------------------
# _RecordingSession
# -----------------------------------------------------------
#
# `responses` fakes the wire but hides the call itself, and
# the timeout is the one argument that has to be asserted: the
# mobile client gives up at 15 s, so the server must always
# fold first. This stub stands in for _SESSION just long
# enough to read the kwargs back.
#
# Used by:
#   - the transport-contract tests below
# -----------------------------------------------------------

class _RecordingSession:

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "body": json, "headers": headers, "timeout": timeout})
        return _FakeResponse(200, self.payload)


class _FakeResponse:

    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = "fake body"

    def json(self):
        return self._payload








# -----------------------------------------------------------
# token_digest — the only shape a token may take in a log
# -----------------------------------------------------------

def test_token_digest_is_the_first_eight_hex_digits_of_sha256():
    import hashlib

    token = _expo_token("digest")
    assert push.token_digest(token) == hashlib.sha256(token.encode()).hexdigest()[:8]


def test_token_digest_is_eight_lowercase_hex_characters():
    digest = push.token_digest(_expo_token("shape"))
    assert len(digest) == 8
    assert all(c in "0123456789abcdef" for c in digest)


def test_token_digest_is_stable_for_the_same_device():
    token = _expo_token("stable")
    assert push.token_digest(token) == push.token_digest(token)


def test_token_digest_separates_two_devices():
    assert push.token_digest(_expo_token("one")) != push.token_digest(_expo_token("two"))


def test_token_digest_never_contains_the_token_itself():
    token = _expo_token("secret")
    assert token not in push.token_digest(token)
    assert "secret" not in push.token_digest(token)








# -----------------------------------------------------------
# _sanitize — upstream text is never trusted into a log line
# -----------------------------------------------------------

def test_sanitize_redacts_a_token_expo_quoted_back_at_us():
    token = _expo_token("quoted")
    cleaned = push._sanitize(f'"{token}" is not a registered device')

    assert token not in cleaned
    assert f"token:{push.token_digest(token)}" in cleaned


def test_sanitize_redacts_the_short_expo_spelling_too():
    cleaned = push._sanitize("ExpoPushToken[abcdef0123456] failed")
    assert "abcdef0123456" not in cleaned
    assert "token:" in cleaned


def test_sanitize_redacts_every_token_in_the_string():
    first, second = _expo_token("first"), _expo_token("second")
    cleaned = push._sanitize(f"{first} and {second}")

    assert first not in cleaned and second not in cleaned
    assert cleaned.count("token:") == 2


def test_sanitize_folds_newlines_so_upstream_text_cannot_forge_log_lines():
    cleaned = push._sanitize("first\nERROR forged second\r\nthird")

    assert "\n" not in cleaned
    assert "\r" not in cleaned
    assert "forged" in cleaned


def test_sanitize_truncates_at_the_default_limit():
    assert len(push._sanitize("x" * 5000)) == 200


def test_sanitize_honours_a_caller_supplied_limit():
    assert len(push._sanitize("x" * 5000, 500)) == 500


def test_sanitize_accepts_something_that_is_not_a_string():
    assert push._sanitize({"errors": [1, 2]}) == "{'errors': [1, 2]}"
    assert push._sanitize(None) == "None"


def test_sanitize_leaves_ordinary_text_alone():
    assert push._sanitize("MessageTooBig") == "MessageTooBig"


def test_sanitize_of_an_empty_string_is_an_empty_string():
    assert push._sanitize("") == ""








# -----------------------------------------------------------
# Transport — the session, the timeout, the paced gate
# -----------------------------------------------------------

def test_the_shared_session_retries_a_post_on_rate_limit_and_server_errors():
    session = push._build_session()
    retry = session.get_adapter("https://exp.host").max_retries

    assert retry.total == 3
    assert retry.backoff_factor == 1
    assert set(retry.status_forcelist) == {429, 500, 502, 503, 504}
    assert retry.respect_retry_after_header is True
    # urllib3 never retries a non-idempotent method unless it
    # is listed — without this a rate-limited slice was dropped
    assert "POST" in retry.allowed_methods


def test_the_module_session_is_the_one_that_carries_those_retries():
    assert push._SESSION.get_adapter("https://exp.host").max_retries.total == 3


def test_the_http_timeout_folds_before_the_mobile_client_gives_up():
    assert push._HTTP_TIMEOUT < 15


def test_a_send_a_slice_and_a_receipt_poll_all_carry_the_timeout_and_json_headers(monkeypatch):
    session = _RecordingSession({"data": []})
    monkeypatch.setattr(push, "_SESSION", session)

    push.send_push_notification(_expo_token("kwargs"), "T", "B")
    push._send_slice([{"to": _expo_token("kwargs")}], _future())
    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-kwargs", _expo_token("kwargs"))
        traveller.shift(push._RECEIPT_DELAY + 1)
        push.poll_push_receipts()

    assert len(session.calls) == 3
    assert {call["timeout"] for call in session.calls} == {push._HTTP_TIMEOUT}
    assert all(call["headers"] == push._EXPO_HEADERS for call in session.calls)
    assert {call["url"] for call in session.calls} == {push.EXPO_PUSH_URL, push.EXPO_RECEIPTS_URL}


def test_no_expo_access_token_header_is_sent():
    # The Expo project keeps enhanced push security OFF; an
    # Authorization header here would be rejected upstream
    assert set(push._EXPO_HEADERS) == {"Accept", "Content-Type"}


def test_the_slice_cap_and_the_pace_stay_under_expos_documented_ceiling():
    assert push._SEND_SLICE == 100
    assert push._SEND_SLICE / SHIPPED_SLICE_INTERVAL <= 600


def test_the_receipt_slice_stays_under_expos_thousand_id_cap():
    assert push._RECEIPT_SLICE <= 1000


def test_the_recipient_chunk_stays_under_sqlites_variable_limit():
    # The query binds one variable per id PLUS the channel name
    assert push._ID_CHUNK + 1 <= 999


def test_pace_slice_waits_out_the_remainder_of_the_interval(monkeypatch):
    slept = []
    monkeypatch.setattr(push, "_SLICE_INTERVAL", 5.0)
    monkeypatch.setattr(push.time, "sleep", slept.append)
    push._last_slice_at = time.monotonic()

    push._pace_slice()

    assert len(slept) == 1
    assert 4.5 <= slept[0] <= 5.0


def test_pace_slice_does_not_wait_when_the_interval_has_already_passed(monkeypatch):
    slept = []
    monkeypatch.setattr(push, "_SLICE_INTERVAL", 0.2)
    monkeypatch.setattr(push.time, "sleep", slept.append)
    push._last_slice_at = time.monotonic() - 100

    push._pace_slice()

    assert slept == []


def test_pace_slice_records_the_moment_the_slice_left(monkeypatch):
    monkeypatch.setattr(push, "_SLICE_INTERVAL", 0.0)
    push._last_slice_at = 0.0
    before = time.monotonic()

    push._pace_slice()

    assert push._last_slice_at >= before








# -----------------------------------------------------------
# _queue_receipt — the bounded, memory-only ticket park
# -----------------------------------------------------------

def test_queue_receipt_parks_the_ticket_with_its_token():
    push._queue_receipt("ticket-1", _expo_token("parked"))

    assert len(push._receipt_queue) == 1
    ticket_id, token, stamp = push._receipt_queue[0]
    assert ticket_id == "ticket-1"
    assert token == _expo_token("parked")
    assert isinstance(stamp, float)


def test_queue_receipt_ignores_a_ticket_without_an_id():
    push._queue_receipt(None, _expo_token("none"))
    push._queue_receipt("", _expo_token("empty"))

    assert len(push._receipt_queue) == 0


def test_the_receipt_queue_is_bounded_so_a_flood_cannot_grow_it_forever():
    assert push._receipt_queue.maxlen == 20000

    for i in range(push._receipt_queue.maxlen + 5):
        push._queue_receipt(f"ticket-{i}", _expo_token(f"flood{i}"))

    assert len(push._receipt_queue) == push._receipt_queue.maxlen
    # The oldest ids are dropped, never the newest
    assert push._receipt_queue[-1][0] == f"ticket-{push._receipt_queue.maxlen + 4}"








# -----------------------------------------------------------
# send_push_notification — one message, one ticket
# -----------------------------------------------------------

@responses.activate
def test_a_single_send_reports_an_accepted_ticket():
    _expo_accepts_all()

    assert push.send_push_notification(_expo_token("single"), "Sveiki", "Naujiena") is True

    sent = _all_messages()
    assert len(sent) == 1
    assert sent[0]["to"] == _expo_token("single")
    assert sent[0]["title"] == "Sveiki"
    assert sent[0]["body"] == "Naujiena"


@responses.activate
def test_a_single_send_carries_the_android_channel_the_app_registers():
    _expo_accepts_all()
    push.send_push_notification(_expo_token("chan"), "T", "B")

    message = _all_messages()[0]
    assert message["channelId"] == "default"
    assert message["sound"] == "default"


@responses.activate
def test_a_single_send_carries_data_and_badge_when_they_are_given():
    _expo_accepts_all()
    push.send_push_notification(_expo_token("extras"), "T", "B",
                                data={"type": "news"}, badge=7)

    message = _all_messages()[0]
    assert message["data"] == {"type": "news"}
    assert message["badge"] == 7


@responses.activate
def test_a_zero_badge_still_rides_the_message():
    _expo_accepts_all()
    push.send_push_notification(_expo_token("zero"), "T", "B", badge=0)

    assert _all_messages()[0]["badge"] == 0


@responses.activate
def test_a_single_send_omits_data_and_badge_when_they_are_not_given():
    _expo_accepts_all()
    push.send_push_notification(_expo_token("bare"), "T", "B")

    message = _all_messages()[0]
    assert "data" not in message
    assert "badge" not in message


@responses.activate
def test_an_empty_data_dict_is_not_stamped_on_the_message():
    _expo_accepts_all()
    push.send_push_notification(_expo_token("emptydata"), "T", "B", data={})

    assert "data" not in _all_messages()[0]


@responses.activate
def test_an_accepted_single_send_parks_its_ticket_for_the_receipt_poll():
    _expo_accepts_all()
    token = _expo_token("parkme")

    push.send_push_notification(token, "T", "B")

    assert len(push._receipt_queue) == 1
    assert push._receipt_queue[0][:2] == (f"ticket:{token}", token)


@responses.activate
def test_a_single_send_returns_false_when_expo_is_unreachable():
    _expo_raises(requests.exceptions.ConnectionError("no route to exp.host"))

    assert push.send_push_notification(_expo_token("down"), "T", "B") is False
    assert len(push._receipt_queue) == 0


@responses.activate
def test_a_single_send_returns_false_on_a_timeout():
    _expo_raises(requests.exceptions.ReadTimeout("expo took too long"))

    assert push.send_push_notification(_expo_token("slow"), "T", "B") is False


@responses.activate
def test_a_single_send_returns_false_on_an_html_error_page(caplog):
    # An Expo error page answers HTML, not JSON — the status is
    # checked BEFORE the body, so this is reported as the HTTP
    # failure it is and not as a generic send failure
    _expo_replies(text="<html><body>400 Bad Request</body></html>", status=400)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        assert push.send_push_notification(_expo_token("html"), "T", "B") is False

    assert "Expo push HTTP 400" in caplog.text
    assert "Bad Request" in caplog.text


@responses.activate
def test_a_status_outside_the_forcelist_is_never_retried(no_backoff):
    _expo_replies(text="nope", status=400)

    push.send_push_notification(_expo_token("noretry"), "T", "B")

    assert len(responses.calls) == 1


@responses.activate
def test_a_server_error_is_retried_before_the_send_is_given_up_on(no_backoff):
    _expo_replies(text="<html>503</html>", status=503)

    assert push.send_push_notification(_expo_token("retry503"), "T", "B") is False

    # The first attempt plus the three the session is built for
    assert len(responses.calls) == 4


@responses.activate
def test_a_rate_limited_slice_is_retried_rather_than_dropped(no_backoff):
    # Before POST was listed in allowed_methods, urllib3
    # refused to retry a non-idempotent method and a 429 slice
    # was simply lost
    _expo_replies({"data": []}, status=429)

    sent, dead, errors = push._send_slice([{"to": _expo_token("retry429")}], _future())

    assert len(responses.calls) == 4
    assert (sent, dead) == (0, [])
    assert errors == {"RequestFailed": 1}


@responses.activate
def test_a_single_send_returns_false_when_a_two_hundred_is_not_json(caplog):
    _expo_replies(text="not json at all", status=200)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        assert push.send_push_notification(_expo_token("nojson"), "T", "B") is False

    assert "non-JSON" in caplog.text


@responses.activate
def test_a_single_send_returns_false_when_the_body_is_a_json_array(caplog):
    # A bare array where the {"data": ...} envelope belongs:
    # the batch path already guarded this shape, and this one
    # used to raise AttributeError straight into the caller
    _expo_replies([{"status": "ok"}])

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        assert push.send_push_notification(_expo_token("array"), "T", "B") is False

    assert "unexpected body shape" in caplog.text


@responses.activate
def test_a_single_send_returns_false_when_data_is_not_an_object(caplog):
    _expo_replies({"data": ["ok"]})

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        assert push.send_push_notification(_expo_token("listdata"), "T", "B") is False

    assert "unexpected body shape" in caplog.text


@responses.activate
def test_a_device_not_registered_ticket_retires_the_token(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("gone"))
    _expo_replies({"data": {"status": "error", "message": "not registered",
                            "details": {"error": "DeviceNotRegistered"}}})

    assert push.send_push_notification(token, "T", "B") is False

    assert _token_row(db, token)["active"] == 0


@responses.activate
def test_another_ticket_error_is_logged_and_leaves_the_token_alone(app, db, make_user, caplog):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("toobig"))
    _expo_replies({"data": {"status": "error", "message": "message too big",
                            "details": {"error": "MessageTooBig"}}})

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        assert push.send_push_notification(token, "T", "B") is False

    assert _token_row(db, token)["active"] == 1
    assert "MessageTooBig" in caplog.text
    assert push.token_digest(token) in caplog.text
    assert token not in caplog.text


@responses.activate
def test_an_error_ticket_without_details_is_still_a_failure(caplog):
    _expo_replies({"data": {"status": "error", "message": "who knows"}})

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        assert push.send_push_notification(_expo_token("nodetail"), "T", "B") is False

    assert "Expo push error None" in caplog.text


@responses.activate
def test_an_error_ticket_whose_details_are_not_an_object_is_still_a_failure():
    _expo_replies({"data": {"status": "error", "details": "DeviceNotRegistered"}})

    assert push.send_push_notification(_expo_token("strdetail"), "T", "B") is False


@responses.activate
def test_a_failed_single_send_parks_nothing_for_the_receipt_poll():
    _expo_replies({"data": {"status": "error", "details": {"error": "MessageTooBig"},
                            "id": "ticket-should-not-be-parked"}})

    push.send_push_notification(_expo_token("noreceipt"), "T", "B")

    assert len(push._receipt_queue) == 0


@responses.activate
def test_an_accepted_ticket_without_an_id_parks_nothing():
    _expo_replies({"data": {"status": "ok"}})

    assert push.send_push_notification(_expo_token("noid"), "T", "B") is True
    assert len(push._receipt_queue) == 0


@responses.activate
def test_a_single_send_never_reaches_the_database_when_the_ticket_is_fine(monkeypatch):
    _expo_accepts_all()
    monkeypatch.setattr(push, "get_db", _explode)

    assert push.send_push_notification(_expo_token("nodb"), "T", "B") is True








# -----------------------------------------------------------
# _send_slice — one POST of at most 100, no database of its own
# -----------------------------------------------------------

@responses.activate
def test_a_slice_past_the_fan_out_deadline_is_abandoned_without_a_request(caplog):
    _expo_accepts_all()
    batch = [{"to": _expo_token(f"late{i}")} for i in range(4)]

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        sent, dead, errors = push._send_slice(batch, _past())

    assert (sent, dead, errors) == (0, [], {"Abandoned": 4})
    assert len(responses.calls) == 0
    assert "deadline reached" in caplog.text


@responses.activate
def test_a_slice_reports_the_http_status_it_got(caplog):
    _expo_replies(text="<html>Bad Request</html>", status=400)
    batch = [{"to": _expo_token("s1")}, {"to": _expo_token("s2")}]

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        sent, dead, errors = push._send_slice(batch, _future())

    assert (sent, dead, errors) == (0, [], {"HTTP 400": 2})
    assert "Expo batch push HTTP 400" in caplog.text


@responses.activate
def test_a_slice_reports_a_request_that_never_completed():
    _expo_raises(requests.exceptions.ConnectionError("boom"))

    sent, dead, errors = push._send_slice([{"to": _expo_token("rf")}], _future())

    assert (sent, dead, errors) == (0, [], {"RequestFailed": 1})


@responses.activate
def test_a_slice_reports_a_timeout_as_a_request_failure():
    _expo_raises(requests.exceptions.ReadTimeout("expo took too long"))

    sent, dead, errors = push._send_slice([{"to": _expo_token("to")}], _future())

    assert errors == {"RequestFailed": 1}


@responses.activate
def test_a_slice_reports_a_two_hundred_that_is_not_json_as_a_request_failure():
    _expo_replies(text="<html>hello</html>", status=200)

    sent, dead, errors = push._send_slice([{"to": _expo_token("nj")}], _future())

    assert errors == {"RequestFailed": 1}


@responses.activate
def test_a_slice_reports_a_top_level_expo_rejection(caplog):
    _expo_replies({"errors": [{"code": "PUSH_TOO_MANY_EXPERIENCE_IDS",
                               "message": "malformed batch"}]})
    batch = [{"to": _expo_token(f"rej{i}")} for i in range(3)]

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        sent, dead, errors = push._send_slice(batch, _future())

    assert (sent, dead, errors) == (0, [], {"Rejected": 3})
    assert "rejected" in caplog.text


@responses.activate
def test_a_slice_reports_a_body_that_is_not_an_object():
    _expo_replies("just a string")

    sent, dead, errors = push._send_slice([{"to": _expo_token("str")}], _future())

    assert errors == {"Malformed": 1}


@responses.activate
def test_a_slice_reports_a_data_field_that_is_not_a_list():
    _expo_replies({"data": {"status": "ok"}})

    sent, dead, errors = push._send_slice([{"to": _expo_token("obj")}], _future())

    assert errors == {"Malformed": 1}


@responses.activate
def test_a_slice_counts_ok_tickets_and_collects_the_dead_ones():
    alive, dead_token = _expo_token("alive"), _expo_token("dead")
    _expo_replies({"data": [
        {"status": "ok", "id": "ticket-alive"},
        {"status": "error", "message": "not registered",
         "details": {"error": "DeviceNotRegistered"}},
    ]})

    sent, dead, errors = push._send_slice([{"to": alive}, {"to": dead_token}], _future())

    assert sent == 1
    assert dead == [dead_token]
    assert errors == {"DeviceNotRegistered": 1}


@responses.activate
def test_a_slice_never_opens_a_database_connection_of_its_own(monkeypatch):
    # The caller retires the whole batch in ONE update; a slice
    # that connected from inside the ticket loop was the bug
    # this shape replaced
    monkeypatch.setattr(push, "get_db", _explode)
    _expo_replies({"data": [{"status": "error", "details": {"error": "DeviceNotRegistered"}}]})

    sent, dead, errors = push._send_slice([{"to": _expo_token("nodb")}], _future())

    assert dead == [_expo_token("nodb")]


@responses.activate
def test_a_slice_tallies_an_error_code_it_has_never_heard_of(caplog):
    _expo_replies({"data": [{"status": "error", "message": "brand new",
                             "details": {"error": "SomethingNovel"}}]})

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        sent, dead, errors = push._send_slice([{"to": _expo_token("novel")}], _future())

    assert errors == {"SomethingNovel": 1}
    assert dead == []
    assert "SomethingNovel" in caplog.text


@responses.activate
def test_a_slice_tallies_an_error_ticket_without_details_as_unknown():
    _expo_replies({"data": [{"status": "error", "message": "no details here"}]})

    sent, dead, errors = push._send_slice([{"to": _expo_token("unk")}], _future())

    assert errors == {"Unknown": 1}


@responses.activate
def test_a_slice_tallies_an_error_ticket_whose_details_are_not_an_object():
    _expo_replies({"data": [{"status": "error", "details": ["DeviceNotRegistered"]}]})

    sent, dead, errors = push._send_slice([{"to": _expo_token("baddet")}], _future())

    assert errors == {"Unknown": 1}
    assert dead == []


@responses.activate
def test_a_slice_tallies_a_ticket_that_is_not_an_object_and_keeps_going():
    _expo_replies({"data": ["nonsense", {"status": "ok", "id": "t2"}]})

    sent, dead, errors = push._send_slice(
        [{"to": _expo_token("bad")}, {"to": _expo_token("good")}], _future())

    assert sent == 1
    assert errors == {"Malformed": 1}


@responses.activate
def test_a_slice_ignores_tickets_beyond_the_messages_it_sent(caplog):
    _expo_replies({"data": [{"status": "ok", "id": "t1"},
                            {"status": "ok", "id": "t2"},
                            {"status": "ok", "id": "t3"}]})

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        sent, dead, errors = push._send_slice([{"to": _expo_token("only")}], _future())

    assert sent == 1
    assert "ignoring the tail" in caplog.text


@responses.activate
def test_a_slice_survives_a_message_with_no_recipient_on_it():
    # batch[idx]["to"] is the one unguarded subscript — a
    # message without a "to" must cost its own entry, not the
    # rest of the slice
    _expo_replies({"data": [{"status": "ok", "id": "t1"}, {"status": "ok", "id": "t2"}]})

    sent, dead, errors = push._send_slice([{}, {"to": _expo_token("fine")}], _future())

    assert sent == 1
    assert errors == {"Malformed": 1}


@responses.activate
def test_a_slice_maps_each_verdict_to_its_own_token_positionally():
    tokens = [_expo_token(f"pos{i}") for i in range(4)]
    _expo_replies({"data": [
        {"status": "ok", "id": "t0"},
        {"status": "error", "details": {"error": "DeviceNotRegistered"}},
        {"status": "ok", "id": "t2"},
        {"status": "error", "details": {"error": "DeviceNotRegistered"}},
    ]})

    sent, dead, errors = push._send_slice([{"to": t} for t in tokens], _future())

    assert sent == 2
    assert dead == [tokens[1], tokens[3]]


@responses.activate
def test_a_slice_parks_every_accepted_ticket():
    tokens = [_expo_token(f"park{i}") for i in range(3)]
    _expo_replies({"data": [{"status": "ok", "id": f"t{i}"} for i in range(3)]})

    push._send_slice([{"to": t} for t in tokens], _future())

    assert [entry[0] for entry in push._receipt_queue] == ["t0", "t1", "t2"]








# -----------------------------------------------------------
# send_push_batch — slices of 100, one deadline, one tally
# -----------------------------------------------------------

@responses.activate
def test_a_batch_of_no_tokens_makes_no_request_at_all():
    _expo_accepts_all()

    assert push.send_push_batch([], "T", "B") == 0
    assert len(responses.calls) == 0


@responses.activate
def test_a_batch_sends_one_message_per_token_in_one_request():
    tokens = [_expo_token(f"b{i}") for i in range(5)]
    _expo_accepts_all()

    assert push.send_push_batch(tokens, "Sveiki", "Tekstas") == 5

    assert len(_push_bodies()) == 1
    assert _sent_tokens() == sorted(tokens)
    assert all(msg["title"] == "Sveiki" for msg in _all_messages())


@responses.activate
def test_a_batch_stamps_data_priority_and_ttl_on_every_message():
    _expo_accepts_all()

    push.send_push_batch([_expo_token("p1"), _expo_token("p2")], "T", "B",
                         data={"type": "chat_message"}, priority="high", ttl=3600)

    for message in _all_messages():
        assert message["data"] == {"type": "chat_message"}
        assert message["priority"] == "high"
        assert message["ttl"] == 3600


@responses.activate
def test_a_batch_omits_priority_and_ttl_when_the_caller_gives_neither():
    _expo_accepts_all()

    push.send_push_batch([_expo_token("np")], "T", "B")

    message = _all_messages()[0]
    assert "priority" not in message
    assert "ttl" not in message
    assert "data" not in message


@responses.activate
def test_a_zero_ttl_still_rides_the_message():
    _expo_accepts_all()

    push.send_push_batch([_expo_token("ttl0")], "T", "B", ttl=0)

    assert _all_messages()[0]["ttl"] == 0


@responses.activate
def test_exactly_a_hundred_tokens_are_one_slice():
    _expo_accepts_all()
    tokens = [_expo_token(f"h{i:03d}") for i in range(push._SEND_SLICE)]

    assert push.send_push_batch(tokens, "T", "B") == 100

    assert len(_push_bodies()) == 1
    assert len(_push_bodies()[0]) == 100


@responses.activate
def test_one_token_past_the_cap_splits_into_two_slices():
    _expo_accepts_all()
    tokens = [_expo_token(f"h{i:03d}") for i in range(push._SEND_SLICE + 1)]

    assert push.send_push_batch(tokens, "T", "B") == 101

    assert sorted(len(body) for body in _push_bodies()) == [1, 100]


@responses.activate
def test_a_broadcast_fans_out_in_slices_of_a_hundred_addressing_every_token_once():
    _expo_accepts_all()
    tokens = [_expo_token(f"f{i:03d}") for i in range(250)]

    assert push.send_push_batch(tokens, "T", "B") == 250

    assert sorted(len(body) for body in _push_bodies()) == [50, 100, 100]
    assert _sent_tokens() == sorted(tokens)


@responses.activate
def test_a_batch_counts_accepted_tickets_not_messages_sent():
    _expo_replies({"data": [{"status": "ok", "id": "t0"},
                            {"status": "error", "details": {"error": "MessageTooBig"}},
                            {"status": "ok", "id": "t2"}]})

    assert push.send_push_batch([_expo_token(f"c{i}") for i in range(3)], "T", "B") == 2


@responses.activate
def test_a_batch_fills_the_stats_dict_a_reporting_caller_passes():
    _expo_replies({"data": [{"status": "ok", "id": "t0"},
                            {"status": "error", "details": {"error": "MessageTooBig"}}]})
    stats = {}

    push.send_push_batch([_expo_token("s0"), _expo_token("s1")], "T", "B", stats=stats)

    assert stats == {"sent": 1, "failed": 1, "errors": {"MessageTooBig": 1}}


@responses.activate
def test_a_second_batch_adds_to_the_same_stats_dict():
    _expo_accepts_all()
    stats = {}

    push.send_push_batch([_expo_token("a1")], "T", "B", stats=stats)
    push.send_push_batch([_expo_token("a2")], "T", "B", stats=stats)

    assert stats["sent"] == 2
    assert stats["failed"] == 0
    assert stats["errors"] == {}


@responses.activate
def test_a_batch_with_no_stats_dict_still_returns_the_count():
    _expo_accepts_all()

    assert push.send_push_batch([_expo_token("nostats")], "T", "B", stats=None) == 1


@responses.activate
def test_a_batch_retires_every_dead_device_in_one_update(app, db, make_user, monkeypatch):
    user = make_user()
    dead_one = _seed_token(db, user["id"], _expo_token("d1"))
    dead_two = _seed_token(db, user["id"], _expo_token("d2"))
    alive = _seed_token(db, user["id"], _expo_token("d3"))

    calls = []
    real = push._deactivate_tokens

    def _watched(tokens):
        calls.append(list(tokens))
        return real(tokens)

    monkeypatch.setattr(push, "_deactivate_tokens", _watched)
    _expo_replies({"data": [{"status": "error", "details": {"error": "DeviceNotRegistered"}},
                            {"status": "error", "details": {"error": "DeviceNotRegistered"}},
                            {"status": "ok", "id": "t3"}]})

    push.send_push_batch([dead_one, dead_two, alive], "T", "B")

    assert len(calls) == 1
    assert sorted(calls[0]) == sorted([dead_one, dead_two])
    assert _token_row(db, dead_one)["active"] == 0
    assert _token_row(db, dead_two)["active"] == 0
    assert _token_row(db, alive)["active"] == 1


@responses.activate
def test_a_batch_touches_no_database_when_nothing_died(monkeypatch):
    _expo_accepts_all()
    monkeypatch.setattr(push, "get_db", _explode)

    assert push.send_push_batch([_expo_token("live")], "T", "B") == 1


@responses.activate
def test_a_batch_returns_zero_when_expo_is_unreachable():
    _expo_raises(requests.exceptions.ConnectionError("no network"))

    assert push.send_push_batch([_expo_token("gone")], "T", "B") == 0


@responses.activate
def test_a_batch_logs_its_error_tally_once(caplog):
    _expo_replies({"data": [{"status": "error", "details": {"error": "MessageTooBig"}},
                            {"status": "error", "details": {"error": "InvalidCredentials"}}]})

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        push.send_push_batch([_expo_token("e1"), _expo_token("e2")], "T", "B")

    assert "InvalidCredentials=1, MessageTooBig=1" in caplog.text


@responses.activate
def test_a_batch_past_the_fan_out_deadline_sends_nothing_and_says_so(monkeypatch):
    _expo_accepts_all()
    monkeypatch.setattr(push, "_FANOUT_DEADLINE", -1)
    stats = {}

    assert push.send_push_batch([_expo_token("dl")], "T", "B", stats=stats) == 0

    assert len(responses.calls) == 0
    assert stats["errors"] == {"Abandoned": 1}


def test_the_fan_out_deadline_is_bounded_so_a_scheduler_tick_cannot_hang():
    assert 0 < push._FANOUT_DEADLINE <= 300
    assert 0 < push._FANOUT_WORKERS <= 8








# -----------------------------------------------------------
# _deactivate_tokens — the whole batch in one UPDATE
# -----------------------------------------------------------

def test_deactivating_a_token_flips_the_row_and_refreshes_its_age(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("flip"))

    assert push._deactivate_tokens([token]) == 1

    row = _token_row(db, token)
    assert row["active"] == 0
    # Naive-UTC isoformat, the shape register_token stamps
    assert "T" in row["updated_at"]


def test_deactivating_keeps_the_row_so_the_next_register_can_revive_it(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("revive"))

    push._deactivate_tokens([token])

    assert _token_row(db, token) is not None


def test_deactivating_nothing_returns_zero_without_a_query(monkeypatch):
    monkeypatch.setattr(push, "get_db", _explode)

    assert push._deactivate_tokens([]) == 0
    assert push._deactivate_tokens([None, "", None]) == 0


def test_deactivating_the_same_token_twice_moves_one_row(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("dupe"))

    assert push._deactivate_tokens([token, token, token]) == 1


def test_deactivating_a_token_nobody_registered_changes_nothing(app, db):
    assert push._deactivate_tokens([_expo_token("stranger")]) == 0


def test_deactivating_several_tokens_at_once_moves_them_all(app, db, make_user):
    user = make_user()
    tokens = [_seed_token(db, user["id"], _expo_token(f"m{i}")) for i in range(3)]

    assert push._deactivate_tokens(tokens) == 3
    assert all(_token_row(db, t)["active"] == 0 for t in tokens)


def test_deactivating_a_token_that_is_already_inactive_is_harmless(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("already"), active=0)

    assert push._deactivate_tokens([token]) == 1
    assert _token_row(db, token)["active"] == 0


def test_deactivating_returns_zero_when_the_database_is_gone(monkeypatch, caplog):
    monkeypatch.setattr(push, "get_db", _explode)

    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        assert push._deactivate_tokens([_expo_token("nodb")]) == 0

    assert "Failed to deactivate tokens" in caplog.text


def test_deactivating_logs_digests_and_never_a_raw_token(app, db, make_user, caplog):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("hygiene"))

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        push._deactivate_tokens([token])

    assert token not in caplog.text
    assert push.token_digest(token) in caplog.text


def test_deactivating_caps_the_digests_it_logs(app, db, make_user, caplog):
    user = make_user()
    tokens = [_seed_token(db, user["id"], _expo_token(f"many{i:02d}")) for i in range(25)]

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        assert push._deactivate_tokens(tokens) == 25

    assert "Deactivated 25 unregistered token(s)" in caplog.text
    # The line names the first twenty digests, not all of them
    assert sum(1 for t in tokens if push.token_digest(t) in caplog.text) == 20








# -----------------------------------------------------------
# poll_push_receipts — stage two, fifteen minutes later
# -----------------------------------------------------------

@responses.activate
def test_an_empty_receipt_queue_asks_expo_nothing():
    _expo_receipts({"data": {}})

    assert push.poll_push_receipts() == 0
    assert len(responses.calls) == 0


@responses.activate
def test_a_ticket_younger_than_the_delay_stays_queued():
    _expo_receipts({"data": {}})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-young", _expo_token("young"))
        traveller.shift(push._RECEIPT_DELAY - 1)

        assert push.poll_push_receipts() == 0

    assert len(responses.calls) == 0
    assert len(push._receipt_queue) == 1


@responses.activate
def test_a_ticket_exactly_at_the_delay_is_due():
    _expo_receipts({"data": {"ticket-edge": {"status": "ok"}}})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-edge", _expo_token("edge"))
        traveller.shift(push._RECEIPT_DELAY)

        assert push.poll_push_receipts() == 1

    assert _receipt_bodies() == [{"ids": ["ticket-edge"]}]


@responses.activate
def test_a_due_ticket_is_traded_for_its_receipt_and_leaves_the_queue():
    _expo_receipts({"data": {"ticket-old": {"status": "ok"}}})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-old", _expo_token("old"))
        traveller.shift(push._RECEIPT_DELAY + 1)

        assert push.poll_push_receipts() == 1
        assert len(push._receipt_queue) == 0
        # Nothing left to ask about
        assert push.poll_push_receipts() == 0

    assert len(_receipt_bodies()) == 1


@responses.activate
def test_the_young_tickets_go_back_while_the_old_ones_go_out():
    _expo_receipts({"data": {"ticket-old": {"status": "ok"}}})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-old", _expo_token("old"))
        traveller.shift(push._RECEIPT_DELAY + 1)
        push._queue_receipt("ticket-new", _expo_token("new"))

        assert push.poll_push_receipts() == 1

    assert [entry[0] for entry in push._receipt_queue] == ["ticket-new"]


@responses.activate
def test_a_device_not_registered_receipt_retires_the_token(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("uninstalled"))
    _expo_receipts({"data": {"ticket-dead": {
        "status": "error", "message": "not registered",
        "details": {"error": "DeviceNotRegistered"}}}})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-dead", token)
        traveller.shift(push._RECEIPT_DELAY + 1)

        assert push.poll_push_receipts() == 1

    assert _token_row(db, token)["active"] == 0


@responses.activate
def test_broken_expo_credentials_are_shouted_about_rather_than_swallowed(caplog):
    _expo_receipts({"data": {"ticket-cred": {
        "status": "error", "message": "apns key is invalid",
        "details": {"error": "InvalidCredentials"}}}})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-cred", _expo_token("cred"))
        traveller.shift(push._RECEIPT_DELAY + 1)

        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            push.poll_push_receipts()

    assert "InvalidCredentials" in caplog.text


@responses.activate
def test_a_rate_limited_receipt_is_logged_and_keeps_the_device(app, db, make_user, caplog):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("rate"))
    _expo_receipts({"data": {"ticket-rate": {
        "status": "error", "message": "slow down",
        "details": {"error": "MessageRateExceeded"}}}})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-rate", token)
        traveller.shift(push._RECEIPT_DELAY + 1)

        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            push.poll_push_receipts()

    assert "MessageRateExceeded" in caplog.text
    assert _token_row(db, token)["active"] == 1
    assert token not in caplog.text
    assert push.token_digest(token) in caplog.text


@responses.activate
def test_a_receipt_error_without_details_is_logged_as_unknown(caplog):
    _expo_receipts({"data": {"ticket-u": {"status": "error", "message": "?"}}})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-u", _expo_token("u"))
        traveller.shift(push._RECEIPT_DELAY + 1)

        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            assert push.poll_push_receipts() == 1

    assert "Expo receipt error Unknown" in caplog.text


@responses.activate
def test_a_receipt_for_a_ticket_we_never_parked_names_no_device(caplog):
    _expo_receipts({"data": {
        "ticket-known": {"status": "ok"},
        "ticket-stranger": {"status": "error", "details": {"error": "MessageTooBig"}},
    }})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-known", _expo_token("known"))
        traveller.shift(push._RECEIPT_DELAY + 1)

        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            assert push.poll_push_receipts() == 2

    assert "token:unknown" in caplog.text


@responses.activate
def test_a_receipt_that_is_not_an_object_is_counted_and_skipped():
    _expo_receipts({"data": {"ticket-weird": "not an object"}})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-weird", _expo_token("weird"))
        traveller.shift(push._RECEIPT_DELAY + 1)

        assert push.poll_push_receipts() == 1


@responses.activate
def test_a_receipt_slice_that_answers_non_200_costs_only_itself(caplog):
    _expo_receipts(text="<html>400</html>", status=400)

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-400", _expo_token("400"))
        traveller.shift(push._RECEIPT_DELAY + 1)

        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            assert push.poll_push_receipts() == 0

    assert "Expo receipts HTTP 400" in caplog.text


@responses.activate
def test_a_receipt_slice_whose_request_failed_is_skipped(caplog):
    _expo_receipts(exc=requests.exceptions.ConnectionError("no network"))

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-conn", _expo_token("conn"))
        traveller.shift(push._RECEIPT_DELAY + 1)

        with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
            assert push.poll_push_receipts() == 0

    assert "Failed to fetch push receipts" in caplog.text


@responses.activate
def test_a_receipt_body_that_is_not_an_object_is_skipped(caplog):
    _expo_receipts({"data": ["not", "a", "map"]})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-shape", _expo_token("shape"))
        traveller.shift(push._RECEIPT_DELAY + 1)

        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            assert push.poll_push_receipts() == 0

    assert "unexpected body shape" in caplog.text


@responses.activate
def test_receipts_are_asked_for_in_slices_of_three_hundred():
    _expo_receipts({"data": {}})
    total = push._RECEIPT_SLICE + 1

    with time_machine.travel(FROZEN, tick=False) as traveller:
        for i in range(total):
            push._queue_receipt(f"ticket-{i:04d}", _expo_token(f"r{i:04d}"))
        traveller.shift(push._RECEIPT_DELAY + 1)

        push.poll_push_receipts()

    bodies = _receipt_bodies()
    assert [len(body["ids"]) for body in bodies] == [push._RECEIPT_SLICE, 1]


@responses.activate
def test_a_receipt_poll_reports_how_many_devices_it_retired(app, db, make_user, caplog):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("retired"))
    _expo_receipts({"data": {"ticket-r": {"status": "error",
                                          "details": {"error": "DeviceNotRegistered"}}}})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-r", token)
        traveller.shift(push._RECEIPT_DELAY + 1)

        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            push.poll_push_receipts()

    assert "1 checked, 1 device(s) retired" in caplog.text








# -----------------------------------------------------------
# prune_orphan_push_tokens — no live session, no push
# -----------------------------------------------------------

def test_pruning_drops_the_tokens_of_a_user_with_no_session(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("orphan"))

    assert push.prune_orphan_push_tokens() == 1
    assert _token_row(db, token) is None


def test_pruning_keeps_the_tokens_of_a_user_who_is_still_logged_in(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("livesession"))
    _seed_session(db, user["id"], (datetime.now(timezone.utc) + timedelta(days=30)).isoformat())

    assert push.prune_orphan_push_tokens() == 0
    assert _token_row(db, token) is not None


def test_pruning_drops_a_token_whose_only_session_has_run_out(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("expired"))
    _seed_session(db, user["id"], (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())

    assert push.prune_orphan_push_tokens() == 1
    assert _token_row(db, token) is None


def test_a_thirty_day_session_stops_covering_the_token_once_it_lapses(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("lapse"))
    _seed_session(db, user["id"], (datetime.now(timezone.utc) + timedelta(days=30)).isoformat())

    with time_machine.travel(datetime.now(timezone.utc) + timedelta(days=31), tick=False):
        assert push.prune_orphan_push_tokens() == 1

    assert _token_row(db, token) is None


def test_pruning_one_users_orphans_leaves_another_users_tokens_alone(app, db, make_user):
    stayer, leaver = make_user(), make_user()
    kept = _seed_token(db, stayer["id"], _expo_token("kept"))
    dropped = _seed_token(db, leaver["id"], _expo_token("dropped"))
    _seed_session(db, stayer["id"], (datetime.now(timezone.utc) + timedelta(days=1)).isoformat())

    assert push.prune_orphan_push_tokens() == 1
    assert _token_row(db, kept) is not None
    assert _token_row(db, dropped) is None


def test_pruning_keeps_every_device_of_a_user_who_is_still_logged_in(app, db, make_user):
    user = make_user()
    phone = _seed_token(db, user["id"], _expo_token("phone"))
    tablet = _seed_token(db, user["id"], _expo_token("tablet"))
    _seed_session(db, user["id"], (datetime.now(timezone.utc) + timedelta(days=1)).isoformat())

    assert push.prune_orphan_push_tokens() == 0
    assert _token_row(db, phone) is not None
    assert _token_row(db, tablet) is not None


def test_pruning_an_empty_table_returns_zero(app, db):
    assert push.prune_orphan_push_tokens() == 0


def test_pruning_returns_zero_when_the_database_is_gone(monkeypatch, caplog):
    monkeypatch.setattr(push, "get_db", _explode)

    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        assert push.prune_orphan_push_tokens() == 0

    assert "Failed to prune push tokens" in caplog.text


def test_pruning_says_how_many_rows_it_removed(app, db, make_user, caplog):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("logged"))

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        push.prune_orphan_push_tokens()

    assert "Pruned 1 push token(s)" in caplog.text








# -----------------------------------------------------------
# _send_by_language — the copy follows push_tokens.language
# -----------------------------------------------------------

@responses.activate
def test_no_rows_means_no_request_at_all():
    _expo_accepts_all()

    assert push._send_by_language("news", [], "T", "B", None, None, None, None) == 0
    assert len(responses.calls) == 0


@responses.activate
def test_english_devices_get_the_english_copy_and_the_rest_get_lithuanian(app, db, make_user):
    lt_user, en_user = make_user(), make_user()
    lt_token = _seed_token(db, lt_user["id"], _expo_token("lt"), language="lt")
    en_token = _seed_token(db, en_user["id"], _expo_token("en"), language="en")
    _expo_accepts_all()

    sent = push.notify_channel("news", "Naujiena", "Tekstas",
                               title_en="News", body_en="Text")

    assert sent == 2
    by_token = {msg["to"]: msg for msg in _all_messages()}
    assert by_token[lt_token]["title"] == "Naujiena"
    assert by_token[lt_token]["body"] == "Tekstas"
    assert by_token[en_token]["title"] == "News"
    assert by_token[en_token]["body"] == "Text"


@responses.activate
def test_the_two_languages_go_out_as_two_separate_batches(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("l1"), language="lt")
    _seed_token(db, user["id"], _expo_token("e1"), language="en")
    _expo_accepts_all()

    push.notify_channel("news", "Naujiena", "Tekstas", title_en="News", body_en="Text")

    assert len(_push_bodies()) == 2


@responses.activate
def test_a_device_language_that_is_neither_rides_the_lithuanian_batch(app, db, make_user):
    user = make_user()
    other = _seed_token(db, user["id"], _expo_token("de"), language="de")
    _expo_accepts_all()

    push.notify_channel("news", "Naujiena", "Tekstas", title_en="News", body_en="Text")

    assert _all_messages()[0]["to"] == other
    assert _all_messages()[0]["title"] == "Naujiena"


@responses.activate
def test_without_a_translation_every_device_takes_one_lithuanian_batch(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("nolt"), language="lt")
    _seed_token(db, user["id"], _expo_token("noen"), language="en")
    _expo_accepts_all()

    assert push.notify_channel("admin", "Skelbimas", "Tekstas") == 2

    # The admin types one text and nothing translates it —
    # still two batches, both carrying the same copy
    assert {msg["title"] for msg in _all_messages()} == {"Skelbimas"}


@responses.activate
def test_a_missing_english_body_alone_falls_back_to_the_lithuanian_one(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("half"), language="en")
    _expo_accepts_all()

    push.notify_channel("news", "Naujiena", "Tekstas", title_en="News")

    message = _all_messages()[0]
    assert message["title"] == "News"
    assert message["body"] == "Tekstas"


@responses.activate
def test_the_channel_is_stamped_on_the_payload(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("stamp"))
    _expo_accepts_all()

    push.notify_channel("schedule", "T", "B", data={"type": "schedule_update"})

    assert _all_messages()[0]["data"] == {"type": "schedule_update", "channel": "schedule"}


@responses.activate
def test_the_callers_data_dict_never_grows_a_channel_key(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("copy"))
    _expo_accepts_all()
    caller_data = {"type": "news"}

    push.notify_channel("news", "T", "B", data=caller_data)

    assert caller_data == {"type": "news"}


@responses.activate
def test_a_channel_stamp_appears_even_without_caller_data(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("onlychan"))
    _expo_accepts_all()

    push.notify_channel("news", "T", "B")

    assert _all_messages()[0]["data"] == {"channel": "news"}


@responses.activate
def test_a_chat_preview_wakes_a_dozing_phone(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("chatprio"))
    _expo_accepts_all()

    push.notify_channel_users("chat", [user["id"]], "Vardas", "Labas")

    message = _all_messages()[0]
    assert message["priority"] == "high"
    assert message["ttl"] == 3600


@pytest.mark.parametrize("channel", ["news", "schedule", "admin"])
@responses.activate
def test_the_other_channels_take_the_default_priority_and_a_day_of_ttl(app, db, make_user, channel):
    user = make_user()
    _seed_token(db, user["id"], _expo_token(f"prio{channel}"))
    _expo_accepts_all()

    push.notify_channel(channel, "T", "B")

    message = _all_messages()[0]
    assert "priority" not in message
    assert message["ttl"] == 86400








# -----------------------------------------------------------
# notify_channel_users — many users, one query, opt-outs honoured
# -----------------------------------------------------------

@responses.activate
def test_an_unknown_channel_sends_nothing_at_all(app, db, make_user, caplog):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("unknownchan"))
    _expo_accepts_all()

    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        assert push.notify_channel_users("marketing", [user["id"]], "T", "B") == 0

    assert len(responses.calls) == 0
    assert "unknown channel" in caplog.text


@responses.activate
def test_an_empty_recipient_list_sends_nothing(app):
    _expo_accepts_all()

    assert push.notify_channel_users("chat", [], "T", "B") == 0
    assert push.notify_channel_users("chat", None, "T", "B") == 0
    assert len(responses.calls) == 0


@responses.activate
def test_recipient_ids_that_are_empty_are_dropped(app):
    _expo_accepts_all()

    assert push.notify_channel_users("chat", [None, "", None], "T", "B") == 0
    assert len(responses.calls) == 0


@responses.activate
def test_a_repeated_recipient_id_is_addressed_once(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("dupeid"))
    _expo_accepts_all()

    assert push.notify_channel_users("chat", [user["id"]] * 4, "T", "B") == 1
    assert _sent_tokens() == [token]


@responses.activate
def test_a_user_who_disabled_the_channel_is_skipped(app, db, make_user):
    optout, listener = make_user(), make_user()
    _seed_token(db, optout["id"], _expo_token("silent"))
    kept = _seed_token(db, listener["id"], _expo_token("loud"))
    _set_channel(db, optout["id"], "chat", 0)
    _expo_accepts_all()

    assert push.notify_channel_users("chat", [optout["id"], listener["id"]], "T", "B") == 1
    assert _sent_tokens() == [kept]


@responses.activate
def test_a_user_who_never_touched_the_switches_still_hears_it(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("default"))
    _expo_accepts_all()

    assert push.notify_channel_users("chat", [user["id"]], "T", "B") == 1
    assert _sent_tokens() == [token]


@responses.activate
def test_an_explicitly_enabled_channel_is_delivered(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("explicit"))
    _set_channel(db, user["id"], "chat", 1)
    _expo_accepts_all()

    assert push.notify_channel_users("chat", [user["id"]], "T", "B") == 1
    assert _sent_tokens() == [token]


@responses.activate
def test_an_opt_out_on_another_channel_does_not_silence_this_one(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("othermute"))
    _set_channel(db, user["id"], "news", 0)
    _expo_accepts_all()

    assert push.notify_channel_users("chat", [user["id"]], "T", "B") == 1
    assert _sent_tokens() == [token]


@responses.activate
def test_a_deactivated_token_row_is_not_addressed(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("retiredrow"), active=0)
    live = _seed_token(db, user["id"], _expo_token("liverow"), active=1)
    _expo_accepts_all()

    assert push.notify_channel_users("chat", [user["id"]], "T", "B") == 1
    assert _sent_tokens() == [live]


@responses.activate
def test_a_recipient_with_no_device_costs_no_request(app, db, make_user):
    user = make_user()
    _expo_accepts_all()

    assert push.notify_channel_users("chat", [user["id"]], "T", "B") == 0
    assert len(responses.calls) == 0


@responses.activate
def test_the_count_is_devices_not_users(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("dev1"))
    _seed_token(db, user["id"], _expo_token("dev2"))
    _expo_accepts_all()

    assert push.notify_channel_users("chat", [user["id"]], "T", "B") == 2


@responses.activate
def test_the_recipient_list_is_chunked(app, db, monkeypatch):
    users = _bulk_users_with_tokens(db, 5, "chunk")
    monkeypatch.setattr(push, "_ID_CHUNK", 2)
    _expo_accepts_all()

    sent = push.notify_channel_users("chat", [uid for uid, _ in users], "T", "B")

    assert sent == 5
    assert _sent_tokens() == sorted(token for _, token in users)


@pytest.mark.slow
@responses.activate
def test_a_recipient_set_past_the_chunk_size_still_reaches_every_device(app, db):
    users = _bulk_users_with_tokens(db, push._ID_CHUNK + 1, "big")
    _expo_accepts_all()

    sent = push.notify_channel_users("chat", [uid for uid, _ in users], "T", "B")

    assert sent == push._ID_CHUNK + 1
    assert _sent_tokens() == sorted(token for _, token in users)


@responses.activate
def test_notify_channel_users_fills_the_stats_dict(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("statsuser"))
    _expo_accepts_all()
    stats = {}

    push.notify_channel_users("chat", [user["id"]], "T", "B", stats=stats)

    assert stats["sent"] == 1
    assert stats["failed"] == 0


@responses.activate
def test_a_dead_device_reported_during_a_fan_out_is_retired(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("fanoutdead"))
    _expo_replies({"data": [{"status": "error", "details": {"error": "DeviceNotRegistered"}}]})

    assert push.notify_channel_users("chat", [user["id"]], "T", "B") == 0
    assert _token_row(db, token)["active"] == 0








# -----------------------------------------------------------
# notify_channel_user — the single-recipient shape
# -----------------------------------------------------------

@responses.activate
def test_one_user_gets_every_device_they_registered(app, db, make_user):
    user = make_user()
    first = _seed_token(db, user["id"], _expo_token("u1a"))
    second = _seed_token(db, user["id"], _expo_token("u1b"))
    _expo_accepts_all()

    assert push.notify_channel_user("chat", user["id"], "T", "B") == 2
    assert _sent_tokens() == sorted([first, second])


@responses.activate
def test_the_single_recipient_shape_honours_the_opt_out(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("u1mute"))
    _set_channel(db, user["id"], "chat", 0)
    _expo_accepts_all()

    assert push.notify_channel_user("chat", user["id"], "T", "B") == 0
    assert len(responses.calls) == 0


@responses.activate
def test_the_single_recipient_shape_refuses_an_unknown_channel(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("u1chan"))
    _expo_accepts_all()

    assert push.notify_channel_user("nonsense", user["id"], "T", "B") == 0
    assert len(responses.calls) == 0


@responses.activate
def test_the_single_recipient_shape_passes_the_translation_through(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("u1en"), language="en")
    _expo_accepts_all()

    push.notify_channel_user("news", user["id"], "Naujiena", "Tekstas",
                             title_en="News", body_en="Text")

    assert _all_messages()[0]["title"] == "News"








# -----------------------------------------------------------
# notify_channel — the broadcast, and the skip
# -----------------------------------------------------------

@responses.activate
def test_a_broadcast_on_an_unknown_channel_is_refused(app, db, make_user, caplog):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("bcastunknown"))
    _expo_accepts_all()

    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        assert push.notify_channel("gossip", "T", "B") == 0

    assert len(responses.calls) == 0
    assert "Refusing broadcast on unknown channel" in caplog.text


@pytest.mark.parametrize("channel", list(push.VALID_CHANNELS))
@responses.activate
def test_every_valid_channel_is_accepted(app, db, make_user, channel):
    user = make_user()
    _seed_token(db, user["id"], _expo_token(f"valid{channel}"))
    _expo_accepts_all()

    assert push.notify_channel(channel, "T", "B") == 1


def test_the_valid_channels_match_the_database_check_constraint(app, db):
    rows = db.execute("SELECT sql FROM sqlite_master WHERE name = 'notification_channels'").fetchone()

    for channel in push.VALID_CHANNELS:
        assert f"'{channel}'" in rows["sql"]


@responses.activate
def test_a_broadcast_reaches_every_active_device_on_the_faculty(app, db, make_user):
    one, two = make_user(), make_user()
    first = _seed_token(db, one["id"], _expo_token("all1"))
    second = _seed_token(db, two["id"], _expo_token("all2"))
    _expo_accepts_all()

    assert push.notify_channel("news", "T", "B") == 2
    assert _sent_tokens() == sorted([first, second])


@responses.activate
def test_a_broadcast_skips_the_device_of_the_excluded_user(app, db, make_user):
    author, reader = make_user(), make_user()
    _seed_token(db, author["id"], _expo_token("author"))
    theirs = _seed_token(db, reader["id"], _expo_token("reader"))
    _expo_accepts_all()

    sent = push.notify_channel("news", "T", "B", exclude_user_id=author["id"])

    assert sent == 1
    assert _sent_tokens() == [theirs]


@responses.activate
def test_excluding_a_user_who_has_no_device_changes_nothing(app, db, make_user):
    reader, stranger = make_user(), make_user()
    theirs = _seed_token(db, reader["id"], _expo_token("stillhere"))
    _expo_accepts_all()

    assert push.notify_channel("news", "T", "B", exclude_user_id=stranger["id"]) == 1
    assert _sent_tokens() == [theirs]


@responses.activate
def test_a_broadcast_skips_a_user_who_opted_out_of_that_channel(app, db, make_user):
    quiet, loud = make_user(), make_user()
    _seed_token(db, quiet["id"], _expo_token("bmute"))
    theirs = _seed_token(db, loud["id"], _expo_token("bloud"))
    _set_channel(db, quiet["id"], "news", 0)
    _expo_accepts_all()

    assert push.notify_channel("news", "T", "B") == 1
    assert _sent_tokens() == [theirs]


@responses.activate
def test_a_broadcast_skips_every_device_of_a_user_who_opted_out(app, db, make_user):
    quiet = make_user()
    _seed_token(db, quiet["id"], _expo_token("bm1"))
    _seed_token(db, quiet["id"], _expo_token("bm2"))
    _set_channel(db, quiet["id"], "news", 0)
    _expo_accepts_all()

    assert push.notify_channel("news", "T", "B") == 0
    assert len(responses.calls) == 0


@responses.activate
def test_a_broadcast_ignores_deactivated_rows(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("binactive"), active=0)
    _expo_accepts_all()

    assert push.notify_channel("news", "T", "B") == 0
    assert len(responses.calls) == 0


@responses.activate
def test_a_broadcast_to_a_faculty_with_no_devices_asks_expo_nothing(app, db):
    _expo_accepts_all()

    assert push.notify_channel("news", "T", "B") == 0
    assert len(responses.calls) == 0


@responses.activate
def test_a_broadcast_excluding_the_only_device_owner_asks_expo_nothing(app, db, make_user):
    author = make_user()
    _seed_token(db, author["id"], _expo_token("onlyauthor"))
    _expo_accepts_all()

    assert push.notify_channel("news", "T", "B", exclude_user_id=author["id"]) == 0
    assert len(responses.calls) == 0


@pytest.mark.slow
@responses.activate
def test_a_faculty_wide_broadcast_slices_at_expos_cap(app, db):
    users = _bulk_users_with_tokens(db, 250, "bcast")
    _expo_accepts_all()

    assert push.notify_channel("news", "T", "B") == 250

    assert sorted(len(body) for body in _push_bodies()) == [50, 100, 100]
    assert _sent_tokens() == sorted(token for _, token in users)


@responses.activate
def test_a_broadcast_fills_the_stats_dict_the_admin_route_reports_from(app, db, make_user):
    one, two = make_user(), make_user()
    _seed_token(db, one["id"], _expo_token("st1"))
    _seed_token(db, two["id"], _expo_token("st2"))
    _expo_replies({"data": [{"status": "ok", "id": "t0"},
                            {"status": "error", "details": {"error": "MessageTooBig"}}]})
    stats = {}

    push.notify_channel("admin", "T", "B", stats=stats)

    assert stats["sent"] == 1
    assert stats["failed"] == 1
    assert stats["errors"] == {"MessageTooBig": 1}


@responses.activate
def test_a_broadcast_retires_the_dead_devices_it_finds(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("bcastdead"))
    _expo_replies({"data": [{"status": "error", "details": {"error": "DeviceNotRegistered"}}]})

    assert push.notify_channel("news", "T", "B") == 0
    assert _token_row(db, token)["active"] == 0








# -----------------------------------------------------------
# Containment — push never fails the thing that asked for it
# -----------------------------------------------------------

@responses.activate
def test_a_broadcast_returns_zero_when_expo_is_unreachable(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("unreachable"))
    _expo_raises(requests.exceptions.ConnectionError("no route"))

    assert push.notify_channel("news", "T", "B") == 0


@responses.activate
def test_a_broadcast_returns_zero_when_expo_times_out(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("timeout"))
    _expo_raises(requests.exceptions.ReadTimeout("expo took too long"))

    assert push.notify_channel("news", "T", "B") == 0


@responses.activate
def test_a_broadcast_returns_zero_when_expo_answers_an_error_page(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("errpage"))
    _expo_replies(text="<html>400</html>", status=400)

    assert push.notify_channel("news", "T", "B") == 0


@responses.activate
def test_a_broadcast_returns_zero_when_expo_answers_nonsense(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("nonsense"))
    _expo_replies({"data": "not a list"})

    assert push.notify_channel("news", "T", "B") == 0


@responses.activate
def test_a_fan_out_returns_zero_when_expo_is_unreachable(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("fanoutdown"))
    _expo_raises(requests.exceptions.ConnectionError("no route"))

    assert push.notify_channel_users("chat", [user["id"]], "T", "B") == 0








# -----------------------------------------------------------
# Wire contracts the production app depends on
# -----------------------------------------------------------

@pytest.mark.contract
@responses.activate
def test_a_chat_push_carries_the_fields_the_app_routes_a_tap_on(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("contractchat"))
    _expo_accepts_all()

    push.notify_channel_users("chat", [user["id"]], "Ona Onaityte", "Labas",
                              data={"type": "chat_message", "conversationId": "conv-1"})

    message = _all_messages()[0]
    assert set(message) == {"to", "title", "body", "sound", "channelId", "data", "priority", "ttl"}
    assert message["channelId"] == "default"
    assert message["sound"] == "default"
    # app/_layout.tsx routes on data.type and opens the room
    # named by data.conversationId
    assert message["data"]["type"] == "chat_message"
    assert message["data"]["conversationId"] == "conv-1"
    assert message["data"]["channel"] == "chat"


@pytest.mark.contract
@responses.activate
def test_a_news_push_carries_the_type_the_app_opens_the_news_tab_on(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("contractnews"))
    _expo_accepts_all()

    push.notify_channel("news", "Naujiena", "Tekstas",
                        data={"type": "news", "source": "knf.vu.lt"})

    message = _all_messages()[0]
    assert message["data"]["type"] == "news"
    assert message["data"]["source"] == "knf.vu.lt"
    assert message["ttl"] == 86400


@pytest.mark.contract
@responses.activate
def test_an_admin_announcement_carries_its_own_type(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("contractadmin"))
    _expo_accepts_all()

    push.notify_channel("admin", "Skelbimas", "Tekstas",
                        data={"type": "admin_announcement"})

    assert _all_messages()[0]["data"]["type"] == "admin_announcement"


@pytest.mark.contract
def test_the_channel_names_the_switches_and_the_sender_share_are_the_four():
    assert push.VALID_CHANNELS == ("news", "chat", "schedule", "admin")


@pytest.mark.contract
def test_the_expo_endpoints_are_the_documented_ones():
    assert push.EXPO_PUSH_URL == "https://exp.host/--/api/v2/push/send"
    assert push.EXPO_RECEIPTS_URL == "https://exp.host/--/api/v2/push/getReceipts"
