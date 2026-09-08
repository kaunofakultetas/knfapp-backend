############################################################
#  [*] Rate limiting — in-memory write quotas
#
#  One process-wide LRU of "scope:identity" keys, each
#  holding the attempt stamps of the last window. In-process
#  on purpose — the budget is a brake on abuse, not
#  bookkeeping, so a restart forgiving every window is
#  fine.
#
#    check(key, max_attempts, record=True) — True when the
#      key is over budget. record=False probes without
#      spending (register/login record failures only —
#      honest traffic must not eat its own budget).
#    record(key) — spend one attempt now.
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


def check(key, max_attempts=MAX_ATTEMPTS, record=True):
    now = time.time()
    with _lock:
        attempts = [t for t in _store.get(key, []) if now - t < WINDOW]
        if len(attempts) >= max_attempts:
            _store[key] = attempts
            _store.move_to_end(key)
            return True
        if record:
            attempts.append(now)
            _store[key] = attempts
            _store.move_to_end(key)
            # The LRU ceiling: the oldest key pays for the new one
            while len(_store) > MAX_KEYS:
                _store.popitem(last=False)
        return False


def record(key):
    check(key, max_attempts=float("inf"), record=True)


def limited_response(message, key):
    now = time.time()
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
# attempt. Stack it UNDER @require_auth so request.user is
# the key.
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
