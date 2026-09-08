############################################################
#  [*] admin — the audit trail
#
#  One row per privileged mutation (mint/revoke a code,
#  role/active change, erasure, broadcast): who, what,
#  which target, and the action's context as JSON text.
#  Written INSIDE the mutating request's transaction, so
#  the trail cannot record an action that rolled back nor
#  miss one that committed. SET_NULL on the actor — the
#  trail outlives an erased admin account. Shape policy as
#  in users/models.py.
############################################################


from django.db import models


from knfapp.users.models import User


class AdminAudit(models.Model):
    id = models.TextField(primary_key=True)
    actor = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                              db_column="actor_id", related_name="admin_actions")
    action = models.TextField()
    target = models.TextField(null=True, blank=True)
    payload = models.TextField(null=True, blank=True)
    created_at = models.TextField()

    class Meta:
        db_table = "admin_audit"
        indexes = [
            models.Index(fields=["-created_at"], name="idx_admin_audit_created"),
            models.Index(fields=["actor"], name="idx_admin_audit_actor"),
        ]
