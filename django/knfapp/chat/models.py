############################################################
#  [*] chat — the messaging tables
#
#  The five messaging tables. Wire stamps are naive UTC —
#  the encoder opted into by api/views.py decides that (see
#  common/http.py), not these columns. In-room search is one
#  case-insensitive substring match — the same query on
#  either engine, NOT the same case folding: SQLite's LIKE
#  folds ASCII only, PostgreSQL folds by collation, so a
#  capitalised Lithuanian diacritic misses only on SQLite.
#  Shape policy as in users/models.py.
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
MESSAGE_KINDS = ("text", "image", "file", "video", "audio", "system")








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
    # Sorted "id|id" of a direct chat's two members, NULL for
    # groups — the DATABASE settles a racing double-create on
    # both engines; a row loses its key when a member leaves,
    # so a recreate inserts fresh
    direct_key = models.TextField(null=True, blank=True, unique=True)
    # db_index=False on purpose — idx_conversations_created_by
    # below is the same index; the FK default would double it
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.DO_NOTHING,
                                   db_column="created_by", db_constraint=True, db_index=False,
                                   related_name="created_conversations")
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

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
    # Columns — both FKs carry db_index=False: conversation_id
    # leads the composite PK, user_id has the hand index below
    pk = models.CompositePrimaryKey("conversation_id", "user_id")
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, db_index=False,
                                     db_column="conversation_id", related_name="participants")
    user = models.ForeignKey(User, on_delete=models.CASCADE, db_index=False,
                             db_column="user_id", related_name="conversation_memberships")
    pinned = models.IntegerField(default=0)
    last_read_at = models.DateTimeField(null=True, blank=True)
    joined_at = models.DateTimeField()

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
# index; `kind` is server-computed and CHECK-enforced — the
# constraint is the only validation layer for it;
# `expires_at` is set on send in TTL rooms and swept by the
# disappearing-messages pass. `reply_to` is a LOOSE
# reference on purpose (db_constraint=False): the TTL sweep
# hard-deletes quoted messages under live replies — a real
# FK would fail that commit — and the read path already
# shapes a dangling ref as the deleted ghost quote.
#
# Table: messages
# -----------------------------------------------------------

class Message(models.Model):
    # Columns — the FK db_index=False flags drop Django's
    # auto-indexes where a hand index below already leads
    # with the same column
    id = models.TextField(primary_key=True)
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, db_index=False,
                                     db_column="conversation_id", related_name="messages")
    sender = models.ForeignKey(User, on_delete=models.CASCADE, db_index=False,
                               db_column="sender_id", related_name="sent_messages")
    text = models.TextField(default="")
    image_url = models.TextField(null=True, blank=True)
    reply_to = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL,
                                 db_column="reply_to_id", related_name="replies",
                                 db_constraint=False, db_index=False)
    deleted_at = models.DateTimeField(null=True, blank=True)
    client_msg_id = models.TextField(null=True, blank=True)
    kind = models.TextField(default="text")
    edited_at = models.DateTimeField(null=True, blank=True)
    attachment_url = models.TextField(null=True, blank=True)
    attachment_name = models.TextField(null=True, blank=True)
    attachment_size = models.IntegerField(null=True, blank=True)
    attachment_mime = models.TextField(null=True, blank=True)
    attachment_meta = models.JSONField(null=True, blank=True)
    link_preview = models.JSONField(null=True, blank=True)
    gallery = models.JSONField(null=True, blank=True)
    pinned_at = models.DateTimeField(null=True, blank=True)
    pinned_by = models.TextField(null=True, blank=True)
    forwarded = models.BooleanField(default=False)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField()

    # Table metadata
    class Meta:
        db_table = "messages"
        constraints = [
            # The idempotency index — a racing double-submit
            # loses here and is answered with the committed twin
            models.UniqueConstraint(fields=["conversation", "sender", "client_msg_id"],
                                    name="idx_messages_client_msg"),
            models.CheckConstraint(condition=models.Q(kind__in=MESSAGE_KINDS),
                                   name="messages_kind_check"),
        ]
        indexes = [
            # The paging index carries the FULL page order —
            # (created_at, id) — so a page is a pure index walk
            # with no sorter, and the per-room last-message
            # probe is one descending seek
            models.Index(fields=["conversation", "-created_at", "-id"], name="idx_messages_conversation"),
            models.Index(fields=["sender"], name="idx_messages_sender"),
            models.Index(fields=["reply_to"], name="idx_messages_reply_to"),
            # Disappearing messages: the hot sweep asks per
            # room (conversation_id equality + overdue range —
            # instantly empty for non-TTL rooms) and the daily
            # cross-room DISTINCT covers off the same partial;
            # only the expiring minority is indexed, so
            # ordinary sends pay nothing on write
            models.Index(fields=["conversation", "expires_at"], name="idx_messages_expires",
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
    # Columns — both FKs carry db_index=False: message_id
    # leads the composite PK, user_id has the hand index below
    pk = models.CompositePrimaryKey("message_id", "user_id")
    message = models.ForeignKey(Message, on_delete=models.CASCADE, db_index=False,
                                db_column="message_id", related_name="reads")
    user = models.ForeignKey(User, on_delete=models.CASCADE, db_index=False,
                             db_column="user_id", related_name="message_reads")
    read_at = models.DateTimeField()

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
    # Columns — both FKs carry db_index=False: message_id
    # leads the composite PK, user_id has the hand index below
    pk = models.CompositePrimaryKey("message_id", "user_id")
    message = models.ForeignKey(Message, on_delete=models.CASCADE, db_index=False,
                                db_column="message_id", related_name="reactions")
    user = models.ForeignKey(User, on_delete=models.CASCADE, db_index=False,
                             db_column="user_id", related_name="message_reactions")
    emoji = models.TextField()
    created_at = models.DateTimeField()

    # Table metadata
    class Meta:
        db_table = "message_reactions"
        indexes = [
            models.Index(fields=["user"], name="idx_message_reactions_user"),
        ]
