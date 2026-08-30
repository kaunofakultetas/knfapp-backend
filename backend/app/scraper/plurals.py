############################################################
#  [*] Scraper — Lithuanian plural forms for push copy
#
#  One helper shared by the three scrapers so their push
#  notification bodies decline correctly: Lithuanian has
#  three cardinal forms (1 naujas straipsnis, 2–9 nauji
#  straipsniai, 10/11–19 naujų straipsnių — and 21 is
#  singular again), which a bare `if n > 1` cannot pick.
#  Deliberately NOT wired to the mobile app's i18next
#  catalogs — the backend has no JS runtime; the scrapers
#  keep their copy inline and only the form choice lives
#  here.
############################################################








############################################################
# lt_plural
############################################################
#
# Picks one of the three Lithuanian cardinal forms for a
# count, per the CLDR rule:
#   one    n % 10 == 1 and n % 100 != 11        (1, 21, 101)
#   few    n % 10 in 2..9 and n % 100 not in
#          11..19                               (2, 23, 102)
#   other  everything else                      (0, 10-19, 30)
# `forms` is the (one, few, other) tuple; negative counts
# are folded to their magnitude, though no caller sends one.
#
# Used by:
#   - scraper/knf_scraper.py — scrape_knf_news push body
#   - scraper/vu_scraper.py — scrape_vu_news push body
#   - scraper/schedule_scraper.py — scrape_knf_schedule
#     push body
############################################################

def lt_plural(n: int, forms: tuple[str, str, str]) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 9 and not 11 <= n % 100 <= 19:
        return forms[1]
    return forms[2]
