############################################################
#  [*] notifications — push token registry (models only)
#
#  The one table the auth core already depends on: an
#  expired session's purge and logout-all both delete the
#  owner's push rows (a device that can no longer
#  authenticate must not keep getting message previews).
#  Shape matches the live schema (db_table/db_column, TEXT
#  ids and stamps) — see users/models.py for the policy.
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
    # Columns
    id = models.TextField(primary_key=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, db_column="user_id", related_name="push_tokens")
    token = models.TextField(unique=True)
    platform = models.TextField(default="unknown")
    # Per-device push copy language (the live schema's
    # column) — the sender picks lt/en per row
    language = models.TextField(default="lt")
    active = models.IntegerField(default=1)
    created_at = models.TextField()
    updated_at = models.TextField()

    # Table metadata
    class Meta:
        db_table = "push_tokens"
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
# row means enabled, only an explicit enabled=0 silences a
# topic — so the reads start from all-True and lay the rows
# over it. Composite PK, as the live table has no surrogate
# id.
#
# Table: notification_channels
# -----------------------------------------------------------

class NotificationChannel(models.Model):
    # Columns
    pk = models.CompositePrimaryKey("user_id", "channel")
    user = models.ForeignKey(User, on_delete=models.CASCADE, db_column="user_id",
                             related_name="notification_channels")
    channel = models.TextField()
    enabled = models.IntegerField(default=1)
    updated_at = models.TextField()

    # Table metadata
    class Meta:
        db_table = "notification_channels"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(channel__in=("news", "chat", "schedule", "admin")),
                name="notification_channels_channel_check",
            ),
        ]
