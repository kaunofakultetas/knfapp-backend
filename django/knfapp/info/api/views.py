############################################################
#  [*] Info API — the faculty handbook
#
#  One public route: the curated bilingual handbook
#  (info/handbook.py) with the scraped faculty_info rows
#  laid over it — scraped contacts and programs REPLACE the
#  curated lists once they clear their freshness, shape and
#  size floors, a scraped general_contact block is added,
#  and links/hours/faq stay curated because knf.vu.lt has
#  nothing to scrape for them. The scraper writes 'lt' only,
#  so ?lang=en borrows the 'lt' overlay while its curated
#  sections stay English — HONESTLY: the closed
#  vocabularies the scraper writes (degree words, "N metai"
#  durations, the "(anglų k.)" marker) are translated, and
#  every borrowed entry whose prose stays Lithuanian (a
#  programme's registered name, a contact group) carries
#  nameLang: "lt" so the client can say so and read it with
#  a Lithuanian voice. The English screen used to show 25
#  Lithuanian cards under "Bakalauras" chips, served as
#  lang "en" (KNF-124/127).
#
#  Split into:
#
#    warn_once / parse_timestamp — process-lifetime helpers
#    get_scraped_info            — the surviving overlay rows
#    clean_program               — the programme item floor
#    localize_borrowed           — a borrowed overlay, made
#                                  honest for its language
#    apply_scraped_overlay       — the floors and the swap
#    effective_handbook          — THE merge, one per language
#    _overlay_signature          — the ETag's exact input, cheap
#    get_faculty_info            — GET /api/info
############################################################


import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone


from django.db import Error as DatabaseError
from django.http import HttpResponse


from knfapp.common.http import (
    clean_param, etag_for, if_none_match_contains, json_error, json_response, require_methods,
)
from knfapp.info.handbook import FACULTY_INFO
from knfapp.info.models import FacultyInfo
from knfapp.info.programs import is_program_name


logger = logging.getLogger(__name__)

# The handbook is identical bytes between two 24 h scrapes
CACHE_MAX_AGE = 24 * 3600

# A blob older than this is ignored — better the curated
# handbook than a scrape from a month ago
SCRAPED_MAX_AGE_DAYS = 30

# Floors a scraped overlay must clear before it may hide the
# curated lists — the scraper regularly returns a single
# category after a partial page load
MIN_SCRAPED_CONTACT_ITEMS = 5
MIN_SCRAPED_PROGRAMS = 3

# Conditions already warned about in this process
_warned = set()

# The overlay's wire policy, part of the ETag seed: bumped when
# the SHAPE of the merge changes (2: borrowed entries translated
# and flagged, programme items floored; 3: the seed names every
# fresh scraped section, not just the newest stamp; 4: admission
# documents are no longer served as programmes), so a client
# holding the old answer's tag revalidates to the new one
OVERLAY_POLICY = 4

# Closed vocabularies the 'lt' scraper writes, per borrowing
# language — anything outside them stays as scraped (the entry
# is flagged nameLang "lt" either way)
_DEGREE_WORDS = {
    "en": {"bakalauras": "Bachelor's", "magistras": "Master's"},
}

# "4 metai", "3,5 metų", "2 m." — the scraper's duration shape
_DURATION_RE = re.compile(r"^\s*(\d+)(?:[.,](\d+))?\s*(?:metai|metų|metu|m\.)\s*$", re.IGNORECASE)

# The suffix Lithuanian listings mark English-taught programmes
# with — in an English UI it becomes a note in plain English
_TAUGHT_IN_ENGLISH_RE = re.compile(r"\s*\((?:anglų|anglu)\s+k(?:\.|alba)\)\s*$", re.IGNORECASE)
_TAUGHT_IN_ENGLISH_NOTE = {"en": "Taught in English"}

# Fingerprint of the curated handbook, computed once at import:
# a deploy that edits it must move the ETag even when no
# scraped row changed
FALLBACK_VERSION = hashlib.sha256(
    json.dumps(FACULTY_INFO, sort_keys=True, ensure_ascii=False).encode("utf-8")
).hexdigest()[:8]








############################################################
# warn_once / parse_timestamp
############################################################
#
# The route is public and hot, and every condition warned
# about here LASTS (an empty table, a stale blob) — one line
# per process, said again after a restart. parse_timestamp
# answers an aware datetime for a datetime or an ISO-ish
# string (space or 'T' separator, 'Z' accepted); naive
# reads as UTC, garbage as None.
#
# Used by:
#   - get_scraped_info, apply_scraped_overlay (below)
############################################################

def warn_once(key, message, *args):
    if key in _warned:
        return
    _warned.add(key)
    logger.warning(message, *args)


def parse_timestamp(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace(" ", "T").replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed








############################################################
# get_scraped_info
############################################################
#
# The faculty_info rows for one language as
# ({section: blob}, newest stamp), or (None, None) when
# nothing usable survives. data_json is a JSON column, so
# the ORM hands each blob back as the structure the scraper
# stored. "Newest" is ranked on the PARSED instant; the
# stored value itself is what the answer displays. Dropped,
# each with one warning per process: an empty table, a blob
# past the age cutoff. A database-level failure falls back
# to the curated handbook instead of 500ing it.
#
# Used by:
#   - effective_handbook (below)
############################################################

def get_scraped_info(lang):
    try:
        rows = list(FacultyInfo.objects.filter(lang=lang).values("section", "data_json", "scraped_at"))
    except DatabaseError:
        logger.exception("faculty_info lookup failed for lang '%s' — serving the curated handbook", lang)
        return None, None

    if not rows:
        warn_once(f"empty-{lang}",
                  "faculty_info holds no '%s' rows — serving the curated handbook "
                  "(the info scraper writes 'lt' on its daily tick)", lang)
        return None, None

    cutoff = datetime.now(timezone.utc) - timedelta(days=SCRAPED_MAX_AGE_DAYS)
    scraped = {}
    newest = None
    newest_at = None

    for row in rows:
        stamp = parse_timestamp(row["scraped_at"])
        if stamp is not None and stamp < cutoff:
            warn_once(f"stale-{lang}-{row['section']}",
                      "faculty_info '%s' section '%s' was scraped at %s, older than %d days — ignored",
                      lang, row["section"], row["scraped_at"], SCRAPED_MAX_AGE_DAYS)
            continue

        scraped[row["section"]] = row["data_json"]

        # Ranked on the parsed instant — an unparseable stamp
        # takes an empty slot but never beats a real one
        beats_newest = stamp is not None and (newest_at is None or stamp > newest_at)
        if row["scraped_at"] and (newest is None or beats_newest):
            newest = row["scraped_at"]
            newest_at = stamp

    if not scraped:
        return None, None
    return scraped, newest








############################################################
# clean_program
############################################################
#
# The item-shape floor for one scraped programme: a dict
# with a non-blank string name and degree, answered as a
# copy with both stripped; duration is OPTIONAL (the
# scraper writes it only when the programme card states
# one) and a blank or non-string value is dropped rather
# than served as an empty line. Anything else is None —
# the curated items never needed this, the scraped ones
# do (KNF-124: every scraped entry lacked duration while
# the client contract called it required). An entry whose
# name is admission vocabulary rather than a programme
# (info/programs.py — KNF-077) is None too, so rows the
# scraper stored before its own filter are cleaned here.
#
# Used by:
#   - apply_scraped_overlay (below) — the programmes floor
############################################################

def clean_program(entry):
    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    degree = entry.get("degree")
    if not (isinstance(name, str) and name.strip() and isinstance(degree, str) and degree.strip()):
        return None
    if not is_program_name(name):
        return None

    clean = dict(entry)
    clean["name"] = name.strip()
    clean["degree"] = degree.strip()
    duration = entry.get("duration")
    if isinstance(duration, str) and duration.strip():
        clean["duration"] = duration.strip()
    else:
        clean.pop("duration", None)
    return clean








############################################################
# localize_borrowed
############################################################
#
#   localize_borrowed({"programs": [...], ...}, "en")
#
# A copy of the 'lt' overlay made honest for a language
# that borrows it: each programme's degree word and its
# "N metai" duration are translated (an untranslatable
# duration is dropped — Lithuanian prose must not pose as
# English), the "(anglų k.)" suffix leaves the name as an
# English note, and every programme and contact group is
# marked nameLang "lt" — its registered name and headings
# stay Lithuanian, and the client labels them so and hands
# them to a Lithuanian screen-reader voice. general_contact
# (an address, a phone, an e-mail) is language-neutral and
# passes untouched. Entries that are not dicts pass through
# for apply_scraped_overlay's floors to judge.
#
# Used by:
#   - effective_handbook (below) — the borrowed branch
############################################################

def localize_borrowed(scraped, lang):
    borrowed = dict(scraped)
    degrees = _DEGREE_WORDS.get(lang, {})


    programs = scraped.get("programs")
    if isinstance(programs, list):
        localized = []
        for entry in programs:
            if not isinstance(entry, dict):
                localized.append(entry)
                continue
            item = dict(entry, nameLang="lt")

            degree = item.get("degree")
            if isinstance(degree, str):
                item["degree"] = degrees.get(degree.strip().lower(), degree)

            duration = item.pop("duration", None)
            match = _DURATION_RE.match(duration) if isinstance(duration, str) else None
            if match and lang == "en":
                whole, fraction = match.group(1), match.group(2)
                amount = f"{whole}.{fraction}" if fraction else whole
                item["duration"] = f"{amount} year" if amount == "1" else f"{amount} years"

            name = item.get("name")
            note = _TAUGHT_IN_ENGLISH_NOTE.get(lang)
            if isinstance(name, str) and note and _TAUGHT_IN_ENGLISH_RE.search(name):
                item["name"] = _TAUGHT_IN_ENGLISH_RE.sub("", name)
                item["note"] = note

            localized.append(item)
        borrowed["programs"] = localized


    contacts = scraped.get("contacts")
    if isinstance(contacts, list):
        borrowed["contacts"] = [dict(group, nameLang="lt") if isinstance(group, dict) else group
                                for group in contacts]

    return borrowed








############################################################
# apply_scraped_overlay
############################################################
#
# Lays the scraped sections over the curated dict in place.
# Every section earns it: contacts must be a list holding
# at least MIN_SCRAPED_CONTACT_ITEMS items in total,
# programs a list of MIN_SCRAPED_PROGRAMS entries that pass
# clean_program's item floor (the malformed ones are
# dropped first, so a blob of junk cannot clear the size
# floor), general_contact a non-empty dict. Anything else
# is skipped with one warning and the curated value stands
# — a half-scraped page must not replace the whole
# handbook, and a non-list blob would crash the Info
# screen.
#
# Used by:
#   - effective_handbook (below)
############################################################

def apply_scraped_overlay(data, scraped):
    contacts = scraped.get("contacts")
    if contacts is not None:
        if not isinstance(contacts, list):
            warn_once("contacts-shape",
                      "Scraped 'contacts' is %s, not a list — keeping the curated contacts",
                      type(contacts).__name__)
        else:
            items = sum(len(c["items"]) for c in contacts
                        if isinstance(c, dict) and isinstance(c.get("items"), list))
            if items < MIN_SCRAPED_CONTACT_ITEMS:
                warn_once("contacts-floor",
                          "Scraped 'contacts' holds %d item(s), under the floor of %d — keeping the curated contacts",
                          items, MIN_SCRAPED_CONTACT_ITEMS)
            else:
                data["contacts"] = contacts

    programs = scraped.get("programs")
    if programs is not None:
        if not isinstance(programs, list):
            warn_once("programs-shape",
                      "Scraped 'programs' is %s, not a list — keeping the curated programs",
                      type(programs).__name__)
        else:
            valid = [item for item in map(clean_program, programs) if item is not None]
            if len(valid) < len(programs):
                warn_once("programs-items",
                          "Scraped 'programs' carries %d malformed entry/entries — dropped",
                          len(programs) - len(valid))
            if len(valid) < MIN_SCRAPED_PROGRAMS:
                warn_once("programs-floor",
                          "Scraped 'programs' holds %d usable entry/entries, under the floor of %d — "
                          "keeping the curated programs",
                          len(valid), MIN_SCRAPED_PROGRAMS)
            else:
                data["programs"] = valid

    general = scraped.get("general_contact")
    if general is not None:
        if isinstance(general, dict) and general:
            data["general_contact"] = general
        else:
            warn_once("general-shape",
                      "Scraped 'general_contact' is %s, not a non-empty object — dropped",
                      type(general).__name__)








############################################################
# effective_handbook
############################################################
#
#   effective_handbook("en") → ({section: blob, ...}, updatedAt)
#
# THE effective handbook for one language, and the ONLY
# place it is assembled: the curated base from
# info/handbook.py, then whatever survives of the scrape
# laid over it through apply_scraped_overlay — the shape
# checks and the size floors, so a partial page load never
# hides a full curated list. A language with no scraped
# rows of its own borrows the 'lt' overlay (the scraper
# writes 'lt' only) through localize_borrowed — degree
# words and durations translated, the Lithuanian names
# flagged nameLang "lt" — its curated sections staying in
# their own language. The second value is the newest
# surviving scrape's stamp, or None when the curated base
# stands alone.
#
# Every consumer of "the handbook" — the Info screen's
# route and the assistant's knowledge base — calls this,
# so the two can never disagree: the English knowledge
# base carries the same complete programme list the
# English screen shows (registered names in Lithuanian,
# degree and duration in English). That parity is
# intended; the naming policy changes on both surfaces
# through this one function or not at all.
#
# Used by:
#   - get_faculty_info (below)
#   - assistant/chunking.py — handbook_chunks, the
#     knowledge-base rows
############################################################

def effective_handbook(lang):
    # STEP 1: the curated base — a copy, the module constant
    # is never overlaid in place
    # =====================================================
    data = dict(FACULTY_INFO[lang])


    # STEP 2: the overlay rows — this language's, else the
    # 'lt' ones the scraper actually writes
    # ====================================================
    scraped, updated_at = get_scraped_info(lang)
    if not scraped and lang != "lt":
        scraped, updated_at = get_scraped_info("lt")
        if scraped:
            scraped = localize_borrowed(scraped, lang)


    # STEP 3: the floors and the swap — never a bare update
    # =====================================================
    if scraped:
        apply_scraped_overlay(data, scraped)
    return data, updated_at








############################################################
# _overlay_signature
############################################################
#
#   _overlay_signature("en") → "lt:contacts@…,programs@…"
#
# What the effective handbook's scraped half depends on, read
# cheaply: the language whose rows would be laid over (the
# 'lt' fallback when this one has no fresh rows) and EVERY
# fresh (section, scraped_at) pair — the same staleness rule
# get_scraped_info applies. A row's content never changes
# without its scraped_at, so this names the overlay exactly:
# an older section ageing out changes it even while the
# newest stamp stays put (the newest stamp alone let that
# change answer a false 304). A failed read signs "db" — the
# answer then is the curated handbook, consistently.
#
# Used by:
#   - get_faculty_info (below) — the ETag seed, decided
#     before the handbook is built
############################################################

def _overlay_signature(lang):
    cutoff = datetime.now(timezone.utc) - timedelta(days=SCRAPED_MAX_AGE_DAYS)

    def fresh(code):
        pairs = []
        for section, scraped_at in FacultyInfo.objects.filter(lang=code).values_list("section", "scraped_at"):
            stamp = parse_timestamp(scraped_at)
            if stamp is not None and stamp < cutoff:
                continue
            pairs.append(f"{section}@{scraped_at}")
        return sorted(pairs)

    try:
        pairs, served = fresh(lang), lang
        if not pairs and lang != "lt":
            pairs, served = fresh("lt"), "lt"
    except DatabaseError:
        return "db"
    return f"{served}:{','.join(pairs)}"








############################################################
# get_faculty_info
############################################################
#
# GET /api/info?lang=lt|en&section= — the whole handbook or
# one section. ?lang is normalised before the whitelist
# ("EN-gb" is English; anything unknown silently becomes
# lt, and the answer says which it served). An unknown
# ?section is a 400 with the stable code. The answer adds
# "lang" and, when a scrape survived, "updatedAt".
#
# Used by:
#   - services/api/info.ts fetchFacultyInfo — the Info
#     screen (sends lang only)
############################################################

@require_methods("GET")
def get_faculty_info(request):
    # STEP 1: normalise ?lang — case and region subtag off
    # before the whitelist decides
    # ====================================================
    lang = (clean_param(request.GET.get("lang")) or "lt").strip().lower().replace("_", "-").split("-")[0]
    if lang not in FACULTY_INFO:
        lang = "lt"

    section = clean_param(request.GET.get("section")) or None


    # STEP 2: the ETag, decided BEFORE the handbook is built
    # (KNF-134) — the overlay's exact signature, not its
    # newest stamp; public, yet Vary on Authorization like
    # every API answer, so no cache anywhere keys an API body
    # on the bare URL
    # ======================================================
    seed = f"info|{lang}|{section}|{_overlay_signature(lang)}|{FALLBACK_VERSION}|{OVERLAY_POLICY}"
    tag = etag_for(seed)

    def with_cache_headers(response):
        response["ETag"] = f'W/"{tag}"'
        response["Vary"] = "Authorization, Accept-Encoding"
        response["Cache-Control"] = f"public, max-age={CACHE_MAX_AGE}"
        return response

    if if_none_match_contains(request.headers.get("If-None-Match"), tag):
        return with_cache_headers(HttpResponse(status=304))


    # STEP 3: the effective handbook — the one merge the
    # assistant's knowledge base is built from too
    # ==================================================
    data, updated_at = effective_handbook(lang)


    # STEP 4: one section or the whole handbook — an unknown
    # name is refused instead of quietly answering everything
    # =======================================================
    if section is not None and section not in data:
        return json_error("Unknown section", 400, code="unknown_section")

    payload = {section: data[section]} if section is not None else data
    payload["lang"] = lang
    if updated_at:
        # The frozen wire shape here is naive UTC — the
        # naive-stamp encoder below strips the offset
        payload["updatedAt"] = updated_at


    return with_cache_headers(json_response(payload, naive_stamps=True))
