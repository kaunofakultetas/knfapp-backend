############################################################
#  [*] chat — the messaging tables
#
#  Five tables mirroring the live schema byte-for-byte. The
#  messages_fts FTS5 shadow table is NOT a model — it rides
#  migration 0002 with a probe-create, so an SQLite built
#  without FTS5 degrades the in-room search to its LIKE
#  fallback instead of failing the migrate. Chat stamps are
#  NAIVE-UTC isoformat text (no offset), compared as
#  strings. Shape policy as in users/models.py.
#
#  Models:
#    - Conversation            — direct/group rooms + the TTL
#    - ConversationParticipant — membership, pin, watermark
#    - Message                 — the message rows, all kinds
#    - MessageRead             — per-message read receipts
#    - MessageReaction         — one emoji per (message, user)
#
#  Changes to these models require running:
#    python3 manage.py makemigrations
#    python3 manage.py migrate
############################################################


from django.db import models


from knfapp.users.models import User


CONVERSATION_TYPES = ("direct", "group")








# -----------------------------------------------------------
# Conversation
# -----------------------------------------------------------
#
# One room per row: 'direct' pairs (title NULL — the peer's
# name is the title) and 'group' rooms with their own title
# and emoji avatar. `message_ttl_seconds` is the
# disappearing-messages setting new sends inherit.
#
# Table: conversations
# -----------------------------------------------------------

class Conversation(models.Model):
    # Columns
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

    # Table metadata
    class Meta:
        db_table = "conversations"
        constraints = [
            models.CheckConstraint(condition=models.Q(type__in=CONVERSATION_TYPES),
                                   name="conversations_type_check"),
        ]
        indexes = [
            models.Index(fields=["created_by"], name="idx_conversations_created_by"),
        ]








# -----------------------------------------------------------
# ConversationParticipant
# -----------------------------------------------------------
#
# The membership rows every chat read is scoped by:
# composite PK over the pair, a per-user pin flag, and
# `last_read_at` — the watermark the unread counts compare
# message stamps against (chat's read model number one; the
# per-message receipts below are number two).
#
# Table: conversation_participants
# -----------------------------------------------------------

class ConversationParticipant(models.Model):
    # Columns
    pk = models.CompositePrimaryKey("conversation_id", "user_id")
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE,
                                     db_column="conversation_id", related_name="participants")
    user = models.ForeignKey(User, on_delete=models.CASCADE,
                             db_column="user_id", related_name="conversation_memberships")
    pinned = models.IntegerField(default=0)
    last_read_at = models.TextField(null=True, blank=True)
    joined_at = models.TextField()

    # Table metadata
    class Meta:
        db_table = "conversation_participants"
        indexes = [
            models.Index(fields=["user"], name="idx_conv_participants_user"),
        ]








# -----------------------------------------------------------
# Message
# -----------------------------------------------------------
#
# The message rows, every kind in one table: text, image,
# attachment (the attachment_* columns), gallery and link
# previews as JSON text. Soft-deleted by `deleted_at` (an
# unsend keeps the row, blanks the content at read time);
# `client_msg_id` is the idempotency nonce under its unique
# index; `expires_at` is set on send in TTL rooms and swept
# by the disappearing-messages pass.
#
# Table: messages
# -----------------------------------------------------------

class Message(models.Model):
    # Columns
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

    # Table metadata
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








# -----------------------------------------------------------
# MessageRead
# -----------------------------------------------------------
#
# Per-message read receipts (chat's read model number two —
# the membership watermark above is number one): one row
# per (message, user), written when a reader confirms a
# specific message.
#
# Table: message_reads
# -----------------------------------------------------------

class MessageRead(models.Model):
    # Columns
    pk = models.CompositePrimaryKey("message_id", "user_id")
    message = models.ForeignKey(Message, on_delete=models.CASCADE,
                                db_column="message_id", related_name="reads")
    user = models.ForeignKey(User, on_delete=models.CASCADE,
                             db_column="user_id", related_name="message_reads")
    read_at = models.TextField()

    # Table metadata
    class Meta:
        db_table = "message_reads"
        indexes = [
            models.Index(fields=["user"], name="idx_message_reads_user"),
        ]








# -----------------------------------------------------------
# MessageReaction
# -----------------------------------------------------------
#
# One emoji per (message, user) — the composite PK enforces
# it; changing the emoji rewrites the row rather than
# adding a second.
#
# Table: message_reactions
# -----------------------------------------------------------

class MessageReaction(models.Model):
    # Columns
    pk = models.CompositePrimaryKey("message_id", "user_id")
    message = models.ForeignKey(Message, on_delete=models.CASCADE,
                                db_column="message_id", related_name="reactions")
    user = models.ForeignKey(User, on_delete=models.CASCADE,
                             db_column="user_id", related_name="message_reactions")
    emoji = models.TextField()
    created_at = models.TextField()

    # Table metadata
    class Meta:
        db_table = "message_reactions"
        indexes = [
            models.Index(fields=["user"], name="idx_message_reactions_user"),
        ]
