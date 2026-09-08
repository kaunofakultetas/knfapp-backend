############################################################
#  [*] chat — the messaging tables
#
#  Five tables mirroring the live schema byte-for-byte:
#  conversations (direct/group + the disappearing-messages
#  TTL), the membership rows (composite PK, per-user pin +
#  last_read_at watermark), messages (soft-deleted by
#  deleted_at; client_msg_id is the idempotency nonce under
#  its unique index; kind/attachment_*/gallery/
#  link_preview carry the richer message shapes), and the
#  two composite-PK side tables: read receipts and
#  reactions. The messages_fts FTS5 shadow table is NOT a
#  model — it rides migration 0002 with a probe-create, so
#  an SQLite built without FTS5 degrades the in-room search
#  to its LIKE fallback instead of failing the migrate.
#  Shape policy as in users/models.py.
############################################################


from django.db import models


from knfapp.users.models import User


CONVERSATION_TYPES = ("direct", "group")


class Conversation(models.Model):
    id = models.TextField(primary_key=True)
    type = models.TextField(default="direct")
    title = models.TextField(null=True, blank=True)
    avatar_emoji = models.TextField(null=True, blank=True)
    message_ttl_seconds = models.IntegerField(null=True, blank=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.DO_NOTHING,
                                   db_column="created_by", db_constraint=True,
                                   related_name="created_conversations")
    created_at = models.TextField()
    updated_at = models.TextField()

    class Meta:
        db_table = "conversations"
        constraints = [
            models.CheckConstraint(condition=models.Q(type__in=CONVERSATION_TYPES),
                                   name="conversations_type_check"),
        ]
        indexes = [
            models.Index(fields=["created_by"], name="idx_conversations_created_by"),
        ]


class ConversationParticipant(models.Model):
    pk = models.CompositePrimaryKey("conversation_id", "user_id")
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE,
                                     db_column="conversation_id", related_name="participants")
    user = models.ForeignKey(User, on_delete=models.CASCADE,
                             db_column="user_id", related_name="conversation_memberships")
    pinned = models.IntegerField(default=0)
    last_read_at = models.TextField(null=True, blank=True)
    joined_at = models.TextField()

    class Meta:
        db_table = "conversation_participants"
        indexes = [
            models.Index(fields=["user"], name="idx_conv_participants_user"),
        ]


class Message(models.Model):
    id = models.TextField(primary_key=True)
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE,
                                     db_column="conversation_id", related_name="messages")
    sender = models.ForeignKey(User, on_delete=models.CASCADE,
                               db_column="sender_id", related_name="sent_messages")
    text = models.TextField(default="")
    image_url = models.TextField(null=True, blank=True)
    reply_to = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL,
                                 db_column="reply_to_id", related_name="replies")
    deleted_at = models.TextField(null=True, blank=True)
    client_msg_id = models.TextField(null=True, blank=True)
    kind = models.TextField(default="text")
    edited_at = models.TextField(null=True, blank=True)
    attachment_url = models.TextField(null=True, blank=True)
    attachment_name = models.TextField(null=True, blank=True)
    attachment_size = models.IntegerField(null=True, blank=True)
    attachment_mime = models.TextField(null=True, blank=True)
    attachment_meta = models.TextField(null=True, blank=True)
    link_preview = models.TextField(null=True, blank=True)
    gallery = models.TextField(null=True, blank=True)
    pinned_at = models.TextField(null=True, blank=True)
    pinned_by = models.TextField(null=True, blank=True)
    forwarded = models.IntegerField(default=0)
    expires_at = models.TextField(null=True, blank=True)
    created_at = models.TextField()

    class Meta:
        db_table = "messages"
        constraints = [
            # The idempotency index — a racing double-submit
            # loses here and is answered with the committed twin
            models.UniqueConstraint(fields=["conversation", "sender", "client_msg_id"],
                                    name="idx_messages_client_msg"),
        ]
        indexes = [
            models.Index(fields=["conversation", "-created_at"], name="idx_messages_conversation"),
            models.Index(fields=["sender"], name="idx_messages_sender"),
            models.Index(fields=["reply_to"], name="idx_messages_reply_to"),
            # Disappearing messages: the sweeps ask for overdue
            # rows across ALL conversations — partial, so the
            # index holds only the tiny expiring minority and
            # ordinary messages pay nothing on write
            models.Index(fields=["expires_at"], name="idx_messages_expires",
                         condition=models.Q(expires_at__isnull=False)),
        ]


class MessageRead(models.Model):
    pk = models.CompositePrimaryKey("message_id", "user_id")
    message = models.ForeignKey(Message, on_delete=models.CASCADE,
                                db_column="message_id", related_name="reads")
    user = models.ForeignKey(User, on_delete=models.CASCADE,
                             db_column="user_id", related_name="message_reads")
    read_at = models.TextField()

    class Meta:
        db_table = "message_reads"
        indexes = [
            models.Index(fields=["user"], name="idx_message_reads_user"),
        ]


class MessageReaction(models.Model):
    pk = models.CompositePrimaryKey("message_id", "user_id")
    message = models.ForeignKey(Message, on_delete=models.CASCADE,
                                db_column="message_id", related_name="reactions")
    user = models.ForeignKey(User, on_delete=models.CASCADE,
                             db_column="user_id", related_name="message_reactions")
    emoji = models.TextField()
    created_at = models.TextField()

    class Meta:
        db_table = "message_reactions"
        indexes = [
            models.Index(fields=["user"], name="idx_message_reactions_user"),
        ]
