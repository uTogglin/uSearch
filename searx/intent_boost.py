# SPDX-License-Identifier: AGPL-3.0-or-later
"""Query-intent-aware result re-ranking (uSearch fork).

The base score (cross-engine consensus + domain popularity) still treats a
"buy the Blu-ray / merch" page and a "where can I stream it" page the same.  For
entertainment queries (a film or TV show) users almost always want *where to
watch* -- ideally something already included in a subscription -- not a shop
selling them the disc.  So when we detect an entertainment query we:

  * boost streaming / "where to watch" domains (JustWatch first -- it is the
    availability aggregator -- then the streaming platforms), and
  * demote pay-to-own / general-shopping domains (Amazon, eBay, iTunes, ...).

Intent is detected from the *results themselves* (language-independent): if the
result set contains a recognised film/TV reference (IMDb, TMDB, Rotten Tomatoes,
JustWatch, ...) the query is treated as entertainment.  No query parsing, no NLP.

These lists are deliberately readable -- extend them as needed.
"""

import re

__all__ = ["is_entertainment", "multiplier", "justwatch_region", "justwatch_country"]


def justwatch_region(lang: "str | None") -> str:
    """Map a search locale (e.g. ``en-GB``) to a JustWatch region path segment.

    JustWatch uses lowercased country codes, except Great Britain which is
    ``uk`` (not ``gb``).  Falls back to ``uk`` (this instance defaults to
    en-GB) when no country is present.
    """
    if not lang or lang == "all":
        return "uk"
    parts = lang.replace("_", "-").split("-")
    country = parts[-1].lower() if len(parts) > 1 else ""
    if country in ("", "gb"):
        return "uk"
    return country


def justwatch_country(lang: "str | None") -> str:
    """Map a search locale (e.g. ``en-GB``) to a JustWatch GraphQL ``country``.

    The GraphQL API keys on ISO 3166-1 alpha-2 codes (upper-case, ``GB`` for
    Great Britain) -- unlike the website path segment, which spells it ``uk``
    (see :func:`justwatch_region`).  Falls back to ``GB`` (this instance
    defaults to en-GB) when no usable country is present.
    """
    if not lang or lang == "all":
        return "GB"
    parts = lang.replace("_", "-").split("-")
    country = parts[-1].upper() if len(parts) > 1 else ""
    if len(country) != 2 or not country.isalpha():
        return "GB"
    if country == "UK":  # tolerate the colloquial code
        return "GB"
    return country


def _rx(*pats: str) -> list[re.Pattern]:
    return [re.compile(p) for p in pats]


# Presence of ANY of these in the result set => entertainment (film/TV) query.
ENTERTAINMENT_REF = _rx(
    r'(^|\.)imdb\.com$',
    r'(^|\.)iket\.me$',  # libremdb (IMDb privacy frontend)
    r'(^|\.)themoviedb\.org$',
    r'(^|\.)rottentomatoes\.com$',
    r'(^|\.)metacritic\.com$',
    r'(^|\.)justwatch\.com$',
    r'(^|\.)thetvdb\.com$',
    r'(^|\.)letterboxd\.com$',
)

# "Where to watch" -> boosted on entertainment intent.  Pure streaming /
# availability domains only (no broad broadcaster portals, which also carry
# news and would over-boost).
WATCH = _rx(
    r'(^|\.)justwatch\.com$',
    r'(^|\.)netflix\.com$',
    r'(^|\.)primevideo\.com$',
    r'(^|\.)hulu\.com$',
    r'(^|\.)disneyplus\.com$',
    r'(^|\.)max\.com$',
    r'(^|\.)hbomax\.com$',
    r'(^|\.)peacocktv\.com$',
    r'(^|\.)paramountplus\.com$',
    r'(^|\.)tv\.apple\.com$',
    r'(^|\.)crunchyroll\.com$',
    r'(^|\.)mubi\.com$',
)

# Pay-to-own / general shopping -> demoted on entertainment intent.  (Note:
# amazon.* is shopping here; the included-with-subscription path is primevideo
# above, plus JustWatch surfaces Prime availability anyway.)
PAID_SHOPPING = _rx(
    r'(^|\.)amazon\.[a-z.]+$',
    r'(^|\.)ebay\.[a-z.]+$',
    r'(^|\.)walmart\.com$',
    r'(^|\.)target\.com$',
    r'(^|\.)bestbuy\.com$',
    r'(^|\.)itunes\.apple\.com$',
    r'(^|\.)apps\.apple\.com$',
)

# JustWatch leads, the platforms follow; shopping sinks hard but stays findable.
WATCH_BOOST = 1.8
JUSTWATCH_BOOST = 2.2
PAID_DEMOTE = 0.15


def _match(rxs: list[re.Pattern], netloc: str) -> bool:
    return any(rx.search(netloc) for rx in rxs)


def is_entertainment(hostnames: "set[str]") -> bool:
    """True if the result set looks like a film/TV query."""
    return any(_match(ENTERTAINMENT_REF, h) for h in hostnames)


def multiplier(netloc: str) -> float:
    """Score multiplier to apply when the query is entertainment intent."""
    if not netloc:
        return 1.0
    netloc = netloc.lower().split(":")[0]
    if netloc == "justwatch.com" or netloc.endswith(".justwatch.com"):
        return JUSTWATCH_BOOST
    if _match(WATCH, netloc):
        return WATCH_BOOST
    if _match(PAID_SHOPPING, netloc):
        return PAID_DEMOTE
    return 1.0
