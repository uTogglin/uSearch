# SPDX-License-Identifier: AGPL-3.0-or-later
"""Domain-popularity ranking signal (uSearch fork).

SearXNG orders main results purely by cross-engine *position consensus* (see
:py:func:`searx.results.calculate_score`); it has no sense of how popular or
trafficked a site actually is.  That lets an obscure shop outrank the official
result whenever it happens to show up in a couple of engines.

This module loads the Tranco top-domains list (built by
``searxng_extra/update/update_popularity.py``) and turns a result's hostname
into a *multiplicative* score boost, giving well-trafficked / authoritative
domains a generic nudge above obscure ones.

The boost is deliberately mild and multiplicative: it *amplifies* the existing
relevance/consensus score rather than injecting popularity from nothing, so a
hugely popular site that is only marginally relevant to the query still cannot
dominate the results.
"""

import gzip
import math

from searx import logger
from searx.data import data_dir

log = logger.getChild("popularity")

_DATA_FILE = data_dir / "domain_popularity.txt.gz"

# Extra boost granted to the single most popular domain (rank 1).  rank-1 gets a
# factor of ``1 + BOOST_STRENGTH``; the boost decays logarithmically to 1.0 for
# the tail and for hostnames absent from the list.
BOOST_STRENGTH = 0.7

_rank: "dict[str, int] | None" = None
_log_cut: float = 1.0


def _load() -> "dict[str, int]":
    global _rank, _log_cut  # pylint: disable=global-statement
    if _rank is not None:
        return _rank
    ranks: "dict[str, int]" = {}
    try:
        with gzip.open(_DATA_FILE, "rt", encoding="utf-8") as f:
            for i, line in enumerate(f, start=1):
                domain = line.strip()
                if domain:
                    ranks[domain] = i
        log.debug("loaded %d popularity ranks", len(ranks))
    except FileNotFoundError:
        log.warning("popularity list %s not found; boost disabled", _DATA_FILE)
    _rank = ranks
    _log_cut = math.log10(len(ranks)) if ranks else 1.0
    return _rank


def _registrable_rank(netloc: str) -> "int | None":
    """Best (lowest) rank for the hostname.

    Tranco lists registrable domains (eTLD+1), so we walk subdomains off the
    front and return the first hit: ``www.bbc.co.uk`` -> ``bbc.co.uk``.
    """
    ranks = _load()
    if not ranks:
        return None
    netloc = netloc.lower().split(":")[0]  # strip any :port
    labels = netloc.split(".")
    for i in range(len(labels) - 1):  # keep at least the final two labels
        r = ranks.get(".".join(labels[i:]))
        if r is not None:
            return r
    return None


def boost(netloc: str) -> float:
    """Multiplicative score boost in ``[1.0, 1.0 + BOOST_STRENGTH]``.

    rank 1 -> ``1 + BOOST_STRENGTH``; logarithmically decaying to 1.0 at the
    tail; unranked / empty hostnames -> 1.0 (neutral).
    """
    if not netloc:
        return 1.0
    r = _registrable_rank(netloc)
    if r is None:
        return 1.0
    frac = max(0.0, (_log_cut - math.log10(r)) / _log_cut)
    return 1.0 + BOOST_STRENGTH * frac
