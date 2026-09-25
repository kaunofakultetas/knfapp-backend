############################################################
#  [*] Regression tests — admission documents are not
#      study programmes (KNF-077)
#
#  The bachelor listing links "VU priėmimo taisyklės" and
#  "LAMA BPO bendrojo priėmimo tvarka" under the programme
#  URL shape; the scraper stored them as bachelor degrees
#  and GET /api/info served them. Pinned at all three
#  layers: the one name rule, the scraper's link pass, and
#  the served list (rows stored before the scraper's filter
#  are cleaned where they are served).
############################################################


from bs4 import BeautifulSoup
from django.test import Client, TestCase, SimpleTestCase

from knfapp.info.api import views
from knfapp.info.programs import is_program_name
from knfapp.scraper.info_scraper import _scrape_programs
from .test_schedule_info import _overlay_row


LISTING = """<div class="article-content">
  <a href="/stojantiesiems/bakalauro-studijos/finansu-analitika">Finansų analitika</a>
  <a href="/stojantiesiems/bakalauro-studijos/ekonomika-ir-vadyba">Ekonomika ir vadyba</a>
  <a href="/stojantiesiems/bakalauro-studijos/marketingo-technologijos">Marketingo technologijos</a>
  <a href="https://www.vu.lt/studijos/stojantiesiems/bakalauro-studijos">VU priėmimo taisyklės</a>
  <a href="https://lamabpo.lt/pirmosios-pakopos-ir-vientisosios-studijos/priemimo">LAMA BPO bendrojo priėmimo tvarka</a>
</div>"""


class ProgramNameTests(SimpleTestCase):

    def test_admission_vocabulary_is_not_a_programme(self):
        for name in ("VU priėmimo taisyklės", "LAMA BPO bendrojo priėmimo tvarka",
                     "Priėmimo tvarka 2027", "Studijų taisyklės"):
            self.assertFalse(is_program_name(name), name)

    def test_real_programme_names_pass(self):
        for name in ("Finansų analitika", "Tvariųjų finansų ekonomika", "Tvariųjų finansų ekonomika (anglų k.)",
                     "Lietuvių filologija ir reklama: kūrybos studijos", "Finansų technologijos (FinTech)",
                     "Viešojo diskurso lingvistika: medijų retorika ir komunikacija"):
            self.assertTrue(is_program_name(name), name)
        self.assertFalse(is_program_name(""))
        self.assertFalse(is_program_name(None))

    def test_the_link_pass_skips_the_admission_documents(self):
        names = [entry["name"] for entry in _scrape_programs(BeautifulSoup(LISTING, "html.parser"), None)]
        self.assertEqual(names, ["Finansų analitika", "Ekonomika ir vadyba", "Marketingo technologijos"])


class ServedProgramsTests(TestCase):

    def setUp(self):
        views._warned.clear()

    def test_rows_stored_before_the_filter_are_cleaned_where_served(self):
        _overlay_row("programs", [
            {"name": "Finansų analitika", "degree": "Bakalauras"},
            {"name": "Ekonomika ir vadyba", "degree": "Bakalauras"},
            {"name": "Marketingo technologijos", "degree": "Bakalauras"},
            {"name": "VU priėmimo taisyklės", "degree": "Bakalauras"},
            {"name": "LAMA BPO bendrojo priėmimo tvarka", "degree": "Bakalauras"},
        ])
        for lang in ("lt", "en"):
            served = Client().get(f"/api/info?lang={lang}&section=programs").json()["programs"]
            names = [program["name"] for program in served]
            self.assertEqual(names, ["Finansų analitika", "Ekonomika ir vadyba", "Marketingo technologijos"], lang)
