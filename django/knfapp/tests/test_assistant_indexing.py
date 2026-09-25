############################################################
#  [*] Regression tests — the knowledge-base indexing
#
#  The chunkers' contracts: FAQ entries chunk one per
#  question with the question as the title, news bodies
#  strip their HTML, long texts split on paragraph seams
#  with the overlap carried, ids stay unique, and the
#  hash moves only when meaning does. The indexer command:
#  first run embeds everything, an unchanged second run
#  embeds nothing but stamps sightings, an edit re-embeds
#  exactly the touched chunk, a vanished source retires
#  its rows, a dead gateway aborts BEFORE any delete, and a
#  run that would retire over a quarter of the corpus — a
#  source gone silently empty — retires NOTHING unless the
#  operator allows it (KNF-051). The scraped general_contact
#  block is titled for humans, never by its code key
#  (KNF-150).
#
#  The gateway client itself: batching, order, the typed
#  failure — all through the injected `post` seam.
############################################################


import json
from types import SimpleNamespace


from django.test import TestCase


from knfapp.assistant import chunking, gateway
from knfapp.assistant.chunking import build_corpus, chunk_hash, news_chunks, split_text
from knfapp.assistant.gateway import GatewayError, embed_texts
from knfapp.assistant.models import SupportChunk
from django.core.management import call_command
from .utils import create_post


def _fake_vector(seed=1.0):
    return [seed] * 1536


class ChunkingTests(TestCase):

    def test_faq_chunks_one_per_question_titled_by_it(self):
        faq = [chunk for chunk in chunking.handbook_chunks()
               if chunk["language"] == "lt" and chunk["section"] == "faq"]
        self.assertGreaterEqual(len(faq), 5)
        wifi = [chunk for chunk in faq if "eduroam" in chunk["text"]]
        self.assertEqual(len(wifi), 1)
        self.assertTrue(wifi[0]["title"].startswith("Kaip prisijungti prie VU Wi-Fi"))

    def test_news_chunks_strip_html_and_point_back(self):
        post = create_post(title="Nauja laboratorija",
                           content="<p>Atidaryta <b>nauja</b> laboratorija.</p>")
        chunks = news_chunks()
        mine = [chunk for chunk in chunks if chunk["source_id"] == str(post.id)]
        self.assertEqual(len(mine), 1)
        self.assertIn("Atidaryta nauja laboratorija.", mine[0]["text"])
        self.assertNotIn("<b>", mine[0]["text"])
        self.assertEqual(mine[0]["source"], "news")

    def test_private_posts_stay_out_of_the_corpus(self):
        create_post(title="Slapta", is_public=0)
        self.assertEqual([chunk for chunk in news_chunks() if chunk["title"] == "Slapta"], [])

    def test_split_text_overlaps_and_short_text_passes_through(self):
        self.assertEqual(split_text("trumpas"), ["trumpas"])
        paragraphs = "\n\n".join(f"Pastraipa {index}. " + "žodis " * 80 for index in range(8))
        pieces = split_text(paragraphs)
        self.assertGreater(len(pieces), 1)
        for piece in pieces:
            self.assertLessEqual(len(piece), chunking.MAX_CHUNK_CHARS + chunking.OVERLAP_CHARS + 2)
        # The seam carries context: the second window repeats the
        # first window's tail
        self.assertIn(pieces[0][-40:].strip()[:20], pieces[1])

    def test_curated_extras_join_the_corpus_one_chunk_per_question(self):
        curated = [chunk for chunk in build_corpus() if chunk["source"] == "curated"]
        self.assertGreaterEqual(len(curated), 4)
        lt = [chunk for chunk in curated if chunk["language"] == "lt"]
        self.assertTrue(all(chunk["section"] == "faq" for chunk in curated))
        self.assertTrue(any(chunk["title"].startswith("Kam skirtas") for chunk in lt))

    def test_corpus_ids_are_unique_and_hash_moves_with_meaning(self):
        corpus = build_corpus()
        self.assertEqual(len({chunk["id"] for chunk in corpus}), len(corpus))

        chunk = dict(corpus[0])
        original = chunk_hash(chunk)
        self.assertEqual(chunk_hash(dict(chunk)), original)
        chunk["text"] += " pakeista"
        self.assertNotEqual(chunk_hash(chunk), original)


class GatewayClientTests(TestCase):

    def _ok_post(self, calls=None):
        def post(url, json=None, headers=None, timeout=None):
            if calls is not None:
                calls.append(json)
            data = [{"index": position, "embedding": _fake_vector(position)}
                    for position in range(len(json["input"]))]
            return SimpleNamespace(status_code=200, json=lambda: {"data": data}, text="")
        return post

    def test_embed_texts_batches_and_preserves_order(self):
        calls = []
        # The model name is pinned, not inherited — the live env
        # carries deployment-prefixed ids (azure/...)
        with self.settings(AI_GATEWAY_KEY="k", AI_EMBED_MODEL="text-embedding-3-small"):
            vectors = embed_texts([f"text {index}" for index in range(gateway.BATCH_SIZE + 3)],
                                  post=self._ok_post(calls))
        self.assertEqual(len(vectors), gateway.BATCH_SIZE + 3)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["model"], "text-embedding-3-small")
        self.assertEqual(calls[0]["dimensions"], 1536)

    def test_embed_texts_fails_typed(self):
        with self.settings(AI_GATEWAY_KEY=""):
            with self.assertRaises(GatewayError):
                embed_texts(["x"])
        with self.settings(AI_GATEWAY_KEY="k"):
            broken = lambda *args, **kwargs: SimpleNamespace(status_code=500, text="boom",
                                                             json=lambda: {})
            with self.assertRaises(GatewayError):
                embed_texts(["x"], post=broken)
        self.assertEqual(embed_texts([]), [])


class IndexerTests(TestCase):

    def setUp(self):
        # The command embeds through the gateway module — fake it
        # at that seam, counting what it was asked to embed
        self.embedded = []

        def fake_embed(texts, post=None):
            self.embedded.append(list(texts))
            return [_fake_vector() for _ in texts]

        # The seam moved with the shared sync — the command and
        # the admin button both run indexing.run_index
        from knfapp.assistant import indexing as indexing_module
        self._module = indexing_module
        self._real = indexing_module.embed_texts
        indexing_module.embed_texts = fake_embed
        self.addCleanup(lambda: setattr(indexing_module, "embed_texts", self._real))

    def _run(self, *args):
        call_command("index_support_corpus", *args)

    def test_first_run_embeds_everything_second_run_nothing(self):
        create_post(title="Naujiena viena")
        self._run()
        total = SupportChunk.objects.count()
        self.assertGreater(total, 0)
        self.assertEqual(sum(len(batch) for batch in self.embedded), total)

        self.embedded.clear()
        self._run()
        self.assertEqual(self.embedded, [[]])
        self.assertEqual(SupportChunk.objects.count(), total)

    def test_edit_reembeds_only_the_touched_chunk_and_gone_source_retires(self):
        post = create_post(title="Sena naujiena", content="Pirmas turinys.")
        self._run()
        self.embedded.clear()

        post.content = "Visiškai kitas turinys."
        post.save(update_fields=["content"])
        self._run()
        self.assertEqual(sum(len(batch) for batch in self.embedded), 1)
        self.assertIn("Visiškai kitas turinys.", self.embedded[-1][0])

        post.delete()
        self._run()
        self.assertEqual(SupportChunk.objects.filter(source="news").count(), 0)

    def test_dead_gateway_aborts_before_any_delete(self):
        create_post(title="Naujiena")
        self._run()
        rows_before = SupportChunk.objects.count()

        def dead_embed(texts, post=None):
            raise GatewayError("down")

        self._module.embed_texts = dead_embed
        create_post(title="Kita naujiena")
        with self.assertRaises(SystemExit):
            self._run()
        self.assertEqual(SupportChunk.objects.count(), rows_before)

    def test_a_mass_retirement_is_refused_unless_the_operator_allows_it(self):
        from knfapp.assistant.indexing import run_index
        for index in range(12):
            create_post(title=f"Naujiena {index}", content=f"Turinys {index}.")
        self._run()
        news_rows = SupportChunk.objects.filter(source="news").count()
        self.assertEqual(news_rows, 12)

        # The news unit comes back EMPTY without raising — the shape
        # of a whitelist drift — so no per-unit guard can see it
        from knfapp.assistant import chunking as chunking_module
        original = chunking_module.news_chunks
        chunking_module.news_chunks = lambda failures=None: []
        self.addCleanup(lambda: setattr(chunking_module, "news_chunks", original))

        with self.assertRaises(SystemExit) as refused:
            self._run()
        self.assertEqual(refused.exception.code, 2)
        self.assertEqual(SupportChunk.objects.filter(source="news").count(), 12)

        counts = run_index()
        self.assertEqual(counts["retired"], 0)
        self.assertEqual(counts["retireBlocked"], 12)

        counts = run_index(allow_mass_retire=True)
        self.assertEqual(counts["retired"], 12)
        self.assertEqual(SupportChunk.objects.filter(source="news").count(), 0)

    def test_a_few_retirements_still_happen_the_same_night(self):
        posts = [create_post(title=f"Naujiena {index}") for index in range(3)]
        self._run()
        posts[0].delete()
        self._run()
        self.assertEqual(SupportChunk.objects.filter(source="news").count(), 2)


class HandbookTitleTests(TestCase):

    def test_general_contact_is_titled_for_humans_and_laid_out_as_contacts(self):
        from knfapp.info.models import FacultyInfo
        from knfapp.common.timestamps import utc_now_iso
        import uuid as uuid_module
        FacultyInfo.objects.create(id=str(uuid_module.uuid4()), lang="lt", section="general_contact",
                                   data_json={"address": "Muitinės g. 8, LT-44280 Kaunas",
                                              "phone": "+370 37 422 523", "email": "knf@knf.vu.lt"},
                                   scraped_at=utc_now_iso())
        rows = {(chunk["language"], chunk["title"]): chunk for chunk in chunking.handbook_chunks()
                if chunk["source_id"].endswith("-general_contact")}
        self.assertIn(("lt", "Fakulteto kontaktai"), rows)
        self.assertIn(("en", "Faculty contacts"), rows)
        chunk = rows[("lt", "Fakulteto kontaktai")]
        self.assertEqual(chunk["section"], "contacts")
        self.assertEqual(chunk["text"].splitlines(),
                         ["Muitinės g. 8, LT-44280 Kaunas", "tel. +370 37 422 523", "knf@knf.vu.lt"])
        self.assertNotIn("general_contact", chunk["title"])

    def test_an_unmapped_section_title_is_its_key_made_readable(self):
        self.assertEqual(chunking._section_title("lt", "study_offices"), "Study offices")
        self.assertEqual(chunking._section_title("en", "programs"), "Study programs")
