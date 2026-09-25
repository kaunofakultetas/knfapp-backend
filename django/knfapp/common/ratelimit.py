############################################################
#  [*] Rate limiting — in-memory write quotas
#
#  One process-wide LRU of "scope:identity" keys, each
#  holding the attempt stamps of the last window. In-process
#  on purpose — the budget is a brake on abuse, not
#  bookkeeping, so a restart forgiving every window is
#  fine. Stamps come from time.monotonic(): they are only
#  ever compared to each other and to now, and a wall-clock
#  step (NTP, a DST-confused host) must neither empty nor
#  freeze a window.
#
#  Every decision is made AND written under one lock hold —
#  there is no probe that a burst of threads can all pass
#  before anyone records:
#
#    reserve(key, max_attempts) — spend one attempt now:
#      True with the stamp appended, False (nothing
#      appended) when the window is already at budget. The
#      gate of the failure-counted flows — register, login,
#      change-password/delete-me take the slot BEFORE the
#      bcrypt work, and
#    refund(key) — hands the newest stamp back once the
#      password proved right, which is how "only failures
#      spend budget" holds with no gap.
#    check(key, max_attempts, record=True) — True when the
#      key is over budget; record=True spends in the same
#      hold (the per_user decorator), record=False only
#      looks — never gate on a look and record later.
#    record(key) — spend one attempt unconditionally.
#    limited_response(message, key) — the 429 body
#      {"error", "code": "rate_limited"} with Retry-After
#      set to the seconds until the oldest attempt ages out.
#    reset() — tests only: an empty store between cases.
############################################################


import threading
import time
from collections import OrderedDict
from functools import wraps


from knfapp.common.http import json_response


WINDOW = 300        # seconds (5 minutes)
MAX_ATTEMPTS = 10   # default budget per window
MAX_KEYS = 4096     # LRU ceiling on distinct keys

_store: OrderedDict[str, list[float]] = OrderedDict()
_lock = threading.Lock()








############################################################
# _live_attempts / _put
############################################################
#
# The two halves every mutator shares, both to be called
# with _lock held: the key's stamps still inside the window
# (pruned, never the raw list), and the write-back that
# bumps the key to most-recently-used and holds the LRU
# ceiling — the oldest key pays for a new one.
#
# Used by:
#   - check, reserve (below)
############################################################

def _live_attempts(key, now):
    return [t for t in _store.get(key, []) if now - t < WINDOW]


def _put(key, attempts):
    _store[key] = attempts
    _store.move_to_end(key)
    while len(_store) > MAX_KEYS:
        _store.popitem(last=False)








############################################################
# check / record
############################################################
#
# check answers "over budget?" for the key, spending one
# attempt in the same lock hold when record=True — that is
# the per_user decorator's whole gate. record=False is a
# bare look for callers that spend elsewhere; the auth
# flows no longer do (they reserve), because a look that
# passes for 24 threads at once reserves nothing. record
# spends unconditionally (an infinite budget never says
# no).
#
# Used by:
#   - per_user (below), users/api/auth_views.py
#     validate_code (check, record=True)
############################################################

def check(key, max_attempts=MAX_ATTEMPTS, record=True):
    now = time.monotonic()
    with _lock:
        attempts = _live_attempts(key, now)
        if len(attempts) >= max_attempts:
            _put(key, attempts)
            return True
        if record:
            attempts.append(now)
            _put(key, attempts)
        return False


def record(key):
    check(key, max_attempts=float("inf"), record=True)








############################################################
# reserve / refund
############################################################
#
# The failure-counted gate. reserve prunes the window,
# compares and appends in ONE lock hold: of 24 threads
# hitting an empty 10-budget key together exactly ten get
# True, and a False appends nothing (a refused caller must
# not shorten the window for the others). The slot is taken
# before the expensive, oracle-shaped work — bcrypt against
# a real hash — so the budget bounds the guesses actually
# made, not the guesses that noticed the budget. refund
# pops the newest stamp (stamps are fungible — the count is
# the budget, and the newest is the one just taken) when
# the attempt turned out honest; an empty or unknown key is
# a no-op, so a refund can never go negative.
#
# Used by:
#   - users/api/auth_views.py — register (reserve only:
#     every validated attempt spends, the budget caps
#     account creation), login (both buckets),
#     change_password and delete_me (the shared chpass
#     bucket)
############################################################

def reserve(key, max_attempts=MAX_ATTEMPTS):
    now = time.monotonic()
    with _lock:
        attempts = _live_attempts(key, now)
        if len(attempts) >= max_attempts:
            _put(key, attempts)
            return False
        attempts.append(now)
        _put(key, attempts)
        return True


def refund(key):
    with _lock:
        attempts = _store.get(key)
        if attempts:
            attempts.pop()








############################################################
# limited_response / reset
############################################################
#
# The 429 every limited route answers: {"error", "code":
# "rate_limited"} plus Retry-After counted from the oldest
# live stamp on the same monotonic clock (never zero — a
# client that retries at once would only see the same
# door). reset empties the store — tests only.
#
# Used by:
#   - per_user (below), users/api/auth_views.py; reset by
#     every test setUp that drives a limited route
############################################################

def limited_response(message, key):
    now = time.monotonic()
    with _lock:
        attempts = _store.get(key, [])
        oldest = min(attempts) if attempts else now
    retry_after = max(1, int(WINDOW - (now - oldest)) + 1)
    response = json_response({"error": message, "code": "rate_limited"}, status=429)
    response["Retry-After"] = str(retry_after)
    return response


def reset():
    with _lock:
        _store.clear()








############################################################
# per_user
############################################################
#
# The write-route decorator: "scope:<user id>" (or the
# client IP before authentication), every call spending one
# attempt in the same lock hold that judges it. Stack it
# UNDER @require_auth so request.user is the key.
#
# Used by:
#   - the write routes across the apps (uploads, news,
#     social, chat, wayfind, admin — each picks its quotas)
############################################################

def per_user(scope, max_attempts=MAX_ATTEMPTS):
    def decorator(view):
        @wraps(view)
        def decorated(request, *args, **kwargs):
            user = getattr(request, "user", None)
            if user:
                actor = user["id"]
            else:
                from knfapp.common.http import client_ip
                actor = client_ip(request)
            key = f"{scope}:{actor}"
            if check(key, max_attempts):
                return limited_response("Too many requests. Please wait a few minutes.", key)
            return view(request, *args, **kwargs)
        return decorated
    return decorator
