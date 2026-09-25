############################################################
#  [*] assistant internal API — the container's door
#
#  Every route here is ISOLATED-NETWORK ONLY: Caddy never
#  proxies /internal/*, and on top of that every request
#  must carry the compose-injected shared secret in
#  X-Internal-Secret — a request without it is refused
#  before any body parsing. The assistant container is the
#  only caller; the mobile app never reaches these.
#
#  The container passes the IDENTITY it resolved (user_id
#  or null for a guest) with each call; the thread access
#  rule lives here, once: a thread with a user is served
#  only to that user, a thread without one is served to
#  whoever presents its uuid — possession of the
#  unguessable id is the guest's credential, as in jauka.
#
#  Split into:
#
#    require_internal   — the shared-secret gate
#    _thread_for        — uuid + identity → thread or None
#    _thread_payload    — one wire shape for thread rows
#    assistant_search   — the searchHandbook retrieval
#    active_prompt      — the versioned prompt appendix
#    threads_create     — new thread row
#    threads_list       — the signed-in thread list
#    threads_lookup     — guest bulk fetch by ids
#    threads_claim      — guest threads → an account
#    thread_messages    — GET transcript / POST turn upsert
#    message_feedback   — thumbs on one assistant message
#    thread_delete      — soft delete
#    turn_log           — one telemetry row
############################################################


import hmac
import logging
import uuid
from datetime import timedelta
from functools import wraps

from django.conf import settings
from django.db import IntegrityError
from django.utils import timezone

from knfapp.assistant.gateway import GatewayError
from knfapp.assistant.models import (
    AssistantMessage, AssistantPrompt, AssistantThread, AssistantTurn, TURN_OUTCOMES,
)
from knfapp.assistant.search import search_chunks
from knfapp.common.http import clean_param, get_json_object, json_error, json_response, require_methods
from knfapp.users.models import User


logger = logging.getLogger(__name__)


# The most thread ids one guest lookup/claim may present —
# a device registry is dozens at most; hundreds is abuse
MAX_LOOKUP_IDS = 100

# The most messages one turn may upsert at once — a turn
# writes the user message and the assistant answer, plus a
# little slack for a client replaying a short backlog
MAX_MESSAGES_PER_POST = 20

# Auto-title length, taken from the first user message
TITLE_CHARS = 60

# Thread-list preview length, taken from the turn's answer
PREVIEW_CHARS = 120








############################################################
# require_internal
############################################################
#
# The shared-secret gate every route below wears. Refuses
# with 403 when the header is absent or wrong — compared in
# constant time, so a byte-by-byte timing never spells the
# secret out — and also when the secret itself is
# unconfigured, so a misconfigured deployment fails closed,
# never open.
#
# Used by:
#   - every view in this file
############################################################

def require_internal(view):
    @wraps(view)
    def decorated(request, *args, **kwargs):
        secret = settings.ASSISTANT_INTERNAL_SECRET
        presented = request.META.get("HTTP_X_INTERNAL_SECRET", "")
        if not secret or not hmac.compare_digest(presented.encode(), secret.encode()):
            return json_error("Forbidden", 403)
        return view(request, *args, **kwargs)
    return decorated








############################################################
# _thread_for
############################################################
#
# The access rule, in one place: resolves a thread uuid
# against the caller-passed identity. An owned thread
# answers only to its owner; an ownerless (guest) thread
# answers to any presenter of the id; a deleted thread
# answers to nobody. Returns None for every refusal — the
# routes turn that into 404, never distinguishing "not
# yours" from "not there".
#
# Used by:
#   - thread_messages / thread_delete (below)
############################################################

def _thread_for(thread_id, user_id):
    thread = AssistantThread.objects.filter(id=thread_id, deleted_at__isnull=True).first()
    if thread is None:
        return None
    if thread.user_id is not None and str(thread.user_id) != str(user_id or ""):
        return None
    return thread








############################################################
# _thread_payload
############################################################
#
# One thread row as the wire dict every listing answers
# with — the container relays these to the app's thread
# list untouched.
#
# Used by:
#   - threads_create / threads_list / threads_lookup
############################################################

def _thread_payload(thread):
    return {
        "id": str(thread.id),
        "title": thread.title,
        "preview": thread.preview,
        "language": thread.language,
        "createdAt": thread.created_at,
        "lastMessageAt": thread.last_message_at,
    }








############################################################
# assistant_search
############################################################
#
# POST /internal/assistant/search
#   {query, limit?, language?} → {results: [...]}
#
# The searchHandbook tool's backend: embeds the query and
# answers the top chunks, shaped exactly like the tool
# contract's entries. A gateway failure is a 502 the
# container turns into a typed tool error for the model —
# never a crash of the stream.
#
# Used by:
#   - the assistant container — tools/searchHandbook
############################################################

@require_methods("POST")
@require_internal
def assistant_search(request):
    body = get_json_object(request)
    if body is None:
        return json_error("Invalid JSON", 400)
    query = str(body.get("query") or "").strip()
    if not query:
        return json_error("query is required", 400)

    language = body.get("language")
    if language not in ("lt", "en"):
        language = None

    try:
        results = search_chunks(query, limit=body.get("limit") or 5, language=language)
    except GatewayError as exc:
        logger.warning("assistant search embedding failed: %s", exc)
        return json_error("Embedding gateway unavailable", 502)
    return json_response({"results": results})








############################################################
# active_prompt
############################################################
#
# GET /internal/assistant/prompt
#   → {version, text} — the active appendix, or
#     {version: null, text: ""} when none is active
#
# The container lays this under its code-owned core prompt
# and stamps the version into every turn's telemetry. No
# active row is a valid state: the agent runs on the core
# prompt alone.
#
# Used by:
#   - the assistant container — cached per minute
############################################################

@require_methods("GET")
@require_internal
def active_prompt(request):
    row = AssistantPrompt.objects.filter(active=True).values("version", "text").first()
    if row is None:
        return json_response({"version": None, "text": ""})
    return json_response({"version": row["version"], "text": row["text"]})








############################################################
# threads_create
############################################################
#
# POST /internal/assistant/threads
#   {user_id?, language?} → the new thread's payload
#
# The uuid is minted here, server-side — the client learns
# it from the answer and, for guests, stores it as the
# credential it is.
#
# An identity the users table does not know (the
# container's session cache raced an account erasure) is
# a 400, decided by a LOOKUP before the insert, as
# threads_claim does. THE RULE on this stack: a
# try/except IntegrityError around a statement catches
# UNIQUE and CHECK violations but never a foreign key —
# Django declares every FK on PostgreSQL DEFERRABLE
# INITIALLY DEFERRED, so the FK is checked at COMMIT,
# after the view has returned, and the request answers 500
# instead. (SQLite checks it at statement time, which is
# why the old except appeared to work under the suite.) An
# inner transaction.atomic() would not help either: a
# savepoint release does not evaluate deferred constraints.
# The except stays as a belt for the other two violations.
#
# Used by:
#   - the assistant container — POST /api/assistant/threads
############################################################

@require_methods("POST")
@require_internal
def threads_create(request):
    body = get_json_object(request)
    if body is None:
        return json_error("Invalid JSON", 400)

    language = body.get("language")
    if language not in ("lt", "en"):
        language = "lt"
    user_id = body.get("user_id") or None
    if user_id and not User.objects.filter(id=user_id).exists():
        return json_error("unknown user_id", 400)
    now = timezone.now()
    try:
        thread = AssistantThread.objects.create(
            user_id=user_id,
            language=language,
            created_at=now,
            last_message_at=now,
        )
    except IntegrityError:
        return json_error("unknown user_id", 400)
    return json_response(_thread_payload(thread), status=201)








############################################################
# threads_list
############################################################
#
# GET /internal/assistant/threads/list?user_id=
#   → {threads: [...]} newest talk first
#
# The signed-in thread list. Guests never reach this route
# — their list is the lookup below, fed by the device's
# own id registry.
#
# Used by:
#   - the assistant container — GET /api/assistant/threads
############################################################

@require_methods("GET")
@require_internal
def threads_list(request):
    user_id = clean_param(request.GET.get("user_id", ""))
    if not user_id:
        return json_error("user_id is required", 400)
    threads = (AssistantThread.objects
               .filter(user_id=user_id, deleted_at__isnull=True)
               .order_by("-last_message_at")[:200])
    return json_response({"threads": [_thread_payload(thread) for thread in threads]})








############################################################
# threads_lookup
############################################################
#
# POST /internal/assistant/threads/lookup
#   {ids: [...], user_id?} → {threads: [...]}
#
# The guest thread list: the device presents the uuids it
# holds and gets back the ones that still exist and are
# either ownerless or owned by the passed identity. Ids
# that answer nothing were pruned or claimed elsewhere —
# the device drops them from its registry.
#
# Used by:
#   - the assistant container — POST /api/assistant/threads/lookup
############################################################

@require_methods("POST")
@require_internal
def threads_lookup(request):
    body = get_json_object(request)
    if body is None:
        return json_error("Invalid JSON", 400)
    ids = body.get("ids")
    if not isinstance(ids, list) or len(ids) > MAX_LOOKUP_IDS:
        return json_error(f"ids must be a list of at most {MAX_LOOKUP_IDS}", 400)

    user_id = body.get("user_id") or None
    found = []
    threads = AssistantThread.objects.filter(id__in=_valid_uuids(ids),
                                             deleted_at__isnull=True)
    for thread in threads:
        if thread.user_id is None or str(thread.user_id) == str(user_id or ""):
            found.append(thread)
    found.sort(key=lambda thread: thread.last_message_at, reverse=True)
    return json_response({"threads": [_thread_payload(thread) for thread in found]})








############################################################
# threads_claim
############################################################
#
# POST /internal/assistant/threads/claim
#   {ids: [...], user_id} → {claimed: n}
#
# A login adopting the device's guest threads: every
# presented id that is still ownerless gets the user set,
# so pre-login history follows the person into the
# account. Already-owned threads are left alone — even
# when owned by this same user.
#
# Used by:
#   - the assistant container — POST /api/assistant/threads/claim
############################################################

@require_methods("POST")
@require_internal
def threads_claim(request):
    body = get_json_object(request)
    if body is None:
        return json_error("Invalid JSON", 400)
    ids = body.get("ids")
    user_id = body.get("user_id")
    if not user_id:
        return json_error("user_id is required", 400)
    if not isinstance(ids, list) or len(ids) > MAX_LOOKUP_IDS:
        return json_error(f"ids must be a list of at most {MAX_LOOKUP_IDS}", 400)

    if not User.objects.filter(id=user_id).exists():
        return json_error("unknown user_id", 400)
    claimed = (AssistantThread.objects
               .filter(id__in=_valid_uuids(ids), user_id__isnull=True,
                       deleted_at__isnull=True)
               .update(user_id=user_id))
    return json_response({"claimed": claimed})








############################################################
# _valid_uuids
############################################################
#
# The lookup/claim bodies carry device-held strings — only
# well-formed uuids reach the query, the rest simply match
# nothing (exactly what a pruned or garbage id should do).
#
# Used by:
#   - threads_lookup / threads_claim (above)
############################################################

def _valid_uuids(ids):
    valid = []
    for one in ids:
        try:
            valid.append(uuid.UUID(str(one)))
        except ValueError:
            continue
    return valid








############################################################
# thread_messages
############################################################
#
# GET  /internal/assistant/threads/<uuid>/messages?user_id=
#   → {messages: [{id, format, content, createdAt}, ...]}
# POST /internal/assistant/threads/<uuid>/messages
#   {user_id?, messages: [UIMessage...]} → {stored: n}
#
# The transcript. GET seeds the client runtime on thread
# open; POST is the container's onFinish persistence —
# upserts by (thread, message id) so a retried turn never
# duplicates, stamps last_message_at, and titles the
# thread from its first user message. Content is stored
# verbatim: it IS the UIMessage the runtime replays.
#
# Used by:
#   - the assistant container — thread open + stream onFinish
############################################################

@require_methods("GET", "POST")
@require_internal
def thread_messages(request, thread_id):
    if request.method == "GET":
        thread = _thread_for(thread_id, clean_param(request.GET.get("user_id")))
        if thread is None:
            return json_error("Not found", 404)
        rows = thread.messages.order_by("created_at", "id").values("id", "format", "content", "created_at")
        return json_response({"messages": [
            {"id": row["id"], "format": row["format"], "content": row["content"],
             "createdAt": row["created_at"]}
            for row in rows
        ]})

    # POST — the turn upsert (the guard admits no third verb)
    body = get_json_object(request)
    if body is None:
        return json_error("Invalid JSON", 400)
    thread = _thread_for(thread_id, body.get("user_id"))
    if thread is None:
        return json_error("Not found", 404)
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages or len(messages) > MAX_MESSAGES_PER_POST:
        return json_error(f"messages must be a non-empty list of at most {MAX_MESSAGES_PER_POST}", 400)

    for message in messages:
        if not isinstance(message, dict) or not message.get("id") or not message.get("role"):
            return json_error("each message needs id and role", 400)

    # A message someone already put a thumb on is EVIDENCE — a
    # replayed batch naming its id must not rewrite what was
    # judged (client-chosen ids made that a one-request edit of
    # a rated answer). The replay tail re-presents old ids by
    # design; skipping the rated ones costs nothing.
    rated = set(
        AssistantMessage.objects
        .filter(thread=thread, id__in=[str(message["id"]) for message in messages],
                rating__isnull=False)
        .values_list("id", flat=True)
    )

    now = timezone.now()
    stored = 0
    for position, message in enumerate(messages):
        if str(message["id"]) in rated:
            continue
        # Each message gets its OWN microsecond-stepped stamp (a
        # shared one makes ORDER BY a coin flip), and created_at
        # sits in create_defaults so a re-upsert of an old
        # message never teleports it to the end of the thread
        AssistantMessage.objects.update_or_create(
            thread=thread, id=str(message["id"]),
            defaults={"format": "aisdk-v7", "content": message},
            create_defaults={"format": "aisdk-v7", "content": message,
                             "created_at": now + timedelta(microseconds=position)},
        )
        stored += 1

    updates = ["last_message_at"]
    thread.last_message_at = now
    if not thread.title:
        title = _first_user_text(messages)
        if title:
            thread.title = title[:TITLE_CHARS]
            updates.append("title")
    preview = _preview_text(messages)
    if preview:
        thread.preview = preview[:PREVIEW_CHARS]
        updates.append("preview")
    thread.save(update_fields=updates)
    return json_response({"stored": stored})








############################################################
# _preview_text
############################################################
#
# The thread list's second line: the LAST assistant text in
# the batch (the newest answer), falling back to the last
# user text when the answer never landed.
#
# Used by:
#   - thread_messages (above)
############################################################

def _preview_text(messages):
    # The ANSWER only — echoing the question as its own
    # "preview" made a tool-only turn's list row read like the
    # assistant had replied with the student's words
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        for part in message.get("parts") or []:
            if isinstance(part, dict) and part.get("type") == "text":
                text = str(part.get("text") or "").strip()
                if text:
                    return text
    return None








############################################################
# _first_user_text
############################################################
#
# The auto-title source: the first text part of the first
# user-role message in the batch, or None when the batch
# holds none (a tool-only replay, say).
#
# Used by:
#   - thread_messages (above)
############################################################

def _first_user_text(messages):
    for message in messages:
        if message.get("role") != "user":
            continue
        for part in message.get("parts") or []:
            if isinstance(part, dict) and part.get("type") == "text":
                text = str(part.get("text") or "").strip()
                if text:
                    return text
    return None








############################################################
# message_feedback
############################################################
#
# POST /internal/assistant/threads/<uuid>/feedback
#   {message_id, rating, user_id?} → {rated: true}
#
# The reader's thumbs verdict on one assistant message:
# +1 / -1 sets, 0 clears (a tapped-again thumb). Same
# access rule as the transcript, and only messages that
# exist in the thread take a rating — a stale client
# pointing at a pruned message gets the same cloaked 404.
#
# Used by:
#   - the assistant container — the kit's thumbs buttons
############################################################

@require_methods("POST")
@require_internal
def message_feedback(request, thread_id):
    body = get_json_object(request)
    if body is None:
        return json_error("Invalid JSON", 400)
    thread = _thread_for(thread_id, body.get("user_id"))
    if thread is None:
        return json_error("Not found", 404)

    rating = body.get("rating")
    if rating not in (-1, 0, 1):
        return json_error("rating must be -1, 0 or 1", 400)
    message_id = str(body.get("message_id") or "")
    if not message_id:
        return json_error("message_id is required", 400)

    changed = (AssistantMessage.objects
               .filter(thread=thread, id=message_id)
               .update(rating=None if rating == 0 else rating))
    if not changed:
        return json_error("Not found", 404)
    return json_response({"rated": True})








############################################################
# thread_delete
############################################################
#
# POST /internal/assistant/threads/<uuid>/delete
#   {user_id?} → {deleted: true}
#
# The GUI's soft delete — the row keeps its messages until
# cron hard-prunes both. Same access rule and the same 404
# for "not yours" and "not there".
#
# Used by:
#   - the assistant container — thread swipe-to-delete
############################################################

@require_methods("POST")
@require_internal
def thread_delete(request, thread_id):
    body = get_json_object(request)
    if body is None:
        return json_error("Invalid JSON", 400)
    thread = _thread_for(thread_id, body.get("user_id"))
    if thread is None:
        return json_error("Not found", 404)
    thread.deleted_at = timezone.now()
    thread.save(update_fields=["deleted_at"])
    return json_response({"deleted": True})








############################################################
# turn_log
############################################################
#
# POST /internal/assistant/turn-log → {logged: true}
#
# One AssistantTurn row per agent turn — fire-and-forget
# from the container, so this endpoint accepts generously:
# unknown outcomes fold to "error", counters clamp at
# zero, and nothing here ever blocks a user-facing stream.
#
# Used by:
#   - the assistant container — after every turn
############################################################

@require_methods("POST")
@require_internal
def turn_log(request):
    body = get_json_object(request)
    if body is None:
        return json_error("Invalid JSON", 400)

    outcome = body.get("outcome")
    if outcome not in TURN_OUTCOMES:
        outcome = "error"
    language = body.get("language")
    if language not in ("lt", "en"):
        language = "lt"

    def _count(name):
        try:
            return max(0, int(body.get(name) or 0))
        except (TypeError, ValueError):
            return 0

    # A malformed uuid or an unknown user folds to null —
    # telemetry never answers 500 over its own labels
    try:
        thread_id = uuid.UUID(str(body.get("thread_id"))) if body.get("thread_id") else None
    except ValueError:
        thread_id = None
    user_id = body.get("user_id") or None
    if thread_id and not AssistantThread.objects.filter(id=thread_id).exists():
        thread_id = None
    if user_id and not User.objects.filter(id=user_id).exists():
        user_id = None

    AssistantTurn.objects.create(
        created_at=timezone.now(),
        thread_id=thread_id,
        user_id=user_id,
        client_version=str(body.get("client_version") or "")[:100] or None,
        language=language,
        model=str(body.get("model") or "")[:100],
        prompt_version=body.get("prompt_version") if isinstance(body.get("prompt_version"), int) else None,
        input_tokens=_count("input_tokens"),
        output_tokens=_count("output_tokens"),
        tool_calls=body.get("tool_calls") if isinstance(body.get("tool_calls"), list) else None,
        duration_ms=_count("duration_ms"),
        outcome=outcome,
    )
    return json_response({"logged": True}, status=201)
