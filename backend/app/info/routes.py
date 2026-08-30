############################################################
#  [*] Info — faculty handbook: contacts, links, hours, FAQ
#
#  The "about the faculty" payload behind the Info screen:
#  contact groups, quick links, opening hours, study
#  programs and FAQ, bilingual (lt first, en). FACULTY_INFO
#  below is the hardcoded fallback; on top of it the route
#  lays whatever scraper/info_scraper.py stored in the
#  faculty_info table (migration v4) — scraped contacts and
#  programs REPLACE the hardcoded lists, a scraped
#  general_contact block is added, and links/hours/faq are
#  always the hardcoded ones because knf.vu.lt has nothing
#  to scrape for them. The info scraper only ever writes
#  lang 'lt' (60 s after boot, then every 24 h); a language
#  with no rows of its own borrows the 'lt' ones — the
#  scraped values are names, rooms, phones and emails,
#  language-neutral in practice — while its hardcoded
#  links/hours/faq stay in that language.
#
#  What a scraped blob has to clear before it is served:
#  it must not be older than SCRAPED_MAX_AGE_DAYS, it must
#  have the right SHAPE (list of categories / list of
#  programs / dict), and it must clear a size floor — a
#  degraded scrape used to replace the curated handbook with
#  two contacts, and a contacts blob that was not a list
#  crashed the Info screen outright. Whatever survives is
#  dated by an additive "updatedAt", and the answer echoes
#  the "lang" it actually served.
#
#  Public, no auth. The app caches the answer on its side
#  (services/cache.ts, cacheKeyInfo) and refetches on a
#  language switch; a weak ETag plus Cache-Control turns a
#  relaunch into a 304 with no body.
#
#    GET /api/info — the handbook for ?lang, or one ?section
############################################################


import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, make_response, request

from app.database import get_db

logger = logging.getLogger(__name__)

# The info scraper ticks every 24 h (scraper/scheduler.py), so
# a client copy may live exactly that long — the mobile app
# keeps its own week-long copy on top (services/cache.ts).
CACHE_MAX_AGE = 24 * 3600

# A blob this old means the scraper has been failing for a
# month (30 missed 24 h ticks); the curated fallback below is
# the fresher answer at that point.
SCRAPED_MAX_AGE_DAYS = 30

# Floors a scraped overlay must clear before it may hide the
# curated lists — the scraper regularly returns a single
# category after a partial page load.
MIN_SCRAPED_CONTACT_ITEMS = 5
MIN_SCRAPED_PROGRAMS = 3

# Conditions already warned about in this process (_warn_once)
_warned = set()

info_bp = Blueprint("info", __name__)








############################################################
# FACULTY_INFO
############################################################
#
# The hardcoded fallback handbook, {lang: {section: ...}},
# in exactly the shape services/api/info.ts types as
# FacultyInfoResponse: contacts → [{category, items:
# [{name, phone?, email?, room?}]}], links [{title, url,
# icon}], hours [{place, address, schedule, note}],
# programs [{name, degree, duration}], faq [{q, a}].
# Lithuanian diacritics are spelled as \u escapes, so the
# literals decode to the real text but a grep for
# "Studijų" will NOT find them. Phone numbers, rooms and
# hours are the pre-scrape defaults — after the first
# successful info scrape only links/hours/faq (and every
# section for 'en') still come from this dict. The two
# language keys double as the ?lang whitelist.
#
# Used by:
#   - get_faculty_info (below) — the base of every answer
############################################################

FACULTY_INFO = {
    "lt": {
        "contacts": [
            {
                "category": "Dekanatas",
                "items": [
                    {"name": "Dekano priimamasis", "phone": "+370 37 422 523", "email": "knf@knf.vu.lt", "room": "101"},
                    {"name": "Studij\u0173 skyrius", "phone": "+370 37 422 604", "email": "studijos@knf.vu.lt", "room": "102"},
                    {"name": "Prodekan\u0117 studijoms", "phone": "+370 37 422 604", "email": "studijos@knf.vu.lt", "room": "103"},
                ]
            },
            {
                "category": "Katedros",
                "items": [
                    {"name": "Informatikos katedra", "phone": "+370 37 422 530", "email": "informatika@knf.vu.lt", "room": "301"},
                    {"name": "Verslo katedra", "phone": "+370 37 422 529", "email": "verslas@knf.vu.lt", "room": "201"},
                    {"name": "Socialini\u0173 moksl\u0173 katedra", "phone": "+370 37 422 528", "email": "socialiniai@knf.vu.lt", "room": "205"},
                ]
            },
            {
                "category": "Paslaugos",
                "items": [
                    {"name": "Biblioteka", "phone": "+370 37 422 535", "email": "biblioteka@knf.vu.lt", "room": "111"},
                    {"name": "IT pagalba", "phone": "+370 37 422 540", "email": "it@knf.vu.lt", "room": "315"},
                    {"name": "Student\u0173 atstovyb\u0117", "email": "sa@knf.vu.lt", "room": "110"},
                ]
            },
        ],
        "links": [
            {"title": "KNF svetain\u0117", "url": "https://knf.vu.lt", "icon": "globe"},
            {"title": "VU svetain\u0117", "url": "https://www.vu.lt", "icon": "school"},
            {"title": "VU informacin\u0117 sistema (VU IS)", "url": "https://is.vu.lt", "icon": "laptop"},
            {"title": "VU el. pa\u0161tas", "url": "https://mail.vu.lt", "icon": "mail"},
            {"title": "VU Moodle (VMA)", "url": "https://vma.vu.lt", "icon": "book"},
            {"title": "VU biblioteka", "url": "https://biblioteka.vu.lt", "icon": "library"},
            {"title": "Kauno fakulteto Facebook", "url": "https://www.facebook.com/VUKaunoFakultetas", "icon": "share-social"},
            {"title": "Akademin\u0117 etika", "url": "https://www.vu.lt/studijos/studentams/akademine-etika", "icon": "document-text"},
        ],
        "hours": [
            {"place": "Fakulteto pastatas", "address": "Muitin\u0117s g. 8, Kaunas", "schedule": "I-V 07:00-21:00, VI 08:00-16:00", "note": "\u012e\u0117jimas su studento pa\u017eym\u0117jimu po 19:00"},
            {"place": "Biblioteka", "address": "111 kab.", "schedule": "I-V 09:00-18:00", "note": "Skaitykla atvira iki 20:00"},
            {"place": "Valgykla / Kavin\u0117", "address": "1 auk\u0161tas", "schedule": "I-V 08:00-16:00", "note": ""},
            {"place": "IT laboratorijos", "address": "3 auk\u0161tas", "schedule": "I-V 08:00-20:00", "note": "Laisva prieiga su studento ID"},
        ],
        "programs": [
            {"name": "Informatikos ir skaitmeninio turinio studij\u0173 kryptis", "degree": "Bakalauras", "duration": "4 metai"},
            {"name": "Verslo ir vadybos studij\u0173 kryptis", "degree": "Bakalauras", "duration": "4 metai"},
            {"name": "Socialinio darbo studij\u0173 kryptis", "degree": "Bakalauras", "duration": "4 metai"},
            {"name": "Informacini\u0173 sistem\u0173 in\u017einerija", "degree": "Magistras", "duration": "2 metai"},
            {"name": "Verslo administravimas", "degree": "Magistras", "duration": "2 metai"},
        ],
        "faq": [
            {
                "q": "Kaip gauti studento pa\u017eym\u0117jim\u0105?",
                "a": "Studento pa\u017eym\u0117jim\u0105 galite atsiimti Studij\u0173 skyriuje (102 kab.) pirm\u0105j\u0105 studij\u0173 savait\u0119. Reikia tur\u0117ti asmens dokument\u0105."
            },
            {
                "q": "Kaip prisijungti prie VU Wi-Fi?",
                "a": "Naudokite tinkl\u0105 \"eduroam\". Prisijungimo vardas: jusu.vardas@stud.vu.lt, slapta\u017eodis - VU IS slapta\u017eodis."
            },
            {
                "q": "Kur rasti savo tvarkara\u0161t\u012f?",
                "a": "Tvarkara\u0161tis skelbiamas VU informacin\u0117je sistemoje (is.vu.lt) ir \u0161ioje program\u0117l\u0117je skiltyje \"Tvarkara\u0161tis\"."
            },
            {
                "q": "Kaip gauti bendrabut\u012f?",
                "a": "Pra\u0161ymus bendrabu\u010diui teikite per VU IS. Pirmakursiai turi prioritet\u0105. Daugiau informacijos - Studij\u0173 skyriuje."
            },
            {
                "q": "Kur yra student\u0173 atstovyb\u0117?",
                "a": "Student\u0173 atstovyb\u0117 yra 110 kabinete (1 auk\u0161tas). Kreipkit\u0117s d\u0117l student\u0173 veiklos, rengini\u0173 ir problem\u0173 sprendimo."
            },
            {
                "q": "Kaip gauti stipendij\u0105?",
                "a": "Stipendijos skiriamos pagal studij\u0173 rezultatus. Socialin\u0117s stipendijos teikiamos per Valstybin\u012f studij\u0173 fond\u0105 (vsf.lrv.lt). Informacija - Studij\u0173 skyriuje."
            },
            {
                "q": "K\u0105 daryti, jei negaliu atvykti \u012f paskait\u0105?",
                "a": "Informuokite d\u0117stytoj\u0105 el. pa\u0161tu i\u0161 anksto. Ilgesniam neatvykimui reikalingas pateisinantis dokumentas Studij\u0173 skyriui."
            },
        ],
    },
    "en": {
        "contacts": [
            {
                "category": "Dean's Office",
                "items": [
                    {"name": "Dean's Reception", "phone": "+370 37 422 523", "email": "knf@knf.vu.lt", "room": "101"},
                    {"name": "Studies Department", "phone": "+370 37 422 604", "email": "studijos@knf.vu.lt", "room": "102"},
                    {"name": "Vice-Dean for Studies", "phone": "+370 37 422 604", "email": "studijos@knf.vu.lt", "room": "103"},
                ]
            },
            {
                "category": "Departments",
                "items": [
                    {"name": "Department of Informatics", "phone": "+370 37 422 530", "email": "informatika@knf.vu.lt", "room": "301"},
                    {"name": "Department of Business", "phone": "+370 37 422 529", "email": "verslas@knf.vu.lt", "room": "201"},
                    {"name": "Department of Social Sciences", "phone": "+370 37 422 528", "email": "socialiniai@knf.vu.lt", "room": "205"},
                ]
            },
            {
                "category": "Services",
                "items": [
                    {"name": "Library", "phone": "+370 37 422 535", "email": "biblioteka@knf.vu.lt", "room": "111"},
                    {"name": "IT Support", "phone": "+370 37 422 540", "email": "it@knf.vu.lt", "room": "315"},
                    {"name": "Student Council", "email": "sa@knf.vu.lt", "room": "110"},
                ]
            },
        ],
        "links": [
            {"title": "KNF Website", "url": "https://knf.vu.lt", "icon": "globe"},
            {"title": "VU Website", "url": "https://www.vu.lt", "icon": "school"},
            {"title": "VU Information System (VU IS)", "url": "https://is.vu.lt", "icon": "laptop"},
            {"title": "VU Email", "url": "https://mail.vu.lt", "icon": "mail"},
            {"title": "VU Moodle (VLE)", "url": "https://vma.vu.lt", "icon": "book"},
            {"title": "VU Library", "url": "https://biblioteka.vu.lt", "icon": "library"},
            {"title": "Kaunas Faculty Facebook", "url": "https://www.facebook.com/VUKaunoFakultetas", "icon": "share-social"},
            {"title": "Academic Ethics", "url": "https://www.vu.lt/studijos/studentams/akademine-etika", "icon": "document-text"},
        ],
        "hours": [
            {"place": "Faculty Building", "address": "Muitines g. 8, Kaunas", "schedule": "Mon-Fri 07:00-21:00, Sat 08:00-16:00", "note": "Student ID required for entry after 19:00"},
            {"place": "Library", "address": "Room 111", "schedule": "Mon-Fri 09:00-18:00", "note": "Reading room open until 20:00"},
            {"place": "Cafeteria", "address": "1st floor", "schedule": "Mon-Fri 08:00-16:00", "note": ""},
            {"place": "IT Labs", "address": "3rd floor", "schedule": "Mon-Fri 08:00-20:00", "note": "Free access with student ID"},
        ],
        "programs": [
            {"name": "Informatics and Digital Content", "degree": "Bachelor's", "duration": "4 years"},
            {"name": "Business and Management", "degree": "Bachelor's", "duration": "4 years"},
            {"name": "Social Work", "degree": "Bachelor's", "duration": "4 years"},
            {"name": "Information Systems Engineering", "degree": "Master's", "duration": "2 years"},
            {"name": "Business Administration", "degree": "Master's", "duration": "2 years"},
        ],
        "faq": [
            {
                "q": "How do I get my student ID card?",
                "a": "Pick up your student ID at the Studies Department (room 102) during the first week. Bring a personal ID document."
            },
            {
                "q": "How do I connect to VU Wi-Fi?",
                "a": "Use the 'eduroam' network. Login: your.name@stud.vu.lt, password - your VU IS password."
            },
            {
                "q": "Where can I find my timetable?",
                "a": "The timetable is published in VU Information System (is.vu.lt) and in this app under the 'Schedule' tab."
            },
            {
                "q": "How do I apply for a dormitory?",
                "a": "Submit applications through VU IS. First-year students have priority. More info at the Studies Department."
            },
            {
                "q": "Where is the Student Council?",
                "a": "The Student Council is in room 110 (1st floor). Contact them about student activities, events, and issue resolution."
            },
            {
                "q": "How do I get a scholarship?",
                "a": "Scholarships are awarded based on academic performance. Social scholarships are available through the State Studies Fund (vsf.lrv.lt). Details at the Studies Department."
            },
            {
                "q": "What if I can't attend a lecture?",
                "a": "Notify your lecturer by email in advance. For longer absences, a supporting document must be submitted to the Studies Department."
            },
        ],
    },
}








############################################################
# _warn_once
############################################################
#
# logger.warning the first time a key shows up in this
# process, silence afterwards. /api/info is public and hot,
# and every condition warned about here LASTS (an empty
# faculty_info table, a blob stale for weeks, a scrape that
# keeps failing its floor), so a plain warning would write
# one line per request. A restart says it again.
#
# Used by:
#   - _get_scraped_info, _apply_scraped_overlay (below)
############################################################

def _warn_once(key, message, *args):
    if key in _warned:
        return

    _warned.add(key)
    logger.warning(message, *args)








############################################################
# _parse_timestamp
############################################################
#
# One stored timestamp → an aware UTC datetime, or None when
# it is missing or unparseable. Three shapes reach this:
# info_scraper's naive utcnow().isoformat(), the aware
# database.utc_now_iso() form, and the legacy space-form
# datetime('now') default that migration v17 normalised (old
# backups still carry it). A naive value is read as UTC —
# every writer here means UTC.
#
# Used by:
#   - _get_scraped_info (below) — the staleness cutoff
############################################################

def _parse_timestamp(value):
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(value.replace(" ", "T").replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed








############################################################
# _get_scraped_info
############################################################
#
# The faculty_info rows (migration v4, UNIQUE(lang,
# section)) for one language as ({section: decoded
# data_json}, newest scraped_at), or (None, None) when there
# is nothing usable. Rows are written by
# scraper/info_scraper.py (_store_info): lang 'lt' only,
# sections contacts / programs / general_contact.
#
# "Newest" is decided on the PARSED instant while the raw
# string is what comes back for display — the stored shapes
# do not sort against each other (' ' < 'T', so a legacy
# space-form stamp lost to every T-form one from the same
# day, and updatedAt then dated the handbook by the OLDER
# scrape). A stamp nothing can parse still fills the slot
# while no parseable one has claimed it.
#
# What gets dropped, each with a once-per-process warning:
# an empty table (the scraper has never succeeded), a blob
# older than SCRAPED_MAX_AGE_DAYS, and a row whose JSON does
# not parse. Only sqlite3.Error is caught — it logs the
# traceback and falls back to the curated handbook, while an
# unexpected exception now propagates to the app's 500
# handler instead of being swallowed by a bare
# `except Exception` that made a broken query look exactly
# like "the scraper never ran".
#
# Used by:
#   - get_faculty_info (below)
############################################################

def _get_scraped_info(lang: str) -> tuple[dict | None, str | None]:
    # STEP 1: the rows — the connection is opened INSIDE the
    # try, so a failed connect/PRAGMA falls back too instead
    # of 500ing the whole handbook
    # ======================================================
    db = None
    try:
        db = get_db()
        rows = db.execute(
            "SELECT section, data_json, scraped_at FROM faculty_info WHERE lang = ?",
            (lang,),
        ).fetchall()

        if not rows:
            _warn_once(
                f"empty-{lang}",
                "faculty_info holds no '%s' rows — serving the curated handbook "
                "(the info scraper writes 'lt' 60 s after boot, then every 24 h)",
                lang,
            )
            return None, None


        # STEP 2: decode what is still fresh, remembering the
        # newest stamp for the response's updatedAt — `newest`
        # is the raw string the answer carries, `newest_at` the
        # instant it parsed to, which is what ranks the rows
        # =====================================================
        cutoff = datetime.now(timezone.utc) - timedelta(days=SCRAPED_MAX_AGE_DAYS)
        scraped = {}
        newest = None
        newest_at = None

        for row in rows:
            stamp = _parse_timestamp(row["scraped_at"])
            if stamp is not None and stamp < cutoff:
                _warn_once(
                    f"stale-{lang}-{row['section']}",
                    "faculty_info '%s' section '%s' was scraped at %s, older than %d days — ignored",
                    lang, row["section"], row["scraped_at"], SCRAPED_MAX_AGE_DAYS,
                )
                continue

            try:
                scraped[row["section"]] = json.loads(row["data_json"])
            # TypeError would be a NULL data_json — the column is NOT NULL,
            # so only the JSONDecodeError branch ever fires
            except (json.JSONDecodeError, TypeError):
                _warn_once(
                    f"undecodable-{lang}-{row['section']}",
                    "faculty_info '%s' section '%s' does not hold valid JSON — ignored",
                    lang, row["section"],
                )
                continue

            # Ranked on the parsed instant, never on the stored string:
            # ' ' < 'T', so a space-form stamp from a pre-v17 backup lost
            # to a T-form one from the same day however much later it was
            # written. A stamp that would not parse takes an empty slot —
            # updatedAt keeps failing open — but never beats a real one
            beats_newest = stamp is not None and (newest_at is None or stamp > newest_at)
            if row["scraped_at"] and (newest is None or beats_newest):
                newest = row["scraped_at"]
                newest_at = stamp

        if not scraped:
            return None, None

        return scraped, newest
    except sqlite3.Error:
        logger.exception("faculty_info lookup failed for lang '%s' — serving the curated handbook", lang)
        return None, None
    finally:
        if db is not None:
            db.close()








############################################################
# _apply_scraped_overlay
############################################################
#
# Lays the scraped sections over the curated `data` dict in
# place. Every section has to earn it: contacts must be a
# list of category blocks holding at least
# MIN_SCRAPED_CONTACT_ITEMS items in total, programs a list
# of at least MIN_SCRAPED_PROGRAMS entries, general_contact a
# non-empty dict. Anything else is skipped with a
# once-per-process warning and the curated value stands —
# the overlay used to gate on truthiness alone, so a
# half-scraped page replaced the whole contacts list and a
# blob that was not a list crashed the Info screen (the
# mobile FacultyInfoResponse types contacts as an array and
# maps over it).
#
# Used by:
#   - get_faculty_info (below)
############################################################

def _apply_scraped_overlay(data, scraped):
    # STEP 1: contacts — right shape, and enough of it to be
    # worth hiding the curated list behind
    # ======================================================
    contacts = scraped.get("contacts")
    if contacts is not None:
        if not isinstance(contacts, list):
            _warn_once(
                "contacts-shape",
                "Scraped 'contacts' is %s, not a list — keeping the curated contacts",
                type(contacts).__name__,
            )
        else:
            items = sum(
                len(c["items"])
                for c in contacts
                if isinstance(c, dict) and isinstance(c.get("items"), list)
            )
            if items < MIN_SCRAPED_CONTACT_ITEMS:
                _warn_once(
                    "contacts-floor",
                    "Scraped 'contacts' holds %d item(s), under the floor of %d — keeping the curated contacts",
                    items, MIN_SCRAPED_CONTACT_ITEMS,
                )
            else:
                data["contacts"] = contacts


    # STEP 2: programs — same idea, its own floor
    # ===========================================
    programs = scraped.get("programs")
    if programs is not None:
        if not isinstance(programs, list):
            _warn_once(
                "programs-shape",
                "Scraped 'programs' is %s, not a list — keeping the curated programs",
                type(programs).__name__,
            )
        elif len(programs) < MIN_SCRAPED_PROGRAMS:
            _warn_once(
                "programs-floor",
                "Scraped 'programs' holds %d entry/entries, under the floor of %d — keeping the curated programs",
                len(programs), MIN_SCRAPED_PROGRAMS,
            )
        else:
            data["programs"] = programs


    # STEP 3: general_contact — no curated counterpart, so it
    # is added only when the scrape produced a real dict
    # =======================================================
    general = scraped.get("general_contact")
    if general is not None:
        if isinstance(general, dict) and general:
            data["general_contact"] = general
        else:
            _warn_once(
                "general-shape",
                "Scraped 'general_contact' is %s, not a non-empty object — dropped",
                type(general).__name__,
            )








############################################################
# _conditional_json
############################################################
#
# Wraps a payload in a cacheable response: a weak ETag over
# `seed` (the newest scrape stamp, the curated handbook's own
# fingerprint and the query that shaped the answer) plus
# Cache-Control: public, max-age=CACHE_MAX_AGE. A matching
# If-None-Match answers 304 with no body — the handbook is
# identical bytes between two 24 h scrapes. The seed is built
# from DATA, never from the response body: app/__init__.py's
# escape_json_output hook re-serialises the body after the
# view returns, so a body hash would describe bytes this
# function never sees.
#
# Used by:
#   - get_faculty_info (below)
############################################################

# Fingerprint of the curated handbook, computed once at import:
# a deploy that edits FACULTY_INFO has to move the ETag even
# when no scraped row changed
_FALLBACK_VERSION = hashlib.sha256(
    json.dumps(FACULTY_INFO, sort_keys=True, ensure_ascii=False).encode("utf-8")
).hexdigest()[:8]


def _conditional_json(payload, seed):
    tag = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]

    if request.if_none_match.contains_weak(tag):
        response = make_response("", 304)
    else:
        response = make_response(jsonify(payload))

    response.set_etag(tag, weak=True)
    response.headers["Cache-Control"] = f"public, max-age={CACHE_MAX_AGE}"

    return response








############################################################
# get_faculty_info
############################################################
#
# GET /api/info
#
# Query: lang=lt|en and an optional section=<key>. The answer
# is FACULTY_INFO for the language with the scraped
# faculty_info rows laid over it (see
# _apply_scraped_overlay: contacts and programs replace the
# curated lists once they clear their shape and size checks,
# general_contact is added when a scrape found one,
# links/hours/faq stay curated), plus two additive keys —
# "lang", the language actually served, and "updatedAt", the
# newest scrape stamp behind the overlay (absent when
# nothing scraped survived).
#
# Gotchas:
#   - ?lang is NORMALISED before the whitelist check ("EN",
#     "en-GB", "en_GB" → "en"), and anything still unknown
#     silently becomes lt — never a 400, but the answer now
#     says which language it is
#   - ?section=<unknown> is a 400 {"error", "code"} instead
#     of the whole handbook under the caller's nose. Valid
#     names are the payload's own keys: contacts, links,
#     hours, programs, faq and general_contact (that last
#     only after a scrape found one). "staff" is NOT a
#     section — info_scraper.py folds staff into contacts
#   - a matching section answers {section: ..., lang,
#     updatedAt}, a different shape from the full payload,
#     so a caller has to know which one it asked for
#   - info_scraper.py stores lang 'lt' only, so ?lang=en
#     borrows the 'lt' scraped rows (contacts / programs /
#     general_contact hold proper nouns, rooms and numbers —
#     effectively language-neutral) rather than freezing on
#     the hardcoded English defaults; the English links /
#     hours / faq stay hardcoded either way
#
# Used by:
#   - services/api/info.ts — fetchFacultyInfo (lang only;
#     nothing in the app sends ?section) → the Info screen
#   - swagger/swagger.yaml documents both params
############################################################

@info_bp.route("", methods=["GET"])
def get_faculty_info():
    # STEP 1: normalise ?lang — case and region subtag off
    # before the whitelist decides, so "EN-gb" is English
    # ====================================================
    lang = (request.args.get("lang") or "lt").strip().lower().replace("_", "-").split("-")[0]
    if lang not in FACULTY_INFO:
        lang = "lt"

    section = request.args.get("section") or None


    # STEP 2: the curated base plus whatever survives of the
    # scrape. Shallow copy on purpose: the overlay swaps whole
    # keys, so the module-level dict is never mutated
    # ========================================================
    data = dict(FACULTY_INFO[lang])

    scraped, updated_at = _get_scraped_info(lang)
    # No rows for this language — borrow the 'lt' ones the scraper
    # actually writes (names/rooms/phones, language-neutral) instead
    # of serving stale hardcoded data forever
    if not scraped and lang != "lt":
        scraped, updated_at = _get_scraped_info("lt")
    if scraped:
        _apply_scraped_overlay(data, scraped)


    # STEP 3: one section or the whole handbook — an unknown
    # name is refused instead of quietly answering with
    # everything under a shape the caller did not ask for
    # ======================================================
    if section is not None and section not in data:
        return jsonify({"error": "Unknown section", "code": "unknown_section"}), 400

    payload = {section: data[section]} if section is not None else data
    payload["lang"] = lang
    if updated_at:
        payload["updatedAt"] = updated_at


    # STEP 4: answer through the ETag — between two 24 h
    # scrapes this is the same handbook every time
    # ==================================================
    seed = f"info|{lang}|{section}|{updated_at or '-'}|{_FALLBACK_VERSION}"

    return _conditional_json(payload, seed)
