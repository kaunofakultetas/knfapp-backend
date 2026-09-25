############################################################
#  [*] Notifications — Expo push delivery and fan-out
#
#  The only path from the backend to a phone: every helper
#  here ends in a POST to Expo's push service
#  (exp.host/--/api/v2/push/send), which relays to APNs and
#  FCM. Tokens arrive through POST /api/notifications/
#  register and live in push_tokens; a "DeviceNotRegistered"
#  verdict — from a ticket or, minutes later, from a receipt
#  — flips the row to active=0 and the next register from
#  that device flips it back.
#
#  Delivery has two stages and both are watched:
#    tickets  — the send response. "ok" means Expo ACCEPTED
#               the message; every return value in this
#               module counts tickets, never deliveries
#    receipts — poll_push_receipts asks Expo what actually
#               became of those tickets 15 minutes later,
#               retires dead devices and surfaces the
#               operator-level failures (InvalidCredentials,
#               MessageRateExceeded) that are otherwise
#               invisible
#
#  The receipt queue is a small JSON file every process in
#  the container shares — the server AND each cron command
#  that fans out (scrape_news, scrape_schedule): a queue in
#  one process's memory died with the command that filled
#  it, ~899 s before its first poll, so the largest
#  broadcasts never had a receipt read. Threads serialize on
#  a lock, processes on an flock beside the file. Two clocks
#  drain it: the first ticket a process queues arms one
#  daemon watcher thread there, which wakes every 15
#  minutes, trades what is due FOR EVERY PROCESS, and exits
#  once the file is empty; and `manage.py poll_push_receipts`
#  does the same pass for a cron line. An id Expo has no
#  verdict for yet, or whose slice failed, goes back for
#  another round (three at most) instead of being thrown
#  away. Where the file cannot be written the queue falls
#  back to this process's memory, as it always was —
#  receipts are diagnostics, not state.
#
#  Transport rules — one module-level requests.Session with
#  TWO retry policies, mounted by endpoint:
#    send     — connection errors only (a connection that
#               never opened reached nobody), NEVER a read
#               timeout or a 429/5xx: once the bytes are on
#               the wire Expo may already have enqueued the
#               slice, and a replay is a duplicate on every
#               phone in it. 5 s to connect, 30 s to read
#    receipts — idempotent, so the full policy: 3 retries
#               with backoff on 429/5xx and read errors,
#               Retry-After honoured but clamped to a few
#               seconds. 5 s to connect, 10 s to read
#  Nothing here runs on a request thread (chat, news and
#  admin hand the fan-out to a daemon thread; the scrapers
#  run under cron), which is what lets a send wait 30 s.
#  Slices of 100 (Expo's cap) go out from a bounded thread
#  pool under a 120 s fan-out deadline, behind a paced gate
#  that keeps the process under Expo's ~600 messages/s
#  ceiling. No Expo access token header (the Expo project
#  has to keep enhanced push security off). Failures are
#  logged and swallowed: push is best-effort everywhere and
#  never fails a request.
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
#      only an explicit enabled=0 suppresses (opt-out
#      model); data["channel"] is stamped on the payload.
#      All take optional title_en/body_en and route them to
#      tokens whose push_tokens.language is 'en'; without
#      them every device gets the Lithuanian text. The
#      channel name is checked against VALID_CHANNELS first
#      — an unknown name would ignore every opt-out, so it
#      sends nothing at all
#
#  Payload contract with the app: a tapped notification is
#  routed on data.type — "chat_message" (+ conversationId)
#  opens the room, "news" and "admin_announcement" open the
#  news tab. Every message carries channelId "default", the
#  Android channel the app registers.
############################################################


import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

# The cross-process lock on the receipt store — POSIX only; a
# platform without it keeps the thread lock alone
try:
    import fcntl
except ImportError:  # pragma: no cover — the backend runs on Linux
    fcntl = None

import requests
from requests.adapters import HTTPAdapter, Retry

from django.conf import settings
from django.db import close_old_connections, connection
from django.db.models import Exists, OuterRef

from knfapp.common.timestamps import utc_now
from knfapp.notifications.core import VALID_CHANNELS, token_digest
from knfapp.notifications.models import NotificationChannel, PushToken
from knfapp.users.models import Session

logger = logging.getLogger(__name__)

# One URL for both shapes: a single message object or an
# array of up to 100 of them
EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"

# Where a ticket id is traded for the delivery verdict
EXPO_RECEIPTS_URL = "https://exp.host/--/api/v2/push/getReceipts"

# Expo caps a send at 100 messages and a receipt query at
# 1000 ids; SQLite caps a statement at 999 variables, so the
# recipient chunk stays well under that
_SEND_SLICE = 100
_RECEIPT_SLICE = 300
_ID_CHUNK = 400

# (connect, read) per endpoint. A send may read for 30 s —
# Expo answers a 100-message slice slowly under load, and no
# send runs on a request thread (module banner); the receipt
# query is cheap and retried, so it folds sooner
_SEND_TIMEOUT = (5, 30)
_RECEIPT_TIMEOUT = (5, 10)

# The receipts policy honours Retry-After — clamped to this
# many seconds, so an upstream "come back in an hour" cannot
# park the watcher thread
_RETRY_AFTER_MAX = 5

# Broadcast fan-out: a few workers and a hard deadline, after
# which the remaining slices are logged and abandoned rather
# than holding a thread for minutes
_FANOUT_WORKERS = 3
_FANOUT_DEADLINE = 120

# ~600 messages/s is Expo's documented ceiling — 100 per
# slice no oftener than every 0.2 s leaves plenty of headroom
_SLICE_INTERVAL = 0.2
_pace_lock = threading.Lock()
_last_slice_at = 0.0

# Tickets waiting for their receipt: [ticket id, token,
# queued-at epoch seconds, tries] rows in a JSON file every
# process in the container shares (the module banner tells
# why), bounded — past the cap the oldest go first. A ticket
# is traded for its receipt once it is this old (s)
_RECEIPT_DELAY = 900
_RECEIPT_STORE_CAP = 20000

# The store's file name inside the temp dir, unless the
# PUSH_RECEIPT_STORE setting (or env var) names a path
_RECEIPT_STORE_NAME = "knfapp-push-receipts.json"

# An id Expo answered nothing for — or whose slice failed —
# rides again, this many rounds in all; an id older than
# this (s) is never asked about (Expo keeps receipts ~24 h)
_RECEIPT_MAX_TRIES = 3
_RECEIPT_MAX_AGE = 20 * 3600

# Threads of one process serialize the store on this — re-
# entrant, because the watcher's exit check holds it across
# a store read; processes serialize on an flock beside it
_receipt_lock = threading.RLock()

# The queue when the store file cannot be used: this
# process's memory, the pre-file behaviour; the flag keeps
# the warning to one line per process
_receipt_memory: list = []
_receipt_store_failed = False

# The lazily-armed per-process receipts watcher (see the
# module banner); the flag lives under _receipt_lock
_receipt_watcher_alive = False
_RECEIPT_POLL_INTERVAL = 900

# Anything token-shaped inside an upstream string is redacted
# before it reaches a log line
_TOKEN_PATTERN = re.compile(r"Expo(?:nent)?PushToken\[[^\]\r\n]*\]")








############################################################
# _build_session
############################################################
#
# The one requests.Session every Expo call shares —
# connection reuse — carrying a retry policy PER ENDPOINT,
# mounted on the two URL prefixes (requests picks the
# longest matching mount):
#
#   send     — Retry(connect=2, read=0, status=0, other=0):
#              a connection that never opened is tried
#              again (nothing reached Expo); a read timeout
#              or a 429/5xx is answered as it came. A POST
#              that timed out READING may already have
#              enqueued its slice — replaying it, as one
#              shared policy once did, delivered every
#              message of the slice up to four times and
#              then reported the slice as failed
#   receipts — a receipt query is idempotent, so the full
#              policy: total=3 (read, connect and status
#              retries alike), a growing backoff, 429 and
#              5xx retried, Retry-After honoured but clamped
#              to _RETRY_AFTER_MAX seconds
#
# POST is listed explicitly on both because urllib3 never
# retries a non-idempotent method on its own — on the send
# policy that keeps the counts, not the method rule, as the
# one thing deciding.
#
# Used by:
#   - _SESSION (below) — built once at import
############################################################

def _build_session() -> requests.Session:
    session = requests.Session()

    send_policy = Retry(
        total=2, connect=2, read=0, status=0, other=0,
        backoff_factor=0.5,
        allowed_methods=["POST"],
        respect_retry_after_header=False,
    )
    receipts_policy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["POST"],
        respect_retry_after_header=True,
        retry_after_max=_RETRY_AFTER_MAX,
    )
    session.mount(EXPO_PUSH_URL, HTTPAdapter(max_retries=send_policy))
    session.mount(EXPO_RECEIPTS_URL, HTTPAdapter(max_retries=receipts_policy))
    return session


# Module-level: one pool of connections for the whole process
_SESSION = _build_session()

# Both Expo endpoints want the same two headers
_EXPO_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
}








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
# _receipt_store_path
############################################################
#
# Where the shared receipt queue lives: the
# PUSH_RECEIPT_STORE setting, else the same-named env var,
# else a file in the temp dir — every process in the
# container (the server and each `docker exec` cron command)
# sees the same /tmp. Read per call, so a test can point it
# at its own file.
#
# Used by:
#   - _with_receipt_queue (below)
############################################################

def _receipt_store_path() -> str:
    configured = getattr(settings, "PUSH_RECEIPT_STORE", None) or os.environ.get("PUSH_RECEIPT_STORE")
    return configured or os.path.join(tempfile.gettempdir(), _RECEIPT_STORE_NAME)








############################################################
# _read_receipt_store
############################################################
#
# The store's rows, or [] for a missing or unreadable file
# (a torn write, a hand edit): a row that is not the
# [id, token, queued-at, tries] shape is dropped, never
# trusted. OSError alone escapes — that is the caller's cue
# to fall back to memory.
#
# Used by:
#   - _with_receipt_queue (below)
############################################################

def _read_receipt_store(path: str) -> list:
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return []
    except ValueError:
        logger.warning("Push receipt store unreadable — starting it afresh")
        return []
    if not isinstance(raw, list):
        return []
    return [
        [row[0], row[1], float(row[2]), int(row[3])]
        for row in raw
        if isinstance(row, list) and len(row) == 4
        and isinstance(row[0], str) and isinstance(row[1], str)
        and isinstance(row[2], (int, float)) and isinstance(row[3], int)
    ]








############################################################
# _write_receipt_store
############################################################
#
# Writes the rows through a temp file and an atomic rename,
# so a process killed mid-write leaves the previous store,
# never half of one.
#
# Used by:
#   - _with_receipt_queue (below)
############################################################

def _write_receipt_store(path: str, rows: list) -> None:
    scratch = f"{path}.tmp"
    with open(scratch, "w", encoding="utf-8") as fh:
        json.dump(rows, fh)
    os.replace(scratch, path)








############################################################
# _with_receipt_queue
############################################################
#
# Runs mutate(rows) on the queue under both locks — the
# thread lock, then an flock on "<store>.lock" — and writes
# the rows back, capped to the newest _RECEIPT_STORE_CAP.
# mutate edits the list in place and returns what the
# caller needs. When the file cannot be used the same call
# runs on this process's memory instead, with one warning
# per process — the queue degrades to what it always was,
# it never fails a send.
#
# Used by:
#   - _queue_receipts, _receipt_watcher,
#     pending_push_receipts, poll_push_receipts (below)
############################################################

def _with_receipt_queue(mutate):
    global _receipt_store_failed

    with _receipt_lock:
        path = _receipt_store_path()
        try:
            with open(f"{path}.lock", "a", encoding="utf-8") as lock_file:
                if fcntl is not None:
                    fcntl.flock(lock_file, fcntl.LOCK_EX)
                rows = _read_receipt_store(path)
                result = mutate(rows)
                _write_receipt_store(path, rows[-_RECEIPT_STORE_CAP:])
                return result
        except OSError:
            if not _receipt_store_failed:
                _receipt_store_failed = True
                logger.warning("Push receipt store %s unusable — queueing receipts in memory", path)
            result = mutate(_receipt_memory)
            del _receipt_memory[:-_RECEIPT_STORE_CAP]
            return result








############################################################
# _queue_receipts
############################################################
#
# Parks accepted tickets for the receipt poll — one store
# write per call, so a slice of 100 is one write, not a
# hundred — and arms this process's watcher when none is
# alive. Tickets without an id are skipped (nothing to ask
# Expo about).
#
# Used by:
#   - _park_receipts (below) — every sender's parking
############################################################

def _queue_receipts(accepted) -> None:
    global _receipt_watcher_alive

    now = time.time()
    fresh = [[ticket_id, token, now, 0] for ticket_id, token in accepted if ticket_id and token]
    if not fresh:
        return

    with _receipt_lock:
        _with_receipt_queue(lambda rows: rows.extend(fresh))
        if not _receipt_watcher_alive:
            _receipt_watcher_alive = True
            threading.Thread(target=_receipt_watcher, daemon=True,
                             name="expo-receipt-watcher").start()








############################################################
# _receipt_watcher
############################################################
#
# The per-process 15-minute clock: sleeps the poll interval,
# trades what is due — every process's tickets, the store is
# shared — and exits once the store is empty, clearing the
# flag under the same lock hold as the emptiness check, so a
# ticket queued meanwhile always finds a live watcher or arms
# a new one. A cron command's watcher dies with its process;
# its tickets stay in the store for the next clock.
#
# Used by:
#   - _queue_receipts (above) — armed on the first ticket
############################################################

def _receipt_watcher():
    global _receipt_watcher_alive

    try:
        while True:
            time.sleep(_RECEIPT_POLL_INTERVAL)
            try:
                checked = poll_push_receipts()
                if checked:
                    logger.info("Checked %d Expo push receipt(s)", checked)
            except Exception:
                logger.exception("Push receipt poll failed")
            finally:
                # The watcher's thread-local connection must not sit
                # open for the 15 quiet minutes between polls
                connection.close()

            with _receipt_lock:
                if _with_receipt_queue(len) == 0:
                    _receipt_watcher_alive = False
                    return
    except Exception:
        with _receipt_lock:
            _receipt_watcher_alive = False
        raise








############################################################
# pending_push_receipts
############################################################
#
# How many tickets still wait for a receipt, across every
# process — what the management command reports after its
# pass.
#
# Used by:
#   - notifications/management/commands/poll_push_receipts.py
############################################################

def pending_push_receipts() -> int:
    return _with_receipt_queue(len)








############################################################
# _park_receipts
############################################################
#
# _queue_receipts behind a guard: receipts are diagnostics,
# so nothing that goes wrong queueing them — a store bug, a
# full disk the memory fallback did not catch — may fail the
# send that produced them.
#
# Used by:
#   - send_push_notification, _send_slice (below)
############################################################

def _park_receipts(accepted) -> None:
    try:
        _queue_receipts(accepted)
    except Exception:
        logger.exception("Failed to queue push receipts")








############################################################
# send_push_notification
############################################################
#
# One message to one token, True only for an "ok" ticket.
# The STATUS is checked before the body is touched (an Expo
# 5xx answers with an HTML error page, which would fall
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


    # STEP 2: POST under the send policy (connect retries only —
    # a replay is a duplicate, see _build_session), status first
    # ==========================================================
    try:
        resp = _SESSION.post(
            EXPO_PUSH_URL,
            json=message,
            headers=_EXPO_HEADERS,
            timeout=_SEND_TIMEOUT,
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
    # off — .get on it would raise straight into the caller,
    # the one path in this module that would not swallow. The
    # batch sender guards the same shape
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

    _park_receipts([(ticket.get("id"), token)])
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


    # STEP 2: paced POST under the send policy (connect retries
    # only — a replay is a duplicate, see _build_session); a
    # non-200 or an exception costs this slice only
    # =========================================================
    try:
        _pace_slice()
        resp = _SESSION.post(
            EXPO_PUSH_URL,
            json=batch,
            headers=_EXPO_HEADERS,
            timeout=_SEND_TIMEOUT,
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
    accepted = []
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
                accepted.append((ticket.get("id"), token))
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


    # STEP 5: the accepted tickets wait for their receipts —
    # one store write for the whole slice
    # ======================================================
    _park_receipts(accepted)
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
# route) — the int return is the wire-facing value.
# priority/ttl ride into every message when given: the
# channel helpers set "high" + 1 h for chat and a day's ttl
# for the rest. The same `data` object rides in every message
# of the batch — fine, it is only serialised.
#
# Used by:
#   - _send_by_language (below) — every notify_* path
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
# revoked) — the WHOLE batch in one UPDATE. token is UNIQUE,
# so each name moves at most one row. Rows are kept, not
# deleted: the next POST /api/notifications/register from
# that device sets active=1 again. updated_at is refreshed
# so the row's age means something. Never raises.
#
# Two kinds of caller reach this helper, and they need the
# connection handled in opposite ways. A thread outside any
# transaction (a spawned push job, the receipt watcher)
# arrives with a connection that may have sat idle for
# minutes, so it recycles a stale one first and lets the
# ORM reconnect. A request thread arrives INSIDE its
# ATOMIC_REQUESTS transaction, where that same
# close_old_connections is fatal: it sees autocommit off
# against AUTOCOMMIT=True, drops the live connection, and
# the request's atomic block then rolls back silently while
# the view still answers 201 — a faculty announcement gone
# because one reader's Expo token was stale. The
# in_atomic_block check tells the two apart, so the helper
# is safe from whichever thread reaches it.
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
        # Only an idle thread may recycle its connection — in
        # a transaction the close would abort it (the banner)
        if not connection.in_atomic_block:
            close_old_connections()
        changed = PushToken.objects.filter(token__in=unique).update(active=0, updated_at=utc_now())
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
# Stage two of delivery: every ticket queued at least 15
# minutes ago — by ANY process, the store is shared — is
# traded for its receipt at Expo. A "DeviceNotRegistered"
# receipt retires the token — the uninstall case the ticket
# stage cannot see, because a ticket only says Expo took the
# message. "InvalidCredentials" (the Expo project's APNs/FCM
# credentials are broken, so NOTHING is arriving on any
# device) and "MessageRateExceeded" are logged at WARNING:
# they are operator problems, and without this job they are
# completely invisible.
#
# Expo answers only for ids it already has a verdict on — an
# id still in flight to APNs/FCM is simply absent — and a
# receipts request can fail outright. Neither is a reason to
# forget the ticket: both go back to the store for another
# round, up to _RECEIPT_MAX_TRIES rounds, and a ticket older
# than Expo's receipt retention is dropped unasked. Returns
# the number of verdicts read.
#
# Used by:
#   - _receipt_watcher (above) — the per-process 15-minute
#     clock
#   - notifications/management/commands/poll_push_receipts.py
#     — the cron pass
############################################################

def poll_push_receipts() -> int:
    # STEP 1: take what is due, drop what Expo no longer
    # keeps, leave the young in the store
    # ==================================================
    now = time.time()
    cutoff = now - _RECEIPT_DELAY
    too_old = now - _RECEIPT_MAX_AGE

    def take_due(rows):
        due, keep, expired = [], [], 0
        for row in rows:
            if row[2] <= too_old:
                expired += 1
            elif row[2] <= cutoff:
                due.append(row)
            else:
                keep.append(row)
        rows[:] = keep
        return due, expired

    due, expired = _with_receipt_queue(take_due)
    if expired:
        logger.info("Dropped %d push receipt(s) past Expo's retention unasked", expired)
    if not due:
        return 0


    # STEP 2: ask Expo in slices; a slice that fails
    # goes back to the store whole, for another round
    # ===============================================
    by_id = {row[0]: row for row in due}
    ids = list(by_id)
    checked = 0
    dead: list[str] = []
    retry: list = []

    for i in range(0, len(ids), _RECEIPT_SLICE):
        part = ids[i : i + _RECEIPT_SLICE]
        try:
            resp = _SESSION.post(
                EXPO_RECEIPTS_URL,
                json={"ids": part},
                headers=_EXPO_HEADERS,
                timeout=_RECEIPT_TIMEOUT,
            )
            if resp.status_code != 200:
                logger.warning("Expo receipts HTTP %d: %s", resp.status_code, _sanitize(resp.text))
                retry.extend(by_id[ticket_id] for ticket_id in part)
                continue
            receipts = resp.json().get("data")
        except Exception:
            logger.exception("Failed to fetch push receipts")
            retry.extend(by_id[ticket_id] for ticket_id in part)
            continue

        if not isinstance(receipts, dict):
            logger.warning("Expo receipts: unexpected body shape %s", _sanitize(receipts))
            retry.extend(by_id[ticket_id] for ticket_id in part)
            continue


        # STEP 3: verdict per ticket — retire dead devices,
        # shout about the operator-level failures; an id
        # Expo did not answer for is still in flight, and
        # rides again
        # =================================================
        for ticket_id in part:
            receipt = receipts.get(ticket_id)
            if receipt is None:
                retry.append(by_id[ticket_id])
                continue
            checked += 1
            if not isinstance(receipt, dict) or receipt.get("status") != "error":
                continue

            detail = receipt.get("details") if isinstance(receipt.get("details"), dict) else {}
            code = detail.get("error") or "Unknown"
            token = by_id[ticket_id][1]

            if code == "DeviceNotRegistered":
                dead.append(token)
            else:
                logger.warning(
                    "Expo receipt error %s for token:%s — %s",
                    code, token_digest(token) if token else "unknown",
                    _sanitize(receipt.get("message", "")),
                )


    # STEP 4: retire the dead in one UPDATE, then hand the
    # unanswered back — one try more each, the last round
    # dropped
    # ====================================================
    if dead:
        _deactivate_tokens(dead)

    again = [[ticket_id, token, stamped, tries + 1]
             for ticket_id, token, stamped, tries in retry if tries + 1 < _RECEIPT_MAX_TRIES]
    given_up = len(retry) - len(again)
    if again:
        _with_receipt_queue(lambda rows: rows.extend(again))

    logger.info(
        "Push receipts: %d checked, %d device(s) retired, %d re-queued, %d given up",
        checked, len(dead), len(again), given_up,
    )
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
# sessions.expires_at is a datetime column, so the cutoff is
# bound as a datetime — each engine compares in the column's
# own type, exactly what the session sweep does.
#
# Used by:
#   - scraper/management/commands/maintenance.py — daily
############################################################

def prune_orphan_push_tokens() -> int:
    try:
        removed, _ = PushToken.objects.exclude(
            user_id__in=Session.objects.filter(expires_at__gt=utc_now()).values("user_id"),
        ).delete()
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
# "channel"), split the (token, language) rows by language
# and make one send_push_batch call per language. Anything
# that is not 'en' rides the Lithuanian batch — 'lt' is the
# column default and the app default alike. A caller that
# passes NO English copy at all (title_en and body_en both
# None) gets ONE batch for every device: the split would send
# the same text twice over, and the single "Push batch" log
# line makes a forgotten translation visible. Either copy may
# be given alone — the other falls back to the Lithuanian. Delivery hints go
# per channel: a chat preview is worth waking a dozing phone
# for (priority "high", ttl 1 h, otherwise Android Doze can
# sit on an FCM normal-priority message for many minutes),
# while news/schedule/admin take the default priority and a
# day of ttl.
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

    # No English copy supplied: one text for every device, so
    # one batch — never two byte-identical round-trips
    if title_en is None and body_en is None:
        return send_push_batch([token for token, _language in rows], title, body, push_data,
                               priority=priority, ttl=ttl, stats=stats)

    lt_tokens = [token for token, language in rows if language != "en"]
    en_tokens = [token for token, language in rows if language == "en"]

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
# a per-recipient send would mean an Expo round-trip each.
# Users with an explicit notification_channels row enabled=0
# for the channel drop out in the query itself (opt-out
# model: a missing row means enabled). The id list is
# chunked so a huge recipient set cannot hit SQLite's
# variable limit. Returns accepted tickets (devices, not
# users); `stats` works as in send_push_batch.
#
# Used by:
#   - chat/api/views.py — the message push fan-out
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
    opted_out = NotificationChannel.objects.filter(
        user_id=OuterRef("user_id"), channel=channel, enabled=0,
    )
    rows = []
    for i in range(0, len(ids), _ID_CHUNK):
        part = ids[i : i + _ID_CHUNK]
        rows.extend(
            PushToken.objects.filter(active=1, user_id__in=part)
            .filter(~Exists(opted_out))
            .values_list("token", "language")
        )

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
# lives in the batched helper, so the two cannot drift apart.
#
# Used by:
#   - nothing at the moment — the chat fan-out takes the
#     batched shape
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
# never touched their settings are included (opt-out model).
# exclude_user_id, when set, is one more filter on top —
# the author of the action being announced stays quiet. An
# unknown channel name has no opt-out rows at all, which
# would silently mean "send to everyone", so it is refused
# here and logged instead. The count is devices, not users;
# "channel" is stamped on a copy of `data`. Devices
# registered with language 'en' get title_en/body_en when
# the caller supplies them — the scrapers do — and the
# Lithuanian text otherwise, as the admin broadcast does (an
# admin types one text, nothing translates it). `stats`
# works as in send_push_batch: pass a dict to learn how many
# slices failed, since the int return counts only what Expo
# accepted — plus stats["users"], the number of DISTINCT
# owners behind the targeted tokens.
#
# Used by:
#   - scraper/knf_scraper.py — scrape_knf_news, "news"
#   - scraper/vu_scraper.py — scrape_vu_news, "news"
#   - scraper/schedule_scraper.py — scrape_knf_schedule,
#     "schedule"
#   - news/api/views.py — _push_news_post, a public faculty
#     post, "news"
#   - admin/api/views.py — _run_broadcast, "admin"
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
    # Opt-out model: a user is excluded only by an explicit
    # enabled=0 row for this channel
    tokens = PushToken.objects.filter(active=1).filter(
        ~Exists(NotificationChannel.objects.filter(
            user_id=OuterRef("user_id"), channel=channel, enabled=0,
        )),
    )
    if exclude_user_id:
        tokens = tokens.exclude(user_id=exclude_user_id)

    rows = list(tokens.values_list("token", "language", "user_id"))

    # Tokens are devices, not people — one reader with nine
    # phones is nine tokens. The distinct-owner count rides in
    # stats so a broadcast can report reach honestly
    if stats is not None:
        stats["users"] = len({user_id for _token, _language, user_id in rows})

    return _send_by_language(channel, [(token, language) for token, language, _user_id in rows],
                             title, body, data, title_en, body_en, stats)
