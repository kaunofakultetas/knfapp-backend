############################################################
#  [*] Info — faculty handbook: contacts, links, hours, FAQ
#
#  The "about the faculty" payload behind the Info screen:
#  contact groups, quick links, opening hours, study
#  programs and FAQ, bilingual (lt first, en). FACULTY_INFO
#  below is the hardcoded fallback; on top of it the route
#  lays whatever scraper/info_scraper.py stored in the
#  faculty_info table (migration v4) — scraped contacts and
#  programs REPLACE the hardcoded lists wholesale, a scraped
#  general_contact block is added, and links/hours/faq are
#  always the hardcoded ones because knf.vu.lt has nothing
#  to scrape for them. The info scraper only ever writes
#  lang 'lt' (60 s after boot, then every 24 h), so English
#  is the fallback data, always.
#
#  Public, no auth. The app caches the answer on its side
#  (services/cache.ts, cacheKeyInfo) and refetches on a
#  language switch.
#
#    GET /api/info — the handbook for ?lang, or one ?section
############################################################


import json

from flask import Blueprint, request

from app.database import get_db

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
# _get_scraped_info
############################################################
#
# The faculty_info rows (migration v4, UNIQUE(lang,
# section)) for one language as {section: decoded
# data_json}, or None when there are no rows, none of them
# decode, or the query itself throws — the bare
# `except Exception` swallows EVERYTHING silently (missing
# table, locked file), no log line, so a broken query only
# ever shows up as "the scraped data never appears" while
# the route keeps answering from the hardcoded dict. A row
# whose JSON does not parse is skipped, not fatal. Rows are
# written by scraper/info_scraper.py (_store_info): lang
# 'lt' only, sections contacts / programs / general_contact.
#
# Used by:
#   - get_faculty_info (below)
############################################################

def _get_scraped_info(lang: str) -> dict | None:
    db = get_db()
    try:
        rows = db.execute(
            "SELECT section, data_json FROM faculty_info WHERE lang = ?",
            (lang,),
        ).fetchall()
        if not rows:
            return None
        scraped = {}
        for row in rows:
            try:
                scraped[row["section"]] = json.loads(row["data_json"])
            # TypeError would be a NULL data_json — the column is NOT NULL,
            # so only the JSONDecodeError branch ever fires
            except (json.JSONDecodeError, TypeError):
                continue
        return scraped if scraped else None
    except Exception:
        return None
    finally:
        db.close()








############################################################
# get_faculty_info
############################################################
#
# GET /api/info
#
# Query: lang=lt|en (anything else silently becomes lt) and
# an optional section=<key>. The answer is FACULTY_INFO for
# the language with the scraped faculty_info rows laid over
# it: scraped "contacts" and "programs" replace the
# hardcoded lists wholesale when non-empty (no merge — an
# empty scraped list keeps the fallback), "general_contact"
# is added when a scrape found one, links/hours/faq stay
# hardcoded. Returns a plain dict, so Flask jsonifies it.
#
# Gotchas:
#   - info_scraper.py stores lang 'lt' only, so ?lang=en is
#     always the hardcoded English fallback
#   - ?section=<unknown> is not an error: the filter is
#     skipped and the WHOLE handbook comes back. The old
#     docstring listed "staff" as a section — it is not a
#     key; info_scraper.py folds staff into contacts
#   - a matching section answers {section: [...]}, a
#     different shape from the full payload, so a caller has
#     to know which one it asked for
#
# Used by:
#   - services/api/info.ts — fetchFacultyInfo (lang only;
#     nothing in the app sends ?section) → the Info screen
#   - swagger/swagger.yaml documents both params
############################################################

@info_bp.route("", methods=["GET"])
def get_faculty_info():
    lang = request.args.get("lang", "lt")
    if lang not in FACULTY_INFO:
        lang = "lt"

    section = request.args.get("section")

    # Shallow copy on purpose: the overlay swaps whole keys, so the
    # module-level dict is never mutated across requests
    data = dict(FACULTY_INFO[lang])

    scraped = _get_scraped_info(lang)
    if scraped:
        # Replace, never merge — a non-empty scraped list hides the
        # hardcoded entries completely
        if "contacts" in scraped and scraped["contacts"]:
            data["contacts"] = scraped["contacts"]
        if "programs" in scraped and scraped["programs"]:
            data["programs"] = scraped["programs"]
        # No hardcoded counterpart — the key only exists after a scrape
        # found one (FacultyInfoResponse types it optional)
        if "general_contact" in scraped:
            data["general_contact"] = scraped["general_contact"]

    # An unknown section is not a 404 — the whole handbook comes back
    if section and section in data:
        return {section: data[section]}

    return data
