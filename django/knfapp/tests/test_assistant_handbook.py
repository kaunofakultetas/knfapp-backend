############################################################
#  [*] Regression tests — the handbook is ONE merge
#
#  The assistant's knowledge base and GET /api/info are
#  built from the same effective handbook: the English
#  corpus borrows the 'lt' overlay exactly as ?lang=en
#  does (the Lithuanian programme names on both surfaces
#  is the intended parity), a partial scrape under the
#  size floors never replaces a full chunk (the Info
#  screen already refused it), and the programme lists the
#  two surfaces show agree in both languages.
############################################################


import uuid


from django.test import Client, TestCase


from knfapp.assistant.chunking import handbook_chunks
from knfapp.common.timestamps import utc_now_iso
from knfapp.info.models import FacultyInfo


def _overlay_row(section, data, lang="lt"):
    # data_json is a JSON column — the structure goes in as-is
    return FacultyInfo.objects.create(
        id=str(uuid.uuid4()), lang=lang, section=section,
        data_json=data, scraped_at=utc_now_iso(),
    )


def _programs(count):
    return [{"name": f"Programa {index}", "degree": "Bakalauras", "duration": "4 metai"}
            for index in range(count)]


def _chunk_text(lang, section):
    # Every chunk of one handbook unit, joined — the unit is
    # what the indexer retires and re-embeds as a whole
    return "\n".join(chunk["text"] for chunk in handbook_chunks()
                     if chunk["source"] == "handbook" and chunk["source_id"] == f"{lang}-{section}")


class EffectiveHandbookTests(TestCase):

    def setUp(self):
        self.client = Client()
        # warn_once state is process-wide — isolate per test
        from knfapp.info.api import views
        views._warned.clear()

    def test_the_english_corpus_borrows_the_lt_overlay_like_the_info_route(self):
        _overlay_row("programs", _programs(6))
        _overlay_row("general_contact", {"phone": "+370 37 422523", "email": "knf@knf.vu.lt"})

        # The English rows carry the scraped programmes — the
        # curated five placeholders are gone, exactly as on
        # /api/info?lang=en
        en_programs = _chunk_text("en", "programs")
        self.assertIn("Programa 5", en_programs)
        self.assertNotIn("Informatics and Digital Content", en_programs)
        # ...and the scraped contact block reaches the English base
        self.assertIn("+370 37 422523", _chunk_text("en", "general_contact"))

    def test_a_partial_scrape_under_the_floor_never_replaces_a_full_chunk(self):
        # A one-entry programmes blob and a one-item contacts blob:
        # the Info screen keeps its curated lists, and so must the
        # corpus — a stub chunk would take a NEW content-derived
        # id and the full chunk would be retired under the old one
        _overlay_row("programs", [{"name": "Tik viena", "degree": "Bakalauras"}])
        _overlay_row("contacts", [{"category": "Dekanatas",
                                   "items": [{"name": "Vardenis", "phone": "+370 1"}]}])

        lt_programs = _chunk_text("lt", "programs")
        self.assertNotIn("Tik viena", lt_programs)
        self.assertEqual(len(lt_programs.strip().splitlines()), 5)   # the curated five
        self.assertNotIn("Vardenis", _chunk_text("lt", "contacts"))

    def test_the_info_route_and_the_corpus_agree_in_both_languages(self):
        _overlay_row("programs", _programs(4))
        for lang in ("lt", "en"):
            served = self.client.get(f"/api/info?lang={lang}").json()["programs"]
            corpus = _chunk_text(lang, "programs")
            self.assertEqual(len(served), 4, lang)
            for program in served:
                self.assertIn(program["name"], corpus, lang)
