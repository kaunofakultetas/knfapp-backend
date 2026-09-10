############################################################
#  [*] Regression tests — scraper pure logic
#
#  The decisions the parsing layer must not lose: the
#  canonical URL shape the dedup key hangs on, the image-src
#  gates that keep an injected src from becoming a beacon,
#  the published_at clamp that keeps a mis-parsed year off
#  the top of the feed, the SSRF host gate, the Lithuanian
#  plural table, the ORDERED programme table with its -EN
#  trap, and the semester ordering that plain text sorting
#  gets wrong. No network anywhere — these are pure
#  functions.
############################################################


from datetime import datetime, timedelta, timezone


from django.test import SimpleTestCase


from knfapp.scraper import common, schedule_scraper
from knfapp.scraper.plurals import lt_plural


class UrlHygieneTests(SimpleTestCase):

    def test_the_dedup_key_collapses_every_variant_to_one_shape(self):
        variants = (
            "http://www.knf.vu.lt/aktualijos/naujiena-1/",
            "https://knf.vu.lt/aktualijos/naujiena-1?utm_source=fb&fbclid=xyz",
            "https://knf.vu.lt/aktualijos/naujiena-1#comments",
        )
        for url in variants:
            self.assertEqual(common.normalise_url(url), "https://knf.vu.lt/aktualijos/naujiena-1", url)
        # A meaningful query survives; only the campaign tags go
        self.assertEqual(common.normalise_url("https://knf.vu.lt/a?page=2&utm_medium=qr"),
                         "https://knf.vu.lt/a?page=2")
        # Unparsable / relative come back stripped, never dropped
        self.assertEqual(common.normalise_url("  /aktualijos/x  "), "/aktualijos/x")

    def test_the_host_gate_refuses_what_a_page_could_inject(self):
        for url in ("https://169.254.169.254/latest/meta-data",
                    "file:///etc/passwd",
                    "javascript:alert(1)",
                    "https://evil.example/knf.vu.lt"):
            self.assertFalse(common.host_allowed(url, common.KNF_HOSTS), url)
        self.assertTrue(common.host_allowed("https://www.knf.vu.lt/aktualijos", common.KNF_HOSTS))

    def test_image_srcs_resolve_but_never_leave_the_allowlist(self):
        page = "https://knf.vu.lt/aktualijos/naujiena-1"
        # Protocol-relative and bare relative become absolute here
        self.assertEqual(common.validate_image_url(page, "//newshub.vu.lt/img/a.jpg"),
                         "https://newshub.vu.lt/img/a.jpg")
        self.assertEqual(common.validate_image_url(page, "images/x.jpg"),
                         "https://knf.vu.lt/aktualijos/images/x.jpg")
        # An injected off-allowlist src must not become a beacon
        self.assertIsNone(common.validate_image_url(page, "https://tracker.example/p.gif"))
        # The lazy-load placeholder shapes all urljoin back to the
        # article page — every one dies before that happens
        for src in ("", "   ", "#", "?v=2", "//", "https://", "https:"):
            self.assertIsNone(common.validate_image_url(page, src), repr(src))
        self.assertIsNone(common.validate_image_url(page, "data:image/gif;base64,R0lGOD"))


class PublishedAtTests(SimpleTestCase):

    def test_the_offset_is_applied_not_dropped_and_the_answer_is_aware(self):
        vilnius = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=3))) - timedelta(days=1)
        stored = common.sanitise_published_at(common.parse_source_datetime(vilnius.isoformat()))
        # One stamp KIND for the whole published_at column — an
        # aware UTC datetime, the same thing member posts write
        self.assertEqual(stored.utcoffset(), timedelta(0))
        self.assertAlmostEqual(stored, vilnius.astimezone(timezone.utc),
                               delta=timedelta(seconds=2))

    def test_out_of_range_stamps_fall_back_to_now(self):
        now = datetime.now(timezone.utc)
        for raw in ("2031-01-01T10:00:00", "1999-05-05T10:00:00",
                    "9999-12-31T23:59:59-05:00"):  # the OverflowError shape
            stored = common.sanitise_published_at(common.parse_source_datetime(raw))
            self.assertEqual(stored.utcoffset(), timedelta(0))
            self.assertAlmostEqual(stored, now, delta=timedelta(seconds=5), msg=raw)
        # Unparsable → now as well, via the None path
        self.assertIsNone(common.parse_source_datetime("rytoj"))


class LithuanianPluralTests(SimpleTestCase):

    def test_the_three_cardinal_forms(self):
        forms = ("naujas straipsnis", "nauji straipsniai", "naujų straipsnių")
        self.assertEqual(lt_plural(1, forms), forms[0])
        self.assertEqual(lt_plural(21, forms), forms[0])   # singular again
        self.assertEqual(lt_plural(2, forms), forms[1])
        self.assertEqual(lt_plural(23, forms), forms[1])
        self.assertEqual(lt_plural(10, forms), forms[2])   # genitive
        self.assertEqual(lt_plural(11, forms), forms[2])
        self.assertEqual(lt_plural(111, forms), forms[2])  # 111 is 11-shaped, not 1-shaped


class GroupNameTests(SimpleTestCase):

    def test_specific_programmes_beat_the_broader_ones_they_contain(self):
        name = schedule_scraper._parse_group_display_name(
            "lfr-tkr-2k", "Turinio kūrimas ir rinkodara (Lietuvių filologija ir reklama) - 2 kursas")
        self.assertEqual(name, "LFR-TKR-2")

    def test_the_english_programme_is_not_english_taught(self):
        # "Anglų ir kita užsienio kalba" carries "angl" in its own
        # name — only an explicit marker earns the -EN suffix
        name = schedule_scraper._parse_group_display_name(
            "akuk-1k", "Anglų ir kita užsienio kalba - 1 kursas")
        self.assertEqual(name, "AKUK-1")
        name = schedule_scraper._parse_group_display_name(
            "ev-1k-en", "Ekonomika ir vadyba (anglų kalba) - 1 kursas")
        self.assertEqual(name, "EV-EN-1")

    def test_a_courseless_parse_keeps_the_unique_slug(self):
        # Emitting bare "EV" would pool four years into one timetable
        name = schedule_scraper._parse_group_display_name("ev-nezinomas", "Ekonomika ir vadyba")
        self.assertEqual(name, "ev-nezinomas")
        # …while the two genuinely course-less pools keep their name
        name = schedule_scraper._parse_group_display_name("bus-x", "Bendrųjų universitetinių studijų dalykai")
        self.assertEqual(name, "BUS")

    def test_masters_groups_carry_the_level_suffix(self):
        name = schedule_scraper._parse_group_display_name(
            "mv-mag-1k", "Meno vadyba, magistrantūra - 1 kursas")
        self.assertEqual(name, "MV-M-1")


class SemesterTests(SimpleTestCase):

    def test_labels_key_on_the_academic_years_first_calendar_year(self):
        self.assertEqual(schedule_scraper._get_semester_label(datetime(2026, 2, 9)), "2025-P")
        self.assertEqual(schedule_scraper._get_semester_label(datetime(2026, 9, 8)), "2026-R")
        self.assertEqual(schedule_scraper._get_semester_label(datetime(2026, 1, 5)), "2025-P")

    def test_spring_sorts_after_its_autumn_despite_the_text_order(self):
        self.assertGreater(schedule_scraper._semester_key("2025-P"),
                           schedule_scraper._semester_key("2025-R"))
        # An off-grammar label keys None — tolerated, never purged
        self.assertIsNone(schedule_scraper._semester_key("2025-pavasaris"))


class RetakeTests(SimpleTestCase):

    def test_every_colour_notation_collapses_to_one_value(self):
        for raw in ("#FF899D", "#ff899d", "rgb(255, 137, 157)"):
            self.assertEqual(schedule_scraper._normalise_colour(raw), "#ff899d", raw)
        self.assertEqual(schedule_scraper._normalise_colour("#f9d"), "#ff99dd")

    def test_a_labelled_retake_is_one_whatever_it_is_painted(self):
        self.assertTrue(schedule_scraper._labelled_retake(
            {"title": "Programavimas (PERLAIKYMAS)"}))
        self.assertFalse(schedule_scraper._labelled_retake({"title": "Programavimas"}))






class ArticleMarkdownTests(SimpleTestCase):

    def _markdown(self, html):
        from bs4 import BeautifulSoup
        el = BeautifulSoup(html, "lxml").select_one("div")
        return common.element_to_markdown(el, "https://knf.vu.lt/aktualijos/x")

    def test_paragraphs_survive_and_inline_tags_stop_chopping_sentences(self):
        md = self._markdown(
            "<div><p>Stojantieji į <a href='/studijos/mag'>magistrantūros programas</a> "
            "vis dar gali .</p><p>Antra pastraipa.</p></div>")
        self.assertEqual(md.split("\n\n"), [
            "Stojantieji į [magistrantūros programas](https://knf.vu.lt/studijos/mag) vis dar gali.",
            "Antra pastraipa.",
        ])

    def test_headings_lists_bold_and_loose_br_text_become_blocks(self):
        md = self._markdown(
            "<div><h2>Antraštė</h2><p><b>Svarbu</b>: taip.</p>"
            "<ul><li>Vienas</li><li>Du</li></ul>"
            "Palaida eilutė<br>ir dar viena<script>x()</script></div>")
        self.assertEqual(md.split("\n\n"), [
            "## Antraštė",
            "**Svarbu**: taip.",
            "- Vienas\n- Du",
            "Palaida eilutė",
            "ir dar viena",
        ])

    def test_a_link_off_https_or_with_nested_markup_stays_prose(self):
        md = self._markdown(
            "<div><p><a href='javascript:alert(1)'>blogas</a> ir "
            "<a href='/geras'>geras <b>storas</b></a></p></div>")
        self.assertEqual(md, "blogas ir [geras storas](https://knf.vu.lt/geras)")

    def test_markdown_to_plain_leaves_prose_only(self):
        plain = common.markdown_to_plain("## A\n\n[t](https://x.lt) ir **b** ir *k*\n\n- vienas")
        self.assertEqual(plain, "A\n\nt ir b ir k\n\nvienas")

    def test_cap_markdown_cuts_at_a_paragraph_boundary(self):
        text = "Pirma pastraipa.\n\nAntra pastraipa."
        self.assertEqual(common.cap_markdown(text, len(text) - 1), "Pirma pastraipa.")
        self.assertEqual(common.cap_markdown(text, len(text)), text)
