############################################################
#  [*] assistant curated FAQ — repo-owned extra answers
#
#  The knowledge the scrape does not carry: answers written
#  BY HAND for questions students actually ask the support
#  agent. Edited like handbook.py — the literals are the
#  content, changed deliberately, and the nightly indexer
#  picks a change up through its content hash on the next
#  tick (or immediately via `manage.py
#  index_support_corpus`).
#
#  Shape: {lang: [{"q": ..., "a": ...}, ...]} — one chunk
#  per entry, the question becoming the chunk title, exactly
#  like the handbook FAQ section. Keep answers short,
#  factual and self-contained: the model quotes them, it
#  does not summarize around them. Room numbers, prices and
#  deadlines belong in the scraped handbook first — put a
#  fact here only when no scraped source can carry it.
#
#  Used by:
#    - chunking.py — curated_chunks()
############################################################


CURATED_FAQ = {
    "lt": [
        {
            "q": "Kam skirtas šis pagalbininkas?",
            "a": "Tai VU Kauno fakulteto programėlės pagalbininkas: atsako apie tvarkaraščius, "
                 "fakulteto naujienas, kontaktus, darbo laiką ir praktinius studijų klausimus. "
                 "Jis nemato pažymių ar asmeninių duomenų ir neatsako už fakulteto ribų.",
        },
        {
            "q": "Kur kreiptis, jei pagalbininkas nežino atsakymo?",
            "a": "Rašykite Studijų skyriui studijos@knf.vu.lt arba užsukite į 102 kabinetą — "
                 "tai pagrindinis kontaktas visais studijų klausimais.",
        },
    ],
    "en": [
        {
            "q": "What is this assistant for?",
            "a": "It is the VU Kaunas Faculty app assistant: it answers about timetables, "
                 "faculty news, contacts, opening hours and practical study questions. "
                 "It cannot see grades or personal records and does not answer beyond faculty topics.",
        },
        {
            "q": "Who do I contact when the assistant does not know?",
            "a": "Write to the Studies Department at studijos@knf.vu.lt or visit room 102 — "
                 "the main contact for all study matters.",
        },
    ],
}
