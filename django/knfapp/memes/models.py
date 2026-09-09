############################################################
#  [*] memes — the shared library's ledger
#
#  Shape policy as in users/models.py.
#
#  Models:
#    - Meme — one row per shared library file
#
#  Changes to these models require running:
#    python3 manage.py makemigrations
#    python3 manage.py migrate
############################################################


from django.db import models


from knfapp.users.models import User








# -----------------------------------------------------------
# Meme
# -----------------------------------------------------------
#
# One row per library file: the stored name, the title and
# tags people search by, and the folded `search` haystack
# (lowercase, Lithuanian diacritics to base letters —
# SQLite's lower() folds ASCII only, so 'ačiū' typed
# against 'AČIŪ' finds nothing without it). Files live in
# MEMES_DIR, a SEPARATE tree from the per-user uploads:
# shared lifecycle, no per-user quota, never touched by an
# unsend.
#
# Table: memes
# -----------------------------------------------------------

class Meme(models.Model):
    # Columns
    id = models.TextField(primary_key=True)
    filename = models.TextField(unique=True)
    title = models.TextField()
    tags = models.TextField(null=True, blank=True)
    added_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                 db_column="added_by", related_name="memes")
    byte_size = models.IntegerField(default=0)
    width = models.IntegerField(null=True, blank=True)
    height = models.IntegerField(null=True, blank=True)
    preview = models.TextField(null=True, blank=True)
    search = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField()

    # Table metadata
    class Meta:
        db_table = "memes"
        indexes = [
            # The library lists newest first
            models.Index(fields=["-created_at"], name="idx_memes_created"),
        ]
