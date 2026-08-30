# -----------------------------------------------------------
#  [*] Tests — the scraper runtime
#
#  What this module proves about scraper/scheduler.py,
#  scraper/routes.py and scraper/plurals.py:
#
#    - building the app NEVER starts a scraper. The scheduler
#      belongs to main.py's --http branch, and a CLI run, a
#      test import or a `flask shell` that quietly began
#      scraping live sites is the regression the guard exists
#      for
#    - SCRAPER_ENABLED and SCRAPER_STARTUP_RUNS are honoured
#      in every spelling ("0", "off", "No", ""), the start is
#      idempotent per process, and the stop is safe to call
#      twice, with nothing running, and when the shutdown
#      itself raises
#    - the seven interval jobs are registered with the
#      intervals, max_instances=1 and the five-minute misfire
#      grace their banner claims, and a missed tick is logged
#      instead of dropped in silence
#    - the startup one-shots are armed staggered, jittered and
#      daemonised — and SKIPPED for a source that completed a
#      run inside its own interval, which is what stops a
#      crash loop hammering knf.vu.lt
#    - each job body does its real work (both news scrapers,
#      the timetable and info scrapers, the Expo receipt poll,
#      the expired-session sweep, the orphaned push-token
#      prune, the daily run reconciliation) and swallows a
#      failure into the log instead of killing the scheduler
#    - the scraper_runs bookkeeping: a killed process's
#      'running' rows are closed as 'interrupted' at the next
#      start, a run in flight is not, and rows older than 30
#      days are pruned at the end of a run — except each
#      source's newest, which survives whatever its age
#    - /api/scraper/* is admin-only: a guest, a student, a
#      teacher and a curator are all refused, and the scrapers
#      are never reached
#    - /status filters, orders, caps at 20 and summarises each
#      source's latest / lastSuccess / lastFailure
#    - the trigger routes' status mapping (200 / 409 / 502),
#      with the raw exception text replaced by a stable slug
#    - plurals.py picks the Lithuanian cardinal form the push
#      copy declines with (1/21 one, 2-9/22-29 few,
#      10-20/111 other)
#
#  No test here reaches the network: every fetch is served by
#  `responses` from fixture HTML authored below, small but
#  shaped the way the parsers expect. Nothing sleeps —
#  time_machine freezes the clock for the retention window.
# -----------------------------------------------------------


import logging
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import responses
import time_machine

from app.notifications import push as push_module
from app.scraper import info_scraper, knf_scraper, scheduler, schedule_scraper, vu_scraper
from app.scraper import routes as scraper_routes
from app.scraper.plurals import lt_plural


STATUS = "/api/scraper/status"
TRIGGER = "/api/scraper/trigger"
RUN = "/api/scraper/run"
SCHEDULE = "/api/scraper/schedule"
INFO = "/api/scraper/info"

# Every admin-only path this blueprint publishes, with the
# verb it answers — the role gate is asserted across all five
ENDPOINTS = (
    ("get", STATUS),
    ("post", TRIGGER),
    ("post", RUN),
    ("post", SCHEDULE),
    ("post", INFO),
)

# The wire shape of one scraper_runs row (_run_row)
RUN_KEYS = {"id", "source", "status", "articlesFound", "articlesNew",
            "itemsFound", "itemsNew", "error", "startedAt", "finishedAt"}

# The seven interval jobs and the interval each one claims
JOB_INTERVALS = {
    "news_scraper": timedelta(minutes=20),
    "schedule_scraper": timedelta(hours=6),
    "info_scraper": timedelta(hours=24),
    "push_receipts": timedelta(minutes=15),
    "session_sweep": timedelta(hours=24),
    "push_token_prune": timedelta(hours=24),
    "run_reconcile": timedelta(hours=24),
}

# Noon on the container clock's own day. A fixed instant makes
# the 30-day retention boundary exact instead of "about a
# month", and staying on today's date keeps the bearer token
# the admin fixture minted (30 days out) valid inside the
# freeze
FROZEN = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)

# The stamp the fixture article pages carry: a Vilnius wall
# clock WITH its +03:00 offset, sixty days back so it sits
# inside the five-year clamp and behind FROZEN whatever the
# container's date. _STORED is the naive UTC the row must end
# up with — the offset applied, not dropped
_PUBLISHED_UTC = (datetime.now(timezone.utc) - timedelta(days=60)).replace(microsecond=0)
PUBLISHED_SOURCE = _PUBLISHED_UTC.astimezone(timezone(timedelta(hours=3))).isoformat()
PUBLISHED_STORED = _PUBLISHED_UTC.replace(tzinfo=None).isoformat()

KNF_LISTING_URL = "https://knf.vu.lt/aktualijos"
KNF_ARTICLES = (
    ("nauja-studiju-programa", "Nauja studijų programa nuo rudens"),
    ("bibliotekos-darbo-laikas", "Bibliotekos darbo laikas per sesiją"),
)

# The listing is fetched with "www.", every article link
# canonicalises to the bare host — normalise_url strips it
VU_LISTING_URL = "https://www.vu.lt/naujienos"
VU_ARTICLE_URL = "https://vu.lt/naujienos/vu-mokslo-festivalis-grizta"
VU_ARTICLE_TITLE = "VU mokslo festivalis grįžta į Kauną"




# -----------------------------------------------------------
# Fixture HTML
# -----------------------------------------------------------
#
# Deliberately small, but every element the parsers actually
# key off is present: the knf listing carries the current
# h2.article-title generation plus one off-topic link the
# "/aktualijos/" filter has to drop, and the vu listing
# carries the article anchor twice (once as the image-only
# anchor whose text is too short to use) plus the two
# navigation hrefs _is_article_href must reject.
# -----------------------------------------------------------

def _knf_listing_html():
    items = "\n".join(
        f'      <div class="item"><h2 class="article-title">'
        f'<a href="/aktualijos/{slug}">{title}</a></h2></div>'
        for slug, title in KNF_ARTICLES
    )
    return f"""<!doctype html>
<html lang="lt"><head><meta charset="utf-8"><title>Aktualijos</title></head>
<body>
  <div class="blog">
{items}
      <div class="item"><h2 class="article-title"><a href="/kontaktai">Kontaktai</a></h2></div>
  </div>
</body></html>"""


def _knf_article_html(slug, title, published=PUBLISHED_SOURCE):
    return f"""<!doctype html>
<html lang="lt"><head><meta charset="utf-8">
<meta property="og:title" content="VU Kauno fakultetas - {title}">
<meta property="og:image" content="https://knf.vu.lt/images/{slug}.jpg">
</head><body>
<div class="item-page">
  <div class="article-content">
    <time datetime="{published}">{published[:10]}</time>
    <p>Fakulteto bendruomenė kviečiama susipažinti su naujienomis ir dalyvauti
    renginiuose visą rudens semestrą.</p>
  </div>
  <span class="article-author">Fakulteto administracija</span>
</div>
</body></html>"""


def _vu_listing_html():
    return f"""<!doctype html>
<html lang="lt"><head><meta charset="utf-8"><title>Naujienos</title></head>
<body>
  <main>
    <a href="/naujienos/vu-mokslo-festivalis-grizta">{VU_ARTICLE_TITLE}</a>
    <a href="/naujienos/vu-mokslo-festivalis-grizta"><img src="/img/festivalis.jpg" alt=""></a>
    <a href="/naujienos">Visos naujienos</a>
    <a href="/naujienos/?page=2">2</a>
  </main>
</body></html>"""


def _vu_article_html(published=PUBLISHED_SOURCE):
    return f"""<!doctype html>
<html lang="lt"><head><meta charset="utf-8">
<meta property="og:description" content="Mokslo festivalis kviečia kauniečius į universiteto laboratorijas ir atviras paskaitas.">
<meta property="og:image" content="https://www.vu.lt/site_files/festivalis.jpg">
<meta property="article:published_time" content="{published}">
</head><body>
<article>
  <h1>{VU_ARTICLE_TITLE}</h1>
  <p>Renginių savaitė universitete prasideda rudenį ir tęsiasi visą mėnesį.</p>
</article>
</body></html>"""




# -----------------------------------------------------------
# _iso
# -----------------------------------------------------------
#
# An aware-UTC ISO stamp offset from `base` (now by default)
# in exactly the shape utc_now_iso() writes, so the string
# comparisons scraper_runs relies on stay chronological.
# -----------------------------------------------------------

def _iso(base=None, **delta):
    return ((base or datetime.now(timezone.utc)) + timedelta(**delta)).isoformat()




# -----------------------------------------------------------
# _clean_rate_limit_store
# -----------------------------------------------------------
#
# The per-IP budget in auth/routes.py is PROCESS state, not
# database state — every test in the suite shares it and the
# whole file logs in from 127.0.0.1. Clearing it per test
# keeps a 429 out of an assertion about a 403.
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_rate_limit_store():
    from app.auth.routes import _rate_limit_store

    _rate_limit_store.clear()
    yield
    _rate_limit_store.clear()




# -----------------------------------------------------------
# _no_scheduler_survives_a_test
# -----------------------------------------------------------
#
# scheduler._scheduler and scheduler._startup_timers are
# module-level PROCESS state: one test that left a scheduler
# up would silently turn every later start into the "already
# running" no-op. Stopped on the way in as well as on the way
# out, so a crashed test cannot poison the rest of the file.
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_scheduler_survives_a_test():
    scheduler.stop_scraper_scheduler()
    yield
    scheduler.stop_scraper_scheduler()




# -----------------------------------------------------------
# armed_timers
# -----------------------------------------------------------
#
# Replaces threading.Timer for the duration of one test and
# collects what start_scraper_scheduler armed. The real thing
# would fire a live scrape a couple of seconds later, which
# is precisely what must never happen in a test container —
# and the recorder is also how the delay, the daemon flag and
# the cancel-on-stop become assertable.
# -----------------------------------------------------------

@pytest.fixture
def armed_timers(monkeypatch):
    armed = []

    class _RecordingTimer:
        def __init__(self, interval, function):
            self.interval = interval
            self.function = function
            self.daemon = False
            self.started = False
            self.cancelled = False
            armed.append(self)

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

    monkeypatch.setattr(threading, "Timer", _RecordingTimer)
    return armed




# -----------------------------------------------------------
# running_scheduler
# -----------------------------------------------------------
#
# A started scheduler with the startup one-shots off, which is
# the only configuration a test may start: the interval jobs'
# first tick is fifteen minutes away, so nothing fires while
# the test runs and the job bodies can be invoked deliberately
# instead.
# -----------------------------------------------------------

@pytest.fixture
def running_scheduler(app, monkeypatch):
    monkeypatch.setenv("SCRAPER_ENABLED", "1")
    monkeypatch.setenv("SCRAPER_STARTUP_RUNS", "0")

    assert scheduler.start_scraper_scheduler(app) is True
    yield scheduler
    scheduler.stop_scraper_scheduler()




# -----------------------------------------------------------
# job
# -----------------------------------------------------------
#
# One registered job's closure, so a test can run the body the
# timer would have run. Poking the scheduler's registry is the
# only way in: the closures are defined inside
# start_scraper_scheduler and reachable nowhere else.
# -----------------------------------------------------------

@pytest.fixture
def job(running_scheduler):

    def _job(job_id):
        found = running_scheduler._scheduler.get_job(job_id)
        assert found is not None, f"no job registered as {job_id}"
        return found

    return _job




# -----------------------------------------------------------
# seed_run
# -----------------------------------------------------------
#
#   seed_run(source="vu.lt", status="failed", minutes=-90)
#
# Inserts one scraper_runs row directly. Direct SQL on
# purpose: /status has to render runs no route can create —
# a run left 'running' by a dead process, a month-old row, a
# source that has not succeeded since spring.
# -----------------------------------------------------------

@pytest.fixture
def seed_run(app):

    def _seed(source="knf.vu.lt", status="completed", started_at=None, found=0, new=0,
              error=None, finished_at=None, run_id=None, base=None, **delta):
        run_id = run_id or str(uuid.uuid4())
        started_at = started_at or _iso(base=base, **delta)

        conn = sqlite3.connect(app.config["DB_PATH"])
        try:
            conn.execute(
                """INSERT INTO scraper_runs
                   (id, source, status, articles_found, articles_new, error_message,
                    started_at, finished_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, source, status, found, new, error, started_at, finished_at),
            )
            conn.commit()
        finally:
            conn.close()

        return run_id

    return _seed




# -----------------------------------------------------------
# seed_session / seed_push_token
# -----------------------------------------------------------
#
# Rows the housekeeping jobs exist to delete. Sessions carry
# an opaque token hash in production; these tests only care
# about expires_at, which is what both sweeps key off.
# -----------------------------------------------------------

@pytest.fixture
def seed_session(app):

    def _seed(user_id, **delta):
        conn = sqlite3.connect(app.config["DB_PATH"])
        try:
            conn.execute(
                "INSERT INTO sessions (id, user_id, token, expires_at) VALUES (?, ?, ?, ?)",
                (str(uuid.uuid4()), user_id, uuid.uuid4().hex, _iso(**delta)),
            )
            conn.commit()
        finally:
            conn.close()

    return _seed


@pytest.fixture
def seed_push_token(app):

    def _seed(user_id):
        token = f"ExponentPushToken[{uuid.uuid4().hex[:16]}]"
        conn = sqlite3.connect(app.config["DB_PATH"])
        try:
            conn.execute(
                "INSERT INTO push_tokens (id, user_id, token, platform) VALUES (?, ?, ?, 'ios')",
                (str(uuid.uuid4()), user_id, token),
            )
            conn.commit()
        finally:
            conn.close()

        return token

    return _seed




# -----------------------------------------------------------
# fake_web
# -----------------------------------------------------------
#
# The whole world the two news scrapers may see. Registering
# a URL without a query string matches any query, so the
# ?start=5 and ?page=2 listing pages are served the same
# markup the first page was — which is exactly how a real
# second page of already-seen articles ends the paging.
# -----------------------------------------------------------

@pytest.fixture
def fake_web():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        mock.add(responses.GET, KNF_LISTING_URL, body=_knf_listing_html(),
                 content_type="text/html; charset=utf-8")
        for slug, title in KNF_ARTICLES:
            mock.add(responses.GET, f"{KNF_LISTING_URL}/{slug}",
                     body=_knf_article_html(slug, title),
                     content_type="text/html; charset=utf-8")
        mock.add(responses.GET, VU_LISTING_URL, body=_vu_listing_html(),
                 content_type="text/html; charset=utf-8")
        mock.add(responses.GET, VU_ARTICLE_URL, body=_vu_article_html(),
                 content_type="text/html; charset=utf-8")
        yield mock




# -----------------------------------------------------------
# stub_scrapers
# -----------------------------------------------------------
#
# The four scrapers as routes.py bound them at import,
# replaced by recorders returning a healthy result. The
# trigger routes run their scraper synchronously inside the
# request, so every route test that is about status mapping,
# body shape or the role gate substitutes them here.
#
# Returns the call log: (name, args, kwargs) per invocation.
# -----------------------------------------------------------

@pytest.fixture
def stub_scrapers(monkeypatch):
    calls = []

    def _recorder(name, result):
        def _fake(*args, **kwargs):
            calls.append((name, args, kwargs))
            return dict(result)
        return _fake

    monkeypatch.setattr(scraper_routes, "scrape_knf_news",
                        _recorder("knf", {"found": 5, "new": 2, "runId": "knf-run"}))
    monkeypatch.setattr(scraper_routes, "scrape_vu_news",
                        _recorder("vu", {"found": 3, "new": 0, "runId": "vu-run"}))
    monkeypatch.setattr(scraper_routes, "scrape_knf_schedule",
                        _recorder("schedule", {"groups_scraped": 4, "lessons_found": 88,
                                               "lessons_new": 12, "dropped": 1,
                                               "runId": "sch-run"}))
    monkeypatch.setattr(scraper_routes, "scrape_faculty_info",
                        _recorder("info", {"pages_scraped": 3, "contacts_found": 21,
                                           "programs_found": 9, "runId": "info-run"}))
    return calls




# -----------------------------------------------------------
# plurals.py — the Lithuanian cardinal forms
# -----------------------------------------------------------

FORMS = ("naujas straipsnis", "nauji straipsniai", "naujų straipsnių")


@pytest.mark.parametrize("count", [1, 21, 31, 101, 121, 1001])
def test_a_count_ending_in_one_takes_the_singular_form(count):
    assert lt_plural(count, FORMS) == FORMS[0]


@pytest.mark.parametrize("count", [2, 3, 4, 5, 6, 7, 8, 9, 22, 23, 29, 102, 1234])
def test_a_count_ending_in_two_to_nine_takes_the_few_form(count):
    assert lt_plural(count, FORMS) == FORMS[1]


@pytest.mark.parametrize("count", [0, 10, 11, 12, 15, 19, 20, 30, 100, 110, 1000])
def test_the_teens_the_round_tens_and_zero_take_the_other_form(count):
    assert lt_plural(count, FORMS) == FORMS[2]


def test_eleven_is_other_even_though_it_ends_in_one():
    assert lt_plural(11, FORMS) == FORMS[2]
    assert lt_plural(1, FORMS) == FORMS[0]


@pytest.mark.parametrize("count", [111, 211, 1011])
def test_a_hundred_and_eleven_is_other_because_of_its_last_two_digits(count):
    assert lt_plural(count, FORMS) == FORMS[2]


@pytest.mark.parametrize("count", [112, 113, 119])
def test_the_teens_stay_other_inside_a_hundred_block(count):
    assert lt_plural(count, FORMS) == FORMS[2]


@pytest.mark.parametrize("count,expected", [(-1, 0), (-5, 1), (-11, 2), (-21, 0)])
def test_a_negative_count_is_folded_to_its_magnitude(count, expected):
    assert lt_plural(count, FORMS) == FORMS[expected]


def test_the_chosen_form_is_the_callers_own_string():
    forms = ("vienas", "keli", "daug")
    assert lt_plural(21, forms) is forms[0]
    assert lt_plural(2, forms) is forms[1]
    assert lt_plural(10, forms) is forms[2]


def test_the_push_body_declines_the_way_the_scrapers_build_it():
    # The exact copy knf_scraper/vu_scraper compose around it
    assert f"21 {lt_plural(21, FORMS)} iš knf.vu.lt" == "21 naujas straipsnis iš knf.vu.lt"
    assert f"3 {lt_plural(3, FORMS)} iš vu.lt" == "3 nauji straipsniai iš vu.lt"
    assert f"10 {lt_plural(10, FORMS)} iš vu.lt" == "10 naujų straipsnių iš vu.lt"




# -----------------------------------------------------------
# scheduler.py — the SCRAPER_ENABLED gate and the guard
# -----------------------------------------------------------

def test_creating_the_app_never_starts_a_scheduler(client):
    # Building the app used to scrape live sites seconds later
    # from a CLI run or a test import
    assert client.get("/api/health").status_code == 200
    assert scheduler._scheduler is None


def test_scraper_enabled_off_builds_no_scheduler_at_all(app, monkeypatch, caplog):
    monkeypatch.setenv("SCRAPER_ENABLED", "0")
    caplog.set_level(logging.INFO, logger="app.scraper.scheduler")

    assert scheduler.start_scraper_scheduler(app) is False
    assert scheduler._scheduler is None
    assert any("SCRAPER_ENABLED is off" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "No", "off", "OFF", " off "])
def test_every_spelling_of_off_keeps_the_scheduler_down(app, monkeypatch, value):
    monkeypatch.setenv("SCRAPER_ENABLED", value)

    assert scheduler.start_scraper_scheduler(app) is False
    assert scheduler._scheduler is None


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "anything"])
def test_anything_else_enables_the_scheduler(app, monkeypatch, value):
    monkeypatch.setenv("SCRAPER_ENABLED", value)
    monkeypatch.setenv("SCRAPER_STARTUP_RUNS", "0")

    assert scheduler.start_scraper_scheduler(app) is True
    assert scheduler._scheduler is not None


def test_an_unset_gate_defaults_to_on(app, monkeypatch):
    # Both gates default ON so an existing compose file that
    # never heard of them behaves exactly as it did
    monkeypatch.delenv("SCRAPER_ENABLED", raising=False)
    monkeypatch.setenv("SCRAPER_STARTUP_RUNS", "0")

    assert scheduler.start_scraper_scheduler(app) is True


def test_an_empty_gate_falls_back_to_the_default(app, monkeypatch):
    monkeypatch.setenv("SCRAPER_ENABLED", "   ")
    monkeypatch.setenv("SCRAPER_STARTUP_RUNS", "0")

    assert scheduler.start_scraper_scheduler(app) is True


def test_a_second_start_in_the_same_process_is_a_no_op(running_scheduler, app):
    first = running_scheduler._scheduler

    assert running_scheduler.start_scraper_scheduler(app) is True
    assert running_scheduler._scheduler is first
    assert len(first.get_jobs()) == len(JOB_INTERVALS)


@pytest.mark.parametrize("name,default,expected", [
    ("KNFAPP_UNSET_FLAG", True, True),
    ("KNFAPP_UNSET_FLAG", False, False),
])
def test_env_flag_falls_back_to_its_default_when_unset(monkeypatch, name, default, expected):
    monkeypatch.delenv(name, raising=False)

    assert scheduler._env_flag(name, default) is expected


@pytest.mark.parametrize("raw,expected", [
    ("0", False), ("false", False), ("No", False), ("OFF", False),
    ("1", True), ("true", True), ("yes", True), ("2", True), ("null", True),
])
def test_env_flag_reads_the_house_boolean_spellings(monkeypatch, raw, expected):
    monkeypatch.setenv("KNFAPP_TEST_FLAG", raw)

    assert scheduler._env_flag("KNFAPP_TEST_FLAG", True) is expected




# -----------------------------------------------------------
# scheduler.py — the job registry
# -----------------------------------------------------------

def test_all_seven_interval_jobs_are_registered(running_scheduler):
    assert {j.id for j in running_scheduler._scheduler.get_jobs()} == set(JOB_INTERVALS)


@pytest.mark.parametrize("job_id,interval", sorted(JOB_INTERVALS.items()))
def test_each_job_carries_the_interval_its_banner_claims(job, job_id, interval):
    assert job(job_id).trigger.interval == interval


@pytest.mark.parametrize("job_id", sorted(JOB_INTERVALS))
def test_no_job_may_overlap_itself(job, job_id):
    assert job(job_id).max_instances == 1


@pytest.mark.parametrize("job_id", sorted(JOB_INTERVALS))
def test_every_job_gets_five_minutes_of_misfire_grace(job, job_id):
    # APScheduler's default is ONE second, which dropped a tick
    # delayed by a long scrape without a word
    assert job(job_id).misfire_grace_time == scheduler.MISFIRE_GRACE_SECONDS
    assert scheduler.MISFIRE_GRACE_SECONDS == 300


def test_no_job_is_due_to_fire_while_a_test_runs(running_scheduler):
    # The first tick of an interval job is one full interval
    # after start() — proof that starting a scheduler in the
    # suite cannot reach a source site
    soon = datetime.now(timezone.utc) + timedelta(minutes=14)
    assert all(j.next_run_time > soon for j in running_scheduler._scheduler.get_jobs())


def test_a_missed_tick_is_logged_with_the_job_that_lost_it(caplog):
    caplog.set_level(logging.WARNING, logger="app.scraper.scheduler")

    class _Event:
        job_id = "news_scraper"
        scheduled_run_time = "2026-06-01T12:00:00+00:00"

    scheduler._log_missed_job(_Event())

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "news_scraper" in logged
    assert "2026-06-01T12:00:00+00:00" in logged


def test_the_missed_tick_listener_is_wired_to_the_scheduler(running_scheduler):
    from apscheduler.events import EVENT_JOB_MISSED

    listeners = running_scheduler._scheduler._listeners
    assert any(callback is scheduler._log_missed_job and mask & EVENT_JOB_MISSED
               for callback, mask in listeners)




# -----------------------------------------------------------
# scheduler.py — the startup one-shots
# -----------------------------------------------------------

def test_the_startup_one_shots_are_staggered_jittered_and_daemonised(app, monkeypatch, armed_timers):
    monkeypatch.setenv("SCRAPER_ENABLED", "1")
    monkeypatch.delenv("SCRAPER_STARTUP_RUNS", raising=False)

    assert scheduler.start_scraper_scheduler(app) is True

    assert len(armed_timers) == 3
    for timer, base in zip(armed_timers, (2.0, 30.0, 60.0)):
        assert base <= timer.interval < base + scheduler.STARTUP_JITTER_SECONDS
        assert timer.daemon is True
        assert timer.started is True

    # A process that never serves must be able to exit instead
    # of sitting through the 60 s info scrape
    assert scheduler._startup_timers == armed_timers


def test_startup_runs_off_leaves_the_interval_jobs_as_the_only_scrapes(app, monkeypatch, armed_timers, caplog):
    caplog.set_level(logging.INFO, logger="app.scraper.scheduler")
    monkeypatch.setenv("SCRAPER_ENABLED", "1")
    monkeypatch.setenv("SCRAPER_STARTUP_RUNS", "0")

    assert scheduler.start_scraper_scheduler(app) is True

    assert armed_timers == []
    assert scheduler._startup_timers == []
    assert any("SCRAPER_STARTUP_RUNS is off" in r.getMessage() for r in caplog.records)


def test_a_source_that_ran_inside_its_interval_skips_its_startup_scrape(app, monkeypatch, armed_timers,
                                                                        seed_run, caplog):
    caplog.set_level(logging.INFO, logger="app.scraper.scheduler")
    seed_run(source="knf.vu.lt", status="completed", minutes=-5)
    monkeypatch.setenv("SCRAPER_ENABLED", "1")
    monkeypatch.delenv("SCRAPER_STARTUP_RUNS", raising=False)

    assert scheduler.start_scraper_scheduler(app) is True

    # The 20-minute news source is covered; the 6 h and 24 h
    # ones are not
    assert len(armed_timers) == 2
    assert [round(t.interval) >= 30 for t in armed_timers] == [True, True]
    assert any("Skipping the startup knf.vu.lt scrape" in r.getMessage() for r in caplog.records)


def test_a_crash_loop_cannot_re_scrape_every_source_on_every_boot(app, monkeypatch, armed_timers, seed_run):
    seed_run(source="knf.vu.lt", status="completed", minutes=-5)
    seed_run(source="tvarkarasciai.vu.lt", status="completed", hours=-1)
    seed_run(source="knf.vu.lt/info", status="completed", hours=-2)
    monkeypatch.setenv("SCRAPER_ENABLED", "1")
    monkeypatch.delenv("SCRAPER_STARTUP_RUNS", raising=False)

    assert scheduler.start_scraper_scheduler(app) is True
    assert armed_timers == []


def test_stopping_cancels_the_one_shots_that_have_not_fired(app, monkeypatch, armed_timers):
    monkeypatch.setenv("SCRAPER_ENABLED", "1")
    monkeypatch.delenv("SCRAPER_STARTUP_RUNS", raising=False)
    scheduler.start_scraper_scheduler(app)
    assert len(armed_timers) == 3

    scheduler.stop_scraper_scheduler()

    assert all(timer.cancelled for timer in armed_timers)
    assert scheduler._startup_timers == []




# -----------------------------------------------------------
# scheduler.py — stopping
# -----------------------------------------------------------

def test_stopping_when_nothing_runs_is_safe():
    assert scheduler._scheduler is None

    scheduler.stop_scraper_scheduler()
    scheduler.stop_scraper_scheduler()

    assert scheduler._scheduler is None


def test_stopping_clears_the_guard_so_a_later_start_builds_a_new_one(app, monkeypatch):
    monkeypatch.setenv("SCRAPER_ENABLED", "1")
    monkeypatch.setenv("SCRAPER_STARTUP_RUNS", "0")
    scheduler.start_scraper_scheduler(app)
    first = scheduler._scheduler

    scheduler.stop_scraper_scheduler()
    assert scheduler._scheduler is None

    assert scheduler.start_scraper_scheduler(app) is True
    assert scheduler._scheduler is not first


def test_a_shutdown_that_raises_still_clears_the_guard(app, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="app.scraper.scheduler")
    monkeypatch.setenv("SCRAPER_ENABLED", "1")
    monkeypatch.setenv("SCRAPER_STARTUP_RUNS", "0")
    scheduler.start_scraper_scheduler(app)

    live = scheduler._scheduler
    real_shutdown = live.shutdown

    def _boom(**kwargs):
        raise RuntimeError("shutdown refused")

    monkeypatch.setattr(live, "shutdown", _boom)
    scheduler.stop_scraper_scheduler()

    assert scheduler._scheduler is None
    assert any("shutdown did not take" in r.getMessage() for r in caplog.records)

    # The guard is clear but the thread is not — the module
    # dropped its only handle on a scheduler that is still up
    real_shutdown(wait=False)




# -----------------------------------------------------------
# scheduler.py — the scraper_runs reconciliation
# -----------------------------------------------------------

def test_starting_closes_the_runs_a_killed_process_left_running(app, monkeypatch, db, seed_run):
    orphan = seed_run(source="knf.vu.lt", status="running", minutes=-40)
    monkeypatch.setenv("SCRAPER_ENABLED", "1")
    monkeypatch.setenv("SCRAPER_STARTUP_RUNS", "0")

    scheduler.start_scraper_scheduler(app)

    row = db.execute("SELECT status, error_message, finished_at FROM scraper_runs WHERE id = ?",
                     (orphan,)).fetchone()
    assert row["status"] == "failed"
    assert row["error_message"] == "interrupted"
    assert row["finished_at"] is not None


def test_reconciliation_leaves_finished_runs_alone(app, db, seed_run):
    done = seed_run(source="vu.lt", status="completed", days=-3, found=7, new=2)
    broken = seed_run(source="vu.lt", status="failed", days=-2, error="boom")

    scheduler._reconcile_interrupted_runs()

    assert db.execute("SELECT status FROM scraper_runs WHERE id = ?", (done,)).fetchone()[0] == "completed"
    assert db.execute("SELECT error_message FROM scraper_runs WHERE id = ?", (broken,)).fetchone()[0] == "boom"


def test_a_reconciliation_failure_does_not_cost_us_the_scheduler(app, monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger="app.scraper.scheduler")
    monkeypatch.setenv("SCRAPER_ENABLED", "1")
    monkeypatch.setenv("SCRAPER_STARTUP_RUNS", "0")

    def _boom():
        raise sqlite3.OperationalError("no such table: scraper_runs")

    monkeypatch.setattr(scheduler, "get_db", _boom)

    assert scheduler.start_scraper_scheduler(app) is True
    assert any("Could not reconcile interrupted scraper runs" in r.getMessage()
               for r in caplog.records)


def test_the_reconciliation_logs_how_many_rows_it_closed(app, seed_run, caplog):
    caplog.set_level(logging.WARNING, logger="app.scraper.scheduler")
    seed_run(source="knf.vu.lt", status="running", minutes=-30)
    seed_run(source="vu.lt", status="running", minutes=-30)

    scheduler._reconcile_interrupted_runs()

    assert any("Closed 2 scraper run(s)" in r.getMessage() for r in caplog.records)


def test_a_reconciliation_that_finds_nothing_says_nothing(app, caplog):
    caplog.set_level(logging.WARNING, logger="app.scraper.scheduler")

    scheduler._reconcile_interrupted_runs()

    assert [r for r in caplog.records if "Closed" in r.getMessage()] == []




# -----------------------------------------------------------
# scheduler.py — _ran_within
# -----------------------------------------------------------

def test_ran_within_sees_a_completed_run_inside_the_window(app, seed_run):
    seed_run(source="knf.vu.lt", status="completed", minutes=-5)

    assert scheduler._ran_within("knf.vu.lt", 20 * 60) is True


def test_ran_within_ignores_a_run_older_than_the_window(app, seed_run):
    seed_run(source="knf.vu.lt", status="completed", minutes=-25)

    assert scheduler._ran_within("knf.vu.lt", 20 * 60) is False


@pytest.mark.parametrize("status", ["running", "failed"])
def test_only_a_completed_run_counts_as_cover(app, seed_run, status):
    seed_run(source="knf.vu.lt", status=status, minutes=-1)

    assert scheduler._ran_within("knf.vu.lt", 20 * 60) is False


def test_another_sources_run_is_not_cover(app, seed_run):
    seed_run(source="vu.lt", status="completed", minutes=-1)

    assert scheduler._ran_within("knf.vu.lt", 20 * 60) is False


def test_a_broken_lookup_is_no_reason_to_stop_scraping(app, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="app.scraper.scheduler")

    def _boom():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(scheduler, "get_db", _boom)

    assert scheduler._ran_within("knf.vu.lt", 20 * 60) is False
    assert any("Could not check recent knf.vu.lt runs" in r.getMessage() for r in caplog.records)




# -----------------------------------------------------------
# scheduler.py — the job bodies
#
# Reached through the registry: the closures are defined
# inside start_scraper_scheduler and there is no other handle
# on them. What each test asserts is the job's EFFECT — the
# scraper it called, the rows it deleted, the log line it left.
# -----------------------------------------------------------

def test_the_news_job_runs_both_news_scrapers_with_the_scheduled_page_counts(job, monkeypatch):
    calls = []
    monkeypatch.setattr(knf_scraper, "scrape_knf_news",
                        lambda **kw: calls.append(("knf", kw)) or {"found": 1, "new": 1})
    monkeypatch.setattr(vu_scraper, "scrape_vu_news",
                        lambda **kw: calls.append(("vu", kw)) or {"found": 2, "new": 0})

    job("news_scraper").func()

    assert calls == [("knf", {"pages": 2}), ("vu", {"pages": 1})]


def test_a_broken_news_scraper_is_logged_and_never_escapes_the_job(job, monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger="app.scraper.scheduler")

    def _boom(**kwargs):
        raise RuntimeError("lxml is gone")

    monkeypatch.setattr(knf_scraper, "scrape_knf_news", _boom)
    # The two sources have a guard each, so vu.lt still runs
    # after knf.vu.lt explodes — patched here only to keep the
    # job off the network
    monkeypatch.setattr(vu_scraper, "scrape_vu_news", lambda **kw: {"found": 0, "new": 0})

    job("news_scraper").func()

    assert any("Scheduled scrape failed" in r.getMessage() for r in caplog.records)


def test_the_timetable_job_runs_the_schedule_scraper(job, monkeypatch):
    calls = []
    monkeypatch.setattr(schedule_scraper, "scrape_knf_schedule",
                        lambda: calls.append("ran") or {"groups_scraped": 3})

    job("schedule_scraper").func()

    assert calls == ["ran"]


def test_a_broken_timetable_scraper_is_logged(job, monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger="app.scraper.scheduler")

    def _boom():
        raise RuntimeError("tvarkarasciai is down")

    monkeypatch.setattr(schedule_scraper, "scrape_knf_schedule", _boom)

    job("schedule_scraper").func()

    assert any("Scheduled schedule scrape failed" in r.getMessage() for r in caplog.records)


def test_the_info_job_runs_the_faculty_info_scraper(job, monkeypatch):
    calls = []
    monkeypatch.setattr(info_scraper, "scrape_faculty_info",
                        lambda: calls.append("ran") or {"pages_scraped": 3})

    job("info_scraper").func()

    assert calls == ["ran"]


def test_a_broken_info_scraper_is_logged(job, monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger="app.scraper.scheduler")

    def _boom():
        raise RuntimeError("contacts page moved")

    monkeypatch.setattr(info_scraper, "scrape_faculty_info", _boom)

    job("info_scraper").func()

    assert any("Scheduled faculty info scrape failed" in r.getMessage() for r in caplog.records)


def test_the_receipt_job_polls_expo_and_reports_what_it_checked(job, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="app.scraper.scheduler")
    monkeypatch.setattr(push_module, "poll_push_receipts", lambda: 3)

    job("push_receipts").func()

    assert any("Checked 3 Expo push receipt(s)" in r.getMessage() for r in caplog.records)


def test_an_empty_receipt_poll_says_nothing(job, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="app.scraper.scheduler")
    monkeypatch.setattr(push_module, "poll_push_receipts", lambda: 0)

    job("push_receipts").func()

    assert [r for r in caplog.records if "Expo push receipt" in r.getMessage()] == []


def test_a_broken_receipt_poll_is_logged(job, monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger="app.scraper.scheduler")

    def _boom():
        raise RuntimeError("expo refused the ticket")

    monkeypatch.setattr(push_module, "poll_push_receipts", _boom)

    job("push_receipts").func()

    assert any("Push receipt poll failed" in r.getMessage() for r in caplog.records)


def test_the_sweep_job_deletes_expired_sessions_and_keeps_live_ones(job, db, make_user, seed_session):
    user = make_user()
    seed_session(user["id"], days=-1)
    seed_session(user["id"], days=1)

    job("session_sweep").func()

    rows = db.execute("SELECT expires_at FROM sessions WHERE user_id = ?", (user["id"],)).fetchall()
    assert len(rows) == 1
    assert rows[0]["expires_at"] > datetime.now(timezone.utc).isoformat()


def test_a_broken_sweep_is_logged(job, monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger="app.scraper.scheduler")

    def _boom():
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(scheduler, "get_db", _boom)

    job("session_sweep").func()

    assert any("Expired-session sweep failed" in r.getMessage() for r in caplog.records)


def test_the_token_prune_drops_tokens_whose_owner_has_no_live_session(job, db, make_user,
                                                                     seed_session, seed_push_token):
    logged_out = make_user()
    still_here = make_user()
    seed_session(logged_out["id"], days=-1)
    seed_session(still_here["id"], days=7)
    dead_token = seed_push_token(logged_out["id"])
    live_token = seed_push_token(still_here["id"])

    job("push_token_prune").func()

    kept = {r["token"] for r in db.execute("SELECT token FROM push_tokens").fetchall()}
    assert live_token in kept
    assert dead_token not in kept


def test_the_token_prune_logs_only_when_it_removed_something(job, make_user, seed_push_token, caplog):
    caplog.set_level(logging.INFO, logger="app.scraper.scheduler")

    job("push_token_prune").func()
    assert [r for r in caplog.records if "Pruned" in r.getMessage()] == []

    seed_push_token(make_user()["id"])
    job("push_token_prune").func()
    assert any("Pruned 1 push token(s)" in r.getMessage() for r in caplog.records)


def test_a_broken_token_prune_is_logged(job, monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger="app.scraper.scheduler")

    def _boom():
        raise sqlite3.OperationalError("no such table: push_tokens")

    monkeypatch.setattr(scheduler, "_prune_orphaned_push_tokens", _boom)

    job("push_token_prune").func()

    assert any("Push-token prune failed" in r.getMessage() for r in caplog.records)


def test_the_daily_reconcile_spares_a_run_that_is_still_in_flight(job, db, seed_run):
    in_flight = seed_run(source="knf.vu.lt", status="running", minutes=-10)
    abandoned = seed_run(source="vu.lt", status="running", hours=-7)

    job("run_reconcile").func()

    assert db.execute("SELECT status FROM scraper_runs WHERE id = ?", (in_flight,)).fetchone()[0] == "running"
    assert db.execute("SELECT status FROM scraper_runs WHERE id = ?", (abandoned,)).fetchone()[0] == "failed"


def test_the_stale_run_budget_is_longer_than_every_scrapers_wall_clock():
    # 6 h: longer than the longest run budget, so the daily
    # pass can never close a scrape happening right now
    assert scheduler.STALE_RUN_SECONDS == 6 * 3600
    assert scheduler.STALE_RUN_SECONDS > knf_scraper.RUN_BUDGET_SECONDS


def test_a_broken_daily_reconcile_is_logged(job, monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger="app.scraper.scheduler")

    def _boom(older_than_seconds=0):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(scheduler, "_reconcile_interrupted_runs", _boom)

    job("run_reconcile").func()

    assert any("Scraper-run reconciliation failed" in r.getMessage() for r in caplog.records)




# -----------------------------------------------------------
# routes.py — the admin-only gate
# -----------------------------------------------------------

@pytest.mark.parametrize("method,path", ENDPOINTS)
def test_a_guest_is_refused_every_scraper_route(client, stub_scrapers, method, path):
    response = getattr(client, method)(path)

    assert response.status_code == 401
    assert stub_scrapers == []


@pytest.mark.parametrize("method,path", ENDPOINTS)
def test_a_student_is_refused_every_scraper_route(client, actor, stub_scrapers, method, path):
    _, headers = actor

    response = getattr(client, method)(path, headers=headers)

    assert response.status_code == 403
    assert response.get_json()["error"] == "Insufficient permissions"
    assert stub_scrapers == []


@pytest.mark.parametrize("method,path", ENDPOINTS)
def test_a_teacher_is_refused_every_scraper_route(client, make_user, auth_headers, stub_scrapers,
                                                  method, path):
    teacher = make_user(role="teacher")

    response = getattr(client, method)(path, headers=auth_headers(teacher))

    assert response.status_code == 403
    assert stub_scrapers == []


@pytest.mark.parametrize("method,path", ENDPOINTS)
def test_a_curator_is_refused_every_scraper_route(client, make_user, auth_headers, stub_scrapers,
                                                  method, path):
    curator = make_user(role="curator")

    response = getattr(client, method)(path, headers=auth_headers(curator))

    assert response.status_code == 403
    assert stub_scrapers == []


@pytest.mark.parametrize("method,path", ENDPOINTS)
def test_an_admin_reaches_every_scraper_route(client, admin, stub_scrapers, method, path):
    _, headers = admin

    response = getattr(client, method)(path, headers=headers)

    assert response.status_code == 200


def test_a_bearer_token_that_is_not_a_session_is_refused(client, stub_scrapers):
    response = client.get(STATUS, headers={"Authorization": "Bearer not-a-real-token"})

    assert response.status_code == 401
    assert stub_scrapers == []




# -----------------------------------------------------------
# routes.py — GET /status
# -----------------------------------------------------------

def test_status_on_a_fresh_database_is_two_empty_lists(client, admin):
    _, headers = admin

    body = client.get(STATUS, headers=headers).get_json()

    assert body == {"runs": [], "sources": []}


def test_a_run_row_carries_exactly_the_documented_keys(client, admin, seed_run):
    _, headers = admin
    run_id = seed_run(source="knf.vu.lt", status="completed", minutes=-1, found=12, new=3,
                      finished_at=_iso())

    runs = client.get(STATUS, headers=headers).get_json()["runs"]

    assert len(runs) == 1
    assert set(runs[0]) == RUN_KEYS
    assert runs[0]["id"] == run_id
    assert runs[0]["source"] == "knf.vu.lt"
    assert runs[0]["status"] == "completed"
    assert runs[0]["articlesFound"] == 12
    assert runs[0]["articlesNew"] == 3
    assert runs[0]["error"] is None


def test_the_source_neutral_names_mirror_the_news_columns(client, admin, seed_run):
    _, headers = admin
    seed_run(source="tvarkarasciai.vu.lt", found=88, new=12, minutes=-1)

    row = client.get(STATUS, headers=headers).get_json()["runs"][0]

    assert (row["itemsFound"], row["itemsNew"]) == (row["articlesFound"], row["articlesNew"])
    assert (row["itemsFound"], row["itemsNew"]) == (88, 12)


def test_a_running_row_has_no_finish_and_no_error(client, admin, seed_run):
    _, headers = admin
    seed_run(source="knf.vu.lt/info", status="running", minutes=-2)

    row = client.get(STATUS, headers=headers).get_json()["runs"][0]

    assert row["status"] == "running"
    assert row["finishedAt"] is None
    assert row["error"] is None


def test_the_newest_runs_come_first(client, admin, seed_run):
    _, headers = admin
    seed_run(source="vu.lt", minutes=-30, run_id="oldest")
    seed_run(source="vu.lt", minutes=-1, run_id="newest")
    seed_run(source="vu.lt", minutes=-10, run_id="middle")

    runs = client.get(STATUS, headers=headers).get_json()["runs"]

    assert [r["id"] for r in runs] == ["newest", "middle", "oldest"]


def test_status_shows_at_most_twenty_runs(client, admin, seed_run):
    _, headers = admin
    for minute in range(25):
        seed_run(source="knf.vu.lt", minutes=-minute, run_id=f"run-{minute:02d}")

    runs = client.get(STATUS, headers=headers).get_json()["runs"]

    assert len(runs) == 20
    assert [r["id"] for r in runs] == [f"run-{m:02d}" for m in range(20)]


def test_status_filters_the_runs_by_source(client, admin, seed_run):
    _, headers = admin
    seed_run(source="knf.vu.lt", minutes=-1)
    seed_run(source="vu.lt", minutes=-2)
    seed_run(source="tvarkarasciai.vu.lt", minutes=-3)

    body = client.get(f"{STATUS}?source=vu.lt", headers=headers).get_json()

    assert [r["source"] for r in body["runs"]] == ["vu.lt"]
    assert [s["source"] for s in body["sources"]] == ["vu.lt"]


@pytest.mark.parametrize("wanted", ["running", "completed", "failed"])
def test_status_filters_the_runs_by_status(client, admin, seed_run, wanted):
    _, headers = admin
    seed_run(source="knf.vu.lt", status="running", minutes=-1)
    seed_run(source="knf.vu.lt", status="completed", minutes=-2)
    seed_run(source="knf.vu.lt", status="failed", minutes=-3, error="boom")

    runs = client.get(f"{STATUS}?status={wanted}", headers=headers).get_json()["runs"]

    assert [r["status"] for r in runs] == [wanted]


def test_an_unknown_status_filter_is_ignored_rather_than_refused(client, admin, seed_run):
    # The route is a dashboard, not a form — a typo shows
    # everything instead of a 400
    _, headers = admin
    seed_run(source="knf.vu.lt", status="completed", minutes=-1)
    seed_run(source="knf.vu.lt", status="failed", minutes=-2)

    response = client.get(f"{STATUS}?status=exploded", headers=headers)

    assert response.status_code == 200
    assert len(response.get_json()["runs"]) == 2


def test_the_status_filter_is_case_insensitive(client, admin, seed_run):
    _, headers = admin
    seed_run(source="knf.vu.lt", status="failed", minutes=-1, error="boom")
    seed_run(source="knf.vu.lt", status="completed", minutes=-2)

    runs = client.get(f"{STATUS}?status=FAILED", headers=headers).get_json()["runs"]

    assert [r["status"] for r in runs] == ["failed"]


def test_both_filters_apply_together(client, admin, seed_run):
    _, headers = admin
    seed_run(source="knf.vu.lt", status="failed", minutes=-1, error="knf broke")
    seed_run(source="vu.lt", status="failed", minutes=-2, error="vu broke")
    seed_run(source="knf.vu.lt", status="completed", minutes=-3)

    runs = client.get(f"{STATUS}?source=knf.vu.lt&status=failed", headers=headers).get_json()["runs"]

    assert [r["error"] for r in runs] == ["knf broke"]


def test_an_unknown_source_filter_yields_nothing(client, admin, seed_run):
    _, headers = admin
    seed_run(source="knf.vu.lt", minutes=-1)

    body = client.get(f"{STATUS}?source=example.com", headers=headers).get_json()

    assert body == {"runs": [], "sources": []}


def test_the_summary_names_each_sources_latest_success_and_failure(client, admin, seed_run):
    _, headers = admin
    seed_run(source="knf.vu.lt", status="completed", days=-41, found=40, run_id="old-success")
    seed_run(source="knf.vu.lt", status="failed", days=-2, error="template changed", run_id="recent-failure")
    seed_run(source="knf.vu.lt", status="failed", hours=-1, error="template changed", run_id="latest")

    sources = client.get(STATUS, headers=headers).get_json()["sources"]

    assert len(sources) == 1
    entry = sources[0]
    assert entry["source"] == "knf.vu.lt"
    assert entry["latest"]["id"] == "latest"
    assert entry["lastSuccess"]["id"] == "old-success"
    assert entry["lastFailure"]["id"] == "latest"


def test_a_source_that_never_succeeded_reports_a_null_last_success(client, admin, seed_run):
    _, headers = admin
    seed_run(source="knf.vu.lt/info", status="failed", days=-1, error="404 on the contacts page")

    entry = client.get(STATUS, headers=headers).get_json()["sources"][0]

    assert entry["lastSuccess"] is None
    assert entry["lastFailure"]["error"] == "404 on the contacts page"


def test_a_source_that_never_failed_reports_a_null_last_failure(client, admin, seed_run):
    _, headers = admin
    seed_run(source="vu.lt", status="completed", days=-1, found=9, new=1)

    entry = client.get(STATUS, headers=headers).get_json()["sources"][0]

    assert entry["lastFailure"] is None
    assert entry["lastSuccess"]["articlesFound"] == 9


def test_the_summary_lists_every_source_in_order(client, admin, seed_run):
    _, headers = admin
    for source in ("vu.lt", "knf.vu.lt", "tvarkarasciai.vu.lt", "knf.vu.lt/info"):
        seed_run(source=source, minutes=-1)

    sources = [s["source"] for s in client.get(STATUS, headers=headers).get_json()["sources"]]

    assert sources == sorted(sources)
    assert set(sources) == {"vu.lt", "knf.vu.lt", "tvarkarasciai.vu.lt", "knf.vu.lt/info"}


def test_a_run_summary_entry_has_the_same_shape_as_a_run_row(client, admin, seed_run):
    _, headers = admin
    seed_run(source="vu.lt", status="completed", minutes=-1, finished_at=_iso())

    entry = client.get(STATUS, headers=headers).get_json()["sources"][0]

    assert set(entry) == {"source", "latest", "lastSuccess", "lastFailure"}
    assert set(entry["latest"]) == RUN_KEYS


def test_error_text_is_escaped_on_the_way_out(client, admin, seed_run):
    _, headers = admin
    seed_run(source="knf.vu.lt", status="failed", minutes=-1,
             error="<script>alert(1)</script>")

    row = client.get(STATUS, headers=headers).get_json()["runs"][0]

    assert "<script>" not in row["error"]
    assert "&lt;script&gt;" in row["error"]




# -----------------------------------------------------------
# routes.py — the trigger routes
# -----------------------------------------------------------

@pytest.mark.parametrize("path", [TRIGGER, RUN])
def test_trigger_and_run_are_the_same_endpoint(client, admin, stub_scrapers, path):
    _, headers = admin

    response = client.post(path, headers=headers)

    assert response.status_code == 200
    assert response.get_json() == {
        "knf": {"found": 5, "new": 2, "runId": "knf-run"},
        "vu": {"found": 3, "new": 0, "runId": "vu-run"},
    }


def test_a_hand_fired_news_scrape_never_pushes_and_keeps_the_timer_page_counts(client, admin, stub_scrapers):
    _, headers = admin

    client.post(TRIGGER, headers=headers)

    assert stub_scrapers == [
        ("knf", (), {"pages": 2, "notify": False}),
        ("vu", (), {"pages": 1, "notify": False}),
    ]


def test_a_scraper_that_stepped_aside_answers_409(client, admin, monkeypatch, stub_scrapers):
    _, headers = admin
    monkeypatch.setattr(scraper_routes, "scrape_knf_news",
                        lambda **kw: {"found": 0, "new": 0, "skipped": True})

    response = client.post(TRIGGER, headers=headers)

    assert response.status_code == 409
    assert response.get_json()["knf"]["skipped"] is True


def test_a_failed_scrape_answers_502_with_a_stable_slug(client, admin, monkeypatch, stub_scrapers, caplog):
    caplog.set_level(logging.WARNING, logger="app.scraper.routes")
    _, headers = admin
    raw = "HTTPSConnectionPool(host='knf.vu.lt'): Max retries exceeded"
    monkeypatch.setattr(scraper_routes, "scrape_vu_news",
                        lambda **kw: {"found": 0, "new": 0, "error": raw, "runId": "vu-run"})

    response = client.post(TRIGGER, headers=headers)

    assert response.status_code == 502
    assert response.get_json()["vu"]["error"] == scraper_routes.ERROR_SLUG
    # The exception text stays in the log, never in the body
    assert raw not in response.get_data(as_text=True)
    assert any(raw in r.getMessage() for r in caplog.records)


def test_a_failure_outranks_a_scraper_that_merely_stepped_aside(client, admin, monkeypatch, stub_scrapers):
    _, headers = admin
    monkeypatch.setattr(scraper_routes, "scrape_knf_news",
                        lambda **kw: {"found": 0, "new": 0, "skipped": True})
    monkeypatch.setattr(scraper_routes, "scrape_vu_news",
                        lambda **kw: {"found": 0, "new": 0, "error": "boom"})

    response = client.post(TRIGGER, headers=headers)

    assert response.status_code == 502


def test_a_run_id_rides_along_so_an_admin_can_read_the_full_error(client, admin, monkeypatch,
                                                                  stub_scrapers, seed_run, db):
    _, headers = admin
    run_id = seed_run(source="vu.lt", status="failed", minutes=-1, error="the whole traceback")
    monkeypatch.setattr(scraper_routes, "scrape_vu_news",
                        lambda **kw: {"found": 0, "new": 0, "error": "the whole traceback",
                                      "runId": run_id})

    body = client.post(TRIGGER, headers=headers).get_json()

    assert body["vu"]["runId"] == run_id
    stored = db.execute("SELECT error_message FROM scraper_runs WHERE id = ?", (run_id,)).fetchone()
    assert stored["error_message"] == "the whole traceback"


def test_a_scraper_that_answers_something_other_than_a_dict_is_a_failure(client, admin, monkeypatch,
                                                                        stub_scrapers):
    _, headers = admin
    monkeypatch.setattr(scraper_routes, "scrape_knf_news", lambda **kw: None)

    response = client.post(TRIGGER, headers=headers)

    assert response.status_code == 502
    assert response.get_json()["knf"] == {"error": scraper_routes.ERROR_SLUG, "source": "knf.vu.lt"}


def test_the_timetable_trigger_returns_the_scrapers_own_counts(client, admin, stub_scrapers):
    _, headers = admin

    response = client.post(SCHEDULE, headers=headers)

    assert response.status_code == 200
    assert response.get_json() == {"groups_scraped": 4, "lessons_found": 88, "lessons_new": 12,
                                   "dropped": 1, "runId": "sch-run"}
    assert stub_scrapers == [("schedule", (), {"notify": False})]


def test_a_timetable_run_already_going_answers_409(client, admin, monkeypatch, stub_scrapers):
    _, headers = admin
    monkeypatch.setattr(scraper_routes, "scrape_knf_schedule",
                        lambda **kw: {"groups_scraped": 0, "lessons_found": 0,
                                      "lessons_new": 0, "skipped": True})

    assert client.post(SCHEDULE, headers=headers).status_code == 409


def test_a_broken_timetable_scrape_answers_502_not_500(client, admin, monkeypatch, stub_scrapers):
    _, headers = admin
    monkeypatch.setattr(scraper_routes, "scrape_knf_schedule",
                        lambda **kw: {"groups_scraped": 0, "error": "ImportError: no module"})

    response = client.post(SCHEDULE, headers=headers)

    assert response.status_code == 502
    assert response.get_json()["error"] == scraper_routes.ERROR_SLUG


def test_the_info_trigger_returns_the_scrapers_own_counts(client, admin, stub_scrapers):
    _, headers = admin

    response = client.post(INFO, headers=headers)

    assert response.status_code == 200
    assert response.get_json() == {"pages_scraped": 3, "contacts_found": 21,
                                   "programs_found": 9, "runId": "info-run"}
    assert stub_scrapers == [("info", (), {})]


def test_an_info_run_already_going_answers_409(client, admin, monkeypatch, stub_scrapers):
    _, headers = admin
    monkeypatch.setattr(scraper_routes, "scrape_faculty_info",
                        lambda: {"pages_scraped": 0, "contacts_found": 0,
                                 "programs_found": 0, "skipped": True})

    assert client.post(INFO, headers=headers).status_code == 409


def test_a_source_site_that_is_down_is_502_and_not_our_500(client, admin, monkeypatch, stub_scrapers):
    _, headers = admin
    monkeypatch.setattr(scraper_routes, "scrape_faculty_info",
                        lambda: {"pages_scraped": 0, "contacts_found": 0, "programs_found": 0,
                                 "error": "503 Server Error", "runId": "info-run"})

    response = client.post(INFO, headers=headers)

    assert response.status_code == 502
    assert response.get_json() == {"pages_scraped": 0, "contacts_found": 0, "programs_found": 0,
                                   "error": scraper_routes.ERROR_SLUG, "runId": "info-run"}


def test_an_info_run_that_completed_with_a_broken_section_still_reads_as_a_failure(client, admin,
                                                                                  monkeypatch,
                                                                                  stub_scrapers):
    # The scraper marks the RUN completed but carries the
    # section's error; the trigger's contract is that any
    # "error" key is a 502
    _, headers = admin
    monkeypatch.setattr(scraper_routes, "scrape_faculty_info",
                        lambda: {"pages_scraped": 3, "contacts_found": 21, "programs_found": 0,
                                 "error": "programs: selector matched nothing"})

    assert client.post(INFO, headers=headers).status_code == 502




# -----------------------------------------------------------
# End to end — the trigger against fixture HTML
#
# Everything below drives the real knf and vu scrapers through
# `responses`. Nothing leaves the container; the container has
# no network to leave through either.
# -----------------------------------------------------------

def test_a_triggered_scrape_stores_the_articles_it_found(client, admin, db, fake_web):
    _, headers = admin

    response = client.post(TRIGGER, headers=headers)

    assert response.status_code == 200
    body = response.get_json()
    assert (body["knf"]["found"], body["knf"]["new"]) == (2, 2)
    assert (body["vu"]["found"], body["vu"]["new"]) == (1, 1)

    stored = db.execute(
        "SELECT source, source_url, title, author_name FROM news_posts ORDER BY source, title"
    ).fetchall()
    assert [r["source"] for r in stored] == ["knf.vu.lt", "knf.vu.lt", "vu.lt"]
    assert stored[2]["source_url"] == VU_ARTICLE_URL
    assert stored[0]["author_name"] == "Fakulteto administracija"


def test_the_scrape_opens_and_closes_a_run_row_per_source(client, admin, db, fake_web):
    _, headers = admin

    client.post(TRIGGER, headers=headers)

    rows = db.execute(
        "SELECT source, status, articles_found, articles_new, finished_at"
        " FROM scraper_runs ORDER BY source"
    ).fetchall()
    assert [r["source"] for r in rows] == ["knf.vu.lt", "vu.lt"]
    assert {r["status"] for r in rows} == {"completed"}
    assert all(r["finished_at"] is not None for r in rows)
    assert (rows[0]["articles_found"], rows[0]["articles_new"]) == (2, 2)


def test_running_the_same_scrape_twice_inserts_nothing_the_second_time(client, admin, db, fake_web):
    _, headers = admin

    first = client.post(TRIGGER, headers=headers).get_json()
    after_first = db.execute("SELECT COUNT(*) FROM news_posts").fetchone()[0]

    second = client.post(TRIGGER, headers=headers).get_json()

    assert (first["knf"]["new"], first["vu"]["new"]) == (2, 1)
    assert (second["knf"]["new"], second["vu"]["new"]) == (0, 0)
    # Still counted as found — a stored article is not re-fetched
    assert (second["knf"]["found"], second["vu"]["found"]) == (2, 1)
    assert db.execute("SELECT COUNT(*) FROM news_posts").fetchone()[0] == after_first == 3


def test_the_sources_own_offset_is_applied_to_the_stored_timestamp(client, admin, db, fake_web):
    # +03:00 Vilnius wall clock converted to UTC, not dropped —
    # dropping it used to land every article 3 h in the future
    _, headers = admin

    client.post(TRIGGER, headers=headers)

    stamps = {r["published_at"] for r in db.execute("SELECT published_at FROM news_posts").fetchall()}
    assert stamps == {PUBLISHED_STORED}


def test_the_status_route_shows_the_run_the_trigger_just_made(client, admin, fake_web):
    _, headers = admin

    client.post(TRIGGER, headers=headers)
    body = client.get(STATUS, headers=headers).get_json()

    assert {r["source"] for r in body["runs"]} == {"knf.vu.lt", "vu.lt"}
    knf = next(s for s in body["sources"] if s["source"] == "knf.vu.lt")
    assert knf["lastSuccess"]["itemsNew"] == 2
    assert knf["lastFailure"] is None


def test_a_listing_that_downloads_but_shows_no_articles_fails_the_run(client, admin, db):
    # 'completed with zero' is how a template change stayed
    # invisible for months
    _, headers = admin
    empty = """<!doctype html><html lang="lt"><head><meta charset="utf-8"></head>
<body><div class="blog"><p>Šiuo metu naujienų nėra.</p></div></body></html>"""

    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        mock.add(responses.GET, KNF_LISTING_URL, body=empty, content_type="text/html; charset=utf-8")
        mock.add(responses.GET, VU_LISTING_URL, body=_vu_listing_html(),
                 content_type="text/html; charset=utf-8")
        mock.add(responses.GET, VU_ARTICLE_URL, body=_vu_article_html(),
                 content_type="text/html; charset=utf-8")
        response = client.post(TRIGGER, headers=headers)

    assert response.status_code == 502
    assert response.get_json()["knf"]["error"] == scraper_routes.ERROR_SLUG
    row = db.execute("SELECT status, error_message FROM scraper_runs WHERE source = 'knf.vu.lt'").fetchone()
    assert row["status"] == "failed"
    assert "template has probably changed" in row["error_message"]


def test_a_trigger_that_finds_the_source_lock_taken_answers_409(client, admin, db, fake_web):
    _, headers = admin
    assert knf_scraper._RUN_LOCK.acquire(blocking=False)
    try:
        response = client.post(TRIGGER, headers=headers)
    finally:
        knf_scraper._RUN_LOCK.release()

    assert response.status_code == 409
    body = response.get_json()
    assert body["knf"]["skipped"] is True
    # The other scraper is a different lock and ran anyway
    assert "skipped" not in body["vu"]
    assert db.execute("SELECT COUNT(*) FROM scraper_runs WHERE source = 'knf.vu.lt'").fetchone()[0] == 0




# -----------------------------------------------------------
# The 30-day scraper_runs retention
#
# The clock is frozen so the boundary is exact: a row one day
# inside the window must survive and a row one day outside it
# must not — except each source's newest, which is kept
# whatever its age, because a scraper that stopped running in
# spring is exactly the one /status has to keep showing.
# -----------------------------------------------------------

def test_runs_older_than_thirty_days_are_pruned_when_a_run_finishes(client, admin, db, seed_run, fake_web):
    _, headers = admin

    with time_machine.travel(FROZEN, tick=False):
        inside = seed_run(source="knf.vu.lt", base=FROZEN, days=-29, run_id="inside-window")
        outside = seed_run(source="knf.vu.lt", base=FROZEN, days=-31, run_id="outside-window")
        ancient = seed_run(source="vu.lt", base=FROZEN, days=-400, run_id="ancient-vu")
        orphaned = seed_run(source="tvarkarasciai.vu.lt", base=FROZEN, days=-400, run_id="only-timetable-run")

        assert client.post(TRIGGER, headers=headers).status_code == 200

    surviving = {r["id"] for r in db.execute("SELECT id FROM scraper_runs").fetchall()}
    assert inside in surviving
    assert outside not in surviving
    assert ancient not in surviving
    # The only run this source ever had — retention must never
    # be the reason it vanishes from /status
    assert orphaned in surviving


def test_retention_never_empties_a_source_that_stopped_running(client, admin, db, seed_run, fake_web):
    _, headers = admin

    with time_machine.travel(FROZEN, tick=False):
        seed_run(source="knf.vu.lt/info", base=FROZEN, days=-300, status="failed",
                 error="contacts page 404", run_id="last-info-run")
        seed_run(source="knf.vu.lt/info", base=FROZEN, days=-301, run_id="older-info-run")

        client.post(TRIGGER, headers=headers)
        body = client.get(STATUS, headers=headers).get_json()

    info = next(s for s in body["sources"] if s["source"] == "knf.vu.lt/info")
    assert info["latest"]["id"] == "last-info-run"
    assert db.execute("SELECT COUNT(*) FROM scraper_runs WHERE source = 'knf.vu.lt/info'").fetchone()[0] == 1


def test_the_retention_window_is_thirty_days():
    from app.scraper.common import RUN_RETENTION_DAYS

    assert RUN_RETENTION_DAYS == 30
