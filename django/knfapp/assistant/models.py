############################################################
#  [*] assistant — the AI support agent's own tables
#
#  Shape policy as in users/models.py. Four tables, two
#  concerns:
#
#  The knowledge base — SupportChunk, the pgvector semantic
#  index over content that LIVES elsewhere (faculty_info,
#  news_posts): existing tables hold the truth, these rows
#  hold the searchable copy and point back through
#  source/source_id. Written only by the indexing command.
#
#  The conversations — AssistantThread + AssistantMessage
#  (the stored transcript, one full AI SDK UIMessage per
#  row, jauka's ChatSession/ChatMessage adapted) and
#  AssistantTurn (per-turn usage telemetry that OUTLIVES a
#  deleted transcript). Written only through the internal
#  API the assistant container calls — deliberately
#  separate from chat/models.py: the unit of storage here
#  is a UIMessage with tool parts, not a chat Message row.
#
#  The prompt store — AssistantPrompt, jauka's versioned
#  prompt sets adapted to this agent's split: the
#  contract-coupled CORE prompt (tool names, citation
#  numbering, injection rules) lives in the container's
#  code under git, and these rows are the admin-editable
#  APPENDIX laid under it — every save is a NEW immutable
#  version, at most one active, and every chat turn stamps
#  the version it ran with into its telemetry row.
#
#  Changes to these models require running:
#    python3 manage.py makemigrations
#    python3 manage.py migrate
############################################################


import uuid

from django.db import models
from pgvector.django import VectorField

from knfapp.users.models import User


# The closed source set of the knowledge base — a chunk row
# always points back at the table its text came from
# ("curated" rows come from the repo-owned extras in
# curated_faq.py, not from a database table)
CHUNK_SOURCES = ("handbook", "news", "curated")

# The two languages the corpus and the threads carry — the
# same pair the handbook and the app's i18n speak
LANGUAGES = ("lt", "en")

# How a turn ended — mirrors the container's outcome wire
TURN_OUTCOMES = ("ok", "error", "aborted")

# The embedding width every chunk row stores — must match
# AI_EMBED_DIMENSIONS the gateway is asked for
EMBED_DIMENSIONS = 1536








# -----------------------------------------------------------
# SupportChunk
# -----------------------------------------------------------
#
# One embedded passage of the knowledge base: the chunk text
# with its title/section breadcrumbs, the sha256
# content_hash the incremental indexer compares, and the
# embedding itself. embed_model rides along because vectors
# from different models are never comparable — search
# filters on it, and a model swap re-embeds selectively.
# The id is stable across runs: "<source>:<source_id>:<seq>".
#
# Table: support_chunks
# -----------------------------------------------------------

class SupportChunk(models.Model):
    # Columns
    id = models.TextField(primary_key=True)
    source = models.TextField()
    source_id = models.TextField()
    title = models.TextField()
    section = models.TextField(null=True, blank=True)
    language = models.TextField(default="lt")
    text = models.TextField()
    content_hash = models.TextField()
    embed_model = models.TextField()
    embedding = VectorField(dimensions=EMBED_DIMENSIONS)
    indexed_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()

    # Table metadata — the HNSW index over `embedding` is not
    # declared here: it is postgres-only SQL, added by a
    # vendor-guarded operation in the initial migration
    class Meta:
        db_table = "support_chunks"
        constraints = [
            models.CheckConstraint(condition=models.Q(source__in=CHUNK_SOURCES),
                                   name="support_chunks_source_check"),
            models.CheckConstraint(condition=models.Q(language__in=LANGUAGES),
                                   name="support_chunks_language_check"),
        ]
        indexes = [
            models.Index(fields=["source", "source_id"], name="idx_support_chunks_source"),
            models.Index(fields=["last_seen_at"], name="idx_support_chunks_seen"),
        ]








# -----------------------------------------------------------
# AssistantThread
# -----------------------------------------------------------
#
# One AI chat thread. The uuid is generated server-side and
# doubles as a guest's credential: a thread with a user is
# served only to that user, a thread without one is served
# to whoever presents the id (unguessable, like jauka's
# session uuids). title is auto-set from the first user
# message; preview is the newest answer's first words for
# the thread list's second line; last_message_at is the
# thread list's sort key; deleted_at is the GUI's soft
# delete, hard-pruned by cron.
#
# Table: assistant_threads
# -----------------------------------------------------------

class AssistantThread(models.Model):
    # Columns
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.CASCADE,
                             db_column="user_id", db_index=False,
                             related_name="assistant_threads")
    title = models.TextField(null=True, blank=True)
    preview = models.TextField(null=True, blank=True)
    language = models.TextField(default="lt")
    created_at = models.DateTimeField()
    last_message_at = models.DateTimeField()
    deleted_at = models.DateTimeField(null=True, blank=True)

    # Table metadata
    class Meta:
        db_table = "assistant_threads"
        constraints = [
            models.CheckConstraint(condition=models.Q(language__in=LANGUAGES),
                                   name="assistant_threads_language_check"),
        ]
        indexes = [
            # The signed-in thread list: newest talk first
            models.Index(fields=["user", "-last_message_at"], name="idx_assistant_threads_user"),
            models.Index(fields=["deleted_at"], name="idx_assistant_threads_deleted"),
        ]








# -----------------------------------------------------------
# AssistantMessage
# -----------------------------------------------------------
#
# One stored UIMessage, verbatim, as the AI SDK streamed it
# — role, parts, tool calls and results — so reopening a
# thread replays exactly what was seen. The AI SDK message
# id is unique per thread, not globally, so the composite
# PK scopes it (jauka's global string PK trusted client ids
# across sessions; this does not). format names the payload
# dialect and moves when the client's `ai` major does.
# rating is the reader's thumbs verdict on an ASSISTANT
# message (+1/-1, null = unrated) — it lives on the message,
# not the turn, because that is what the person judged.
#
# Table: assistant_messages
# -----------------------------------------------------------

class AssistantMessage(models.Model):
    # Columns — thread_id leads the composite PK, so the
    # per-thread read needs no second index
    pk = models.CompositePrimaryKey("thread_id", "id")
    thread = models.ForeignKey(AssistantThread, on_delete=models.CASCADE, db_index=False,
                               db_column="thread_id", related_name="messages")
    id = models.TextField()
    format = models.TextField(default="aisdk-v7")
    content = models.JSONField()
    rating = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField()

    # Table metadata
    class Meta:
        db_table = "assistant_messages"
        constraints = [
            models.CheckConstraint(condition=models.Q(rating__in=(-1, 1)) | models.Q(rating__isnull=True),
                                   name="assistant_messages_rating_check"),
        ]
        indexes = [
            models.Index(fields=["thread", "created_at"], name="idx_assistant_messages_thread"),
        ]








# -----------------------------------------------------------
# AssistantTurn
# -----------------------------------------------------------
#
# Per-turn usage telemetry, kept apart from the transcript
# on purpose: SET_NULL FKs let usage stats survive a
# deleted thread or an erased account, and no transcript
# text is ever stored here. tool_calls holds the container's
# per-call summary (name, ms, ok). Pruned by cron after its
# retention window.
#
# Table: assistant_turns
# -----------------------------------------------------------

class AssistantTurn(models.Model):
    # Columns
    id = models.BigAutoField(primary_key=True)
    created_at = models.DateTimeField()
    thread = models.ForeignKey(AssistantThread, null=True, blank=True, on_delete=models.SET_NULL,
                               db_column="thread_id", db_index=False, related_name="turns")
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                             db_column="user_id", db_index=False, related_name="assistant_turns")
    client_version = models.TextField(null=True, blank=True)
    language = models.TextField(default="lt")
    model = models.TextField()
    prompt_version = models.IntegerField(null=True, blank=True)
    input_tokens = models.IntegerField(default=0)
    output_tokens = models.IntegerField(default=0)
    tool_calls = models.JSONField(null=True, blank=True)
    duration_ms = models.IntegerField(default=0)
    outcome = models.TextField(default="ok")

    # Table metadata
    class Meta:
        db_table = "assistant_turns"
        constraints = [
            models.CheckConstraint(condition=models.Q(outcome__in=TURN_OUTCOMES),
                                   name="assistant_turns_outcome_check"),
        ]
        indexes = [
            models.Index(fields=["created_at"], name="idx_assistant_turns_created"),
        ]








# -----------------------------------------------------------
# AssistantPrompt
# -----------------------------------------------------------
#
# One immutable version of the admin-editable prompt
# appendix (jauka's ChatPromptSetVersion, single-set). The
# version number is minted server-side and never reused;
# text is what the container lays under its code-owned core
# prompt; the partial unique constraint lets AT MOST ONE
# row be active — activating is deactivate-then-activate in
# one transaction. Deactivating everything is valid: the
# agent then runs on the core prompt alone.
#
# Table: assistant_prompts
# -----------------------------------------------------------

class AssistantPrompt(models.Model):
    # Columns
    id = models.BigAutoField(primary_key=True)
    version = models.IntegerField(unique=True)
    text = models.TextField()
    notes = models.TextField(null=True, blank=True)
    active = models.BooleanField(default=False)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                   db_column="created_by", db_index=False,
                                   related_name="assistant_prompts")
    created_at = models.DateTimeField()

    # Table metadata
    class Meta:
        db_table = "assistant_prompts"
        constraints = [
            models.UniqueConstraint(fields=["active"], condition=models.Q(active=True),
                                    name="assistant_prompts_one_active"),
        ]
