############################################################
#  [*] assistant search — query-time retrieval
#
#  One function: a user's question in, the top matching
#  knowledge-base chunks out. Embeds the query through the
#  same gateway module the indexer used (same model, same
#  dimensions — drift is impossible by construction), then
#  takes a WIDE top slice by pure cosine distance — the one
#  ordering the HNSW index can serve — and re-ranks it in
#  Python with a SOFT language penalty: a same-language row
#  wins a coin flip, but a clearly closer foreign-language
#  row still wins the seat (an EN asker must reach the
#  LT-only corpus). The distance ceiling applies BEFORE the
#  final cut, so a far row never occupies a seat a good row
#  wanted — an empty answer is better grounding than a
#  wrong quote.
#
#  Postgres-only by design: the cosine operator lives in
#  pgvector, so the sqlite test suite exercises callers
#  with THIS function faked, and this function itself is
#  verified against the live dev database.
############################################################


from pgvector.django import CosineDistance

from django.conf import settings

from knfapp.assistant.gateway import embed_texts
from knfapp.assistant.models import SupportChunk


# Cosine distance past this is noise, not an answer — the
# row is dropped even if it made the top k
MAX_DISTANCE = 0.65

# How many rows a search may return at most, whatever the
# caller asked for
MAX_LIMIT = 8

# How many nearest rows the wide slice fetches before the
# Python re-rank — generous enough that the ceiling and the
# language penalty always have alternatives to promote
CANDIDATES = 40

# Added to a row's distance when its language differs from
# the asker's — a nudge, never a wall: ~0.08 loses close
# calls and survives clear wins
LANGUAGE_PENALTY = 0.08

# How much chunk text the excerpt carries back to the model —
# the WHOLE chunk (MAX_CHUNK_CHARS + overlap headroom): the
# consumer is a model, and a truncated contacts chunk once
# hid the library's phone number from every answer
EXCERPT_CHARS = 1800








############################################################
# search_chunks
############################################################
#
#   search_chunks("kaip gauti stipendija?", limit=5,
#                 language="lt")
#     → [{id, title, excerpt, section, language}, ...]
#
# The searchHandbook tool's data, shaped exactly like its
# wire contract's entries. Only rows embedded by the
# CURRENT model are searched. `embed` is the test seam for
# the gateway call; the app never passes it.
#
# Used by:
#   - api/internal_views.py — assistant_search
############################################################

def search_chunks(query, limit=5, language=None, embed=None):
    try:
        limit = max(1, min(int(limit or 5), MAX_LIMIT))
    except (TypeError, ValueError):
        limit = 5
    vector = (embed or embed_texts)([query])[0]

    # Phase one: the wide slice by PURE distance — the ordering
    # the HNSW index serves; every other criterion is Python's
    candidates = list(
        SupportChunk.objects.filter(embed_model=settings.AI_EMBED_MODEL)
        .annotate(distance=CosineDistance("embedding", vector))
        .order_by("distance")[:CANDIDATES]
    )

    # Phase two: ceiling first (a far row never takes a seat),
    # then the soft language nudge decides the final order
    def score(row):
        penalty = LANGUAGE_PENALTY if language in ("lt", "en") and row.language != language else 0.0
        return (row.distance or 0.0) + penalty

    ranked = sorted(
        (row for row in candidates if row.distance is not None and row.distance <= MAX_DISTANCE),
        key=score,
    )

    return [
        {
            "id": row.id,
            "title": row.title,
            "excerpt": row.text[:EXCERPT_CHARS],
            "section": row.section,
            "language": row.language,
        }
        for row in ranked[:limit]
    ]
