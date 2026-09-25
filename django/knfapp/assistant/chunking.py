############################################################
#  [*] assistant chunking — corpus rows out of live tables
#
#  Turns the app's existing content into the flat chunk
#  list the indexer embeds: the EFFECTIVE handbook (info
#  views' effective_handbook — curated FACULTY_INFO with
#  the surviving faculty_info rows laid over it through the
#  same floors and the same 'lt' fallback the info API
#  serves, so the knowledge base and the Info screen can
#  never disagree) and the public news posts. Existing
#  tables stay the truth — a chunk carries
#  source/source_id pointing back, and its id is CONTENT-
#  derived ("<source>:<source_id>:<hash12>", the chunk
#  hash's prefix): reordering entries in a document never
#  renames its unchanged chunks, so nothing re-embeds over
#  a reorder and an old transcript's cited id keeps naming
#  the text it cited (a positional seq silently re-pointed
#  both). One malformed section or post logs and skips —
#  it must never take the rest of the corpus down with it.
#
#  Chunk texts are prose, not JSON: each known handbook
#  section has a renderer that lays the fields out as
#  lines a model can quote (an unknown scraped section
#  falls back to a generic key/value walk), and every FAQ
#  entry becomes its own chunk — question-shaped passages
#  retrieve best for question-shaped queries. Every chunk
#  carries a HUMAN title — it is what a student reads in an
#  answer's Sources list, so an unmapped section is titled
#  from its key made readable, never the raw code key
#  ("general_contact — general_contact" was cited once,
#  KNF-150). Long texts split on paragraph seams with
#  overlap.
#
#  Split into:
#
#    chunk_hash        — the indexer's change detector
#    split_text        — the long-text splitter
#    _flatten_generic  — unknown JSON → indented lines
#    _handbook_*       — one renderer per known section
#    _section_title    — the human title of a section
#    handbook_chunks   — the merged handbook, chunked
#    news_chunks       — public news posts, chunked
#    admin_entry_chunk — one console-authored entry → chunk
#    admin_curated_rows — the console's stored rows, re-read
#    curated_chunks    — the repo-owned extras + the console's
#    build_corpus      — everything, one list
############################################################


import hashlib
import logging
import re

from bs4 import BeautifulSoup

from knfapp.assistant.curated_faq import CURATED_FAQ
from knfapp.assistant.models import SupportChunk
from knfapp.info.api.views import effective_handbook
from knfapp.info.handbook import FACULTY_INFO


logger = logging.getLogger(__name__)


# What marks a curated row as console-authored: its source_id
# is this prefix plus the entry's uuid (the repo-owned
# CURATED_FAQ rows carry the bare language instead)
ADMIN_SOURCE_PREFIX = "admin:"


# Texts longer than this split into overlapping windows —
# roughly a page; embeddings degrade on much longer inputs
MAX_CHUNK_CHARS = 1500

# Characters repeated from the previous window so a fact
# straddling a seam still lives whole in one chunk
OVERLAP_CHARS = 200

# Newest-first cap on embedded news — old posts age out of
# the semantic index while news_posts keeps them forever
MAX_NEWS_POSTS = 300

# Collapses runs of blank lines the renderers produce
BLANK_RUN_RE = re.compile(r"\n{3,}")








############################################################
# chunk_hash
############################################################
#
# sha256 over everything that makes a chunk's meaning: the
# rendered text plus its breadcrumbs. The indexer compares
# this against the stored row to decide whether the chunk
# needs re-embedding at all.
#
# Used by:
#   - management/commands/index_support_corpus.py
#   - the chunking tests — hash stability is pinned
############################################################

def chunk_hash(chunk):
    seed = "\x1f".join([chunk["title"], chunk.get("section") or "", chunk["text"]])
    return hashlib.sha256(seed.encode()).hexdigest()




def _make_chunk(source, source_id, title, section, language, text):
    # The id carries the hash's prefix, so it follows the
    # CONTENT: a reordered document keeps its unchanged
    # chunks' ids (nothing re-embeds, old citations keep
    # naming their text), and an edited chunk gets a NEW id
    # (a dangling old citation, honestly, instead of one
    # silently re-pointed at different text)
    chunk = {
        "source": source,
        "source_id": source_id,
        "title": title,
        "section": section,
        "language": language,
        "text": text,
    }
    chunk["id"] = f"{source}:{source_id}:{chunk_hash(chunk)[:12]}"
    return chunk








############################################################
# split_text
############################################################
#
#   split_text("...4000 chars...") → ["...", "...", "..."]
#
# Splits on paragraph seams (blank lines) packing greedily
# up to MAX_CHUNK_CHARS; a single oversized paragraph falls
# back to a hard character split. Consecutive windows
# repeat OVERLAP_CHARS of tail so no fact is cut in half at
# a seam. Short texts come back as [text] untouched.
#
# Used by:
#   - handbook_chunks / news_chunks (below)
############################################################

def split_text(text):
    text = text.strip()
    if len(text) <= MAX_CHUNK_CHARS:
        return [text] if text else []

    paragraphs = [part.strip() for part in text.split("\n\n") if part.strip()]
    pieces = []
    for paragraph in paragraphs:
        while len(paragraph) > MAX_CHUNK_CHARS:
            pieces.append(paragraph[:MAX_CHUNK_CHARS])
            paragraph = paragraph[MAX_CHUNK_CHARS - OVERLAP_CHARS:]
        pieces.append(paragraph)

    chunks = []
    current = ""
    for piece in pieces:
        if current and len(current) + 2 + len(piece) > MAX_CHUNK_CHARS:
            chunks.append(current)
            # Carry the tail over so the next window keeps the
            # previous context in reach
            current = current[-OVERLAP_CHARS:] + "\n\n" + piece
        else:
            current = f"{current}\n\n{piece}" if current else piece
    if current:
        chunks.append(current)
    return chunks








############################################################
# _flatten_generic
############################################################
#
# The fallback renderer for scraped sections this module
# has no dedicated shape for: walks the JSON and lays
# key/value leaves out as lines, lists as repeated blocks.
# Ugly but honest — a new scraped section is searchable
# the day it appears, and can earn a renderer later.
#
# Used by:
#   - handbook_chunks (below)
############################################################

def _flatten_generic(value, indent=0):
    pad = "  " * indent
    lines = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                lines.append(f"{pad}{key}:")
                lines.extend(_flatten_generic(item, indent + 1))
            elif item not in (None, ""):
                lines.append(f"{pad}{key}: {item}")
    elif isinstance(value, list):
        for item in value:
            lines.extend(_flatten_generic(item, indent))
            lines.append("")
    elif value not in (None, ""):
        lines.append(f"{pad}{value}")
    return lines








############################################################
# _handbook_contacts / _handbook_links / _handbook_hours /
# _handbook_programs / _handbook_faq /
# _handbook_general_contact
############################################################
#
# One renderer per known section: each returns a list of
# (section_label, text) pairs — most sections render as one
# text, the FAQ as one pair PER ENTRY, because a chunk
# shaped like the question it answers is what cosine
# search finds first. general_contact is the scraper's
# faculty block ({address, phone, email}), laid out like a
# contacts entry under the contacts label; a key it does not
# know still lands as a key: value line, and a blob that is
# not an object takes the generic walk.
############################################################

def _handbook_contacts(blob):
    lines = []
    for group in blob or []:
        lines.append(f"{group.get('category', '')}:")
        for item in group.get("items", []):
            parts = [item.get("name", "")]
            if item.get("phone"):
                parts.append(f"tel. {item['phone']}")
            if item.get("email"):
                parts.append(item["email"])
            if item.get("room"):
                parts.append(f"kab. {item['room']}")
            lines.append("  " + ", ".join(part for part in parts if part))
        lines.append("")
    return [("contacts", "\n".join(lines))]


def _handbook_links(blob):
    lines = [f"{item.get('title', '')}: {item.get('url', '')}" for item in blob or []]
    return [("links", "\n".join(lines))]


def _handbook_hours(blob):
    lines = []
    for item in blob or []:
        parts = [item.get("place", ""), item.get("address", ""), item.get("schedule", ""), item.get("note", "")]
        lines.append(" — ".join(part for part in parts if part))
    return [("hours", "\n".join(lines))]


def _handbook_programs(blob):
    lines = []
    for item in blob or []:
        parts = [item.get("name", ""), item.get("degree", ""), item.get("duration", "")]
        lines.append(" — ".join(part for part in parts if part))
    return [("programs", "\n".join(lines))]


def _handbook_faq(blob):
    pairs = []
    for item in blob or []:
        question = (item.get("q") or "").strip()
        answer = (item.get("a") or "").strip()
        if question and answer:
            pairs.append(("faq", f"{question}\n{answer}"))
    return pairs


def _handbook_general_contact(blob):
    if not isinstance(blob, dict):
        return [("general_contact", "\n".join(_flatten_generic(blob)))]
    lines = []
    if blob.get("address"):
        lines.append(str(blob["address"]).strip())
    if blob.get("phone"):
        lines.append(f"tel. {str(blob['phone']).strip()}")
    if blob.get("email"):
        lines.append(str(blob["email"]).strip())
    for key, value in blob.items():
        if key not in ("address", "phone", "email") and value not in (None, ""):
            lines.append(f"{key}: {value}")
    return [("contacts", "\n".join(lines))]


_SECTION_RENDERERS = {
    "contacts": _handbook_contacts,
    "links": _handbook_links,
    "hours": _handbook_hours,
    "programs": _handbook_programs,
    "faq": _handbook_faq,
    "general_contact": _handbook_general_contact,
}








############################################################
# handbook_chunks
############################################################
#
# The EFFECTIVE handbook per language — info views'
# effective_handbook, the very merge GET /api/info serves:
# the curated base, the scraped rows laid over it through
# apply_scraped_overlay's shape checks and size floors (a
# one-entry partial scrape keeps the full curated list, so
# a full chunk is never replaced by a stub and then
# retired under its content-derived id), and the 'lt'
# overlay borrowed for a language the scraper never
# writes. The English rows therefore carry the Lithuanian
# programme names exactly as ?lang=en does — intended
# parity, see effective_handbook's banner. Rendered
# section by section into chunk dicts: FAQ entries chunk
# one per question with the question as the title; other
# sections carry a section title and split only if
# oversized; a section without a renderer (the scraped
# general_contact block) takes the generic key/value walk.
#
# Used by:
#   - build_corpus (below)
############################################################

# What the chunk lists as its human title per section, per
# language — the model shows these when it cites, and the
# app's Sources list prints them
_SECTION_TITLES = {
    "lt": {"contacts": "Kontaktai", "links": "Nuorodos", "hours": "Darbo laikas",
           "programs": "Studijų programos", "faq": "D.U.K.",
           "general_contact": "Fakulteto kontaktai"},
    "en": {"contacts": "Contacts", "links": "Links", "hours": "Opening hours",
           "programs": "Study programs", "faq": "FAQ",
           "general_contact": "Faculty contacts"},
}


def _section_title(lang, section):
    # A section the table does not know yet is titled from its
    # key made readable — never the raw snake_case identifier
    return (_SECTION_TITLES.get(lang, {}).get(section)
            or section.replace("_", " ").strip().capitalize())


def handbook_chunks(failures=None):
    chunks = []
    for lang in FACULTY_INFO:
        merged, _ = effective_handbook(lang)

        for section, blob in merged.items():
            # One malformed scraped section (a shape the
            # renderer never expected) skips with a log line —
            # it must not cost the corpus its other sections,
            # and the failure is REPORTED so the indexer knows
            # this unit's stored rows are missing-not-deleted
            try:
                renderer = _SECTION_RENDERERS.get(section)
                if renderer:
                    rendered = renderer(blob)
                else:
                    rendered = [(section, "\n".join(_flatten_generic(blob)))]

                for label, text in rendered:
                    text = BLANK_RUN_RE.sub("\n\n", text).strip()
                    if not text:
                        continue
                    title = _section_title(lang, section)
                    # FAQ chunks take their question as the title —
                    # that is the line the assistant cites
                    if section == "faq":
                        title = text.split("\n", 1)[0]
                    for piece in split_text(text):
                        chunks.append(_make_chunk("handbook", f"{lang}-{section}",
                                                  title, label, lang, piece))
            except Exception:
                logger.exception("Handbook section %s/%s failed to chunk — skipped", lang, section)
                if failures is not None:
                    failures.append(("handbook", f"{lang}-{section}"))
    return chunks








############################################################
# news_chunks
############################################################
#
# The newest MAX_NEWS_POSTS public posts, title + body per
# chunk. Bodies arrive as scraped HTML or plain text — both
# strip to prose through BeautifulSoup. News is ALSO served
# live by the searchNews tool; embedding it besides lets
# semantic handbook-style questions surface an old
# announcement the keyword search would miss.
#
# Used by:
#   - build_corpus (below)
############################################################

def news_chunks(failures=None):
    # Imported here, not at module top: chunking is also used
    # by tests that fake the news table entirely
    from knfapp.news.models import SCRAPED_SOURCES, NewsPost

    chunks = []
    # STAFF AND SCRAPED posts only — a student's own post must
    # never become citable knowledge-base content (any public
    # user post is attacker-authorable prose). The scraped
    # sources come from the news model itself: a hand-copied
    # list drifts silently the day the scraper renames one,
    # and an empty news unit raises nothing (KNF-051)
    posts = (NewsPost.objects.filter(is_public=True,
                                     source__in=(*SCRAPED_SOURCES, "faculty"))
             .order_by("-published_at")
             .values("id", "title", "summary", "content", "source", "published_at")[:MAX_NEWS_POSTS])
    for post in posts:
        # One unparseable post skips with a log line — the
        # nightly run must never lose the corpus over it
        try:
            body = post["content"] or post["summary"] or ""
            if "<" in body:
                body = BeautifulSoup(body, "lxml").get_text(" ", strip=True)
            published = post["published_at"].date().isoformat() if post["published_at"] else ""
            text = "\n".join(part for part in [post["title"], published, body.strip()] if part)
            for piece in split_text(text):
                chunks.append(_make_chunk("news", str(post["id"]),
                                          post["title"], post["source"], "lt", piece))
        except Exception:
            logger.exception("News post %s failed to chunk — skipped", post.get("id"))
            if failures is not None:
                failures.append(("news", str(post.get("id"))))
    return chunks








############################################################
# admin_entry_chunk
############################################################
#
#   admin_entry_chunk(entry_id, question, answer, lang)
#     → chunk dict
#
# ONE console-authored entry as a chunk, shaped exactly like
# a repo-owned FAQ entry (question on top, the question as
# the cited title) — the one builder both the console's
# save and the indexer's re-read go through, so the id and
# hash always agree. Never split: the console caps the
# answer under the splitter's window, so an entry is always
# exactly one row.
#
# Used by:
#   - curated.py — save_entry
############################################################

def admin_entry_chunk(entry_id, question, answer, language):
    return _make_chunk("curated", f"{ADMIN_SOURCE_PREFIX}{entry_id}", question, "faq",
                       language, f"{question}\n{answer}")








############################################################
# admin_curated_rows
############################################################
#
# The console's stored entries, re-emitted as chunk dicts
# from their own stored fields — so a nightly run finds
# each one in the corpus under the id it already carries
# (unchanged: stamped, never re-embedded, never retired).
# Their source of truth is the row itself; there is no
# document to re-render.
#
# Used by:
#   - curated_chunks (below)
############################################################

def admin_curated_rows():
    rows = (SupportChunk.objects
            .filter(source="curated", source_id__startswith=ADMIN_SOURCE_PREFIX)
            .values("source_id", "title", "section", "language", "text"))
    return [_make_chunk("curated", row["source_id"], row["title"], row["section"],
                        row["language"], row["text"])
            for row in rows]








############################################################
# curated_chunks
############################################################
#
# The repo-owned extras (curated_faq.py), chunked exactly
# like the handbook FAQ: one chunk per entry, the question
# as the title — question-shaped passages retrieve best for
# question-shaped queries — plus the console-authored rows
# read back from support_chunks (admin_curated_rows).
#
# Used by:
#   - build_corpus (below)
############################################################

def curated_chunks():
    chunks = []
    for lang, entries in CURATED_FAQ.items():
        for entry in entries:
            question = (entry.get("q") or "").strip()
            answer = (entry.get("a") or "").strip()
            if not question or not answer:
                continue
            for piece in split_text(f"{question}\n{answer}"):
                chunks.append(_make_chunk("curated", lang, question, "faq", lang, piece))
    chunks.extend(admin_curated_rows())
    return chunks








############################################################
# build_corpus
############################################################
#
# The whole corpus as one list of chunk dicts — what the
# indexer diffs against support_chunks. Ids are content-
# derived, so a duplicate id means IDENTICAL content minted
# twice inside one document (say a copy-pasted FAQ entry) —
# one copy is kept; embedding the same text twice buys
# nothing. `failures`, when passed, collects the
# (source, source_id) units that failed to chunk — the
# indexer spares their stored rows from retirement, because
# "failed to read" is not "deleted".
#
# Used by:
#   - management/commands/index_support_corpus.py
############################################################

def build_corpus(failures=None):
    chunks = handbook_chunks(failures) + news_chunks(failures) + curated_chunks()
    unique = []
    seen = set()
    for chunk in chunks:
        if chunk["id"] in seen:
            continue
        seen.add(chunk["id"])
        unique.append(chunk)
    return unique
