# -----------------------------------------------------------
#  [*] Tests — chat/events.py: the handshake gate, the socket
#      rate limiter and the revocation kill switch
#
#  The exhaustive pass over three functions the whole live
#  layer hangs from — _authenticate_socket, _socket_rate_check
#  and disconnect_user_sockets. No engineio, no transport: the
#  functions are called directly, with a Flask request context
#  pushed only where the legacy ?token= fallback needs one.
#
#  What this module proves, arm by arm:
#
#    - EXTRACTION. The handshake reads `auth: {token}` first
#      and the legacy query string only when that token is
#      missing or falsy; a truthy token that is not a string
#      is refused outright and never falls back. The string
#      reaches auth's resolve_session_token byte for byte —
#      unstripped, undecorated, case intact.
#    - SESSIONS. Through that one lookup a socket inherits
#      every REST rule: the sha256-at-rest match (migration
#      v13), the aware expiry boundary (equal is still live,
#      one second past is not), the purge of the expired row
#      AND its push tokens, the users.active gate, a session
#      whose user row is gone, and revocation by logout,
#      logout-all, password change and admin deactivation.
#    - THE LIMITER. Every configured event accepts exactly its
#      quota and rejects the next call; rejections are never
#      recorded; the window prunes at exactly WINDOW seconds
#      and a backwards clock cannot reopen it; the key store
#      holds exactly its cap, evicts the least recently
#      touched, and a REJECTED check refreshes a key's place
#      too. Threads racing for the last slot spend it once.
#    - THE KILL SWITCH. Every sid of one user, on the "/"
#      namespace, others untouched; a socket that will not
#      close is not counted but still leaves presence clean;
#      a missing socket layer, a server that is not up, a
#      sid that dies mid-pass and a socket that reconnects
#      mid-pass are all non-events for the caller's route.
# -----------------------------------------------------------

import hashlib
import logging
import sys
import threading
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

import flask
import pytest
import time_machine

from app.chat import events as chat_events


# Read from the module so a retuned constant moves the
# boundary tests with it instead of silently passing
WINDOW = chat_events._SOCKET_RATE_WINDOW
MAX_KEYS = chat_events._SOCKET_RATE_MAX_KEYS
LIMITS = dict(chat_events._SOCKET_RATE_LIMITS)
PER_USER_CAP = chat_events._MAX_SOCKETS_PER_USER

# One fixed instant for every frozen-clock test
FROZEN = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)

# The public column set resolve_session_token narrows to —
# password_hash must never be in it
PUBLIC_COLUMNS = {
    "id", "username", "email", "display_name", "role", "avatar_url",
    "invited", "active", "student_number", "study_group", "study_program",
}




# -----------------------------------------------------------
# clean_sockets
# -----------------------------------------------------------
#
# _connected_users, _connected_names and _socket_rate are
# MODULE-level stores that outlive a test, so they are wiped
# on the way in and on the way out: a leaked sid would make
# the next test's presence lookups lie, and a leaked rate key
# would eat someone else's quota. Deliberately does NOT depend
# on the `app` fixture — the limiter tests need no database.
# -----------------------------------------------------------

@pytest.fixture
def clean_sockets():
    _wipe_socket_state()
    yield
    _wipe_socket_state()


def _wipe_socket_state():
    chat_events._connected_users.clear()
    chat_events._connected_names.clear()
    chat_events._socket_rate.clear()




# -----------------------------------------------------------
# _Disconnects
# -----------------------------------------------------------
#
# Stands in for socketio.server.disconnect. Records every
# (sid, namespace) it was asked to close, can be told to fail
# for chosen sids (the "already gone" race), and can run a
# hook BEFORE each close so a test can mutate the presence
# table from inside the loop the way a real disconnect handler
# on another thread would.
# -----------------------------------------------------------

class _Disconnects:

    def __init__(self):
        self.calls = []
        self.fail_on = set()
        self.before = None

    def __call__(self, sid, namespace=None):
        self.calls.append((sid, namespace))
        if self.before is not None:
            self.before(sid)
        if sid in self.fail_on:
            raise RuntimeError(f"sid {sid} already gone")

    @property
    def sids(self):
        return [sid for sid, _ in self.calls]




# -----------------------------------------------------------
# disconnects
# -----------------------------------------------------------
#
# The recorder above, bound onto the ONE SocketIO singleton
# create_app() initialised, which is what disconnect_user_
# sockets reaches through its lazy `from app import socketio`.
# -----------------------------------------------------------

@pytest.fixture
def disconnects(app, monkeypatch, clean_sockets):
    from app import socketio as real_socketio

    recorder = _Disconnects()
    monkeypatch.setattr(real_socketio.server, "disconnect", recorder)
    return recorder




# -----------------------------------------------------------
# _present / _handshake / _raw_token / _spy_lookup
# -----------------------------------------------------------
#
# _present    — plant a live socket in the presence tables
# _handshake  — run _authenticate_socket inside a request
#               context, the one thing flask-socketio gives
#               the handshake (the query string lives there)
# _raw_token  — the bearer the real login minted, never a
#               hand-built one
# _spy_lookup — replace the session lookup with a recorder,
#               so a test can assert WHICH string the
#               extraction handed on, and whether it bothered
#               to ask at all
# -----------------------------------------------------------

def _present(sid, user_id, display_name="Testas"):
    chat_events._connected_users[sid] = user_id
    chat_events._connected_names[sid] = display_name


def _handshake(app, auth=None, query=""):
    with app.test_request_context("/socket.io/" + query):
        return chat_events._authenticate_socket(auth)


def _raw_token(headers):
    return headers["Authorization"].split(" ", 1)[1]


def _spy_lookup(monkeypatch, result=None):
    seen = []

    def _resolve(token):
        seen.append(token)
        return result

    monkeypatch.setattr(chat_events, "resolve_session_token", _resolve)
    return seen


# The dict a patched lookup hands back — identity-checked, so
# a test can prove the extraction returned the lookup's answer
# untouched
SENTINEL_USER = {"id": "u-sentinel", "display_name": "Testas"}


class _LooksLikeAMapping:
    def get(self, key, default=None):
        return "duck-token"




# -----------------------------------------------------------
# _expire_sessions / _seed_push_token
# -----------------------------------------------------------
#
# State no route can create: an expiry moved by hand (the
# suite never sleeps) and a push row that must die with the
# session it belongs to.
# -----------------------------------------------------------

def _expire_sessions(db, user_id, expires_at):
    db.execute("UPDATE sessions SET expires_at = ? WHERE user_id = ?", (expires_at, user_id))
    db.commit()


def _seed_push_token(db, user_id, token="ExponentPushToken[xxx]"):
    db.execute(
        "INSERT INTO push_tokens (id, user_id, token, platform) VALUES (?, ?, ?, 'ios')",
        (str(uuid.uuid4()), user_id, token),
    )
    db.commit()
    return token




# -----------------------------------------------------------
# _socket_rate_check — the limits table
# -----------------------------------------------------------


@pytest.mark.parametrize("event,limit", sorted(LIMITS.items()))
def test_each_configured_event_accepts_its_whole_quota_then_rejects_the_next(clean_sockets, event, limit):
    with time_machine.travel(FROZEN, tick=False):
        verdicts = [chat_events._socket_rate_check("naudotojas", event) for _ in range(limit)]
        assert verdicts == [False] * limit
        assert chat_events._socket_rate_check("naudotojas", event) is True

    assert len(chat_events._socket_rate[("naudotojas", event)]) == limit


@pytest.mark.parametrize("event,limit", sorted(LIMITS.items()))
def test_one_call_short_of_the_quota_still_leaves_a_slot(clean_sockets, event, limit):
    with time_machine.travel(FROZEN, tick=False):
        for _ in range(limit - 1):
            chat_events._socket_rate_check("naudotojas", event)

        assert chat_events._socket_rate_check("naudotojas", event) is False


@pytest.mark.parametrize("event", [
    "", "unlisted_event", "TYPING", "Typing", " typing", "typing ",
    "mark-read", "connect", "disconnect", "join", None, 7, ("typing",),
])
def test_an_event_outside_the_limits_table_is_never_capped(clean_sockets, event):
    assert all(chat_events._socket_rate_check("naudotojas", event) is False for _ in range(200))
    # An unlimited event costs no storage either
    assert chat_events._socket_rate == {}


def test_a_configured_limit_of_zero_is_treated_as_no_limit_at_all(clean_sockets, monkeypatch):
    # `if not limit` swallows 0 with the missing key — a table
    # entry of 0 would mean "unlimited", never "closed"
    monkeypatch.setitem(chat_events._SOCKET_RATE_LIMITS, "free_event", 0)

    assert all(chat_events._socket_rate_check("naudotojas", "free_event") is False for _ in range(100))
    assert ("naudotojas", "free_event") not in chat_events._socket_rate


def test_a_negative_limit_rejects_every_call_and_records_none_of_them(clean_sockets, monkeypatch):
    monkeypatch.setitem(chat_events._SOCKET_RATE_LIMITS, "shut_event", -1)

    assert all(chat_events._socket_rate_check("naudotojas", "shut_event") is True for _ in range(10))
    # The key exists but stays empty: a rejection is never recorded
    assert chat_events._socket_rate[("naudotojas", "shut_event")] == []


def test_the_limits_table_is_never_mutated_by_a_check(clean_sockets):
    with time_machine.travel(FROZEN, tick=False):
        for event in list(LIMITS) + ["unlisted_event"]:
            for _ in range(3):
                chat_events._socket_rate_check("naudotojas", event)

    assert chat_events._SOCKET_RATE_LIMITS == LIMITS


def test_the_check_answers_with_real_booleans(clean_sockets):
    with time_machine.travel(FROZEN, tick=False):
        types = {type(chat_events._socket_rate_check("naudotojas", "typing"))
                 for _ in range(LIMITS["typing"] + 5)}

    assert types == {bool}




# -----------------------------------------------------------
# _socket_rate_check — the sliding window
# -----------------------------------------------------------


def test_a_stamp_exactly_one_window_old_is_pruned(clean_sockets):
    limit = LIMITS["mark_read"]

    with time_machine.travel(FROZEN, tick=False) as traveller:
        for _ in range(limit):
            chat_events._socket_rate_check("naudotojas", "mark_read")

        # now - t == WINDOW is NOT < WINDOW, so the whole window goes
        traveller.shift(WINDOW)
        assert chat_events._socket_rate_check("naudotojas", "mark_read") is False

    assert len(chat_events._socket_rate[("naudotojas", "mark_read")]) == 1


def test_a_stamp_half_a_second_inside_the_window_still_counts(clean_sockets):
    limit = LIMITS["mark_read"]

    with time_machine.travel(FROZEN, tick=False) as traveller:
        for _ in range(limit):
            chat_events._socket_rate_check("naudotojas", "mark_read")

        traveller.shift(WINDOW - 0.5)
        assert chat_events._socket_rate_check("naudotojas", "mark_read") is True


def test_only_the_expired_half_of_a_window_is_pruned(clean_sockets):
    limit = LIMITS["typing"]
    half = limit // 2

    with time_machine.travel(FROZEN, tick=False) as traveller:
        for _ in range(half):
            chat_events._socket_rate_check("naudotojas", "typing")

        traveller.shift(WINDOW / 2)
        for _ in range(limit - half):
            chat_events._socket_rate_check("naudotojas", "typing")

        assert chat_events._socket_rate_check("naudotojas", "typing") is True

        # Past the first half's window only the later stamps survive,
        # so exactly that many slots come back — no more
        traveller.shift(WINDOW / 2 + 0.5)
        assert [chat_events._socket_rate_check("naudotojas", "typing")
                for _ in range(half)] == [False] * half
        assert chat_events._socket_rate_check("naudotojas", "typing") is True


def test_a_backwards_clock_step_cannot_reopen_a_spent_window(clean_sockets):
    limit = LIMITS["join_conversation"]

    with time_machine.travel(FROZEN, tick=False) as traveller:
        for _ in range(limit):
            chat_events._socket_rate_check("naudotojas", "join_conversation")

        # A negative age is still inside the window: a clock that
        # jumps backwards hands out no free quota
        traveller.shift(-3600)
        assert chat_events._socket_rate_check("naudotojas", "join_conversation") is True


def test_a_key_idle_for_an_hour_keeps_only_its_new_stamp(clean_sockets):
    limit = LIMITS["leave_conversation"]

    with time_machine.travel(FROZEN, tick=False) as traveller:
        for _ in range(limit):
            chat_events._socket_rate_check("naudotojas", "leave_conversation")

        traveller.shift(3600)
        assert chat_events._socket_rate_check("naudotojas", "leave_conversation") is False

    # The prune is the only place a key shrinks, and it rewrote
    # the whole list
    assert len(chat_events._socket_rate[("naudotojas", "leave_conversation")]) == 1


def test_the_budget_is_whole_again_one_window_after_the_last_accepted_call(clean_sockets):
    limit = LIMITS["typing"]

    with time_machine.travel(FROZEN, tick=False) as traveller:
        for _ in range(limit):
            chat_events._socket_rate_check("naudotojas", "typing")

        traveller.shift(WINDOW + 0.5)
        accepted = [chat_events._socket_rate_check("naudotojas", "typing") for _ in range(limit)]

    assert accepted == [False] * limit




# -----------------------------------------------------------
# _socket_rate_check — key isolation
# -----------------------------------------------------------


@pytest.mark.parametrize("user_id", ["", "u", "user-1", "user-10", "ąžuolas", "x" * 10000, 7, None])
def test_every_user_id_shape_gets_a_budget_of_its_own(clean_sockets, user_id):
    limit = LIMITS["mark_read"]

    with time_machine.travel(FROZEN, tick=False):
        assert all(chat_events._socket_rate_check(user_id, "mark_read") is False
                   for _ in range(limit))
        assert chat_events._socket_rate_check(user_id, "mark_read") is True
        # Somebody else's budget is untouched by that spending
        assert chat_events._socket_rate_check("kitas-naudotojas", "mark_read") is False


def test_a_user_id_that_only_looks_like_another_is_a_separate_key(clean_sockets):
    limit = LIMITS["mark_read"]

    with time_machine.travel(FROZEN, tick=False):
        for _ in range(limit):
            chat_events._socket_rate_check("user-1", "mark_read")

        assert chat_events._socket_rate_check("user-1", "mark_read") is True
        assert chat_events._socket_rate_check("user-10", "mark_read") is False
        assert chat_events._socket_rate_check("user-", "mark_read") is False


def test_an_integer_id_and_its_string_form_do_not_share_a_budget(clean_sockets):
    limit = LIMITS["typing"]

    with time_machine.travel(FROZEN, tick=False):
        for _ in range(limit):
            chat_events._socket_rate_check("7", "typing")

        assert chat_events._socket_rate_check("7", "typing") is True
        assert chat_events._socket_rate_check(7, "typing") is False


def test_the_store_is_keyed_by_the_user_and_the_event_together(clean_sockets):
    with time_machine.travel(FROZEN, tick=False):
        chat_events._socket_rate_check("a", "typing")
        chat_events._socket_rate_check("a", "mark_read")
        chat_events._socket_rate_check("b", "typing")

    assert set(chat_events._socket_rate) == {("a", "typing"), ("a", "mark_read"), ("b", "typing")}


def test_exhausting_one_event_leaves_every_other_event_of_that_user_whole(clean_sockets):
    with time_machine.travel(FROZEN, tick=False):
        for _ in range(LIMITS["typing"]):
            chat_events._socket_rate_check("naudotojas", "typing")

        assert chat_events._socket_rate_check("naudotojas", "typing") is True
        for event in LIMITS:
            if event != "typing":
                assert chat_events._socket_rate_check("naudotojas", event) is False




# -----------------------------------------------------------
# _socket_rate_check — the bounded key store
# -----------------------------------------------------------


def test_the_store_holds_the_whole_key_cap_without_evicting_anything(clean_sockets):
    with time_machine.travel(FROZEN, tick=False):
        for index in range(MAX_KEYS):
            chat_events._socket_rate_check(f"user-{index}", "typing")

    assert len(chat_events._socket_rate) == MAX_KEYS
    assert ("user-0", "typing") in chat_events._socket_rate


def test_one_key_past_the_cap_evicts_exactly_the_oldest(clean_sockets):
    with time_machine.travel(FROZEN, tick=False):
        for index in range(MAX_KEYS + 1):
            chat_events._socket_rate_check(f"user-{index}", "typing")

    assert len(chat_events._socket_rate) == MAX_KEYS
    assert ("user-0", "typing") not in chat_events._socket_rate
    assert ("user-1", "typing") in chat_events._socket_rate
    assert (f"user-{MAX_KEYS}", "typing") in chat_events._socket_rate


def test_the_store_never_grows_past_the_cap_however_many_keys_arrive(clean_sockets):
    with time_machine.travel(FROZEN, tick=False):
        for index in range(MAX_KEYS + 50):
            chat_events._socket_rate_check(f"user-{index}", "typing")

    assert len(chat_events._socket_rate) == MAX_KEYS
    assert ("user-49", "typing") not in chat_events._socket_rate
    assert ("user-50", "typing") in chat_events._socket_rate


def test_an_accepted_check_moves_its_key_out_of_the_eviction_line(clean_sockets):
    with time_machine.travel(FROZEN, tick=False):
        for index in range(MAX_KEYS):
            chat_events._socket_rate_check(f"user-{index}", "typing")

        # Touching the oldest key again puts it at the back
        assert chat_events._socket_rate_check("user-0", "typing") is False
        chat_events._socket_rate_check("naujokas", "typing")

    assert len(chat_events._socket_rate) == MAX_KEYS
    assert ("user-0", "typing") in chat_events._socket_rate
    assert ("user-1", "typing") not in chat_events._socket_rate


def test_a_rejected_check_refreshes_its_key_place_in_the_line_too(clean_sockets):
    limit = LIMITS["typing"]

    with time_machine.travel(FROZEN, tick=False):
        # The over-quota key goes in FIRST, so it is the eviction
        # candidate until something moves it
        for _ in range(limit):
            chat_events._socket_rate_check("karstas", "typing")
        for index in range(MAX_KEYS - 1):
            chat_events._socket_rate_check(f"user-{index}", "typing")

        assert len(chat_events._socket_rate) == MAX_KEYS
        assert chat_events._socket_rate_check("karstas", "typing") is True

        chat_events._socket_rate_check("naujokas", "typing")

    assert ("karstas", "typing") in chat_events._socket_rate
    assert ("user-0", "typing") not in chat_events._socket_rate




# -----------------------------------------------------------
# _socket_rate_check — the lock
# -----------------------------------------------------------


def test_threads_racing_for_the_last_slot_spend_it_exactly_once(clean_sockets):
    limit = LIMITS["mark_read"]
    workers = 16
    per_worker = 5
    gate = threading.Barrier(workers)
    tally = []
    tally_lock = threading.Lock()

    def _spend():
        gate.wait()
        mine = sum(1 for _ in range(per_worker)
                   if chat_events._socket_rate_check("lenktynes", "mark_read") is False)
        with tally_lock:
            tally.append(mine)

    threads = [threading.Thread(target=_spend) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Without the lock two threads read the same window, both saw
    # room and both recorded — the quota would be spent twice over
    assert sum(tally) == limit
    assert len(chat_events._socket_rate[("lenktynes", "mark_read")]) == limit


def test_threads_on_their_own_keys_each_get_their_own_whole_quota(clean_sockets):
    limit = LIMITS["join_conversation"]
    workers = 8
    gate = threading.Barrier(workers)
    results = {}
    results_lock = threading.Lock()

    def _spend(index):
        gate.wait()
        verdicts = [chat_events._socket_rate_check(f"gija-{index}", "join_conversation")
                    for _ in range(limit + 1)]
        with results_lock:
            results[index] = verdicts

    threads = [threading.Thread(target=_spend, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == workers
    for verdicts in results.values():
        assert verdicts == [False] * limit + [True]
    assert len(chat_events._socket_rate) == workers




# -----------------------------------------------------------
# _authenticate_socket — where the token comes from
# -----------------------------------------------------------


def test_the_auth_payload_token_reaches_the_lookup_untouched(app, clean_sockets, monkeypatch):
    seen = _spy_lookup(monkeypatch, SENTINEL_USER)

    assert _handshake(app, {"token": "  ŽETONAS-Ąčę  "}) is SENTINEL_USER
    # No strip, no case fold, no decoration
    assert seen == ["  ŽETONAS-Ąčę  "]


def test_the_query_string_is_read_only_when_the_payload_has_no_token(app, clean_sockets, monkeypatch):
    seen = _spy_lookup(monkeypatch, SENTINEL_USER)

    _handshake(app, {"token": "payload"}, query="?token=query")

    assert seen == ["payload"]


@pytest.mark.parametrize("auth", [
    None, {}, {"token": None}, {"token": ""}, {"token": 0}, {"token": False},
    {"token": []}, {"token": {}}, {"kitas": "raktas"},
    [], (), set(), "", "raw-token", 0, 42, True, 3.5, object(), _LooksLikeAMapping(),
])
def test_a_payload_without_a_usable_token_falls_back_to_the_query_string(app, clean_sockets, monkeypatch, auth):
    seen = _spy_lookup(monkeypatch, SENTINEL_USER)

    assert _handshake(app, auth, query="?token=legacy") is SENTINEL_USER
    assert seen == ["legacy"]


@pytest.mark.parametrize("token", [1, 42, 3.5, b"tok", bytearray(b"tok"), ["tok"], {"t": 1}, object(), True])
def test_a_truthy_token_that_is_not_a_string_is_refused_with_no_query_fallback(app, clean_sockets, monkeypatch, token):
    seen = _spy_lookup(monkeypatch, SENTINEL_USER)

    # Truthy skips the fallback, then the isinstance gate drops it —
    # a live legacy token in the query cannot rescue it
    assert _handshake(app, {"token": token}, query="?token=legacy") is None
    assert seen == []


def test_no_token_anywhere_never_reaches_the_session_lookup(app, clean_sockets, monkeypatch):
    seen = _spy_lookup(monkeypatch, SENTINEL_USER)

    assert _handshake(app, {}) is None
    assert _handshake(app, {"token": ""}, query="?token=") is None
    assert _handshake(app, None) is None
    assert seen == []


def test_a_dict_subclass_payload_is_read_like_a_dict(app, clean_sockets, monkeypatch):
    seen = _spy_lookup(monkeypatch, SENTINEL_USER)

    assert _handshake(app, OrderedDict(token="zetonas"), query="?token=legacy") is SENTINEL_USER
    assert seen == ["zetonas"]


def test_the_first_of_two_query_tokens_wins(app, clean_sockets, monkeypatch):
    seen = _spy_lookup(monkeypatch, SENTINEL_USER)

    _handshake(app, None, query="?token=pirmas&token=antras")

    assert seen == ["pirmas"]


def test_the_query_parameter_name_is_case_sensitive(app, clean_sockets, monkeypatch):
    seen = _spy_lookup(monkeypatch, SENTINEL_USER)

    assert _handshake(app, None, query="?Token=legacy") is None
    assert _handshake(app, None, query="?TOKEN=legacy") is None
    assert _handshake(app, None, query="?access_token=legacy") is None
    assert seen == []


def test_a_percent_encoded_query_token_is_decoded_before_the_lookup(app, clean_sockets, monkeypatch):
    seen = _spy_lookup(monkeypatch, SENTINEL_USER)

    _handshake(app, None, query="?token=a%20b%2Bc")

    assert seen == ["a b+c"]


def test_a_whitespace_only_token_is_handed_on_rather_than_trimmed_away(app, clean_sockets, monkeypatch):
    seen = _spy_lookup(monkeypatch, SENTINEL_USER)

    _handshake(app, {"token": "   "})

    assert seen == ["   "]


def test_the_lookups_answer_is_returned_untouched(app, clean_sockets, monkeypatch):
    _spy_lookup(monkeypatch, None)

    # None from the lookup is None from the handshake — no
    # invented fallback user
    assert _handshake(app, {"token": "bet-koks"}) is None


def test_the_auth_payload_path_never_touches_the_request(clean_sockets, actor):
    user, headers = actor

    # No request context at all: the query-string fallback is
    # reached ONLY when the payload has no token
    assert not flask.has_request_context()
    resolved = chat_events._authenticate_socket({"token": _raw_token(headers)})

    assert resolved["id"] == user["id"]




# -----------------------------------------------------------
# _authenticate_socket — the session the token names
# -----------------------------------------------------------


def test_a_live_token_resolves_to_the_public_user_columns(app, clean_sockets, actor):
    user, headers = actor

    resolved = _handshake(app, {"token": _raw_token(headers)})

    assert resolved["id"] == user["id"]
    assert set(resolved) == PUBLIC_COLUMNS
    assert "password_hash" not in resolved
    # A plain dict, not a sqlite3.Row — handle_connect indexes it
    assert isinstance(resolved, dict)


def test_the_legacy_query_token_resolves_the_same_session(app, clean_sockets, actor):
    user, headers = actor

    resolved = _handshake(app, None, query=f"?token={_raw_token(headers)}")

    assert resolved["id"] == user["id"]


@pytest.mark.parametrize("role", ["student", "teacher", "curator", "admin"])
def test_a_handshake_resolves_a_user_of_every_role(app, clean_sockets, make_user, auth_headers, role):
    user = make_user(role=role)

    resolved = _handshake(app, {"token": _raw_token(auth_headers(user))})

    assert resolved["role"] == role
    assert resolved["id"] == user["id"]


def test_the_session_row_keeps_only_the_hash_yet_the_raw_token_authenticates(app, clean_sockets, actor, db):
    user, headers = actor
    raw = _raw_token(headers)

    stored = db.execute("SELECT token FROM sessions WHERE user_id = ?", (user["id"],)).fetchone()["token"]

    assert stored == hashlib.sha256(raw.encode()).hexdigest()
    assert stored != raw
    assert _handshake(app, {"token": raw})["id"] == user["id"]


@pytest.mark.parametrize("token", [
    " ", "   ", "\n", "\t", "not-a-token", "bogus", "Bearer abc",
    "tok\x00en", "žetonas-ąčę", "x" * 100000, "%00", "' OR 1=1 --",
])
def test_a_token_matching_no_session_is_refused(app, clean_sockets, actor, token):
    assert _handshake(app, {"token": token}) is None


def test_a_random_uuid_token_is_refused(app, clean_sockets, actor):
    assert _handshake(app, {"token": str(uuid.uuid4())}) is None


def test_a_token_one_character_off_a_live_one_is_refused(app, clean_sockets, actor):
    raw = _raw_token(actor[1])
    mangled = raw[:-1] + ("a" if raw[-1] != "a" else "b")

    assert _handshake(app, {"token": mangled}) is None
    assert _handshake(app, {"token": raw[:-1]}) is None
    assert _handshake(app, {"token": raw + "x"}) is None
    assert _handshake(app, {"token": raw}) is not None


def test_resolving_the_same_token_twice_returns_the_same_user(app, clean_sockets, actor, db):
    user, headers = actor
    raw = _raw_token(headers)

    first = _handshake(app, {"token": raw})
    second = _handshake(app, {"token": raw})

    assert first == second
    # A handshake never consumes the session — a second device
    # authenticates with the very same token
    assert db.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ?",
                      (user["id"],)).fetchone()[0] == 1


def test_a_session_expiring_at_this_very_instant_still_authenticates(app, clean_sockets, actor, db):
    user, headers = actor
    _expire_sessions(db, user["id"], FROZEN.isoformat())

    with time_machine.travel(FROZEN, tick=False):
        # expires < now is FALSE when they are equal
        assert _handshake(app, {"token": _raw_token(headers)})["id"] == user["id"]


def test_a_session_one_second_past_its_expiry_is_refused_and_purged(app, clean_sockets, actor, db):
    user, headers = actor
    _expire_sessions(db, user["id"], FROZEN.isoformat())

    with time_machine.travel(FROZEN + timedelta(seconds=1), tick=False):
        assert _handshake(app, {"token": _raw_token(headers)}) is None

    assert db.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ?",
                      (user["id"],)).fetchone()[0] == 0


def test_a_naive_expiry_in_the_future_is_read_as_utc_and_accepted(app, clean_sockets, actor, db):
    user, headers = actor
    future = (datetime.now(timezone.utc) + timedelta(days=1)).replace(tzinfo=None).isoformat()
    _expire_sessions(db, user["id"], future)

    assert _handshake(app, {"token": _raw_token(headers)})["id"] == user["id"]


def test_a_naive_expiry_in_the_past_is_read_as_utc_and_refused(app, clean_sockets, actor, db):
    user, headers = actor
    past = (datetime.now(timezone.utc) - timedelta(days=1)).replace(tzinfo=None).isoformat()
    _expire_sessions(db, user["id"], past)

    assert _handshake(app, {"token": _raw_token(headers)}) is None


@pytest.mark.parametrize("expires_at", ["", "labas", "2026-13-45", "0", "netikra data"])
def test_a_malformed_expiry_counts_as_expired(app, clean_sockets, actor, db, expires_at):
    user, headers = actor
    _expire_sessions(db, user["id"], expires_at)

    assert _handshake(app, {"token": _raw_token(headers)}) is None
    assert db.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ?",
                      (user["id"],)).fetchone()[0] == 0


def test_an_expired_session_takes_the_users_push_tokens_with_it(app, clean_sockets, actor, db):
    user, headers = actor
    _seed_push_token(db, user["id"])
    _expire_sessions(db, user["id"], (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())

    assert _handshake(app, {"token": _raw_token(headers)}) is None
    assert db.execute("SELECT COUNT(*) FROM push_tokens WHERE user_id = ?",
                      (user["id"],)).fetchone()[0] == 0


def test_a_deactivated_user_cannot_open_a_socket(app, clean_sockets, actor, db):
    user, headers = actor
    db.execute("UPDATE users SET active = 0 WHERE id = ?", (user["id"],))
    db.commit()

    assert _handshake(app, {"token": _raw_token(headers)}) is None
    # Only expiry purges — the row is left for the admin to see
    assert db.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ?",
                      (user["id"],)).fetchone()[0] == 1


def test_reactivating_a_user_lets_the_same_token_back_in(app, clean_sockets, actor, db):
    user, headers = actor
    raw = _raw_token(headers)

    db.execute("UPDATE users SET active = 0 WHERE id = ?", (user["id"],))
    db.commit()
    assert _handshake(app, {"token": raw}) is None

    db.execute("UPDATE users SET active = 1 WHERE id = ?", (user["id"],))
    db.commit()
    assert _handshake(app, {"token": raw})["id"] == user["id"]


def test_a_session_whose_user_row_is_gone_is_refused(app, clean_sockets, actor, db):
    user, headers = actor
    # The conftest connection leaves PRAGMA foreign_keys off, so
    # the session row outlives the user it points at
    db.execute("DELETE FROM users WHERE id = ?", (user["id"],))
    db.commit()

    assert _handshake(app, {"token": _raw_token(headers)}) is None


def test_a_lone_surrogate_token_is_refused_rather_than_crashing(app, clean_sockets):
    assert _handshake(app, {"token": "\ud800"}) is None




# -----------------------------------------------------------
# _authenticate_socket — revocation, end to end
# -----------------------------------------------------------


def test_a_logged_out_token_can_no_longer_open_a_socket(app, client, clean_sockets, actor):
    user, headers = actor
    raw = _raw_token(headers)
    assert _handshake(app, {"token": raw}) is not None

    assert client.post("/api/auth/logout", headers=headers).status_code == 200

    assert _handshake(app, {"token": raw}) is None


def test_logout_all_kills_the_handshake_for_every_device(app, client, clean_sockets, make_user, auth_headers):
    user = make_user()
    phone = auth_headers(user)
    laptop = auth_headers(user)

    assert client.post("/api/auth/logout-all", headers=phone).status_code == 200

    assert _handshake(app, {"token": _raw_token(phone)}) is None
    assert _handshake(app, {"token": _raw_token(laptop)}) is None


def test_a_password_change_keeps_the_rotating_device_and_drops_the_others(app, client, clean_sockets, make_user, auth_headers):
    user = make_user()
    phone = auth_headers(user)
    laptop = auth_headers(user)

    response = client.post("/api/auth/change-password", headers=laptop, json={
        "oldPassword": user["password"],
        "newPassword": "NaujasSlaptazodis2026",
    })
    assert response.status_code == 200

    assert _handshake(app, {"token": _raw_token(phone)}) is None
    assert _handshake(app, {"token": _raw_token(laptop)})["id"] == user["id"]


def test_an_admin_deactivation_kills_the_handshake(app, client, clean_sockets, admin, make_user, auth_headers):
    _, admin_headers = admin
    target = make_user()
    token = _raw_token(auth_headers(target))

    response = client.patch(f"/api/admin/users/{target['id']}", headers=admin_headers,
                            json={"active": False})
    assert response.status_code == 200

    assert _handshake(app, {"token": token}) is None




# -----------------------------------------------------------
# disconnect_user_sockets — who gets cut
# -----------------------------------------------------------


def test_every_socket_of_the_named_user_is_closed_on_the_default_namespace(disconnects):
    _present("phone", "user-1")
    _present("laptop", "user-1")
    _present("tablet", "user-1")
    _present("kitas", "user-2")

    assert chat_events.disconnect_user_sockets("user-1") == 3
    assert sorted(disconnects.sids) == ["laptop", "phone", "tablet"]
    assert {namespace for _, namespace in disconnects.calls} == {"/"}
    assert chat_events._connected_users == {"kitas": "user-2"}
    assert chat_events._connected_names == {"kitas": "Testas"}


def test_the_kill_switch_runs_outside_any_flask_context(disconnects):
    assert not flask.has_request_context()
    assert not flask.has_app_context()
    _present("phone", "user-1")

    assert chat_events.disconnect_user_sockets("user-1") == 1


def test_an_empty_presence_table_closes_nothing(disconnects):
    assert chat_events.disconnect_user_sockets("user-1") == 0
    assert disconnects.calls == []


@pytest.mark.parametrize("user_id", [None, "", "user-2", "USER-1", "user-1 ", 7])
def test_a_user_id_nothing_is_keyed_by_closes_nothing(disconnects, user_id):
    _present("phone", "user-1")

    assert chat_events.disconnect_user_sockets(user_id) == 0
    assert disconnects.calls == []
    assert chat_events._connected_users == {"phone": "user-1"}


def test_a_user_id_that_is_a_prefix_of_another_matches_only_itself(disconnects):
    _present("phone", "user-1")
    _present("laptop", "user-10")

    assert chat_events.disconnect_user_sockets("user-1") == 1
    assert disconnects.sids == ["phone"]
    assert chat_events._connected_users == {"laptop": "user-10"}


def test_the_count_is_a_plain_int(disconnects):
    _present("phone", "user-1")

    closed = chat_events.disconnect_user_sockets("user-1")

    assert type(closed) is int


def test_a_user_at_the_per_user_socket_cap_loses_every_one_of_them(disconnects):
    for index in range(PER_USER_CAP):
        _present(f"sid-{index}", "user-1")

    assert chat_events.disconnect_user_sockets("user-1") == PER_USER_CAP
    assert chat_events._connected_users == {}


def test_a_large_presence_table_is_closed_in_full(disconnects):
    for index in range(200):
        _present(f"sid-{index}", "user-1")
    _present("kitas", "user-2")

    assert chat_events.disconnect_user_sockets("user-1") == 200
    assert chat_events._connected_users == {"kitas": "user-2"}


def test_a_second_kill_finds_nothing_left_to_close(disconnects):
    _present("phone", "user-1")

    assert chat_events.disconnect_user_sockets("user-1") == 1
    assert chat_events.disconnect_user_sockets("user-1") == 0
    assert disconnects.sids == ["phone"]


def test_a_sid_missing_from_the_name_cache_is_no_error(disconnects):
    chat_events._connected_users["phone"] = "user-1"

    assert chat_events.disconnect_user_sockets("user-1") == 1
    assert chat_events._connected_names == {}




# -----------------------------------------------------------
# disconnect_user_sockets — the failure paths
# -----------------------------------------------------------


def test_a_socket_that_will_not_close_is_not_counted_but_still_leaves_presence(disconnects):
    disconnects.fail_on = {"laptop"}
    _present("phone", "user-1")
    _present("laptop", "user-1")
    _present("tablet", "user-1")

    # The loop carries on past the failure: two of three closed,
    # all three gone from presence
    assert chat_events.disconnect_user_sockets("user-1") == 2
    assert sorted(disconnects.sids) == ["laptop", "phone", "tablet"]
    assert chat_events._connected_users == {}
    assert chat_events._connected_names == {}


def test_a_socket_layer_whose_server_is_not_up_closes_nothing_yet_clears_presence(app, monkeypatch, clean_sockets):
    from app import socketio as real_socketio

    monkeypatch.setattr(real_socketio, "server", None)
    _present("phone", "user-1")

    assert chat_events.disconnect_user_sockets("user-1") == 0
    assert chat_events._connected_users == {}


def test_a_missing_socket_layer_leaves_presence_exactly_as_it_was(app, monkeypatch, clean_sockets):
    monkeypatch.delattr(sys.modules["app"], "socketio")
    _present("phone", "user-1")
    _present("laptop", "user-1")

    # Nothing was closed, so nothing may be dropped from presence
    assert chat_events.disconnect_user_sockets("user-1") == 0
    assert chat_events._connected_users == {"phone": "user-1", "laptop": "user-1"}
    assert chat_events._connected_names == {"phone": "Testas", "laptop": "Testas"}


def test_a_sid_that_dies_mid_pass_does_not_break_the_loop(disconnects):
    _present("pirmas", "user-1")
    _present("antras", "user-1")

    # The disconnect handler on another thread pops the sid the
    # snapshot is still holding
    def _drop_the_other(sid):
        chat_events._connected_users.pop("antras", None)
        chat_events._connected_names.pop("antras", None)

    disconnects.before = _drop_the_other

    assert chat_events.disconnect_user_sockets("user-1") == 2
    assert chat_events._connected_users == {}


def test_a_socket_that_reconnects_mid_pass_survives_the_kill(disconnects):
    _present("senas", "user-1")

    def _reconnect(sid):
        chat_events._connected_users["naujas"] = "user-1"
        chat_events._connected_names["naujas"] = "Testas"

    disconnects.before = _reconnect

    # The list() snapshot predates the new sid, so the kill is
    # honestly best-effort: a reconnect that lands mid-pass lives
    assert chat_events.disconnect_user_sockets("user-1") == 1
    assert chat_events._connected_users == {"naujas": "user-1"}




# -----------------------------------------------------------
# disconnect_user_sockets — what it logs
# -----------------------------------------------------------


def test_a_kill_that_closed_nothing_logs_no_summary(disconnects, caplog):
    _present("kitas", "user-2")

    with caplog.at_level(logging.INFO, logger="app.chat.events"):
        assert chat_events.disconnect_user_sockets("user-1") == 0

    assert "Disconnected" not in caplog.text


def test_closed_sockets_are_summarised_in_one_line(disconnects, caplog):
    _present("phone", "user-1")
    _present("laptop", "user-1")

    with caplog.at_level(logging.INFO, logger="app.chat.events"):
        chat_events.disconnect_user_sockets("user-1")

    assert "Disconnected 2 socket(s) for user=user-1" in caplog.text


def test_a_socket_that_will_not_close_is_logged_as_a_warning(disconnects, caplog):
    disconnects.fail_on = {"phone"}
    _present("phone", "user-1")

    with caplog.at_level(logging.WARNING, logger="app.chat.events"):
        chat_events.disconnect_user_sockets("user-1")

    assert "Could not disconnect socket sid=phone user=user-1" in caplog.text


def test_a_missing_socket_layer_is_logged_as_a_warning(app, monkeypatch, clean_sockets, caplog):
    monkeypatch.delattr(sys.modules["app"], "socketio")

    with caplog.at_level(logging.WARNING, logger="app.chat.events"):
        chat_events.disconnect_user_sockets("user-1")

    assert "Socket layer unavailable" in caplog.text




# -----------------------------------------------------------
# disconnect_user_sockets — the routes that call it
# -----------------------------------------------------------


def test_logging_out_cuts_that_users_live_sockets(client, actor, disconnects):
    user, headers = actor
    _present("phone", user["id"])
    _present("laptop", user["id"])
    _present("kitas", "svetimas")

    assert client.post("/api/auth/logout", headers=headers).status_code == 200

    assert sorted(disconnects.sids) == ["laptop", "phone"]
    assert chat_events._connected_users == {"kitas": "svetimas"}


def test_logging_out_everywhere_cuts_every_device(client, make_user, auth_headers, disconnects):
    user = make_user()
    phone = auth_headers(user)
    auth_headers(user)
    _present("phone", user["id"])
    _present("laptop", user["id"])

    assert client.post("/api/auth/logout-all", headers=phone).status_code == 200

    assert sorted(disconnects.sids) == ["laptop", "phone"]
    assert chat_events._connected_users == {}


def test_a_password_change_cuts_the_live_sockets_too(client, make_user, auth_headers, disconnects):
    user = make_user()
    headers = auth_headers(user)
    _present("phone", user["id"])

    response = client.post("/api/auth/change-password", headers=headers, json={
        "oldPassword": user["password"],
        "newPassword": "NaujasSlaptazodis2026",
    })

    assert response.status_code == 200
    assert disconnects.sids == ["phone"]


def test_deactivating_a_user_cuts_their_sockets_and_nobody_elses(client, admin, make_user, disconnects):
    _, admin_headers = admin
    target = make_user()
    _present("phone", target["id"])
    _present("kitas", "svetimas")

    response = client.patch(f"/api/admin/users/{target['id']}", headers=admin_headers,
                            json={"active": False})

    assert response.status_code == 200
    assert disconnects.sids == ["phone"]
    assert chat_events._connected_users == {"kitas": "svetimas"}


def test_reactivating_a_user_cuts_nothing(client, admin, make_user, disconnects):
    _, admin_headers = admin
    target = make_user(active=0)
    _present("phone", target["id"])

    response = client.patch(f"/api/admin/users/{target['id']}", headers=admin_headers,
                            json={"active": True})

    assert response.status_code == 200
    assert disconnects.calls == []
    assert chat_events._connected_users == {"phone": target["id"]}


def test_a_socket_layer_failure_never_fails_the_logout_route(client, actor, disconnects):
    user, headers = actor
    disconnects.fail_on = {"phone"}
    _present("phone", user["id"])

    assert client.post("/api/auth/logout", headers=headers).status_code == 200
    assert chat_events._connected_users == {}
