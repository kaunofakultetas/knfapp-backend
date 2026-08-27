############################################################
#  [*] Scraper — vu.lt news (the central newsroom)
#
#  Sibling of knf_scraper.py: pulls www.vu.lt/naujienos
#  into news_posts with source 'vu.lt' (the feed ranks it
#  +10, faculty news +15). One listing fetch per run, at
#  most 10 article links, each unseen article fetched and
#  parsed on its own page; every run gets a scraper_runs
#  row that the admin GET /api/scraper/status lists. vu.lt
#  is a Next.js site — only what the server renders into
#  the initial HTML is seen, nothing the client hydrates.
#
#  scheduler.py runs it every 20 minutes (and 2 s after
#  boot); admins fire it by hand through POST
#  /api/scraper/trigger (alias /run). A run that inserted
#  anything sends ONE push on the "news" channel,
#  Lithuanian text only. published_at lands as a naive ISO
#  string with the site's UTC offset dropped, not applied —
#  see _fetch_vu_article.
############################################################


import logging
import uuid
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from app.database import get_db

logger = logging.getLogger(__name__)

BASE_URL = "https://www.vu.lt"
NEWS_URL = f"{BASE_URL}/naujienos"








############################################################
# scrape_vu_news
############################################################
#
# One run: opens a scraper_runs row, fetches the listing,
# harvests article links, fetches and inserts the ones
# news_posts has not seen (a SELECT on source_url first —
# the column is UNIQUE, so a manual trigger overlapping the
# scheduled run can still collide and fail the whole run),
# closes the row and returns {"found", "new"} — or
# {"found": 0, "new": 0, "error"} when the listing fetch or
# anything else throws. `found` counts links CONSIDERED
# (max 10), fetched or not; `new` counts rows inserted.
#
# Gotchas:
#   - `pages` is accepted and never read — one listing page
#     always; both callers pass pages=1 anyway (the knf
#     sibling really loops)
#   - the href is marked seen BEFORE the title test, so
#     when a card's image link precedes its title link the
#     article is lost for that run (the image anchor has
#     no text, the titled one is "already seen")
#   - the cap is on links considered, not inserts: links
#     11+ on the listing are never imported, not even on
#     the first run
#   - the 'running' row is committed before any network
#     I/O; a process death mid-run leaves it 'running'
#     forever, nothing sweeps scraper_runs
#   - the inserts share one transaction with the closing
#     UPDATE, and the failure path commits too — posts
#     inserted before a later exception survive under a
#     'failed' run
#   - _fetch_vu_article always carries "title" and "date",
#     so the .get() defaults here never fire: a page with
#     no h1 lands as "Untitled" even though the listing had
#     a real title
#   - the push is per run, not per article, Lithuanian
#     only; a push failure is logged and the run still
#     reports success
#
# Used by:
#   - scraper/scheduler.py — run_scrapers, every 20 min
#     and 2 s after boot (pages=1)
#   - scraper/routes.py — trigger_scrape, POST
#     /api/scraper/trigger and /run (admin, pages=1)
############################################################

def scrape_vu_news(pages=1):
    run_id = str(uuid.uuid4())
    db = get_db()

    try:


        # STEP 1: register the run as 'running' — committed now, so the
        # admin status page shows it while the fetches take their time
        # =============================================================
        db.execute(
            "INSERT INTO scraper_runs (id, source, status) VALUES (?, 'vu.lt', 'running')",
            (run_id,),
        )
        db.commit()

        articles_found = 0
        articles_new = 0


        # STEP 2: fetch the listing; a transport error closes the run as
        # 'failed' with the error text and answers found=0/new=0
        # ==============================================================
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


        # STEP 3: harvest article links from the server-rendered HTML:
        # anchors under /visos-naujienos/ or /naujienos/ that carry a
        # real title, first sighting of each href wins
        # ============================================================
        article_links = []
        seen_hrefs = set()

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if any(pattern in href for pattern in ["/visos-naujienos/", "/naujienos/"]):
                # Fewer than three slashes is the category root or a page
                # link ("/naujienos/", "/naujienos/?page=2"), not an article.
                # Only relative hrefs can fail this — "https://" alone has
                # three — and "/lt/naujienos/" passes with its three
                if href.count("/") < 3:
                    continue
                if href in seen_hrefs:
                    continue
                # Marked seen before the title test — see the banner
                seen_hrefs.add(href)

                title = a.get_text(strip=True)
                # Drops image-only anchors (empty text) and short labels
                if title and len(title) > 10:
                    article_links.append({"href": href, "title": title})


        # STEP 4: the first 10 links — skip URLs news_posts already
        # holds, fetch the rest one page at a time and insert them
        # =========================================================
        for link_data in article_links[:10]:  # Limit to 10 articles per scrape
            href = link_data["href"]
            full_url = href if href.startswith("http") else f"{BASE_URL}{href}"

            # Counted as found even when already stored or unfetchable
            articles_found += 1

            existing = db.execute(
                "SELECT id FROM news_posts WHERE source_url = ?", (full_url,)
            ).fetchone()
            if existing:
                continue

            # None only on a transport error — a page that parsed to
            # nothing still comes back and is inserted as "Untitled"
            article_data = _fetch_vu_article(full_url)
            if not article_data:
                continue

            # The .get() defaults are dead — see the banner. Not committed
            # per row: one transaction for the whole loop
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


        # STEP 5: close the run — counts and finished_at in one UPDATE
        # ============================================================
        db.execute(
            """UPDATE scraper_runs
               SET status = 'completed', articles_found = ?, articles_new = ?, finished_at = datetime('now')
               WHERE id = ?""",
            (articles_found, articles_new, run_id),
        )
        db.commit()

        logger.info("vu.lt scrape complete: found=%d, new=%d", articles_found, articles_new)


        # STEP 6: one push for the whole run on the "news" channel —
        # every active token unless its user opted that channel out;
        # a push failure is logged and does not fail the run
        # ==========================================================
        if articles_new > 0:
            try:
                # Lazy import, as in knf_scraper.py
                from app.notifications.push import notify_channel
                title = "VU naujienos" if articles_new == 1 else f"VU naujienos ({articles_new})"
                body = f"Naujas straipsnis i\u0161 vu.lt" if articles_new == 1 else f"{articles_new} nauji straipsniai i\u0161 vu.lt"
                notify_channel("news", title, body, data={"type": "news", "source": "vu.lt"})
            except Exception:
                logger.exception("Failed to send push notification for new vu.lt articles")

        return {"found": articles_found, "new": articles_new}

    except Exception as e:
        # This commit also lands the inserts made before the failure —
        # see the banner
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








############################################################
# _fetch_vu_article
############################################################
#
# Fetches one article page and boils it down to {title,
# content, summary, image_url, date, author}. Returns None
# ONLY on a transport error (the caller then skips the
# link); a page that yields nothing still comes back as
# "Untitled" with empty content and gets inserted as such.
# Everything is selector guesswork against a Next.js site
# with no stable markup contract:
#
#   - title: the first <h1>. The three fallback selectors
#     ("article h1", "[class*='title'] h1", "main h1") all
#     match a subset of "h1", so they can never fire
#   - content: <article>, else [class*='content'], else
#     <main> ("main article" is dead the same way);
#     script/style/nav/header/footer/aside inside it are
#     decompose()d, which MUTATES the shared soup — the
#     image and date lookups below only see what survived
#   - summary: the first 200 chars cut at the last space
#     plus "...", or the whole content when shorter
#   - image: og:image verbatim, else the first <img> whose
#     src names vu.lt and is not a logo/icon/pixel/
#     tracking/avatar; a relative src gets BASE_URL
#     ("newshub.vu.lt" is a redundant test — it contains
#     "vu.lt")
#   - date: article:published_time meta, else <time>
#     datetime attr or text. "Z", a "+hh:mm" suffix and
#     fractional seconds are stripped, the rest parsed as
#     ISO date-time or plain date; anything else (a
#     negative offset, "2025 m. sausio 5 d.") falls back
#     to utcnow(). The offset is DROPPED, not applied: the
#     site's local wall clock is stored as if it were UTC,
#     2-3 h ahead of the truth
#   - author: the constant "Vilniaus universitetas"
#
# Used by:
#   - scrape_vu_news (above) — once per unseen link
############################################################

def _fetch_vu_article(url):
    # STEP 1: fetch the page — a transport error is the one None exit
    # ===============================================================
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


    # STEP 2: title — the first <h1>; the other three selectors are
    # subsets of it and never fire (banner)
    # =============================================================
    title = ""
    for selector in ["h1", "article h1", "[class*='title'] h1", "main h1"]:
        el = soup.select_one(selector)
        if el:
            title = el.get_text(strip=True)
            break


    # STEP 3: content — the first matching region with its chrome
    # decompose()d out of the SHARED soup, then a 200-char summary
    # ============================================================
    content = ""
    for selector in ["article", "main article", "[class*='content']", "main"]:
        el = soup.select_one(selector)
        if el:
            for tag in el.find_all(["script", "style", "nav", "header", "footer", "aside"]):
                tag.decompose()
            content = el.get_text(separator="\n", strip=True)
            break

    # The conditional binds last: (cut + "...") if long, else content
    summary = content[:200].rsplit(" ", 1)[0] + "..." if len(content) > 200 else content


    # STEP 4: image — og:image wins (the reliable one on a Next.js
    # site), else the first vu.lt-hosted <img> that is not chrome
    # ============================================================
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


    # STEP 5: date — article:published_time meta, else <time>; the
    # UTC offset is stripped, never applied (banner)
    # ============================================================
    date_str = None
    for meta in soup.find_all("meta", {"property": "article:published_time"}):
        date_str = meta.get("content")
        break
    if not date_str:
        time_el = soup.find("time")
        if time_el:
            date_str = time_el.get("datetime") or time_el.get_text(strip=True)

    # Naive UTC fallback (utcnow() is deprecated since 3.12 — the image
    # runs 3.13 — but only warns); also what a parse failure keeps
    published_at = datetime.utcnow().isoformat()
    if date_str:
        # "Z", "+hh:mm" and fractional seconds go; a "-hh:mm" offset
        # survives and makes both strptime formats fail
        clean = date_str.replace("Z", "").split("+")[0].split(".")[0]
        for fmt in ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"]:
            try:
                published_at = datetime.strptime(clean, fmt).isoformat()
                break
            # IndexError cannot come out of strptime — a harmless guard
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
