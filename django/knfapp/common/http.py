############################################################
#  [*] Request/response helpers — JSON in, JSON out
#
#  The primitives every api view leans on:
#
#    get_json_object(request) — the body as a dict, or None
#      for anything else (bad JSON, an array, a bare
#      number). Handlers treat None as "body missing" and
#      answer their usual 400 instead of crashing. Every
#      string in the structure (values AND keys, however
#      deep) is stripped of NUL and the other C0 control
#      characters except \t \n \r before the view sees it:
#      PostgreSQL's text type refuses NUL (a DataError, so
#      a 500 from a body no human typed) and jsonb refuses
#      \u0000; SQLite stores them and answers wrong later.
#    clean_param(value) — the same stripping for ONE
#      request.GET value (None passes through). Query
#      strings never go through get_json_object, so every
#      TEXT ?param reader wraps its read AT THE READ SITE,
#      around the request.GET.get call itself —
#      clean_param(request.GET.get("q")) — never once at
#      the top of a view: the reader's own line then shows
#      the guarantee, and `grep 'clean_param(request.GET'`
#      lists every wrapped reader. Adopted by chat ?q /
#      ?before / ?after / ?before_id / ?after_id / ?around /
#      ?since, news ?q / ?source / ?before, schedule ?group
#      / ?teacher / ?semester / ?day / ?from / ?to, memes
#      ?q, social ?cursor / ?before / ?direction / ?user_id,
#      admin ?status, info ?lang / ?section, scraper
#      ?source / ?status, the assistant's ?user_id and
#      ?rating, wayfind ?buildingId. Readers that go
#      through int() need nothing — a control byte is a
#      ValueError there, never a query.
#    json_response(payload, status, naive_stamps=False) —
#      JsonResponse without key sorting, so serializer
#      dicts keep their shape, and with the app's own
#      datetime encoding: plain isoformat(), so a stamp
#      serialises with its "+00:00" (never rewritten to
#      "Z" the way Django's stock encoder does). The
#      surfaces whose frozen wire is NAIVE UTC (chat,
#      scraper status, info) pass naive_stamps=True and
#      the encoder drops the offset — code carries ONE
#      stamp kind (aware UTC), the RESPONSE picks the
#      shape. (The GDPR export is the lone mixed response;
#      it shapes its chat sections per field.)
#    json_error(message, status, code=None) — the app-wide
#      error body {"error": prose, "code"?: slug}. Clients
#      translate off the machine `code` and never show the
#      English prose.
#    client_ip(request) — the rate-limit identity: the
#      X-Forwarded-For entry TRUSTED_PROXY_HOPS places from
#      the right (see its banner below), falling back to
#      REMOTE_ADDR for direct calls (tests).
#    require_methods(*verbs) / method_not_allowed(allowed)
#      — the HTTP-verb gate and its 405 envelope (see their
#      banner below).
#    handler400 / handler403 / handler404 / handler500 —
#      the envelope for what no view answered (see their
#      banner below).
############################################################


import hashlib
import json
import re
from datetime import date, datetime, timezone
from functools import wraps


from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.http import JsonResponse


# isoformat() verbatim — the aware wire shape ends "+00:00",
# the naive one carries no offset; Django's stock encoder
# would rewrite the former to "Z" and change response bytes
class _StampEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        return super().default(o)


# The naive-wire sibling: an aware stamp is moved onto UTC
# and stripped of its offset before serialising, so the
# chat/scraper/info surfaces keep their frozen no-offset
# shape while the code above them carries aware datetimes
class _NaiveStampEncoder(_StampEncoder):
    def default(self, o):
        if isinstance(o, datetime) and o.tzinfo is not None:
            o = o.astimezone(timezone.utc).replace(tzinfo=None)
        return super().default(o)


# The C0 range minus the three a text may legitimately carry
# (\t \n \r) — NUL above all; no typed field ever means the
# rest. DEL (\x7f) and the C1 range are left alone: both
# engines store them and nothing renders them as controls
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _strip_controls(value):
    if isinstance(value, str):
        return _CONTROL_CHARS.sub("", value)
    if isinstance(value, list):
        return [_strip_controls(item) for item in value]
    if isinstance(value, dict):
        return {_strip_controls(key): _strip_controls(item) for key, item in value.items()}
    return value


def get_json_object(request):
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        return None
    return _strip_controls(data) if isinstance(data, dict) else None


def clean_param(value):
    return _CONTROL_CHARS.sub("", value) if isinstance(value, str) else value


def json_response(payload, status=200, naive_stamps=False):
    return JsonResponse(payload, status=status,
                        encoder=_NaiveStampEncoder if naive_stamps else _StampEncoder,
                        json_dumps_params={"ensure_ascii": False})


def json_error(message, status, code=None):
    body = {"error": message}
    if code is not None:
        body["code"] = code
    return json_response(body, status=status)








############################################################
# client_ip
############################################################
#
# The identity every IP-keyed rate limit buckets on. The
# request reaches Django through TWO proxies: the host's
# TLS terminator stamps X-Forwarded-For with the client
# and hands the request to this stack's ingress
# (endpoint/Caddyfile), which — with trusted_proxies —
# keeps that chain and appends its own peer, the outer
# proxy. So the header reads "<client>, <outer proxy>":
# counting from the RIGHT, hop 1 is the outer proxy and
# the client is one further left. settings.
# TRUSTED_PROXY_HOPS says how many rightmost entries are
# infrastructure (1 for this deployment; a third proxy
# would make it 2), read at call time so a test can
# override it. Three cases:
#
#   - more entries than trusted hops: the entry just left
#     of the trusted tail is the client;
#   - a chain no longer than the trusted tail: the request
#     did not come through the outer proxy (a direct hit on
#     the published port 80 — or the ingress before its
#     trusted_proxies reload), so the FIRST entry, the
#     address the ingress itself stamped, is the peer;
#   - no header at all: REMOTE_ADDR (tests, a bare
#     runserver), "unknown" when even that is empty.
#
# This is only as honest as the ingress: counting hops
# trusts the header, and it is the ingress's
# trusted_proxies that stops a public peer from forging
# entries to its left. Without that half every entry is
# the client's to invent.
#
# Used by:
#   - common/ratelimit.py per_user (unauthenticated calls)
#   - users/api/auth_views.py — validate_code, register,
#     login
############################################################

def client_ip(request):
    hops = [hop.strip() for hop in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")]
    hops = [hop for hop in hops if hop]
    trusted = settings.TRUSTED_PROXY_HOPS
    if len(hops) > trusted:
        return hops[-(trusted + 1)]
    if hops:
        return hops[0]
    return request.META.get("REMOTE_ADDR") or "unknown"








############################################################
# require_methods / method_not_allowed
############################################################
#
# The HTTP-verb gate a view wears OUTERMOST — above
# @require_auth / @require_role and the rate limiter, so a
# wrong verb never spends budget, never resolves a session
# and never reaches a write. A verb off the list answers the
# app's own envelope, {"error": "Method not allowed",
# "code": "method_not_allowed"} with status 405 and an Allow
# header naming the verbs, instead of Django's empty
# HttpResponseNotAllowed. HEAD is admitted wherever GET is
# (proxies and prefetchers probe with it) and listed beside
# it in Allow. method_not_allowed(allowed) is the response
# alone — the one-path-many-verbs dispatchers in
# knfapp/urls.py answer their unmatched branch with it.
#
# Used by:
#   - every view routed in knfapp/urls.py: users, uploads,
#     news, social, notifications, schedule, info, memes,
#     admin, scraper, chat, wayfind, ops, the assistant's
#     admin and internal doors
#   - knfapp/urls.py — the dispatchers' unmatched branch
############################################################

def method_not_allowed(allowed):
    verbs = list(allowed)
    if "GET" in verbs and "HEAD" not in verbs:
        verbs.insert(verbs.index("GET") + 1, "HEAD")
    response = json_error("Method not allowed", 405, code="method_not_allowed")
    response["Allow"] = ", ".join(verbs)
    return response


def require_methods(*verbs):
    admitted = set(verbs)
    if "GET" in admitted:
        admitted.add("HEAD")

    def decorator(view):
        @wraps(view)
        def decorated(request, *args, **kwargs):
            if request.method not in admitted:
                return method_not_allowed(verbs)
            return view(request, *args, **kwargs)
        return decorated
    return decorator








############################################################
# parse_pagination
############################################################
#
# ?page / ?per_page as (page, per_page, error) — error is a
# ready 400 response and the other two None when a value is
# garbage. page is a positive int capped at max_page (OFFSET
# stays sane); per_page is a positive int REJECTED above
# max_per_page. Defaults: page 1, per_page 20.
#
# Used by:
#   - news/api/views.py — get_feed, get_comments
############################################################

def parse_pagination(request, max_per_page=50, default_per_page=20, max_page=10_000):
    raw_page = request.GET.get("page")
    raw_per_page = request.GET.get("per_page")

    if raw_page is not None:
        try:
            page = int(raw_page)
        except (ValueError, TypeError):
            return None, None, json_error("page must be a positive integer", 400)
        if page < 1:
            return None, None, json_error("page must be a positive integer", 400)
        if page > max_page:
            return None, None, json_error(f"page must be at most {max_page}", 400)
    else:
        page = 1

    if raw_per_page is not None:
        try:
            per_page = int(raw_per_page)
        except (ValueError, TypeError):
            return None, None, json_error("per_page must be a positive integer", 400)
        if per_page < 1:
            return None, None, json_error("per_page must be a positive integer", 400)
        if per_page > max_per_page:
            return None, None, json_error(f"per_page must be at most {max_per_page}", 400)
    else:
        per_page = default_per_page

    return page, per_page, None








############################################################
# etag_for / if_none_match_contains
############################################################
#
# The weak-ETag pair the cacheable GET routes share: a
# 32-hex tag over a DATA-derived seed, and the
# If-None-Match check that accepts the weak form, the
# strong form and the '*' wildcard.
#
# Used by:
#   - news/api/views.py — the feed (via news/core.py)
#   - schedule/api/views.py, info/api/views.py — their
#     public cache paths
############################################################

def etag_for(seed):
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def if_none_match_contains(header_value, tag):
    if not header_value:
        return False
    if header_value.strip() == "*":
        return True
    candidates = {part.strip() for part in header_value.split(",")}
    return f'W/"{tag}"' in candidates or f'"{tag}"' in candidates








############################################################
# handler400 / handler403 / handler404 / handler500
############################################################
#
# The four error views knfapp/urls.py names as Django's
# handler400 / handler403 / handler404 / handler500, so a
# request no view answered still speaks the documented
# envelope: an unknown /api/... path, a PermissionDenied
# raised below a view, a request Django itself refuses (a
# SuspiciousOperation — a Host outside ALLOWED_HOSTS, too
# many form fields), and an exception no view caught (a
# DataError from a control byte that reached a column,
# anything else). Without them Django answers its HTML
# pages, which the app's clients cannot parse. The 500
# carries no detail on the wire — the traceback is already
# in the log.
#
# One SuspiciousOperation is not a 400: RequestDataTooBig,
# which request.body raises when Content-Length exceeds
# DATA_UPLOAD_MAX_MEMORY_SIZE. Django routes every
# SuspiciousOperation to handler400 and returns whatever
# that handler answers, status included
# (django/core/handlers/exception.py — get_exception_
# response hands the callback's response back untouched),
# so handler400 answers 413 {"error": "Request body too
# large", "code": "too_large"} for that one exception and
# the plain 400 for the rest. This is also why
# get_json_object's except clause need not widen to catch
# RequestDataTooBig: the exception leaves the view, the
# handler answers, and the dict-or-None contract every
# view relies on stays exactly that. Bodies over the
# INGRESS cap (endpoint/Caddyfile request_body) never get
# this far — Caddy answers its own empty 413.
#
# DEBUG=True BYPASSES ALL FOUR: Django shows its technical
# pages instead, by design. The compose dev container runs
# with DJANGO_DEBUG=True, so through Caddy these handlers
# are seen only in production; the test runner forces DEBUG
# off, which is where the suite pins them.
#
# Used by:
#   - knfapp/urls.py — handler400 / handler403 / handler404
#     / handler500
############################################################

def handler400(request, exception=None):
    if isinstance(exception, RequestDataTooBig):
        return json_error("Request body too large", 413, code="too_large")
    return json_error("Bad request", 400, code="bad_request")


def handler403(request, exception=None):
    return json_error("Forbidden", 403, code="forbidden")


def handler404(request, exception=None):
    return json_error("Not found", 404, code="not_found")


def handler500(request):
    return json_error("Internal server error", 500, code="server_error")
