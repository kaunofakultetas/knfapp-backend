############################################################
#  [*] uploads — the ownership ledger
#
#  Shape matches the live schema — see users/models.py for
#  the policy.
#
#  Models:
#    - Upload — one claim ticket per stored file
#
#  Changes to these models require running:
#    python3 manage.py makemigrations
#    python3 manage.py migrate
############################################################


from django.db import models


from knfapp.users.models import User








# -----------------------------------------------------------
# Upload
# -----------------------------------------------------------
#
# One row per stored file: who uploaded it and how many
# bytes it holds — the two facts the 100 MB per-account
# quota and the owner-or-admin delete rule are built on.
# The FILE lives on disk under its uuid-hex name; this row
# is the claim ticket. SET_NULL on the owner: an erased
# account's files become ownerless (admin-reachable, swept
# when unreferenced) instead of vanishing mid-transaction.
#
# Table: uploads
# -----------------------------------------------------------

class Upload(models.Model):
    # Columns
    id = models.TextField(primary_key=True)
    filename = models.TextField(unique=True)
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                             db_column="user_id", related_name="uploads")
    byte_size = models.IntegerField(default=0)
    created_at = models.TextField()

    # Table metadata
    class Meta:
        db_table = "uploads"
        indexes = [models.Index(fields=["user"], name="idx_uploads_user")]
