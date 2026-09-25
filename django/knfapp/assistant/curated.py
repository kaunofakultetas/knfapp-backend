############################################################
#  [*] assistant curated store — the console's own answers
#
#  The knowledge-base entries an ADMIN writes in the console
#  (the critique's P1-14: the questions students actually
#  ask — sessions, retakes, fees, certificates — that no
#  scraped page carries): a question and an answer in one
#  language, embedded on save and stored straight into
#  support_chunks under source "curated". The chunk row IS
#  the entry — there is no second table, a migration-free
#  choice because the live runserver never migrates. The
#  nightly indexer re-emits these rows from their own stored
#  fields (chunking.admin_curated_rows), so their content-
#  derived ids round-trip as "unchanged": never re-embedded,
#  never retired. An edit embeds into a NEW id and drops the
#  old row in the same transaction (an old transcript's
#  citation dangles honestly instead of naming new text); a
#  delete drops the row outright. The repo-owned CURATED_FAQ
#  literals stay the base these sit beside.
#
#  Split into:
#
#    entry_payload — one row as the console reads it
#    list_entries  — every console entry, newest first
#    entry_exists  — the 404 test for edit and delete
#    save_entry    — embed + upsert one entry
#    delete_entry  — drop one entry
############################################################


from django.conf import settings
from django.db import transaction
from django.utils import timezone

from knfapp.assistant.chunking import ADMIN_SOURCE_PREFIX, admin_entry_chunk, chunk_hash
from knfapp.assistant.gateway import embed_texts
from knfapp.assistant.models import SupportChunk


# A question is the chunk's cited title — one line, not a
# paragraph
MAX_QUESTION_CHARS = 200

# An answer stays one chunk: under the splitter's window with
# the question on top, so an entry is always exactly one row
# (edit and delete are 1:1) and the model quotes it whole
MAX_ANSWER_CHARS = 1200








############################################################
# entry_payload
############################################################
#
# One stored row as the console reads it — the question is
# the row's title, the answer is the text with the question
# line taken back off the top (the chunk carries both so a
# question-shaped query retrieves it).
#
# Used by:
#   - list_entries (below)
#   - api/admin_views.py — create/update answers
############################################################

def entry_payload(row):
    question = row.title
    answer = row.text
    if answer.startswith(question + "\n"):
        answer = answer[len(question) + 1:]
    return {
        "id": row.source_id[len(ADMIN_SOURCE_PREFIX):],
        "question": question,
        "answer": answer,
        "language": row.language,
        "indexedAt": row.indexed_at,
    }








############################################################
# list_entries
############################################################
#
# Every console-authored entry, newest first. The repo-owned
# CURATED_FAQ base is not in this list — that is code,
# changed by a commit.
#
# Used by:
#   - api/admin_views.py — list_curated
############################################################

def list_entries():
    rows = (SupportChunk.objects
            .filter(source="curated", source_id__startswith=ADMIN_SOURCE_PREFIX)
            .order_by("-indexed_at", "id"))
    return [entry_payload(row) for row in rows]








############################################################
# entry_exists
############################################################
#
# Used by:
#   - api/admin_views.py — update_curated
############################################################

def entry_exists(entry_id):
    return SupportChunk.objects.filter(source="curated",
                                       source_id=f"{ADMIN_SOURCE_PREFIX}{entry_id}").exists()








############################################################
# save_entry
############################################################
#
#   save_entry(entry_id, question, answer, language) → row
#
# Embeds FIRST — a dead gateway raises GatewayError and
# nothing is written — then, in one transaction, drops any
# earlier row of this entry (an edit changes the content-
# derived id) and upserts the new one, stamped exactly as
# the indexer stamps its rows so the nightly sync reads it
# back as unchanged.
#
# Used by:
#   - api/admin_views.py — create_curated, update_curated
############################################################

def save_entry(entry_id, question, answer, language):
    chunk = admin_entry_chunk(entry_id, question, answer, language)
    vector = embed_texts([chunk["text"]])[0]
    now = timezone.now()
    with transaction.atomic():
        (SupportChunk.objects
         .filter(source="curated", source_id=chunk["source_id"])
         .exclude(id=chunk["id"])
         .delete())
        row, _ = SupportChunk.objects.update_or_create(
            id=chunk["id"],
            defaults={
                "source": chunk["source"],
                "source_id": chunk["source_id"],
                "title": chunk["title"],
                "section": chunk["section"],
                "language": chunk["language"],
                "text": chunk["text"],
                "content_hash": chunk_hash(chunk),
                "embed_model": settings.AI_EMBED_MODEL,
                "embedding": vector,
                "indexed_at": now,
                "last_seen_at": now,
            },
        )
    return row








############################################################
# delete_entry
############################################################
#
#   delete_entry(entry_id) → rows dropped (0 = unknown id)
#
# Used by:
#   - api/admin_views.py — delete_curated
############################################################

def delete_entry(entry_id):
    deleted, _ = (SupportChunk.objects
                  .filter(source="curated", source_id=f"{ADMIN_SOURCE_PREFIX}{entry_id}")
                  .delete())
    return deleted
