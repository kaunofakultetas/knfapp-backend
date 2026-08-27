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
#  the previous good blob in place. 'general_contact' is
#  the exception: it starts from hardcoded defaults and is
#  written on every run, even one where zero pages loaded.
#  info/routes.py overlays the blobs on its hardcoded
#  fallback at request time.
#
#  Neighbours: scheduler.py runs scrape_faculty_info every
#  24 h (first run 60 s after boot) and scraper/routes.py
#  exposes it to admins as POST /api/scraper/info. Every
#  run lands in scraper_runs under source 'knf.vu.lt/info',
#  with contacts + programs counted in the news-shaped
#  articles_found/articles_new columns (always equal here).
############################################################


import json
import logging
import re
import uuid
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from app.database import get_db

logger = logging.getLogger(__name__)

BASE_URL = "https://knf.vu.lt"
USER_AGENT = "KNFAPP/1.0 (Vilnius University Kaunas Faculty Mobile App)"
REQUEST_TIMEOUT = 20  # seconds, per page

# Every entry is fetched on every run and counted in
# pages_scraped, but scrape_faculty_info only ever reads the
# 'main', 'contacts', 'structure', 'bachelor' and 'master'
# soups — 'about' and 'studies' are two wasted requests per
# run whose HTML is parsed and dropped.
INFO_PAGES = [
    {"url": f"{BASE_URL}", "type": "main"},
    {"url": f"{BASE_URL}/fakultetas", "type": "about"},
    {"url": f"{BASE_URL}/fakultetas/struktura", "type": "structure"},
    {"url": f"{BASE_URL}/fakultetas/kontaktai", "type": "contacts"},
    {"url": f"{BASE_URL}/studijos", "type": "studies"},
    {"url": f"{BASE_URL}/studijos/bakalauro-studijos", "type": "bachelor"},
    {"url": f"{BASE_URL}/studijos/magistranturos-studijos", "type": "master"},
]








############################################################
# _fetch_page
############################################################
#
# One GET with the app's User-Agent and a 20 s timeout,
# parsed with lxml. Any requests failure — timeout, DNS,
# a non-2xx status via raise_for_status — is logged at
# WARNING and becomes None; the callers treat a missing
# soup as "skip this page", never as a failed run (with
# one exception, see _scrape_contacts).
#
# Used by:
#   - scrape_faculty_info (below) — once per INFO_PAGES
#     entry
############################################################

def _fetch_page(url: str) -> BeautifulSoup | None:
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={
            "User-Agent": USER_AGENT,
        })
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "lxml")
    except requests.RequestException as e:
        logger.warning("Failed to fetch %s: %s", url, e)
        return None








############################################################
# _extract_email
############################################################
#
# First email-looking token in the text, or None. The
# domain class [\w.-]+ happily eats a sentence-ending dot,
# so "rašykite knf@knf.vu.lt." yields "knf@knf.vu.lt." —
# nothing downstream trims it.
#
# Used by:
#   - _scrape_contacts (below) — paragraph and table-row
#     items
#   - _scrape_staff (below) — also the staff-entry detector
############################################################

def _extract_email(text: str) -> str | None:
    match = re.search(r'[\w.+-]+@[\w-]+\.[\w.-]+', text)
    return match.group(0) if match else None








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
# Used by:
#   - _scrape_contacts (below)
#   - _scrape_staff (below)
#   - _scrape_general_contact (below) — over the WHOLE front
#     page text, so the first number on the page wins
############################################################

def _extract_phone(text: str) -> str | None:
    match = re.search(r'(\+370[\s\-]?\d{1,2}[\s\-]?\d{3}[\s\-]?\d{3,4})', text)
    if match:
        return re.sub(r'\s+', ' ', match.group(1).strip())
    match = re.search(r'(\(8[\-\s]?\d{2,3}\)\s*\d{2}[\s\-]?\d{2}[\s\-]?\d{2})', text)
    if match:
        return match.group(1).strip()
    return None








############################################################
# _scrape_contacts
############################################################
#
# Turns the contacts page into [{category, items}] groups:
# each h2/h3/h4 opens a category and the paragraphs, list
# items, divs and table rows after it that carry an email
# or a phone become its items ({name, phone?, email?,
# room?}). Categories with no items are dropped, and the
# result is [] when none of the known content selectors
# match — the caller then keeps whatever contacts blob a
# previous run stored.
#
# NOT None-safe, unlike its siblings: the parameter is typed
# BeautifulSoup but scrape_faculty_info passes
# soups.get("contacts"), which is None when the contacts
# page failed to fetch — soup.select_one then raises
# AttributeError and the WHOLE run is marked failed.
#
# find_all yields NESTED matches in document order, so a
# <div> wrapping several entry <p>s is seen first with all
# their text concatenated: it produces one item whose name
# is everything before the first email (capped at 100
# chars), and then every inner <p> produces its own — no
# deduplication. "table" is in the tag list but has no
# branch; tables contribute only through their <tr> rows,
# whose items never get a room.
#
# Name extraction has a dead path: name starts as the full
# text, so the `phone and not name` branch is reached only
# when the email OPENED the text. A phone-only entry keeps
# the phone number inside its name.
#
# Used by:
#   - scrape_faculty_info (below) — on the 'contacts' soup
############################################################

def _scrape_contacts(soup: BeautifulSoup) -> list[dict]:
    contacts = []


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
    # every entry under the most recent heading
    # ======================================================
    current_category = None
    current_items = []

    for el in content_el.find_all(["h2", "h3", "h4", "p", "div", "table", "tr", "li"]):
        tag = el.name

        if tag in ("h2", "h3", "h4"):
            # STEP 2.1: heading — flush the previous category if it has items
            if current_category and current_items:
                contacts.append({
                    "category": current_category,
                    "items": current_items,
                })
            current_category = el.get_text(strip=True)
            current_items = []

        elif tag in ("p", "li", "div"):
            # STEP 2.2: free-text entry — kept only with an email or phone
            text = el.get_text(separator=" ", strip=True)
            if not text or len(text) < 5:
                continue

            email = _extract_email(text)
            phone = _extract_phone(text)

            if email or phone:
                # the name is what precedes the email, minus a
                # trailing "," or ":"; under 2 chars falls back
                # to the first 60 chars of the whole text
                name = text
                if email:
                    name = text.split(email)[0].strip().rstrip(",").rstrip(":")
                if phone and not name:
                    name = text.split(phone)[0].strip().rstrip(",").rstrip(":")
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

                current_items.append(item)

        elif tag == "tr":
            # STEP 2.3: table row — cell 0 is the name, the rest is searched
            cells = el.find_all(["td", "th"])
            if len(cells) >= 2:
                name = cells[0].get_text(strip=True)
                rest = " ".join(c.get_text(separator=" ", strip=True) for c in cells[1:])
                email = _extract_email(rest)
                phone = _extract_phone(rest)
                if name and (email or phone):
                    item = {"name": name[:100]}
                    if phone:
                        item["phone"] = phone
                    if email:
                        item["email"] = email
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
# [{name, degree, duration}]. The link pass keeps every
# anchor whose href mentions /studij or /program with more
# than 8 chars of text, minus "daugiau" ("more") links;
# names are deduplicated case-insensitively PER PAGE, so a
# program listed on both pages appears twice. degree and
# duration are the fixed Lithuanian labels from the tuple
# ("Bakalauras"/"4 metai", "Magistras"/"2 metai") — no
# English variant is produced, and degree_en is unpacked
# but never read.
#
# The heading/list fallback is gated on `if not programs`,
# and programs is shared across BOTH pages: once the
# bachelor page yielded links, the master page never gets
# its fallback, so a master page without program links
# contributes nothing. A page that failed to fetch (None)
# or matches no content selector is skipped silently.
#
# Used by:
#   - scrape_faculty_info (below) — the 'bachelor' and
#     'master' soups
############################################################

def _scrape_programs(bachelor_soup: BeautifulSoup | None,
                     master_soup: BeautifulSoup | None) -> list[dict]:
    programs = []

    for soup, degree_lt, degree_en, duration in [
        (bachelor_soup, "Bakalauras", "Bachelor's", "4 metai"),
        (master_soup, "Magistras", "Master's", "2 metai"),
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
        seen_names = set()

        for link in content_el.find_all("a", href=True):
            href = link["href"]
            text = link.get_text(strip=True)
            if ("/studij" in href or "/program" in href) and len(text) > 8:
                name = text.strip()
                if name.lower() not in seen_names and "daugiau" not in name.lower():
                    seen_names.add(name.lower())
                    programs.append({
                        "name": name,
                        "degree": degree_lt,
                        "duration": duration,
                    })


        # STEP 3: fallback to headings/list items mentioning
        # "studij" — see banner: only runs while programs is
        # still empty across every page walked so far
        # ==================================================
        if not programs:
            for el in content_el.find_all(["h3", "h4", "li", "strong"]):
                text = el.get_text(strip=True)
                if len(text) > 10 and "studij" in text.lower():
                    name = text.strip()
                    if name.lower() not in seen_names:
                        seen_names.add(name.lower())
                        programs.append({
                            "name": name,
                            "degree": degree_lt,
                            "duration": duration,
                        })

    return programs








############################################################
# _scrape_staff
############################################################
#
# Departments and their people from the structure page as
# [{department, staff: [{name, email?, phone?,
# position?}]}]. Each h2/h3/h4 opens a department; a
# <p>/<li> under it is a staff entry when it is under 200
# chars and either has an email or one of the Lithuanian
# title prefixes (prof., doc., dr., lekt., asist., vedėj-)
# as a substring — so "dr." also fires inside "adr.". "a"
# is in the find_all list but has no branch, and a <p>
# inside an <li> is visited twice (the li first, with the
# same text), so such entries double up.
#
# name is the text up to the first comma, academic title
# INCLUDED ("Prof. dr. Jonas Jonaitis"). position is the
# first matching prefix (list order, not text order),
# capitalised, plus what follows it up to the next comma:
# when the title leads the entry — the usual knf.vu.lt
# shape — that is "Prof. dr. Jonas Jonaitis" again, a copy
# of name, never the role written after the comma. The
# prefix is searched in lowercased text but sliced from the
# original; same length, so the offsets line up.
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

    for el in content_el.find_all(["h2", "h3", "h4", "p", "li", "a"]):
        tag = el.name
        # no separator: child strings are glued together, so
        # "<strong>Prof. dr.</strong> Jonas" reads "Prof. dr.Jonas"
        text = el.get_text(strip=True)

        if tag in ("h2", "h3", "h4") and text:
            # STEP 2.1: heading — flush the previous department if it has people
            if current_dept and current_staff:
                departments.append({
                    "department": current_dept,
                    "staff": current_staff,
                })
            current_dept = text
            current_staff = []

        elif tag in ("p", "li"):
            # STEP 2.2: candidate entry — a title prefix or email marks a person
            if not text or len(text) < 3:
                continue
            email = _extract_email(text)
            phone = _extract_phone(text)

            title_prefixes = ["prof.", "doc.", "dr.", "lekt.", "asist.", "ved\u0117j"]
            is_staff_entry = any(p in text.lower() for p in title_prefixes) or email

            if is_staff_entry and len(text) < 200:
                entry = {"name": text.split(",")[0].strip()[:100]}
                if email:
                    entry["email"] = email
                if phone:
                    entry["phone"] = phone
                # position = first prefix in LIST order + the text
                # after it up to a comma — see banner for why this
                # usually just repeats the name
                for prefix in title_prefixes:
                    if prefix in text.lower():
                        pos_end = text.lower().find(prefix) + len(prefix)
                        remaining = text[pos_end:].strip()
                        if remaining:
                            entry["position"] = prefix.capitalize() + " " + remaining.split(",")[0].strip()
                        break
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
# _scrape_general_contact
############################################################
#
# The faculty's main {address, phone, email} for the info
# screen's footer block. Starts from the hardcoded Muitinės
# g. 8 values and overwrites a field only when the FRONT
# page's full text yields a match, so the result is never
# empty — and scrape_faculty_info stores it on every run,
# even when the page failed to fetch (then the defaults are
# re-saved as if scraped).
#
# The phone is whatever _extract_phone finds first in the
# whole page text, not necessarily the switchboard; the
# email prefers knf@…, then info@….
#
# Used by:
#   - scrape_faculty_info (below) — on the 'main' soup
############################################################

def _scrape_general_contact(main_soup: BeautifulSoup | None) -> dict:
    info = {
        "address": "Muitin\u0117s g. 8, LT-44280 Kaunas",
        "phone": "+370 37 422 523",
        "email": "knf@knf.vu.lt",
    }

    if not main_soup:
        return info

    # newline-joined so the address regex's [^,\n]* stops at
    # element boundaries, while its \s* still lets the postcode
    # sit in the next element
    text = main_soup.get_text(separator="\n")

    # "Muitinės g. <nr> ..., LT-<5 digits> Kaunas"
    addr_match = re.search(r'(Muitin\u0117s\s+g\.?\s*\d+[^,\n]*,?\s*(?:LT-)?\d{5}\s*Kaunas)', text)
    if addr_match:
        info["address"] = addr_match.group(1).strip()

    phone = _extract_phone(text)
    if phone:
        info["phone"] = phone

    # knf@ wins over info@; the domain class eats a trailing
    # dot just like _extract_email
    for pattern in [r'knf@[\w.-]+', r'info@[\w.-]+']:
        match = re.search(pattern, text)
        if match:
            info["email"] = match.group(0)
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
# row ('knf.vu.lt/info': running → completed/failed) —
# contacts + programs go into BOTH articles_found and
# articles_new, so "new" is meaningless for this source.
# Returns {pages_scraped, contacts_found, programs_found},
# plus "error" on failure (the route turns that into a 500;
# the scheduler only logs it).
#
# Only _scrape_contacts is NOT None-safe: when the contacts
# page fails to fetch, soups["contacts"] is None and the
# AttributeError aborts the whole run — programs and the
# general contact fetched in the same run are lost and the
# run is recorded as failed. The other pages degrade to
# "skip".
#
# scraped_at is a NAIVE utcnow() ISO string with a 'T', not
# the "YYYY-MM-DD HH:MM:SS" the column default produces; no
# reader compares it (info/routes.py selects only section
# and data_json), so the mismatch is cosmetic for now.
#
# Used by:
#   - scraper/scheduler.py — run_info_scraper, every 24 h
#     and once 60 s after boot
#   - scraper/routes.py — trigger_info_scrape, the admin
#     POST /api/scraper/info (nothing in the mobile app
#     calls it)
############################################################

def scrape_faculty_info() -> dict:
    # STEP 1: mint the run id, open the DB and register the run
    # as 'running' first, so a crash further down still has a
    # row to mark failed
    # =========================================================
    run_id = str(uuid.uuid4())
    db = get_db()

    try:
        db.execute(
            "INSERT INTO scraper_runs (id, source, status) VALUES (?, 'knf.vu.lt/info', 'running')",
            (run_id,),
        )
        db.commit()


        # STEP 2: fetch every page; a failed one is stored as None
        # (not counted) but keeps its slot for the .get()s below
        # ========================================================
        soups: dict[str, BeautifulSoup | None] = {}
        pages_scraped = 0

        for page in INFO_PAGES:
            soup = _fetch_page(page["url"])
            soups[page["type"]] = soup
            if soup:
                pages_scraped += 1


        # STEP 3: extract — the 'about' and 'studies' soups are
        # never read; _scrape_contacts raises on a None soup
        # =====================================================
        contacts = _scrape_contacts(soups.get("contacts"))
        programs = _scrape_programs(soups.get("bachelor"), soups.get("master"))
        staff = _scrape_staff(soups.get("structure"))
        general = _scrape_general_contact(soups.get("main"))


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
        # previous good blob alive through a bad scrape; general
        # always carries its defaults, so `if general` is always
        # true and that section is written every run
        # ======================================================
        now = datetime.utcnow().isoformat()

        scraped_data_lt = {}
        if contacts:
            scraped_data_lt["contacts"] = contacts
        if programs:
            scraped_data_lt["programs"] = programs
        if general:
            scraped_data_lt["general_contact"] = general

        if scraped_data_lt:
            _store_info(db, "lt", scraped_data_lt, now)


        # STEP 6: close the run — found and new get the same number
        # =========================================================
        db.execute(
            """UPDATE scraper_runs
               SET status = 'completed', articles_found = ?, articles_new = ?,
                   finished_at = datetime('now')
               WHERE id = ?""",
            (contacts_found + programs_found, contacts_found + programs_found, run_id),
        )
        db.commit()

        result = {
            "pages_scraped": pages_scraped,
            "contacts_found": contacts_found,
            "programs_found": programs_found,
        }
        logger.info("Faculty info scrape complete: %s", result)
        return result

    except Exception as e:
        # marks the run failed and answers an error dict instead
        # of raising. Same connection as _store_info, so this
        # commit also flushes any section rows a mid-loop failure
        # left pending there. If the DB itself is what broke, the
        # UPDATE raises out through the finally and the caller
        # sees the exception instead of the dict.
        logger.exception("Faculty info scraper error")
        db.execute(
            """UPDATE scraper_runs
               SET status = 'failed', error_message = ?, finished_at = datetime('now')
               WHERE id = ?""",
            (str(e), run_id),
        )
        db.commit()
        return {"pages_scraped": 0, "contacts_found": 0, "programs_found": 0, "error": str(e)}
    finally:
        db.close()








############################################################
# _store_info
############################################################
#
# Upserts one blob per section for a language: SELECT the
# (lang, section) row, UPDATE it in place (its id survives
# re-scrapes) or INSERT a fresh uuid4 row — done by hand
# rather than leaning on the UNIQUE(lang, section)
# constraint. One commit at the end covers every section;
# a failure mid-loop leaves the earlier sections pending on
# the connection, and scrape_faculty_info's except handler
# then commits them together with the 'failed' status.
# ensure_ascii=False keeps Lithuanian letters readable in
# DbGate instead of \uXXXX escapes.
#
# Used by:
#   - scrape_faculty_info (above) — once per run, lang "lt"
############################################################

def _store_info(db, lang: str, data: dict, scraped_at: str):
    for section, section_data in data.items():
        data_json = json.dumps(section_data, ensure_ascii=False)
        existing = db.execute(
            "SELECT id FROM faculty_info WHERE lang = ? AND section = ?",
            (lang, section),
        ).fetchone()

        if existing:
            db.execute(
                "UPDATE faculty_info SET data_json = ?, scraped_at = ? WHERE id = ?",
                (data_json, scraped_at, existing["id"]),
            )
        else:
            db.execute(
                "INSERT INTO faculty_info (id, lang, section, data_json, scraped_at) VALUES (?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), lang, section, data_json, scraped_at),
            )
    db.commit()
