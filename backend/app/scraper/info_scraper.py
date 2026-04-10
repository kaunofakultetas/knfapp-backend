"""Scraper for faculty information from knf.vu.lt.

Scrapes staff contacts, study programs, department info, and general
contact details. Stores in faculty_info table with language and section
keys. Runs daily via APScheduler — faculty info changes rarely.
"""

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
REQUEST_TIMEOUT = 20

# Pages to scrape for faculty info
INFO_PAGES = [
    {"url": f"{BASE_URL}", "type": "main"},
    {"url": f"{BASE_URL}/fakultetas", "type": "about"},
    {"url": f"{BASE_URL}/fakultetas/struktura", "type": "structure"},
    {"url": f"{BASE_URL}/fakultetas/kontaktai", "type": "contacts"},
    {"url": f"{BASE_URL}/studijos", "type": "studies"},
    {"url": f"{BASE_URL}/studijos/bakalauro-studijos", "type": "bachelor"},
    {"url": f"{BASE_URL}/studijos/magistranturos-studijos", "type": "master"},
]


def _fetch_page(url: str) -> BeautifulSoup | None:
    """Fetch a page and return a BeautifulSoup object."""
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={
            "User-Agent": USER_AGENT,
        })
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "lxml")
    except requests.RequestException as e:
        logger.warning("Failed to fetch %s: %s", url, e)
        return None


def _extract_email(text: str) -> str | None:
    """Extract an email address from text."""
    match = re.search(r'[\w.+-]+@[\w-]+\.[\w.-]+', text)
    return match.group(0) if match else None


def _extract_phone(text: str) -> str | None:
    """Extract a phone number from text."""
    # Lithuanian phone patterns: +370 XX XXX XXX or (8-XX) XX XX XX
    match = re.search(r'(\+370[\s\-]?\d{1,2}[\s\-]?\d{3}[\s\-]?\d{3,4})', text)
    if match:
        return re.sub(r'\s+', ' ', match.group(1).strip())
    match = re.search(r'(\(8[\-\s]?\d{2,3}\)\s*\d{2}[\s\-]?\d{2}[\s\-]?\d{2})', text)
    if match:
        return match.group(1).strip()
    return None


def _scrape_contacts(soup: BeautifulSoup) -> list[dict]:
    """Extract contact information from the contacts page."""
    contacts = []

    # Look for structured contact info in tables, definition lists, or repeated blocks
    # knf.vu.lt typically uses article-content with structured text

    content_el = None
    for selector in [".article-content", ".item-page", "#content", "article"]:
        content_el = soup.select_one(selector)
        if content_el:
            break

    if not content_el:
        return contacts

    # Parse headings + following content as contact categories
    current_category = None
    current_items = []

    for el in content_el.find_all(["h2", "h3", "h4", "p", "div", "table", "tr", "li"]):
        tag = el.name

        if tag in ("h2", "h3", "h4"):
            # Save previous category if it had items
            if current_category and current_items:
                contacts.append({
                    "category": current_category,
                    "items": current_items,
                })
            current_category = el.get_text(strip=True)
            current_items = []

        elif tag in ("p", "li", "div"):
            text = el.get_text(separator=" ", strip=True)
            if not text or len(text) < 5:
                continue

            email = _extract_email(text)
            phone = _extract_phone(text)

            if email or phone:
                # Try to extract a name — text before phone/email
                name = text
                if email:
                    name = text.split(email)[0].strip().rstrip(",").rstrip(":")
                if phone and not name:
                    name = text.split(phone)[0].strip().rstrip(",").rstrip(":")
                if not name or len(name) < 2:
                    name = text[:60]

                # Extract room number
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

    # Save last category
    if current_category and current_items:
        contacts.append({
            "category": current_category,
            "items": current_items,
        })

    return contacts


def _scrape_programs(bachelor_soup: BeautifulSoup | None,
                     master_soup: BeautifulSoup | None) -> list[dict]:
    """Extract study programs from the studies pages."""
    programs = []

    for soup, degree_lt, degree_en, duration in [
        (bachelor_soup, "Bakalauras", "Bachelor's", "4 metai"),
        (master_soup, "Magistras", "Master's", "2 metai"),
    ]:
        if not soup:
            continue

        content_el = None
        for selector in [".article-content", ".item-page", "#content", "article"]:
            content_el = soup.select_one(selector)
            if content_el:
                break

        if not content_el:
            continue

        # Programs are typically listed as headings, links, or list items
        seen_names = set()

        # Check for links to program pages
        for link in content_el.find_all("a", href=True):
            href = link["href"]
            text = link.get_text(strip=True)
            # Program links typically contain "/studijos/" and have meaningful text
            if ("/studij" in href or "/program" in href) and len(text) > 8:
                name = text.strip()
                if name.lower() not in seen_names and "daugiau" not in name.lower():
                    seen_names.add(name.lower())
                    programs.append({
                        "name": name,
                        "degree": degree_lt,
                        "duration": duration,
                    })

        # Fallback: check headings and list items
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


def _scrape_staff(structure_soup: BeautifulSoup | None) -> list[dict]:
    """Extract staff/department structure from the structure page."""
    departments = []

    if not structure_soup:
        return departments

    content_el = None
    for selector in [".article-content", ".item-page", "#content", "article"]:
        content_el = structure_soup.select_one(selector)
        if content_el:
            break

    if not content_el:
        return departments

    current_dept = None
    current_staff = []

    for el in content_el.find_all(["h2", "h3", "h4", "p", "li", "a"]):
        tag = el.name
        text = el.get_text(strip=True)

        if tag in ("h2", "h3", "h4") and text:
            if current_dept and current_staff:
                departments.append({
                    "department": current_dept,
                    "staff": current_staff,
                })
            current_dept = text
            current_staff = []

        elif tag in ("p", "li"):
            if not text or len(text) < 3:
                continue
            # Look for name + title patterns
            email = _extract_email(text)
            phone = _extract_phone(text)

            # Staff entries typically have a name and optional title/position
            # e.g. "Prof. dr. Jonas Jonaitis" or "Lekt. Tomas Vanagas"
            title_prefixes = ["prof.", "doc.", "dr.", "lekt.", "asist.", "ved\u0117j"]
            is_staff_entry = any(p in text.lower() for p in title_prefixes) or email

            if is_staff_entry and len(text) < 200:
                entry = {"name": text.split(",")[0].strip()[:100]}
                if email:
                    entry["email"] = email
                if phone:
                    entry["phone"] = phone
                # Extract position/title
                for prefix in title_prefixes:
                    if prefix in text.lower():
                        # The title is usually at the beginning
                        pos_end = text.lower().find(prefix) + len(prefix)
                        remaining = text[pos_end:].strip()
                        if remaining:
                            entry["position"] = prefix.capitalize() + " " + remaining.split(",")[0].strip()
                        break
                current_staff.append(entry)

    if current_dept and current_staff:
        departments.append({
            "department": current_dept,
            "staff": current_staff,
        })

    return departments


def _scrape_general_contact(main_soup: BeautifulSoup | None) -> dict:
    """Extract general faculty contact info (address, phone, email) from main page."""
    info = {
        "address": "Muitin\u0117s g. 8, LT-44280 Kaunas",
        "phone": "+370 37 422 523",
        "email": "knf@knf.vu.lt",
    }

    if not main_soup:
        return info

    # Look for footer or contact block with address/phone/email
    text = main_soup.get_text(separator="\n")

    # Try to find address
    addr_match = re.search(r'(Muitin\u0117s\s+g\.?\s*\d+[^,\n]*,?\s*(?:LT-)?\d{5}\s*Kaunas)', text)
    if addr_match:
        info["address"] = addr_match.group(1).strip()

    # Try to find main phone
    phone = _extract_phone(text)
    if phone:
        info["phone"] = phone

    # Try to find main email
    for pattern in [r'knf@[\w.-]+', r'info@[\w.-]+']:
        match = re.search(pattern, text)
        if match:
            info["email"] = match.group(0)
            break

    return info


def scrape_faculty_info() -> dict:
    """Full faculty info scrape. Fetches all info pages and stores results.

    Returns:
        Dict with 'pages_scraped', 'contacts_found', 'programs_found' counts.
    """
    run_id = str(uuid.uuid4())
    db = get_db()

    try:
        db.execute(
            "INSERT INTO scraper_runs (id, source, status) VALUES (?, 'knf.vu.lt/info', 'running')",
            (run_id,),
        )
        db.commit()

        # Fetch all pages
        soups: dict[str, BeautifulSoup | None] = {}
        pages_scraped = 0

        for page in INFO_PAGES:
            soup = _fetch_page(page["url"])
            soups[page["type"]] = soup
            if soup:
                pages_scraped += 1

        # Extract data
        contacts = _scrape_contacts(soups.get("contacts"))
        programs = _scrape_programs(soups.get("bachelor"), soups.get("master"))
        staff = _scrape_staff(soups.get("structure"))
        general = _scrape_general_contact(soups.get("main"))

        # If scraping returned useful contacts, convert staff to contact format
        # and merge with direct contacts
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

        # Store scraped data in faculty_info table
        # Each row: (id, lang, section, data_json, scraped_at)
        now = datetime.utcnow().isoformat()

        # Build the scraped data structure (Lithuanian)
        scraped_data_lt = {}
        if contacts:
            scraped_data_lt["contacts"] = contacts
        if programs:
            scraped_data_lt["programs"] = programs
        if general:
            scraped_data_lt["general_contact"] = general

        # Store as single entry per language
        if scraped_data_lt:
            _store_info(db, "lt", scraped_data_lt, now)

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


def _store_info(db, lang: str, data: dict, scraped_at: str):
    """Upsert scraped faculty info into the database."""
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
