############################################################
#  [*] Notifications API — push tokens and channel switches
#
#  The client-facing half of Expo push: a device registers
#  its token after login and drops it on logout; a user
#  flips the four topic switches and the chat-preview
#  privacy flag. Delivery lives in the sender module
#  (notifications/push.py) — nothing here sends a push.
#
#  Split into:
#
#    register_token / unregister_token
#    get_channels / update_channels
#    get_chat_preview / update_chat_preview
############################################################


import logging
import re
import uuid


from knfapp.common import ratelimit
from knfapp.common.http import get_json_object, json_error, json_response, require_methods
from knfapp.common.timestamps import utc_now
from knfapp.notifications.core import VALID_CHANNELS, token_digest
from knfapp.notifications.models import NotificationChannel, PushToken
from knfapp.users.auth import require_auth
from knfapp.users.models import User


logger = logging.getLogger(__name__)

# The whole token grammar, not just the prefix — anything with
# a control character, a quote or markup in it was never a token
TOKEN_RE = re.compile(r"ExponentPushToken\[[A-Za-z0-9_-]{10,64}\]")

# A phone, a tablet, the odd reinstall — ten rows is generous.
# Past that the oldest go, so one account can never amplify a
# broadcast without bound
MAX_TOKENS_PER_USER = 10








############################################################
# register_token / unregister_token
############################################################
#
# POST body {"token", "platform"?, "language"?}: the full
# Expo grammar or a 400; a platform outside the whitelist is
# quietly stored 'unknown'; language falls back to 'lt'. One
# atomic upsert on the token's unique column covers insert,
# reactivate AND takeover (devices change hands — logged,
# never silent); the caller's fleet is trimmed to the cap
# first.
# Both outcomes answer {"registered": true, "tokenId"} — 200
# for a token the caller already held, 201 otherwise.
# DELETE removes the caller's OWN row only (a moved token is
# the same 404 as an unknown one), with no grammar check —
# an owner must be able to name any stored row, whatever
# its shape.
#
# Used by:
#   - the mobile engine's transport — register on login/
#     restore, detach on logout
############################################################

@require_methods("POST")
@require_auth
@ratelimit.per_user("push_register", max_attempts=20)
def register_token(request):
    # STEP 1: the body — shape, the full grammar, the whitelist
    # =========================================================
    data = get_json_object(request)
    if not data or not data.get("token"):
        return json_error("Push token required", 400)
    if not isinstance(data["token"], str):
        return json_error("Token must be a string", 400)

    token = data["token"].strip()
    if len(token) > 200:
        return json_error("Token too long", 400)
    if not TOKEN_RE.fullmatch(token):
        return json_error("Invalid Expo push token format", 400)

    platform = data.get("platform", "unknown")
    if platform not in ("ios", "android", "web", "unknown"):
        platform = "unknown"
    language = data.get("language")
    if language not in ("lt", "en"):
        language = "lt"

    user_id = request.user["id"]
    now = utc_now()


    # STEP 2: who holds this token today — that decides 200 vs
    # 201, and is where a device changing hands gets noticed
    # ========================================================
    existing = PushToken.objects.filter(token=token).values("id", "user_id").first()
    own = bool(existing) and existing["user_id"] == user_id
    if existing and not own:
        logger.warning("Push token reassigned from user %s to user %s (token:%s)",
                       existing["user_id"], user_id, token_digest(token))


    # STEP 3: keep the caller's fleet bounded — the rows longest
    # without a re-register go first
    # ==========================================================
    keep = list(
        PushToken.objects.filter(user_id=user_id).exclude(token=token)
        .order_by("-updated_at").values_list("id", flat=True)[:MAX_TOKENS_PER_USER - 1]
    )
    surplus, _ = PushToken.objects.filter(user_id=user_id).exclude(token=token).exclude(id__in=keep).delete()
    if surplus:
        logger.info("Dropped %d push token(s) over the cap for user %s", surplus, user_id)


    # STEP 4: one atomic upsert — insert, reactivate and reassign
    # are the same statement, so nothing interleaves; a token
    # collision rewrites owner, platform, language, active and
    # updated_at while id and created_at stay the row's own
    # ===========================================================
    PushToken.objects.bulk_create(
        [PushToken(id=str(uuid.uuid4()), user_id=user_id, token=token, platform=platform,
                   language=language, created_at=now, updated_at=now, active=True)],
        update_conflicts=True,
        unique_fields=["token"],
        update_fields=["user", "platform", "language", "active", "updated_at"],
    )


    # STEP 5: the conflict path keeps the original row id — it
    # goes out of the TABLE, never out of the fresh insert
    # ========================================================
    row = PushToken.objects.filter(token=token).values("id").first()
    if not row:
        logger.error("Push token vanished during registration (token:%s)", token_digest(token))
        return json_error("Could not register push token", 500)

    return json_response({"registered": True, "tokenId": row["id"]}, status=200 if own else 201)


@require_methods("DELETE")
@require_auth
@ratelimit.per_user("push_register", max_attempts=20)
def unregister_token(request):
    data = get_json_object(request)
    if not data or not data.get("token"):
        return json_error("Push token required", 400)
    if not isinstance(data["token"], str):
        return json_error("Token must be a string", 400)

    # No length or grammar cap here: a stored row outside the
    # POST's rules must still be nameable by its owner
    token = data["token"].strip()
    deleted, _ = PushToken.objects.filter(user_id=request.user["id"], token=token).delete()
    if deleted == 0:
        return json_error("Token not found", 404)
    return json_response({"unregistered": True})








############################################################
# get_channels / update_channels
############################################################
#
# The four topic switches on the opt-out model: the answer
# always carries all four keys, starting all-True with only
# existing rows overriding. PUT takes a partial dict —
# validated in FULL before the first write (a typo'd name
# is a 400 naming it, a non-boolean names the channel and
# the type it got), each entry upserted on the composite
# primary key under one transaction, and the response is
# the resulting full state — the confirmed truth a debounced
# batch of toggles settles on.
#
# Used by:
#   - the mobile engine's transport — the settings switches
############################################################

@require_methods("GET")
@require_auth
def get_channels(request):
    rows = NotificationChannel.objects.filter(user_id=request.user["id"]).values("channel", "enabled")
    channels = {ch: True for ch in VALID_CHANNELS}
    for row in rows:
        channels[row["channel"]] = bool(row["enabled"])
    return json_response({"channels": channels})


@require_methods("PUT")
@require_auth
@ratelimit.per_user("push_channels", max_attempts=60)
def update_channels(request):
    # STEP 1: shape, then every name and value before any write
    # =========================================================
    data = get_json_object(request)
    if not data or not isinstance(data.get("channels"), dict):
        return json_error("channels dict required", 400)

    channels_input = data["channels"]
    for channel, enabled in channels_input.items():
        if channel not in VALID_CHANNELS:
            return json_error(f"Unknown channel '{channel}'", 400)
        if not isinstance(enabled, bool):
            return json_error(
                f"Channel '{channel}' value must be a boolean (true/false), got {type(enabled).__name__}", 400)


    # STEP 2: upsert the listed rows — one transaction
    # (ATOMIC_REQUESTS), the composite PK deciding the conflict
    # =========================================================
    now = utc_now()
    for channel, enabled in channels_input.items():
        NotificationChannel.objects.update_or_create(
            user_id=request.user["id"], channel=channel,
            defaults={"enabled": enabled, "updated_at": now},
        )


    # STEP 3: the full resulting state, same shape as GET
    # ===================================================
    rows = NotificationChannel.objects.filter(user_id=request.user["id"]).values("channel", "enabled")
    result = {ch: True for ch in VALID_CHANNELS}
    for row in rows:
        result[row["channel"]] = bool(row["enabled"])
    return json_response({"channels": result})








############################################################
# get_chat_preview / update_chat_preview
############################################################
#
# The "show message text in notifications" privacy flag
# (users.chat_push_preview): enabled ships the first 100
# characters of a chat message as the push body, disabled
# sends the content-free copy so private text never leaves
# for the push processor at all. Separate from the channels
# dict on purpose — it is not a topic subscription.
#
# Used by:
#   - the mobile engine's transport — the settings toggle
############################################################

@require_methods("GET")
@require_auth
def get_chat_preview(request):
    row = User.objects.filter(id=request.user["id"]).values("chat_push_preview").first()
    return json_response({"enabled": bool(row["chat_push_preview"]) if row else True})


@require_methods("PUT")
@require_auth
@ratelimit.per_user("notif_prefs", max_attempts=30)
def update_chat_preview(request):
    data = get_json_object(request)
    if not data or not isinstance(data.get("enabled"), bool):
        return json_error("enabled must be a boolean", 400)

    User.objects.filter(id=request.user["id"]).update(
        chat_push_preview=data["enabled"],
    )
    return json_response({"enabled": data["enabled"]})
