# -----------------------------------------------------------
#  [*] Info — the faculty handbook (GET /api/info)
#
#  What this module proves about app/info/routes.py:
#
#    - The payload is the curated FACULTY_INFO handbook for the
#      language asked for, section for section, in exactly the
#      shape services/api/info.ts types as FacultyInfoResponse
#      — plus the two additive keys "lang" and "updatedAt".
#      The route is PUBLIC: no token, no role, no 401.
#    - ?lang is normalised before the whitelist decides ("EN",
#      "en-GB", "en_GB", " en " are all English) and anything
#      still unknown silently becomes lt — never a 400 — while
#      the answer says which language it actually served.
#    - The scraped overlay only ever wins when it earns it:
#      contacts and programs must be lists AND clear their item
#      floors, general_contact must be a non-empty dict. Every
#      rejected shape leaves the curated value standing instead
#      of blanking the Info screen — the half-scraped page and
#      the contacts blob that was not a list are the two
#      regressions this section exists for.
#    - Rows the scraper never refreshed (older than
#      SCRAPED_MAX_AGE_DAYS), rows whose JSON does not parse and
#      a faculty_info table that cannot be read at all all fall
#      back to the curated handbook with a 200, never a 500.
#    - THE LANGUAGE FALLBACK: info_scraper.py writes lang 'lt'
#      only, so ?lang=en must borrow those rows. An English
#      answer may never carry a DIFFERENT contacts / programs /
#      general_contact dataset than the Lithuanian one — only
#      its links, hours and faq stay English. Lithuanian never
#      borrows the other way.
#    - The caching contract: a weak ETag over (lang, section,
#      newest scrape stamp, handbook fingerprint), Cache-Control
#      public for CACHE_MAX_AGE, a matching If-None-Match
#      answered 304 with no body, and a moved ETag as soon as a
#      new scrape lands.
#
#  Everything is driven through the route; the direct `db`
#  connection only arranges faculty_info rows, which nothing in
#  the API can write (the scraper owns that table), and drops
#  the table to force the lookup failure.
# -----------------------------------------------------------

import html
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import time_machine

from app.info.routes import (
    CACHE_MAX_AGE,
    FACULTY_INFO,
    MIN_SCRAPED_CONTACT_ITEMS,
    MIN_SCRAPED_PROGRAMS,
    SCRAPED_MAX_AGE_DAYS,
    _parse_timestamp,
    _warn_once,
    _warned,
)


# The exact wire shape the mobile client reads
# (services/api/info.ts FacultyInfoResponse) plus the two keys
# the route adds on top
_SECTION_KEYS = {"contacts", "links", "hours", "programs", "faq"}
_CONTACT_KEYS = {"name", "phone", "email", "room", "position"}

# A stamp far from any real "now", so a frozen clock and the
# stored rows can never accidentally agree by coincidence
_NOW = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)




# -----------------------------------------------------------
# _clean_rate_limits
# -----------------------------------------------------------
#
# The global per-IP limiter (app/__init__.py before_request,
# 600 requests per five minutes) keeps its store in a MODULE
# level dict that outlives the per-test app and is shared with
# every other file in the process. This file is request-heavy
# and every test client call has the same remote_addr, so
# without this the 601st request of the run would 429 whichever
# test happened to make it.
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_rate_limits():
    from app.auth.routes import _rate_limit_store

    _rate_limit_store.clear()
    yield
    _rate_limit_store.clear()




# -----------------------------------------------------------
# _forget_warnings
# -----------------------------------------------------------
#
# _warn_once remembers, for the life of the PROCESS, which
# conditions it has already logged. Tests that assert a warning
# was emitted (or counted) would otherwise depend on which test
# ran first, so the memory is wiped around each of them.
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _forget_warnings():
    _warned.clear()
    yield
    _warned.clear()




# -----------------------------------------------------------
# _seed_section
# -----------------------------------------------------------
#
#   _seed_section(db, "contacts", blob)              — fresh lt
#   _seed_section(db, "programs", blob, lang="en")
#   _seed_section(db, "contacts", raw="{oops")       — bad JSON
#   _seed_section(db, "contacts", blob, scraped_at=…)
#
# One faculty_info row, written the way scraper/info_scraper.py
# writes it (`_store_info`: uuid id, ensure_ascii=False JSON, an
# ISO stamp). Nothing in the API can create these rows, so this
# is the only way to arrange a scraped overlay. `raw` bypasses
# the encoder for the undecodable-blob cases.
# -----------------------------------------------------------

def _seed_section(db, section, data=None, lang="lt", scraped_at=None, raw=None):
    db.execute(
        "INSERT OR REPLACE INTO faculty_info (id, lang, section, data_json, scraped_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (uuid.uuid4().hex, lang, section,
         raw if raw is not None else json.dumps(data, ensure_ascii=False),
         datetime.now(timezone.utc).isoformat() if scraped_at is None else scraped_at),
    )
    db.commit()




# -----------------------------------------------------------
# _contacts / _programs / _general
# -----------------------------------------------------------
#
# Scraped blobs in the shape info_scraper.py produces, sized by
# the caller so a test can sit exactly on, just under or well
# over the overlay floors. Deliberately free of &, <, ", ' so a
# comparison is against the seeded value itself — the JSON
# provider entity-escapes every string on the way out (see
# _as_served below).
# -----------------------------------------------------------

def _contacts(items=6, category="Scraped Dekanatas"):
    return [{
        "category": category,
        "items": [{"name": f"Scraped person {i}", "phone": f"+370 37 000 0{i}",
                   "email": f"person{i}@knf.vu.lt", "room": str(100 + i)}
                  for i in range(items)],
    }]


def _programs(count=4, prefix="Scraped program"):
    return [{"name": f"{prefix} {i}", "degree": "Bakalauras", "duration": "4 metai"}
            for i in range(count)]


def _general(email="knf@knf.vu.lt"):
    return {"address": "Muitines g. 8, LT-44280 Kaunas", "phone": "+370 37 422 523", "email": email}




# -----------------------------------------------------------
# _as_served
# -----------------------------------------------------------
#
# The curated handbook as the wire carries it: app/__init__.py
# serialises every JSON body through an escaping provider, so
# "Dean's Office" leaves as "Dean&#x27;s Office". Comparing a
# response against _as_served(FACULTY_INFO[...]) proves the
# payload IS the curated dict rather than merely resembling it.
# -----------------------------------------------------------

def _as_served(value):
    if isinstance(value, str):
        return html.escape(value, quote=True)
    if isinstance(value, list):
        return [_as_served(v) for v in value]
    if isinstance(value, dict):
        return {key: _as_served(item) for key, item in value.items()}
    return value


def _get(client, query="", **kwargs):
    return client.get(f"/api/info{query}", **kwargs)




# -----------------------------------------------------------
# The public contract: no token, both languages, every section
# -----------------------------------------------------------

def test_the_handbook_is_public_and_needs_no_token(client):
    response = _get(client)

    assert response.status_code == 200


def test_a_bearer_token_changes_nothing_about_the_answer(client, actor):
    _user, headers = actor

    anonymous = _get(client).get_json()
    signed_in = _get(client, headers=headers).get_json()

    assert anonymous == signed_in


@pytest.mark.contract
def test_the_lithuanian_payload_carries_every_curated_section(client):
    body = _get(client).get_json()

    assert set(body) == _SECTION_KEYS | {"lang"}
    for section in _SECTION_KEYS:
        assert body[section] == _as_served(FACULTY_INFO["lt"][section])


@pytest.mark.contract
def test_the_english_payload_carries_every_curated_section(client):
    body = _get(client, "?lang=en").get_json()

    assert set(body) == _SECTION_KEYS | {"lang"}
    for section in _SECTION_KEYS:
        assert body[section] == _as_served(FACULTY_INFO["en"][section])


def test_no_language_at_all_serves_lithuanian(client):
    body = _get(client).get_json()

    assert body["lang"] == "lt"
    assert body["contacts"][0]["category"] == "Dekanatas"


def test_english_is_a_different_dataset_from_lithuanian(client):
    lithuanian = _get(client).get_json()
    english = _get(client, "?lang=en").get_json()

    assert english["links"][0]["title"] == "KNF Website"
    assert lithuanian["links"][0]["title"] != english["links"][0]["title"]
    assert english["hours"][0]["place"] == "Faculty Building"


@pytest.mark.contract
def test_every_contact_category_holds_the_shape_the_app_maps_over(client):
    for lang in ("lt", "en"):
        body = _get(client, f"?lang={lang}").get_json()

        assert isinstance(body["contacts"], list) and body["contacts"]
        for category in body["contacts"]:
            assert set(category) == {"category", "items"}
            assert isinstance(category["items"], list) and category["items"]
            for item in category["items"]:
                assert item["name"]
                assert set(item) <= _CONTACT_KEYS


@pytest.mark.contract
def test_every_link_hour_program_and_faq_entry_holds_its_documented_keys(client):
    for lang in ("lt", "en"):
        body = _get(client, f"?lang={lang}").get_json()

        for link in body["links"]:
            assert set(link) == {"title", "url", "icon"}
            assert link["url"].startswith("https://")
            assert link["icon"]
        for hours in body["hours"]:
            assert set(hours) == {"place", "address", "schedule", "note"}
        for program in body["programs"]:
            assert set(program) == {"name", "degree", "duration"}
        for entry in body["faq"]:
            assert set(entry) == {"q", "a"}
            assert entry["q"] and entry["a"]


def test_a_contact_without_a_phone_still_carries_its_email_and_room(client):
    services = [c for c in _get(client).get_json()["contacts"] if c["category"] == "Paslaugos"][0]
    council = [i for i in services["items"] if i["room"] == "110"][0]

    assert "phone" not in council
    assert council["email"] == "sa@knf.vu.lt"


def test_the_two_languages_stay_in_step_section_for_section(client):
    lithuanian = _get(client).get_json()
    english = _get(client, "?lang=en").get_json()

    for section in _SECTION_KEYS:
        assert len(lithuanian[section]) == len(english[section]), \
            f"'{section}' was edited in one language only"
    for lt_category, en_category in zip(lithuanian["contacts"], english["contacts"]):
        assert len(lt_category["items"]) == len(en_category["items"])


def test_the_curated_handbook_carries_no_updated_at(client):
    assert "updatedAt" not in _get(client).get_json()


def test_the_curated_handbook_carries_no_general_contact(client):
    assert "general_contact" not in _get(client).get_json()




# -----------------------------------------------------------
# ?lang normalisation — never a 400, always an echo
# -----------------------------------------------------------

@pytest.mark.parametrize("value", ["en", "EN", "En", "en-GB", "EN-gb", "en_GB", " en ", "en-"])
def test_every_english_spelling_serves_english(client, value):
    body = _get(client, f"?lang={value}").get_json()

    assert body["lang"] == "en"
    assert body["links"][0]["title"] == "KNF Website"


@pytest.mark.parametrize("value", ["lt", "LT", "lt-LT", "lt_LT"])
def test_every_lithuanian_spelling_serves_lithuanian(client, value):
    assert _get(client, f"?lang={value}").get_json()["lang"] == "lt"


@pytest.mark.parametrize("value", ["de", "ru", "xx", "english", "e", "123", "%20", "lt%00"])
def test_an_unknown_language_silently_becomes_lithuanian(client, value):
    response = _get(client, f"?lang={value}")

    assert response.status_code == 200
    assert response.get_json()["lang"] == "lt"


def test_an_empty_language_becomes_lithuanian(client):
    assert _get(client, "?lang=").get_json()["lang"] == "lt"


def test_a_whitespace_only_language_becomes_lithuanian(client):
    assert _get(client, "?lang=%20%20").get_json()["lang"] == "lt"


def test_the_first_lang_value_wins_when_the_query_repeats_it(client):
    assert _get(client, "?lang=en&lang=lt").get_json()["lang"] == "en"


def test_the_answer_always_echoes_the_language_it_served(client):
    for asked, served in [("en", "en"), ("EN-gb", "en"), ("de", "lt"), ("", "lt")]:
        assert _get(client, f"?lang={asked}").get_json()["lang"] == served




# -----------------------------------------------------------
# ?section — one slice, or a 400
# -----------------------------------------------------------

@pytest.mark.parametrize("section", sorted(_SECTION_KEYS))
def test_a_known_section_answers_with_that_section_alone(client, section):
    body = _get(client, f"?section={section}").get_json()

    assert set(body) == {section, "lang"}
    assert body[section] == _as_served(FACULTY_INFO["lt"][section])


def test_a_section_honours_the_language_too(client):
    body = _get(client, "?section=links&lang=en").get_json()

    assert body["lang"] == "en"
    assert body["links"] == _as_served(FACULTY_INFO["en"]["links"])


def test_an_unknown_section_is_refused_rather_than_answered_with_everything(client):
    response = _get(client, "?section=nonsense")

    assert response.status_code == 400
    assert response.get_json() == {"error": "Unknown section", "code": "unknown_section"}


def test_staff_is_not_a_section_because_the_scraper_folds_it_into_contacts(client):
    assert _get(client, "?section=staff").status_code == 400


def test_section_names_are_case_sensitive(client):
    assert _get(client, "?section=Contacts").status_code == 400


def test_the_additive_lang_key_is_not_addressable_as_a_section(client):
    assert _get(client, "?section=lang").status_code == 400


def test_the_additive_updated_at_key_is_not_addressable_as_a_section(client, db):
    _seed_section(db, "contacts", _contacts())

    assert _get(client, "?section=updatedAt").status_code == 400


def test_an_empty_section_returns_the_whole_handbook(client):
    body = _get(client, "?section=").get_json()

    assert set(body) == _SECTION_KEYS | {"lang"}


def test_a_refused_section_is_never_cached(client):
    response = _get(client, "?section=nope")

    assert response.headers["Cache-Control"] == "no-store"
    assert "ETag" not in response.headers


def test_general_contact_is_not_a_section_before_a_scrape_found_one(client):
    assert _get(client, "?section=general_contact").status_code == 400


def test_general_contact_becomes_a_section_once_a_scrape_found_one(client, db):
    _seed_section(db, "general_contact", _general())

    response = _get(client, "?section=general_contact")

    assert response.status_code == 200
    assert response.get_json()["general_contact"] == _general()


def test_a_dropped_general_contact_is_not_a_section_either(client, db):
    _seed_section(db, "general_contact", {})

    assert _get(client, "?section=general_contact").status_code == 400




# -----------------------------------------------------------
# The scraped overlay — contacts
# -----------------------------------------------------------

def test_a_full_scrape_replaces_the_curated_contacts(client, db):
    _seed_section(db, "contacts", _contacts(items=6))

    body = _get(client).get_json()

    assert body["contacts"] == _contacts(items=6)
    assert body["contacts"][0]["category"] != "Dekanatas"


def test_a_scrape_exactly_on_the_contact_floor_is_served(client, db):
    _seed_section(db, "contacts", _contacts(items=MIN_SCRAPED_CONTACT_ITEMS))

    assert _get(client).get_json()["contacts"] == _contacts(items=MIN_SCRAPED_CONTACT_ITEMS)


def test_a_scrape_one_item_under_the_contact_floor_keeps_the_curated_list(client, db):
    _seed_section(db, "contacts", _contacts(items=MIN_SCRAPED_CONTACT_ITEMS - 1))

    assert _get(client).get_json()["contacts"] == _as_served(FACULTY_INFO["lt"]["contacts"])


def test_a_half_scraped_page_never_hides_the_curated_contacts(client, db):
    _seed_section(db, "contacts", [{"category": "Dekanatas", "items": [{"name": "Vienas"}]}])

    assert _get(client).get_json()["contacts"] == _as_served(FACULTY_INFO["lt"]["contacts"])


def test_an_empty_contacts_list_keeps_the_curated_contacts(client, db):
    _seed_section(db, "contacts", [])

    assert _get(client).get_json()["contacts"] == _as_served(FACULTY_INFO["lt"]["contacts"])


def test_contacts_that_are_not_a_list_are_refused_instead_of_crashing_the_screen(client, db):
    _seed_section(db, "contacts", {"category": "Dekanatas", "items": _contacts()[0]["items"]})

    response = _get(client)

    assert response.status_code == 200
    assert response.get_json()["contacts"] == _as_served(FACULTY_INFO["lt"]["contacts"])


@pytest.mark.parametrize("blob", ["kontaktai", 7, True])
def test_a_scalar_contacts_blob_is_refused(client, db, blob):
    _seed_section(db, "contacts", blob)

    assert _get(client).get_json()["contacts"] == _as_served(FACULTY_INFO["lt"]["contacts"])


def test_a_null_contacts_blob_leaves_the_curated_list_alone(client, db):
    _seed_section(db, "contacts", None)

    assert _get(client).get_json()["contacts"] == _as_served(FACULTY_INFO["lt"]["contacts"])


def test_junk_entries_do_not_count_toward_the_contact_floor(client, db):
    blob = ["ne kategorija", 42, None, {"category": "Katedros", "items": "ne sarasas"},
            {"category": "Dekanatas"}, {"category": "Vienas", "items": [{"name": "A"}]}]
    _seed_section(db, "contacts", blob)

    assert _get(client).get_json()["contacts"] == _as_served(FACULTY_INFO["lt"]["contacts"])


def test_items_are_counted_across_every_category_toward_the_floor(client, db):
    blob = [{"category": "A", "items": [{"name": "a1"}, {"name": "a2"}, {"name": "a3"}]},
            {"category": "B", "items": [{"name": "b1"}, {"name": "b2"}]}]
    _seed_section(db, "contacts", blob)

    assert _get(client).get_json()["contacts"] == blob




# -----------------------------------------------------------
# The scraped overlay — programs and general_contact
# -----------------------------------------------------------

def test_a_full_scrape_replaces_the_curated_programs(client, db):
    _seed_section(db, "programs", _programs(count=4))

    assert _get(client).get_json()["programs"] == _programs(count=4)


def test_a_scrape_exactly_on_the_program_floor_is_served(client, db):
    _seed_section(db, "programs", _programs(count=MIN_SCRAPED_PROGRAMS))

    assert _get(client).get_json()["programs"] == _programs(count=MIN_SCRAPED_PROGRAMS)


def test_a_scrape_one_entry_under_the_program_floor_keeps_the_curated_list(client, db):
    _seed_section(db, "programs", _programs(count=MIN_SCRAPED_PROGRAMS - 1))

    assert _get(client).get_json()["programs"] == _as_served(FACULTY_INFO["lt"]["programs"])


def test_an_empty_programs_list_keeps_the_curated_list(client, db):
    _seed_section(db, "programs", [])

    assert _get(client).get_json()["programs"] == _as_served(FACULTY_INFO["lt"]["programs"])


def test_programs_that_are_not_a_list_are_refused(client, db):
    _seed_section(db, "programs", {"bakalauras": _programs()})

    assert _get(client).get_json()["programs"] == _as_served(FACULTY_INFO["lt"]["programs"])


def test_a_null_programs_blob_leaves_the_curated_list_alone(client, db):
    _seed_section(db, "programs", None)

    assert _get(client).get_json()["programs"] == _as_served(FACULTY_INFO["lt"]["programs"])


def test_the_program_floor_counts_entries_not_their_contents(client, db):
    blob = [{"name": "A"}, {"name": "B"}, {"name": "C"}]
    _seed_section(db, "programs", blob)

    assert _get(client).get_json()["programs"] == blob


@pytest.mark.contract
def test_a_scraped_general_contact_is_added_to_the_payload(client, db):
    _seed_section(db, "general_contact", _general())

    body = _get(client).get_json()

    assert body["general_contact"] == _general()
    assert set(body) == _SECTION_KEYS | {"lang", "updatedAt", "general_contact"}


def test_an_empty_general_contact_is_dropped(client, db):
    _seed_section(db, "general_contact", {})

    assert "general_contact" not in _get(client).get_json()


@pytest.mark.parametrize("blob", [[], ["knf@knf.vu.lt"], "knf@knf.vu.lt", 0])
def test_a_general_contact_that_is_not_an_object_is_dropped(client, db, blob):
    _seed_section(db, "general_contact", blob)

    assert "general_contact" not in _get(client).get_json()


def test_a_null_general_contact_is_dropped(client, db):
    _seed_section(db, "general_contact", None)

    assert "general_contact" not in _get(client).get_json()


def test_a_partial_general_contact_is_served_as_scraped(client, db):
    _seed_section(db, "general_contact", {"phone": "+370 37 422 523"})

    assert _get(client).get_json()["general_contact"] == {"phone": "+370 37 422 523"}


def test_all_three_scraped_sections_land_together(client, db):
    _seed_section(db, "contacts", _contacts())
    _seed_section(db, "programs", _programs())
    _seed_section(db, "general_contact", _general())

    body = _get(client).get_json()

    assert body["contacts"] == _contacts()
    assert body["programs"] == _programs()
    assert body["general_contact"] == _general()




# -----------------------------------------------------------
# What the overlay must never touch
# -----------------------------------------------------------

def test_links_hours_and_faq_stay_curated_through_a_scrape(client, db):
    _seed_section(db, "contacts", _contacts())
    _seed_section(db, "programs", _programs())

    body = _get(client).get_json()

    assert body["links"] == _as_served(FACULTY_INFO["lt"]["links"])
    assert body["hours"] == _as_served(FACULTY_INFO["lt"]["hours"])
    assert body["faq"] == _as_served(FACULTY_INFO["lt"]["faq"])


def test_a_stray_links_row_in_faculty_info_is_ignored(client, db):
    _seed_section(db, "links", [{"title": "Piktas", "url": "https://evil.example", "icon": "bug"}])

    assert _get(client).get_json()["links"] == _as_served(FACULTY_INFO["lt"]["links"])


def test_a_stray_section_name_never_reaches_the_payload(client, db):
    _seed_section(db, "staff", [{"name": "Destytojas"}])

    body = _get(client).get_json()

    assert "staff" not in body
    assert _get(client, "?section=staff").status_code == 400


def test_the_overlay_never_mutates_the_module_level_handbook(client, db):
    _seed_section(db, "contacts", _contacts())
    _seed_section(db, "programs", _programs())
    assert _get(client).get_json()["contacts"] == _contacts()

    db.execute("DELETE FROM faculty_info")
    db.commit()

    body = _get(client).get_json()
    assert body["contacts"] == _as_served(FACULTY_INFO["lt"]["contacts"])
    assert body["programs"] == _as_served(FACULTY_INFO["lt"]["programs"])
    assert "lang" not in FACULTY_INFO["lt"], "the response's additive key leaked into the handbook"




# -----------------------------------------------------------
# Freshness — stale, undecodable and unreadable rows
# -----------------------------------------------------------

def test_a_blob_older_than_the_age_limit_is_ignored(client, db):
    stamp = (datetime.now(timezone.utc) - timedelta(days=SCRAPED_MAX_AGE_DAYS + 1)).isoformat()
    _seed_section(db, "contacts", _contacts(), scraped_at=stamp)

    body = _get(client).get_json()

    assert body["contacts"] == _as_served(FACULTY_INFO["lt"]["contacts"])
    assert "updatedAt" not in body


def test_a_blob_exactly_on_the_age_limit_is_still_served(client, db):
    with time_machine.travel(_NOW, tick=False):
        stamp = (_NOW - timedelta(days=SCRAPED_MAX_AGE_DAYS)).isoformat()
        _seed_section(db, "contacts", _contacts(), scraped_at=stamp)

        body = _get(client).get_json()

    assert body["contacts"] == _contacts()
    assert body["updatedAt"] == stamp


def test_a_blob_one_second_past_the_age_limit_is_ignored(client, db):
    with time_machine.travel(_NOW, tick=False):
        stamp = (_NOW - timedelta(days=SCRAPED_MAX_AGE_DAYS, seconds=1)).isoformat()
        _seed_section(db, "contacts", _contacts(), scraped_at=stamp)

        body = _get(client).get_json()

    assert body["contacts"] == _as_served(FACULTY_INFO["lt"]["contacts"])


def test_a_stale_section_does_not_take_a_fresh_one_down_with_it(client, db):
    stale = (datetime.now(timezone.utc) - timedelta(days=SCRAPED_MAX_AGE_DAYS + 5)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()
    _seed_section(db, "contacts", _contacts(), scraped_at=stale)
    _seed_section(db, "programs", _programs(), scraped_at=fresh)

    body = _get(client).get_json()

    assert body["contacts"] == _as_served(FACULTY_INFO["lt"]["contacts"])
    assert body["programs"] == _programs()
    assert body["updatedAt"] == fresh


def test_a_row_that_does_not_hold_json_is_ignored(client, db):
    _seed_section(db, "contacts", raw="{ne visai json")

    body = _get(client).get_json()

    assert body["contacts"] == _as_served(FACULTY_INFO["lt"]["contacts"])
    assert "updatedAt" not in body


def test_an_empty_data_json_is_ignored(client, db):
    _seed_section(db, "contacts", raw="")

    assert _get(client).get_json()["contacts"] == _as_served(FACULTY_INFO["lt"]["contacts"])


def test_one_undecodable_row_does_not_hide_a_good_one(client, db):
    _seed_section(db, "contacts", _contacts())
    _seed_section(db, "programs", raw="<html>404</html>")

    body = _get(client).get_json()

    assert body["contacts"] == _contacts()
    assert body["programs"] == _as_served(FACULTY_INFO["lt"]["programs"])


@pytest.mark.parametrize("stamp", ["2026-08-29T10:00:00", "2026-08-29 10:00:00",
                                   "2026-08-29T10:00:00Z", "2026-08-29T10:00:00+00:00"])
def test_every_stored_timestamp_shape_is_understood(client, db, stamp):
    with time_machine.travel(_NOW, tick=False):
        _seed_section(db, "contacts", _contacts(), scraped_at=stamp)

        body = _get(client).get_json()

    assert body["contacts"] == _contacts()
    assert body["updatedAt"] == stamp


def test_an_unreadable_timestamp_fails_open_and_the_blob_is_served(client, db):
    _seed_section(db, "contacts", _contacts(), scraped_at="niekada")

    body = _get(client).get_json()

    assert body["contacts"] == _contacts()
    assert body["updatedAt"] == "niekada"


def test_a_blank_timestamp_serves_the_blob_without_an_updated_at(client, db):
    _seed_section(db, "contacts", _contacts(), scraped_at="")

    body = _get(client).get_json()

    assert body["contacts"] == _contacts()
    assert "updatedAt" not in body


def test_an_unreadable_faculty_info_table_falls_back_instead_of_500ing(client, db):
    db.execute("DROP TABLE faculty_info")
    db.commit()

    response = _get(client)

    assert response.status_code == 200
    assert response.get_json()["contacts"] == _as_served(FACULTY_INFO["lt"]["contacts"])
    assert "updatedAt" not in response.get_json()


def test_a_database_that_cannot_be_opened_falls_back_too(client, monkeypatch):
    def _refuse():
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr("app.info.routes.get_db", _refuse)

    response = _get(client)

    assert response.status_code == 200
    assert response.get_json()["contacts"] == _as_served(FACULTY_INFO["lt"]["contacts"])


def test_a_non_sqlite_failure_is_not_swallowed(client, monkeypatch, app):
    def _explode():
        raise MemoryError("out of memory")

    monkeypatch.setattr("app.info.routes.get_db", _explode)
    app.config["PROPAGATE_EXCEPTIONS"] = False

    assert _get(client).status_code == 500




# -----------------------------------------------------------
# updatedAt
# -----------------------------------------------------------

def test_updated_at_is_the_newest_stamp_behind_the_overlay(client, db):
    older = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    newer = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    _seed_section(db, "contacts", _contacts(), scraped_at=older)
    _seed_section(db, "programs", _programs(), scraped_at=newer)

    assert _get(client).get_json()["updatedAt"] == newer


def test_updated_at_ignores_the_order_the_rows_were_written_in(client, db):
    newer = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    older = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    _seed_section(db, "contacts", _contacts(), scraped_at=newer)
    _seed_section(db, "programs", _programs(), scraped_at=older)

    assert _get(client).get_json()["updatedAt"] == newer


def test_updated_at_survives_a_blob_that_failed_its_floor(client, db):
    stamp = datetime.now(timezone.utc).isoformat()
    _seed_section(db, "programs", _programs(count=1), scraped_at=stamp)

    body = _get(client).get_json()

    assert body["updatedAt"] == stamp
    assert body["programs"] == _as_served(FACULTY_INFO["lt"]["programs"])


def test_a_section_answer_carries_updated_at_too(client, db):
    stamp = datetime.now(timezone.utc).isoformat()
    _seed_section(db, "contacts", _contacts(), scraped_at=stamp)

    body = _get(client, "?section=contacts").get_json()

    assert set(body) == {"contacts", "lang", "updatedAt"}
    assert body["updatedAt"] == stamp




# -----------------------------------------------------------
# The language fallback — 'en' borrows the 'lt' scrape
#
# info_scraper.py only ever writes lang 'lt'. Without the
# fallback an English reader would be frozen on the hardcoded
# defaults forever while a Lithuanian one saw the live contacts,
# so these are the tests that stop the two datasets diverging.
# -----------------------------------------------------------

def test_english_borrows_the_lithuanian_scraped_contacts(client, db):
    _seed_section(db, "contacts", _contacts())

    body = _get(client, "?lang=en").get_json()

    assert body["lang"] == "en"
    assert body["contacts"] == _contacts()


def test_english_and_lithuanian_serve_the_same_scraped_dataset(client, db):
    stamp = datetime.now(timezone.utc).isoformat()
    _seed_section(db, "contacts", _contacts(), scraped_at=stamp)
    _seed_section(db, "programs", _programs(), scraped_at=stamp)
    _seed_section(db, "general_contact", _general(), scraped_at=stamp)

    lithuanian = _get(client).get_json()
    english = _get(client, "?lang=en").get_json()

    for section in ("contacts", "programs", "general_contact", "updatedAt"):
        assert english[section] == lithuanian[section], f"'{section}' diverged between languages"


def test_the_borrowed_scrape_leaves_the_english_links_hours_and_faq_alone(client, db):
    _seed_section(db, "contacts", _contacts())

    body = _get(client, "?lang=en").get_json()

    assert body["links"] == _as_served(FACULTY_INFO["en"]["links"])
    assert body["hours"] == _as_served(FACULTY_INFO["en"]["hours"])
    assert body["faq"] == _as_served(FACULTY_INFO["en"]["faq"])


def test_english_prefers_its_own_rows_when_it_has_them(client, db):
    _seed_section(db, "contacts", _contacts(category="Lietuviskai"), lang="lt")
    _seed_section(db, "contacts", _contacts(category="In English"), lang="en")

    assert _get(client, "?lang=en").get_json()["contacts"][0]["category"] == "In English"
    assert _get(client).get_json()["contacts"][0]["category"] == "Lietuviskai"


def test_english_falls_back_when_its_own_rows_are_all_stale(client, db):
    stale = (datetime.now(timezone.utc) - timedelta(days=SCRAPED_MAX_AGE_DAYS + 1)).isoformat()
    _seed_section(db, "contacts", _contacts(category="Sena anglu"), lang="en", scraped_at=stale)
    _seed_section(db, "contacts", _contacts(category="Sviezia lietuviu"), lang="lt")

    assert _get(client, "?lang=en").get_json()["contacts"][0]["category"] == "Sviezia lietuviu"


def test_english_falls_back_when_its_own_rows_do_not_decode(client, db):
    _seed_section(db, "contacts", raw="{sugadinta", lang="en")
    _seed_section(db, "contacts", _contacts(category="Sviezia lietuviu"), lang="lt")

    assert _get(client, "?lang=en").get_json()["contacts"][0]["category"] == "Sviezia lietuviu"


def test_english_carries_the_lithuanian_updated_at_when_it_borrows(client, db):
    stamp = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    _seed_section(db, "contacts", _contacts(), scraped_at=stamp)

    assert _get(client, "?lang=en").get_json()["updatedAt"] == stamp


def test_lithuanian_never_borrows_from_english(client, db):
    _seed_section(db, "contacts", _contacts(category="In English"), lang="en")
    _seed_section(db, "programs", _programs(prefix="English program"), lang="en")

    body = _get(client).get_json()

    assert body["contacts"] == _as_served(FACULTY_INFO["lt"]["contacts"])
    assert body["programs"] == _as_served(FACULTY_INFO["lt"]["programs"])
    assert "updatedAt" not in body


def test_a_borrowed_general_contact_reaches_the_english_screen(client, db):
    _seed_section(db, "general_contact", _general())

    assert _get(client, "?lang=en").get_json()["general_contact"] == _general()


def test_english_with_nothing_scraped_anywhere_is_the_plain_english_handbook(client, db):
    body = _get(client, "?lang=en").get_json()

    assert set(body) == _SECTION_KEYS | {"lang"}
    assert body["contacts"] == _as_served(FACULTY_INFO["en"]["contacts"])




# -----------------------------------------------------------
# Caching — weak ETag, max-age, 304
# -----------------------------------------------------------

def test_the_handbook_is_cacheable_for_a_day(client):
    response = _get(client)

    assert response.headers["Cache-Control"] == f"public, max-age={CACHE_MAX_AGE}"
    assert CACHE_MAX_AGE == 24 * 3600


def test_the_etag_is_weak(client):
    etag = _get(client).headers["ETag"]

    assert etag.startswith('W/"') and etag.endswith('"')


def test_the_etag_is_stable_between_two_identical_requests(client):
    assert _get(client).headers["ETag"] == _get(client).headers["ETag"]


def test_a_matching_if_none_match_answers_304_with_no_body(client):
    first = _get(client)

    second = _get(client, headers={"If-None-Match": first.headers["ETag"]})

    assert second.status_code == 304
    assert second.get_data() == b""
    assert second.headers["ETag"] == first.headers["ETag"]


def test_a_304_still_carries_the_caching_headers(client):
    etag = _get(client).headers["ETag"]

    response = _get(client, headers={"If-None-Match": etag})

    assert response.headers["Cache-Control"] == f"public, max-age={CACHE_MAX_AGE}"


def test_a_stale_etag_gets_the_whole_body_back(client):
    response = _get(client, headers={"If-None-Match": 'W/"deadbeef"'})

    assert response.status_code == 200
    assert response.get_json()["contacts"]


def test_a_strong_form_of_the_same_tag_is_accepted_as_a_match(client):
    weak = _get(client).headers["ETag"]

    response = _get(client, headers={"If-None-Match": weak.removeprefix("W/")})

    assert response.status_code == 304


def test_a_wildcard_if_none_match_answers_304(client):
    assert _get(client, headers={"If-None-Match": "*"}).status_code == 304


def test_the_two_languages_do_not_share_an_etag(client):
    assert _get(client, "?lang=lt").headers["ETag"] != _get(client, "?lang=en").headers["ETag"]


def test_a_section_does_not_share_the_full_handbook_etag(client):
    assert _get(client).headers["ETag"] != _get(client, "?section=contacts").headers["ETag"]


def test_two_sections_do_not_share_an_etag(client):
    assert _get(client, "?section=links").headers["ETag"] != _get(client, "?section=faq").headers["ETag"]


def test_an_english_etag_from_one_client_is_not_a_match_for_lithuanian(client):
    english = _get(client, "?lang=en").headers["ETag"]

    assert _get(client, "?lang=lt", headers={"If-None-Match": english}).status_code == 200


def test_a_new_scrape_moves_the_etag(client, db):
    _seed_section(db, "contacts", _contacts(), scraped_at="2026-08-01T10:00:00")
    before = _get(client).headers["ETag"]

    db.execute("UPDATE faculty_info SET scraped_at = '2026-08-02T10:00:00'")
    db.commit()

    after = _get(client).headers["ETag"]
    assert after != before
    assert _get(client, headers={"If-None-Match": before}).status_code == 200


def test_a_relaunch_between_two_scrapes_is_a_304(client, db):
    _seed_section(db, "contacts", _contacts(), scraped_at="2026-08-01T10:00:00")
    etag = _get(client).headers["ETag"]

    assert _get(client, headers={"If-None-Match": etag}).status_code == 304


def test_a_head_request_carries_the_etag_and_no_body(client):
    response = client.head("/api/info")

    assert response.status_code == 200
    assert response.get_data() == b""
    assert response.headers["ETag"]


def test_the_route_answers_get_only(client):
    for call in (client.post, client.put, client.delete, client.patch):
        assert call("/api/info").status_code == 405


def test_the_trailing_slash_is_not_the_info_route(client):
    assert _get(client, "/").status_code == 404


def test_the_cacheable_handbook_does_not_also_claim_pragma_no_cache(client):
    response = _get(client)

    assert response.headers["Cache-Control"].startswith("public")
    assert "Pragma" not in response.headers




# -----------------------------------------------------------
# The once-per-process warnings
#
# /api/info is public and hot and every condition warned about
# here LASTS, so a plain logger.warning would write one line per
# request. These assert the muting, not the wording.
# -----------------------------------------------------------

def _info_warnings(caplog):
    return [r for r in caplog.records if r.name == "app.info.routes" and r.levelno == logging.WARNING]


def test_an_empty_faculty_info_table_warns_once_not_once_per_request(client, caplog):
    with caplog.at_level(logging.WARNING, logger="app.info.routes"):
        for _ in range(5):
            _get(client)

    assert len(_info_warnings(caplog)) == 1


def test_a_rejected_contacts_shape_warns_once_and_names_the_type(client, db, caplog):
    _seed_section(db, "contacts", {"category": "Dekanatas"})

    with caplog.at_level(logging.WARNING, logger="app.info.routes"):
        _get(client)
        _get(client)

    warnings = _info_warnings(caplog)
    assert len(warnings) == 1
    assert "dict" in warnings[0].getMessage()


def test_a_rejected_programs_floor_warns_once(client, db, caplog):
    _seed_section(db, "programs", _programs(count=1))

    with caplog.at_level(logging.WARNING, logger="app.info.routes"):
        _get(client)
        _get(client)

    assert len(_info_warnings(caplog)) == 1


def test_a_rejected_general_contact_warns_once(client, db, caplog):
    _seed_section(db, "general_contact", [])

    with caplog.at_level(logging.WARNING, logger="app.info.routes"):
        _get(client)
        _get(client)

    assert len(_info_warnings(caplog)) == 1


def test_a_stale_row_and_an_undecodable_row_warn_separately(client, db, caplog):
    stale = (datetime.now(timezone.utc) - timedelta(days=SCRAPED_MAX_AGE_DAYS + 1)).isoformat()
    _seed_section(db, "contacts", _contacts(), scraped_at=stale)
    _seed_section(db, "programs", raw="{")

    with caplog.at_level(logging.WARNING, logger="app.info.routes"):
        _get(client)
        _get(client)

    assert len(_info_warnings(caplog)) == 2


def test_an_unreadable_table_logs_the_failure_with_its_traceback(client, db, caplog):
    db.execute("DROP TABLE faculty_info")
    db.commit()

    with caplog.at_level(logging.ERROR, logger="app.info.routes"):
        _get(client)

    errors = [r for r in caplog.records if r.levelno == logging.ERROR and r.exc_info]
    assert errors, "a broken faculty_info lookup must not fail silently"


def test_warn_once_stays_quiet_after_the_first_time(caplog):
    with caplog.at_level(logging.WARNING, logger="app.info.routes"):
        _warn_once("test-key", "kartojasi %s", "vienas")
        _warn_once("test-key", "kartojasi %s", "du")
        _warn_once("kitas-raktas", "kitas")

    messages = [r.getMessage() for r in _info_warnings(caplog)]
    assert messages == ["kartojasi vienas", "kitas"]




# -----------------------------------------------------------
# _parse_timestamp — the shapes the route cannot reach through
# the database (the column is NOT NULL and holds text only)
# -----------------------------------------------------------

@pytest.mark.parametrize("value", [None, "", 0, [], {}])
def test_an_empty_timestamp_parses_to_nothing(value):
    assert _parse_timestamp(value) is None


@pytest.mark.parametrize("value", ["niekada", "2026-13-45T99:00:00", "šiandien", 20260829, 3.5])
def test_an_unparseable_timestamp_parses_to_nothing(value):
    assert _parse_timestamp(value) is None


def test_a_naive_timestamp_is_read_as_utc():
    assert _parse_timestamp("2026-08-29T10:00:00") == datetime(2026, 8, 29, 10, tzinfo=timezone.utc)


def test_the_legacy_space_form_and_the_zulu_form_agree():
    assert _parse_timestamp("2026-08-29 10:00:00") == _parse_timestamp("2026-08-29T10:00:00Z")


def test_an_offset_timestamp_keeps_its_offset():
    parsed = _parse_timestamp("2026-08-29T13:00:00+03:00")

    assert parsed == datetime(2026, 8, 29, 10, tzinfo=timezone.utc)
