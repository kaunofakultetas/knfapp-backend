"""Scraper for vu.lt news articles."""

import logging
import uuid
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from app.database import get_db

logger = logging.getLogger(__name__)

BASE_URL = "https://www.vu.lt"
NEWS_URL = f"{BASE_URL}/naujienos"


def scrape_vu_news(pages=1):
    """
    Scrape news articles from vu.lt/naujienos.

    vu.lt uses Next.js with server-side rendering. We fetch the HTML
    and parse whatever content is available in the initial response.

    Args:
        pages: Number of listing pages to scrape.

    Returns:
        dict with 'found' and 'new' counts.
    """
    run_id = str(uuid.uuid4())
    db = get_db()

    try:
        db.execute(
            "INSERT INTO scraper_runs (id, source, status) VALUES (?, 'vu.lt', 'running')",
            (run_id,),
        )
        db.commit()

        articles_found = 0
        articles_new = 0

        try:
            resp = requests.get(NEWS_URL, timeout=20, headers={
                "User-Agent": "KNFAPP/1.0 (Vilnius University Kaunas Faculty Mobile App)",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "lt",
            })
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.warning("Failed to fetch %s: %s", NEWS_URL, e)
            db.execute(
                """UPDATE scraper_runs SET status = 'failed', error_message = ?, finished_at = datetime('now')
                   WHERE id = ?""",
                (str(e), run_id),
            )
            db.commit()
            return {"found": 0, "new": 0, "error": str(e)}

        soup = BeautifulSoup(resp.text, "lxml")

        # vu.lt uses Next.js — look for article links in the rendered HTML
        # Pattern: /lt/visos-naujienos/[slug] or /naujienos/[slug]
        article_links = []
        seen_hrefs = set()

        for a in soup.find_all("a", href=True):
            href = a["href"]
            # Match news article patterns
            if any(pattern in href for pattern in ["/visos-naujienos/", "/naujienos/"]):
                # Skip pagination/category links
                if href.count("/") < 3:
                    continue
                if href in seen_hrefs:
                    continue
                seen_hrefs.add(href)

                title = a.get_text(strip=True)
                if title and len(title) > 10:
                    article_links.append({"href": href, "title": title})

        for link_data in article_links[:10]:  # Limit to 10 articles per scrape
            href = link_data["href"]
            full_url = href if href.startswith("http") else f"{BASE_URL}{href}"

            articles_found += 1

            # Deduplication
            existing = db.execute(
                "SELECT id FROM news_posts WHERE source_url = ?", (full_url,)
            ).fetchone()
            if existing:
                continue

            # Fetch article content
            article_data = _fetch_vu_article(full_url)
            if not article_data:
                continue

            post_id = str(uuid.uuid4())
            db.execute(
                """INSERT INTO news_posts
                   (id, title, content, summary, image_url, author_name, source, source_url, post_type, published_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'vu.lt', ?, 'article', ?)""",
                (
                    post_id,
                    article_data.get("title", link_data["title"]),
                    article_data.get("content", ""),
                    article_data.get("summary", ""),
                    article_data.get("image_url"),
                    article_data.get("author", "Vilniaus universitetas"),
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

        logger.info("vu.lt scrape complete: found=%d, new=%d", articles_found, articles_new)

        # Send push notification if new articles were found (respects channel preferences)
        if articles_new > 0:
            try:
                from app.notifications.push import notify_channel
                title = "VU naujienos" if articles_new == 1 else f"VU naujienos ({articles_new})"
                body = f"Naujas straipsnis i\u0161 vu.lt" if articles_new == 1 else f"{articles_new} nauji straipsniai i\u0161 vu.lt"
                notify_channel("news", title, body, data={"type": "news", "source": "vu.lt"})
            except Exception:
                logger.exception("Failed to send push notification for new vu.lt articles")

        return {"found": articles_found, "new": articles_new}

    except Exception as e:
        logger.exception("vu.lt scraper error")
        db.execute(
            """UPDATE scraper_runs SET status = 'failed', error_message = ?, finished_at = datetime('now')
               WHERE id = ?""",
            (str(e), run_id),
        )
        db.commit()
        return {"found": 0, "new": 0, "error": str(e)}
    finally:
        db.close()


def _fetch_vu_article(url):
    """Fetch and parse a single vu.lt article page."""
    try:
        resp = requests.get(url, timeout=20, headers={
            "User-Agent": "KNFAPP/1.0 (Vilnius University Kaunas Faculty Mobile App)",
            "Accept": "text/html",
            "Accept-Language": "lt",
        })
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("Failed to fetch article %s: %s", url, e)
        return None

    soup = BeautifulSoup(resp.text, "lxml")

    # Title
    title = ""
    for selector in ["h1", "article h1", "[class*='title'] h1", "main h1"]:
        el = soup.select_one(selector)
        if el:
            title = el.get_text(strip=True)
            break

    # Content
    content = ""
    for selector in ["article", "main article", "[class*='content']", "main"]:
        el = soup.select_one(selector)
        if el:
            for tag in el.find_all(["script", "style", "nav", "header", "footer", "aside"]):
                tag.decompose()
            content = el.get_text(separator="\n", strip=True)
            break

    summary = content[:200].rsplit(" ", 1)[0] + "..." if len(content) > 200 else content

    # Image - try og:image first (most reliable for Next.js sites)
    image_url = None
    og_image = soup.find("meta", {"property": "og:image"})
    if og_image and og_image.get("content"):
        image_url = og_image["content"]
    else:
        for img in soup.find_all("img", src=True):
            src = img["src"]
            if any(skip in src.lower() for skip in ["logo", "icon", "pixel", "tracking", "avatar"]):
                continue
            if "newshub.vu.lt" in src or "vu.lt" in src:
                image_url = src if src.startswith("http") else f"{BASE_URL}{src}"
                break

    # Date
    date_str = None
    for meta in soup.find_all("meta", {"property": "article:published_time"}):
        date_str = meta.get("content")
        break
    if not date_str:
        time_el = soup.find("time")
        if time_el:
            date_str = time_el.get("datetime") or time_el.get_text(strip=True)

    published_at = datetime.utcnow().isoformat()
    if date_str:
        # Strip timezone suffix if present (e.g. "+03:00", "Z")
        clean = date_str.replace("Z", "").split("+")[0].split(".")[0]
        for fmt in ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"]:
            try:
                published_at = datetime.strptime(clean, fmt).isoformat()
                break
            except (ValueError, IndexError):
                continue

    author = "Vilniaus universitetas"

    return {
        "title": title or "Untitled",
        "content": content,
        "summary": summary,
        "image_url": image_url,
        "date": published_at,
        "author": author,
    }
