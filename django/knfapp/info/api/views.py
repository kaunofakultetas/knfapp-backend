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
#  so ?lang=en borrows the 'lt' overlay (names, rooms and
#  numbers are language-neutral) while its curated sections
#  stay English.
#
#  Split into:
#
#    warn_once / parse_timestamp — process-lifetime helpers
#    get_scraped_info            — the surviving overlay rows
#    apply_scraped_overlay       — the floors and the swap
#    get_faculty_info            — GET /api/info
############################################################


import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone


from django.db import Error as DatabaseError
from django.http import HttpResponse


from knfapp.common.http import etag_for, if_none_match_contains, json_error, json_response
from knfapp.info.handbook import FACULTY_INFO
from knfapp.info.models import FacultyInfo


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
#   - get_faculty_info (below)
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
# apply_scraped_overlay
############################################################
#
# Lays the scraped sections over the curated dict in place.
# Every section earns it: contacts must be a list holding
# at least MIN_SCRAPED_CONTACT_ITEMS items in total,
# programs a list of MIN_SCRAPED_PROGRAMS entries,
# general_contact a non-empty dict. Anything else is
# skipped with one warning and the curated value stands —
# a half-scraped page must not replace the whole handbook,
# and a non-list blob would crash the Info screen.
#
# Used by:
#   - get_faculty_info (below)
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
        elif len(programs) < MIN_SCRAPED_PROGRAMS:
            warn_once("programs-floor",
                      "Scraped 'programs' holds %d entry/entries, under the floor of %d — keeping the curated programs",
                      len(programs), MIN_SCRAPED_PROGRAMS)
        else:
            data["programs"] = programs

    general = scraped.get("general_contact")
    if general is not None:
        if isinstance(general, dict) and general:
            data["general_contact"] = general
        else:
            warn_once("general-shape",
                      "Scraped 'general_contact' is %s, not a non-empty object — dropped",
                      type(general).__name__)








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

def get_faculty_info(request):
    # STEP 1: normalise ?lang — case and region subtag off
    # before the whitelist decides
    # ====================================================
    lang = (request.GET.get("lang") or "lt").strip().lower().replace("_", "-").split("-")[0]
    if lang not in FACULTY_INFO:
        lang = "lt"

    section = request.GET.get("section") or None


    # STEP 2: the curated base plus whatever survives of the
    # scrape; a language with no rows borrows the 'lt' overlay
    # ========================================================
    data = dict(FACULTY_INFO[lang])
    scraped, updated_at = get_scraped_info(lang)
    if not scraped and lang != "lt":
        scraped, updated_at = get_scraped_info("lt")
    if scraped:
        apply_scraped_overlay(data, scraped)


    # STEP 3: one section or the whole handbook — an unknown
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


    # STEP 4: the ETag — same handbook between two daily scrapes
    # ==========================================================
    seed = f"info|{lang}|{section}|{updated_at or '-'}|{FALLBACK_VERSION}"
    tag = etag_for(seed)
    if if_none_match_contains(request.headers.get("If-None-Match"), tag):
        response = HttpResponse(status=304)
    else:
        response = json_response(payload, naive_stamps=True)
    response["ETag"] = f'W/"{tag}"'
    response["Cache-Control"] = f"public, max-age={CACHE_MAX_AGE}"
    return response
