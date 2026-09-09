############################################################
#  [*] social — friendships, requests, blocks, activity,
#      reports
#
#  friendships and user_blocks carry composite primary keys
#  (Django 5.2 CompositePrimaryKey — the live tables have
#  no surrogate id). Shape policy as in users/models.py.
#
#  Models:
#    - Friendship    — accepted pairs, one row per direction
#    - Activity      — the "X liked your post" list
#    - FriendRequest — the pending → accepted/rejected
#                      handshake
#    - UserBlock     — one row blocker→blocked
#    - Report        — the complaint ledger
#
#  Changes to these models require running:
#    python3 manage.py makemigrations
#    python3 manage.py migrate
############################################################


from django.db import models


from knfapp.users.models import User


ACTIVITY_KINDS = ("like", "comment", "connect_request", "connect_accept")








# -----------------------------------------------------------
# Friendship
# -----------------------------------------------------------
#
# The table the news feed's visibility contract depends on:
# a private wall post shows to friends only. Written in
# BOTH directions on accept, so one direction is enough to
# check anywhere.
#
# Table: friendships
# -----------------------------------------------------------

class Friendship(models.Model):
    # Columns — user_id leads the composite PK, so its FK
    # auto-index would be a duplicate; friend_id keeps its
    # index for the reverse-direction lookups
    pk = models.CompositePrimaryKey("user_id", "friend_id")
    user = models.ForeignKey(User, on_delete=models.CASCADE, db_column="user_id",
                             db_index=False, related_name="friendships")
    friend = models.ForeignKey(User, on_delete=models.CASCADE, db_column="friend_id", related_name="friend_of")
    created_at = models.DateTimeField()

    # Table metadata
    class Meta:
        db_table = "friendships"
        constraints = [
            # Self-friendship is refused in the views; the CHECK
            # makes it impossible for any writer
            models.CheckConstraint(condition=~models.Q(user=models.F("friend")),
                                   name="friendships_not_self_check"),
        ]








# -----------------------------------------------------------
# Activity
# -----------------------------------------------------------
#
# The per-user notification list a like, comment or friend
# event writes into. The unique row over (user, kind,
# actor, subject) makes the writers idempotent — un-like
# and re-like is one notification, not two. `read` drives
# the tab badge.
#
# Table: activity
# -----------------------------------------------------------

class Activity(models.Model):
    # Columns — user_id's FK auto-index would duplicate the
    # two hand indexes below; actor_id keeps its own (the
    # erasure delete and nothing else lead with it)
    id = models.TextField(primary_key=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, db_column="user_id",
                             db_index=False, related_name="activity")
    kind = models.TextField()
    actor = models.ForeignKey(User, on_delete=models.CASCADE, db_column="actor_id", related_name="acted_activity")
    subject_id = models.TextField(null=True, blank=True)
    subject_preview = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField()
    read = models.BooleanField(default=False)

    # Table metadata
    class Meta:
        db_table = "activity"
        constraints = [
            models.CheckConstraint(condition=models.Q(kind__in=ACTIVITY_KINDS), name="activity_kind_check"),
            models.UniqueConstraint(fields=["user", "kind", "actor", "subject_id"], name="activity_unique_row"),
            # NULLs are distinct to SQLite, so the quadruple
            # unique above cannot bind the subject-less rows
            # (connect_accept) — this partial index does
            models.UniqueConstraint(fields=["user", "kind", "actor"],
                                    condition=models.Q(subject_id__isnull=True),
                                    name="activity_unique_null_subject"),
        ]
        indexes = [
            models.Index(fields=["user", "-created_at", "-id"], name="idx_activity_user"),
            # The tab badge counts WHERE user_id = ? AND read = 0
            # on every app focus
            models.Index(fields=["user", "read"], name="idx_activity_unread"),
        ]


REQUEST_STATUSES = ("pending", "accepted", "rejected")
REPORT_TARGET_TYPES = ("user", "post", "message")
REPORT_STATUSES = ("open", "resolved")








# -----------------------------------------------------------
# FriendRequest
# -----------------------------------------------------------
#
# The handshake (pending → accepted/rejected). An accept
# DELETES the row — the two friendships rows are the state
# of record — and 'rejected' rows live only as long as the
# re-ask cooldown. The partial unique index on the pending
# pair settles a mutual-send race as a 409, never two rows.
#
# Table: friend_requests
# -----------------------------------------------------------

class FriendRequest(models.Model):
    # Columns — both FK auto-indexes would duplicate the two
    # hand composites below, which lead with the same columns
    id = models.TextField(primary_key=True)
    from_user = models.ForeignKey(User, on_delete=models.CASCADE, db_column="from_user_id",
                                  db_index=False, related_name="sent_friend_requests")
    to_user = models.ForeignKey(User, on_delete=models.CASCADE, db_column="to_user_id",
                                db_index=False, related_name="received_friend_requests")
    status = models.TextField(default="pending")
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    # Table metadata
    class Meta:
        db_table = "friend_requests"
        constraints = [
            models.CheckConstraint(condition=models.Q(status__in=REQUEST_STATUSES),
                                   name="friend_requests_status_check"),
            # Self-requests are refused in the views; the CHECK
            # makes them impossible for any writer
            models.CheckConstraint(condition=~models.Q(from_user=models.F("to_user")),
                                   name="friend_requests_not_self_check"),
            # The partial unique index: ONE pending row per
            # directed pair — the guard that settles a lost race
            models.UniqueConstraint(fields=["from_user", "to_user"],
                                    condition=models.Q(status="pending"),
                                    name="idx_friend_requests_pending"),
        ]
        indexes = [
            models.Index(fields=["to_user", "status"], name="idx_friend_requests_to"),
            models.Index(fields=["from_user", "status"], name="idx_friend_requests_from"),
        ]








# -----------------------------------------------------------
# UserBlock
# -----------------------------------------------------------
#
# One row blocker→blocked, bidirectional in EFFECT at every
# enforcement site (either direction hides both parties
# from each other). Blocking severs the friendship and any
# pending requests in the same transaction.
#
# Table: user_blocks
# -----------------------------------------------------------

class UserBlock(models.Model):
    # Columns — blocker_id leads the composite PK and
    # blocked_id has the hand index below, so both FK
    # auto-indexes would be duplicates
    pk = models.CompositePrimaryKey("blocker_id", "blocked_id")
    blocker = models.ForeignKey(User, on_delete=models.CASCADE, db_column="blocker_id",
                                db_index=False, related_name="blocks_made")
    blocked = models.ForeignKey(User, on_delete=models.CASCADE, db_column="blocked_id",
                                db_index=False, related_name="blocks_received")
    created_at = models.DateTimeField()

    # Table metadata
    class Meta:
        db_table = "user_blocks"
        constraints = [
            # Self-blocks are refused in the views; the CHECK
            # makes them impossible for any writer
            models.CheckConstraint(condition=~models.Q(blocker=models.F("blocked")),
                                   name="user_blocks_not_self_check"),
        ]
        indexes = [models.Index(fields=["blocked"], name="idx_user_blocks_blocked")]








# -----------------------------------------------------------
# Report
# -----------------------------------------------------------
#
# The complaint ledger the admin queue reads: a polymorphic
# target (`target_type` names the table, `target_id` is a
# loose reference on purpose — a deleted target must not
# take the complaint down with it), the reporter's words,
# and the open/resolved state the moderators flip.
#
# Table: reports
# -----------------------------------------------------------

class Report(models.Model):
    # Columns
    id = models.TextField(primary_key=True)
    reporter = models.ForeignKey(User, on_delete=models.CASCADE, db_column="reporter_id",
                                 related_name="reports")
    target_type = models.TextField()
    target_id = models.TextField()
    reason = models.TextField()
    status = models.TextField(default="open")
    created_at = models.DateTimeField()

    # Table metadata
    class Meta:
        db_table = "reports"
        constraints = [
            models.CheckConstraint(condition=models.Q(target_type__in=REPORT_TARGET_TYPES),
                                   name="reports_target_type_check"),
            models.CheckConstraint(condition=models.Q(status__in=REPORT_STATUSES),
                                   name="reports_status_check"),
        ]
        indexes = [
            # The admin queue lists one status, newest first
            models.Index(fields=["status", "-created_at"], name="idx_reports_status"),
        ]
