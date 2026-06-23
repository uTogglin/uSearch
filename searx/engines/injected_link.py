# SPDX-License-Identifier: AGPL-3.0-or-later
"""Injected search link (offline engine)

A reusable *offline* engine that returns a single result linking straight into a
target site's own search page, with the user's query injected.  Use it for "good
sites" that have no usable search API (or whose API is gated behind bot
protection / request signing) but where a direct, one-click search link is still
worth surfacing.

Configure one entry per site in ``settings.yml`` -- every key below is a normal
engine setting, so the same module powers any number of sites::

  - name: makerworld
    engine: injected_link
    shortcut: mkw
    categories: [3d print]
    site_name: MakerWorld
    search_url: https://makerworld.com/en/search/models?keyword={query}

``search_url`` must contain the ``{query}`` placeholder; the URL-encoded query
(spaces as ``+``) is substituted in.  Results are emitted with ``high`` priority
by default so the link is visible rather than buried beneath richer engine
results; override per-site with ``result_priority: ""`` (normal) or ``"low"``.
"""

import typing as t
from urllib.parse import quote_plus

from searx.result_types import EngineResults

if t.TYPE_CHECKING:
    from searx.search.processors import RequestParams

engine_type = "offline"

about = {
    "website": "",
    "wikidata_id": None,
    "official_api_documentation": None,
    "use_official_api": False,
    "require_api_key": False,
    "results": "HTML",
}

# Per-site settings (overridden by the settings.yml entry via setattr).
site_name: str = ""
search_url: str = ""
result_priority: str = "high"


def search(query: str, params: "RequestParams") -> EngineResults:  # pylint: disable=unused-argument
    results = EngineResults()
    query = (query or "").strip()
    if not query or "{query}" not in search_url:
        return results

    name = site_name or "this site"
    results.add(
        results.types.LegacyResult(
            {
                "url": search_url.replace("{query}", quote_plus(query)),
                "title": f'Search {name} for "{query}"',
                "content": f"Open {name}'s own search results for this query.",
                "priority": result_priority,
            }
        )
    )
    return results
