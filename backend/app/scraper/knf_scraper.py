############################################################
#  [*] Scraper — knf.vu.lt news into news_posts
#
#  Pulls the faculty news listing (knf.vu.lt/aktualijos, a
#  Joomla site paginated 5 per page via ?start=<offset>),
#  fetches every article page not yet in news_posts and
#  inserts it with source='knf.vu.lt', post_type='article'
#  and no author_id. Deduplication is by source_url, which
#  is also UNIQUE in the schema — the SELECT check is the
#  polite path, the constraint the backstop. Every run is
#  logged in scraper_runs (running → completed/failed) for
#  GET /api/scraper/status; the rows surface through
#  news/routes.py (+15 source boost in the feed ranking)
#  and the mobile news tab's 'knf.vu.lt' source filter.
#
#  Runs from two places: scheduler.py (pages=2, every 20
#  minutes and once 2 s after startup, max_instances=1) and
#  POST /api/scraper/trigger|/run (scraper/routes.py,
#  pages=3). Nothing stops a manual trigger overlapping a
#  scheduled run: both pass the SELECT check for the same
#  article and the loser's INSERT hits the UNIQUE
#  constraint, failing its whole run (commits are per
#  listing page, so earlier pages survive).
#
#  Timestamps are mixed: <time datetime> values keep the
#  Vilnius wall clock and DROP the offset, while the
#  fallback is utcnow() — both land in published_at as
#  naive ISO strings, so the julianday() recency score in
#  news/routes.py is skewed by 2–3 h between the two.
#
#  New articles trigger one push on the "news" channel
#  (Lithuanian copy, never localised per user); a push
#  failure is logged and does not fail the run.
############################################################


import logging
import uuid
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from app.database import get_db

logger = logging.getLogger(__name__)

BASE_URL = "https://knf.vu.lt"
NEWS_URL = f"{BASE_URL}/aktualijos"








############################################################
# scrape_knf_news
############################################################
#
# The run: open a scraper_runs row, walk `pages` listing
# pages, insert unseen articles, close the row, push. The
# returned dict {"found", "new"} — plus "error" on failure —
# is what the scheduler logs and /api/scraper/trigger
# returns. "found" counts listing links with a non-empty
# title INCLUDING ones already in the DB, and it is per
# page: an article listed on two pages counts twice (the
# seen_hrefs set resets per page) though it is inserted
# once. A listing page that fails to download is skipped
# with a warning; an article page that fails is skipped
# and, having no row, is retried on the next run.
#
# Gotchas: `article_data.get("title") or listing_title`
# never falls back — _fetch_article returns "Untitled"
# (truthy) when it finds no title, so the listing title is
# dead; likewise every .get() default on article_data is
# dead because _fetch_article always sets all six keys.
# The except path reuses `db` to mark the run failed, so a
# database error inside the run raises a second time out
# of the function (the scheduler catches it; the admin
# route would 500). A process kill leaves the row at
# 'running' forever — nothing sweeps those.
#
# Used by:
#   - scraper/scheduler.py — run_scrapers, pages=2
#   - scraper/routes.py — trigger_scrape (POST /api/scraper/
#     trigger and /run), pages=3
############################################################

def scrape_knf_news(pages=2):
    # STEP 1: open the scraper_runs row before any network I/O
    # so the admin status page sees the run while it is going
    # ========================================================
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


        # STEP 2: walk the listing pages — Joomla paginates 5 per
        # page through ?start=<offset>; a page that fails to load
        # is skipped, not fatal
        # =======================================================
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

            # STEP 2.1: article links — three template generations, first hit wins
            article_links = []

            # h2.article-title > a is the current template
            for h2 in soup.select("h2.article-title"):
                a = h2.find("a", href=True)
                if a and "/aktualijos/" in a["href"]:
                    article_links.append(a)

            # h4 > a — the older Joomla template
            if not article_links:
                for h4 in soup.find_all("h4"):
                    a = h4.find("a", href=True)
                    if a and "/aktualijos/" in a["href"]:
                        article_links.append(a)

            # Last resort: any element carrying an article-title
            # class, whatever the heading level
            if not article_links:
                for heading in soup.select("[class*='article-title'] a"):
                    if "/aktualijos/" in heading.get("href", ""):
                        article_links.append(heading)

            # STEP 2.2: one row per unseen article — page-local dedupe, then source_url
            seen_hrefs = set()
            for link in article_links:
                href = link["href"]
                if href in seen_hrefs:
                    continue
                seen_hrefs.add(href)

                full_url = href if href.startswith("http") else f"{BASE_URL}{href}"
                listing_title = link.get_text(strip=True)

                # An empty link text is dropped before counting
                if not listing_title:
                    continue

                articles_found += 1

                # Already scraped on an earlier run — counted in
                # "found", never re-fetched
                existing = db.execute(
                    "SELECT id FROM news_posts WHERE source_url = ?", (full_url,)
                ).fetchone()
                if existing:
                    continue

                # None = download failed; no row is written, so
                # the next run tries this article again
                article_data = _fetch_article(full_url)
                if not article_data:
                    continue

                # BUG (documented, not fixed): _fetch_article never
                # returns an empty title — "Untitled" is truthy —
                # so listing_title is never used here
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

            # Commit per listing page: a failure on page N keeps
            # everything from pages < N
            db.commit()


        # STEP 3: close the run — the counts land in scraper_runs
        # for GET /api/scraper/status
        # =======================================================
        db.execute(
            """UPDATE scraper_runs
               SET status = 'completed', articles_found = ?, articles_new = ?, finished_at = datetime('now')
               WHERE id = ?""",
            (articles_found, articles_new, run_id),
        )
        db.commit()

        logger.info("knf.vu.lt scrape complete: found=%d, new=%d", articles_found, articles_new)


        # STEP 4: one push for the whole run on the "news" channel
        # (opt-outs honoured inside notify_channel); a push failure
        # is logged and the run still counts as completed
        # =========================================================
        if articles_new > 0:
            try:
                # Lazy import, not a cycle guard — push.py only
                # imports app.database
                from app.notifications.push import notify_channel
                title = "KNF naujienos" if articles_new == 1 else f"KNF naujienos ({articles_new})"
                body = f"Naujas straipsnis i\u0161 knf.vu.lt" if articles_new == 1 else f"{articles_new} nauji straipsniai i\u0161 knf.vu.lt"
                notify_channel("news", title, body, data={"type": "news", "source": "knf.vu.lt"})
            except Exception:
                logger.exception("Failed to send push notification for new knf.vu.lt articles")

        return {"found": articles_found, "new": articles_new}

    except Exception as e:
        # Anything that escaped the per-page guards, including an
        # IntegrityError from a concurrent run inserting the same
        # source_url. If the DB itself is the problem this UPDATE
        # raises again and the error leaves the function
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








############################################################
# _fetch_article
############################################################
#
# Downloads one article page and scrapes title, content,
# summary, image, date and author with a chain of fallbacks
# per field (the Joomla template has changed before; the
# first selector in each list is the current one). None
# only when the download fails — a page that parses to
# nothing still returns a dict with title "Untitled" and
# empty content, and gets inserted as such.
#
# Per field:
#   title   — og:title minus the "VU Kauno fakultetas - "
#             site prefix (hyphen and en-dash variants),
#             else the first <h1> that is not a section name
#   content — text of .article-content (or two older
#             selectors) with script/style/nav/header/footer
#             removed; the broad fallback drops leading nav
#             crumbs by skipping to the first line over 30
#             characters
#   summary — first 200 chars cut back to a word boundary
#   image   — og:image, else the first <img> not looking
#             like a logo/icon/banner/pixel
#   date    — the page's FIRST <time datetime>, tz-stripped
#             (see the header), else article:published_time,
#             else utcnow(). An unparsable <time> skips the
#             meta fallback entirely — the else belongs to
#             the if, not to the try
#   author  — .article-author, .author, .createdby or
#             span.author; default "VU Kauno fakultetas"
#
# A protocol-relative "//host/…" src also startswith("/")
# and is glued onto BASE_URL into a broken URL; a bare
# relative og:image ("images/x.jpg") is stored verbatim.
#
# Used by:
#   - scrape_knf_news (above) — once per unseen article
############################################################

def _fetch_article(url):
    # STEP 1: download — None on failure; the caller skips the
    # article and, with no row written, retries it next run
    # ========================================================
    try:
        resp = requests.get(url, timeout=15, headers={
            "User-Agent": "KNFAPP/1.0 (Vilnius University Kaunas Faculty Mobile App)"
        })
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("Failed to fetch article %s: %s", url, e)
        return None

    soup = BeautifulSoup(resp.text, "lxml")


    # STEP 2: title — og:title first, minus the site prefix the
    # template prepends (both the hyphen and the en-dash form)
    # =========================================================
    title = ""
    og_title = soup.find("meta", {"property": "og:title"})
    if og_title and og_title.get("content"):
        title = og_title["content"]
        for prefix in ["VU Kauno fakultetas - ", "VU Kauno fakultetas – "]:
            if title.startswith(prefix):
                title = title[len(prefix):]
                break

    # STEP 2.1: no og:title — the first <h1> that is not a section name
    if not title:
        h1_tags = soup.find_all("h1")
        for h1 in h1_tags:
            text = h1.get_text(strip=True)
            if text.lower() not in ("aktualijos", "naujienos", "renginiai", ""):
                title = text
                break


    # STEP 3: content — the clean article body first; the broad
    # fallback also has to shed leading navigation crumbs
    # =========================================================
    content = ""
    for selector in [".article-content", ".item-page .article-body", ".item-content"]:
        el = soup.select_one(selector)
        if el:
            # decompose() mutates the shared soup — the <img> and
            # author lookups below never see the tags removed here
            for tag in el.find_all(["script", "style", "nav", "header", "footer"]):
                tag.decompose()
            content = el.get_text(separator="\n", strip=True)
            break

    # STEP 3.1: broad selectors — skip to the first line over 30 chars
    if not content:
        for selector in [".item-page", "article", "#content .content"]:
            el = soup.select_one(selector)
            if el:
                for tag in el.find_all(["script", "style", "nav", "header", "footer"]):
                    tag.decompose()
                text = el.get_text(separator="\n", strip=True)
                lines = text.split("\n")
                # start stays 0 when no line qualifies — the whole
                # text, crumbs included, is kept
                start = 0
                for i, line in enumerate(lines):
                    if len(line.strip()) > 30 and line.strip().lower() not in ("aktualijos", "naujienos"):
                        start = i
                        break
                content = "\n".join(lines[start:])
                break


    # STEP 4: summary — 200 chars cut back to the last space,
    # or the whole content when it is short enough
    # =======================================================
    summary = content[:200].rsplit(" ", 1)[0] + "..." if len(content) > 200 else content


    # STEP 5: image — og:image, else the first <img> that does
    # not look like page chrome; root-relative srcs get BASE_URL
    # (and so do protocol-relative ones — see the banner)
    # ==========================================================
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
            # Unlike og:image, a bare relative src is dropped here
            if src.startswith("http"):
                image_url = src
                break


    # STEP 6: date — the page's first <time datetime>, offset
    # DROPPED not converted (Vilnius wall clock stored as naive
    # ISO); the meta fallback runs only when there is no <time>
    # =========================================================
    published_at = datetime.utcnow().isoformat()
    time_el = soup.find("time")
    if time_el and time_el.get("datetime"):
        dt_str = time_el["datetime"]
        try:
            # "Z" swapped for "+00:00" — fromisoformat rejected
            # it before Python 3.11
            parsed = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            published_at = parsed.replace(tzinfo=None).isoformat()
        except (ValueError, TypeError):
            # Unparsable <time> keeps utcnow(); the meta fallback
            # below is NOT tried (it hangs off the if, not the try)
            pass
    else:
        for meta in soup.find_all("meta", {"property": "article:published_time"}):
            dt_str = meta.get("content")
            if dt_str:
                try:
                    parsed = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                    published_at = parsed.replace(tzinfo=None).isoformat()
                except (ValueError, TypeError):
                    pass
                break


    # STEP 7: author — default is the faculty itself
    # ==============================================
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
