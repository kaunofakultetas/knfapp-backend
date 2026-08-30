# -----------------------------------------------------------
#  [*] Tests — the shared gate primitives of app/auth/routes.py
#
#  One slice, exhaustively: the five helpers every OTHER
#  blueprint leans on, tested as units rather than through the
#  routes that happen to call them.
#
#    _check_rate_limit      the budget probe + recorder
#    _record_attempt        the record half of a record=False probe
#    _rate_limited_response the house 429 with Retry-After
#    rate_limit             the decorator on every write route
#    get_json_object        dict-or-None body parsing
#
#  What this module proves:
#
#    - the budget boundary in both directions (0, 1, max-1,
#      max, max+1, negative, huge), and that a rejected call
#      spends nothing while still pruning and LRU-refreshing
#      the key it rejected
#    - the 5-minute window is a STRICT less-than: a stamp
#      exactly one window old no longer counts, one a hair
#      inside it still does — pinned against a frozen clock,
#      not a sleep
#    - the store cannot grow through any door: a pure probe
#      plants nothing, a window that empties deletes its key,
#      and both writers trim to the LRU ceiling however far
#      over it they start
#    - Retry-After is computed off the OLDEST live stamp,
#      ignores stamps that already aged out, never drops below
#      one second and stays positive even if the monotonic
#      clock steps backwards
#    - the decorator keys signed-in callers by user id and
#      anonymous ones by IP (falling back to "unknown"),
#      isolates scopes, shares a scope across routes, and never
#      lets the handler body run once the budget is gone —
#      including the documented trap of stacking it ABOVE
#      require_auth, where it silently meters by IP instead
#    - get_json_object answers a dict ONLY for a JSON object:
#      arrays, scalars, null, malformed bytes, invalid UTF-8, a
#      missing body and a body without a JSON content type all
#      come back None, so no caller's data.get() can 500
#
#  No wall-clock sleeping and no network: the limiter's clock
#  is swapped for a frozen one (time_machine deliberately does
#  not patch time.monotonic, which is what this module uses),
#  and every body is built in a local request context.
# -----------------------------------------------------------

import json
import threading

import pytest
from flask import jsonify, request
from werkzeug.exceptions import RequestEntityTooLarge

from app.auth import routes as auth_routes
from app.auth.routes import (
    _check_rate_limit,
    _rate_limit_store,
    _rate_limited_response,
    _record_attempt,
    _RATE_LIMIT_MAX,
    _RATE_LIMIT_MAX_KEYS,
    _RATE_LIMIT_WINDOW,
    get_json_object,
    rate_limit,
    require_auth,
)




# -----------------------------------------------------------
# _clean_store
# -----------------------------------------------------------
#
# The limiter store is a module-level OrderedDict that outlives
# any test — the `app` fixture rebuilds the database, never
# this dict. Cleared on both sides so no sibling module's
# failed logins leak in and none of the floods below leak out.
#
# Used by:
#   - every test in this module (autouse)
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_store():
    _rate_limit_store.clear()
    yield
    _rate_limit_store.clear()




# -----------------------------------------------------------
# _FrozenClock
# -----------------------------------------------------------
#
# A stand-in for the `time` module inside auth/routes.py: the
# module calls time.monotonic() and nothing else, so replacing
# the whole name with this gives a monotonic clock a test can
# park on an exact second. time_machine cannot do this job —
# it deliberately leaves time.monotonic alone.
#
# Used by:
#   - every window-boundary and Retry-After test below
# -----------------------------------------------------------

class _FrozenClock:

    def __init__(self, now=0.0):
        self.now = now

    def monotonic(self):
        return self.now


def _freeze(monkeypatch, now=0.0):
    clock = _FrozenClock(now)
    monkeypatch.setattr(auth_routes, "time", clock)
    return clock




# -----------------------------------------------------------
# _plant / _stamps / _age
# -----------------------------------------------------------
#
# Arranging and reading the limiter's window without going
# through a route: plant an exact list of monotonic stamps,
# read back what a key holds, and push every stamp further
# into the past (what wall-clock passage would do, without
# sleeping).
#
# Used by:
#   - the window, LRU and Retry-After tests below
# -----------------------------------------------------------

def _plant(key, *stamps):
    with auth_routes._rate_limit_lock:
        _rate_limit_store[key] = list(stamps)


def _stamps(key):
    return list(_rate_limit_store.get(key, []))


def _age(seconds=None):
    shift = _RATE_LIMIT_WINDOW + 1 if seconds is None else seconds
    with auth_routes._rate_limit_lock:
        for key in list(_rate_limit_store):
            _rate_limit_store[key] = [stamp - shift for stamp in _rate_limit_store[key]]




# -----------------------------------------------------------
# _parse
# -----------------------------------------------------------
#
# get_json_object() over one hand-built request, bytes on the
# wire exactly as posted — never through `json=`, which the
# app's own escaping provider would rewrite (TESTPLAN rule 10).
# A `content_type` of None means the request carries no
# Content-Type header at all.
#
# Used by:
#   - every get_json_object test below
# -----------------------------------------------------------

def _parse(app, body=None, content_type="application/json", method="POST"):
    kwargs = {"method": method}
    if body is not None:
        kwargs["data"] = body
    if content_type is not None:
        kwargs["content_type"] = content_type
    with app.test_request_context("/api/_kunas", **kwargs):
        return get_json_object()




# ===========================================================
# _check_rate_limit — the budget
# ===========================================================

def test_a_fresh_key_is_allowed_and_starts_its_window():
    assert _check_rate_limit("biudzetas:naujas") is False

    assert len(_stamps("biudzetas:naujas")) == 1


def test_a_budget_of_one_allows_exactly_one_call():
    assert _check_rate_limit("biudzetas:vienas", max_attempts=1) is False

    assert _check_rate_limit("biudzetas:vienas", max_attempts=1) is True


def test_every_attempt_up_to_the_budget_is_allowed_and_the_next_is_not():
    verdicts = [_check_rate_limit("biudzetas:trys", max_attempts=3) for _ in range(4)]

    assert verdicts == [False, False, False, True]


def test_the_default_budget_is_the_module_wide_ten():
    assert _RATE_LIMIT_MAX == 10
    for _ in range(_RATE_LIMIT_MAX):
        assert _check_rate_limit("biudzetas:numatytas") is False

    assert _check_rate_limit("biudzetas:numatytas") is True


def test_a_zero_budget_rejects_the_very_first_call():
    # len([]) >= 0 — there is no free attempt to spend
    assert _check_rate_limit("biudzetas:nulis", max_attempts=0) is True


def test_a_negative_budget_rejects_the_very_first_call():
    assert _check_rate_limit("biudzetas:neigiamas", max_attempts=-1) is True


def test_a_rejected_call_spends_nothing():
    for _ in range(2):
        _check_rate_limit("biudzetas:nesikaupia", max_attempts=2)
    for _ in range(5):
        assert _check_rate_limit("biudzetas:nesikaupia", max_attempts=2) is True

    # Five rejections later the window still holds exactly the two
    # attempts that were actually allowed
    assert len(_stamps("biudzetas:nesikaupia")) == 2


def test_a_huge_budget_never_rejects():
    for _ in range(50):
        assert _check_rate_limit("biudzetas:didelis", max_attempts=1_000_000) is False

    assert len(_stamps("biudzetas:didelis")) == 50


def test_two_keys_never_share_one_budget():
    for _ in range(_RATE_LIMIT_MAX):
        _check_rate_limit("biudzetas:a")

    assert _check_rate_limit("biudzetas:a") is True
    assert _check_rate_limit("biudzetas:b") is False


def test_the_empty_string_is_a_key_like_any_other():
    assert _check_rate_limit("", max_attempts=1) is False

    assert _check_rate_limit("", max_attempts=1) is True
    assert "" in _rate_limit_store


def test_a_very_long_key_is_stored_verbatim():
    key = "ilgas:" + "x" * 10_000

    assert _check_rate_limit(key) is False

    assert key in _rate_limit_store




# ===========================================================
# _check_rate_limit — the pure probe (record=False)
# ===========================================================

def test_a_probe_never_spends_the_budget():
    for _ in range(_RATE_LIMIT_MAX * 3):
        assert _check_rate_limit("zondas:nesikaupia", record=False) is False

    assert _stamps("zondas:nesikaupia") == []


def test_a_probe_on_an_unknown_key_plants_nothing():
    assert _check_rate_limit("zondas:nezinomas", record=False) is False

    assert "zondas:nezinomas" not in _rate_limit_store


def test_a_probe_reports_a_budget_that_is_already_gone():
    for _ in range(_RATE_LIMIT_MAX):
        _record_attempt("zondas:isnaudotas")

    assert _check_rate_limit("zondas:isnaudotas", record=False) is True


def test_a_probe_leaves_a_live_window_untouched():
    _record_attempt("zondas:gyvas")
    _record_attempt("zondas:gyvas")
    before = _stamps("zondas:gyvas")

    _check_rate_limit("zondas:gyvas", record=False)

    assert _stamps("zondas:gyvas") == before


def test_a_probe_deletes_a_key_whose_window_has_emptied():
    _record_attempt("zondas:pasenes")
    _age()

    assert _check_rate_limit("zondas:pasenes", record=False) is False

    # The pruned-empty key is dropped, not kept as an empty list
    assert "zondas:pasenes" not in _rate_limit_store


def test_a_probe_at_the_budget_edge_still_lets_the_last_attempt_through():
    for _ in range(_RATE_LIMIT_MAX - 1):
        _record_attempt("zondas:riba")

    assert _check_rate_limit("zondas:riba", record=False) is False
    assert _check_rate_limit("zondas:riba", record=True) is False
    assert _check_rate_limit("zondas:riba", record=False) is True




# ===========================================================
# _check_rate_limit — the 5-minute window
# ===========================================================

def test_stamps_older_than_the_window_stop_counting():
    for _ in range(_RATE_LIMIT_MAX):
        _check_rate_limit("langas:pasenes")
    assert _check_rate_limit("langas:pasenes") is True

    _age()

    assert _check_rate_limit("langas:pasenes") is False


def test_a_stamp_exactly_one_window_old_no_longer_counts(monkeypatch):
    # The comparison is a STRICT less-than: at exactly 300 s the
    # stamp is out
    clock = _freeze(monkeypatch, now=0.0)
    _plant("langas:tiksliai", 0.0)
    clock.now = float(_RATE_LIMIT_WINDOW)

    assert _check_rate_limit("langas:tiksliai", max_attempts=1) is False

    assert _stamps("langas:tiksliai") == [float(_RATE_LIMIT_WINDOW)]


def test_a_stamp_a_hair_inside_the_window_still_counts(monkeypatch):
    clock = _freeze(monkeypatch, now=0.0)
    _plant("langas:beveik", 0.0)
    clock.now = _RATE_LIMIT_WINDOW - 0.001

    assert _check_rate_limit("langas:beveik", max_attempts=1) is True


def test_only_the_stale_half_of_a_window_is_pruned(monkeypatch):
    clock = _freeze(monkeypatch, now=0.0)
    _plant("langas:mišrus", 0.0, 100.0, 250.0)
    clock.now = 350.0

    assert _check_rate_limit("langas:mišrus", max_attempts=5) is False

    # 0.0 aged out (350 s ago), the other two survive and the new
    # attempt joins them
    assert _stamps("langas:mišrus") == [100.0, 250.0, 350.0]


def test_a_rejected_call_still_prunes_the_stored_window(monkeypatch):
    clock = _freeze(monkeypatch, now=0.0)
    _plant("langas:atmestas", 0.0, 200.0, 250.0)
    clock.now = 350.0

    assert _check_rate_limit("langas:atmestas", max_attempts=2) is True

    # The reject path writes the pruned list back — a stamp that
    # aged out must not be resurrected by the next check
    assert _stamps("langas:atmestas") == [200.0, 250.0]


def test_a_lifted_lockout_starts_a_brand_new_window():
    for _ in range(_RATE_LIMIT_MAX):
        _check_rate_limit("langas:atsinaujina")
    _age()

    _check_rate_limit("langas:atsinaujina")

    assert len(_stamps("langas:atsinaujina")) == 1


def test_a_window_only_partly_aged_out_keeps_its_remaining_budget():
    for _ in range(_RATE_LIMIT_MAX):
        _check_rate_limit("langas:dalinis")
    _age(_RATE_LIMIT_WINDOW - 60)

    # Every stamp is still 60 s inside the window
    assert _check_rate_limit("langas:dalinis") is True




# ===========================================================
# _check_rate_limit — store hygiene and the LRU ceiling
# ===========================================================

def test_an_allowed_call_moves_its_key_to_the_fresh_end():
    _check_rate_limit("lru:a")
    _check_rate_limit("lru:b")

    _check_rate_limit("lru:a")

    assert list(_rate_limit_store)[-1] == "lru:a"


def test_a_rejected_call_also_moves_its_key_to_the_fresh_end():
    _check_rate_limit("lru:c", max_attempts=1)
    _check_rate_limit("lru:d", max_attempts=1)

    assert _check_rate_limit("lru:c", max_attempts=1) is True

    assert list(_rate_limit_store)[-1] == "lru:c"


def test_the_store_stops_growing_at_the_ceiling(monkeypatch):
    monkeypatch.setattr(auth_routes, "_RATE_LIMIT_MAX_KEYS", 4)

    for index in range(20):
        _check_rate_limit(f"riba:{index}")

    assert len(_rate_limit_store) == 4
    assert list(_rate_limit_store) == [f"riba:{n}" for n in range(16, 20)]


def test_a_store_far_over_the_ceiling_is_trimmed_in_one_call(monkeypatch):
    for index in range(9):
        _plant(f"perteklius:{index}", 0.0)
    monkeypatch.setattr(auth_routes, "_RATE_LIMIT_MAX_KEYS", 3)

    _check_rate_limit("perteklius:naujas")

    # One call evicts as many keys as it takes, not just one
    assert list(_rate_limit_store) == ["perteklius:7", "perteklius:8", "perteklius:naujas"]


def test_a_store_exactly_at_the_ceiling_loses_nothing(monkeypatch):
    monkeypatch.setattr(auth_routes, "_RATE_LIMIT_MAX_KEYS", 3)
    _check_rate_limit("lygiai:a")
    _check_rate_limit("lygiai:b")

    _check_rate_limit("lygiai:c")

    assert list(_rate_limit_store) == ["lygiai:a", "lygiai:b", "lygiai:c"]


def test_the_least_recently_touched_key_is_the_one_evicted(monkeypatch):
    monkeypatch.setattr(auth_routes, "_RATE_LIMIT_MAX_KEYS", 2)
    _check_rate_limit("senas:a")
    _check_rate_limit("senas:b")
    # Touching a again makes b the least recently used
    _check_rate_limit("senas:a")

    _check_rate_limit("senas:c")

    assert "senas:b" not in _rate_limit_store
    assert list(_rate_limit_store) == ["senas:a", "senas:c"]


def test_the_real_ceiling_holds_against_a_spoofed_key_flood():
    for index in range(_RATE_LIMIT_MAX_KEYS + 300):
        _check_rate_limit(f"potvynis:{index}")

    assert len(_rate_limit_store) == _RATE_LIMIT_MAX_KEYS
    assert "potvynis:0" not in _rate_limit_store
    assert f"potvynis:{_RATE_LIMIT_MAX_KEYS + 299}" in _rate_limit_store


def test_a_key_flood_can_evict_another_callers_spent_budget(monkeypatch):
    # The documented cost of the LRU ceiling: an attacker who can
    # mint keys can push a locked-out bucket out of the store and
    # hand it a fresh budget. Pinned so the tradeoff is visible if
    # anyone changes the eviction rule.
    monkeypatch.setattr(auth_routes, "_RATE_LIMIT_MAX_KEYS", 4)
    for _ in range(_RATE_LIMIT_MAX):
        _check_rate_limit("auka:vartotojas")
    assert _check_rate_limit("auka:vartotojas", record=False) is True

    for index in range(10):
        _check_rate_limit(f"uzpuolikas:{index}")

    assert "auka:vartotojas" not in _rate_limit_store
    assert _check_rate_limit("auka:vartotojas", record=False) is False




# ===========================================================
# _check_rate_limit — the lock
# ===========================================================

def test_racing_callers_never_get_more_than_the_budget():
    # The read-modify-write runs under one module lock, so the
    # count of allowed calls is exact however the threads interleave
    allowed = []
    barrier = threading.Barrier(8)

    def hammer():
        barrier.wait()
        for _ in range(20):
            if _check_rate_limit("lenktynes:raktas", max_attempts=_RATE_LIMIT_MAX) is False:
                allowed.append(1)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(allowed) == _RATE_LIMIT_MAX
    assert len(_stamps("lenktynes:raktas")) == _RATE_LIMIT_MAX




# ===========================================================
# _record_attempt
# ===========================================================

def test_recording_creates_a_missing_key():
    assert _record_attempt("irasas:naujas") is None

    assert len(_stamps("irasas:naujas")) == 1


def test_recording_appends_to_a_live_window():
    _record_attempt("irasas:kaupiasi")
    _record_attempt("irasas:kaupiasi")
    _record_attempt("irasas:kaupiasi")

    assert len(_stamps("irasas:kaupiasi")) == 3


def test_recording_prunes_the_window_before_it_appends():
    for _ in range(4):
        _record_attempt("irasas:pasenes")
    _age()

    _record_attempt("irasas:pasenes")

    assert len(_stamps("irasas:pasenes")) == 1


def test_recording_drops_a_stamp_exactly_one_window_old(monkeypatch):
    clock = _freeze(monkeypatch, now=0.0)
    _plant("irasas:tiksliai", 0.0, 150.0)
    clock.now = float(_RATE_LIMIT_WINDOW)

    _record_attempt("irasas:tiksliai")

    assert _stamps("irasas:tiksliai") == [150.0, float(_RATE_LIMIT_WINDOW)]


def test_recording_ignores_the_budget_entirely():
    # _record_attempt is the record half of a record=False probe —
    # the caller has already decided the attempt may be spent, so
    # this side never refuses
    for _ in range(_RATE_LIMIT_MAX * 2):
        _record_attempt("irasas:be_ribos")

    assert len(_stamps("irasas:be_ribos")) == _RATE_LIMIT_MAX * 2


def test_recording_moves_the_key_to_the_fresh_end():
    _record_attempt("irasas:a")
    _record_attempt("irasas:b")
    _record_attempt("irasas:c")

    _record_attempt("irasas:a")

    assert list(_rate_limit_store) == ["irasas:b", "irasas:c", "irasas:a"]


def test_recording_obeys_the_lru_ceiling(monkeypatch):
    monkeypatch.setattr(auth_routes, "_RATE_LIMIT_MAX_KEYS", 3)

    for index in range(12):
        _record_attempt(f"irasas:riba:{index}")

    assert len(_rate_limit_store) == 3
    assert list(_rate_limit_store) == [f"irasas:riba:{n}" for n in range(9, 12)]


def test_recording_trims_a_store_that_starts_far_over_the_ceiling(monkeypatch):
    for index in range(9):
        _plant(f"irasas:virs:{index}", 0.0)
    monkeypatch.setattr(auth_routes, "_RATE_LIMIT_MAX_KEYS", 2)

    _record_attempt("irasas:virs:naujas")

    assert list(_rate_limit_store) == ["irasas:virs:8", "irasas:virs:naujas"]


def test_a_recorded_attempt_is_visible_to_the_very_next_check():
    for _ in range(_RATE_LIMIT_MAX):
        _record_attempt("irasas:matomas")

    assert _check_rate_limit("irasas:matomas") is True


def test_recording_works_under_an_empty_key_name():
    _record_attempt("")

    assert len(_stamps("")) == 1


def test_racing_recorders_lose_no_stamps():
    barrier = threading.Barrier(6)

    def hammer():
        barrier.wait()
        for _ in range(25):
            _record_attempt("lenktynes:irasas")

    threads = [threading.Thread(target=hammer) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(_stamps("lenktynes:irasas")) == 150




# ===========================================================
# _rate_limited_response
# ===========================================================

def test_the_429_carries_the_message_and_the_machine_code(app):
    with app.app_context():
        response, status = _rate_limited_response("Per daug bandymų.", "atsakas:a")

    assert status == 429
    assert response.get_json() == {"error": "Per daug bandymų.", "code": "rate_limited"}


def test_the_429_is_json(app):
    with app.app_context():
        response, _ = _rate_limited_response("Per daug", "atsakas:json")

    assert response.mimetype == "application/json"


def test_an_unknown_key_asks_for_a_one_second_retry(app):
    with app.app_context():
        response, _ = _rate_limited_response("Per daug", "atsakas:nera")

    assert response.headers["Retry-After"] == "1"


def test_building_the_429_plants_no_key_in_the_store(app):
    with app.app_context():
        _rate_limited_response("Per daug", "atsakas:svarus")

    assert "atsakas:svarus" not in _rate_limit_store


def test_building_the_429_never_prunes_the_stored_window(monkeypatch, app):
    clock = _freeze(monkeypatch, now=0.0)
    _plant("atsakas:nekeicia", 0.0, 100.0)
    clock.now = 400.0

    with app.app_context():
        _rate_limited_response("Per daug", "atsakas:nekeicia")

    # The stale stamp is ignored for the calculation but left in
    # place — this helper only reads
    assert _stamps("atsakas:nekeicia") == [0.0, 100.0]


def test_retry_after_counts_down_from_the_oldest_live_stamp(monkeypatch, app):
    clock = _freeze(monkeypatch, now=0.0)
    _plant("atsakas:seniausias", 0.0, 100.0)
    clock.now = 150.0

    with app.app_context():
        response, _ = _rate_limited_response("Per daug", "atsakas:seniausias")

    assert response.headers["Retry-After"] == "151"


def test_retry_after_ignores_stamps_that_already_aged_out(monkeypatch, app):
    clock = _freeze(monkeypatch, now=0.0)
    _plant("atsakas:pasenes", 0.0, 200.0)
    clock.now = 350.0

    with app.app_context():
        response, _ = _rate_limited_response("Per daug", "atsakas:pasenes")

    # 0.0 is 350 s old — out of the window — so the countdown runs
    # from 200.0, which frees up in 150 s
    assert response.headers["Retry-After"] == "151"


def test_a_bucket_filled_this_instant_asks_for_the_whole_window_plus_one(monkeypatch, app):
    clock = _freeze(monkeypatch, now=500.0)
    _plant("atsakas:sviezias", 500.0)

    with app.app_context():
        response, _ = _rate_limited_response("Per daug", "atsakas:sviezias")

    assert response.headers["Retry-After"] == str(_RATE_LIMIT_WINDOW + 1)
    assert clock.now == 500.0


def test_a_bucket_at_the_very_edge_of_the_window_asks_for_one_second(monkeypatch, app):
    clock = _freeze(monkeypatch, now=0.0)
    _plant("atsakas:kraštas", 0.0)
    clock.now = _RATE_LIMIT_WINDOW - 0.4

    with app.app_context():
        response, _ = _rate_limited_response("Per daug", "atsakas:kraštas")

    # int() truncates the remaining 0.4 s to 0, and the +1 keeps the
    # client off a busy loop
    assert response.headers["Retry-After"] == "1"


def test_a_window_of_only_stale_stamps_asks_for_one_second(monkeypatch, app):
    clock = _freeze(monkeypatch, now=0.0)
    _plant("atsakas:visi_seni", 0.0, 10.0)
    clock.now = 5000.0

    with app.app_context():
        response, _ = _rate_limited_response("Per daug", "atsakas:visi_seni")

    assert response.headers["Retry-After"] == "1"


def test_a_backward_clock_step_never_asks_for_a_negative_wait(monkeypatch, app):
    clock = _freeze(monkeypatch, now=0.0)
    _plant("atsakas:atgal", 100.0)
    clock.now = 50.0

    with app.app_context():
        response, _ = _rate_limited_response("Per daug", "atsakas:atgal")

    assert int(response.headers["Retry-After"]) > 0


def test_retry_after_goes_out_as_a_string_header(app):
    _record_attempt("atsakas:tipas")

    with app.app_context():
        response, _ = _rate_limited_response("Per daug", "atsakas:tipas")

    assert isinstance(response.headers["Retry-After"], str)
    assert 1 <= int(response.headers["Retry-After"]) <= _RATE_LIMIT_WINDOW + 1


def test_the_429_message_is_html_escaped_like_every_other_body(app):
    # The house JSON provider escapes on the way out; this shape is
    # no exception
    with app.app_context():
        response, _ = _rate_limited_response("Per daug <b>užklausų</b>", "atsakas:kabutes")

    assert response.get_json()["error"] == "Per daug &lt;b&gt;užklausų&lt;/b&gt;"


def test_the_same_key_answers_the_same_countdown_twice(monkeypatch, app):
    clock = _freeze(monkeypatch, now=0.0)
    _plant("atsakas:stabilus", 0.0)
    clock.now = 60.0

    with app.app_context():
        first, _ = _rate_limited_response("Per daug", "atsakas:stabilus")
        second, _ = _rate_limited_response("Per daug", "atsakas:stabilus")

    assert first.headers["Retry-After"] == second.headers["Retry-After"] == "241"




# ===========================================================
# rate_limit — the decorator every write route wears
# ===========================================================

def test_the_decorator_keeps_the_wrapped_functions_identity():
    def rasyti_zinute():
        return "ok"

    wrapped = rate_limit("zinutes")(rasyti_zinute)

    assert wrapped.__name__ == "rasyti_zinute"


def test_an_anonymous_caller_is_keyed_by_client_ip(app):
    @app.route("/api/_anonimas", methods=["POST"])
    @rate_limit("anonimas", max_attempts=1)
    def _anonimas():
        return jsonify({"ok": True})

    caller = app.test_client()
    assert caller.post("/api/_anonimas", environ_base={"REMOTE_ADDR": "10.1.1.1"}).status_code == 200

    assert "anonimas:10.1.1.1" in _rate_limit_store
    assert caller.post("/api/_anonimas", environ_base={"REMOTE_ADDR": "10.1.1.1"}).status_code == 429


def test_two_client_ips_get_two_budgets(app):
    @app.route("/api/_du_ip", methods=["POST"])
    @rate_limit("du_ip", max_attempts=1)
    def _du_ip():
        return jsonify({"ok": True})

    caller = app.test_client()
    caller.post("/api/_du_ip", environ_base={"REMOTE_ADDR": "10.2.2.2"})

    assert caller.post("/api/_du_ip", environ_base={"REMOTE_ADDR": "10.2.2.2"}).status_code == 429
    assert caller.post("/api/_du_ip", environ_base={"REMOTE_ADDR": "10.2.2.3"}).status_code == 200


def test_a_caller_without_a_remote_address_is_keyed_unknown(app):
    @app.route("/api/_be_adreso", methods=["POST"])
    @rate_limit("be_adreso", max_attempts=1)
    def _be_adreso():
        return jsonify({"ok": True})

    caller = app.test_client()
    assert caller.post("/api/_be_adreso", environ_base={"REMOTE_ADDR": None}).status_code == 200

    assert "be_adreso:unknown" in _rate_limit_store
    assert caller.post("/api/_be_adreso", environ_base={"REMOTE_ADDR": None}).status_code == 429


def test_a_signed_in_caller_is_keyed_by_user_id(app, make_user, auth_headers):
    @app.route("/api/_prisijunges", methods=["POST"])
    @require_auth
    @rate_limit("prisijunges", max_attempts=1)
    def _prisijunges():
        return jsonify({"ok": True})

    user = make_user()
    headers = auth_headers(user)

    assert app.test_client().post("/api/_prisijunges", headers=headers).status_code == 200

    assert f"prisijunges:{user['id']}" in _rate_limit_store


def test_two_signed_in_callers_from_one_ip_keep_separate_budgets(app, make_user, auth_headers):
    @app.route("/api/_du_vartotojai", methods=["POST"])
    @require_auth
    @rate_limit("du_vartotojai", max_attempts=1)
    def _du_vartotojai():
        return jsonify({"ok": True})

    first = auth_headers(make_user())
    second = auth_headers(make_user())
    caller = app.test_client()

    assert caller.post("/api/_du_vartotojai", headers=first).status_code == 200
    assert caller.post("/api/_du_vartotojai", headers=first).status_code == 429
    assert caller.post("/api/_du_vartotojai", headers=second).status_code == 200


def test_a_falsy_user_falls_back_to_the_client_ip(app):
    # `if user else` is a truthiness test, not `is None`: an empty
    # user dict degrades to IP metering instead of a KeyError 500
    from functools import wraps

    def _tuscias_vartotojas(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            request.user = {}
            return f(*args, **kwargs)
        return decorated

    @app.route("/api/_tuscias", methods=["POST"])
    @_tuscias_vartotojas
    @rate_limit("tuscias", max_attempts=1)
    def _tuscias():
        return jsonify({"ok": True})

    caller = app.test_client()
    assert caller.post("/api/_tuscias", environ_base={"REMOTE_ADDR": "10.3.3.3"}).status_code == 200

    assert "tuscias:10.3.3.3" in _rate_limit_store


def test_a_user_row_without_an_id_is_a_programming_error(app):
    # The decorator's contract: require_auth resolved the row, so it
    # carries an id. A hand-made partial dict is a bug at the call
    # site, and it must surface as one rather than silently metering
    # every such caller together
    def zinute():
        return "ok"

    guarded = rate_limit("netinkamas")(zinute)

    with app.test_request_context("/api/_netinkamas", method="POST"):
        request.user = {"role": "admin"}
        with pytest.raises(KeyError):
            guarded()


def test_two_scopes_never_share_one_budget(app):
    @app.route("/api/_sritis_a", methods=["POST"])
    @rate_limit("sritis_a", max_attempts=1)
    def _sritis_a():
        return jsonify({"ok": True})

    @app.route("/api/_sritis_b", methods=["POST"])
    @rate_limit("sritis_b", max_attempts=1)
    def _sritis_b():
        return jsonify({"ok": True})

    caller = app.test_client()
    assert caller.post("/api/_sritis_a").status_code == 200
    assert caller.post("/api/_sritis_a").status_code == 429
    assert caller.post("/api/_sritis_b").status_code == 200


def test_two_routes_in_one_scope_share_the_budget(app):
    @app.route("/api/_bendra_pirma", methods=["POST"])
    @rate_limit("bendra", max_attempts=1)
    def _bendra_pirma():
        return jsonify({"ok": True})

    @app.route("/api/_bendra_antra", methods=["POST"])
    @rate_limit("bendra", max_attempts=1)
    def _bendra_antra():
        return jsonify({"ok": True})

    caller = app.test_client()
    assert caller.post("/api/_bendra_pirma").status_code == 200

    assert caller.post("/api/_bendra_antra").status_code == 429


def test_the_decorators_default_budget_is_ten(app):
    @app.route("/api/_numatytas", methods=["POST"])
    @rate_limit("numatytas")
    def _numatytas():
        return jsonify({"ok": True})

    caller = app.test_client()
    for _ in range(_RATE_LIMIT_MAX):
        assert caller.post("/api/_numatytas").status_code == 200

    assert caller.post("/api/_numatytas").status_code == 429


def test_the_handler_never_runs_once_the_budget_is_gone(app):
    calls = []

    @app.route("/api/_neveikia", methods=["POST"])
    @rate_limit("neveikia", max_attempts=1)
    def _neveikia():
        calls.append(1)
        return jsonify({"ok": True})

    caller = app.test_client()
    caller.post("/api/_neveikia")
    caller.post("/api/_neveikia")
    caller.post("/api/_neveikia")

    assert calls == [1]


def test_a_zero_budget_blocks_the_very_first_call(app):
    calls = []

    @app.route("/api/_nulinis", methods=["POST"])
    @rate_limit("nulinis", max_attempts=0)
    def _nulinis():
        calls.append(1)
        return jsonify({"ok": True})

    response = app.test_client().post("/api/_nulinis")

    assert response.status_code == 429
    assert calls == []


def test_url_parameters_reach_the_handler_untouched(app):
    @app.route("/api/_su_parametru/<int:numeris>", methods=["POST"])
    @rate_limit("su_parametru", max_attempts=5)
    def _su_parametru(numeris):
        return jsonify({"numeris": numeris})

    response = app.test_client().post("/api/_su_parametru/42")

    assert response.status_code == 200
    assert response.get_json() == {"numeris": 42}


def test_the_handlers_own_status_and_body_pass_through(app):
    @app.route("/api/_savas_atsakas", methods=["POST"])
    @rate_limit("savas_atsakas", max_attempts=5)
    def _savas_atsakas():
        return jsonify({"sukurta": True}), 201

    response = app.test_client().post("/api/_savas_atsakas")

    assert response.status_code == 201
    assert response.get_json() == {"sukurta": True}


def test_the_refusal_carries_the_house_shape_and_a_retry_after(app):
    @app.route("/api/_forma", methods=["POST"])
    @rate_limit("forma", max_attempts=1)
    def _forma():
        return jsonify({"ok": True})

    caller = app.test_client()
    caller.post("/api/_forma")
    blocked = caller.post("/api/_forma")

    assert blocked.status_code == 429
    assert blocked.get_json()["code"] == "rate_limited"
    assert blocked.get_json()["error"] == "Too many requests. Please wait a few minutes."
    assert 1 <= int(blocked.headers["Retry-After"]) <= _RATE_LIMIT_WINDOW + 1


def test_the_budget_hit_is_logged_with_the_key(app, monkeypatch):
    warnings = []
    monkeypatch.setattr(auth_routes.logger, "warning",
                        lambda message, *args: warnings.append(message % args))

    @app.route("/api/_zurnalas", methods=["POST"])
    @rate_limit("zurnalas", max_attempts=1)
    def _zurnalas():
        return jsonify({"ok": True})

    caller = app.test_client()
    caller.post("/api/_zurnalas", environ_base={"REMOTE_ADDR": "10.4.4.4"})
    caller.post("/api/_zurnalas", environ_base={"REMOTE_ADDR": "10.4.4.4"})

    assert "Rate limit hit: zurnalas:10.4.4.4" in warnings


def test_the_route_reopens_once_the_window_passes(app):
    @app.route("/api/_atsidaro", methods=["POST"])
    @rate_limit("atsidaro", max_attempts=1)
    def _atsidaro():
        return jsonify({"ok": True})

    caller = app.test_client()
    caller.post("/api/_atsidaro")
    assert caller.post("/api/_atsidaro").status_code == 429

    _age()

    assert caller.post("/api/_atsidaro").status_code == 200


def test_stacking_the_decorator_above_require_auth_meters_by_ip_instead(app, make_user, auth_headers):
    # The banner says stack it UNDER require_auth. Stacked ABOVE it,
    # request.user is not resolved yet, so two different accounts
    # behind one address share a single bucket — pinned so the
    # ordering rule has teeth
    @app.route("/api/_bloga_tvarka", methods=["POST"])
    @rate_limit("bloga_tvarka", max_attempts=1)
    @require_auth
    def _bloga_tvarka():
        return jsonify({"ok": True})

    first = auth_headers(make_user())
    second = auth_headers(make_user())
    caller = app.test_client()

    assert caller.post("/api/_bloga_tvarka", headers=first,
                       environ_base={"REMOTE_ADDR": "10.5.5.5"}).status_code == 200
    assert caller.post("/api/_bloga_tvarka", headers=second,
                       environ_base={"REMOTE_ADDR": "10.5.5.5"}).status_code == 429
    assert "bloga_tvarka:10.5.5.5" in _rate_limit_store


def test_the_budget_is_process_wide_not_per_client_object(app):
    @app.route("/api/_procesas", methods=["POST"])
    @rate_limit("procesas", max_attempts=1)
    def _procesas():
        return jsonify({"ok": True})

    assert app.test_client().post("/api/_procesas",
                                  environ_base={"REMOTE_ADDR": "10.6.6.6"}).status_code == 200

    # A brand-new client, same address: the store lives in the
    # process, not in the connection
    assert app.test_client().post("/api/_procesas",
                                  environ_base={"REMOTE_ADDR": "10.6.6.6"}).status_code == 429


def test_a_call_the_handler_itself_rejects_still_costs_budget(app):
    # The meter runs BEFORE the handler, so a request the handler
    # then refuses has already spent its attempt — the property the
    # auth routes deliberately avoid by probing with record=False
    @app.route("/api/_blogas_kunas", methods=["POST"])
    @rate_limit("blogas_kunas", max_attempts=1)
    def _blogas_kunas():
        return jsonify({"error": "Blogas kūnas"}), 400

    caller = app.test_client()
    assert caller.post("/api/_blogas_kunas").status_code == 400

    assert caller.post("/api/_blogas_kunas").status_code == 429


def test_a_metered_route_answers_get_as_well(app):
    @app.route("/api/_skaitymas", methods=["GET"])
    @rate_limit("skaitymas", max_attempts=1)
    def _skaitymas():
        return jsonify({"ok": True})

    caller = app.test_client()
    assert caller.get("/api/_skaitymas").status_code == 200

    assert caller.get("/api/_skaitymas").status_code == 429




# ===========================================================
# get_json_object — a dict, or nothing
# ===========================================================

def test_a_json_object_comes_back_as_a_dict(app):
    assert _parse(app, b'{"kodas": "ABC", "skaicius": 7}') == {"kodas": "ABC", "skaicius": 7}


def test_an_empty_object_comes_back_as_an_empty_dict_not_none(app):
    parsed = _parse(app, b"{}")

    assert parsed == {}
    assert parsed is not None
    # Callers branch on `if not data` — an empty object is a missing
    # body as far as they are concerned
    assert not parsed


def test_a_top_level_array_comes_back_as_none(app):
    assert _parse(app, b"[1, 2, 3]") is None


def test_an_empty_array_comes_back_as_none(app):
    assert _parse(app, b"[]") is None


def test_a_top_level_string_comes_back_as_none(app):
    assert _parse(app, b'"labas"') is None


def test_a_top_level_number_comes_back_as_none(app):
    assert _parse(app, b"42") is None
    assert _parse(app, b"-3.5") is None
    assert _parse(app, b"0") is None


def test_a_json_null_body_comes_back_as_none(app):
    assert _parse(app, b"null") is None


def test_a_json_boolean_body_comes_back_as_none(app):
    assert _parse(app, b"true") is None
    assert _parse(app, b"false") is None


def test_malformed_json_comes_back_as_none(app):
    assert _parse(app, b'{"kodas": ') is None
    assert _parse(app, b"{kodas: 1}") is None
    assert _parse(app, b"{'kodas': 1}") is None


def test_a_body_of_plain_words_comes_back_as_none(app):
    assert _parse(app, b"visai ne json") is None


def test_an_absent_body_comes_back_as_none(app):
    assert _parse(app) is None


def test_a_whitespace_only_body_comes_back_as_none(app):
    assert _parse(app, b"   \n\t ") is None


def test_a_body_without_a_json_content_type_comes_back_as_none(app):
    # Valid JSON object, wrong media type: get_json(silent=True)
    # refuses to parse it, and the route answers its own 400
    assert _parse(app, b'{"kodas": "ABC"}', content_type="text/plain") is None


def test_a_body_with_no_content_type_at_all_comes_back_as_none(app):
    assert _parse(app, b'{"kodas": "ABC"}', content_type=None) is None


def test_a_form_post_comes_back_as_none(app):
    assert _parse(app, b"kodas=ABC",
                  content_type="application/x-www-form-urlencoded") is None


def test_a_vendor_json_content_type_is_still_parsed(app):
    assert _parse(app, b'{"kodas": "ABC"}',
                  content_type="application/vnd.api+json") == {"kodas": "ABC"}


def test_a_charset_on_the_json_content_type_is_tolerated(app):
    assert _parse(app, b'{"kodas": "ABC"}',
                  content_type="application/json; charset=utf-8") == {"kodas": "ABC"}


def test_invalid_utf8_bytes_come_back_as_none(app):
    assert _parse(app, b'{"kodas": "\xff\xfe"}') is None


def test_nested_structures_survive_intact(app):
    body = {"a": {"b": {"c": [1, {"d": "gilyn"}]}}}

    assert _parse(app, json.dumps(body).encode()) == body


def test_null_values_inside_an_object_survive(app):
    assert _parse(app, b'{"avatarUrl": null}') == {"avatarUrl": None}


def test_duplicate_keys_keep_the_last_value(app):
    assert _parse(app, b'{"kodas": "pirmas", "kodas": "antras"}') == {"kodas": "antras"}


def test_unicode_keys_and_values_survive(app):
    assert _parse(app, '{"vardas": "Ąžuolas", "grupė": "PS-1"}'.encode()) == {
        "vardas": "Ąžuolas", "grupė": "PS-1"}


def test_markup_in_a_value_arrives_exactly_as_it_was_posted(app):
    # Raw bytes on the wire, so nothing is escaped on the way IN —
    # the app escapes on the way OUT (TESTPLAN rule 10)
    assert _parse(app, b'{"turinys": "as <3 tave & tave"}') == {
        "turinys": "as <3 tave & tave"}


def test_a_five_thousand_key_body_is_parsed(app):
    body = {f"laukas{n}": n for n in range(5000)}

    assert _parse(app, json.dumps(body).encode()) == body


def test_a_deeply_nested_body_is_parsed(app):
    body = {"gylis": 0}
    for depth in range(1, 40):
        body = {"gylis": depth, "vidus": body}

    assert _parse(app, json.dumps(body).encode()) == body


def test_a_get_request_body_is_parsed_the_same_way(app):
    assert _parse(app, b'{"kodas": "ABC"}', method="GET") == {"kodas": "ABC"}


def test_two_calls_hand_back_the_very_same_object(app):
    # Werkzeug caches the parsed body, so a route that parses twice
    # pays once — and a mutation by one caller is seen by the next
    with app.test_request_context("/api/_kunas", method="POST",
                                  data=b'{"kodas": "ABC"}',
                                  content_type="application/json"):
        first = get_json_object()
        first["kodas"] = "pakeista"
        second = get_json_object()

    assert first is second
    assert second["kodas"] == "pakeista"


def test_the_silent_slot_of_the_werkzeug_cache_is_the_one_read(app):
    # validate_json_input installs its NUL-stripped copy by writing
    # BOTH cache slots; this pins which one get_json_object reads, so
    # a Werkzeug change cannot silently bypass that cleaning
    with app.test_request_context("/api/_kunas", method="POST",
                                  data=b'{"kodas": "originalus"}',
                                  content_type="application/json"):
        request._cached_json = ({"slot": "loud"}, {"slot": "silent"})

        assert get_json_object() == {"slot": "silent"}


def test_an_oversized_body_is_not_swallowed_into_none(app):
    # The one shape this helper does NOT turn into None: a body over
    # MAX_CONTENT_LENGTH raises out of get_data before json.loads is
    # reached. It must stay an exception — the app's 413 handler
    # owns that answer, and a silent None would let the route treat
    # a 6 MB flood as a missing body
    oversized = b'{"a": "' + b"x" * (7 * 1024 * 1024) + b'"}'

    with pytest.raises(RequestEntityTooLarge):
        _parse(app, oversized)


def test_a_body_sent_as_text_plain_is_no_body_at_all_to_a_route(client):
    # End to end: the object-body before_request hook only looks at
    # JSON content types, so this lands on get_json_object, which
    # answers None and lets the route give its own 400
    response = client.post("/api/auth/register", data=b'{"username": "jonas"}',
                           content_type="text/plain")

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body required"
