############################################################
#  [*] Ops API — the readiness probe
#
#  GET /api/health. Public on purpose: it carries no data,
#  and a probe that needs credentials is a probe nobody
#  wires up.
############################################################


import logging
import os


from django.conf import settings
from django.db import connection


from knfapp.common.http import json_response


logger = logging.getLogger(__name__)








############################################################
# health
############################################################
#
# GET /api/health
#
# Readiness, not liveness: the process answering is not
# enough — the database must answer a trivial query and the
# upload directory must still be writable (an avatar upload
# is the first thing a read-only mount breaks). The two
# published keys are {"status", "service"}; the per-check
# fields are additive. A failure answers 503 with the first
# reason.
#
# Used by:
#   - nothing calls this at the moment — no compose
#     healthcheck; it is described in swagger/swagger.yaml
#     and useful for a manual curl after a deploy
############################################################

def health(request):
    # STEP 1: the database must answer a trivial query
    # ================================================
    checks = {"database": "ok", "uploads": "ok"}
    reason = None

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception as exc:
        checks["database"] = "error"
        reason = f"database: {exc}"
        logger.exception("Health probe: database check failed")


    # STEP 2: uploads must still be writable
    # ======================================
    upload_dir = settings.UPLOAD_DIR
    if not os.access(upload_dir, os.W_OK):
        checks["uploads"] = "error"
        reason = reason or f"uploads: {upload_dir} is not writable"
        logger.warning("Health probe: %s is not writable", upload_dir)

    if reason:
        return json_response({"status": "error", "service": "knfapp-backend", "reason": reason, **checks},
                             status=503)

    return json_response({"status": "ok", "service": "knfapp-backend", **checks})
