############################################################
#  [*] Schedule — the event-type vocabulary
#
#  The timetable site names every event's type in Lithuanian
#  ("Paskaita", "Pratybos", "Egzaminas", "Paskaitos ir
#  seminarai", …) and the scraper stores that word as it
#  came (schedule_scraper.join_lecture_type). Two readers
#  need to understand it: the scraper, settling a slot two
#  feeds type differently (an exam must win), and the
#  iCalendar feed, which marks exams in the SUMMARY and
#  speaks English on request. Both read it here, matched on
#  the diacritic-folded, lower-cased word STEM so "EGZAMINAS"
#  and "egzaminas" agree. The mobile engine keeps the same
#  table in TypeScript (timetableengine adapters/knf
#  KIND_STEMS) — keep the two in step.
#
#  Split into:
#
#    KIND_STEMS    — stem → (English name, badge?) table
#    fold          — the diacritic-free lower-case key
#    is_badge_kind — an exam-like type, never an ordinary class
#    english_kind  — the English name of a known type
############################################################


import unicodedata


# ORDER SIGNIFICANT — the first stem the folded word starts
# with wins, so the two combined types stand before the
# "paskait" they begin with. The flag marks the types a
# student must not mistake for an ordinary class
KIND_STEMS = (
    ("paskaitos ir seminar", "Lectures and seminars", False),
    ("paskaitos ir pratyb", "Lectures and practicals", False),
    ("paskait", "Lecture", False),
    ("pratyb", "Practical", False),
    ("seminar", "Seminar", False),
    ("laborator", "Lab work", False),
    ("egzamin", "Exam", True),
    ("perlaikym", "Retake", True),
    ("atsiskaitym", "Assessment", True),
    ("kolokvium", "Assessment", True),
    ("koliokvium", "Assessment", True),
    ("iskait", "Assessment", True),
    ("kontrolin", "Assessment", True),
    ("konsultacij", "Consultation", True),
)








############################################################
# fold / _entry
############################################################
#
# The key every lookup matches on: diacritics stripped (NFD,
# combining marks dropped — "Įskaita" → "iskaita"), case
# folded, whitespace collapsed; and the table row a type word
# starts with, or None. An "X ir Y" combination the table
# does not name is None too — it must never pass for its
# first half.
#
# Used by:
#   - is_badge_kind, english_kind (below)
############################################################

def fold(word: str) -> str:
    decomposed = unicodedata.normalize("NFD", word or "")
    bare = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(bare.casefold().split())


def _entry(word: str):
    folded = fold(word)
    if not folded:
        return None
    for stem, english, badge in KIND_STEMS:
        if folded.startswith(stem):
            # Only the table's own combined stems may match a
            # combination; any other "X ir Y" is unknown
            if " ir " in folded and " ir " not in stem:
                return None
            return stem, english, badge
    return None








############################################################
# is_badge_kind
############################################################
#
# True for an exam-like type — an exam, a retake, an
# assessment, a consultation.
#
# Used by:
#   - scraper/schedule_scraper.py _pick_kind — an exam wins a
#     slot two feeds type differently
#   - schedule/ical.py — the "Egzaminas: …" SUMMARY prefix
############################################################

def is_badge_kind(word: str) -> bool:
    entry = _entry(word)
    return bool(entry and entry[2])








############################################################
# english_kind
############################################################
#
# The English name of a type word, or the word itself (first
# letter raised) when the table does not know it — printed
# in the source's language rather than guessed.
#
# Used by:
#   - schedule/ical.py — the feed's ?lang=en rendering
############################################################

def english_kind(word: str) -> str:
    entry = _entry(word)
    if entry:
        return entry[1]
    word = (word or "").strip()
    return word[:1].upper() + word[1:]
