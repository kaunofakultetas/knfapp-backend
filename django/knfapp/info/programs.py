############################################################
#  [*] info programmes — what is NOT a study programme
#
#  The knf.vu.lt bachelor listing links its admission-
#  procedure documents under the same URL shape as its
#  programmes ("VU priėmimo taisyklės", "LAMA BPO bendrojo
#  priėmimo tvarka"), so the scraper's link pass took them
#  for programmes and GET /api/info served them to students
#  as bachelor degrees (KNF-077). No programme title ever
#  carries admission vocabulary, so the NAME decides — the
#  href host cannot (programme pages live on both knf.vu.lt
#  and www.vu.lt). One rule, used where the list is written
#  and where it is served, so rows already stored are
#  cleaned without waiting for the next scrape.
#
#  Split into:
#
#    is_program_name — the one test
############################################################


import re


# Admission-procedure vocabulary: priėmimas / priėmimo …
# (admission), taisyklės (rules), tvarka (procedure) and the
# national admissions body LAMA BPO — whole words, any case
_NOT_A_PROGRAM_RE = re.compile(
    r"\bpriėmim\w*|\btaisykl\w*|\btvark(?:a|os|ą)\b|\blama\s*bpo\b",
    re.IGNORECASE,
)








############################################################
# is_program_name
############################################################
#
#   is_program_name("Finansų analitika")      → True
#   is_program_name("VU priėmimo taisyklės")  → False
#
# Used by:
#   - scraper/info_scraper.py — _scrape_programs' link pass
#   - info/api/views.py — clean_program, for rows already
#     stored
############################################################

def is_program_name(name):
    return isinstance(name, str) and bool(name.strip()) and not _NOT_A_PROGRAM_RE.search(name)
