############################################################
#  [*] Scraper — knf.vu.lt news into news_posts
#
#  Pulls the faculty news listing (knf.vu.lt/aktualijos, a
#  Joomla site paginated 5 per page via ?start=<offset>),
#  fetches every article page not yet in news_posts and
#  inserts it with source='knf.vu.lt', post_type='article'
#  and no author_id. Deduplication is by source_url in its
#  canonical normalise_url shape, which is also UNIQUE in
#  the schema — the SELECT is the fetch-avoidance path and
#  the DO NOTHING insert the backstop, so a lost race costs one
#  row instead of the whole run. A title already stored
#  under this source counts as the same article too, which
#  catches one story republished under a second URL.
#
#  Paging continues while a listing page still holds unseen
#  articles (hard cap MAX_LISTING_PAGES), so a publishing
#  burst is picked up instead of falling off the end of page
#  two. Every run is logged in scraper_runs (running →
#  completed/failed) for GET /api/scraper/status and prunes
#  run rows older than 30 days on its way out.
#
#  Runs from two places: the cron container's scrape_news
#  command (pages=2, every 20 minutes) and POST
#  /api/scraper/trigger|/run (the same pages=2 and
#  notify=False) — `pages` is the MINIMUM number of listing
#  pages walked. Those are different PROCESSES, so the run
#  lock is two-layered: the module threading.Lock is the
#  in-process fast path, common.open_run the cross-process
#  guard — either one refusing reports {"skipped": True},
#  which the route turns into 409.
#
#  Network I/O never happens inside a write transaction: a
#  run fetches and parses everything first, then writes the
#  batch inside one transaction.atomic() block — which also
#  owns the rollback on failure.
#  published_at is aware UTC (+00:00) with the source's
#  offset APPLIED (not dropped) and clamped to the last
#  five years; created_at/updated_at are stamped aware UTC
#  too.
#
#  New articles trigger one push on the "news" channel —
#  Lithuanian copy (declined via scraper/plurals.py) with an
#  English variant for devices registered with language
#  'en'; a push failure is logged and does not fail the run.
#  common.push_allowed keeps a first-boot backfill, a
#  re-import burst and a second push inside the hour silent.
############################################################


import logging
import threading
import uuid
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from django.db import connection, transaction

from knfapp.common.timestamps import utc_now
from knfapp.news.models import NewsPost
from knfapp.scraper.common import (
    KNF_HOSTS,
    MAX_AUTHOR_LENGTH,
    MAX_CONTENT_LENGTH,
    MAX_SUMMARY_LENGTH,
    MAX_TITLE_LENGTH,
    check_yield_drop,
    close_run,
    deadline_passed,
    fetch,
    host_allowed,
    load_deleted_urls,
    mark_run_failed,
    normalise_url,
    open_run,
    parse_source_datetime,
    prune_scraper_runs,
    push_allowed,
    run_deadline,
    sanitise_published_at,
    validate_image_url,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://knf.vu.lt"
NEWS_URL = f"{BASE_URL}/aktualijos"

# Paging stops as soon as a listing page holds nothing
# unseen; these two caps bound a first run against an empty
# database (and any day the source starts looping pages)
MAX_LISTING_PAGES = 10
MAX_ARTICLE_FETCHES = 20

# Wall-clock budget for one run, comfortably inside the
# 20-minute tick — and the staleness horizon open_run uses
# to tell a live run from a corpse
RUN_BUDGET_SECONDS = 600

# One knf.vu.lt run at a time IN THIS PROCESS — the cheap
# half; open_run guards the other processes
_RUN_LOCK = threading.Lock()

# Every NOT NULL column named: the Django-built table
# carries no DDL defaults (the model's defaults are ORM-side
# and a raw INSERT walks past them). is_public is BOUND —
# Python True — so each engine stores its native boolean
_INSERT_SQL = """
    INSERT INTO news_posts
      (id, title, content, summary, image_url, author_name, source, source_url, post_type,
       is_public, likes_count, comments_count, shares_count, published_at, created_at, updated_at)
    VALUES (%s, %s, %s, %s, %s, %s, 'knf.vu.lt', %s, 'article', %s, 0, 0, 0, %s, %s, %s)
    ON CONFLICT (source_url) DO NOTHING
"""








############################################################
# scrape_knf_news
############################################################
#
# The run: take both halves of the source lock, open a
# scraper_runs row, walk listing pages while they still hold
# unseen articles, fetch and parse them all, write the batch
# in ONE transaction, close the row, prune old runs, push.
# The returned dict {"found", "new"} — plus "error" on
# failure or "skipped" when another run holds the source —
# is what the command logs and /api/scraper/trigger returns.
# "found" counts distinct listing links with a non-empty
# title INCLUDING ones already stored; "new" counts rows
# actually inserted (the DO NOTHING insert's rowcount, so a row
# another run wrote first is not counted twice). A link is
# only marked seen once it HAS that title, so the thumbnail
# anchor of a blog card cannot swallow the titled link
# beside it.
#
# `pages` is the MINIMUM number of listing pages walked:
# paging continues past it while pages keep yielding unseen
# articles and stops on MAX_LISTING_PAGES, MAX_ARTICLE_
# FETCHES or the wall-clock budget. A listing page that
# fails to download is skipped with a warning; an article
# page that fails is skipped and, having no row, is retried
# on the next run.
#
# A listing page that DOWNLOADED and still yielded no
# article link fails the run: every selector generation
# missing at once is a template change, not a quiet news
# day, and 'completed with zero' is how that would stay
# invisible for months.
#
# `notify` is what separates the timer run from the admin
# trigger: the scheduled run pushes, a hand-fired one does
# not wake every device in the faculty. Even with it on,
# common.push_allowed still refuses a backfill, a burst and
# a second push within the hour.
#
# Used by:
#   - management/commands/scrape_news.py — pages=2, notify on
#   - api/views.py — trigger_scrape (POST /api/scraper/
#     trigger and /run), pages=2, notify=False
############################################################

def scrape_knf_news(pages=2, notify=True):
    # STEP 1: the in-process lock, then the cross-process
    # conditional INSERT — either refusing means a live run
    # already owns knf.vu.lt and this trigger steps aside
    # ====================================================
    if not _RUN_LOCK.acquire(blocking=False):
        logger.info("knf.vu.lt scrape already running — this trigger is skipped")
        return {"found": 0, "new": 0, "skipped": True}

    try:
        run_id = open_run("knf.vu.lt", RUN_BUDGET_SECONDS)
        if run_id is None:
            return {"found": 0, "new": 0, "skipped": True}

        deadline = run_deadline(RUN_BUDGET_SECONDS)
        return _run(run_id, pages, notify, deadline)
    finally:
        _RUN_LOCK.release()


def _run(run_id, pages, notify, deadline):
    try:
        # STEP 2: what an admin deleted from the feed stays
        # deleted — re-inserting them is what the tombstones
        # prevent
        # =================================================
        tombstoned = load_deleted_urls()

        articles_found = 0
        seen_urls = set()
        pending = []
        newest_published = None
        # Pages that actually downloaded — the difference between
        # "the site is down" (not our problem) and "the site is up
        # and we recognised nothing on it" (very much our problem)
        pages_fetched = 0


        # STEP 3: walk the listing pages — Joomla paginates 5 per
        # page through ?start=<offset>; a page that fails to load
        # is skipped, not fatal
        # =======================================================
        page_num = 0
        while page_num < MAX_LISTING_PAGES:
            # Tested before the fetch too, so a site that is down
            # cannot burn the whole budget on retried listing pages
            if deadline_passed(deadline):
                logger.warning("knf.vu.lt scrape out of time after %d listing page(s)", page_num)
                break

            offset = page_num * 5
            url = NEWS_URL if offset == 0 else f"{NEWS_URL}?start={offset}"
            page_num += 1

            result = fetch(url, KNF_HOSTS)
            if not result:
                continue

            pages_fetched += 1
            soup = BeautifulSoup(result[0], "lxml")
            unseen_on_page = 0

            # STEP 3.1: one candidate per unseen article — run-wide
            # dedupe on the canonical URL, then the stored rows
            for link in _listing_links(soup):
                full_url = normalise_url(urljoin(BASE_URL, link["href"]))

                # An injected absolute href must not become an
                # outbound probe published to guests
                if not host_allowed(full_url, KNF_HOSTS):
                    logger.warning("Skipping off-allowlist article link %s", full_url)
                    continue

                if full_url in seen_urls:
                    continue

                # An empty link text is dropped before counting and
                # BEFORE the URL is marked seen: a Joomla card puts a
                # thumbnail anchor beside the titled one, and marking
                # the URL seen on the thumbnail would swallow the
                # article for the whole run. The text itself is kept
                # as the title of last resort for an article page
                # whose own markup parses to nothing
                listing_title = link.get_text(strip=True)
                if not listing_title:
                    continue

                seen_urls.add(full_url)
                articles_found += 1

                if full_url in tombstoned:
                    continue

                # Already scraped on an earlier run — counted in
                # "found", never re-fetched
                if NewsPost.objects.filter(source_url=full_url).exists():
                    continue

                unseen_on_page += 1

                # STEP 3.2: fetch and park the article; the insert
                # waits until every fetch of the run is done
                if len(pending) >= MAX_ARTICLE_FETCHES or deadline_passed(deadline):
                    break

                # None = download failed; no row is written, so
                # the next run tries this article again
                article_data = _fetch_article(full_url)
                if not article_data:
                    continue

                pending.append((full_url, listing_title, article_data))
                if newest_published is None or article_data["date"] > newest_published:
                    newest_published = article_data["date"]

            # A page holding nothing unseen means the pages behind
            # it are older still — stop once the caller's minimum
            # number of pages has been walked
            if unseen_on_page == 0 and page_num >= pages:
                break
            if len(pending) >= MAX_ARTICLE_FETCHES or deadline_passed(deadline):
                logger.info("knf.vu.lt scrape stopped at listing page %d (fetch cap or budget)", page_num)
                break


        # STEP 3.3: a listing that downloaded and held not one
        # recognisable article link is a template change — fail
        # the run so /status shows it instead of a tidy zero
        # =====================================================
        if pages_fetched and articles_found == 0:
            message = "no article links on the knf.vu.lt listing — the template has probably changed"
            logger.error("knf.vu.lt scrape found nothing on %d downloaded listing page(s)", pages_fetched)
            mark_run_failed(run_id, message)
            return {"found": 0, "new": 0, "error": message, "runId": run_id}


        # STEP 4: ONE short write transaction, every fetch already
        # done. ON CONFLICT DO NOTHING keeps a lost race to a single
        # skipped row instead of a failed run
        # ========================================================
        articles_new = 0
        with transaction.atomic():
            for full_url, listing_title, article_data in pending:
                # The article page's own title, then the listing
                # link text — storing an unparsable page as
                # "Untitled" would keep every later run from
                # revisiting it
                title = (article_data["title"] or listing_title)[:MAX_TITLE_LENGTH]

                # Nothing recognisable on the page at all: write NO
                # row, so the article is simply retried next run
                if not title and not article_data["content"]:
                    logger.warning("knf.vu.lt article parsed to nothing — not stored: %s", full_url)
                    continue

                # The same story republished under a second URL. Only
                # with a title — an empty one would match every other
                # title-less row of this source
                if title and NewsPost.objects.filter(source="knf.vu.lt", title=title).exists():
                    logger.info("knf.vu.lt article already stored under another URL: %s", title)
                    continue

                now = utc_now()
                with connection.cursor() as cursor:
                    cursor.execute(_INSERT_SQL, (
                        str(uuid.uuid4()),
                        title,
                        article_data["content"],
                        article_data["summary"],
                        article_data["image_url"],
                        article_data["author"],
                        full_url,
                        True,  # is_public — a scraped article is guest-visible
                        article_data["date"],
                        now,
                        now,
                    ))
                    articles_new += cursor.rowcount


        # STEP 5: close the run — the counts land in scraper_runs
        # for GET /api/scraper/status — and prune ancient rows
        # =======================================================
        close_run(run_id, articles_found, articles_new)

        # A run that still 'completed' but harvested a tenth of
        # what the last one did gets its own ERROR line
        check_yield_drop("knf.vu.lt", articles_found, run_id)

        prune_scraper_runs()

        # The newest article seen this run is logged so a gap
        # between source and feed is visible in the container log
        logger.info("knf.vu.lt scrape complete: found=%d, new=%d, newest=%s",
                    articles_found, articles_new, newest_published)


        # STEP 6: one push for the whole run on the "news" channel
        # (opt-outs honoured inside notify_channel); a push failure
        # is logged and the run still counts as completed. The
        # admin trigger passes notify=False, and push_allowed
        # refuses a backfill, a burst and an hourly repeat
        # =========================================================
        if articles_new > 0 and notify and push_allowed("knf.vu.lt", articles_new, run_id):
            try:
                from knfapp.notifications.push import notify_channel
                from knfapp.scraper.plurals import lt_plural
                title = "KNF naujienos" if articles_new == 1 else f"KNF naujienos ({articles_new})"
                # lt_plural picks the declined Lithuanian form:
                # 21 is singular again, 10 takes the genitive
                phrase = lt_plural(articles_new, ("naujas straipsnis", "nauji straipsniai", "naujų straipsnių"))
                body = f"Naujas straipsnis iš knf.vu.lt" if articles_new == 1 else f"{articles_new} {phrase} iš knf.vu.lt"
                title_en = "KNF news" if articles_new == 1 else f"KNF news ({articles_new})"
                body_en = "New article from knf.vu.lt" if articles_new == 1 else f"{articles_new} new articles from knf.vu.lt"
                notify_channel("news", title, body, data={"type": "news", "source": "knf.vu.lt"},
                               title_en=title_en, body_en=body_en)
            except Exception:
                logger.exception("Failed to send push notification for new knf.vu.lt articles")

        return {"found": articles_found, "new": articles_new, "runId": run_id}

    except Exception as e:
        # Anything that escaped the per-page guards. The atomic
        # block has already rolled the half-written batch back;
        # the run row is closed through a freshened connection
        logger.exception("knf.vu.lt scraper error")
        mark_run_failed(run_id, str(e))
        return {"found": 0, "new": 0, "error": str(e), "runId": run_id}








############################################################
# _listing_links
############################################################
#
# The article anchors on one listing page, across three
# template generations — h2.article-title > a is the current
# one, h4 > a the older Joomla shape, and any element
# carrying an article-title class the last resort. The first
# generation that matches anything wins; every anchor is
# checked for "/aktualijos/" so the sidebar and menu links
# are left behind.
#
# Used by:
#   - scrape_knf_news (above) — once per listing page
############################################################

def _listing_links(soup):
    # STEP 1: the current template
    # ============================
    links = []
    for h2 in soup.select("h2.article-title"):
        a = h2.find("a", href=True)
        if a and "/aktualijos/" in a["href"]:
            links.append(a)

    # STEP 1.1: the older Joomla template
    if not links:
        for h4 in soup.find_all("h4"):
            a = h4.find("a", href=True)
            if a and "/aktualijos/" in a["href"]:
                links.append(a)

    # STEP 1.2: any element carrying an article-title class,
    # whatever the heading level
    if not links:
        for heading in soup.select("[class*='article-title'] a"):
            if "/aktualijos/" in heading.get("href", ""):
                links.append(heading)

    return links








############################################################
# _fetch_article
############################################################
#
# Downloads one article page through the pooled session
# (host allowlist, tuple timeouts, byte cap) and scrapes
# title, content, summary, image, date and author with a
# chain of fallbacks per field (the Joomla template has
# changed before; the first selector in each list is the
# current one). None only when the download fails or the
# response is refused — a page that parses to nothing comes
# back with an EMPTY title, which is what lets the caller
# fall back to the listing link text and, failing even that,
# store no row at all. Title, content and summary come back
# already cut to the same limits the news views enforce on
# hand-written posts.
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
#             like a logo/icon/banner/pixel; every candidate
#             goes through common.validate_image_url
#   date    — a <time> inside the article body, then the
#             page's first <time>, then
#             article:published_time, then og:updated_time,
#             then now. Each source falls through to the
#             next when it is missing OR unparsable; the
#             source offset is applied and the result
#             clamped to the last five years
#   author  — .article-author, .author, .createdby or
#             span.author, the first one holding actual TEXT
#             (an empty match leaves the default alone);
#             default "VU Kauno fakultetas", cut to
#             MAX_AUTHOR_LENGTH like every other stored field
#
# Used by:
#   - scrape_knf_news (above) — once per unseen article
############################################################

def _fetch_article(url):
    # STEP 1: download — None on failure; the caller skips the
    # article and, with no row written, retries it next run
    # ========================================================
    result = fetch(url, KNF_HOSTS)
    if not result:
        return None

    body, page_url = result
    soup = BeautifulSoup(body, "lxml")


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
    # not look like page chrome. validate_image_url resolves the
    # src against THIS page (root-relative, protocol-relative and
    # bare relative all become absolute) and holds it to the
    # image host allowlist and the length cap
    # ==========================================================
    image_url = None
    og_image = soup.find("meta", {"property": "og:image"})
    if og_image and og_image.get("content"):
        image_url = validate_image_url(page_url, og_image["content"])

    if not image_url:
        for img in soup.find_all("img", src=True):
            src = img["src"]
            if any(skip in src.lower() for skip in ["logo", "icon", "banner", "pixel", "tracking"]):
                continue
            image_url = validate_image_url(page_url, src)
            if image_url:
                break


    # STEP 6: date — four sources tried IN ORDER, each falling
    # through to the next when it is missing OR unparsable, so
    # one broken <time> cannot cost the article its real
    # publication date. The offset is APPLIED (Vilnius wall
    # clock converted to UTC) and the result clamped
    # ========================================================
    parsed = None

    # STEP 6.1: a <time> inside the article body beats the
    # page's first one, which can belong to a sidebar teaser
    for container in [".article-content", ".item-page .article-body", ".item-content", "article"]:
        el = soup.select_one(container)
        if el is None:
            continue
        for time_el in el.find_all("time"):
            parsed = parse_source_datetime(time_el.get("datetime") or time_el.get_text(strip=True))
            if parsed is not None:
                break
        if parsed is not None:
            break

    # STEP 6.2: any <time> on the page
    if parsed is None:
        for time_el in soup.find_all("time"):
            parsed = parse_source_datetime(time_el.get("datetime") or time_el.get_text(strip=True))
            if parsed is not None:
                break

    # STEP 6.3: the metas, published before updated
    if parsed is None:
        for prop in ("article:published_time", "og:updated_time"):
            for meta in soup.find_all("meta", {"property": prop}):
                parsed = parse_source_datetime(meta.get("content"))
                if parsed is not None:
                    break
            if parsed is not None:
                break

    # STEP 6.4: nothing parsed — sanitise_published_at stamps now
    published_at = sanitise_published_at(parsed)


    # STEP 7: author — default is the faculty itself. What ends
    # the ladder is the TEXT, not the match: a bs4 tag is truthy
    # whatever it holds, so an empty <span class="article-author">
    # would serve every guest a blank byline
    # ==========================================================
    author = "VU Kauno fakultetas"
    for selector in [".article-author", ".author", ".createdby", "span.author"]:
        el = soup.select_one(selector)
        if el is None:
            continue

        text = el.get_text(strip=True)
        if text:
            author = text
            break

    # Cut to the limits the news routes enforce on hand-
    # written posts — a scraped row must not bypass them. An
    # empty title stays EMPTY: the caller has the listing
    # link text to fall back on
    return {
        "title": title[:MAX_TITLE_LENGTH],
        "content": content[:MAX_CONTENT_LENGTH],
        "summary": summary[:MAX_SUMMARY_LENGTH],
        "image_url": image_url,
        "date": published_at,
        "author": author[:MAX_AUTHOR_LENGTH],
    }
