############################################################
#  [*] Regression tests — the Info handbook per language
#
#  KNF-124 / KNF-127: GET /api/info?lang=en borrowed the
#  Lithuanian scrape wholesale — 25 programme cards under
#  "Bakalauras" chips, served as lang "en" — and the
#  scraped items carried no duration while the client
#  contract called it required. Pinned here:
#    - a borrowed overlay translates the closed
#      vocabularies (degree words, "N metai" durations, the
#      "(anglų k.)" marker) and flags every entry whose
#      prose stays Lithuanian with nameLang "lt";
#    - the Lithuanian answer is left exactly as scraped;
#    - the programme item floor drops malformed entries
#      BEFORE the size floor counts them, and a missing or
#      blank duration is omitted, never served empty.
############################################################


import uuid


from django.test import Client, TestCase


from knfapp.common.timestamps import utc_now_iso
from knfapp.info.models import FacultyInfo


def _overlay_row(section, data, lang="lt"):
    # data_json is a JSON column — the structure goes in as-is
    return FacultyInfo.objects.create(
        id=str(uuid.uuid4()), lang=lang, section=section,
        data_json=data, scraped_at=utc_now_iso(),
    )


# What the 'lt' scraper writes: the registered name, the page's
# degree word, and a duration only when the card stated one
SCRAPED_PROGRAMS = [
    {"name": "Audiovizualinis vertimas", "degree": "Bakalauras", "duration": "4 metai"},
    {"name": "Kalba ir dirbtinio intelekto valdymas (anglų k.)", "degree": "Magistras",
     "duration": "1,5 metai"},
    {"name": "Verslo informatika", "degree": "Bakalauras"},
    {"name": "Finansų technologijos", "degree": "Magistras", "duration": "1 m."},
]


class InfoLanguageTests(TestCase):

    def setUp(self):
        self.client = Client()
        # warn_once state is process-wide — isolate per test
        from knfapp.info.api import views
        views._warned.clear()

    def _programs(self, lang):
        body = self.client.get(f"/api/info?lang={lang}").json()
        self.assertEqual(body["lang"], lang)
        return body["programs"]


    def test_english_translates_the_closed_vocabularies_and_flags_the_names(self):
        _overlay_row("programs", SCRAPED_PROGRAMS)
        programs = self._programs("en")

        self.assertEqual(programs[0], {"name": "Audiovizualinis vertimas", "degree": "Bachelor's",
                                       "duration": "4 years", "nameLang": "lt"})
        # The English-taught marker leaves the name as an English note
        self.assertEqual(programs[1], {"name": "Kalba ir dirbtinio intelekto valdymas", "degree": "Master's",
                                       "duration": "1.5 years", "note": "Taught in English", "nameLang": "lt"})
        # No duration scraped — none served, not an empty line
        self.assertEqual(programs[2], {"name": "Verslo informatika", "degree": "Bachelor's", "nameLang": "lt"})
        self.assertEqual(programs[3]["duration"], "1 year")
        # Not one Lithuanian degree word left on the English screen
        self.assertFalse({"Bakalauras", "Magistras"} & {program["degree"] for program in programs})

    def test_the_lithuanian_answer_is_served_exactly_as_scraped(self):
        _overlay_row("programs", SCRAPED_PROGRAMS)
        programs = self._programs("lt")
        self.assertEqual(programs[1]["name"], "Kalba ir dirbtinio intelekto valdymas (anglų k.)")
        self.assertEqual(programs[0]["degree"], "Bakalauras")
        self.assertEqual(programs[0]["duration"], "4 metai")
        self.assertNotIn("nameLang", programs[0])
        self.assertNotIn("duration", programs[2])

    def test_an_untranslatable_duration_never_poses_as_english(self):
        _overlay_row("programs", [
            {"name": "Programa A", "degree": "Bakalauras", "duration": "ketveri metai su praktika"},
            {"name": "Programa B", "degree": "Bakalauras"},
            {"name": "Programa C", "degree": "Bakalauras"},
        ])
        programs = self._programs("en")
        self.assertNotIn("duration", programs[0])
        # ...while Lithuanian keeps its own prose
        self.assertEqual(self._programs("lt")[0]["duration"], "ketveri metai su praktika")

    def test_malformed_items_are_dropped_before_the_size_floor_counts(self):
        # Two usable entries hidden among junk: the floor of three
        # is judged on the usable ones, so the curated list stands
        _overlay_row("programs", [
            {"name": "Tikra programa", "degree": "Bakalauras"},
            {"name": "Kita programa", "degree": "Magistras", "duration": "   "},
            {"name": "", "degree": "Bakalauras"},
            {"name": "Be laipsnio"},
            "ne objektas",
            None,
        ])
        self.assertEqual(len(self._programs("lt")), 5)   # the curated five

        # Enough usable ones: served, the junk dropped and the
        # blank duration omitted instead of an empty line
        FacultyInfo.objects.filter(section="programs").delete()
        _overlay_row("programs", [
            {"name": "Tikra programa", "degree": "Bakalauras"},
            {"name": "Kita programa", "degree": "Magistras", "duration": "   "},
            {"name": "Trečia programa", "degree": "Bakalauras"},
            {"name": "Be laipsnio"},
        ])
        programs = self._programs("lt")
        self.assertEqual([program["name"] for program in programs],
                         ["Tikra programa", "Kita programa", "Trečia programa"])
        self.assertNotIn("duration", programs[1])

    def test_borrowed_contact_groups_are_flagged_and_the_general_block_is_neutral(self):
        _overlay_row("contacts", [{"category": "Dekanatas",
                                   "items": [{"name": f"Kabinetas {i}", "room": str(100 + i)} for i in range(6)]}])
        _overlay_row("general_contact", {"address": "Muitinės g. 8, LT-44280 Kaunas",
                                         "phone": "+370 37 422 523", "email": "knf@knf.vu.lt"})
        body = self.client.get("/api/info?lang=en").json()
        self.assertEqual(body["contacts"][0]["nameLang"], "lt")
        self.assertEqual(len(body["contacts"][0]["items"]), 6)
        self.assertEqual(body["general_contact"]["phone"], "+370 37 422 523")
        self.assertNotIn("nameLang", body["general_contact"])

        lt = self.client.get("/api/info?lang=lt").json()
        self.assertNotIn("nameLang", lt["contacts"][0])
