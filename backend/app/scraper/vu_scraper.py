############################################################
#  [*] Scraper — vu.lt news (the central newsroom)
#
#  Sibling of knf_scraper.py: pulls www.vu.lt/naujienos
#  into news_posts with source 'vu.lt' (the feed ranks it
#  +10, faculty news +15). Listing pages are walked while
#  they still hold unseen articles (?page=N, hard cap
#  MAX_LISTING_PAGES), each unseen article is fetched and
#  parsed on its own page, and the whole batch is written in
#  ONE short transaction after every fetch is done — the
#  SQLite write lock is never held across the network. Every
#  run gets a scraper_runs row that the admin GET
#  /api/scraper/status lists, and prunes run rows older than
#  30 days on its way out. vu.lt is a Next.js site — only
#  what the server renders into the initial HTML is seen,
#  nothing the client hydrates.
#
#  Dedup key and stored source_url are the canonical
#  normalise_url form of the POST-REDIRECT URL, so one
#  article behind two links (or a campaign-tagged one) is
#  stored once; INSERT OR IGNORE is the backstop and
#  _RUN_LOCK keeps a manual trigger from racing the timer
#  run at all.
#
#  scheduler.py runs it every 20 minutes (and 2 s after
#  boot); admins fire it by hand through POST
#  /api/scraper/trigger (alias /run — which passes
#  notify=False, so a hand-fired scrape never wakes a
#  device). A scheduled run that inserted anything sends ONE
#  push on the "news" channel — Lithuanian copy (declined
#  via scraper/plurals.py) plus an English variant for
#  devices registered with language 'en' — unless
#  common.push_allowed calls it a backfill, a burst or an
#  hourly repeat. published_at is naive UTC with the site's
#  offset APPLIED and clamped to the last five years — see
#  _fetch_vu_article.
############################################################


import logging
import re
import sqlite3
import threading
import uuid
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.database import get_db, utc_now_iso
from app.scraper.common import (
    MAX_CONTENT_LENGTH,
    MAX_SUMMARY_LENGTH,
    MAX_TITLE_LENGTH,
    VU_HOSTS,
    check_yield_drop,
    deadline_passed,
    fetch,
    host_allowed,
    load_deleted_urls,
    mark_run_failed,
    normalise_url,
    parse_source_datetime,
    prune_scraper_runs,
    push_allowed,
    run_deadline,
    sanitise_published_at,
    validate_image_url,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://www.vu.lt"
NEWS_URL = f"{BASE_URL}/naujienos"

# Paging stops as soon as a listing page holds nothing
# unseen; these two caps bound a first run against an empty
# database and a site that answers ?page=N with page 1
MAX_LISTING_PAGES = 5
MAX_ARTICLE_FETCHES = 20

# Wall-clock budget for one run — inside the 20-minute tick
RUN_BUDGET_SECONDS = 600

# An article path is the news category plus at least one more
# segment: "/naujienos/kazkoks-straipsnis",
# "/lt/visos-naujienos/tema/straipsnis". The category root
# itself ("/naujienos/", "/lt/naujienos") has no segment after
# it and is a LISTING page — storing one as an article is how
# a category page ended up in the feed. Segments BEFORE the
# category are free (a language prefix, a faculty section)
_ARTICLE_PATH_RE = re.compile(
    r"^(?:/[^/]+)*?/(?:visos-)?naujienos/(?:[^/]+/)*(?P<slug>[^/]+)/?$",
    re.IGNORECASE,
)

# Last segments that are navigation rather than a story
_NON_ARTICLE_SLUGS = frozenset({"naujienos", "visos-naujienos", "page", "puslapis"})

# One vu.lt run at a time, whoever asked for it
_RUN_LOCK = threading.Lock()








############################################################
# scrape_vu_news
############################################################
#
# One run: takes the source lock, opens a scraper_runs row,
# walks listing pages while they still hold unseen articles,
# fetches and parses every candidate, writes the batch in one
# short transaction, closes the row, prunes old runs and
# pushes. Returns {"found", "new"} — {"found": 0, "new": 0,
# "error"} when the FIRST listing fetch or anything else
# fails, and {"skipped": True} when another run holds the
# lock. `found` counts distinct article links considered,
# stored or not; `new` counts rows actually inserted
# (INSERT OR IGNORE rowcount).
#
# `pages` is the MINIMUM number of listing pages walked:
# paging continues past it while pages yield unseen articles
# and stops on MAX_LISTING_PAGES, MAX_ARTICLE_FETCHES or the
# wall-clock budget. A link is only marked seen once it has
# a usable title, so a card whose image anchor precedes its
# text anchor is no longer lost for the run.
#
# A listing page that DOWNLOADED and yielded no article link
# fails the run: the whole harvest is one anchor heuristic
# against a Next.js site, and 'completed with zero' is how a
# markup change would otherwise stay invisible.
#
# `notify` separates the timer run from the admin trigger —
# a hand-fired scrape does not wake every device — and
# common.push_allowed still refuses a backfill, a burst and
# a second push within the hour.
#
# The failure path rolls back before recording the run as
# failed, on a FRESH connection, so a half-written batch is
# discarded instead of persisting under a 'failed' row and a
# broken connection cannot raise a second time out of the
# scraper. A process death mid-run leaves the row 'running'
# until the next scheduler start reconciles it.
#
# Used by:
#   - scraper/scheduler.py — run_scrapers, every 20 min
#     and 2 s after boot (pages=1)
#   - scraper/routes.py — trigger_scrape, POST
#     /api/scraper/trigger and /run (admin, pages=1,
#     notify=False)
############################################################

def scrape_vu_news(pages=1, notify=True):
    # STEP 1: one vu.lt run at a time — a manual trigger that
    # overlaps the timer run steps aside instead of racing it
    # =======================================================
    if not _RUN_LOCK.acquire(blocking=False):
        logger.info("vu.lt scrape already running — this trigger is skipped")
        return {"found": 0, "new": 0, "skipped": True}

    run_id = str(uuid.uuid4())
    db = get_db()
    deadline = run_deadline(RUN_BUDGET_SECONDS)

    try:
        # STEP 2: register the run as 'running' — committed now, so the
        # admin status page shows it while the fetches take their time
        # =============================================================
        db.execute(
            "INSERT INTO scraper_runs (id, source, status, started_at) VALUES (?, 'vu.lt', 'running', ?)",
            (run_id, utc_now_iso()),
        )
        db.commit()

        # Articles an admin deleted from the feed stay deleted
        tombstoned = load_deleted_urls(db)

        articles_found = 0
        seen_urls = set()
        pending = []
        newest_published = None
        # Pages that actually downloaded — "the site is down" and
        # "the site is up and we recognised nothing" are different
        # failures and only the second is ours
        pages_fetched = 0


        # STEP 3: walk the listing pages. Only the FIRST page failing
        # is fatal — a later one just ends the paging
        # ===========================================================
        page_num = 0
        while page_num < MAX_LISTING_PAGES:
            if deadline_passed(deadline):
                logger.warning("vu.lt scrape out of time after %d listing page(s)", page_num)
                break

            params = None if page_num == 0 else {"page": page_num + 1}
            page_num += 1

            result = fetch(NEWS_URL, VU_HOSTS, params=params)
            if not result:
                if page_num == 1:
                    message = f"Failed to fetch {NEWS_URL}"
                    mark_run_failed(run_id, message)
                    return {"found": 0, "new": 0, "error": message, "runId": run_id}
                break

            pages_fetched += 1
            soup = BeautifulSoup(result[0], "lxml")
            unseen_on_page = 0

            # STEP 3.1: harvest article links from the server-rendered
            # HTML: anchors under /visos-naujienos/ or /naujienos/ that
            # carry a real title, resolved and canonicalised
            for link_data in _listing_links(soup):
                full_url = normalise_url(urljoin(BASE_URL, link_data["href"]))

                # An injected absolute href must not become an
                # outbound probe published to guests
                if not host_allowed(full_url, VU_HOSTS):
                    logger.warning("Skipping off-allowlist article link %s", full_url)
                    continue

                if full_url in seen_urls:
                    continue
                seen_urls.add(full_url)

                # Counted as found even when already stored or unfetchable
                articles_found += 1

                if full_url in tombstoned:
                    continue

                existing = db.execute(
                    "SELECT id FROM news_posts WHERE source_url = ?", (full_url,)
                ).fetchone()
                if existing:
                    continue

                unseen_on_page += 1

                # STEP 3.2: fetch and park the article; nothing is
                # written until every fetch of the run is done
                if len(pending) >= MAX_ARTICLE_FETCHES or deadline_passed(deadline):
                    break

                # None only on a transport error — a page that parsed to
                # nothing still comes back and is inserted as "Untitled"
                article_data = _fetch_vu_article(full_url)
                if not article_data:
                    continue

                # The stored key is the POST-REDIRECT URL: two links to
                # the same article collapse to one row. The listing
                # title rides along as the title of last resort
                pending.append((article_data["url"], link_data["title"], article_data))
                if newest_published is None or article_data["date"] > newest_published:
                    newest_published = article_data["date"]

            # A page holding nothing unseen means the pages behind it
            # are older still — stop once the caller's minimum is met
            if unseen_on_page == 0 and page_num >= pages:
                break
            if len(pending) >= MAX_ARTICLE_FETCHES or deadline_passed(deadline):
                logger.info("vu.lt scrape stopped at listing page %d (fetch cap or budget)", page_num)
                break


        # STEP 3.3: a listing that downloaded and held not one
        # recognisable article link is a markup change — fail the
        # run so /status shows it instead of a tidy zero
        # =======================================================
        if pages_fetched and articles_found == 0:
            message = "no article links on the vu.lt listing — the markup has probably changed"
            logger.error("vu.lt scrape found nothing on %d downloaded listing page(s)", pages_fetched)
            mark_run_failed(run_id, message)
            return {"found": 0, "new": 0, "error": message, "runId": run_id}


        # STEP 4: ONE short write transaction, every fetch already done
        # =============================================================
        articles_new = 0
        for full_url, listing_title, article_data in pending:
            # The article page's own <h1>, then the listing link
            # text — an unparsable page used to be stored as
            # "Untitled" forever, and no later run would revisit it
            title = (article_data["title"] or listing_title)[:MAX_TITLE_LENGTH]

            # Nothing recognisable on the page at all: write NO row,
            # so the article is simply retried on the next run
            if not title and not article_data["content"]:
                logger.warning("vu.lt article parsed to nothing — not stored: %s", full_url)
                continue

            # The same story republished under a second URL. Only
            # with a title — an empty one would match every other
            # title-less row of this source
            duplicate = None
            if title:
                duplicate = db.execute(
                    "SELECT id FROM news_posts WHERE source = 'vu.lt' AND title = ?",
                    (title,),
                ).fetchone()
            if duplicate:
                logger.info("vu.lt article already stored under another URL: %s", title)
                continue

            cursor = db.execute(
                """INSERT OR IGNORE INTO news_posts
                   (id, title, content, summary, image_url, author_name, source, source_url, post_type, published_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'vu.lt', ?, 'article', ?)""",
                (
                    str(uuid.uuid4()),
                    title,
                    article_data["content"],
                    article_data["summary"],
                    article_data["image_url"],
                    article_data["author"],
                    full_url,
                    article_data["date"],
                ),
            )
            articles_new += cursor.rowcount

        db.commit()


        # STEP 5: close the run — counts and finished_at in one UPDATE,
        # then the 30-day retention pass over scraper_runs
        # =============================================================
        db.execute(
            """UPDATE scraper_runs
               SET status = 'completed', articles_found = ?, articles_new = ?, finished_at = ?
               WHERE id = ?""",
            (articles_found, articles_new, utc_now_iso(), run_id),
        )
        db.commit()

        # A run that still 'completed' but harvested a tenth of
        # what the last one did gets its own ERROR line
        check_yield_drop(db, "vu.lt", articles_found, run_id)

        prune_scraper_runs(db)

        # The newest article seen this run is logged so a gap
        # between source and feed is visible in the container log
        logger.info("vu.lt scrape complete: found=%d, new=%d, newest=%s",
                    articles_found, articles_new, newest_published)


        # STEP 6: one push for the whole run on the "news" channel —
        # every active token unless its user opted that channel out;
        # a push failure is logged and does not fail the run. The
        # admin trigger passes notify=False, and push_allowed
        # refuses a backfill, a burst and an hourly repeat
        # ==========================================================
        if articles_new > 0 and notify and push_allowed(db, "vu.lt", articles_new, run_id):
            try:
                # Lazy import, as in knf_scraper.py
                from app.notifications.push import notify_channel
                from app.scraper.plurals import lt_plural
                title = "VU naujienos" if articles_new == 1 else f"VU naujienos ({articles_new})"
                # lt_plural picks the declined Lithuanian form:
                # 21 is singular again, 10 takes the genitive
                phrase = lt_plural(articles_new, ("naujas straipsnis", "nauji straipsniai", "nauj\u0173 straipsni\u0173"))
                body = f"Naujas straipsnis i\u0161 vu.lt" if articles_new == 1 else f"{articles_new} {phrase} i\u0161 vu.lt"
                title_en = "VU news" if articles_new == 1 else f"VU news ({articles_new})"
                body_en = "New article from vu.lt" if articles_new == 1 else f"{articles_new} new articles from vu.lt"
                notify_channel("news", title, body, data={"type": "news", "source": "vu.lt"},
                               title_en=title_en, body_en=body_en)
            except Exception:
                logger.exception("Failed to send push notification for new vu.lt articles")

        return {"found": articles_found, "new": articles_new, "runId": run_id}

    except Exception as e:
        # Roll back FIRST so a half-written batch is discarded
        # instead of riding along with the 'failed' status, then
        # close the run row on a connection known to work
        logger.exception("vu.lt scraper error")
        try:
            db.rollback()
        except sqlite3.Error:
            logger.warning("Rollback after the vu.lt failure did not take", exc_info=True)
        mark_run_failed(run_id, str(e))
        return {"found": 0, "new": 0, "error": str(e), "runId": run_id}
    finally:
        db.close()
        _RUN_LOCK.release()








############################################################
# _listing_links
############################################################
#
# The article cards on one listing page as [{"href",
# "title"}], in page order. An href qualifies only when its
# PATH is article-shaped (_is_article_href): the news
# category plus at least one more segment, so a category
# root or a paging link can no longer be stored as a news
# article. Counting slashes used to stand in for that and
# let "/lt/naujienos/" through.
#
# A card is usually two anchors around the same href — the
# image first, the headline second — so every anchor for an
# href is considered and the LONGEST usable text wins; an
# anchor with no text at all falls back to the first of its
# title / aria-label attributes that holds more than
# whitespace. Marking the href seen on the first anchor is
# what used to drop every image-first card.
#
# The anchor text is joined with SPACES: a headline split
# across <span>/<b> came out as one run-together word, and
# that word is what a page with no <h1> stores as its title.
#
# Used by:
#   - scrape_vu_news (above) — once per listing page
############################################################

def _listing_links(soup) -> list[dict]:
    # STEP 1: best title per article href, page order kept
    # ====================================================
    best = {}
    order = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not _is_article_href(href):
            continue

        # STEP 1.1: the anchor's own text, joined with SPACES —
        # a headline broken across inline tags used to harvest as
        # "Naujasmokslocentras", and this text is the title of
        # last resort actually written to news_posts.title
        title = a.get_text(" ", strip=True)

        # STEP 1.2: else what an image-only anchor carries in an
        # attribute. Each candidate is stripped BEFORE it is
        # judged: a blank-but-present title="   " is truthy, and
        # used to shadow the usable aria-label beside it
        if len(title) <= 10:
            attr_title = ""
            for value in (a.get("title"), a.get("aria-label")):
                attr_title = (value or "").strip()
                if attr_title:
                    break

            if len(attr_title) > len(title):
                title = attr_title

        # Still a bare icon or a "Daugiau" label — this ANCHOR is
        # unusable, the href stays open for the card's other one
        if len(title) <= 10:
            continue

        # STEP 1.3: first sighting keeps page order, a later
        # anchor only wins with a longer headline
        if href not in best:
            order.append(href)
            best[href] = title
        elif len(title) > len(best[href]):
            best[href] = title

    return [{"href": href, "title": best[href]} for href in order]








############################################################
# _is_article_href
############################################################
#
# True when an href points at a vu.lt news ARTICLE rather
# than the listing it was found on. The path must reach the
# news category ("naujienos" or "visos-naujienos", under any
# leading segments) and carry at least one more segment
# after it, and that last segment must not itself be
# navigation ("page", "naujienos", …). Query and fragment
# are ignored, so "/naujienos/?page=2" is rejected on its
# path alone.
#
# Used by:
#   - _listing_links (above) — once per anchor
############################################################

def _is_article_href(href: str) -> bool:
    if not href:
        return False

    try:
        path = urlparse(urljoin(BASE_URL, href)).path
    except ValueError:
        return False

    match = _ARTICLE_PATH_RE.match(path)
    if not match:
        return False

    slug = match.group("slug").lower()

    return slug not in _NON_ARTICLE_SLUGS and not slug.isdigit()








############################################################
# _fetch_vu_article
############################################################
#
# Fetches one article page and boils it down to {url, title,
# content, summary, image_url, date, author}. Returns None
# ONLY when the fetch fails or is refused (the caller then
# skips the link); a page that yields nothing comes back
# with an EMPTY title, which is what lets the caller fall
# back to the listing link text and, failing even that,
# store no row at all. "url" is the canonical POST-REDIRECT
# URL — the key the caller stores and dedupes on. The image
# candidates go through common.validate_image_url.
# Everything else is
# selector guesswork against a Next.js site with no stable
# markup contract:
#
#   - title: the first <h1> HOLDING TEXT. The three fallback
#     selectors ("article h1", "[class*='title'] h1",
#     "main h1") all match a subset of "h1", so they only
#     ever fire when an empty <h1> stands in front of the
#     real one — a bs4 4.13 tag is truthy whatever it holds
#   - content: <article>, else [class*='content'], else
#     <main> ("main article" is dead the same way), taking
#     the first that is left with text;
#     script/style/nav/header/footer/aside inside it are
#     decompose()d, which MUTATES the shared soup — the
#     image and date lookups below only see what survived,
#     and a region that was nothing but chrome hands the
#     ladder on instead of ending it
#   - summary: og:description / meta description when the
#     page carries one, else the body with its leading
#     chrome dropped — see _article_summary
#   - image: og:image verbatim, else the first <img> whose
#     src names vu.lt and is not a logo/icon/pixel/
#     tracking/avatar; a relative src gets BASE_URL
#     ("newshub.vu.lt" is a redundant test — it contains
#     "vu.lt")
#   - date: the first article:published_time meta that
#     carries a content attribute, else <time>
#     datetime attr or text, parsed by
#     common.parse_source_datetime — a negative offset
#     parses now too, the offset is APPLIED rather than
#     dropped, and the result is clamped to the last five
#     years
#   - author: the constant "Vilniaus universitetas"
#
# Title, content and summary come back cut to the same
# limits news/routes.py enforces on hand-written posts.
#
# Used by:
#   - scrape_vu_news (above) — once per unseen link
############################################################

def _fetch_vu_article(url):
    # STEP 1: fetch the page — a refused or failed fetch is the
    # one None exit
    # =========================================================
    result = fetch(url, VU_HOSTS)
    if not result:
        return None

    body, final_url = result
    soup = BeautifulSoup(body, "lxml")


    # STEP 2: title — the first <h1> that actually holds text. A
    # bs4 4.13 tag is truthy whatever it contains, so an empty
    # <h1> used to END the ladder; only now can the narrower
    # selectors below it reach the real headline
    # =============================================================
    title = ""
    for selector in ["h1", "article h1", "[class*='title'] h1", "main h1"]:
        el = soup.select_one(selector)
        if el is None:
            continue

        text = el.get_text(strip=True)
        if text:
            title = text
            break


    # STEP 3: content — the first region left with text once its
    # chrome is decompose()d out of the SHARED soup, then the
    # teaser. An empty <article> in front of a populated <main>
    # is the same bs4 truthiness trap as the title ladder
    # ============================================================
    content = ""
    for selector in ["article", "main article", "[class*='content']", "main"]:
        el = soup.select_one(selector)
        if el is None:
            continue

        for tag in el.find_all(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()

        text = el.get_text(separator="\n", strip=True)
        if text:
            content = text
            break

    summary = _article_summary(soup, content, title)


    # STEP 4: image — og:image wins (the reliable one on a Next.js
    # site), else the first <img> that is not chrome.
    # validate_image_url resolves the src against THIS page and
    # holds it to the image host allowlist and the length cap, so
    # the "vu.lt in src" substring test is no longer the guard
    # ============================================================
    image_url = None
    og_image = soup.find("meta", {"property": "og:image"})
    if og_image and og_image.get("content"):
        image_url = validate_image_url(final_url, og_image["content"])

    if not image_url:
        for img in soup.find_all("img", src=True):
            src = img["src"]
            if any(skip in src.lower() for skip in ["logo", "icon", "pixel", "tracking", "avatar"]):
                continue
            image_url = validate_image_url(final_url, src)
            if image_url:
                break


    # STEP 5: date — article:published_time meta, else <time>; the
    # source's UTC offset is applied and the result clamped
    # ============================================================
    date_str = None
    for meta in soup.find_all("meta", {"property": "article:published_time"}):
        # The first meta CARRYING a date, not simply the first
        # one: a bare <meta property="article:published_time">
        # used to discard the good one below it
        published = (meta.get("content") or "").strip()
        if published:
            date_str = published
            break
    if not date_str:
        time_el = soup.find("time")
        if time_el:
            date_str = time_el.get("datetime") or time_el.get_text(strip=True)

    published_at = sanitise_published_at(parse_source_datetime(date_str))

    author = "Vilniaus universitetas"

    # Cut to the limits news/routes.py enforces on hand-written
    # posts — a scraped row used to bypass them entirely. An
    # empty title stays EMPTY: the caller has the listing link
    # text to fall back on, "Untitled" defeated it
    return {
        "url": normalise_url(final_url),
        "title": title[:MAX_TITLE_LENGTH],
        "content": content[:MAX_CONTENT_LENGTH],
        "summary": summary[:MAX_SUMMARY_LENGTH],
        "image_url": image_url,
        "date": published_at,
        "author": author,
    }








############################################################
# _article_summary
############################################################
#
# The teaser stored in news_posts.summary, and the one the
# mobile news card shows. og:description (or the plain meta
# description) wins when the page has one — on a Next.js
# site that is the only summary with an editor behind it.
# Otherwise the body text is used with its LEADING CHROME
# dropped: breadcrumbs, the repeated title, dates and
# bylines are all short lines, so lines under 40 characters,
# lines equal to the title and date-shaped lines are skipped
# until the first real paragraph. Without this the median
# vu.lt teaser was a few dozen characters of navigation.
# The result is 200 characters cut back to a word boundary.
#
# Used by:
#   - _fetch_vu_article (above)
############################################################

def _article_summary(soup, content: str, title: str) -> str:
    # STEP 1: the page's own description, when it has one
    # ===================================================
    for attrs in ({"property": "og:description"}, {"name": "description"}):
        meta = soup.find("meta", attrs)
        described = (meta.get("content") or "").strip() if meta else ""
        if len(described) >= 40:
            return described


    # STEP 2: the body minus its leading chrome — the same rule
    # the client's stripScrapedPreamble applies for display
    # =========================================================
    body = []
    for line in content.split("\n"):
        line = line.strip()
        if not body and _looks_like_chrome(line, title):
            continue
        if line:
            body.append(line)

    text = " ".join(body) or content.strip()
    if len(text) <= 200:
        return text

    return text[:200].rsplit(" ", 1)[0] + "..."








############################################################
# _looks_like_chrome
############################################################
#
# True for a line that is page furniture rather than the
# article: empty, shorter than 40 characters (breadcrumbs,
# tags, "Dalintis"), the title repeated, or a short
# date/byline line ("2026-08-29", "2026 m. rugpjūčio 29
# d."). Only ever applied to the lines BEFORE the first real
# paragraph, so a short sentence inside the body is safe.
#
# Used by:
#   - _article_summary (above)
############################################################

def _looks_like_chrome(line: str, title: str) -> bool:
    if len(line) < 40:
        return True

    if title and line.casefold() == title.casefold():
        return True

    if len(line) < 80 and (re.match(r"^\d{4}[-./\s]", line) or re.search(r"\d{4}\s*m\.", line)):
        return True

    return False
