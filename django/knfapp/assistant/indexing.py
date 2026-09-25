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
#  gateway can never empty the knowledge base. And a run
#  that would retire more than a quarter of the stored rows
#  retires NOTHING (KNF-051): a chunker that silently
#  returns [] — a source whitelist drifting from the news
#  model, a scrape gone empty — raises no exception, so no
#  per-unit guard can see it, while one lost table is most
#  of the corpus. The operator re-runs with
#  allow_mass_retire when the shrink is real.
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
# untouched; the callers decide how to surface it. The
# answer's `retireBlocked` counts rows a run WOULD have
# retired but did not (the share guard); allow_mass_retire
# is the operator's override for a real, large shrink.
#
# Used by:
#   - management/commands/index_support_corpus.py — cron
#   - api/admin_views.py — the console's re-index button
############################################################

# The largest share of the stored rows one run may retire —
# a nightly run retires a handful (news ageing out of the
# window, one edited section); a quarter means a source died
MAX_RETIRE_SHARE = 0.25

# Retirements up to this many never trip the share guard — a
# small corpus legitimately loses a whole section at once
RETIRE_FLOOR = 10


def run_index(reindex_all=False, allow_mass_retire=False):
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
    # (their content is unreadable tonight, not gone). And a
    # retirement larger than MAX_RETIRE_SHARE of the stored
    # rows (past RETIRE_FLOOR) is refused whole: a chunker that
    # returned [] without raising looks exactly like its
    # content being deleted, and the share is what tells the
    # two apart — an empty corpus is just the extreme case. A
    # genuinely deleted post or entry still retires the same
    # night — its unit chunked FINE, just without it.
    corpus_ids = {chunk["id"] for chunk in corpus}
    retire = SupportChunk.objects.exclude(id__in=corpus_ids)
    if failed_units:
        spared = Q()
        for source, source_id in failed_units:
            spared |= Q(source=source, source_id=source_id)
        retire = retire.exclude(spared)

    retired = 0
    blocked = 0
    doomed = retire.count()
    stored_total = SupportChunk.objects.count()
    if doomed and not allow_mass_retire and doomed > max(RETIRE_FLOOR, stored_total * MAX_RETIRE_SHARE):
        blocked = doomed
        logger.error("Index run would retire %d of %d knowledge-base rows — refusing; a source "
                     "probably returned nothing. Re-run with allow_mass_retire if the shrink is real",
                     doomed, stored_total)
    elif doomed:
        retired, _ = retire.delete()

    return {"corpus": len(corpus), "embedded": len(to_embed),
            "unchanged": len(fresh_ids), "retired": retired, "retireBlocked": blocked}
