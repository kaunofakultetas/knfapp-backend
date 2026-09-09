############################################################
#  [*] admin — the audit trail
#
#  Shape policy as in users/models.py.
#
#  Models:
#    - AdminAudit — one row per privileged mutation
#
#  Changes to these models require running:
#    python3 manage.py makemigrations
#    python3 manage.py migrate
############################################################


from django.db import models


from knfapp.users.models import User








# -----------------------------------------------------------
# AdminAudit
# -----------------------------------------------------------
#
# One row per privileged mutation (mint/revoke a code,
# role/active change, erasure, broadcast, report decision,
# tombstone restore): who, what, which target, and the
# action's context as a structured JSON payload (a dict in,
# the same dict back out). Written INSIDE the
# mutating request's transaction, so the trail cannot
# record an action that rolled back nor miss one that
# committed. SET_NULL on the actor — the trail outlives an
# erased admin account. Read over HTTP by GET
# /api/admin/audit.
#
# Table: admin_audit
# -----------------------------------------------------------

class AdminAudit(models.Model):
    # Columns — the FK's auto-index would duplicate
    # idx_admin_audit_actor below
    id = models.TextField(primary_key=True)
    actor = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                              db_column="actor_id", db_index=False, related_name="admin_actions")
    action = models.TextField()
    target = models.TextField(null=True, blank=True)
    payload = models.JSONField(null=True)
    created_at = models.DateTimeField()

    # Table metadata
    class Meta:
        db_table = "admin_audit"
        indexes = [
            models.Index(fields=["-created_at"], name="idx_admin_audit_created"),
            models.Index(fields=["actor"], name="idx_admin_audit_actor"),
        ]
