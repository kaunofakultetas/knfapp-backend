############################################################
#  [*] Scraper plumbing — HTTP, URL hygiene, run bookkeeping
#
#  The four scrapers (knf_scraper, vu_scraper,
#  schedule_scraper, info_scraper) talk to three hosts under
#  the same rules, so the shared half lives here:
#
#    - ONE pooled requests.Session with a retry/backoff
#      adapter, reused by every scraper
#    - fetch() — the host allowlist is checked BEFORE the
#      request and again AFTER redirects (an injected
#      absolute href must never become an SSRF probe), with
#      (connect, read) tuple timeouts, a Content-Type check
#      and a hard byte cap on the body
#    - run_deadline / deadline_passed — the wall-clock budget
#      a run tests between fetches
#    - normalise_url — the dedup key for
#      news_posts.source_url: post-redirect, no fragment, no
#      tracking params, canonical scheme/host, no vu.lt
#      language segment, no trailing slash
#    - utc_now_naive / parse_source_datetime /
#      sanitise_published_at — source datetimes with the
#      offset APPLIED instead of dropped, a [now - 5 years,
#      now] clamp, and an AWARE (+00:00) stored shape so
#      published_at carries one shape for every source
#    - open_run / mark_run_failed / prune_scraper_runs /
#      load_deleted_urls — the scraper_runs and tombstone
#      bookkeeping all four scrapers repeat
#    - validate_image_url — a scraped <img>/og:image src
#      resolved against its page, capped and held to the
#      image host allowlist
#    - push_allowed / check_yield_drop — the two run-shape
#      guards: no push for a backfill or a burst (and at
#      most one per source per hour), and one ERROR line
#      when a run's yield collapses
#
#  Runs live in SEVERAL processes (gunicorn workers take
#  the admin triggers, the cron container execs a fresh
#  python per tick), so the per-scraper threading.Lock
#  alone cannot guard an overlap. open_run is the
#  cross-process half: ONE conditional INSERT that registers
#  the run only while a live 'running' row is absent — a row
#  older than the scraper's own wall-clock budget is not a
#  live run (every run self-limits to that budget), so a
#  SIGKILLed run blocks its source for minutes, never until
#  the daily reconcile. The in-process lock is the cheap
#  fast path.
#
#  The scrapers run in AUTOCOMMIT (management commands, and
#  the trigger routes are @transaction.non_atomic_requests):
#  the run row must be visible to /status while fetches take
#  their minutes, and the write batches wrap themselves in
#  transaction.atomic() at their own commit boundaries. The
#  run bookkeeping is plain ORM except open_run, whose
#  conditional INSERT must test and claim in ONE statement —
#  that one rides django.db.connection. mark_run_failed
#  opens with close_old_connections() — it runs exactly when
#  the connection in hand may be the thing that broke, and
#  that call is Django's way of handing the next query a
#  fresh one.
############################################################


import logging
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from bs4 import NavigableString

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from django.db import Error as DatabaseError, close_old_connections, connection
from django.db.models import OuterRef, Subquery

from knfapp.common.timestamps import utc_now

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

# The same limits the news views enforce on hand-written
# posts; scraped rows must not bypass them
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
# the end of every run
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
#   - knf_scraper.py / vu_scraper.py — every listing link
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
# dropped, vu.lt's language segment (/lt/, /en/) dropped,
# and no trailing slash on a non-root path. Two links to
# the same article — one from a listing, one after a
# redirect, one with a campaign tag — collapse to one key.
# Anything unparsable is handed back stripped, never
# dropped.
#
# The language rule is what makes the vu.lt scraper's
# listing key equal its stored key: the listing cards link
# "/lt/visos-naujienos/<slug>", the article itself lands on
# "/visos-naujienos/<slug>" after the site's redirect, and
# the stored row and any tombstone hold that landing form.
# Keyed on the raw listing href, the already-stored and
# tombstone tests at the listing stage matched nothing —
# every tick re-fetched twenty stored articles, and an
# article an admin had deleted came back on the next one.
# vu.lt only: the faculty site's paths are left alone.
#
# Used by:
#   - knf_scraper.py, vu_scraper.py — the dedup key and the
#     stored source_url
#   - load_deleted_urls (below) — tombstones are matched in
#     the same shape
############################################################

# The language segment vu.lt serves every page under and
# redirects away from, as the first path segment only
_VU_LANGUAGE_PREFIX_RE = re.compile(r"^/(?:lt|en)(?=/|$)", re.IGNORECASE)


def _strip_vu_language_prefix(host: str, path: str) -> str:
    if host not in VU_HOSTS:
        return path
    return _VU_LANGUAGE_PREFIX_RE.sub("", path, count=1) or "/"


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


    # STEP 2: path without vu.lt's language segment and without
    # its trailing slash, query without the campaign tags,
    # fragment gone entirely
    # =========================================================
    path = _strip_vu_language_prefix(host, parsed.path or "/")
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
# Four guards:
#   - the host allowlist, tested on the requested URL and
#     again on resp.url after redirects
#   - (connect, read) tuple timeouts
#   - a Content-Type check, so an unexpected PDF or image is
#     not parsed as a page
#   - a byte cap read chunk by chunk, so an endless body is
#     cut instead of swallowing the run — or, for a caller
#     that cannot use half a document (a JSON feed passes
#     allow_truncated=False), refused whole: None, exactly
#     like any other failed fetch, never a cut body that
#     looks complete
# The body comes back as BYTES on purpose: BeautifulSoup
# sniffs the document's own charset, which is more reliable
# than the ISO-8859-1 requests falls back to when the header
# carries no charset.
#
# Used by:
#   - knf_scraper.py / vu_scraper.py — listings and articles
#   - info_scraper.py — _fetch_page
#   - schedule_scraper.py — the group list and event feeds
############################################################

def fetch(url: str, allowed_hosts, params=None, timeout=DEFAULT_TIMEOUT,
          content_types=HTML_CONTENT_TYPES, max_bytes=MAX_RESPONSE_BYTES,
          extra_headers=None, allow_truncated=True):
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
        # bytes kept, so a truncated page still parses; a caller
        # that cannot parse half a body gets None instead
        # ======================================================
        chunks = []
        total = 0
        for chunk in resp.iter_content(chunk_size=16384):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            # PAST the cap, not at it — a body exactly max_bytes
            # long is complete and must not be reported as cut
            if total > max_bytes:
                if not allow_truncated:
                    logger.warning("Body of %s exceeded %d bytes — refused as incomplete",
                                   resp.url, max_bytes)
                    return None
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
# run_deadline / deadline_passed
############################################################
#
# The monotonic instant a run must be finished by, and the
# test for it — monotonic and not wall clock on purpose (an
# NTP step mid-run must not extend or end a budget), checked
# BETWEEN fetches, never during one: the point is to stop a
# slow-drip source from wedging the job past its tick.
#
# Used by:
#   - all four scrapers — once at the top, then between
#     page/group fetches
############################################################

def run_deadline(seconds: float) -> float:
    return time.monotonic() + seconds


def deadline_passed(deadline: float) -> bool:
    return time.monotonic() >= deadline








############################################################
# utc_now_naive
############################################################
#
# Now as a NAIVE UTC datetime — the shape published_at and
# the scraper cursors store.
#
# Used by:
#   - sanitise_published_at (below)
#   - the scrapers — every timestamp they stamp themselves
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
# offset through — including a NEGATIVE one — and
# sanitise_published_at applies it.
#
# Used by:
#   - knf_scraper.py — <time> and article:published_time
#   - vu_scraper.py — the same two
############################################################

def parse_source_datetime(value: str):
    if not value:
        return None

    text = value.strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        pass

    # Fallbacks for date-only and space-separated stamps
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
# The AWARE UTC datetime a news row stores — the
# same shape member and faculty posts write, so the whole
# published_at column carries ONE stamp shape and its
# string order IS its time order (the feed's keyset anchor
# and ORDER BY tiebreak compare it raw). An aware source
# datetime is converted to UTC (the offset applied, not
# dropped — Vilnius news would land 2-3 h in the future
# otherwise), a naive one is taken as UTC already, and
# anything outside [now - 5 years, now] falls back to now.
# The clamp is what keeps a mis-parsed year from dividing
# the feed's recency term by ~0 and pinning the article to
# the top forever.
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

def sanitise_published_at(parsed) -> datetime:
    # The comparisons run naive; only the RETURNS attach UTC,
    # so every stored stamp goes out aware
    now = utc_now_naive()

    if parsed is None:
        return now.replace(tzinfo=timezone.utc)

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
            return now.replace(tzinfo=timezone.utc)

    if parsed > now or parsed < now - timedelta(days=MAX_ARTICLE_AGE_DAYS):
        logger.warning("published_at %s out of range — stamping now instead", parsed.isoformat())
        return now.replace(tzinfo=timezone.utc)

    return parsed.replace(tzinfo=timezone.utc)








############################################################
# open_run
############################################################
#
# Registers a run as 'running' and answers its id — or None
# when a LIVE run of the same source already holds the
# source, which the caller reports as {"skipped": True}.
# This is the cross-process half of the run lock (see the
# module banner): one conditional INSERT, atomic under
# SQLite's write serialisation, that only lands while no
# 'running' row of this source is younger than the source's
# own wall-clock budget. A row past its budget is a corpse —
# the process died mid-scrape — and is overlapped rather
# than obeyed; the daily reconcile closes it for the status
# page. Committed immediately (autocommit), so /status shows
# the run while the fetches take their minutes.
#
# Used by:
#   - all four scrapers — right after taking their
#     in-process lock
############################################################

def open_run(source: str, budget_seconds: int):
    run_id = str(uuid.uuid4())
    stale_cutoff = datetime.now(timezone.utc) - timedelta(seconds=budget_seconds)

    # The counts are named explicitly — the Django-built table
    # carries no DDL defaults for its NOT NULL columns
    with connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO scraper_runs (id, source, status, articles_found, articles_new, started_at)
               SELECT %s, %s, 'running', 0, 0, %s
               WHERE NOT EXISTS (
                   SELECT 1 FROM scraper_runs
                   WHERE source = %s AND status = 'running' AND started_at > %s
               )""",
            (run_id, source, utc_now(), source, stale_cutoff),
        )
        if cursor.rowcount == 0:
            logger.info("%s scrape already running in another process — this trigger is skipped", source)
            return None

    return run_id








############################################################
# close_run
############################################################
#
# Closes one run as 'completed' with its counts (the columns
# keep the news scraper's names whatever the source counts)
# and an optional error note — the info scraper completes
# WITH the sections that failed named there.
#
# Used by:
#   - all four scrapers — the success path
############################################################

def close_run(run_id: str, found: int, new: int, error_message=None):
    from knfapp.scraper.models import ScraperRun

    ScraperRun.objects.filter(id=run_id).update(
        status="completed", articles_found=found, articles_new=new,
        error_message=error_message, finished_at=utc_now(),
    )








############################################################
# mark_run_failed
############################################################
#
# Closes a scraper_runs row as 'failed' and swallows
# whatever that costs. The scrapers call it from their
# except handlers, where the connection they were using is
# exactly the thing that may have broken —
# close_old_connections() is what hands the UPDATE a fresh
# one.
#
# Used by:
#   - all four scrapers — every failure path
############################################################

def mark_run_failed(run_id: str, message: str):
    from knfapp.scraper.models import ScraperRun

    try:
        close_old_connections()
        ScraperRun.objects.filter(id=run_id).update(
            status="failed", error_message=str(message)[:1000], finished_at=utc_now(),
        )
    except Exception:
        logger.exception("Failed to close scraper run %s as failed", run_id)








############################################################
# reconcile_interrupted_runs
############################################################
#
# Closes every scraper_runs row still marked 'running' and
# older than `older_than_seconds` as 'failed' with
# error_message 'interrupted'. A row only reaches this state
# one way: the process died mid-scrape (SIGKILL, OOM, a
# replaced container) — every in-process failure path closes
# its own row. open_run already refuses to OBEY such a
# corpse past its budget; this pass is what stops /status
# showing it as "running" for months. The threshold is
# longer than any scraper's wall-clock budget, so a scrape
# genuinely in flight is never mistaken for an orphan.
#
# Used by:
#   - management/commands/maintenance.py — the daily tick
############################################################

STALE_RUN_SECONDS = 6 * 3600


def reconcile_interrupted_runs(older_than_seconds: int = STALE_RUN_SECONDS) -> int:
    from knfapp.scraper.models import ScraperRun

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)
    closed = ScraperRun.objects.filter(status="running", started_at__lt=cutoff).update(
        status="failed", error_message="interrupted", finished_at=utc_now(),
    )

    if closed:
        logger.warning("Closed %d scraper run(s) left 'running' by a killed process", closed)
    return closed








############################################################
# prune_scraper_runs
############################################################
#
# Deletes scraper_runs rows older than 30 days at the end of
# a run — the table grows by a row per scheduled scrape
# forever otherwise.
#
# The newest run of each source is KEPT whatever its age —
# retention must never be the reason a source that has not
# succeeded in months disappears from /api/scraper/status.
# The keep-set rides a correlated subquery (the newest row
# id per source, id as the same-stamp tiebreak) — plain ORM,
# same plan on either engine.
#
# Used by:
#   - all four scrapers — after the run row is closed
############################################################

def prune_scraper_runs():
    from knfapp.scraper.models import ScraperRun

    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=RUN_RETENTION_DAYS)
        newest_of_source = (
            ScraperRun.objects.filter(source=OuterRef("source"))
            .order_by("-started_at", "-id").values("id")[:1]
        )
        pruned, _ = (
            ScraperRun.objects.filter(started_at__lt=cutoff)
            .exclude(id=Subquery(newest_of_source))
            .delete()
        )
        if pruned:
            logger.info("Pruned %d scraper_runs row(s) older than %d days", pruned, RUN_RETENTION_DAYS)
    except DatabaseError:
        # Retention is housekeeping — it must never turn a
        # completed run into a failed one
        logger.warning("Could not prune scraper_runs", exc_info=True)








############################################################
# load_deleted_urls
############################################################
#
# The tombstone set: source_urls an admin deleted from the
# news feed, in normalise_url shape, so a scraper skips them
# instead of re-inserting the article on its next tick.
# Migrations guarantee the table, so no guard is needed.
#
# Used by:
#   - knf_scraper.py, vu_scraper.py — once per run
############################################################

def load_deleted_urls() -> set:
    from knfapp.news.models import DeletedSourceUrl

    return {
        normalise_url(url)
        for url in DeletedSourceUrl.objects.values_list("source_url", flat=True)
        if url
    }








############################################################
# validate_image_url
############################################################
#
# One scraped image src to the absolute URL a news row may
# store, or None. The source page decides the shape and the
# app shows the result to every guest, so:
#
#   - refused when the src is EMPTY-SHAPED: blank after
#     strip(), or carrying neither host nor path ("#",
#     "?v=2", "//", "https://", "https:"). Every one of them
#     is a real lazy-load placeholder and every one of them
#     urljoins back to the article page itself
#   - resolved with urljoin against the page it came from,
#     so protocol-relative and bare relative srcs become
#     absolute here, and only here
#   - http(s) only — no data:, javascript: or file:
#   - capped at MAX_IMAGE_URL_LENGTH
#   - host held to IMAGE_HOSTS, so an injected src cannot
#     turn every reader's device into a beacon for someone
#     else's server
#
# Used by:
#   - knf_scraper.py / vu_scraper.py — og:image and the
#     first content <img>
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
# _tidy_inline
############################################################
#
# One inline run made presentable: whitespace runs collapsed
# to single spaces (the walk joins inline elements with
# spaces, never newlines — that is the whole point), then no
# space left before closing punctuation or after an opening
# bracket, so "programas ." from an inline <a> boundary
# reads "programas.".
#
# Used by:
#   - _inline_markdown, _emit_blocks (below)
############################################################

def _tidy_inline(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.;:!?)\]])", r"\1", text)
    text = re.sub(r"([(\[])\s+", r"\1", text)
    return text







############################################################
# _inline_markdown
############################################################
#
# One node rendered as inline markdown text: plain strings
# pass through, <strong>/<b> become **bold**, <em>/<i>
# *italic*, and <a href> becomes [text](absolute url) — the
# href resolved against the article page, kept only when it
# lands on http(s) and under the URL length cap; a refused
# link keeps its text and loses the link. <br> is a plain
# space here — a break INSIDE a paragraph is not a
# paragraph. Square brackets are dropped from link text so a
# page cannot fake nested markdown.
#
# Used by:
#   - _emit_blocks (below) — per block and for loose inline
#     runs
############################################################

def _inline_markdown(node, base_url: str) -> str:
    if isinstance(node, NavigableString):
        return str(node)

    name = getattr(node, "name", None)
    if name in ("script", "style"):
        return ""

    inner = "".join(_inline_markdown(child, base_url) for child in node.children)

    if name in ("strong", "b"):
        inner = _tidy_inline(inner).strip()
        return f" **{inner}** " if inner else ""

    if name in ("em", "i"):
        inner = _tidy_inline(inner).strip()
        return f" *{inner}* " if inner else ""

    if name == "a":
        # Link text is PLAIN — nested bold/italic inside an <a>
        # must not leave ** markers inside the [text](url) form
        inner = _tidy_inline(node.get_text(" ", strip=True)).strip().replace("[", "").replace("]", "")
        target = urljoin(base_url, (node.get("href") or "").strip())
        if inner and target.startswith(("http://", "https://")) and len(target) <= MAX_IMAGE_URL_LENGTH:
            return f" [{inner}]({target}) "
        return f" {inner} " if inner else ""

    if name == "br":
        return " "

    return inner







############################################################
# _emit_blocks
############################################################
#
# The block walk behind element_to_markdown: children are
# consumed in document order, block tags each become one
# markdown block, and everything between them (text nodes,
# inline tags) accumulates into a pending run that flushes
# as a paragraph at the next block boundary — old Joomla
# bodies write text straight into the container with <br>
# separators, and this is what keeps those readable. A bare
# <br> at this level IS a paragraph break (unlike inside a
# paragraph); chrome tags are skipped even when the caller
# already decompose()d — the fallback selectors are broad.
#
# Used by:
#   - element_to_markdown (below) — and itself, recursing
#     into container tags
############################################################

_SKIP_TAGS = frozenset({"script", "style", "nav", "header", "footer", "aside", "iframe", "form", "button"})
_CONTAINER_TAGS = frozenset({"div", "section", "article", "main", "figure", "figcaption", "table", "tbody", "thead", "tr"})

def _emit_blocks(el, base_url: str, blocks: list) -> None:
    pending = []

    def flush():
        text = _tidy_inline("".join(pending)).strip()
        pending.clear()
        if text:
            blocks.append(text)

    for child in el.children:
        name = getattr(child, "name", None)

        if name in _SKIP_TAGS:
            continue

        if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            flush()
            text = _tidy_inline(_inline_markdown(child, base_url)).strip()
            if text:
                blocks.append(("## " if name in ("h1", "h2") else "### ") + text)

        elif name in ("p", "blockquote"):
            flush()
            text = _tidy_inline(_inline_markdown(child, base_url)).strip()
            if text:
                blocks.append(text)

        elif name in ("ul", "ol"):
            flush()
            items = []
            for li in child.find_all("li", recursive=False):
                text = _tidy_inline(_inline_markdown(li, base_url)).strip()
                if text:
                    items.append(f"- {text}")
            if items:
                blocks.append("\n".join(items))

        elif name == "br":
            flush()

        elif name in _CONTAINER_TAGS:
            flush()
            _emit_blocks(child, base_url, blocks)

        else:
            pending.append(_inline_markdown(child, base_url))

    flush()







############################################################
# element_to_markdown
############################################################
#
#   element_to_markdown(el, base_url) — article body as a
#                                       light markdown string
#
# The block-aware replacement for get_text("\n"): that call
# put a newline at EVERY element boundary, so an inline link
# chopped its sentence mid-thought and a real paragraph
# break was indistinguishable from an accidental one. This
# walks the block structure instead and emits the small
# markdown subset the mobile app renders (NewsBody):
# paragraphs separated by ONE blank line, "## " / "### "
# headings, "- " list lines, **bold**, *italic* and
# [text](url) links. The caller decides which element to
# hand over and has usually decompose()d its chrome first.
#
# Used by:
#   - knf_scraper._fetch_article — both body paths
#   - vu_scraper._fetch_article — the body ladder
############################################################

def element_to_markdown(el, base_url: str) -> str:
    blocks = []
    _emit_blocks(el, base_url, blocks)
    return "\n\n".join(blocks)







############################################################
# markdown_to_plain
############################################################
#
#   markdown_to_plain(text) — the same body with every
#                             markdown marker removed
#
# Links keep their text and lose the URL, bold/italic keep
# their text, heading and list markers drop. This is what
# summaries are cut from — a summary is prose, and 200
# chars of prose with a raw "[Vilnius](https://…)" in the
# middle is not.
#
# Used by:
#   - knf_scraper._fetch_article — the summary cut
#   - vu_scraper._fetch_article — the _article_summary input
############################################################

def markdown_to_plain(text: str) -> str:
    text = re.sub(r"\[([^\]]*)\]\([^)\s]*\)", r"\1", text)
    text = re.sub(r"\*{1,2}([^*\n]+)\*{1,2}", r"\1", text)
    text = re.sub(r"^#{2,3} ", "", text, flags=re.MULTILINE)
    text = re.sub(r"^- ", "", text, flags=re.MULTILINE)
    return text







############################################################
# cap_markdown
############################################################
#
#   cap_markdown(text, limit) — the body under the storage
#                               cap without a torn tail
#
# A plain [:limit] slice can cut through the middle of a
# link and leave "[Vilniaus universi" as the article's last
# words. Over-limit bodies are cut back to the last
# paragraph boundary inside the limit instead; a single
# paragraph longer than the whole limit (no boundary to cut
# at) falls back to the last word boundary.
#
# Used by:
#   - knf_scraper._fetch_article, vu_scraper._fetch_article
#     — the stored content field
############################################################

def cap_markdown(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text

    cut = text[:limit]
    if "\n\n" in cut:
        return cut.rsplit("\n\n", 1)[0]
    return cut.rsplit(" ", 1)[0]








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
# must not open the gate) — under cron every tick is a
# fresh process, so the cap's real work is against back-to-
# back manual triggers in one worker.
# Failing OPEN on a database error is deliberate — a broken
# probe must not silence the feature that works.
#
# Used by:
#   - knf_scraper.py, vu_scraper.py, schedule_scraper.py —
#     right before notify_channel
############################################################

def push_allowed(source: str, new_count: int, run_id: str) -> bool:
    from knfapp.scraper.models import ScraperRun


    # STEP 1: first successful run for this source = backfill
    # =======================================================
    try:
        earlier = (
            ScraperRun.objects.filter(source=source, status="completed")
            .exclude(id=run_id)
            .exists()
        )
    except DatabaseError:
        logger.warning("Could not check for earlier %s runs — allowing the push", source, exc_info=True)
        earlier = True

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

def check_yield_drop(source: str, found: int, run_id: str):
    from knfapp.scraper.models import ScraperRun

    try:
        previous = (
            ScraperRun.objects.filter(source=source, status="completed")
            .exclude(id=run_id)
            .order_by("-started_at")
            .values_list("articles_found", flat=True)
            .first()
        )
    except DatabaseError:
        logger.warning("Could not read the previous %s run", source, exc_info=True)
        return

    previous = previous or 0

    # Under ten there is no order of magnitude to lose — a
    # source that always yields two items is not broken
    if previous >= 10 and found * 10 < previous:
        logger.error("%s yield collapsed: %d item(s) this run against %d last run — "
                     "the source markup has probably changed", source, found, previous)
