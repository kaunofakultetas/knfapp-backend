############################################################
#  [*] Scraper plumbing — HTTP, URL hygiene, run bookkeeping
#
#  The four scrapers (knf_scraper, vu_scraper,
#  schedule_scraper, info_scraper) talk to three hosts under
#  the same rules, so the shared half lives here:
#
#    - ONE pooled requests.Session with a retry/backoff
#      adapter, reused by every scraper — before this each
#      page fetch opened its own TLS connection
#    - fetch() — the host allowlist is checked BEFORE the
#      request and again AFTER redirects (an injected
#      absolute href must never become an SSRF probe), with
#      (connect, read) tuple timeouts, a Content-Type check
#      and a hard byte cap on the body
#    - run_deadline / deadline_passed — the wall-clock budget
#      a run tests between fetches: a scalar read timeout is
#      NOT a total timeout, and max_instances=1 lets one
#      wedged run swallow every later tick
#    - normalise_url — the dedup key for
#      news_posts.source_url: post-redirect, no fragment, no
#      tracking params, canonical scheme/host, no trailing
#      slash (migration v35 normalised the rows written
#      before this existed)
#    - utc_now_naive / parse_source_datetime /
#      sanitise_published_at — naive-UTC timestamps with the
#      source's offset APPLIED instead of dropped, plus a
#      [now - 5 years, now] clamp so a mis-parsed year cannot
#      pin an article to the top of the feed
#    - mark_run_failed / prune_scraper_runs /
#      load_deleted_urls — the scraper_runs and tombstone
#      bookkeeping all four scrapers repeat
#    - validate_image_url — a scraped <img>/og:image src
#      resolved against its page, capped and held to the
#      image host allowlist, the way _validate_avatar_url
#      treats a user-supplied one
#    - push_allowed / check_yield_drop — the two run-shape
#      guards: no push for a backfill or a burst (and at
#      most one per source per hour), and one ERROR line
#      when a run's yield collapses against the last
#      successful one
#
#  mark_run_failed opens its OWN connection on purpose: it
#  runs exactly when the caller's connection is the thing
#  that broke.
############################################################


import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.database import get_db, utc_now_iso

logger = logging.getLogger(__name__)

USER_AGENT = "KNFAPP/1.0 (Vilnius University Kaunas Faculty Mobile App)"

# (connect, read) — a scalar timeout only ever bounds the
# quiet time between bytes, never the whole request
DEFAULT_TIMEOUT = (5, 20)

# Nothing the faculty publishes is anywhere near this big;
# the cap is what stops a slow multi-megabyte body
MAX_RESPONSE_BYTES = 2_000_000

HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")
JSON_CONTENT_TYPES = ("application/json", "text/json")

# Per-scraper allowlists — every scraped href is resolved
# against its scraper's own base URL and rejected when the
# resulting host is not in the matching set
KNF_HOSTS = frozenset({"knf.vu.lt", "www.knf.vu.lt"})
VU_HOSTS = frozenset({"vu.lt", "www.vu.lt"})
SCHEDULE_HOSTS = frozenset({"tvarkarasciai.vu.lt"})

# Where a scraped image may live. Wider than the two page
# allowlists on purpose — both sites serve their article
# images from the shared newshub host — and never a wildcard:
# a stored image_url is handed to every guest's <Image>
IMAGE_HOSTS = frozenset({
    "knf.vu.lt", "www.knf.vu.lt",
    "vu.lt", "www.vu.lt",
    "newshub.vu.lt", "www.newshub.vu.lt",
})

# news_posts.image_url is TEXT with no cap; a data: URI or a
# tracking URL with a kilobyte of query is not an image
MAX_IMAGE_URL_LENGTH = 2048

# The same limits news/routes.py enforces on hand-written
# posts; scraped rows used to bypass them entirely
MAX_TITLE_LENGTH = 200
MAX_CONTENT_LENGTH = 10000
MAX_SUMMARY_LENGTH = 500

# news_posts.author_name is TEXT with no cap of its own, and a
# byline comes off the source page like everything else — the
# longest real one is an institute name, nowhere near this
MAX_AUTHOR_LENGTH = 120

# A published_at outside this window is a parse accident,
# not news — the feed ranking divides by article age
MAX_ARTICLE_AGE_DAYS = 5 * 365

# scraper_runs retention: rows older than this are pruned at
# the end of every run (migration v19 did the one-off pass)
RUN_RETENTION_DAYS = 30

_TRACKING_PREFIXES = ("utm_",)
_TRACKING_PARAMS = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid", "_ga", "ref"})

# Push guards. More new rows than this in one run is an
# import, not news (a first boot, a re-import, a source that
# republished its whole archive) — and whatever the count, a
# source may wake every device at most once an hour
PUSH_BURST_THRESHOLD = 25
PUSH_MIN_INTERVAL_SECONDS = 3600

# source -> monotonic instant of its last allowed push;
# per PROCESS, so a restart forgives the hourly cap
_LAST_PUSH = {}
_LAST_PUSH_LOCK = threading.Lock()

# Built once on first use and reused by every scraper thread
_SESSION = None








############################################################
# get_session
############################################################
#
# The one pooled requests.Session, built on first use: a
# retry/backoff adapter on both schemes (2 retries, 0.5 s
# backoff factor, GET only, retrying the transient statuses
# 429/500/502/503/504) over a small connection pool, so a
# run of twenty article pages reuses one TLS connection
# instead of opening twenty. Two threads racing the first
# call can build two sessions and one of them wins the
# global — harmless, the loser is simply garbage.
#
# Used by:
#   - fetch (below) — every scraper request goes through it
############################################################

def get_session() -> requests.Session:
    global _SESSION

    if _SESSION is not None:
        return _SESSION

    session = requests.Session()
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        status=2,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    _SESSION = session
    return _SESSION








############################################################
# host_allowed
############################################################
#
# True only for an http(s) URL whose hostname is in the
# given allowlist — the scrapers publish what they find to
# guests, so a link the source page was made to carry
# ("https://169.254.169.254/…") must never be fetched. The
# port is ignored; both sites serve on the defaults.
#
# Used by:
#   - fetch (below) — before the request and again on the
#     post-redirect URL
#   - knf_scraper.py — scrape_knf_news, on every listing link
#   - vu_scraper.py — scrape_vu_news, on every listing link
############################################################

def host_allowed(url: str, allowed_hosts) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False

    if parsed.scheme not in ("http", "https"):
        return False

    return (parsed.hostname or "").lower() in allowed_hosts








############################################################
# normalise_url
############################################################
#
# The canonical form of an article URL, used both as the
# news_posts.source_url dedup key and as the stored value:
# https scheme, lowercase host without a leading "www.", no
# fragment, tracking parameters (utm_*, fbclid, gclid, …)
# dropped, and no trailing slash on a non-root path. Two
# links to the same article — one from a listing, one after
# a redirect, one with a campaign tag — collapse to one key.
# Anything unparsable is handed back stripped, never
# dropped.
#
# Used by:
#   - knf_scraper.py, vu_scraper.py — the dedup key and the
#     stored source_url
#   - load_deleted_urls (below) — tombstones are matched in
#     the same shape
#   - database/__init__.py — _migration_v35_normalise_source_
#     urls, so old rows carry today's shape
############################################################

def normalise_url(url: str) -> str:
    if not url:
        return url

    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return url.strip()

    # A bare relative reference has no host to canonicalise
    if not parsed.netloc:
        return url.strip()


    # STEP 1: host and scheme — "www." is the same site, and
    # both faculty sites redirect http to https anyway
    # ======================================================
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]


    # STEP 2: path without its trailing slash, query without
    # the campaign tags, fragment gone entirely
    # ======================================================
    path = parsed.path or "/"
    if len(path) > 1:
        path = path.rstrip("/") or "/"

    kept = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_PARAMS
        and not key.lower().startswith(_TRACKING_PREFIXES)
    ]

    return urlunparse(("https", host, path, "", urlencode(kept), ""))








############################################################
# fetch
############################################################
#
# One guarded GET through the pooled session, answering
# (body bytes, post-redirect URL) or None — never raising.
# Four guards the raw requests.get calls never had:
#   - the host allowlist, tested on the requested URL and
#     again on resp.url after redirects
#   - (connect, read) tuple timeouts
#   - a Content-Type check, so an unexpected PDF or image is
#     not parsed as a page
#   - a byte cap read chunk by chunk, so an endless body is
#     cut instead of swallowing the run
# The body comes back as BYTES on purpose: BeautifulSoup
# sniffs the document's own charset, which is more reliable
# than the ISO-8859-1 requests falls back to when the header
# carries no charset.
#
# Used by:
#   - knf_scraper.py — the listing and article pages
#   - vu_scraper.py — the listing and article pages
#   - info_scraper.py — _fetch_page
#   - schedule_scraper.py — the group list and event feeds
############################################################

def fetch(url: str, allowed_hosts, params=None, timeout=DEFAULT_TIMEOUT,
          content_types=HTML_CONTENT_TYPES, max_bytes=MAX_RESPONSE_BYTES,
          extra_headers=None):
    # STEP 1: refuse anything off the allowlist before a
    # single packet leaves the container
    # ==================================================
    if not host_allowed(url, allowed_hosts):
        logger.warning("Refusing to fetch off-allowlist URL %s", url)
        return None

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": ", ".join(content_types),
        "Accept-Language": "lt",
    }
    if extra_headers:
        headers.update(extra_headers)

    resp = None
    try:
        resp = get_session().get(url, params=params, timeout=timeout,
                                 headers=headers, stream=True)
        resp.raise_for_status()


        # STEP 2: a redirect can leave the allowlist — re-check
        # the URL the response actually came from
        # ====================================================
        if not host_allowed(resp.url, allowed_hosts):
            logger.warning("Refusing redirect off the allowlist: %s -> %s", url, resp.url)
            return None


        # STEP 3: the declared type must be one we can parse;
        # a server that declares nothing gets the benefit of
        # the doubt
        # ==================================================
        declared = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if declared and declared not in content_types:
            logger.warning("Unexpected Content-Type %s for %s", declared, resp.url)
            return None


        # STEP 4: read with a hard budget — the cap is on the
        # bytes kept, so a truncated page still parses
        # ==================================================
        chunks = []
        total = 0
        for chunk in resp.iter_content(chunk_size=16384):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total >= max_bytes:
                logger.warning("Body of %s exceeded %d bytes — truncated", resp.url, max_bytes)
                break

        return b"".join(chunks)[:max_bytes], resp.url

    except requests.RequestException as e:
        logger.warning("Failed to fetch %s: %s", url, e)
        return None
    finally:
        if resp is not None:
            resp.close()








############################################################
# run_deadline
############################################################
#
# The monotonic instant a run must be finished by. Monotonic
# and not wall clock on purpose — an NTP step mid-run must
# not extend or end a budget.
#
# Used by:
#   - knf_scraper.py, vu_scraper.py, schedule_scraper.py,
#     info_scraper.py — once at the top of every run
############################################################

def run_deadline(seconds: float) -> float:
    return time.monotonic() + seconds








############################################################
# deadline_passed
############################################################
#
# True once the budget from run_deadline is spent. Checked
# BETWEEN fetches, never during one: the point is to stop a
# slow-drip source from wedging a job that APScheduler will
# then skip on every later tick (max_instances=1).
#
# Used by:
#   - knf_scraper.py, vu_scraper.py, schedule_scraper.py,
#     info_scraper.py — between page/group fetches
############################################################

def deadline_passed(deadline: float) -> bool:
    return time.monotonic() >= deadline








############################################################
# utc_now_naive
############################################################
#
# Now as a NAIVE UTC datetime. datetime.utcnow() is
# deprecated on the 3.13 base image; this is the same value
# from an aware clock, and naive because that is the shape
# published_at and the scraper cursors already store.
#
# Used by:
#   - sanitise_published_at (below)
#   - knf_scraper.py, vu_scraper.py, schedule_scraper.py,
#     info_scraper.py — every timestamp they stamp
#     themselves
############################################################

def utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)








############################################################
# parse_source_datetime
############################################################
#
# A source timestamp ("2026-08-29T10:00:00+03:00",
# "2026-08-29T07:00:00Z", "2026-08-29") to a datetime, or
# None when it is none of those. fromisoformat carries the
# offset through — including a NEGATIVE one, which the old
# split("+") surgery in vu_scraper turned into a parse
# failure — and sanitise_published_at applies it.
#
# Used by:
#   - knf_scraper.py — _fetch_article, <time> and the
#     article:published_time meta
#   - vu_scraper.py — _fetch_vu_article, the same two
############################################################

def parse_source_datetime(value: str):
    if not value:
        return None

    text = value.strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        pass

    # A date-only or space-form value from an older template
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue

    return None








############################################################
# sanitise_published_at
############################################################
#
# The naive-UTC ISO string a news row stores: an AWARE
# datetime is converted to UTC (the offset applied, not
# dropped — Vilnius news used to land 2-3 h in the future),
# a naive one is taken as UTC already, and anything outside
# [now - 5 years, now] falls back to now. The clamp is what
# keeps a mis-parsed year from dividing the feed's recency
# term by ~0 and pinning the article to the top forever.
#
# An aware stamp at either END of the datetime range
# ("9999-12-31T23:59:59-05:00" in a <time datetime=…>) cannot
# be converted to UTC at all — it falls into the same
# "stamp now" answer instead of raising OverflowError out of
# the article parser and failing the whole run.
#
# Used by:
#   - knf_scraper.py — _fetch_article
#   - vu_scraper.py — _fetch_vu_article
############################################################

def sanitise_published_at(parsed) -> str:
    now = utc_now_naive()

    if parsed is None:
        return now.isoformat()

    if parsed.tzinfo is not None:
        # Applying the offset to a year-9999 or year-1 stamp walks
        # off the end of the datetime range. That date is exactly
        # what the clamp below exists to neutralise, so it answers
        # here rather than raising through the whole run
        try:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        except OverflowError:
            logger.warning("published_at %s is outside the datetime range — stamping now instead",
                           parsed.isoformat())
            return now.isoformat()

    if parsed > now or parsed < now - timedelta(days=MAX_ARTICLE_AGE_DAYS):
        logger.warning("published_at %s out of range — stamping now instead", parsed.isoformat())
        return now.isoformat()

    return parsed.isoformat()








############################################################
# mark_run_failed
############################################################
#
# Closes a scraper_runs row as 'failed' on a FRESH
# connection and swallows whatever that costs. The scrapers
# call it from their except handlers, where the connection
# they were using is exactly the thing that may have broken
# — the old code reused it, so a database error raised a
# second time and escaped the scraper into the admin route
# as a 500.
#
# Used by:
#   - knf_scraper.py, vu_scraper.py, schedule_scraper.py,
#     info_scraper.py — every failure path
############################################################

def mark_run_failed(run_id: str, message: str):
    try:
        db = get_db()
        try:
            db.execute(
                """UPDATE scraper_runs
                   SET status = 'failed', error_message = ?, finished_at = ?
                   WHERE id = ?""",
                (str(message)[:1000], utc_now_iso(), run_id),
            )
            db.commit()
        finally:
            db.close()
    except Exception:
        logger.exception("Failed to close scraper run %s as failed", run_id)








############################################################
# prune_scraper_runs
############################################################
#
# Deletes scraper_runs rows older than 30 days at the end of
# a run — the table grows by a row per scheduled scrape
# forever otherwise (migration v19 did the one-off pass and
# added the started_at index this DELETE rides on). The
# cutoff is built in Python so it compares against v17's
# ISO-T text. The caller owns the connection; the delete is
# committed here because it is the last thing a run does.
#
# The newest run of each source is KEPT whatever its age —
# retention must never be the reason a source that has not
# succeeded in months disappears from /api/scraper/status.
#
# Used by:
#   - knf_scraper.py, vu_scraper.py, schedule_scraper.py,
#     info_scraper.py — after the run row is closed
############################################################

def prune_scraper_runs(db):
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=RUN_RETENTION_DAYS)).isoformat()
        # The newest row of EVERY source survives the cutoff:
        # a scraper that stopped running months ago is exactly
        # the one whose last run /status has to keep showing.
        # SQLite's bare-column rule makes "id, MAX(started_at)"
        # return the id OF the newest row per group
        pruned = db.execute(
            """DELETE FROM scraper_runs
               WHERE started_at < ?
                 AND id NOT IN (
                     SELECT id FROM (
                         SELECT id, MAX(started_at) FROM scraper_runs GROUP BY source
                     )
                 )""",
            (cutoff,),
        ).rowcount
        db.commit()
        if pruned:
            logger.info("Pruned %d scraper_runs row(s) older than %d days", pruned, RUN_RETENTION_DAYS)
    except sqlite3.Error:
        # Retention is housekeeping — it must never turn a
        # completed run into a failed one
        logger.warning("Could not prune scraper_runs", exc_info=True)








############################################################
# load_deleted_urls
############################################################
#
# The tombstone set: source_urls an admin deleted from the
# news feed, in normalise_url shape, so a scraper skips them
# instead of re-inserting the article on its next tick. The
# table is owned by the news package; until it exists this
# returns an empty set and the scrapers behave exactly as
# before, which is why the missing-table case is a debug
# line and not a warning.
#
# Used by:
#   - knf_scraper.py — scrape_knf_news, once per run
#   - vu_scraper.py — scrape_vu_news, once per run
############################################################

def load_deleted_urls(db) -> set:
    try:
        rows = db.execute("SELECT source_url FROM deleted_source_urls").fetchall()
    except sqlite3.Error:
        logger.debug("No deleted_source_urls table yet — no tombstones to honour")
        return set()

    return {normalise_url(row[0]) for row in rows if row[0]}








############################################################
# validate_image_url
############################################################
#
# One scraped image src to the absolute URL a news row may
# store, or None. The source page decides the shape and the
# app shows the result to every guest, so it gets the same
# treatment users/routes.py gives a hand-typed avatar:
#
#   - refused when the src is EMPTY-SHAPED: blank after
#     strip(), or carrying neither host nor path ("#",
#     "?v=2", "//", "https://", "https:"). Every one of them
#     is a real lazy-load placeholder and every one of them
#     urljoins back to the article page itself
#   - resolved with urljoin against the page it came from,
#     which is what finally fixes both the protocol-relative
#     "//newshub.vu.lt/x.jpg" (glued onto BASE_URL into a
#     broken URL before) and the bare relative "images/x.jpg"
#     (stored verbatim before)
#   - http(s) only — no data:, javascript: or file:
#   - capped at MAX_IMAGE_URL_LENGTH
#   - host held to IMAGE_HOSTS, so an injected src cannot
#     turn every reader's device into a beacon for someone
#     else's server
#
# Used by:
#   - knf_scraper.py — _fetch_article, og:image and the
#     first content <img>
#   - vu_scraper.py — _fetch_vu_article, the same two
############################################################

def validate_image_url(page_url: str, src: str):
    if not src:
        return None

    # A blank-but-present src ("<img src=' '>") must die here:
    # urljoin resolves "" to the ARTICLE PAGE, which would store
    # an HTML document as image_url and stop the caller ever
    # reaching its next <img> candidate
    candidate = src.strip()
    if not candidate:
        return None


    # STEP 1: the OTHER empty shapes. "#", "?v=2", "//",
    # "https://" and "https:" carry neither a host nor a path,
    # so urljoin hands back the article page exactly as a blank
    # src does — and each of them is a shape a lazy-load
    # placeholder really uses
    # =========================================================
    try:
        parts = urlparse(candidate)
    except ValueError:
        logger.warning("Unparsable image src %.100s", candidate)
        return None

    if not parts.netloc and not parts.path:
        logger.warning("Dropping empty-shaped image src %.100s", candidate)
        return None


    # STEP 2: resolve against the article page — a relative or
    # protocol-relative src becomes absolute here, and only here
    # ==========================================================
    try:
        resolved = urljoin(page_url, candidate)
    except ValueError:
        logger.warning("Unparsable image src %.100s", candidate)
        return None

    if len(resolved) > MAX_IMAGE_URL_LENGTH:
        logger.warning("Image URL over %d chars — dropped", MAX_IMAGE_URL_LENGTH)
        return None


    # STEP 3: scheme and host — host_allowed covers both, and
    # the allowlist is the part a source page cannot talk us
    # out of
    # =======================================================
    if not host_allowed(resolved, IMAGE_HOSTS):
        logger.warning("Dropping off-allowlist image URL %.200s", resolved)
        return None

    return resolved








############################################################
# push_allowed
############################################################
#
# Whether a finished run may wake every device. Three ways
# to answer no, all of them about the SHAPE of the run
# rather than its content:
#
#   - the source has no earlier completed run: this is the
#     first boot / first backfill, and its "247 new
#     articles" is history, not news
#   - more than PUSH_BURST_THRESHOLD new rows: a re-import
#     or a source that republished its archive
#   - this source already pushed within the last hour
#
# The hourly cap is per process and monotonic (an NTP step
# must not open the gate). Failing OPEN on a database error
# is deliberate — a broken count must not silence the
# feature that works.
#
# Used by:
#   - knf_scraper.py, vu_scraper.py, schedule_scraper.py —
#     right before notify_channel
############################################################

def push_allowed(db, source: str, new_count: int, run_id: str) -> bool:
    # STEP 1: first successful run for this source = backfill
    # =======================================================
    try:
        earlier = db.execute(
            """SELECT COUNT(*) FROM scraper_runs
               WHERE source = ? AND status = 'completed' AND id != ?""",
            (source, run_id),
        ).fetchone()[0]
    except sqlite3.Error:
        logger.warning("Could not count earlier %s runs — allowing the push", source, exc_info=True)
        earlier = 1

    if not earlier:
        logger.info("Suppressing the %s push: first completed run (%d row(s) is a backfill)",
                    source, new_count)
        return False


    # STEP 2: a burst is an import, not an edition
    # ============================================
    if new_count > PUSH_BURST_THRESHOLD:
        logger.info("Suppressing the %s push: %d new row(s) is over the burst threshold of %d",
                    source, new_count, PUSH_BURST_THRESHOLD)
        return False


    # STEP 3: at most one push per source per hour
    # ============================================
    now = time.monotonic()
    with _LAST_PUSH_LOCK:
        last = _LAST_PUSH.get(source)
        if last is not None and now - last < PUSH_MIN_INTERVAL_SECONDS:
            logger.info("Suppressing the %s push: one was sent %.0f s ago", source, now - last)
            return False
        _LAST_PUSH[source] = now

    return True








############################################################
# check_yield_drop
############################################################
#
# One ERROR line when a run harvested an order of magnitude
# less than the last completed run of the same source. A
# selector the site quietly changed does not fail a run — it
# just starts returning three items where it returned forty
# — so this is the only signal between "working" and "the
# table stopped growing months ago". Purely diagnostic: the
# run's own status is decided by its caller.
#
# Used by:
#   - knf_scraper.py, vu_scraper.py, schedule_scraper.py —
#     once, just before the run row is closed
############################################################

def check_yield_drop(db, source: str, found: int, run_id: str):
    try:
        row = db.execute(
            """SELECT articles_found FROM scraper_runs
               WHERE source = ? AND status = 'completed' AND id != ?
               ORDER BY started_at DESC LIMIT 1""",
            (source, run_id),
        ).fetchone()
    except sqlite3.Error:
        logger.warning("Could not read the previous %s run", source, exc_info=True)
        return

    previous = (row[0] or 0) if row else 0

    # Under ten there is no order of magnitude to lose — a
    # source that always yields two items is not broken
    if previous >= 10 and found * 10 < previous:
        logger.error("%s yield collapsed: %d item(s) this run against %d last run — "
                     "the source markup has probably changed", source, found, previous)
