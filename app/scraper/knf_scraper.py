"""Scraper for knf.vu.lt news articles."""

import logging
import uuid
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from app.database import get_db

logger = logging.getLogger(__name__)

BASE_URL = "https://knf.vu.lt"
NEWS_URL = f"{BASE_URL}/aktualijos"


def scrape_knf_news(pages=2):
    """
    Scrape news articles from knf.vu.lt/aktualijos.

    Args:
        pages: Number of listing pages to scrape (5 articles per page).

    Returns:
        dict with 'found' and 'new' counts.
    """
    run_id = str(uuid.uuid4())
    db = get_db()

    try:
        db.execute(
            "INSERT INTO scraper_runs (id, source, status) VALUES (?, 'knf.vu.lt', 'running')",
            (run_id,),
        )
        db.commit()

        articles_found = 0
        articles_new = 0

        for page_num in range(pages):
            offset = page_num * 5
            url = NEWS_URL if offset == 0 else f"{NEWS_URL}?start={offset}"

            try:
                resp = requests.get(url, timeout=15, headers={
                    "User-Agent": "KNFAPP/1.0 (Vilnius University Kaunas Faculty Mobile App)"
                })
                resp.raise_for_status()
            except requests.RequestException as e:
                logger.warning("Failed to fetch %s: %s", url, e)
                continue

            soup = BeautifulSoup(resp.text, "lxml")

            # Find article links - knf.vu.lt uses h4 > a pattern in aktualijos
            # Also try common Joomla blog patterns
            article_links = []

            # Pattern 1: h4 > a with /aktualijos/ links
            for h4 in soup.find_all("h4"):
                a = h4.find("a", href=True)
                if a and "/aktualijos/" in a["href"]:
                    article_links.append(a)

            # Pattern 2: h2 > a with /aktualijos/ links (some Joomla templates)
            if not article_links:
                for h2 in soup.find_all("h2"):
                    a = h2.find("a", href=True)
                    if a and "/aktualijos/" in a["href"]:
                        article_links.append(a)

            # Pattern 3: any link to /aktualijos/ that looks like an article
            if not article_links:
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if "/aktualijos/" in href and href != "/aktualijos" and a.get_text(strip=True):
                        article_links.append(a)

            seen_hrefs = set()
            for link in article_links:
                href = link["href"]
                if href in seen_hrefs:
                    continue
                seen_hrefs.add(href)

                full_url = href if href.startswith("http") else f"{BASE_URL}{href}"
                title = link.get_text(strip=True)

                if not title:
                    continue

                articles_found += 1

                # Check if already scraped (deduplication by source_url)
                existing = db.execute(
                    "SELECT id FROM news_posts WHERE source_url = ?", (full_url,)
                ).fetchone()
                if existing:
                    continue

                # Fetch the full article
                article_data = _fetch_article(full_url)
                if not article_data:
                    continue

                post_id = str(uuid.uuid4())
                db.execute(
                    """INSERT INTO news_posts
                       (id, title, content, summary, image_url, author_name, source, source_url, post_type, published_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'knf.vu.lt', ?, 'article', ?)""",
                    (
                        post_id,
                        article_data.get("title", title),
                        article_data.get("content", ""),
                        article_data.get("summary", ""),
                        article_data.get("image_url"),
                        article_data.get("author", "VU Kauno fakultetas"),
                        full_url,
                        article_data.get("date", datetime.utcnow().isoformat()),
                    ),
                )
                articles_new += 1

            db.commit()

        db.execute(
            """UPDATE scraper_runs
               SET status = 'completed', articles_found = ?, articles_new = ?, finished_at = datetime('now')
               WHERE id = ?""",
            (articles_found, articles_new, run_id),
        )
        db.commit()

        logger.info("knf.vu.lt scrape complete: found=%d, new=%d", articles_found, articles_new)
        return {"found": articles_found, "new": articles_new}

    except Exception as e:
        logger.exception("knf.vu.lt scraper error")
        db.execute(
            """UPDATE scraper_runs
               SET status = 'failed', error_message = ?, finished_at = datetime('now')
               WHERE id = ?""",
            (str(e), run_id),
        )
        db.commit()
        return {"found": 0, "new": 0, "error": str(e)}
    finally:
        db.close()


def _fetch_article(url):
    """Fetch and parse a single article page."""
    try:
        resp = requests.get(url, timeout=15, headers={
            "User-Agent": "KNFAPP/1.0 (Vilnius University Kaunas Faculty Mobile App)"
        })
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("Failed to fetch article %s: %s", url, e)
        return None

    soup = BeautifulSoup(resp.text, "lxml")

    # Title: try multiple selectors
    title = ""
    for selector in ["h2.item-title", "h1.item-title", "h2", "h1", ".page-header h2"]:
        el = soup.select_one(selector)
        if el:
            title = el.get_text(strip=True)
            break

    # Content: try article body selectors
    content = ""
    for selector in [".item-page", ".article-content", ".item-content", "article", "#content .content"]:
        el = soup.select_one(selector)
        if el:
            # Remove scripts, styles, nav
            for tag in el.find_all(["script", "style", "nav", "header", "footer"]):
                tag.decompose()
            content = el.get_text(separator="\n", strip=True)
            break

    if not content:
        # Fallback: get main content area
        main = soup.find("main") or soup.find("div", {"id": "content"}) or soup.find("div", {"class": "content"})
        if main:
            content = main.get_text(separator="\n", strip=True)

    # Summary: first 200 chars of content
    summary = content[:200].rsplit(" ", 1)[0] + "..." if len(content) > 200 else content

    # Image: first meaningful image
    image_url = None
    for img in soup.find_all("img", src=True):
        src = img["src"]
        if any(skip in src.lower() for skip in ["logo", "icon", "banner", "pixel", "tracking"]):
            continue
        if src.startswith("/"):
            src = f"{BASE_URL}{src}"
        if src.startswith("http"):
            image_url = src
            break

    # Date: look for date patterns
    date_str = None
    # Try meta tags
    for meta in soup.find_all("meta", {"property": "article:published_time"}):
        date_str = meta.get("content")
        break

    # Try common date selectors
    if not date_str:
        for selector in [".article-info time", ".published time", "time", ".date", ".article-date"]:
            el = soup.select_one(selector)
            if el:
                date_str = el.get("datetime") or el.get_text(strip=True)
                break

    # Parse date or use now
    published_at = datetime.utcnow().isoformat()
    if date_str:
        for fmt in ["%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d %B %Y"]:
            try:
                published_at = datetime.strptime(date_str[:19], fmt[:len(date_str[:19])+2]).isoformat()
                break
            except (ValueError, IndexError):
                continue

    # Author
    author = "VU Kauno fakultetas"
    for selector in [".article-author", ".author", ".createdby", "span.author"]:
        el = soup.select_one(selector)
        if el:
            author = el.get_text(strip=True)
            break

    return {
        "title": title or "Untitled",
        "content": content,
        "summary": summary,
        "image_url": image_url,
        "date": published_at,
        "author": author,
    }
