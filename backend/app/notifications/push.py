############################################################
#  [*] Notifications — Expo push delivery and fan-out
#
#  The only path from the backend to a phone: every helper
#  here ends in a POST to Expo's push service
#  (exp.host/--/api/v2/push/send), which relays to APNs and
#  FCM. Tokens arrive through POST /api/notifications/
#  register (notifications/routes.py) and live in
#  push_tokens; a "DeviceNotRegistered" verdict — from a
#  ticket or, minutes later, from a receipt — flips the row
#  to active=0 and the next register from that device flips
#  it back.
#
#  Delivery has two stages and both are watched now:
#    tickets  — the send response. "ok" means Expo ACCEPTED
#               the message; every return value in this
#               module counts tickets, never deliveries
#    receipts — poll_push_receipts asks Expo what actually
#               became of those tickets 15 minutes later,
#               retires dead devices and surfaces the
#               operator-level failures (InvalidCredentials,
#               MessageRateExceeded) that used to be
#               invisible
#
#  Transport rules, all in one module-level requests.Session:
#  3 retries with backoff on 429/5xx honouring Retry-After,
#  a 10 s timeout per call so the server always gives up
#  before the app's own 15 s, slices of 100 (Expo's cap) sent
#  from a bounded thread pool under a 120 s fan-out deadline,
#  and a paced gate that keeps the process under Expo's
#  ~600 messages/s ceiling. No Expo access token header (the
#  Expo project has to keep enhanced push security off).
#  Failures are logged and swallowed: push is best-effort
#  everywhere and never fails a request.
#
#  Nothing here logs a raw token. Every upstream excerpt goes
#  through _sanitize, which folds newlines away and redacts
#  anything token-shaped to its sha256[:8] digest.
#
#  Two tiers of helpers:
#    send_push_notification / send_push_batch — raw Expo
#      calls (one ticket / slices of 100)
#    notify_channel_user / notify_channel_users /
#      notify_channel — honour the per-user opt-out in
#      notification_channels: a missing row means ENABLED,
#      only an explicit enabled=0 suppresses (opt-out model,
#      migration v7); data["channel"] is stamped on the
#      payload. All take optional title_en/body_en and route
#      them to tokens whose push_tokens.language (migration
#      v11) is 'en'; without them every device gets the
#      Lithuanian text. The channel name is checked against
#      VALID_CHANNELS first — an unknown name would ignore
#      every opt-out, so it sends nothing at all
#
#  Payload contract with the app: app/_layout.tsx routes a
#  tapped notification on data.type — "chat_message" (+
#  conversationId) opens the room, "news" and
#  "admin_announcement" open the news tab. Nothing in the
#  app reads the "channel" stamp yet. Every message carries
#  channelId "default", the Android channel
#  services/notifications.ts registers.
############################################################


import hashlib
import logging
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional

import requests
from requests.adapters import HTTPAdapter, Retry

from app.database import get_db, utc_now_iso

logger = logging.getLogger(__name__)

# One URL for both shapes: a single message object or an
# array of up to 100 of them
EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"

# Where a ticket id is traded for the delivery verdict
EXPO_RECEIPTS_URL = "https://exp.host/--/api/v2/push/getReceipts"

# The channel names the fan-out helpers accept — the same
# four as the CHECK on notification_channels.channel
# (database/__init__.py); notifications/routes.py imports
# this tuple instead of keeping a second copy
VALID_CHANNELS = ("news", "chat", "schedule", "admin")

# Expo caps a send at 100 messages and a receipt query at
# 1000 ids; SQLite caps a statement at 999 variables, so the
# recipient chunk stays well under that
_SEND_SLICE = 100
_RECEIPT_SLICE = 300
_ID_CHUNK = 400

# Short on purpose: the mobile client gives up at 15 s, so
# the server must always fold first
_HTTP_TIMEOUT = 10

# Broadcast fan-out: a few workers and a hard deadline, after
# which the remaining slices are logged and abandoned rather
# than holding a scheduler thread for minutes
_FANOUT_WORKERS = 3
_FANOUT_DEADLINE = 120

# ~600 messages/s is Expo's documented ceiling — 100 per
# slice no oftener than every 0.2 s leaves plenty of headroom
_SLICE_INTERVAL = 0.2
_pace_lock = threading.Lock()
_last_slice_at = 0.0

# Tickets waiting for their receipt: (ticket id, token,
# monotonic stamp), bounded and in memory only — losing the
# queue on restart is acceptable for best-effort push, the
# next send refills it
_RECEIPT_DELAY = 900
_receipt_queue = deque(maxlen=20000)
_receipt_lock = threading.Lock()

# Anything token-shaped inside an upstream string is redacted
# before it reaches a log line
_TOKEN_PATTERN = re.compile(r"Expo(?:nent)?PushToken\[[^\]\r\n]*\]")








############################################################
# _build_session
############################################################
#
# The one requests.Session every Expo call shares:
# connection reuse plus urllib3 retries — 3 attempts, a
# growing backoff, 429 and 5xx retried, Retry-After honoured,
# and POST listed explicitly because urllib3 never retries a
# non-idempotent method on its own. Before this a
# rate-limited slice was simply dropped.
#
# Used by:
#   - _SESSION (below) — built once at import
############################################################

def _build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        respect_retry_after_header=True,
        allowed_methods=["POST"],
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


# Module-level: one pool of connections for the whole process
_SESSION = _build_session()

# Both Expo endpoints want the same two headers
_EXPO_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
}








############################################################
# token_digest
############################################################
#
# The first 8 hex digits of sha256(token) — the ONLY form a
# push token may take in a log line. Enough to tie two lines
# to the same device, useless to anyone who wants to push to
# it (a token is a bearer credential for that phone).
#
# Used by:
#   - _sanitize, _send_slice, _deactivate_tokens,
#     poll_push_receipts (below)
#   - notifications/routes.py — register_token's cross-user
#     takeover warning
############################################################

def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:8]








############################################################
# _sanitize
############################################################
#
# Log-safe form of an upstream string: CR/LF folded to spaces
# so an Expo body can never forge extra log lines, every
# token-shaped substring replaced by its digest (Expo quotes
# the offending token back at us), then truncated. Every
# excerpt logged in this module goes through here.
#
# Used by:
#   - send_push_notification, _send_slice,
#     poll_push_receipts (below)
############################################################

def _sanitize(text, limit: int = 200) -> str:
    if not isinstance(text, str):
        text = str(text)
    text = _TOKEN_PATTERN.sub(lambda m: f"token:{token_digest(m.group(0))}", text)
    return text.replace("\r", " ").replace("\n", " ")[:limit]








############################################################
# _pace_slice
############################################################
#
# Blocks until at least _SLICE_INTERVAL has passed since the
# previous slice left the process — a process-wide gate, so
# the fan-out pool cannot collectively outrun Expo's ~600
# messages/s ceiling. The sleep happens under the lock on
# purpose: that is what serialises the waiters.
#
# Used by:
#   - _send_slice (below)
############################################################

def _pace_slice():
    global _last_slice_at

    with _pace_lock:
        wait = _SLICE_INTERVAL - (time.monotonic() - _last_slice_at)
        if wait > 0:
            time.sleep(wait)
        _last_slice_at = time.monotonic()








############################################################
# _queue_receipt
############################################################
#
# Parks one accepted ticket for poll_push_receipts to look
# up later. The deque is bounded, so a flood of sends drops
# the oldest ids instead of growing without limit, and it
# never touches the database — receipts are diagnostics, not
# state worth persisting.
#
# Used by:
#   - send_push_notification, _send_slice (below)
############################################################

def _queue_receipt(ticket_id, token: str):
    if not ticket_id:
        return

    with _receipt_lock:
        _receipt_queue.append((ticket_id, token, time.monotonic()))








############################################################
# send_push_notification
############################################################
#
# One message to one token, True only for an "ok" ticket.
# The STATUS is checked before the body is touched (an Expo
# 5xx answers with an HTML error page, which used to fall
# into the generic except and be logged as "Failed to send"),
# and the JSON parse has its own guard. A
# "DeviceNotRegistered" ticket retires the token; an accepted
# one is parked for the receipt poll. Only this sender takes
# a badge count — the batch sender does not.
#
# Used by:
#   - nothing calls this at the moment — every notify_*
#     helper goes through send_push_batch, even for a
#     single device
############################################################

def send_push_notification(
    token: str,
    title: str,
    body: str,
    data: Optional[dict] = None,
    badge: Optional[int] = None,
) -> bool:
    # STEP 1: the message — channelId matches the Android
    # channel the app registers at startup
    # ===================================================
    message = {
        "to": token,
        "title": title,
        "body": body,
        "sound": "default",
        "channelId": "default",
    }
    if data:
        message["data"] = data
    if badge is not None:
        message["badge"] = badge


    # STEP 2: POST through the retrying session, status first
    # =======================================================
    try:
        resp = _SESSION.post(
            EXPO_PUSH_URL,
            json=message,
            headers=_EXPO_HEADERS,
            timeout=_HTTP_TIMEOUT,
        )
    except Exception:
        logger.exception("Failed to send push notification")
        return False

    if resp.status_code != 200:
        logger.warning("Expo push HTTP %d: %s", resp.status_code, _sanitize(resp.text, 500))
        return False

    try:
        payload = resp.json()
    except ValueError:
        logger.warning("Expo push answered non-JSON: %s", _sanitize(resp.text, 500))
        return False

    # A 200 whose body is valid JSON but not an OBJECT (an
    # array, a bare string) carries no envelope to read "data"
    # off — .get on it used to raise straight into the caller,
    # the one path in this module that did not swallow. The
    # batch sender has always guarded the same shape
    ticket = payload.get("data") if isinstance(payload, dict) else None


    # STEP 3: a 200 still carries the per-message verdict in
    # data.status; details.error names the reason
    # ======================================================
    if not isinstance(ticket, dict):
        logger.warning("Expo push: unexpected body shape %s", _sanitize(ticket))
        return False

    if ticket.get("status") == "error":
        detail = ticket.get("details") if isinstance(ticket.get("details"), dict) else {}
        error_type = detail.get("error")
        if error_type == "DeviceNotRegistered":
            _deactivate_tokens([token])
        else:
            logger.warning(
                "Expo push error %s for token:%s — %s",
                error_type, token_digest(token), _sanitize(ticket.get("message", "")),
            )
        return False

    _queue_receipt(ticket.get("id"), token)
    return True








############################################################
# _send_slice
############################################################
#
# One POST of at most 100 messages, returning (accepted,
# dead tokens, error tally) — the caller does the
# deactivating and the counting, so a slice never opens a
# database connection of its own. Past the fan-out deadline
# it sends nothing and says so. Tickets come back in request
# order, which is the only reason batch[idx] maps a verdict
# to its token; every step of that mapping is guarded (list
# shape, dict entries, index bound, per-ticket try) so one
# malformed entry cannot abandon the rest of the slice.
#
# Used by:
#   - send_push_batch (below) — inline for a single slice,
#     from the thread pool for several
############################################################

def _send_slice(batch: list[dict], deadline: float):
    dead: list[str] = []
    errors: dict = {}

    # STEP 1: the deadline belongs to the whole fan-out — a
    # broadcast must never hold its caller's thread for
    # minutes
    # =====================================================
    if time.monotonic() >= deadline:
        logger.warning("Push fan-out deadline reached — abandoning a slice of %d message(s)", len(batch))
        errors["Abandoned"] = len(batch)
        return 0, dead, errors


    # STEP 2: paced POST through the retrying session; a
    # non-200 or an exception costs this slice only
    # ==================================================
    try:
        _pace_slice()
        resp = _SESSION.post(
            EXPO_PUSH_URL,
            json=batch,
            headers=_EXPO_HEADERS,
            timeout=_HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.warning("Expo batch push HTTP %d: %s", resp.status_code, _sanitize(resp.text))
            errors[f"HTTP {resp.status_code}"] = len(batch)
            return 0, dead, errors
        payload = resp.json()
    except Exception:
        logger.exception("Failed to send push batch")
        errors["RequestFailed"] = len(batch)
        return 0, dead, errors


    # STEP 3: a 200 can carry a top-level "errors" array
    # instead of "data" — that is how Expo reports a
    # request-level problem (a malformed batch, bad
    # credentials)
    # ==================================================
    if isinstance(payload, dict) and payload.get("errors"):
        logger.warning("Expo batch push rejected: %s", _sanitize(payload.get("errors")))
        errors["Rejected"] = len(batch)
        return 0, dead, errors

    results = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        logger.warning("Expo batch push: unexpected body shape %s", _sanitize(payload))
        errors["Malformed"] = len(batch)
        return 0, dead, errors


    # STEP 4: one ticket per message, positionally, every
    # error code logged and tallied
    # ===================================================
    sent = 0
    for idx, ticket in enumerate(results):
        if idx >= len(batch):
            logger.warning("Expo returned %d ticket(s) for %d message(s) — ignoring the tail", len(results), len(batch))
            break

        try:
            if not isinstance(ticket, dict):
                errors["Malformed"] = errors.get("Malformed", 0) + 1
                continue

            token = batch[idx]["to"]
            if ticket.get("status") == "ok":
                sent += 1
                _queue_receipt(ticket.get("id"), token)
                continue

            detail = ticket.get("details") if isinstance(ticket.get("details"), dict) else {}
            code = detail.get("error") or "Unknown"
            errors[code] = errors.get(code, 0) + 1

            # The dead-device case is retired in one UPDATE by
            # the caller and logged there with its digest
            if code == "DeviceNotRegistered":
                dead.append(token)
            else:
                logger.warning(
                    "Expo ticket error %s for token:%s — %s",
                    code, token_digest(token), _sanitize(ticket.get("message", "")),
                )
        except Exception:
            logger.exception("Failed to read an Expo ticket")
            errors["Malformed"] = errors.get("Malformed", 0) + 1

    return sent, dead, errors








############################################################
# send_push_batch
############################################################
#
# The same title/body to many tokens, POSTed in slices of
# 100 (Expo's per-request cap) — one slice inline, several
# from a bounded thread pool sharing one 120 s deadline, so
# a slow Expo can delay a broadcast but never park a request
# or a scheduler tick indefinitely. Returns the number of
# "ok" TICKETS; pass a `stats` dict to also receive
# {"sent", "failed", "errors": {code: count}} for a caller
# that reports more than a single number (admin's broadcast
# route) — the int return stays the wire-facing value.
# priority/ttl ride into every message when given: the
# channel helpers set "high" + 1 h for chat and a day's ttl
# for the rest. The same `data` object rides in every message
# of the batch — fine, it is only serialised.
#
# Used by:
#   - _send_by_language (below) — every notify_* path
#   - chat/routes.py — _push_chat_message's standalone
#     fallback (retires once notify_channel_users is in)
############################################################

def send_push_batch(
    tokens: list[str],
    title: str,
    body: str,
    data: Optional[dict] = None,
    priority: Optional[str] = None,
    ttl: Optional[int] = None,
    stats: Optional[dict] = None,
) -> int:
    if not tokens:
        return 0


    # STEP 1: one message per token, identical apart from "to"
    # ========================================================
    messages = []
    for token in tokens:
        msg = {
            "to": token,
            "title": title,
            "body": body,
            "sound": "default",
            "channelId": "default",
        }
        if data:
            msg["data"] = data
        if priority:
            msg["priority"] = priority
        if ttl is not None:
            msg["ttl"] = ttl
        messages.append(msg)


    # STEP 2: slice at Expo's cap, then send — a lone slice
    # inline (no pool for one HTTP call), the rest through a
    # small pool under one shared deadline
    # =====================================================
    slices = [messages[i : i + _SEND_SLICE] for i in range(0, len(messages), _SEND_SLICE)]
    deadline = time.monotonic() + _FANOUT_DEADLINE

    if len(slices) == 1:
        results = [_send_slice(slices[0], deadline)]
    else:
        with ThreadPoolExecutor(max_workers=min(_FANOUT_WORKERS, len(slices))) as pool:
            results = list(pool.map(lambda part: _send_slice(part, deadline), slices))


    # STEP 3: merge the per-slice verdicts on this thread —
    # the workers share no mutable state
    # =====================================================
    sent = 0
    dead: list[str] = []
    errors: dict = {}
    for slice_sent, slice_dead, slice_errors in results:
        sent += slice_sent
        dead.extend(slice_dead)
        for code, count in slice_errors.items():
            errors[code] = errors.get(code, 0) + count


    # STEP 4: every dead device of the whole batch in ONE
    # UPDATE, then the tally
    # ===================================================
    if dead:
        _deactivate_tokens(dead)

    if stats is not None:
        stats["sent"] = stats.get("sent", 0) + sent
        stats["failed"] = stats.get("failed", 0) + (len(tokens) - sent)
        tally = stats.setdefault("errors", {})
        for code, count in errors.items():
            tally[code] = tally.get(code, 0) + count

    if errors:
        logger.warning(
            "Push batch errors: %s",
            ", ".join(f"{code}={count}" for code, count in sorted(errors.items())),
        )

    # Per token, not per slice — and accepted, not delivered
    logger.info("Push batch: %d/%d accepted by Expo", sent, len(tokens))
    return sent








############################################################
# _deactivate_tokens
############################################################
#
# Flips push_tokens.active to 0 for every token Expo reported
# as "DeviceNotRegistered" (app uninstalled, permission
# revoked) — the WHOLE batch in one connection and one
# UPDATE, where the per-token version used to open a fresh
# connection and write transaction from inside the ticket
# loop. token is UNIQUE (idx_push_tokens_token), so each name
# moves at most one row. Rows are kept, not deleted: the next
# POST /api/notifications/register from that device
# (notifications/routes.py, register_token) sets active=1
# again. updated_at is refreshed so the row's age means
# something. Never raises; the try also covers get_db()
# itself failing.
#
# Used by:
#   - send_push_notification, send_push_batch,
#     poll_push_receipts (above and below)
############################################################

def _deactivate_tokens(tokens) -> int:
    unique = sorted({t for t in tokens if t})
    if not unique:
        return 0

    try:
        db = get_db()
        try:
            # Naive-UTC isoformat — the shape every push_tokens
            # timestamp already carries (register_token stamps
            # the same way)
            now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
            placeholders = ",".join("?" * len(unique))
            changed = db.execute(
                f"UPDATE push_tokens SET active = 0, updated_at = ? WHERE token IN ({placeholders})",
                [now, *unique],
            ).rowcount
            db.commit()
        finally:
            db.close()
    except Exception:
        logger.exception("Failed to deactivate tokens")
        return 0

    if changed:
        # Digests only — a raw token is a bearer credential
        logger.info(
            "Deactivated %d unregistered token(s): %s",
            changed, ", ".join(token_digest(t) for t in unique[:20]),
        )
    return changed








############################################################
# poll_push_receipts
############################################################
#
# Stage two of delivery: every ticket parked at least 15
# minutes ago is traded for its receipt at Expo. A
# "DeviceNotRegistered" receipt retires the token — the
# uninstall case the ticket stage cannot see, because a
# ticket only says Expo took the message. "InvalidCredentials"
# (the Expo project's APNs/FCM credentials are broken, so
# NOTHING is arriving on any device) and "MessageRateExceeded"
# are logged at WARNING: they are operator problems, and
# before this job they were completely invisible. Entries
# younger than the delay go back on the queue. Best-effort
# throughout — the queue is memory-only and a failed slice is
# simply skipped.
#
# Used by:
#   - scraper/scheduler.py — the 15-minute receipts job
#     (registered by the scrapers package, max_instances=1)
############################################################

def poll_push_receipts() -> int:
    # STEP 1: drain what is old enough, put the young back
    # ====================================================
    cutoff = time.monotonic() - _RECEIPT_DELAY
    due = {}
    keep = []
    with _receipt_lock:
        while _receipt_queue:
            ticket_id, token, stamped = _receipt_queue.popleft()
            if stamped <= cutoff:
                due[ticket_id] = token
            else:
                keep.append((ticket_id, token, stamped))
        _receipt_queue.extend(keep)

    if not due:
        return 0


    # STEP 2: ask Expo in slices; a slice that fails costs
    # only its own ids
    # ====================================================
    ids = list(due.keys())
    checked = 0
    dead: list[str] = []

    for i in range(0, len(ids), _RECEIPT_SLICE):
        part = ids[i : i + _RECEIPT_SLICE]
        try:
            resp = _SESSION.post(
                EXPO_RECEIPTS_URL,
                json={"ids": part},
                headers=_EXPO_HEADERS,
                timeout=_HTTP_TIMEOUT,
            )
            if resp.status_code != 200:
                logger.warning("Expo receipts HTTP %d: %s", resp.status_code, _sanitize(resp.text))
                continue
            receipts = resp.json().get("data")
        except Exception:
            logger.exception("Failed to fetch push receipts")
            continue

        if not isinstance(receipts, dict):
            logger.warning("Expo receipts: unexpected body shape %s", _sanitize(receipts))
            continue


        # STEP 3: verdict per ticket — retire dead devices,
        # shout about the operator-level failures
        # =================================================
        for ticket_id, receipt in receipts.items():
            checked += 1
            if not isinstance(receipt, dict) or receipt.get("status") != "error":
                continue

            detail = receipt.get("details") if isinstance(receipt.get("details"), dict) else {}
            code = detail.get("error") or "Unknown"
            token = due.get(ticket_id) or ""

            if code == "DeviceNotRegistered":
                dead.append(token)
            else:
                logger.warning(
                    "Expo receipt error %s for token:%s — %s",
                    code, token_digest(token) if token else "unknown",
                    _sanitize(receipt.get("message", "")),
                )

    if dead:
        _deactivate_tokens(dead)

    logger.info("Push receipts: %d checked, %d device(s) retired", checked, len(dead))
    return checked








############################################################
# prune_orphan_push_tokens
############################################################
#
# Deletes every push_tokens row whose owner holds no
# unexpired session. A phone that logged out — or whose
# 30-day session simply ran out — must stop receiving chat
# previews, and a device that IS still logged in re-registers
# its token on the next cold start, so the row comes right
# back. Deliberately NOT a per-logout wipe: dropping all of a
# user's tokens when one device logs out would silence their
# other phones (auth's logout deletes only the row for the
# optional "pushToken" it is handed).
#
# sessions.expires_at is aware isoformat UTC everywhere
# (migration v17 normalised the stragglers), so the plain
# string comparison against utc_now_iso() is correct —
# exactly what sweep_expired_sessions does.
#
# Used by:
#   - scraper/scheduler.py — the daily prune job (registered
#     by the scrapers package)
############################################################

def prune_orphan_push_tokens() -> int:
    try:
        db = get_db()
        try:
            removed = db.execute(
                """
                DELETE FROM push_tokens
                WHERE user_id NOT IN (
                    SELECT user_id FROM sessions WHERE expires_at > ?
                )
                """,
                (utc_now_iso(),),
            ).rowcount
            db.commit()
        finally:
            db.close()
    except Exception:
        logger.exception("Failed to prune push tokens")
        return 0

    if removed:
        logger.info("Pruned %d push token(s) whose owner has no live session", removed)
    return removed








############################################################
# _send_by_language
############################################################
#
# The tail every notify_* helper shares: stamp the channel on
# a COPY of the caller's data (their dict must never grow a
# "channel"), split the rows by push_tokens.language and make
# one send_push_batch call per language. Anything that is not
# 'en' rides the Lithuanian batch — 'lt' is the column
# default and the app default alike. Delivery hints go per
# channel: a chat preview is worth waking a dozing phone for
# (priority "high", ttl 1 h, otherwise Android Doze can sit
# on an FCM normal-priority message for many minutes), while
# news/schedule/admin take the default priority and a day of
# ttl.
#
# Used by:
#   - notify_channel_users, notify_channel (below)
############################################################

def _send_by_language(channel, rows, title, body, data, title_en, body_en, stats) -> int:
    if not rows:
        return 0

    push_data = dict(data or {})
    push_data["channel"] = channel

    priority = "high" if channel == "chat" else None
    ttl = 3600 if channel == "chat" else 86400

    lt_tokens = [r["token"] for r in rows if r["language"] != "en"]
    en_tokens = [r["token"] for r in rows if r["language"] == "en"]

    sent = 0
    if lt_tokens:
        sent += send_push_batch(lt_tokens, title, body, push_data, priority=priority, ttl=ttl, stats=stats)
    if en_tokens:
        sent += send_push_batch(en_tokens, title_en or title, body_en or body, push_data,
                                priority=priority, ttl=ttl, stats=stats)
    return sent








############################################################
# notify_channel_users
############################################################
#
# MANY users, one channel, in ONE database query and one Expo
# batch per language — the shape chat's fan-out needs, where
# calling notify_channel_user per offline participant used to
# mean a connection and an Expo round-trip each. Users with
# an explicit notification_channels row enabled=0 for the
# channel drop out in SQL (opt-out model: a missing row means
# enabled). The id list is chunked so a huge recipient set
# cannot hit SQLite's variable limit. Returns accepted
# tickets (devices, not users); `stats` works as in
# send_push_batch.
#
# Used by:
#   - chat/routes.py — _push_chat_message, "chat" channel,
#     off the request thread once send_message has committed
#   - notify_channel_user (below) — the one-user shape
############################################################

def notify_channel_users(channel: str, user_ids, title: str, body: str, data: Optional[dict] = None,
                         title_en: Optional[str] = None, body_en: Optional[str] = None,
                         stats: Optional[dict] = None) -> int:
    # STEP 1: an unknown channel has no opt-out rows at all,
    # so it would send to EVERY device — refuse it loudly
    # ======================================================
    if channel not in VALID_CHANNELS:
        logger.error("Refusing push on unknown channel %r", channel)
        return 0

    ids = sorted({u for u in (user_ids or []) if u})
    if not ids:
        return 0


    # STEP 2: tokens minus the channel's opt-outs, one query
    # per chunk of recipients
    # ======================================================
    rows = []
    db = get_db()
    try:
        for i in range(0, len(ids), _ID_CHUNK):
            part = ids[i : i + _ID_CHUNK]
            placeholders = ",".join("?" * len(part))
            rows.extend(db.execute(
                f"""
                SELECT pt.token, pt.language
                FROM push_tokens pt
                WHERE pt.active = 1
                  AND pt.user_id IN ({placeholders})
                  AND NOT EXISTS (
                    SELECT 1 FROM notification_channels nc
                    WHERE nc.user_id = pt.user_id
                      AND nc.channel = ?
                      AND nc.enabled = 0
                  )
                """,
                [*part, channel],
            ).fetchall())
    finally:
        db.close()

    return _send_by_language(channel, rows, title, body, data, title_en, body_en, stats)








############################################################
# notify_channel_user
############################################################
#
# One user, one channel — the single-recipient shape of
# notify_channel_users, kept because reading
# notify_channel_users("chat", [user_id], ...) at a call site
# is worse than reading this. Everything that matters (the
# opt-out check, the language split, the channel validation)
# lives in the batched helper, so the two can no longer
# drift apart.
#
# Used by:
#   - nothing calls this at the moment — chat/routes.py
#     moved to notify_channel_users when the fan-out went
#     off the request thread
############################################################

def notify_channel_user(channel: str, user_id: str, title: str, body: str, data: Optional[dict] = None,
                        title_en: Optional[str] = None, body_en: Optional[str] = None,
                        stats: Optional[dict] = None) -> int:
    return notify_channel_users(channel, [user_id], title, body, data=data,
                                title_en=title_en, body_en=body_en, stats=stats)








############################################################
# notify_channel
############################################################
#
# Broadcast to every active token whose owner has NOT
# opted out of the channel: NOT EXISTS on a
# notification_channels row with enabled=0, so users who
# never touched their settings are included (opt-out
# model). The exclude clause is appended after the NOT
# EXISTS closes, so it ANDs at the top level as intended.
# The DISTINCT is gone — token is UNIQUE, so it only cost a
# sort — and migration v46 gives the scan the
# (active, user_id) index it wants. An unknown channel name
# has no opt-out rows at all, which would silently mean "send
# to everyone", so it is refused here and logged instead.
# The count is devices, not users; "channel" is stamped on
# a copy of `data`. Devices registered with language 'en'
# (push_tokens.language, migration v11) get title_en/
# body_en when the caller supplies them — the scrapers do —
# and the Lithuanian text otherwise, as the admin broadcast
# does (an admin types one text, nothing translates it).
# `stats` works as in send_push_batch: pass a dict to learn
# how many slices failed, since the int return counts only
# what Expo accepted — plus stats["users"], the number of
# DISTINCT owners behind the targeted tokens.
#
# Used by:
#   - scraper/knf_scraper.py — scrape_knf_news, "news"
#   - scraper/vu_scraper.py — scrape_vu_news, "news"
#   - scraper/schedule_scraper.py — scrape_knf_schedule,
#     "schedule"
#   - admin/routes.py — send_admin_notification, "admin"
#     (that route itself has no caller in the app yet)
############################################################

def notify_channel(channel: str, title: str, body: str, data: Optional[dict] = None, exclude_user_id: Optional[str] = None,
                   title_en: Optional[str] = None, body_en: Optional[str] = None,
                   stats: Optional[dict] = None) -> int:
    # STEP 1: a name outside VALID_CHANNELS would ignore every
    # opt-out — nothing goes out on it
    # ========================================================
    if channel not in VALID_CHANNELS:
        logger.error("Refusing broadcast on unknown channel %r", channel)
        return 0


    # STEP 2: every active token minus this channel's opt-outs
    # ========================================================
    db = get_db()
    try:
        # Opt-out model in SQL: a user is excluded only by an
        # explicit enabled=0 row for this channel
        query = """
            SELECT pt.token, pt.language, pt.user_id
            FROM push_tokens pt
            WHERE pt.active = 1
              AND NOT EXISTS (
                SELECT 1 FROM notification_channels nc
                WHERE nc.user_id = pt.user_id
                  AND nc.channel = ?
                  AND nc.enabled = 0
              )
        """
        params: list = [channel]

        if exclude_user_id:
            query += " AND pt.user_id != ?"
            params.append(exclude_user_id)

        rows = db.execute(query, params).fetchall()
    finally:
        db.close()

    # Tokens are devices, not people — one reader with nine
    # phones is nine tokens. The distinct-owner count rides in
    # stats so a broadcast can report reach honestly
    if stats is not None:
        stats["users"] = len({r["user_id"] for r in rows})

    return _send_by_language(channel, rows, title, body, data, title_en, body_en, stats)
