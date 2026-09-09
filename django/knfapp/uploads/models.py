############################################################
#  [*] uploads — the ownership ledger
#
#  Shape policy as in users/models.py.
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
    # Columns — the FK's auto-index would duplicate
    # idx_uploads_user below
    id = models.TextField(primary_key=True)
    filename = models.TextField(unique=True)
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                             db_column="user_id", db_index=False, related_name="uploads")
    byte_size = models.IntegerField(default=0)
    created_at = models.DateTimeField()

    # Table metadata
    class Meta:
        db_table = "uploads"
        indexes = [
            models.Index(fields=["user"], name="idx_uploads_user"),
            # The admin listing shows newest first
            models.Index(fields=["-created_at"], name="idx_uploads_created"),
        ]
