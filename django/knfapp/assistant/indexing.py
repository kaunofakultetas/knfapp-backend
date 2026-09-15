############################################################
#  [*] assistant indexing — the sync, callable
#
#  The knowledge-base sync as one function, so cron's
#  management command and the admin console's re-index
#  button run EXACTLY the same code: build the corpus from
#  the live tables, diff by content hash, embed only what
#  changed, stamp sightings, retire vanished rows. The
#  gateway is asked FIRST — a dead embeddings API raises
#  before any stamp, upsert or delete runs, so a broken
#  gateway can never empty the knowledge base.
#
#  Split into:
#
#    run_index — the whole sync, counts back
############################################################


import logging

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from knfapp.assistant.chunking import build_corpus, chunk_hash
from knfapp.assistant.gateway import embed_texts
from knfapp.assistant.models import SupportChunk


logger = logging.getLogger(__name__)








############################################################
# run_index
############################################################
#
#   run_index()                → {"corpus": 248, "embedded":
#   run_index(reindex_all=True)   3, "unchanged": 245,
#                                 "retired": 0}
#
# One incremental sync (or a forced full re-embed — the
# move after changing AI_EMBED_MODEL). Raises GatewayError
# untouched; the callers decide how to surface it.
#
# Used by:
#   - management/commands/index_support_corpus.py — cron
#   - api/admin_views.py — the console's re-index button
############################################################

def run_index(reindex_all=False):
    now = timezone.now()
    # Units the chunkers could not read (a malformed section, an
    # unparseable post) — their stored rows must survive the
    # retirement below: missing-because-broken is not deleted
    failed_units = []
    corpus = build_corpus(failures=failed_units)
    for chunk in corpus:
        chunk["hash"] = chunk_hash(chunk)

    stored = {
        row["id"]: row
        for row in SupportChunk.objects.values("id", "content_hash", "embed_model")
    }

    # Unchanged rows only get their sighting stamped; a row
    # embedded by another model counts as changed even when
    # the text hash matches
    fresh_ids = []
    to_embed = []
    for chunk in corpus:
        known = stored.get(chunk["id"])
        unchanged = (known is not None
                     and known["content_hash"] == chunk["hash"]
                     and known["embed_model"] == settings.AI_EMBED_MODEL)
        if unchanged and not reindex_all:
            fresh_ids.append(chunk["id"])
        else:
            to_embed.append(chunk)

    # Embed FIRST — if the gateway is down this raises and
    # nothing below (stamps, upserts, deletes) runs
    vectors = embed_texts([chunk["text"] for chunk in to_embed])

    if fresh_ids:
        SupportChunk.objects.filter(id__in=fresh_ids).update(last_seen_at=now)

    for chunk, vector in zip(to_embed, vectors):
        SupportChunk.objects.update_or_create(
            id=chunk["id"],
            defaults={
                "source": chunk["source"],
                "source_id": chunk["source_id"],
                "title": chunk["title"],
                "section": chunk["section"],
                "language": chunk["language"],
                "text": chunk["text"],
                "content_hash": chunk["hash"],
                "embed_model": settings.AI_EMBED_MODEL,
                "embedding": vector,
                "indexed_at": now,
                "last_seen_at": now,
            },
        )

    # Retire rows whose source content vanished — with two
    # guards. Units that FAILED to chunk keep their stored rows
    # (their content is unreadable tonight, not gone), and an
    # entirely empty corpus retires nothing at all: the curated
    # handbook base alone guarantees a healthy run is never
    # empty, so an empty one is a broken run, and a broken run
    # must not empty the knowledge base. A genuinely deleted
    # post or entry still retires the same night — its unit
    # chunked FINE, just without it.
    corpus_ids = {chunk["id"] for chunk in corpus}
    retired = 0
    if corpus_ids:
        retire = SupportChunk.objects.exclude(id__in=corpus_ids)
        if failed_units:
            spared = Q()
            for source, source_id in failed_units:
                spared |= Q(source=source, source_id=source_id)
            retire = retire.exclude(spared)
        retired, _ = retire.delete()
    else:
        logger.warning("Index run produced an EMPTY corpus — retiring nothing")

    return {"corpus": len(corpus), "embedded": len(to_embed),
            "unchanged": len(fresh_ids), "retired": retired}
