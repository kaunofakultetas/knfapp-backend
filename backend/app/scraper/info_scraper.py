############################################################
#  [*] Info scraper — faculty contacts, programs, structure
#
#  Daily scrape of the static knf.vu.lt pages (front page,
#  contacts, structure, bachelor and master studies) into
#  the faculty_info table: one JSON blob per (lang, section)
#  for the sections 'contacts', 'programs' and
#  'general_contact'. Lithuanian ONLY — nothing ever writes
#  lang='en', so /api/info?lang=en never sees scraped data
#  and serves the hardcoded English dict from info/routes.py
#  untouched.
#
#  A section is upserted only when the run produced
#  something for it, so a page that fails to fetch leaves
#  the previous good blob in place — 'general_contact'
#  included: it is written only when the front page actually
#  yielded a field, merged over the previously stored blob
#  so a good value is never replaced by a hardcoded default.
#  info/routes.py overlays the blobs on its hardcoded
#  fallback at request time.
#
#  No section can fail the run any more: each extractor runs
#  in its own guard, so a dead contacts page costs the
#  contacts section and nothing else, and the run is closed
#  as 'completed' with the failed section named in
#  error_message.
#
#  Only institutional contact details are republished:
#  emails outside the *.vu.lt domains and phone numbers
#  outside the faculty's Kaunas switchboard prefix are
#  dropped before anything is stored — the endpoint serving
#  this is public.
#
#  Neighbours: scheduler.py runs scrape_faculty_info every
#  24 h (first run 60 s after boot) and scraper/routes.py
#  exposes it to admins as POST /api/scraper/info. Every
#  run lands in scraper_runs under source 'knf.vu.lt/info':
#  articles_found holds contacts + programs found, and
#  articles_new the number of SECTIONS whose blob actually
#  changed this run (the column names are the news
#  scraper's; /api/scraper/status also serves them under the
#  neutral itemsFound/itemsNew keys).
############################################################


import json
import logging
import re
import sqlite3
import threading
import uuid

from bs4 import BeautifulSoup

from app.database import get_db, utc_now_iso
from app.scraper.common import (
    KNF_HOSTS,
    deadline_passed,
    fetch,
    mark_run_failed,
    prune_scraper_runs,
    run_deadline,
    utc_now_naive,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://knf.vu.lt"

# Only the five pages something actually reads; 'about' and
# 'studies' used to be fetched and parsed on every run for
# nothing
INFO_PAGES = [
    {"url": f"{BASE_URL}", "type": "main"},
    {"url": f"{BASE_URL}/fakultetas/struktura", "type": "structure"},
    {"url": f"{BASE_URL}/fakultetas/kontaktai", "type": "contacts"},
    {"url": f"{BASE_URL}/studijos/bakalauro-studijos", "type": "bachelor"},
    {"url": f"{BASE_URL}/studijos/magistranturos-studijos", "type": "master"},
]

# The floor under general_contact: a scraped field wins over
# the previously stored one, which in turn wins over these
GENERAL_CONTACT_DEFAULTS = {
    "address": "Muitinės g. 8, LT-44280 Kaunas",
    "phone": "+370 37 422 523",
    "email": "knf@knf.vu.lt",
}

# Wall-clock budget for one run — five pages, generous
RUN_BUDGET_SECONDS = 300

# One info run at a time, whoever asked for it
_RUN_LOCK = threading.Lock()

# Academic title prefixes marking a staff entry. Matched
# with a word boundary so "dr." no longer fires inside
# "adr."; "vedėj-" carries no dot of its own
_STAFF_TITLE_RE = re.compile(r"\b(?:prof|doc|dr|lekt|asist)\.|\bvedėj", re.IGNORECASE)

# The two phone spellings these pages are written in, hoisted
# so _extract_phone and _faculty_phone_in read one source
_PHONE_INTERNATIONAL_RE = re.compile(r'(\+370[\s\-]*\d{1,2}[\s\-]*\d{3}[\s\-]*\d{3,4})')
_PHONE_NATIONAL_RE = re.compile(r'(\(8[\-\s]?\d{2,3}\)\s*\d{2}[\s\-]?\d{2}[\s\-]?\d{2})')

# Six or more digits running together — spaces, dashes,
# dots and brackets allowed between them — is somebody's
# number and never their job title
_NUMBER_RUN_RE = re.compile(r"(?:\d[\s\-().]*){6,}")

# The two general mailboxes worth publishing. Each carries a
# left-hand boundary so it cannot match from inside a longer
# local part — "administracija-knf@knf.vu.lt" is that
# department's address, not the faculty's. Case SENSITIVE:
# the pages write both in lower case
_KNF_MAILBOX_RE = re.compile(r'(?<![\w.+-])knf@[\w.-]+')
_INFO_MAILBOX_RE = re.compile(r'(?<![\w.+-])info@[\w.-]+')

# The tags the programme fallback walks, read twice per
# element: once for the element itself, once for whatever it
# wraps
_FALLBACK_TAGS = ["h3", "h4", "li", "strong"]








############################################################
# _fetch_page
############################################################
#
# One GET through the pooled session shared by all four
# scrapers — host allowlist, (connect, read) timeouts, a
# Content-Type check and a byte cap — parsed with lxml.
# Any failure is logged at WARNING and becomes None; every
# caller treats a missing soup as "skip this page", never
# as a failed run.
#
# Used by:
#   - scrape_faculty_info (below) — once per INFO_PAGES
#     entry
############################################################

def _fetch_page(url: str) -> BeautifulSoup | None:
    result = fetch(url, KNF_HOSTS)
    if not result:
        return None

    return BeautifulSoup(result[0], "lxml")








############################################################
# _extract_email
############################################################
#
# First email-looking token in the text, or None. The
# domain class eats a sentence-ending dot ("rašykite
# knf@knf.vu.lt." captures the stop as well), so trailing
# dots are trimmed back off — a mailto: built from the raw
# match was not a deliverable address.
#
# Used by:
#   - _scrape_contacts (below) — paragraph and table-row
#     items
#   - _scrape_staff (below) — also the staff-entry detector
#   - _scrape_general_contact (below) — the knf@/info@ pass
############################################################

def _extract_email(text: str) -> str | None:
    match = re.search(r'[\w.+-]+@[\w-]+\.[\w.-]+', text)
    return match.group(0).rstrip('.') if match else None








############################################################
# _institutional_email
############################################################
#
# The email back again when it belongs to the university
# (any *.vu.lt host, vu.lt itself included), otherwise None.
# Everything this scraper harvests ends up on an
# unauthenticated /api/info, so a private gmail address
# somebody left in a paragraph is not republished
# machine-readably.
#
# Used by:
#   - _scrape_contacts, _scrape_staff,
#     _scrape_general_contact (below) — before any email is
#     put in an item
############################################################

def _institutional_email(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None

    domain = email.rsplit("@", 1)[1].lower()
    return email if domain == "vu.lt" or domain.endswith(".vu.lt") else None








############################################################
# _faculty_phone
############################################################
#
# The phone back again when it is a Kaunas landline — the
# faculty switchboard's own prefix, area code 37, in either
# the "+370 37 …" or the "(8-37) …" spelling — otherwise
# None. A lecturer's mobile number found in a paragraph is
# personal data and is not republished.
#
# Used by:
#   - _scrape_contacts, _scrape_staff,
#     _scrape_general_contact (below) — before any phone is
#     put in an item
############################################################

def _faculty_phone(phone: str | None) -> str | None:
    if not phone:
        return None

    digits = re.sub(r"\D", "", phone)
    if digits.startswith("370"):
        digits = digits[3:]
    elif digits.startswith("8"):
        digits = digits[1:]

    return phone if digits.startswith("37") else None








############################################################
# _extract_phone
############################################################
#
# First Lithuanian phone number in the text, or None. Two
# shapes, tried in order: the international
# "+370 XX XXX XXX" (inner whitespace runs collapsed to one
# space, dashes kept as written) and the old national
# "(8-XX) XX XX XX" — the latter only WITH the parentheses.
# A bare "8 37 ..." or "370..." without the plus is not a
# phone to this function.
#
# The international separators are runs and not single
# characters: a number split across elements
# ("<span>+370</span> <span>37 422 523</span>") reaches the
# unstripped front-page text with " \n " in the gap, and a
# one-character class silently dropped the switchboard
# number the whole general_contact block hangs on.
#
# Used by:
#   - _scrape_contacts (below)
#   - _scrape_staff (below)
############################################################

def _extract_phone(text: str) -> str | None:
    match = _PHONE_INTERNATIONAL_RE.search(text)
    if match:
        return re.sub(r'\s+', ' ', match.group(1).strip())
    match = _PHONE_NATIONAL_RE.search(text)
    if match:
        return match.group(1).strip()
    return None








############################################################
# _faculty_phone_in
############################################################
#
# The first FACULTY landline in the text, or None: every
# number either spelling can find is offered to
# _faculty_phone and the first it accepts wins, in the same
# order _extract_phone tries them (international, then the
# old national form).
#
# _extract_phone answers the first number and nothing else,
# so a footer listing the university's Vilnius switchboard
# above the faculty's Kaunas landline lost the landline
# entirely and general_contact fell back to a hardcoded
# default. Only the general-contact block needs this — the
# contacts and staff walks read one entry's own line, where
# there is nothing to look past.
#
# Used by:
#   - _scrape_general_contact (below) — the contact block
############################################################

def _faculty_phone_in(text: str) -> str | None:
    for match in _PHONE_INTERNATIONAL_RE.finditer(text):
        phone = _faculty_phone(re.sub(r'\s+', ' ', match.group(1).strip()))
        if phone:
            return phone

    for match in _PHONE_NATIONAL_RE.finditer(text):
        phone = _faculty_phone(match.group(1).strip())
        if phone:
            return phone

    return None








############################################################
# _scrape_contacts
############################################################
#
# Turns the contacts page into [{category, items}] groups:
# each h2/h3/h4 opens a category and the paragraphs, list
# items, divs and table rows after it that carry an
# institutional email or a faculty phone become its items
# ({name, phone?, email?, room?}). Entries found before the
# first heading go under an implicit "Kontaktai" rather than
# being thrown away. Categories with no items are dropped,
# and the result is [] when the soup is None (a failed
# fetch) or none of the known content selectors match — the
# caller then keeps whatever contacts blob a previous run
# stored.
#
# None-safe like its siblings _scrape_programs /
# _scrape_staff / _scrape_general_contact: a dead contacts
# page costs this section only, never the whole run.
#
# find_all yields NESTED matches in document order, so a
# <div> wrapping several entry <p>s used to produce one
# glued-together item AND every inner <p>'s item. Only LEAF
# candidates are read now — an element containing another
# candidate is a wrapper and is skipped — and the items of a
# category are deduplicated on (name, email).
#
# Name extraction: the text before the email (or before the
# phone when there is no email), minus a trailing "," or
# ":"; under 2 characters it falls back to the first 60
# characters of the whole text.
#
# Headings are flattened with a space separator like the
# entries under them, so "Dekanatas ir
# <strong>administracija</strong>" names its category with
# the space the page wrote and not "Dekanatas
# iradministracija".
#
# Used by:
#   - scrape_faculty_info (below) — on the 'contacts' soup
############################################################

def _scrape_contacts(soup: BeautifulSoup | None) -> list[dict]:
    contacts = []

    if not soup:
        return contacts


    # STEP 1: pick the content container — first selector that
    # matches wins, the same list every page scraper here uses
    # ========================================================
    content_el = None
    for selector in [".article-content", ".item-page", "#content", "article"]:
        content_el = soup.select_one(selector)
        if content_el:
            break

    if not content_el:
        return contacts


    # STEP 2: walk the container in document order, grouping
    # every entry under the most recent heading — the walk
    # starts inside an implicit category so nothing written
    # above the first heading is lost
    # ======================================================
    entry_tags = ["p", "div", "tr", "li"]
    current_category = "Kontaktai"
    current_items = []
    seen_keys = set()

    for el in content_el.find_all(["h2", "h3", "h4"] + entry_tags):
        tag = el.name

        if tag in ("h2", "h3", "h4"):
            # STEP 2.1: heading — flush the previous category if it has items
            if current_category and current_items:
                contacts.append({
                    "category": current_category,
                    "items": current_items,
                })
            current_category = el.get_text(separator=" ", strip=True)
            current_items = []
            seen_keys = set()
            continue

        # STEP 2.2: wrappers are skipped — only the innermost
        # candidate carries one entry's text
        if el.find(entry_tags) is not None:
            continue

        if tag == "tr":
            # STEP 2.3: table row — cell 0 is the name, the rest is searched
            cells = el.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            name = cells[0].get_text(separator=" ", strip=True)
            rest = " ".join(c.get_text(separator=" ", strip=True) for c in cells[1:])
            email = _institutional_email(_extract_email(rest))
            phone = _faculty_phone(_extract_phone(rest))
            if not name or not (email or phone):
                continue
            item = {"name": name[:100]}
            if phone:
                item["phone"] = phone
            if email:
                item["email"] = email

        else:
            # STEP 2.4: free-text entry — kept only with a
            # publishable email or phone
            text = el.get_text(separator=" ", strip=True)
            if not text or len(text) < 5:
                continue

            raw_email = _extract_email(text)
            raw_phone = _extract_phone(text)
            email = _institutional_email(raw_email)
            phone = _faculty_phone(raw_phone)
            if not (email or phone):
                continue

            name = text
            if raw_email:
                name = text.split(raw_email)[0].strip().rstrip(",").rstrip(":")
            if raw_phone and not name:
                name = text.split(raw_phone)[0].strip().rstrip(",").rstrip(":")
            if not name or len(name) < 2:
                name = text[:60]

            # room = a 3-digit number next to kab/kabinetas/room
            # in either order; 1- and 2-digit rooms are missed
            room = None
            room_match = re.search(r'(\d{3})\s*(?:kab|kabinetas|room)', text, re.IGNORECASE)
            if not room_match:
                room_match = re.search(r'(?:kab|kabinetas|room)[\.\s]*(\d{3})', text, re.IGNORECASE)
            if room_match:
                room = room_match.group(1)

            item = {"name": name.strip()[:100]}
            if phone:
                item["phone"] = phone
            if email:
                item["email"] = email
            if room:
                item["room"] = room

        # STEP 2.5: one entry per (name, email) inside a category
        key = (item["name"], item.get("email"))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        current_items.append(item)


    # STEP 3: flush the category still open when the walk ends
    # ========================================================
    if current_category and current_items:
        contacts.append({
            "category": current_category,
            "items": current_items,
        })

    return contacts








############################################################
# _scrape_programs
############################################################
#
# Study programs from the bachelor and master pages as
# [{name, degree, duration?}]. The link pass keeps every
# anchor whose href mentions /studij or /program with more
# than 8 chars of text, minus "daugiau" ("more") links.
#
# The heading/list fallback is per PAGE now: a master page
# that lists no program links still gets its fallback even
# when the bachelor page yielded plenty. seen_names, by
# contrast, is shared across both pages, so a program listed
# on both is stored once. It applies the link pass's
# "daugiau" filter too, and reads LEAF candidates only the
# way _scrape_contacts and _scrape_staff do — a <li> around
# a <strong> is one programme, not two.
#
# degree comes from the programme card's own wording when it
# says so, otherwise from the page the programme was listed
# on — the bachelor listing IS the bachelor degree, which is
# evidence rather than an assumption. duration is parsed
# from the card ("4 metai", "3,5 metų") and OMITTED when the
# page does not state it: the old fixed "4 metai" / "2
# metai" published factually wrong course lengths for every
# programme that runs longer or shorter.
#
# A page that failed to fetch (None) or matches no content
# selector is skipped silently.
#
# Used by:
#   - scrape_faculty_info (below) — the 'bachelor' and
#     'master' soups
############################################################

def _scrape_programs(bachelor_soup: BeautifulSoup | None,
                     master_soup: BeautifulSoup | None) -> list[dict]:
    programs = []
    seen_names = set()

    for soup, page_degree in [
        (bachelor_soup, "Bakalauras"),
        (master_soup, "Magistras"),
    ]:
        if not soup:
            continue


        # STEP 1: the content container, same selector list as
        # the other page scrapers
        # ====================================================
        content_el = None
        for selector in [".article-content", ".item-page", "#content", "article"]:
            content_el = soup.select_one(selector)
            if content_el:
                break

        if not content_el:
            continue


        # STEP 2: link pass — program pages are linked from the
        # listing, so anchor text is the cleanest name source
        # =====================================================
        from_this_page = []

        for link in content_el.find_all("a", href=True):
            href = link["href"]
            text = link.get_text(strip=True)
            if ("/studij" in href or "/program" in href) and len(text) > 8:
                name = text.strip()
                if name.lower() not in seen_names and "daugiau" not in name.lower():
                    seen_names.add(name.lower())
                    # The card around the link is where a duration
                    # or an explicit degree is written, if anywhere
                    from_this_page.append(_program_entry(name, page_degree, _card_text(link)))


        # STEP 3: fallback to headings/list items mentioning
        # "studij" — gated on THIS page's result, so one page
        # yielding links no longer silences the other's fallback
        # ======================================================
        if not from_this_page:
            for el in content_el.find_all(_FALLBACK_TAGS):
                name = _fallback_programme(el)
                if not name:
                    continue

                # An element wrapping another programme label is
                # the listing around it, not an entry of its own:
                # a <li> holding a <strong> published the same
                # programme twice, the outer copy with its words
                # glued together
                if any(_fallback_programme(inner) for inner in el.find_all(_FALLBACK_TAGS)):
                    continue

                if name.lower() not in seen_names:
                    seen_names.add(name.lower())
                    from_this_page.append(_program_entry(name, page_degree, name))

        programs.extend(from_this_page)

    return programs








############################################################
# _fallback_programme
############################################################
#
# The programme name one heading or list item states, or
# None when it states none: the text has to be longer than
# ten characters, mention "studij", and not be the "daugiau"
# ("more") navigation label the link pass already drops —
# "Daugiau apie studijas" is a link to the rest of the
# listing, not a study programme.
#
# Flattened with a space separator, so a card written as
# "<li><strong>Informatikos studijų programa</strong>
# nuolatinės</li>" no longer glues its words together.
#
# Used by:
#   - _scrape_programs (above) — the heading/list fallback,
#     once for the element itself and again for everything
#     it wraps, which is how a wrapper is recognised
############################################################

def _fallback_programme(el) -> str | None:
    text = el.get_text(separator=" ", strip=True)
    lowered = text.lower()

    if len(text) > 10 and "studij" in lowered and "daugiau" not in lowered:
        return text

    return None








############################################################
# _card_text
############################################################
#
# The text of the programme card around a listing link: the
# nearest ancestor that holds noticeably more than the link
# itself (up to three levels up), else the link's own text.
# That is where "4 metai", "3,5 metų" or an explicit degree
# is written, when the page states it at all.
#
# Used by:
#   - _scrape_programs (above) — one call per program link
############################################################

def _card_text(link) -> str:
    text = link.get_text(separator=" ", strip=True)

    parent = link.parent
    for _ in range(3):
        if parent is None:
            break

        # A container holding a second programme link is the
        # LISTING, not this card: its text describes other
        # programmes and would hand them each other's duration
        if len(parent.find_all("a", href=True)) > 1:
            break

        candidate = parent.get_text(separator=" ", strip=True)
        # A few characters more is just whitespace or a bullet;
        # a real card carries a whole label like "3,5 metai"
        if len(candidate) > len(text) + 3:
            return candidate

        parent = parent.parent

    return text








############################################################
# _program_entry
############################################################
#
# One {name, degree, duration?} entry. The degree is taken
# from the card's own wording when it names one
# ("magistrantūros studijos"), else from the page the
# programme was listed on. The duration is only present when
# the card actually states it — "4 metai", "3,5 metų",
# "2 m." — so the app shows nothing rather than a wrong
# course length.
#
# Used by:
#   - _scrape_programs (above) — both the link pass and the
#     heading fallback
############################################################

def _program_entry(name: str, page_degree: str, card_text: str) -> dict:
    entry = {"name": name, "degree": page_degree}

    lowered = (card_text or "").lower()
    if "magistrant" in lowered or "magistro" in lowered:
        entry["degree"] = "Magistras"
    elif "bakalaur" in lowered:
        entry["degree"] = "Bakalauras"

    # The lookbehind keeps a year out of it: "2026 m." must not
    # become a six-year programme
    duration = re.search(r"(?<!\d)(\d(?:[.,]\d)?)\s*(?:metai|met[ųu]|m\.)", lowered)
    if duration:
        entry["duration"] = f"{duration.group(1)} metai"

    return entry








############################################################
# _scrape_staff
############################################################
#
# Departments and their people from the structure page as
# [{department, staff: [{name, email?, phone?,
# position?}]}]. Each h2/h3/h4 opens a department; a
# <p>/<li> under it is a staff entry when it is under 200
# chars and either carries an institutional email or has a
# Lithuanian academic title (prof., doc., dr., lekt.,
# asist., vedėj-) AND reads like a person's name. The title
# is matched on a word boundary, so "dr." no longer fires
# inside "adr.", and a line that merely mentions a title
# without a name-shaped token is not filed as a person.
# Only LEAF candidates are read — a <p> inside an <li> used
# to be visited twice, once through each — and "a" is no
# longer walked (it never had a branch).
#
# name is the text up to the first comma, academic title
# INCLUDED ("Prof. dr. Jonas Jonaitis"). position is what
# stands AFTER the first comma ("Dekanė", "katedros
# vedėja") — the role the page actually states, instead of
# the old reconstruction that copied the name back into the
# field, and only when that part is a role at all: an email
# or a phone number standing there is contact detail and is
# dropped, mobile numbers included (see _contact_detail).
# The text is flattened with a space separator, so
# "<strong>Prof. dr.</strong>Jonas" no longer glues.
#
# Departments with no staff are dropped; None or a page
# without a known content container yields [].
#
# Used by:
#   - scrape_faculty_info (below) — on the 'structure' soup,
#     merged into the contacts blob as extra categories
############################################################

def _scrape_staff(structure_soup: BeautifulSoup | None) -> list[dict]:
    departments = []

    if not structure_soup:
        return departments


    # STEP 1: the content container, same selector list as
    # the other page scrapers
    # ====================================================
    content_el = None
    for selector in [".article-content", ".item-page", "#content", "article"]:
        content_el = structure_soup.select_one(selector)
        if content_el:
            break

    if not content_el:
        return departments


    # STEP 2: walk headings and paragraphs, grouping people
    # under the most recent heading
    # =====================================================
    current_dept = None
    current_staff = []

    for el in content_el.find_all(["h2", "h3", "h4", "p", "li"]):
        tag = el.name
        text = el.get_text(separator=" ", strip=True)

        if tag in ("h2", "h3", "h4") and text:
            # STEP 2.1: heading — flush the previous department if it has people
            if current_dept and current_staff:
                departments.append({
                    "department": current_dept,
                    "staff": current_staff,
                })
            current_dept = text
            current_staff = []
            continue

        # STEP 2.2: wrappers are skipped — only the innermost
        # element carries one person's line
        if el.find(["p", "li"]) is not None:
            continue

        # STEP 2.3: candidate entry — a publishable email, or a
        # real academic title next to something name-shaped
        if not text or len(text) < 3 or len(text) >= 200:
            continue

        email = _institutional_email(_extract_email(text))
        phone = _faculty_phone(_extract_phone(text))
        titled = _STAFF_TITLE_RE.search(text) is not None

        if not (email or (titled and _name_shaped(text))):
            continue

        parts = [part.strip() for part in text.split(",")]
        entry = {"name": parts[0][:100]}
        if email:
            entry["email"] = email
        if phone:
            entry["phone"] = phone

        # The role written after the first comma, when that is
        # what it is — never the contact details following it
        if len(parts) > 1 and parts[1] and not _contact_detail(parts[1]):
            entry["position"] = parts[1][:100]

        current_staff.append(entry)


    # STEP 3: flush the department still open when the walk ends
    # ==========================================================
    if current_dept and current_staff:
        departments.append({
            "department": current_dept,
            "staff": current_staff,
        })

    return departments








############################################################
# _name_shaped
############################################################
#
# True when the first few tokens look like a person's name —
# at least two capitalised words among them, which
# "Prof. dr. Jonas Jonaitis" has and "Studijų dalykų
# aprašai" does not. Paired with the academic-title test so
# a sentence that merely mentions a title is not filed as a
# staff member.
#
# Used by:
#   - _scrape_staff (above)
############################################################

def _name_shaped(text: str) -> bool:
    words = [word for word in re.split(r"[\s,]+", text) if word]
    capitalised = [word for word in words[:5] if word[:1].isupper()]

    return len(capitalised) >= 2








############################################################
# _contact_detail
############################################################
#
# True when a comma-part of a staff line is contact details
# rather than a role: an address, or a number — either of
# _extract_phone's two spellings, or any run of six or more
# digits, which is what a mobile written "+370 612 34567"
# or "8 612 34567" leaves behind.
#
# The digit test is the point. _faculty_phone refuses to put
# a lecturer's mobile in the `phone` field because it is
# personal data, and the position slot was republishing the
# very same number verbatim on the unauthenticated
# /api/info. A room ("302 kab.") or a year stays a position:
# three or four digits are not a phone number.
#
# Used by:
#   - _scrape_staff (above) — the position guard
############################################################

def _contact_detail(part: str) -> bool:
    if "@" in part:
        return True

    return bool(_extract_phone(part) or _NUMBER_RUN_RE.search(part))








############################################################
# _general_mailbox
############################################################
#
# The faculty's general mailbox somewhere in one block of
# text, or None. knf@… wins over info@…, and EVERY match of
# a pattern is tested for being institutional rather than
# only the first: a private "knf@gmail.com" standing earlier
# in the document used to hide the real "knf@knf.vu.lt"
# further down, and the email field was dropped altogether.
#
# Both patterns carry a left-hand boundary, so an address
# that merely ENDS in the mailbox name
# ("administracija-knf@knf.vu.lt") is that department's own
# address and not the general one — publishing the truncated
# "knf@knf.vu.lt" put an address on /api/info that the page
# never stated.
#
# Used by:
#   - _scrape_general_contact (below) — the contact block
#     first, then the whole page
############################################################

def _general_mailbox(text: str) -> str | None:
    for pattern in (_KNF_MAILBOX_RE, _INFO_MAILBOX_RE):
        for match in pattern.finditer(text):
            email = _institutional_email(match.group(0).rstrip('.'))
            if email:
                return email

    return None








############################################################
# _scrape_general_contact
############################################################
#
# The faculty's main contact fields for the info screen's
# footer block, as a dict holding ONLY what this page
# actually yielded — {} when the page failed to fetch or
# nothing matched. That is what lets the caller's "non-empty
# sections only" rule protect general_contact the way it
# already protects contacts and programs: a bad scrape no
# longer overwrites good stored values with hardcoded
# defaults (the caller fills the gaps from the stored blob
# and GENERAL_CONTACT_DEFAULTS).
#
# The phone is searched in the footer / contact block only,
# not the whole front page, and has to be a faculty landline
# — the first number anywhere on the page was routinely
# something else entirely. Every number in that block is
# tried, not just the first: the university's Vilnius
# switchboard is often written above the faculty's own.
# The email prefers knf@…, then info@…, must be
# institutional, and is likewise the first ACCEPTED match
# rather than the first match.
#
# Used by:
#   - scrape_faculty_info (below) — on the 'main' soup
############################################################

def _scrape_general_contact(main_soup: BeautifulSoup | None) -> dict:
    info = {}

    if not main_soup:
        return info


    # STEP 1: two texts \u2014 the whole page for the address (the
    # template writes it wherever it likes) and the footer /
    # contact block for the number
    # =======================================================
    # newline-joined so the address regex's [^,\n]* stops at
    # element boundaries, while its \s* still lets the postcode
    # sit in the next element
    text = main_soup.get_text(separator="\n")

    contact_el = None
    for selector in ["footer", ".footer", "#footer", "[class*='contact']", "[id*='contact']"]:
        contact_el = main_soup.select_one(selector)
        if contact_el:
            break
    contact_text = contact_el.get_text(separator="\n") if contact_el else ""


    # STEP 2: "Muitinės g. <nr> ..., LT-<5 digits> Kaunas"
    # ===================================================
    addr_match = re.search(r'(Muitin\u0117s\s+g\.?\s*\d+[^,\n]*,?\s*(?:LT-)?\d{5}\s*Kaunas)', text)
    if addr_match:
        info["address"] = addr_match.group(1).strip()


    # STEP 3: the switchboard number, from the contact block
    # only — the first number the faculty gate ACCEPTS, so a
    # Vilnius number written above it no longer hides it
    # ======================================================
    phone = _faculty_phone_in(contact_text)
    if phone:
        info["phone"] = phone


    # STEP 4: knf@ wins over info@, and the contact block wins
    # over the rest of the page
    # ========================================================
    for haystack in (contact_text, text):
        email = _general_mailbox(haystack)
        if email:
            info["email"] = email
            break

    return info








############################################################
# scrape_faculty_info
############################################################
#
# One full run: fetch every INFO_PAGES entry, extract
# contacts / programs / staff / general contact, fold the
# staff departments into the contacts list as extra
# categories, and upsert the non-empty sections as the
# Lithuanian faculty_info blobs. Bracketed by a scraper_runs
# row ('knf.vu.lt/info': running → completed/failed):
# articles_found holds contacts + programs found and
# articles_new the number of SECTIONS whose stored blob
# actually changed, so the status page finally says
# something true for this source. Returns {pages_scraped,
# contacts_found, programs_found}, plus "error" on failure
# (the route turns that into a 500; the scheduler only logs
# it).
#
# Every extractor runs inside its own guard: a section that
# throws is logged, named in the run row's error_message and
# skipped, while the rest of the run still lands. The run is
# only 'failed' when the bookkeeping itself fails. All four
# extractors are None-safe, so a page that did not download
# simply costs its own section.
#
# scraped_at is a NAIVE UTC ISO string with a 'T', matching
# the house shape; no reader compares it (info/routes.py
# selects section, data_json and scraped_at for the
# staleness check).
#
# Used by:
#   - scraper/scheduler.py — run_info_scraper, every 24 h
#     and once 60 s after boot
#   - scraper/routes.py — trigger_info_scrape, the admin
#     POST /api/scraper/info (nothing in the mobile app
#     calls it)
############################################################

def scrape_faculty_info() -> dict:
    # STEP 1: one info run at a time; then mint the run id, open
    # the DB and register the run as 'running' first, so a crash
    # further down still has a row to mark failed
    # ==========================================================
    if not _RUN_LOCK.acquire(blocking=False):
        logger.info("Faculty info scrape already running — this trigger is skipped")
        return {"pages_scraped": 0, "contacts_found": 0, "programs_found": 0, "skipped": True}

    run_id = str(uuid.uuid4())
    db = get_db()
    deadline = run_deadline(RUN_BUDGET_SECONDS)

    try:
        db.execute(
            "INSERT INTO scraper_runs (id, source, status, started_at) VALUES (?, 'knf.vu.lt/info', 'running', ?)",
            (run_id, utc_now_iso()),
        )
        db.commit()


        # STEP 2: fetch every page; a failed one is stored as None
        # (not counted) but keeps its slot for the .get()s below
        # ========================================================
        soups: dict[str, BeautifulSoup | None] = {}
        pages_scraped = 0

        for page in INFO_PAGES:
            if deadline_passed(deadline):
                logger.warning("Faculty info scrape out of time after %d page(s)", pages_scraped)
                break
            soup = _fetch_page(page["url"])
            soups[page["type"]] = soup
            if soup:
                pages_scraped += 1


        # STEP 3: extract — each section in its own guard, so one
        # dead page degrades to a skipped section
        # =======================================================
        contacts, contacts_error = _extract_section(
            "contacts", _scrape_contacts, soups.get("contacts"))
        programs, programs_error = _extract_section(
            "programs", _scrape_programs, soups.get("bachelor"), soups.get("master"))
        staff, staff_error = _extract_section(
            "staff", _scrape_staff, soups.get("structure"))
        general, general_error = _extract_section(
            "general_contact", _scrape_general_contact, soups.get("main"))

        contacts = contacts or []
        programs = programs or []
        staff = staff or []
        general = general or {}
        section_errors = [
            message for message in
            (contacts_error, programs_error, staff_error, general_error)
            if message
        ]


        # STEP 4: fold departments into the contacts list as extra
        # categories — position passes through, staff never has a
        # room, and an empty department contributes nothing
        # ========================================================
        if staff:
            for dept in staff:
                items = []
                for s in dept["staff"]:
                    item = {"name": s["name"]}
                    if "email" in s:
                        item["email"] = s["email"]
                    if "phone" in s:
                        item["phone"] = s["phone"]
                    if "position" in s:
                        item["position"] = s["position"]
                    items.append(item)
                if items:
                    contacts.append({
                        "category": dept["department"],
                        "items": items,
                    })

        contacts_found = sum(len(c.get("items", [])) for c in contacts)
        programs_found = len(programs)


        # STEP 5: assemble the Lithuanian blobs — a section is
        # written only when non-empty, which is what keeps a
        # previous good blob alive through a bad scrape.
        # general_contact now obeys that rule too: its fields
        # are laid over the stored blob, which is itself laid
        # over the hardcoded defaults
        # ======================================================
        now = utc_now_naive().isoformat()

        scraped_data_lt = {}
        if contacts:
            scraped_data_lt["contacts"] = contacts
        if programs:
            scraped_data_lt["programs"] = programs
        if general:
            scraped_data_lt["general_contact"] = {
                **GENERAL_CONTACT_DEFAULTS,
                **_stored_section(db, "lt", "general_contact"),
                **general,
            }

        changed_sections = _store_info(db, "lt", scraped_data_lt, now) if scraped_data_lt else 0


        # STEP 6: close the run — articles_found counts what was
        # found, articles_new the sections that actually changed;
        # a section that threw is named in error_message without
        # failing the run. Then the 30-day scraper_runs prune
        # =======================================================
        db.execute(
            """UPDATE scraper_runs
               SET status = 'completed', articles_found = ?, articles_new = ?,
                   error_message = ?, finished_at = ?
               WHERE id = ?""",
            (contacts_found + programs_found, changed_sections,
             "; ".join(section_errors) or None, utc_now_iso(), run_id),
        )
        db.commit()

        prune_scraper_runs(db)

        result = {
            "pages_scraped": pages_scraped,
            "contacts_found": contacts_found,
            "programs_found": programs_found,
            # Additive: the row an admin looks up in
            # GET /api/scraper/status
            "runId": run_id,
        }
        logger.info("Faculty info scrape complete: %s (%d section(s) changed)", result, changed_sections)
        return result

    except Exception as e:
        # Roll the pending section writes back before recording
        # the failure, and record it on a FRESH connection: the
        # one in hand is exactly what may have broken
        logger.exception("Faculty info scraper error")
        try:
            db.rollback()
        except sqlite3.Error:
            logger.warning("Rollback after the info scrape failure did not take", exc_info=True)
        mark_run_failed(run_id, str(e))
        return {"pages_scraped": 0, "contacts_found": 0, "programs_found": 0,
                "error": str(e), "runId": run_id}
    finally:
        db.close()
        _RUN_LOCK.release()








############################################################
# _extract_section
############################################################
#
# Runs one extractor and answers (value, error message).
# A section that throws is logged with its traceback and
# reported as an error string instead of taking the whole
# run down with it — before this a single dead page (the
# contacts one) failed 62 runs out of 62 and cost the
# programs and general contact fetched alongside it.
#
# Used by:
#   - scrape_faculty_info (above) — once per section
############################################################

def _extract_section(name: str, extractor, *args):
    try:
        return extractor(*args), None
    except Exception as e:
        logger.exception("Faculty info section '%s' failed", name)
        return None, f"{name}: {e}"








############################################################
# _stored_section
############################################################
#
# The blob a previous run stored for one (lang, section), as
# a dict — {} when there is none or it does not decode. Used
# to keep a good general_contact field alive through a run
# that only matched some of them.
#
# Used by:
#   - scrape_faculty_info (above) — the general_contact merge
############################################################

def _stored_section(db, lang: str, section: str) -> dict:
    row = db.execute(
        "SELECT data_json FROM faculty_info WHERE lang = ? AND section = ?",
        (lang, section),
    ).fetchone()
    if not row:
        return {}

    try:
        stored = json.loads(row["data_json"])
    except (ValueError, TypeError):
        return {}

    return stored if isinstance(stored, dict) else {}








############################################################
# _store_info
############################################################
#
# Upserts one blob per section for a language and answers
# how many sections actually CHANGED — the number
# scrape_faculty_info reports as articles_new, which is the
# only figure that means anything for this source. The blob
# is compared with the stored one before writing; an
# unchanged section still has its scraped_at refreshed
# (info/routes.py drops sections it considers stale), it
# just does not count as new.
#
# Per section: SELECT the (lang, section) row, UPDATE it in
# place (its id survives re-scrapes) or INSERT a fresh uuid4
# row — done by hand rather than leaning on the
# UNIQUE(lang, section) constraint. One commit at the end
# covers every section; a failure mid-loop leaves the
# earlier ones pending, and the caller's except handler now
# rolls those back instead of committing them.
# ensure_ascii=False keeps Lithuanian letters readable in
# DbGate instead of \uXXXX escapes.
#
# Used by:
#   - scrape_faculty_info (above) — once per run, lang "lt"
############################################################

def _store_info(db, lang: str, data: dict, scraped_at: str) -> int:
    changed = 0

    for section, section_data in data.items():
        data_json = json.dumps(section_data, ensure_ascii=False)
        existing = db.execute(
            "SELECT id, data_json FROM faculty_info WHERE lang = ? AND section = ?",
            (lang, section),
        ).fetchone()

        if existing:
            if existing["data_json"] != data_json:
                changed += 1
            db.execute(
                "UPDATE faculty_info SET data_json = ?, scraped_at = ? WHERE id = ?",
                (data_json, scraped_at, existing["id"]),
            )
        else:
            changed += 1
            db.execute(
                "INSERT INTO faculty_info (id, lang, section, data_json, scraped_at) VALUES (?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), lang, section, data_json, scraped_at),
            )

    db.commit()
    return changed
