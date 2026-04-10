"""Expo Push Notification helper.

Calls https://exp.host/--/api/v2/push/send to deliver push notifications
through Expo's push service.
"""

import logging
from typing import Optional

import requests

from app.database import get_db

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"


def send_push_notification(
    token: str,
    title: str,
    body: str,
    data: Optional[dict] = None,
    badge: Optional[int] = None,
) -> bool:
    """Send a single push notification via Expo Push API.

    Returns True if accepted by Expo, False otherwise.
    """
    message = {
        "to": token,
        "title": title,
        "body": body,
        "sound": "default",
    }
    if data:
        message["data"] = data
    if badge is not None:
        message["badge"] = badge

    try:
        resp = requests.post(
            EXPO_PUSH_URL,
            json=message,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        result = resp.json()
        # Expo returns {"data": {"status": "ok"}} on success
        if resp.status_code == 200:
            status = result.get("data", {}).get("status")
            if status == "error":
                detail = result.get("data", {}).get("details", {})
                error_type = detail.get("error")
                if error_type == "DeviceNotRegistered":
                    _deactivate_token(token)
                    logger.info("Deactivated unregistered token: %s...", token[:20])
                else:
                    logger.warning("Expo push error: %s", result)
                return False
            return True
        logger.warning("Expo push HTTP %d: %s", resp.status_code, resp.text[:200])
        return False
    except Exception:
        logger.exception("Failed to send push notification")
        return False


def send_push_batch(
    tokens: list[str],
    title: str,
    body: str,
    data: Optional[dict] = None,
) -> int:
    """Send push notification to multiple tokens in batches of 100.

    Returns count of successfully accepted notifications.
    """
    if not tokens:
        return 0

    messages = []
    for token in tokens:
        msg = {
            "to": token,
            "title": title,
            "body": body,
            "sound": "default",
        }
        if data:
            msg["data"] = data
        messages.append(msg)

    sent = 0
    # Expo accepts batches of up to 100
    for i in range(0, len(messages), 100):
        batch = messages[i : i + 100]
        try:
            resp = requests.post(
                EXPO_PUSH_URL,
                json=batch,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                timeout=30,
            )
            if resp.status_code == 200:
                results = resp.json().get("data", [])
                for idx, r in enumerate(results):
                    if r.get("status") == "ok":
                        sent += 1
                    elif r.get("status") == "error":
                        detail = r.get("details", {})
                        if detail.get("error") == "DeviceNotRegistered":
                            _deactivate_token(batch[idx]["to"])
            else:
                logger.warning("Expo batch push HTTP %d", resp.status_code)
        except Exception:
            logger.exception("Failed to send push batch")

    logger.info("Push batch: %d/%d accepted", sent, len(tokens))
    return sent


def _deactivate_token(token: str):
    """Mark a push token as inactive (device unregistered)."""
    try:
        db = get_db()
        try:
            db.execute(
                "UPDATE push_tokens SET active = 0 WHERE token = ?",
                (token,),
            )
            db.commit()
        finally:
            db.close()
    except Exception:
        logger.exception("Failed to deactivate token")


def notify_user(user_id: str, title: str, body: str, data: Optional[dict] = None) -> int:
    """Send push notification to all active devices of a user.

    Returns count of successfully sent notifications.
    """
    db = get_db()
    try:
        rows = db.execute(
            "SELECT token FROM push_tokens WHERE user_id = ? AND active = 1",
            (user_id,),
        ).fetchall()
    finally:
        db.close()

    tokens = [r["token"] for r in rows]
    if not tokens:
        return 0

    return send_push_batch(tokens, title, body, data)


def notify_all_users(title: str, body: str, data: Optional[dict] = None, exclude_user_id: Optional[str] = None) -> int:
    """Send push notification to all users with active tokens.

    Returns count of successfully sent notifications.
    """
    db = get_db()
    try:
        if exclude_user_id:
            rows = db.execute(
                "SELECT DISTINCT token FROM push_tokens WHERE active = 1 AND user_id != ?",
                (exclude_user_id,),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT DISTINCT token FROM push_tokens WHERE active = 1"
            ).fetchall()
    finally:
        db.close()

    tokens = [r["token"] for r in rows]
    if not tokens:
        return 0

    return send_push_batch(tokens, title, body, data)


def notify_channel(channel: str, title: str, body: str, data: Optional[dict] = None, exclude_user_id: Optional[str] = None) -> int:
    """Send push notification only to users subscribed to a specific channel.

    Users who haven't set any preference default to enabled (opt-out model).
    Returns count of successfully sent notifications.
    """
    db = get_db()
    try:
        # Get tokens for users who have the channel enabled (or no explicit preference, which defaults to enabled)
        query = """
            SELECT DISTINCT pt.token
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

    tokens = [r["token"] for r in rows]
    if not tokens:
        return 0

    # Inject channel info into data so the client knows the topic
    push_data = dict(data or {})
    push_data["channel"] = channel

    return send_push_batch(tokens, title, body, push_data)
