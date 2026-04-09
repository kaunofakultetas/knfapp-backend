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

            # knf.vu.lt uses h2.article-title > a for article links
            article_links = []

            # Pattern 1: h2.article-title > a (current site structure)
            for h2 in soup.select("h2.article-title"):
                a = h2.find("a", href=True)
                if a and "/aktualijos/" in a["href"]:
                    article_links.append(a)

            # Pattern 2: h4 > a (fallback for older Joomla templates)
            if not article_links:
                for h4 in soup.find_all("h4"):
                    a = h4.find("a", href=True)
                    if a and "/aktualijos/" in a["href"]:
                        article_links.append(a)

            # Pattern 3: any heading with .article-title class
            if not article_links:
                for heading in soup.select("[class*='article-title'] a"):
                    if "/aktualijos/" in heading.get("href", ""):
                        article_links.append(heading)

            seen_hrefs = set()
            for link in article_links:
                href = link["href"]
                if href in seen_hrefs:
                    continue
                seen_hrefs.add(href)

                full_url = href if href.startswith("http") else f"{BASE_URL}{href}"
                listing_title = link.get_text(strip=True)

                if not listing_title:
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

                # Use article page title if available, else listing title
                title = article_data.get("title") or listing_title

                post_id = str(uuid.uuid4())
                db.execute(
                    """INSERT INTO news_posts
                       (id, title, content, summary, image_url, author_name, source, source_url, post_type, published_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'knf.vu.lt', ?, 'article', ?)""",
                    (
                        post_id,
                        title,
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

    # Title: prefer og:title (most reliable), strip site prefix
    title = ""
    og_title = soup.find("meta", {"property": "og:title"})
    if og_title and og_title.get("content"):
        title = og_title["content"]
        # Strip common prefixes like "VU Kauno fakultetas - "
        for prefix in ["VU Kauno fakultetas - ", "VU Kauno fakultetas – "]:
            if title.startswith(prefix):
                title = title[len(prefix):]
                break

    # Fallback: article h1 tags — skip section headers like "Aktualijos"
    if not title:
        h1_tags = soup.find_all("h1")
        for h1 in h1_tags:
            text = h1.get_text(strip=True)
            # Skip generic section headers
            if text.lower() not in ("aktualijos", "naujienos", "renginiai", ""):
                title = text
                break

    # Content: use .article-content which has the clean body text
    content = ""
    for selector in [".article-content", ".item-page .article-body", ".item-content"]:
        el = soup.select_one(selector)
        if el:
            for tag in el.find_all(["script", "style", "nav", "header", "footer"]):
                tag.decompose()
            content = el.get_text(separator="\n", strip=True)
            break

    # Fallback: broader selectors
    if not content:
        for selector in [".item-page", "article", "#content .content"]:
            el = soup.select_one(selector)
            if el:
                for tag in el.find_all(["script", "style", "nav", "header", "footer"]):
                    tag.decompose()
                text = el.get_text(separator="\n", strip=True)
                # Strip leading navigation text like "Smulkiau\nAktualijos\n..."
                lines = text.split("\n")
                # Find first line that's actual content (longer than 30 chars)
                start = 0
                for i, line in enumerate(lines):
                    if len(line.strip()) > 30 and line.strip().lower() not in ("aktualijos", "naujienos"):
                        start = i
                        break
                content = "\n".join(lines[start:])
                break

    # Summary: first 200 chars of content
    summary = content[:200].rsplit(" ", 1)[0] + "..." if len(content) > 200 else content

    # Image: try og:image first, then article images
    image_url = None
    og_image = soup.find("meta", {"property": "og:image"})
    if og_image and og_image.get("content"):
        src = og_image["content"]
        if src.startswith("/"):
            src = f"{BASE_URL}{src}"
        image_url = src
    else:
        for img in soup.find_all("img", src=True):
            src = img["src"]
            if any(skip in src.lower() for skip in ["logo", "icon", "banner", "pixel", "tracking"]):
                continue
            if src.startswith("/"):
                src = f"{BASE_URL}{src}"
            if src.startswith("http"):
                image_url = src
                break

    # Date: try <time> tag first (most reliable on knf.vu.lt)
    published_at = datetime.utcnow().isoformat()
    time_el = soup.find("time")
    if time_el and time_el.get("datetime"):
        dt_str = time_el["datetime"]
        try:
            # Parse ISO format like "2026-03-24T14:43:09+02:00"
            parsed = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            published_at = parsed.replace(tzinfo=None).isoformat()
        except (ValueError, TypeError):
            pass
    else:
        # Fallback: meta tags
        for meta in soup.find_all("meta", {"property": "article:published_time"}):
            dt_str = meta.get("content")
            if dt_str:
                try:
                    parsed = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                    published_at = parsed.replace(tzinfo=None).isoformat()
                except (ValueError, TypeError):
                    pass
                break

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
