# SPDX-License-Identifier: AGPL-3.0-or-later
"""Curated "featured" bangs surfaced in the search-bar autocomplete menu.

SearXNG ships ~13k DuckDuckGo external bangs (``!!``) plus the per-engine
internal bangs (``!``). That long tail is great as a fallback but useless as a
*menu*: there are far too many and most are irrelevant to any one person. This
module defines a small, hand-picked, UK-leaning set — a Kagi-style featured-bang
list — that is the only thing the autocomplete dropdown surfaces, each with a
human name and a favicon. The full ``!!`` database stays available untouched.

Each entry resolves a single ``!shortcut`` to a site search (or the site home
page when the site has no clean public search endpoint). Resolution reuses the
existing external-bang plumbing (see :py:func:`searx.external_bang.get_bang_url`)
so nothing downstream needs to know these exist. A curated featured shortcut
takes priority over a like-named engine (see :py:meth:`searx.query.BangParser`),
so ``!reddit`` always gets its featured behaviour rather than falling through to
the ``reddit`` engine; the engine stays reachable via its own shortcut (``!re``).
"""

from __future__ import annotations

import typing as t
from urllib.parse import quote_plus

__all__ = ["is_featured_bang", "resolve_featured_bang", "featured_bang_suggestions", "FeaturedBang"]

_QUERY = "{query}"


class FeaturedBang(t.NamedTuple):
    shortcut: str  # the bang key, without the leading '!'
    name: str  # human label shown in the menu
    domain: str  # authority used to resolve the favicon
    search: str | None  # URL template containing '{query}', or None → home page


# Order here is the order shown in the menu (within a match group).
FEATURED_BANGS: list[FeaturedBang] = [
    # — UK money & comparison —
    FeaturedBang("mse", "MoneySavingExpert", "moneysavingexpert.com", "https://www.moneysavingexpert.com/search/?q={query}"),
    FeaturedBang("bcwyc", "Be Clever With Your Cash", "becleverwithyourcash.com", "https://becleverwithyourcash.com/?s={query}"),
    FeaturedBang("msm", "MoneySuperMarket", "moneysupermarket.com", None),
    FeaturedBang("uswitch", "Uswitch", "uswitch.com", None),
    FeaturedBang("meerkat", "Compare the Market", "comparethemarket.com", None),
    # — shopping —
    FeaturedBang("amazon", "Amazon UK", "amazon.co.uk", "https://www.amazon.co.uk/s?k={query}"),
    FeaturedBang("ebay", "eBay UK", "ebay.co.uk", "https://www.ebay.co.uk/sch/i.html?_nkw={query}"),
    FeaturedBang("argos", "Argos", "argos.co.uk", "https://www.argos.co.uk/search/{query}/"),
    # — news & reference —
    FeaturedBang("bbc", "BBC", "bbc.co.uk", "https://www.bbc.co.uk/search?q={query}"),
    FeaturedBang("guardian", "The Guardian", "theguardian.com", "https://www.theguardian.com/search?q={query}"),
    FeaturedBang("wiki", "Wikipedia", "en.wikipedia.org", "https://en.wikipedia.org/w/index.php?search={query}"),
    # — government & health —
    FeaturedBang("gov", "GOV.UK", "gov.uk", "https://www.gov.uk/search/all?keywords={query}"),
    FeaturedBang("nhs", "NHS", "nhs.uk", "https://www.nhs.uk/search/results?q={query}"),
    # — film & TV (ties into the watch box) —
    FeaturedBang("imdb", "IMDb", "imdb.com", "https://www.imdb.com/find/?q={query}"),
    FeaturedBang("jw", "JustWatch", "justwatch.com", "https://www.justwatch.com/uk/search?q={query}"),
    # — dev —
    FeaturedBang("gh", "GitHub", "github.com", "https://github.com/search?q={query}&type=repositories"),
    FeaturedBang("so", "Stack Overflow", "stackoverflow.com", "https://stackoverflow.com/search?q={query}"),
    # — social & video —
    FeaturedBang("reddit", "Reddit", "reddit.com", "https://www.reddit.com/search?q={query}"),
    FeaturedBang("yt", "YouTube", "youtube.com", "https://www.youtube.com/results?search_query={query}"),
    # — UK travel & property —
    FeaturedBang("trainline", "Trainline", "thetrainline.com", None),
    FeaturedBang("tfl", "Transport for London", "tfl.gov.uk", None),
    FeaturedBang("rightmove", "Rightmove", "rightmove.co.uk", None),
    # — archive —
    FeaturedBang("archive", "Internet Archive", "archive.org", "https://archive.org/search?query={query}"),
]


def _normalize(shortcut: str) -> str:
    # Match BangParser's own normalisation: lower-cased, separators dropped. This
    # lets '!money-saving' or '!money_saving' reach the same entry as '!moneysaving'.
    return shortcut.strip().lower().replace(" ", "").replace("-", "").replace("_", "")


_BY_SHORTCUT: dict[str, FeaturedBang] = {b.shortcut: b for b in FEATURED_BANGS}


def is_featured_bang(shortcut: str) -> bool:
    return _normalize(shortcut) in _BY_SHORTCUT


def featured_bang_domain(shortcut: str) -> str | None:
    """The site authority for a featured ``!shortcut`` (used by the in-uSearch,
    ``site:`` scoped search mode), or ``None`` if it is not a featured bang."""
    bang = _BY_SHORTCUT.get(_normalize(shortcut))
    return bang.domain if bang else None


def resolve_featured_bang(shortcut: str, query: str) -> str | None:
    """Redirect URL for a featured ``!shortcut`` and its query, or ``None``.

    With a query and a search template, returns the site search URL; otherwise
    (no query, or a home-page-only entry) returns the site's home page.
    """
    bang = _BY_SHORTCUT.get(_normalize(shortcut))
    if bang is None:
        return None
    if bang.search and query:
        return bang.search.replace(_QUERY, quote_plus(query))
    return f"https://{bang.domain}/"


def featured_bang_suggestions(prefix: str, limit: int = 10) -> list[FeaturedBang]:
    """Featured bangs matching ``prefix`` (after the '!'), best matches first.

    Matches by shortcut prefix first, then by name substring, so typing
    ``!money`` surfaces both MoneySavingExpert and MoneySuperMarket.
    """
    prefix = _normalize(prefix)
    if not prefix:
        return FEATURED_BANGS[:limit]
    by_shortcut = [b for b in FEATURED_BANGS if b.shortcut.startswith(prefix)]
    by_name = [
        b for b in FEATURED_BANGS if b not in by_shortcut and prefix in b.name.lower().replace(" ", "")
    ]
    return (by_shortcut + by_name)[:limit]
