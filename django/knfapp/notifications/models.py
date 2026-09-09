############################################################
#  [*] notifications — device registry and topic switches
#
#  The tables the push fan-out reads and the auth core
#  prunes: an expired session's purge and logout-all both
#  delete the owner's push rows (a device without a live
#  session must not keep getting message previews). Shape
#  policy as in users/models.py.
#
#  Models:
#    - PushToken           — one row per registered device
#    - NotificationChannel — per-topic opt-OUT switches
#
#  Changes to these models require running:
#    python3 manage.py makemigrations
#    python3 manage.py migrate
############################################################


from django.db import models


from knfapp.users.models import User


PLATFORMS = ("ios", "android", "web", "unknown")








# -----------------------------------------------------------
# PushToken
# -----------------------------------------------------------
#
# One row per registered push device. `active` gates the
# fan-outs; registering the same token again re-owns the
# row (a phone handed to another account must not keep the
# old owner's pushes).
#
# Table: push_tokens
# -----------------------------------------------------------

class PushToken(models.Model):
    # Columns — the FK's auto-index would duplicate
    # idx_push_tokens_user below
    id = models.TextField(primary_key=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, db_column="user_id",
                             db_index=False, related_name="push_tokens")
    token = models.TextField(unique=True)
    platform = models.TextField(default="unknown")
    # Per-device push copy language (the live schema's
    # column) — the sender picks lt/en per row
    language = models.TextField(default="lt")
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    # Table metadata
    class Meta:
        db_table = "push_tokens"
        constraints = [
            # The register view clamps to this set; the CHECK
            # holds any other writer to it too
            models.CheckConstraint(condition=models.Q(platform__in=PLATFORMS),
                                   name="push_tokens_platform_check"),
        ]
        indexes = [
            models.Index(fields=["user"], name="idx_push_tokens_user"),
            # The broadcast fan-out scans WHERE active = 1 joined
            # against the opt-outs — filter column first
            models.Index(fields=["active", "user"], name="idx_push_tokens_active"),
        ]








# -----------------------------------------------------------
# NotificationChannel
# -----------------------------------------------------------
#
# The four topic switches on the OPT-OUT model: a missing
# row means enabled, only an explicit enabled=False row
# silences a topic — so the reads start from all-True and
# lay the rows over it. Composite PK, as the live table has
# no surrogate id.
#
# Table: notification_channels
# -----------------------------------------------------------

class NotificationChannel(models.Model):
    # Columns — user_id leads the composite PK, so the FK's
    # auto-index would be a duplicate
    pk = models.CompositePrimaryKey("user_id", "channel")
    user = models.ForeignKey(User, on_delete=models.CASCADE, db_column="user_id",
                             db_index=False, related_name="notification_channels")
    channel = models.TextField()
    enabled = models.BooleanField(default=True)
    updated_at = models.DateTimeField()

    # Table metadata
    class Meta:
        db_table = "notification_channels"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(channel__in=("news", "chat", "schedule", "admin")),
                name="notification_channels_channel_check",
            ),
        ]
