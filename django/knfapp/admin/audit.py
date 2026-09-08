############################################################
#  [*] Audit helper — one row per privileged action
#
#  Appends an admin_audit row on the request's open
#  transaction (ATOMIC_REQUESTS), so a mutation and its
#  trail entry land together or not at all. NEVER raises:
#  an audit write must not be able to fail an admin action
#  — a database-level failure is logged and swallowed. The
#  INSERT rides its own savepoint for exactly that promise:
#  a swallowed error outside one would leave the request's
#  atomic block poisoned, and the real write — the reason
#  the handler ran — would fail at commit anyway.
#
#  Used by:
#    - api/views.py — every mutating handler
############################################################


import json
import logging
import uuid


from django.db import Error as DatabaseError, transaction


from knfapp.admin.models import AdminAudit
from knfapp.common.timestamps import utc_now_iso


logger = logging.getLogger(__name__)


def write_audit(actor_id, action, target=None, payload=None):
    try:
        with transaction.atomic():
            AdminAudit.objects.create(
                id=str(uuid.uuid4()),
                actor_id=actor_id,
                action=action,
                target=target,
                payload=json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                created_at=utc_now_iso(),
            )
    except DatabaseError:
        logger.warning("admin_audit unavailable — '%s' by %s went unrecorded", action, actor_id)
