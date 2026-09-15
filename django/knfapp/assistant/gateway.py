############################################################
#  [*] assistant gateway — the embeddings call
#
#  The one place Django talks to the faculty AI gateway
#  (ai.knf.vu.lt — Bifrost, OpenAI wire format). Only the
#  embeddings endpoint is used here: the chat completions
#  side belongs to the assistant container, never to
#  Django. The key rides the x-bf-vk header Bifrost asks
#  for, plus a standard Authorization bearer so any
#  OpenAI-compatible stand-in accepts the same request.
#
#  Both the indexer and query-time search embed THROUGH
#  THIS MODULE with the same model and dimensions settings,
#  so index-time and query-time vectors can never drift
#  apart.
#
#  Split into:
#
#    GatewayError — one typed failure for every caller
#    embed_texts  — texts in, vectors out, batched
############################################################


import requests

from django.conf import settings


# One embeddings call carries at most this many inputs —
# well under the API's 2048 ceiling, keeps request bodies
# comfortably small
BATCH_SIZE = 128

# Seconds to wait for the gateway before giving up — the
# indexer runs in cron and search runs inside a user-facing
# request, so neither may hang forever
TIMEOUT_S = 30








############################################################
# GatewayError
############################################################
#
# The one exception shape every gateway failure surfaces
# as — HTTP status errors, timeouts, malformed bodies. The
# callers (indexer, search) catch THIS, never requests'
# zoo of exception classes.
#
# Used by:
#   - embed_texts (below)
#   - management/commands/index_support_corpus.py
#   - api/internal_views.py — assistant_search's 502
############################################################

class GatewayError(Exception):
    pass








############################################################
# embed_texts
############################################################
#
#   embed_texts(["klausimas", ...]) → [[0.01, ...], ...]
#
# Embeds texts through the gateway in BATCH_SIZE slices,
# preserving order — the result list always matches the
# input list one to one, or GatewayError is raised. The
# model and dimensions come from settings (AI_EMBED_MODEL /
# AI_EMBED_DIMENSIONS); an empty input list answers []
# without touching the network. `post` is the test seam —
# the suite injects a fake, the app never passes it.
#
# Used by:
#   - management/commands/index_support_corpus.py
#   - search.py — the query-time embedding
############################################################

def embed_texts(texts, post=None):
    if not texts:
        return []
    if not settings.AI_GATEWAY_KEY:
        raise GatewayError("AI_GATEWAY_KEY is not configured")

    send = post or requests.post
    url = settings.AI_GATEWAY_URL.rstrip("/") + "/embeddings"
    headers = {
        "x-bf-vk": settings.AI_GATEWAY_KEY,
        "Authorization": f"Bearer {settings.AI_GATEWAY_KEY}",
    }

    vectors = []
    for start in range(0, len(texts), BATCH_SIZE):
        batch = texts[start:start + BATCH_SIZE]
        payload = {
            "model": settings.AI_EMBED_MODEL,
            "input": batch,
            "dimensions": settings.AI_EMBED_DIMENSIONS,
        }
        try:
            response = send(url, json=payload, headers=headers, timeout=TIMEOUT_S)
        except requests.RequestException as exc:
            raise GatewayError(f"embeddings request failed: {exc}") from exc
        if response.status_code != 200:
            raise GatewayError(f"embeddings answered {response.status_code}: {response.text[:200]}")

        try:
            rows = response.json()["data"]
            # The API documents index-ordered rows; sort defensively
            # so a permuted answer still lines up with the batch
            rows = sorted(rows, key=lambda row: row["index"])
            batch_vectors = [row["embedding"] for row in rows]
        except (KeyError, TypeError, ValueError) as exc:
            raise GatewayError(f"embeddings answered an unexpected shape: {exc}") from exc
        if len(batch_vectors) != len(batch):
            raise GatewayError(f"embeddings returned {len(batch_vectors)} vectors for {len(batch)} inputs")
        vectors.extend(batch_vectors)

    return vectors
