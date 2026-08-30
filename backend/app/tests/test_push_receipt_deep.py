# -----------------------------------------------------------
#  [*] Tests — push.py's receipt / retirement slice, exhaustive
#
#  The gap-closing pass over five functions of
#  app/notifications/push.py, and nothing else:
#
#    token_digest             — the only shape a push token
#                               may take in a log line
#    _queue_receipt           — the bounded, in-memory park
#                               where accepted tickets wait
#    poll_push_receipts       — stage two of delivery: what
#                               Expo says HAPPENED to those
#                               tickets fifteen minutes later
#    _deactivate_tokens       — the one UPDATE that retires
#                               every dead device of a batch
#    prune_orphan_push_tokens — the daily sweep that drops
#                               tokens whose owner holds no
#                               unexpired session
#
#  Every branch of those five is driven here: each guard
#  clause, each except handler, each boundary (0, 1, the
#  slice cap, the cap + 1, empty, None, a wrong type, a
#  thousand), and the failure modes a broken Expo or a broken
#  database can actually produce.
#
#  What it proves, beyond the happy path:
#
#    - THE WINDOW: _RECEIPT_DELAY is inclusive — a ticket
#      stamped exactly 900 s ago is due, half a second short
#      is not, and the young ones go back in their original
#      order while the old ones go out
#    - THE LOSS: a drained ticket whose slice fails is NOT
#      re-queued. Receipts are diagnostics; the module says
#      best-effort and this pins what that costs
#    - CONTAINMENT: a 400, a timeout, an HTML error page, a
#      JSON array where an object belongs, a receipt that is
#      a string, a details field that is not a dict, a
#      database that cannot be opened — none of them raise
#      into the scheduler and none of them abandon the
#      remaining slices
#    - RETIREMENT: every dead device of a whole poll goes in
#      ONE _deactivate_tokens call, the row is flipped and
#      kept (never deleted), updated_at is refreshed to the
#      naive-UTC shape the rest of push_tokens carries, and
#      an empty/unknown token cannot crash the sweep
#    - HYGIENE: no log line this slice emits may contain a
#      raw token — not the retirement line, not the receipt
#      error line, not even when Expo quotes the token back
#    - THE SWEEP: expires_at is compared as a STRING against
#      utc_now_iso(), so the comparison is exclusive at the
#      microsecond and a non-UTC offset sorts by its text
# -----------------------------------------------------------

import hashlib
import json
import logging
import re
import sqlite3
import threading
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
# traveller, which is the only reason a 15-minute window and a
# 30-day session are testable without a real second passing
FROZEN = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)




# -----------------------------------------------------------
# _isolated_receipt_state
# -----------------------------------------------------------
#
# push.py keeps process-wide state: the in-memory receipt
# queue and the paced gate's last-slice stamp. Both are reset
# around EVERY test here.
#
# The stamp reset is not tidiness. These tests travel decades
# in time, which moves time.monotonic(); leaving that value in
# _last_slice_at would make the next slice — here or in
# another agent's file — block under the pace lock for years.
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_receipt_state():
    _reset_push_state()
    yield
    _reset_push_state()


def _reset_push_state():
    push._last_slice_at = 0.0
    with push._receipt_lock:
        push._receipt_queue.clear()




# -----------------------------------------------------------
# Row helpers
# -----------------------------------------------------------
#
# _expo_token pads the opaque part so the strings also satisfy
# the intake grammar notifications/routes.py enforces — these
# tests insert rows directly, but a realistic token is what
# makes the redaction assertions mean anything.
# -----------------------------------------------------------

def _expo_token(name):
    return f"ExponentPushToken[{name}-bbbbbbbbbb]"


def _seed_token(db, user_id, token, language="lt", active=1, platform="ios"):
    db.execute(
        "INSERT INTO push_tokens (id, user_id, token, platform, language, active)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), user_id, token, platform, language, active),
    )
    db.commit()
    return token


def _seed_session(db, user_id, expires_at):
    db.execute(
        "INSERT INTO sessions (id, user_id, token, expires_at) VALUES (?, ?, ?, ?)",
        (str(uuid.uuid4()), user_id, uuid.uuid4().hex, expires_at),
    )
    db.commit()


def _token_row(db, token):
    return db.execute("SELECT * FROM push_tokens WHERE token = ?", (token,)).fetchone()


def _token_count(db):
    return db.execute("SELECT COUNT(*) AS n FROM push_tokens").fetchone()["n"]


def _iso(moment):
    return moment.isoformat()




# -----------------------------------------------------------
# _bulk_tokens
# -----------------------------------------------------------
#
# Hundreds of push_tokens rows in one executemany — the only
# way to reach the "one UPDATE for a whole batch" and "one
# DELETE for a whole sweep" claims with real rows. user_id is
# planted directly because the fixture connection does not
# enforce foreign keys, which is also how the orphan-row case
# below gets made.
#
# Used by:
#   - the large-batch retirement and prune tests
# -----------------------------------------------------------

def _bulk_tokens(db, count, prefix, user_id):
    tokens = [_expo_token(f"{prefix}{i:04d}") for i in range(count)]
    db.executemany(
        "INSERT INTO push_tokens (id, user_id, token, platform, language, active)"
        " VALUES (?, ?, ?, 'ios', 'lt', 1)",
        [(f"{prefix}-{i:04d}", user_id, tokens[i]) for i in range(count)],
    )
    db.commit()
    return tokens




# -----------------------------------------------------------
# Expo receipt fakes
# -----------------------------------------------------------
#
# _expo_receipts registers one canned answer; _expo_receipts_echo
# answers ANY slice with an "ok" receipt per id it was asked
# about, which is what makes the 300-id slice boundary
# testable without writing a body per slice.
# -----------------------------------------------------------

def _expo_receipts(body=None, status=200, text=None, exc=None):
    if exc is not None:
        responses.add(responses.POST, push.EXPO_RECEIPTS_URL, body=exc)
    elif text is not None:
        responses.add(responses.POST, push.EXPO_RECEIPTS_URL, body=text, status=status,
                      content_type="text/html")
    else:
        responses.add(responses.POST, push.EXPO_RECEIPTS_URL, json=body, status=status)


def _expo_receipts_echo(status_value="ok"):

    def _reply(request):
        ids = json.loads(request.body)["ids"]
        return 200, {}, json.dumps({"data": {i: {"status": status_value} for i in ids}})

    responses.add_callback(responses.POST, push.EXPO_RECEIPTS_URL,
                           callback=_reply, content_type="application/json")


def _receipt_bodies():
    return [json.loads(call.request.body) for call in responses.calls
            if call.request.url == push.EXPO_RECEIPTS_URL]


def _asked_ids():
    out = []
    for body in _receipt_bodies():
        out.extend(body["ids"])
    return out


def _queued_ids():
    return [entry[0] for entry in push._receipt_queue]




# -----------------------------------------------------------
# _StubDb
# -----------------------------------------------------------
#
# Stands in for a get_db() connection where the point is a
# rowcount the real schema cannot easily produce, or a failure
# at a named step. It records whether close() ran, which is
# how the finally-block claims are proved.
#
# Used by:
#   - the containment tests for _deactivate_tokens and
#     prune_orphan_push_tokens
# -----------------------------------------------------------

class _StubCursor:

    def __init__(self, rowcount):
        self.rowcount = rowcount


class _StubDb:

    def __init__(self, rowcount=0, fail_on=None):
        self.rowcount = rowcount
        self.fail_on = fail_on
        self.statements = []
        self.committed = False
        self.closed = False

    def execute(self, sql, params=()):
        self.statements.append((sql, params))
        if self.fail_on == "execute":
            raise sqlite3.OperationalError("no such table: push_tokens")
        return _StubCursor(self.rowcount)

    def commit(self):
        if self.fail_on == "commit":
            raise sqlite3.OperationalError("disk I/O error")
        self.committed = True

    def close(self):
        if self.fail_on == "close":
            raise sqlite3.ProgrammingError("cannot close")
        self.closed = True




# -----------------------------------------------------------
# _CountingDb
# -----------------------------------------------------------
#
# A real connection with a tally around execute(), so "the
# WHOLE batch in ONE UPDATE" is an assertion instead of a
# comment. sqlite3.Connection takes no attributes of its own,
# hence the proxy.
#
# Used by:
#   - the one-statement tests
# -----------------------------------------------------------

class _CountingDb:

    def __init__(self, conn, log):
        self._conn = conn
        self._log = log

    def execute(self, sql, params=()):
        self._log.append((sql, params))
        return self._conn.execute(sql, params)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def _count_statements(monkeypatch):
    log = []
    real = push.get_db
    monkeypatch.setattr(push, "get_db", lambda: _CountingDb(real(), log))
    return log


def _explode(*args, **kwargs):
    raise sqlite3.OperationalError("unable to open database file")




# -----------------------------------------------------------
# _spy_deactivate
# -----------------------------------------------------------
#
# Swaps the retirement helper for a recorder, so a poll's
# "every dead device in ONE call" claim can be read off the
# call list rather than guessed at from the rows.
#
# Used by:
#   - the poll_push_receipts retirement tests
# -----------------------------------------------------------

def _spy_deactivate(monkeypatch):
    seen = []

    def _record(tokens):
        seen.append(list(tokens))
        return len(tokens)

    monkeypatch.setattr(push, "_deactivate_tokens", _record)
    return seen




# -----------------------------------------------------------
# _RecordingSession
# -----------------------------------------------------------
#
# `responses` fakes the wire but hides the call itself, and
# the timeout plus the headers are exactly what has to be
# asserted: the mobile client gives up at 15 s, so the server
# must fold first, and no Expo access token may ride along.
#
# Used by:
#   - the receipt request-shape tests
# -----------------------------------------------------------

class _FakeResponse:

    def __init__(self, status_code, payload, text="fake body"):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _RecordingSession:

    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "body": json, "headers": headers, "timeout": timeout})
        return _FakeResponse(self.status, self.payload)




# -----------------------------------------------------------
# no_backoff
# -----------------------------------------------------------
#
# The shipped Retry waits 0 s, then 2 s, then 4 s between
# attempts. This swaps it for the same policy with the waiting
# removed, so the retry COUNT under test stays the shipped one
# and the test stays instant.
#
# Used by:
#   - the retried-receipt-slice tests
# -----------------------------------------------------------

@pytest.fixture
def no_backoff(monkeypatch):
    adapter = push._SESSION.get_adapter("https://exp.host")
    monkeypatch.setattr(adapter, "max_retries", adapter.max_retries.new(backoff_factor=0))








# -----------------------------------------------------------
# token_digest — a bearer credential never reaches a log
# -----------------------------------------------------------

def test_a_digest_is_the_first_eight_hex_digits_of_the_sha256():
    token = _expo_token("digest")
    assert push.token_digest(token) == hashlib.sha256(token.encode()).hexdigest()[:8]


def test_a_digest_is_always_eight_lowercase_hex_characters():
    for name in ["a", "b" * 500, "žžž", "ExponentPushToken[]"]:
        assert re.fullmatch(r"[0-9a-f]{8}", push.token_digest(name))


def test_the_digest_of_the_empty_string_is_the_known_sha256_prefix():
    assert push.token_digest("") == "e3b0c442"


def test_the_same_device_digests_the_same_way_every_time():
    token = _expo_token("stable")
    assert len({push.token_digest(token) for _ in range(50)}) == 1


def test_one_changed_character_changes_the_digest():
    assert push.token_digest(_expo_token("aaa")) != push.token_digest(_expo_token("aab"))


def test_case_alone_separates_two_digests():
    assert push.token_digest("ExponentPushToken[ABC]") != push.token_digest("ExponentPushToken[abc]")


def test_a_digest_is_taken_over_the_utf8_bytes_of_the_token():
    token = "ExponentPushToken[ąčęėįšųū-ž]"
    assert push.token_digest(token) == hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


def test_a_hundred_kilobyte_token_still_digests_to_eight_characters():
    assert len(push.token_digest("x" * 100_000)) == 8


def test_a_digest_never_contains_a_slice_of_the_token_it_stands_for():
    token = _expo_token("deadbeefcafe")
    digest = push.token_digest(token)
    assert digest not in token
    assert token not in digest


def test_whitespace_is_a_token_like_any_other_to_the_digest():
    assert push.token_digest(" ") != push.token_digest("  ")


@pytest.mark.parametrize("value", [None, 123, 4.5, b"ExponentPushToken[x]", ["t"], {"t": 1}])
def test_a_digest_of_something_that_is_not_a_string_is_refused_loudly(value):
    # The helper takes a str: every caller in the module hands
    # it one, and a silent str() would hash the repr instead
    with pytest.raises(AttributeError):
        push.token_digest(value)








# -----------------------------------------------------------
# _queue_receipt — parking a ticket for stage two
# -----------------------------------------------------------

def test_a_queued_ticket_keeps_its_id_its_token_and_the_moment_it_was_parked():
    token = _expo_token("park")
    with time_machine.travel(FROZEN, tick=False):
        stamp = time.monotonic()
        push._queue_receipt("ticket-park", token)

    assert list(push._receipt_queue) == [("ticket-park", token, stamp)]


@pytest.mark.parametrize("ticket_id", [None, "", 0, False, [], {}, ()])
def test_a_ticket_without_an_id_is_not_worth_parking(ticket_id):
    push._queue_receipt(ticket_id, _expo_token("noid"))
    assert len(push._receipt_queue) == 0


@pytest.mark.parametrize("ticket_id", [7, "0", 0.5, True, ("a",), "XXX-YYY"])
def test_any_truthy_id_is_parked_exactly_as_expo_named_it(ticket_id):
    push._queue_receipt(ticket_id, _expo_token("truthy"))
    assert _queued_ids() == [ticket_id]


def test_a_ticket_with_no_token_at_all_is_still_parked():
    push._queue_receipt("ticket-tokenless", None)
    assert list(push._receipt_queue)[0][1] is None


def test_the_same_ticket_id_parked_twice_makes_two_entries():
    push._queue_receipt("ticket-twin", _expo_token("one"))
    push._queue_receipt("ticket-twin", _expo_token("two"))
    assert len(push._receipt_queue) == 2


def test_tickets_come_out_of_the_queue_in_the_order_they_went_in():
    for i in range(5):
        push._queue_receipt(f"ticket-{i}", _expo_token(str(i)))

    assert _queued_ids() == ["ticket-0", "ticket-1", "ticket-2", "ticket-3", "ticket-4"]


def test_the_queue_is_bounded_at_twenty_thousand_entries():
    assert push._receipt_queue.maxlen == 20000


def test_exactly_a_full_queue_still_holds_its_oldest_ticket():
    for i in range(push._receipt_queue.maxlen):
        push._queue_receipt(f"ticket-{i}", "t")

    assert len(push._receipt_queue) == push._receipt_queue.maxlen
    assert push._receipt_queue[0][0] == "ticket-0"


def test_one_ticket_past_the_cap_drops_the_oldest_rather_than_growing():
    for i in range(push._receipt_queue.maxlen + 1):
        push._queue_receipt(f"ticket-{i}", "t")

    assert len(push._receipt_queue) == push._receipt_queue.maxlen
    assert push._receipt_queue[0][0] == "ticket-1"
    assert push._receipt_queue[-1][0] == f"ticket-{push._receipt_queue.maxlen}"


def test_parking_a_ticket_never_opens_a_database_connection(monkeypatch):
    # Receipts are diagnostics, not state worth persisting
    monkeypatch.setattr(push, "get_db", _explode)
    push._queue_receipt("ticket-nodb", _expo_token("nodb"))
    assert len(push._receipt_queue) == 1


def test_the_stamp_is_the_monotonic_clock_not_the_wall_clock():
    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-a", "t")
        traveller.shift(60)
        push._queue_receipt("ticket-b", "t")

    first, second = list(push._receipt_queue)
    assert second[2] - first[2] == pytest.approx(60, abs=0.01)


def test_every_thread_parking_at_once_keeps_every_ticket():
    barrier = threading.Barrier(8)

    def _park(worker):
        barrier.wait()
        for i in range(200):
            push._queue_receipt(f"ticket-{worker}-{i}", "t")

    threads = [threading.Thread(target=_park, args=(w,)) for w in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(push._receipt_queue) == 1600
    assert len(set(_queued_ids())) == 1600








# -----------------------------------------------------------
# _deactivate_tokens — what it accepts before it opens a
# connection
# -----------------------------------------------------------

def test_retiring_nothing_returns_zero_without_touching_the_database(monkeypatch):
    monkeypatch.setattr(push, "get_db", _explode)
    assert push._deactivate_tokens([]) == 0


@pytest.mark.parametrize("tokens", [[], (), set(), iter([]), [None], ["", None], [None, "", False, 0]])
def test_an_iterable_of_nothing_usable_never_reaches_sql(monkeypatch, tokens):
    monkeypatch.setattr(push, "get_db", _explode)
    assert push._deactivate_tokens(tokens) == 0


def test_the_empty_and_none_entries_are_dropped_and_the_real_one_kept(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("mixed"))

    assert push._deactivate_tokens([None, "", token, None]) == 1
    assert _token_row(db, token)["active"] == 0


def test_a_tuple_a_set_and_a_generator_of_tokens_all_work(app, db, make_user):
    user = make_user()
    tokens = [_seed_token(db, user["id"], _expo_token(f"iter{i}")) for i in range(3)]

    assert push._deactivate_tokens((tokens[0],)) == 1
    assert push._deactivate_tokens({tokens[1]}) == 1
    assert push._deactivate_tokens(t for t in [tokens[2]]) == 1


def test_the_same_token_five_times_moves_one_row(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("dupe"))

    assert push._deactivate_tokens([token] * 5) == 1


def test_a_batch_of_tokens_that_cannot_be_sorted_is_not_swallowed(monkeypatch):
    # The de-dupe runs BEFORE the try, deliberately: every
    # caller in this module hands it a list of str, and a
    # comparison failure there is a programming error, not an
    # operational one
    monkeypatch.setattr(push, "get_db", _explode)
    with pytest.raises(TypeError):
        push._deactivate_tokens(["a", 7])


def test_retiring_from_something_that_is_not_iterable_is_not_swallowed(monkeypatch):
    monkeypatch.setattr(push, "get_db", _explode)
    with pytest.raises(TypeError):
        push._deactivate_tokens(None)




# -----------------------------------------------------------
# _deactivate_tokens — the UPDATE itself
# -----------------------------------------------------------

def test_retiring_a_device_flips_the_row_and_keeps_it(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("gone"))

    assert push._deactivate_tokens([token]) == 1

    row = _token_row(db, token)
    assert row["active"] == 0
    assert row["token"] == token
    assert _token_count(db) == 1


def test_retiring_refreshes_updated_at_to_the_naive_utc_shape(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("stamp"))

    with time_machine.travel(FROZEN, tick=False):
        push._deactivate_tokens([token])

    stamped = _token_row(db, token)["updated_at"]
    assert stamped == "2026-08-29T12:00:00"
    assert datetime.fromisoformat(stamped).tzinfo is None


def test_retiring_leaves_every_other_column_of_the_row_alone(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("cols"), language="en", platform="android")
    before = _token_row(db, token)

    push._deactivate_tokens([token])
    after = _token_row(db, token)

    assert (after["user_id"], after["platform"], after["language"], after["created_at"]) == \
           (before["user_id"], before["platform"], before["language"], before["created_at"])


def test_only_the_named_devices_move(app, db, make_user):
    user = make_user()
    dead = _seed_token(db, user["id"], _expo_token("dead"))
    alive = _seed_token(db, user["id"], _expo_token("alive"))

    assert push._deactivate_tokens([dead]) == 1
    assert _token_row(db, alive)["active"] == 1


def test_a_token_nobody_ever_registered_moves_nothing(app, db):
    assert push._deactivate_tokens([_expo_token("stranger")]) == 0


def test_a_batch_of_known_and_unknown_tokens_counts_only_the_known(app, db, make_user):
    user = make_user()
    known = _seed_token(db, user["id"], _expo_token("known"))

    assert push._deactivate_tokens([known, _expo_token("nobody")]) == 1


def test_retiring_the_same_device_twice_leaves_it_retired(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("twice"))

    assert push._deactivate_tokens([token]) == 1
    assert push._deactivate_tokens([token]) == 1
    assert _token_row(db, token)["active"] == 0


def test_an_already_inactive_row_is_matched_again_and_restamped(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("already"), active=0)

    with time_machine.travel(FROZEN, tick=False):
        assert push._deactivate_tokens([token]) == 1

    assert _token_row(db, token)["updated_at"] == "2026-08-29T12:00:00"


def test_the_device_of_another_user_is_retired_all_the_same(app, db, make_user):
    # Expo names devices, never users: whoever owns the row,
    # the phone is gone
    owner = make_user()
    other = make_user()
    theirs = _seed_token(db, other["id"], _expo_token("theirs"))
    _seed_token(db, owner["id"], _expo_token("mine"))

    assert push._deactivate_tokens([theirs]) == 1
    assert _token_row(db, theirs)["active"] == 0


def test_a_token_that_differs_only_by_trailing_space_does_not_match(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("exact"))

    assert push._deactivate_tokens([token + " "]) == 0
    assert _token_row(db, token)["active"] == 1


def test_a_token_full_of_sql_punctuation_is_bound_never_interpolated(app, db, make_user):
    user = make_user()
    nasty = "'; DROP TABLE push_tokens; --"
    _seed_token(db, user["id"], nasty)

    assert push._deactivate_tokens([nasty]) == 1
    assert _token_row(db, nasty)["active"] == 0


def test_a_unicode_token_round_trips_through_the_update(app, db, make_user):
    user = make_user()
    token = "ExponentPushToken[ąžuolas-🌳]"
    _seed_token(db, user["id"], token)

    assert push._deactivate_tokens([token]) == 1
    assert _token_row(db, token)["active"] == 0


def test_the_whole_batch_moves_in_a_single_update(app, db, make_user, monkeypatch):
    user = make_user()
    tokens = [_seed_token(db, user["id"], _expo_token(f"one{i}")) for i in range(6)]
    statements = _count_statements(monkeypatch)

    assert push._deactivate_tokens(tokens) == 6

    updates = [sql for sql, _ in statements if "UPDATE push_tokens" in sql]
    assert len(updates) == 1
    assert updates[0].count("?") == 7


def test_a_thousand_dead_devices_still_go_in_one_statement(app, db, monkeypatch):
    tokens = _bulk_tokens(db, 1000, "bulk", "ghost-user")
    statements = _count_statements(monkeypatch)

    assert push._deactivate_tokens(tokens) == 1000

    assert len([sql for sql, _ in statements if "UPDATE push_tokens" in sql]) == 1
    assert db.execute("SELECT COUNT(*) AS n FROM push_tokens WHERE active = 1").fetchone()["n"] == 0


def test_the_update_is_committed_so_another_connection_sees_it(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("commit"))

    push._deactivate_tokens([token])

    fresh = sqlite3.connect(app.config["DB_PATH"])
    try:
        assert fresh.execute("SELECT active FROM push_tokens WHERE token = ?", (token,)).fetchone()[0] == 0
    finally:
        fresh.close()


def test_the_statement_binds_the_timestamp_first_then_every_token(monkeypatch):
    stub = _StubDb(rowcount=2)
    monkeypatch.setattr(push, "get_db", lambda: stub)

    with time_machine.travel(FROZEN, tick=False):
        assert push._deactivate_tokens(["b-token", "a-token"]) == 2

    sql, params = stub.statements[0]
    assert params == ["2026-08-29T12:00:00", "a-token", "b-token"]
    assert "SET active = 0" in sql
    assert stub.committed is True
    assert stub.closed is True




# -----------------------------------------------------------
# _deactivate_tokens — nothing here may raise at a caller
# -----------------------------------------------------------

def test_a_database_that_cannot_be_opened_costs_zero_not_an_exception(monkeypatch, caplog):
    monkeypatch.setattr(push, "get_db", _explode)

    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        assert push._deactivate_tokens([_expo_token("nodb")]) == 0

    assert "Failed to deactivate tokens" in caplog.text


def test_a_failing_update_still_closes_the_connection(monkeypatch, caplog):
    stub = _StubDb(fail_on="execute")
    monkeypatch.setattr(push, "get_db", lambda: stub)

    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        assert push._deactivate_tokens([_expo_token("boom")]) == 0

    assert stub.closed is True
    assert "Failed to deactivate tokens" in caplog.text


def test_a_failing_commit_costs_zero_and_still_closes(monkeypatch):
    stub = _StubDb(rowcount=1, fail_on="commit")
    monkeypatch.setattr(push, "get_db", lambda: stub)

    assert push._deactivate_tokens([_expo_token("nocommit")]) == 0
    assert stub.closed is True


def test_a_connection_that_cannot_even_close_is_swallowed_too(monkeypatch):
    stub = _StubDb(rowcount=1, fail_on="close")
    monkeypatch.setattr(push, "get_db", lambda: stub)

    assert push._deactivate_tokens([_expo_token("noclose")]) == 0


def test_a_dropped_push_tokens_table_is_survived(app, db, caplog):
    db.execute("DROP TABLE push_tokens")
    db.commit()

    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        assert push._deactivate_tokens([_expo_token("gone")]) == 0

    assert "Failed to deactivate tokens" in caplog.text




# -----------------------------------------------------------
# _deactivate_tokens — the log line
# -----------------------------------------------------------

def test_moving_no_rows_says_nothing_at_all(app, db, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        assert push._deactivate_tokens([_expo_token("silent")]) == 0

    assert "Deactivated" not in caplog.text


def test_moving_a_row_reports_the_count_and_the_digest(app, db, make_user, caplog):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("logged"))

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        push._deactivate_tokens([token])

    assert "Deactivated 1 unregistered token(s)" in caplog.text
    assert push.token_digest(token) in caplog.text
    assert token not in caplog.text


def test_exactly_twenty_digests_are_logged_for_twenty_devices(app, db, caplog):
    tokens = _bulk_tokens(db, 20, "twenty", "ghost-user")

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        push._deactivate_tokens(tokens)

    for token in tokens:
        assert push.token_digest(token) in caplog.text


def test_past_twenty_devices_the_digest_list_is_capped_but_the_count_is_not(app, db, caplog):
    tokens = _bulk_tokens(db, 25, "capped", "ghost-user")

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        assert push._deactivate_tokens(tokens) == 25

    assert "Deactivated 25 unregistered token(s)" in caplog.text
    # unique is sorted, so the first twenty by name are the
    # ones that made the line
    assert push.token_digest(tokens[19]) in caplog.text
    assert push.token_digest(tokens[24]) not in caplog.text


def test_not_one_raw_token_reaches_the_log_for_a_whole_dead_batch(app, db, caplog):
    tokens = _bulk_tokens(db, 25, "hygiene", "ghost-user")

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        push._deactivate_tokens(tokens)

    for token in tokens:
        assert token not in caplog.text








# -----------------------------------------------------------
# prune_orphan_push_tokens — what goes and what stays
# -----------------------------------------------------------

def test_a_phone_whose_owner_has_no_session_at_all_is_dropped(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("orphan"))

    assert push.prune_orphan_push_tokens() == 1
    assert _token_count(db) == 0


def test_a_phone_whose_owner_is_still_logged_in_stays(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("live"))
    _seed_session(db, user["id"], _iso(FROZEN + timedelta(days=30)))

    with time_machine.travel(FROZEN, tick=False):
        assert push.prune_orphan_push_tokens() == 0

    assert _token_row(db, token) is not None


def test_a_lapsed_session_no_longer_protects_the_phone(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("lapsed"))
    _seed_session(db, user["id"], _iso(FROZEN - timedelta(seconds=1)))

    with time_machine.travel(FROZEN, tick=False):
        assert push.prune_orphan_push_tokens() == 1


def test_one_live_session_among_several_dead_ones_is_enough(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("mixed-sessions"))
    _seed_session(db, user["id"], _iso(FROZEN - timedelta(days=5)))
    _seed_session(db, user["id"], _iso(FROZEN - timedelta(days=1)))
    _seed_session(db, user["id"], _iso(FROZEN + timedelta(minutes=1)))

    with time_machine.travel(FROZEN, tick=False):
        assert push.prune_orphan_push_tokens() == 0


def test_a_thirty_day_session_stops_covering_the_phone_the_day_it_runs_out(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("thirty"))
    _seed_session(db, user["id"], _iso(FROZEN + timedelta(days=30)))

    with time_machine.travel(FROZEN + timedelta(days=29), tick=False):
        assert push.prune_orphan_push_tokens() == 0

    with time_machine.travel(FROZEN + timedelta(days=31), tick=False):
        assert push.prune_orphan_push_tokens() == 1


def test_every_device_of_one_orphan_goes_in_the_same_sweep(app, db, make_user):
    user = make_user()
    for i in range(4):
        _seed_token(db, user["id"], _expo_token(f"device{i}"))

    assert push.prune_orphan_push_tokens() == 4
    assert _token_count(db) == 0


def test_every_device_of_a_signed_in_user_survives(app, db, make_user):
    user = make_user()
    for i in range(4):
        _seed_token(db, user["id"], _expo_token(f"kept{i}"))
    _seed_session(db, user["id"], _iso(FROZEN + timedelta(days=1)))

    with time_machine.travel(FROZEN, tick=False):
        assert push.prune_orphan_push_tokens() == 0

    assert _token_count(db) == 4


def test_one_users_orphans_go_while_anothers_devices_stay(app, db, make_user):
    logged_in = make_user()
    logged_out = make_user()
    kept = _seed_token(db, logged_in["id"], _expo_token("keeper"))
    _seed_token(db, logged_out["id"], _expo_token("goner"))
    _seed_session(db, logged_in["id"], _iso(FROZEN + timedelta(days=2)))

    with time_machine.travel(FROZEN, tick=False):
        assert push.prune_orphan_push_tokens() == 1

    assert _token_row(db, kept) is not None


def test_a_retired_row_is_pruned_like_any_other_when_its_owner_is_gone(app, db, make_user):
    # The sweep is about sessions, not about active
    user = make_user()
    _seed_token(db, user["id"], _expo_token("retired"), active=0)

    assert push.prune_orphan_push_tokens() == 1


def test_a_retired_row_of_a_signed_in_user_is_left_where_it_is(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("retired-live"), active=0)
    _seed_session(db, user["id"], _iso(FROZEN + timedelta(days=1)))

    with time_machine.travel(FROZEN, tick=False):
        assert push.prune_orphan_push_tokens() == 0

    assert _token_row(db, token)["active"] == 0


def test_a_row_pointing_at_a_user_that_does_not_exist_is_pruned(app, db):
    _seed_token(db, "no-such-user", _expo_token("ghost"))
    assert push.prune_orphan_push_tokens() == 1


def test_somebody_elses_live_session_protects_nobody(app, db, make_user):
    stranger = make_user()
    owner = make_user()
    _seed_token(db, owner["id"], _expo_token("unprotected"))
    _seed_session(db, stranger["id"], _iso(FROZEN + timedelta(days=1)))

    with time_machine.travel(FROZEN, tick=False):
        assert push.prune_orphan_push_tokens() == 1


def test_an_empty_push_tokens_table_is_a_no_op(app, db):
    assert push.prune_orphan_push_tokens() == 0


def test_the_sweep_is_idempotent(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("once"))

    assert push.prune_orphan_push_tokens() == 1
    assert push.prune_orphan_push_tokens() == 0


def test_the_sweep_touches_neither_the_sessions_nor_the_users(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("scope"))
    _seed_session(db, user["id"], _iso(FROZEN - timedelta(days=1)))

    with time_machine.travel(FROZEN, tick=False):
        push.prune_orphan_push_tokens()

    assert db.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"] == 1
    assert db.execute("SELECT COUNT(*) AS n FROM users WHERE id = ?", (user["id"],)).fetchone()["n"] == 1


def test_five_hundred_orphans_go_in_one_delete(app, db, monkeypatch):
    _bulk_tokens(db, 500, "sweep", "ghost-user")
    statements = _count_statements(monkeypatch)

    assert push.prune_orphan_push_tokens() == 500

    assert len([sql for sql, _ in statements if "DELETE FROM push_tokens" in sql]) == 1




# -----------------------------------------------------------
# prune_orphan_push_tokens — the boundary is a STRING compare
# -----------------------------------------------------------

def test_a_session_expiring_exactly_now_no_longer_covers_the_phone(app, db, make_user):
    # expires_at > now, strictly: equality is expired
    user = make_user()
    _seed_token(db, user["id"], _expo_token("edge"))
    _seed_session(db, user["id"], "2026-08-29T12:00:00+00:00")

    with time_machine.travel(FROZEN, tick=False):
        assert push.prune_orphan_push_tokens() == 1


def test_a_session_expiring_one_microsecond_from_now_still_covers_it(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("microsecond"))
    _seed_session(db, user["id"], "2026-08-29T12:00:00.000001+00:00")

    with time_machine.travel(FROZEN, tick=False):
        assert push.prune_orphan_push_tokens() == 0


def test_a_legacy_space_form_timestamp_in_the_future_still_sorts_ahead(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("spaceform"))
    _seed_session(db, user["id"], "2030-01-01 00:00:00")

    with time_machine.travel(FROZEN, tick=False):
        assert push.prune_orphan_push_tokens() == 0


def test_an_offset_that_is_not_utc_is_compared_as_text_not_as_a_moment(app, db, make_user):
    # Documented limitation, not an accident: migration v17
    # normalised expires_at to aware UTC everywhere, so the
    # plain string comparison is correct for every row the app
    # writes. A hand-planted +03:00 row reads as live half an
    # hour after the moment it actually names
    user = make_user()
    _seed_token(db, user["id"], _expo_token("offset"))
    _seed_session(db, user["id"], "2026-08-29T15:00:00+03:00")

    with time_machine.travel(FROZEN + timedelta(minutes=30), tick=False):
        assert push.prune_orphan_push_tokens() == 0




# -----------------------------------------------------------
# prune_orphan_push_tokens — containment and the log line
# -----------------------------------------------------------

def test_a_sweep_on_a_database_that_cannot_be_opened_returns_zero(monkeypatch, caplog):
    monkeypatch.setattr(push, "get_db", _explode)

    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        assert push.prune_orphan_push_tokens() == 0

    assert "Failed to prune push tokens" in caplog.text


def test_a_failing_delete_still_closes_the_connection(monkeypatch):
    stub = _StubDb(fail_on="execute")
    monkeypatch.setattr(push, "get_db", lambda: stub)

    assert push.prune_orphan_push_tokens() == 0
    assert stub.closed is True


def test_a_failing_commit_leaves_the_sweep_reporting_zero(monkeypatch):
    stub = _StubDb(rowcount=9, fail_on="commit")
    monkeypatch.setattr(push, "get_db", lambda: stub)

    assert push.prune_orphan_push_tokens() == 0
    assert stub.closed is True


def test_a_sweep_of_a_dropped_table_is_survived(app, db, caplog):
    db.execute("DROP TABLE push_tokens")
    db.commit()

    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        assert push.prune_orphan_push_tokens() == 0

    assert "Failed to prune push tokens" in caplog.text


def test_the_sweep_compares_against_an_aware_utc_timestamp(monkeypatch):
    stub = _StubDb(rowcount=0)
    monkeypatch.setattr(push, "get_db", lambda: stub)

    with time_machine.travel(FROZEN, tick=False):
        push.prune_orphan_push_tokens()

    sql, params = stub.statements[0]
    assert params == ("2026-08-29T12:00:00+00:00",)
    assert "DELETE FROM push_tokens" in sql


def test_a_sweep_that_removed_nothing_says_nothing(app, db, make_user, caplog):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("quiet"))
    _seed_session(db, user["id"], _iso(FROZEN + timedelta(days=1)))

    with time_machine.travel(FROZEN, tick=False):
        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            push.prune_orphan_push_tokens()

    assert "Pruned" not in caplog.text


def test_a_sweep_reports_exactly_how_many_rows_it_removed(app, db, make_user, caplog):
    user = make_user()
    for i in range(3):
        _seed_token(db, user["id"], _expo_token(f"loud{i}"))

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        push.prune_orphan_push_tokens()

    assert "Pruned 3 push token(s)" in caplog.text


def test_the_prune_log_never_names_a_token(app, db, make_user, caplog):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("private"))

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        push.prune_orphan_push_tokens()

    assert token not in caplog.text








# -----------------------------------------------------------
# poll_push_receipts — draining the queue
# -----------------------------------------------------------

@responses.activate
def test_an_empty_queue_asks_expo_nothing_and_reports_nothing():
    _expo_receipts({"data": {}})

    assert push.poll_push_receipts() == 0
    assert len(responses.calls) == 0


@responses.activate
def test_a_ticket_parked_a_moment_ago_is_left_where_it_is():
    _expo_receipts({"data": {}})

    with time_machine.travel(FROZEN, tick=False):
        push._queue_receipt("ticket-fresh", _expo_token("fresh"))

        assert push.poll_push_receipts() == 0

    assert len(responses.calls) == 0
    assert _queued_ids() == ["ticket-fresh"]


@responses.activate
def test_half_a_second_short_of_the_window_is_still_too_young():
    _expo_receipts({"data": {}})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-almost", _expo_token("almost"))
        traveller.shift(push._RECEIPT_DELAY - 0.5)

        assert push.poll_push_receipts() == 0

    assert _queued_ids() == ["ticket-almost"]


@responses.activate
def test_exactly_the_window_is_due():
    _expo_receipts({"data": {"ticket-exact": {"status": "ok"}}})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-exact", _expo_token("exact"))
        traveller.shift(push._RECEIPT_DELAY)

        assert push.poll_push_receipts() == 1

    assert _asked_ids() == ["ticket-exact"]


@responses.activate
def test_a_ticket_from_yesterday_is_long_due():
    _expo_receipts({"data": {"ticket-old": {"status": "ok"}}})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-old", _expo_token("old"))
        traveller.shift(86400)

        assert push.poll_push_receipts() == 1


@responses.activate
def test_the_due_tickets_leave_the_queue_and_the_young_ones_keep_their_order():
    _expo_receipts_echo()

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-due-1", "t1")
        push._queue_receipt("ticket-due-2", "t2")
        traveller.shift(push._RECEIPT_DELAY + 1)
        push._queue_receipt("ticket-young-1", "t3")
        push._queue_receipt("ticket-young-2", "t4")

        assert push.poll_push_receipts() == 2

    assert _asked_ids() == ["ticket-due-1", "ticket-due-2"]
    assert _queued_ids() == ["ticket-young-1", "ticket-young-2"]


@responses.activate
def test_a_young_ticket_is_picked_up_by_the_next_poll_once_it_ripens():
    _expo_receipts_echo()

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-later", "t")
        assert push.poll_push_receipts() == 0
        traveller.shift(push._RECEIPT_DELAY)
        assert push.poll_push_receipts() == 1

    assert _asked_ids() == ["ticket-later"]


@responses.activate
def test_a_re_queued_ticket_keeps_its_original_stamp():
    _expo_receipts_echo()

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-stamp", "t")
        stamp = push._receipt_queue[0][2]
        traveller.shift(10)
        push.poll_push_receipts()

    assert push._receipt_queue[0][2] == stamp


@responses.activate
def test_two_parks_of_one_ticket_id_collapse_to_the_last_token(app, db, make_user):
    user = make_user()
    first = _seed_token(db, user["id"], _expo_token("first"))
    second = _seed_token(db, user["id"], _expo_token("second"))
    _expo_receipts({"data": {"ticket-clash": {
        "status": "error", "details": {"error": "DeviceNotRegistered"}}}})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-clash", first)
        push._queue_receipt("ticket-clash", second)
        traveller.shift(push._RECEIPT_DELAY + 1)

        assert push.poll_push_receipts() == 1

    assert _asked_ids() == ["ticket-clash"]
    assert _token_row(db, second)["active"] == 0
    assert _token_row(db, first)["active"] == 1


@responses.activate
def test_a_drained_ticket_whose_slice_failed_is_gone_for_good(caplog):
    # Best-effort by design: the queue is memory-only and a
    # failed slice is simply skipped, never re-parked
    _expo_receipts(text="<html>bad request</html>", status=400)

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-lost", _expo_token("lost"))
        traveller.shift(push._RECEIPT_DELAY + 1)

        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            assert push.poll_push_receipts() == 0

    assert len(push._receipt_queue) == 0
    assert "Expo receipts HTTP 400" in caplog.text




# -----------------------------------------------------------
# poll_push_receipts — the request it makes
# -----------------------------------------------------------

def test_a_receipt_poll_posts_the_ids_to_the_documented_endpoint(monkeypatch):
    session = _RecordingSession({"data": {}})
    monkeypatch.setattr(push, "_SESSION", session)

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-shape", _expo_token("shape"))
        traveller.shift(push._RECEIPT_DELAY + 1)
        push.poll_push_receipts()

    assert session.calls[0]["url"] == "https://exp.host/--/api/v2/push/getReceipts"
    assert session.calls[0]["body"] == {"ids": ["ticket-shape"]}


def test_a_receipt_poll_folds_before_the_mobile_client_gives_up(monkeypatch):
    session = _RecordingSession({"data": {}})
    monkeypatch.setattr(push, "_SESSION", session)

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-timeout", "t")
        traveller.shift(push._RECEIPT_DELAY + 1)
        push.poll_push_receipts()

    assert session.calls[0]["timeout"] == push._HTTP_TIMEOUT
    assert push._HTTP_TIMEOUT < 15


def test_a_receipt_poll_carries_the_json_headers_and_no_expo_access_token(monkeypatch):
    session = _RecordingSession({"data": {}})
    monkeypatch.setattr(push, "_SESSION", session)

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-headers", "t")
        traveller.shift(push._RECEIPT_DELAY + 1)
        push.poll_push_receipts()

    headers = session.calls[0]["headers"]
    assert headers == {"Accept": "application/json", "Content-Type": "application/json"}
    assert "Authorization" not in headers


@responses.activate
def test_exactly_a_full_slice_of_ids_is_one_request():
    _expo_receipts_echo()

    with time_machine.travel(FROZEN, tick=False) as traveller:
        for i in range(push._RECEIPT_SLICE):
            push._queue_receipt(f"ticket-{i:04d}", "t")
        traveller.shift(push._RECEIPT_DELAY + 1)

        assert push.poll_push_receipts() == push._RECEIPT_SLICE

    assert len(_receipt_bodies()) == 1


@responses.activate
def test_one_id_past_the_slice_needs_a_second_request():
    _expo_receipts_echo()

    with time_machine.travel(FROZEN, tick=False) as traveller:
        for i in range(push._RECEIPT_SLICE + 1):
            push._queue_receipt(f"ticket-{i:04d}", "t")
        traveller.shift(push._RECEIPT_DELAY + 1)

        assert push.poll_push_receipts() == push._RECEIPT_SLICE + 1

    assert [len(body["ids"]) for body in _receipt_bodies()] == [push._RECEIPT_SLICE, 1]


@responses.activate
def test_a_thousand_due_tickets_are_asked_about_once_each():
    _expo_receipts_echo()

    with time_machine.travel(FROZEN, tick=False) as traveller:
        for i in range(1000):
            push._queue_receipt(f"ticket-{i:04d}", "t")
        traveller.shift(push._RECEIPT_DELAY + 1)

        assert push.poll_push_receipts() == 1000

    assert [len(body["ids"]) for body in _receipt_bodies()] == [300, 300, 300, 100]
    assert len(set(_asked_ids())) == 1000


@responses.activate
def test_a_full_queue_drains_in_a_bounded_number_of_requests():
    # 20 000 parked tickets is the most the deque can hold, so
    # one poll can never make more than 67 requests
    _expo_receipts_echo()

    with time_machine.travel(FROZEN, tick=False) as traveller:
        for i in range(2000):
            push._queue_receipt(f"ticket-{i:05d}", "t")
        traveller.shift(push._RECEIPT_DELAY + 1)

        assert push.poll_push_receipts() == 2000

    assert len(_receipt_bodies()) == 2000 // push._RECEIPT_SLICE + 1




# -----------------------------------------------------------
# poll_push_receipts — a slice that goes wrong costs only
# itself
# -----------------------------------------------------------

@responses.activate
def test_an_unreachable_expo_is_logged_and_swallowed(caplog):
    _expo_receipts(exc=requests.exceptions.ConnectionError("no route"))

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-down", "t")
        traveller.shift(push._RECEIPT_DELAY + 1)

        with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
            assert push.poll_push_receipts() == 0

    assert "Failed to fetch push receipts" in caplog.text


@responses.activate
def test_a_timed_out_receipt_call_is_swallowed():
    _expo_receipts(exc=requests.exceptions.ReadTimeout("too slow"))

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-slow", "t")
        traveller.shift(push._RECEIPT_DELAY + 1)

        assert push.poll_push_receipts() == 0


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413])
@responses.activate
def test_any_error_status_costs_the_slice_and_nothing_more(status, caplog):
    _expo_receipts(text="<html>nope</html>", status=status)

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-status", "t")
        traveller.shift(push._RECEIPT_DELAY + 1)

        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            assert push.poll_push_receipts() == 0

    assert f"Expo receipts HTTP {status}" in caplog.text


@responses.activate
def test_a_two_hundred_that_is_not_json_is_swallowed(caplog):
    _expo_receipts(text="<html>hello</html>", status=200)

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-html", "t")
        traveller.shift(push._RECEIPT_DELAY + 1)

        with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
            assert push.poll_push_receipts() == 0

    assert "Failed to fetch push receipts" in caplog.text


@responses.activate
def test_a_body_that_is_a_json_array_has_no_data_to_read(caplog):
    # .get on a list raises — the except is what keeps a
    # scheduler tick alive
    _expo_receipts([{"status": "ok"}])

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-array", "t")
        traveller.shift(push._RECEIPT_DELAY + 1)

        with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
            assert push.poll_push_receipts() == 0

    assert "Failed to fetch push receipts" in caplog.text


@responses.activate
def test_a_failing_slice_does_not_stop_the_next_one():
    _expo_receipts(text="<html>nope</html>", status=400)
    _expo_receipts({"data": {"ticket-0300": {"status": "ok"}}})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        for i in range(push._RECEIPT_SLICE + 1):
            push._queue_receipt(f"ticket-{i:04d}", "t")
        traveller.shift(push._RECEIPT_DELAY + 1)

        assert push.poll_push_receipts() == 1

    assert len(_receipt_bodies()) == 2


@responses.activate
def test_a_rate_limited_receipt_slice_is_retried_rather_than_dropped(no_backoff):
    _expo_receipts(text="slow down", status=429)
    _expo_receipts({"data": {"ticket-retry": {"status": "ok"}}})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-retry", "t")
        traveller.shift(push._RECEIPT_DELAY + 1)

        assert push.poll_push_receipts() == 1

    assert len(responses.calls) == 2


@responses.activate
def test_a_server_error_is_retried_before_the_slice_is_given_up_on(no_backoff, caplog):
    for _ in range(5):
        _expo_receipts(text="boom", status=503)

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-503", "t")
        traveller.shift(push._RECEIPT_DELAY + 1)

        with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
            assert push.poll_push_receipts() == 0

    assert len(responses.calls) > 1
    assert "Failed to fetch push receipts" in caplog.text




# -----------------------------------------------------------
# poll_push_receipts — body shapes Expo should never send
# -----------------------------------------------------------

@pytest.mark.parametrize("body", [
    {},
    {"data": None},
    {"data": []},
    {"data": "nope"},
    {"data": 7},
    {"errors": [{"code": "SOMETHING"}]},
])
@responses.activate
def test_a_body_with_no_receipt_object_is_logged_and_skipped(body, caplog):
    _expo_receipts(body)

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-shape", "t")
        traveller.shift(push._RECEIPT_DELAY + 1)

        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            assert push.poll_push_receipts() == 0

    assert "unexpected body shape" in caplog.text


@responses.activate
def test_an_empty_receipt_object_is_a_perfectly_good_answer():
    _expo_receipts({"data": {}})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-none-yet", "t")
        traveller.shift(push._RECEIPT_DELAY + 1)

        assert push.poll_push_receipts() == 0


@pytest.mark.parametrize("receipt", ["a string", None, 7, ["ok"], True])
@responses.activate
def test_a_receipt_that_is_not_an_object_is_counted_and_skipped(receipt):
    _expo_receipts({"data": {"ticket-weird": receipt}})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-weird", _expo_token("weird"))
        traveller.shift(push._RECEIPT_DELAY + 1)

        assert push.poll_push_receipts() == 1


@responses.activate
def test_expo_answering_about_tickets_we_never_asked_about_still_counts_them():
    _expo_receipts({"data": {
        "ticket-mine": {"status": "ok"},
        "ticket-stranger-1": {"status": "ok"},
        "ticket-stranger-2": {"status": "ok"},
    }})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-mine", "t")
        traveller.shift(push._RECEIPT_DELAY + 1)

        assert push.poll_push_receipts() == 3


@responses.activate
def test_a_receipt_with_extra_fields_is_read_all_the_same(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("extra"))
    _expo_receipts({"data": {"ticket-extra": {
        "status": "error", "message": "gone", "expoVersion": "3",
        "details": {"error": "DeviceNotRegistered", "sentAt": 1, "fault": "developer"}}}})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-extra", token)
        traveller.shift(push._RECEIPT_DELAY + 1)

        assert push.poll_push_receipts() == 1

    assert _token_row(db, token)["active"] == 0




# -----------------------------------------------------------
# poll_push_receipts — the verdict on each ticket
# -----------------------------------------------------------

@pytest.mark.parametrize("receipt", [
    {"status": "ok"},
    {"status": "ok", "id": "x"},
    {},
    {"status": None},
    {"status": "OK"},
    {"details": {"error": "DeviceNotRegistered"}},
])
@responses.activate
def test_anything_that_is_not_an_error_status_is_merely_counted(app, db, make_user, receipt):
    # Only status == "error" is a verdict; a details block on
    # its own means nothing
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("notanerror"))
    _expo_receipts({"data": {"ticket-fine": receipt}})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-fine", token)
        traveller.shift(push._RECEIPT_DELAY + 1)

        assert push.poll_push_receipts() == 1

    assert _token_row(db, token)["active"] == 1


@pytest.mark.parametrize("details", [None, {}, "boom", ["boom"], 7, {"error": None}, {"error": ""}])
@responses.activate
def test_an_error_the_details_do_not_name_is_logged_as_unknown(details, caplog):
    _expo_receipts({"data": {"ticket-u": {"status": "error", "message": "?", "details": details}}})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-u", _expo_token("u"))
        traveller.shift(push._RECEIPT_DELAY + 1)

        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            assert push.poll_push_receipts() == 1

    assert "Expo receipt error Unknown" in caplog.text


@responses.activate
def test_an_error_with_no_details_key_at_all_is_unknown_too(caplog):
    _expo_receipts({"data": {"ticket-bare": {"status": "error"}}})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-bare", _expo_token("bare"))
        traveller.shift(push._RECEIPT_DELAY + 1)

        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            assert push.poll_push_receipts() == 1

    assert "Expo receipt error Unknown" in caplog.text


@pytest.mark.parametrize("code", ["InvalidCredentials", "MessageRateExceeded", "MessageTooBig",
                                  "DeveloperError", "ExpoError"])
@responses.activate
def test_every_operator_level_failure_is_shouted_about(app, db, make_user, code, caplog):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("operator"))
    _expo_receipts({"data": {"ticket-op": {
        "status": "error", "message": f"{code} happened", "details": {"error": code}}}})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-op", token)
        traveller.shift(push._RECEIPT_DELAY + 1)

        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            assert push.poll_push_receipts() == 1

    assert f"Expo receipt error {code}" in caplog.text
    # Not a dead device: the phone keeps its row
    assert _token_row(db, token)["active"] == 1


@responses.activate
def test_a_receipt_error_names_the_device_only_by_its_digest(app, db, make_user, caplog):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("digestonly"))
    _expo_receipts({"data": {"ticket-d": {
        "status": "error", "message": f"the token {token} is too big",
        "details": {"error": "MessageTooBig"}}}})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-d", token)
        traveller.shift(push._RECEIPT_DELAY + 1)

        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            push.poll_push_receipts()

    assert token not in caplog.text
    assert push.token_digest(token) in caplog.text


@responses.activate
def test_an_upstream_message_cannot_forge_extra_log_lines(caplog):
    _expo_receipts({"data": {"ticket-inject": {
        "status": "error", "message": "line one\nWARNING forged line two",
        "details": {"error": "MessageTooBig"}}}})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-inject", _expo_token("inject"))
        traveller.shift(push._RECEIPT_DELAY + 1)

        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            push.poll_push_receipts()

    assert "line one WARNING forged line two" in caplog.text


@responses.activate
def test_an_error_about_a_ticket_we_never_parked_names_no_device(caplog):
    _expo_receipts({"data": {"ticket-stranger": {
        "status": "error", "details": {"error": "MessageTooBig"}}}})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-mine", _expo_token("mine"))
        traveller.shift(push._RECEIPT_DELAY + 1)

        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            assert push.poll_push_receipts() == 1

    assert "token:unknown" in caplog.text


@responses.activate
def test_an_error_for_a_ticket_parked_without_a_token_names_no_device(caplog):
    _expo_receipts({"data": {"ticket-null": {
        "status": "error", "details": {"error": "MessageTooBig"}}}})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-null", None)
        traveller.shift(push._RECEIPT_DELAY + 1)

        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            assert push.poll_push_receipts() == 1

    assert "token:unknown" in caplog.text




# -----------------------------------------------------------
# poll_push_receipts — retiring the devices Expo says are gone
# -----------------------------------------------------------

@responses.activate
def test_a_device_not_registered_receipt_retires_the_row(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("uninstalled"))
    _expo_receipts({"data": {"ticket-dead": {
        "status": "error", "message": "not registered",
        "details": {"error": "DeviceNotRegistered"}}}})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-dead", token)
        traveller.shift(push._RECEIPT_DELAY + 1)

        assert push.poll_push_receipts() == 1

    row = _token_row(db, token)
    assert row["active"] == 0
    assert row is not None


@responses.activate
def test_every_dead_device_of_a_poll_is_retired_in_one_call(monkeypatch):
    seen = _spy_deactivate(monkeypatch)
    _expo_receipts({"data": {
        "ticket-a": {"status": "error", "details": {"error": "DeviceNotRegistered"}},
        "ticket-b": {"status": "ok"},
        "ticket-c": {"status": "error", "details": {"error": "DeviceNotRegistered"}},
    }})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-a", "token-a")
        push._queue_receipt("ticket-b", "token-b")
        push._queue_receipt("ticket-c", "token-c")
        traveller.shift(push._RECEIPT_DELAY + 1)

        assert push.poll_push_receipts() == 3

    assert seen == [["token-a", "token-c"]]


@responses.activate
def test_dead_devices_found_across_two_slices_are_retired_together(monkeypatch):
    seen = _spy_deactivate(monkeypatch)

    def _reply(request):
        ids = json.loads(request.body)["ids"]
        return 200, {}, json.dumps({"data": {
            ids[0]: {"status": "error", "details": {"error": "DeviceNotRegistered"}}}})

    responses.add_callback(responses.POST, push.EXPO_RECEIPTS_URL,
                           callback=_reply, content_type="application/json")

    with time_machine.travel(FROZEN, tick=False) as traveller:
        for i in range(push._RECEIPT_SLICE + 1):
            push._queue_receipt(f"ticket-{i:04d}", f"token-{i:04d}")
        traveller.shift(push._RECEIPT_DELAY + 1)

        push.poll_push_receipts()

    assert seen == [["token-0000", "token-0300"]]


@responses.activate
def test_a_poll_with_nothing_dead_never_opens_a_connection(monkeypatch):
    seen = _spy_deactivate(monkeypatch)
    _expo_receipts({"data": {"ticket-ok": {"status": "ok"}}})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-ok", "t")
        traveller.shift(push._RECEIPT_DELAY + 1)

        assert push.poll_push_receipts() == 1

    assert seen == []


@responses.activate
def test_a_dead_verdict_for_an_unknown_ticket_retires_nothing_and_crashes_nothing(app, db, make_user, caplog):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("survivor"))
    _expo_receipts({"data": {"ticket-nobody": {
        "status": "error", "details": {"error": "DeviceNotRegistered"}}}})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-ours", token)
        traveller.shift(push._RECEIPT_DELAY + 1)

        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            assert push.poll_push_receipts() == 1

    assert _token_row(db, token)["active"] == 1
    assert "Deactivated" not in caplog.text


@responses.activate
def test_a_dead_device_and_a_live_one_in_the_same_answer(app, db, make_user):
    user = make_user()
    dead = _seed_token(db, user["id"], _expo_token("dead"))
    alive = _seed_token(db, user["id"], _expo_token("alive"))
    _expo_receipts({"data": {
        "ticket-dead": {"status": "error", "details": {"error": "DeviceNotRegistered"}},
        "ticket-alive": {"status": "ok"},
    }})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-dead", dead)
        push._queue_receipt("ticket-alive", alive)
        traveller.shift(push._RECEIPT_DELAY + 1)

        assert push.poll_push_receipts() == 2

    assert _token_row(db, dead)["active"] == 0
    assert _token_row(db, alive)["active"] == 1


@responses.activate
def test_the_same_device_reported_dead_twice_moves_one_row(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("twicedead"))
    _expo_receipts({"data": {
        "ticket-1": {"status": "error", "details": {"error": "DeviceNotRegistered"}},
        "ticket-2": {"status": "error", "details": {"error": "DeviceNotRegistered"}},
    }})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-1", token)
        push._queue_receipt("ticket-2", token)
        traveller.shift(push._RECEIPT_DELAY + 1)

        assert push.poll_push_receipts() == 2

    assert _token_row(db, token)["active"] == 0


@responses.activate
def test_a_poll_survives_the_database_being_unreachable_when_a_device_dies(monkeypatch, caplog):
    monkeypatch.setattr(push, "get_db", _explode)
    _expo_receipts({"data": {"ticket-dead": {
        "status": "error", "details": {"error": "DeviceNotRegistered"}}}})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-dead", _expo_token("nodb"))
        traveller.shift(push._RECEIPT_DELAY + 1)

        with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
            assert push.poll_push_receipts() == 1

    assert "Failed to deactivate tokens" in caplog.text


@responses.activate
def test_the_retirement_a_poll_causes_refreshes_updated_at(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("restamped"))
    _expo_receipts({"data": {"ticket-dead": {
        "status": "error", "details": {"error": "DeviceNotRegistered"}}}})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-dead", token)
        traveller.shift(push._RECEIPT_DELAY + 1)
        push.poll_push_receipts()

    assert _token_row(db, token)["updated_at"] == "2026-08-29T12:15:01"




# -----------------------------------------------------------
# poll_push_receipts — what it reports back
# -----------------------------------------------------------

@responses.activate
def test_the_poll_counts_receipts_read_not_ids_asked_about():
    _expo_receipts({"data": {"ticket-a": {"status": "ok"}}})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-a", "t")
        push._queue_receipt("ticket-b", "t")
        traveller.shift(push._RECEIPT_DELAY + 1)

        assert push.poll_push_receipts() == 1

    assert sorted(_asked_ids()) == ["ticket-a", "ticket-b"]


@responses.activate
def test_the_poll_reports_what_it_checked_and_what_it_retired(app, db, make_user, caplog):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("reported"))
    _expo_receipts({"data": {
        "ticket-dead": {"status": "error", "details": {"error": "DeviceNotRegistered"}},
        "ticket-ok": {"status": "ok"},
    }})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-dead", token)
        push._queue_receipt("ticket-ok", _expo_token("fine"))
        traveller.shift(push._RECEIPT_DELAY + 1)

        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            assert push.poll_push_receipts() == 2

    assert "Push receipts: 2 checked, 1 device(s) retired" in caplog.text


@responses.activate
def test_a_poll_that_found_nothing_due_logs_nothing(caplog):
    _expo_receipts({"data": {}})

    with time_machine.travel(FROZEN, tick=False):
        push._queue_receipt("ticket-young", "t")

        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            assert push.poll_push_receipts() == 0

    assert "Push receipts:" not in caplog.text


@responses.activate
def test_the_second_poll_of_the_same_tickets_asks_nothing():
    _expo_receipts_echo()

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-once", "t")
        traveller.shift(push._RECEIPT_DELAY + 1)

        assert push.poll_push_receipts() == 1
        assert push.poll_push_receipts() == 0

    assert len(_receipt_bodies()) == 1


@responses.activate
def test_the_poll_returns_a_plain_integer():
    _expo_receipts({"data": {"ticket-int": {"status": "ok"}}})

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-int", "t")
        traveller.shift(push._RECEIPT_DELAY + 1)

        checked = push.poll_push_receipts()

    assert isinstance(checked, int) and not isinstance(checked, bool)


@responses.activate
def test_tickets_parked_after_a_poll_wait_for_the_next_one():
    _expo_receipts_echo()

    with time_machine.travel(FROZEN, tick=False) as traveller:
        push._queue_receipt("ticket-first", "t")
        traveller.shift(push._RECEIPT_DELAY + 1)
        assert push.poll_push_receipts() == 1

        push._queue_receipt("ticket-second", "t")
        assert push.poll_push_receipts() == 0
        traveller.shift(push._RECEIPT_DELAY)
        assert push.poll_push_receipts() == 1

    assert _asked_ids() == ["ticket-first", "ticket-second"]
