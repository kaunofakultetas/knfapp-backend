############################################################
#  [*] Request/response helpers — JSON in, JSON out
#
#  The primitives every api view leans on:
#
#    get_json_object(request) — the body as a dict, or None
#      for anything else (bad JSON, an array, a bare
#      number). Handlers treat None as "body missing" and
#      answer their usual 400 instead of crashing.
#    json_response(payload, status) — JsonResponse without
#      key sorting, so serializer dicts keep their shape.
#    json_error(message, status, code=None) — the app-wide
#      error body {"error": prose, "code"?: slug}. Clients
#      translate off the machine `code` and never show the
#      English prose.
#    client_ip(request) — the rate-limit identity: the LAST
#      X-Forwarded-For hop (Caddy appends the real peer
#      itself, so the last hop is trustworthy), falling
#      back to REMOTE_ADDR for direct calls (tests).
############################################################


import hashlib
import json


from django.http import JsonResponse


def get_json_object(request):
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def json_response(payload, status=200):
    return JsonResponse(payload, status=status, json_dumps_params={"ensure_ascii": False})


def json_error(message, status, code=None):
    body = {"error": message}
    if code is not None:
        body["code"] = code
    return json_response(body, status=status)


def client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        last_hop = forwarded.rsplit(",", 1)[-1].strip()
        if last_hop:
            return last_hop
    return request.META.get("REMOTE_ADDR") or "unknown"








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
