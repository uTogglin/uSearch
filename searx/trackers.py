# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-domain tracker-density signal (uSearch fork).

Powers the privacy figure on each result's "shield": *how many third-party
trackers a site typically loads*, the same at-a-glance idea as Kagi's per-result
privacy badge.  The numbers come from Ghostery's `whotracks.me`_ monthly public
measurement and are baked into ``searx/data/tracker_radar.json.gz`` by
``searxng_extra/update/update_trackers.py``.

This module just loads that map and answers lookups by hostname, walking
subdomains off the front so ``www.cnn.com`` resolves to ``cnn.com`` (the list is
keyed by registrable domain, exactly like :py:mod:`searx.popularity`).

.. _whotracks.me: https://whotracks.me/
"""

import gzip
import json

from searx import logger
from searx.data import data_dir

log = logger.getChild("trackers")

_DATA_FILE = data_dir / "tracker_radar.json.gz"

# host -> (tracker_count, company_count); None until first load.
_sites: "dict[str, list[int]] | None" = None
_month: str = ""


def _load() -> "dict[str, list[int]]":
    global _sites, _month  # pylint: disable=global-statement
    if _sites is not None:
        return _sites
    sites: "dict[str, list[int]]" = {}
    try:
        with gzip.open(_DATA_FILE, "rt", encoding="utf-8") as f:
            blob = json.load(f)
        sites = blob.get("sites") or {}
        _month = blob.get("_month") or ""
        log.debug("loaded %d tracker-density entries (%s)", len(sites), _month)
    except FileNotFoundError:
        log.warning("tracker list %s not found; tracker badges disabled", _DATA_FILE)
    _sites = sites
    return _sites


def _registrable(host: str) -> "list[int] | None":
    sites = _load()
    if not sites:
        return None
    host = host.lower().split(":")[0]
    labels = host.split(".")
    for i in range(len(labels) - 1):  # keep at least the final two labels
        hit = sites.get(".".join(labels[i:]))
        if hit is not None:
            return hit
    return None


def lookup(host: str) -> "dict | None":
    """Return ``{"trackers": int, "companies": int}`` for a hostname, or None.

    ``None`` means "not in the measured top-sites list" — i.e. unknown, *not*
    "zero trackers".  Callers should present it as "no data" rather than "clean".
    """
    if not host:
        return None
    hit = _registrable(host)
    if hit is None:
        return None
    return {"trackers": hit[0], "companies": hit[1] if len(hit) > 1 else 0}
