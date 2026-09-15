############################################################
#  [*] assistant admin API — the prompt store's editors
#
#  The admin console's door to the versioned prompt
#  appendix (jauka's prompt-set editing, single set): list
#  the versions, save a NEW immutable version, switch which
#  one is active — including to none, which runs the agent
#  on its code-owned core prompt alone. Role-gated like the
#  rest of the console and every mutation writes an
#  admin_audit row. Editing never touches an existing row:
#  a fix is a new version, so a turn's stamped
#  prompt_version always names exactly the text it ran
#  with.
#
#  Beyond the prompt store, the console's other two
#  assistant surfaces live here too: the KNOWLEDGE BASE
#  (per-source counts and freshness, a retrieval test box,
#  the re-index button running cron's exact sync) and the
#  THREAD REVIEW — the stored conversations with their
#  thumbs verdicts, transcript reads audited person by
#  person because student chats are personal data.
#
#  Split into:
#
#    _prompt_payload   — one wire shape for a version row
#    list_prompts      — GET the version history
#    create_prompt     — POST a new version (optionally live)
#    activate_prompt   — POST switch the active version
#    deactivate_prompt — POST back to the core prompt alone
#    assistant_overview — GET the dashboard numbers
#    reindex_knowledge — POST cron's sync, on demand
#    knowledge_search  — POST a retrieval test
#    review_threads    — GET the conversation list
#    review_thread     — GET one transcript, audited
############################################################


from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Q
from django.utils import timezone

from knfapp.admin.audit import write_audit
from knfapp.assistant.gateway import GatewayError
from knfapp.assistant.indexing import run_index
from knfapp.assistant.models import (
    AssistantMessage, AssistantPrompt, AssistantThread, AssistantTurn, SupportChunk,
)
from knfapp.assistant.search import search_chunks
from knfapp.common.http import get_json_object, json_error, json_response, parse_pagination
from knfapp.users.auth import require_role


# The appendix rides under the core prompt on every turn —
# a novel-sized one is a mistake, not a use case
MAX_PROMPT_CHARS = 8000








############################################################
# _prompt_payload
############################################################
#
# One version row as the console reads it.
#
# Used by:
#   - list_prompts / create_prompt (below)
############################################################

def _prompt_payload(row):
    return {
        "version": row.version,
        "text": row.text,
        "notes": row.notes,
        "active": row.active,
        "createdBy": row.created_by_id,
        "createdAt": row.created_at,
    }








############################################################
# list_prompts
############################################################
#
# GET /api/admin/assistant/prompts → {prompts: [...]} newest
# version first — the whole history; versions are immutable,
# so the list is also the changelog.
#
# Used by:
#   - the admin console
############################################################

@require_role("admin")
def list_prompts(request):
    if request.method != "GET":
        return json_error("Method not allowed", 405)
    rows = AssistantPrompt.objects.order_by("-version")[:200]
    return json_response({"prompts": [_prompt_payload(row) for row in rows]})








############################################################
# create_prompt
############################################################
#
# POST /api/admin/assistant/prompts
#   {text, notes?, activate?} → the new version's payload
#
# Mints version max+1 — never reuses a number, never edits
# an old row. activate=true flips the new version live in
# the same transaction (deactivating whichever was).
#
# Used by:
#   - the admin console
############################################################

@require_role("admin")
def create_prompt(request):
    if request.method != "POST":
        return json_error("Method not allowed", 405)
    body = get_json_object(request)
    if body is None:
        return json_error("Invalid JSON", 400)
    text = str(body.get("text") or "").strip()
    if not text:
        return json_error("text is required", 400)
    if len(text) > MAX_PROMPT_CHARS:
        return json_error(f"text is capped at {MAX_PROMPT_CHARS} characters", 400)

    # Two admins saving at once race on version max+1 (and on
    # the one-active constraint) — the loser's IntegrityError
    # is a retryable 409, never a raw 500
    try:
        with transaction.atomic():
            latest = AssistantPrompt.objects.aggregate(top=Max("version"))["top"] or 0
            if body.get("activate"):
                AssistantPrompt.objects.filter(active=True).update(active=False)
            row = AssistantPrompt.objects.create(
                version=latest + 1,
                text=text,
                notes=str(body.get("notes") or "").strip() or None,
                active=bool(body.get("activate")),
                created_by_id=request.user["id"],
                created_at=timezone.now(),
            )
    except IntegrityError:
        return json_error("Another admin saved at the same moment — reload and retry", 409)
    write_audit(request.user["id"], "assistant_prompt_create",
                target=f"v{row.version}", payload={"activate": row.active})
    return json_response(_prompt_payload(row), status=201)








############################################################
# activate_prompt
############################################################
#
# POST /api/admin/assistant/prompts/<version>/activate
#   → the now-active version's payload
#
# Deactivate-then-activate in one transaction — the partial
# unique constraint holds a racing double-activate to one
# winner.
#
# Used by:
#   - the admin console
############################################################

@require_role("admin")
def activate_prompt(request, version):
    if request.method != "POST":
        return json_error("Method not allowed", 405)
    # A racing double-activate leaves one loser on the partial
    # unique constraint — 409, retry, never a raw 500
    try:
        with transaction.atomic():
            row = AssistantPrompt.objects.filter(version=version).first()
            if row is None:
                return json_error("Not found", 404)
            AssistantPrompt.objects.filter(active=True).update(active=False)
            row.active = True
            row.save(update_fields=["active"])
    except IntegrityError:
        return json_error("Another admin activated at the same moment — reload and retry", 409)
    write_audit(request.user["id"], "assistant_prompt_activate", target=f"v{version}")
    return json_response(_prompt_payload(row))








############################################################
# deactivate_prompt
############################################################
#
# POST /api/admin/assistant/prompts/deactivate
#   → {active: null}
#
# Back to the core prompt alone — the valid empty state.
#
# Used by:
#   - the admin console
############################################################

@require_role("admin")
def deactivate_prompt(request):
    if request.method != "POST":
        return json_error("Method not allowed", 405)
    AssistantPrompt.objects.filter(active=True).update(active=False)
    write_audit(request.user["id"], "assistant_prompt_deactivate")
    return json_response({"active": None})








############################################################
# assistant_overview
############################################################
#
# GET /api/admin/assistant/overview
#
# The dashboard's numbers in one call: the knowledge base
# per source (count + newest indexing stamp), the active
# prompt version, and the last week of turns by outcome
# with the thumbs totals.
#
# Used by:
#   - the admin console — the assistant dashboard
############################################################

@require_role("admin")
def assistant_overview(request):
    if request.method != "GET":
        return json_error("Method not allowed", 405)

    sources = list(SupportChunk.objects.values("source")
                   .annotate(count=Count("id"), newest=Max("indexed_at"))
                   .order_by("source"))
    week_ago = timezone.now() - timedelta(days=7)
    turns = AssistantTurn.objects.filter(created_at__gte=week_ago)
    ratings = AssistantMessage.objects.aggregate(
        up=Count("id", filter=Q(rating=1)),
        down=Count("id", filter=Q(rating=-1)),
    )
    active = AssistantPrompt.objects.filter(active=True).values_list("version", flat=True).first()

    return json_response({
        "knowledge": {
            "total": sum(row["count"] for row in sources),
            "sources": [{"source": row["source"], "count": row["count"], "indexedAt": row["newest"]}
                        for row in sources],
        },
        "turns7d": {
            "total": turns.count(),
            "ok": turns.filter(outcome="ok").count(),
            "error": turns.filter(outcome="error").count(),
            "aborted": turns.filter(outcome="aborted").count(),
        },
        "ratings": {"up": ratings["up"], "down": ratings["down"]},
        "activePromptVersion": active,
        "threads": AssistantThread.objects.filter(deleted_at__isnull=True).count(),
    })








############################################################
# reindex_knowledge
############################################################
#
# POST /api/admin/assistant/knowledge/reindex {all?}
#
# Cron's exact sync, on demand — the button for "I just
# fixed the handbook, index it NOW". Synchronous on
# purpose: the incremental run is subsecond when nothing
# changed, and even a forced full re-embed of this corpus
# is well inside the request budget. A dead gateway
# answers 502 having written nothing.
#
# Used by:
#   - the admin console — the re-index buttons
############################################################

@require_role("admin")
def reindex_knowledge(request):
    if request.method != "POST":
        return json_error("Method not allowed", 405)
    body = get_json_object(request) or {}
    reindex_all = bool(body.get("all"))
    try:
        counts = run_index(reindex_all=reindex_all)
    except GatewayError as exc:
        return json_error(f"Embedding gateway unavailable: {exc}", 502)
    write_audit(request.user["id"], "assistant_reindex",
                payload={"all": reindex_all, **counts})
    return json_response(counts)








############################################################
# knowledge_search
############################################################
#
# POST /api/admin/assistant/knowledge/search {query,
#   language?} → {results: [...]}
#
# The retrieval test box: exactly what the agent's
# searchHandbook tool would be handed for this query, so an
# admin can check WHY an answer cited what it cited.
#
# Used by:
#   - the admin console — the knowledge test box
############################################################

@require_role("admin")
def knowledge_search(request):
    if request.method != "POST":
        return json_error("Method not allowed", 405)
    body = get_json_object(request)
    if body is None:
        return json_error("Invalid JSON", 400)
    query = str(body.get("query") or "").strip()
    if not query:
        return json_error("query is required", 400)
    language = body.get("language") if body.get("language") in ("lt", "en") else None
    try:
        results = search_chunks(query, limit=body.get("limit") or 5, language=language)
    except GatewayError as exc:
        return json_error(f"Embedding gateway unavailable: {exc}", 502)
    return json_response({"results": results})








############################################################
# review_threads
############################################################
#
# GET /api/admin/assistant/threads?page&per_page&rating=down
#
# The conversation list for review, newest talk first:
# title, preview, the asker (username or "guest"), message
# and thumbs counts. ?rating=down narrows to conversations
# holding at least one thumbs-down — the complaint queue.
# Soft-deleted threads stay listed (flagged) until cron
# hard-prunes them: a complaint does not vanish because the
# student swiped the thread away.
#
# Used by:
#   - the admin console — the review list
############################################################

@require_role("admin")
def review_threads(request):
    if request.method != "GET":
        return json_error("Method not allowed", 405)
    page, per_page, err = parse_pagination(request)
    if err:
        return err

    # The message model's composite PK rules COUNT(DISTINCT)
    # out — the page's counts come from ONE grouped query over
    # the page's threads instead of join annotations
    threads = AssistantThread.objects.select_related("user")
    if request.GET.get("rating") == "down":
        threads = threads.filter(messages__rating=-1).distinct()
    threads = threads.order_by("-last_message_at")

    total = threads.count()
    offset = (page - 1) * per_page
    page_threads = list(threads[offset:offset + per_page])
    stats = {
        row["thread_id"]: row
        for row in AssistantMessage.objects
        .filter(thread_id__in=[thread.id for thread in page_threads])
        .values("thread_id")
        .annotate(messages=Count("id"),
                  down=Count("id", filter=Q(rating=-1)),
                  up=Count("id", filter=Q(rating=1)))
    }
    rows = [{
        "id": str(thread.id),
        "title": thread.title,
        "preview": thread.preview,
        "language": thread.language,
        "user": thread.user.username if thread.user else None,
        "messages": stats.get(thread.id, {}).get("messages", 0),
        "up": stats.get(thread.id, {}).get("up", 0),
        "down": stats.get(thread.id, {}).get("down", 0),
        "deleted": thread.deleted_at is not None,
        "lastMessageAt": thread.last_message_at,
    } for thread in page_threads]
    return json_response({"threads": rows, "page": page, "perPage": per_page, "total": total})








############################################################
# review_thread
############################################################
#
# GET /api/admin/assistant/threads/<uuid>
#
# One transcript, flattened for reading: per message the
# role, the joined text parts, the tool names it called and
# its thumbs verdict. Every read writes an audit row —
# student conversations are personal data, and WHO looked
# at WHOSE chat must be answerable.
#
# Used by:
#   - the admin console — the transcript reader
############################################################

@require_role("admin")
def review_thread(request, thread_id):
    if request.method != "GET":
        return json_error("Method not allowed", 405)
    thread = AssistantThread.objects.filter(id=thread_id).select_related("user").first()
    if thread is None:
        return json_error("Not found", 404)

    messages = []
    for row in thread.messages.order_by("created_at"):
        parts = row.content.get("parts") if isinstance(row.content, dict) else None
        texts = []
        tools = []
        for part in parts or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text" and part.get("text"):
                texts.append(str(part["text"]))
            elif str(part.get("type") or "").startswith("tool-"):
                tools.append(str(part["type"])[5:])
        messages.append({
            "id": row.id,
            "role": row.content.get("role") if isinstance(row.content, dict) else None,
            "text": "\n\n".join(texts),
            "tools": tools,
            "rating": row.rating,
            "createdAt": row.created_at,
        })

    write_audit(request.user["id"], "assistant_thread_view", target=str(thread.id))
    return json_response({
        "id": str(thread.id),
        "title": thread.title,
        "language": thread.language,
        "user": thread.user.username if thread.user else None,
        "deleted": thread.deleted_at is not None,
        "createdAt": thread.created_at,
        "lastMessageAt": thread.last_message_at,
        "messages": messages,
    })
