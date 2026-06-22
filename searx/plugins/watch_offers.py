# SPDX-License-Identifier: AGPL-3.0-or-later
"""
SearXNG Watch Offers Plugin — "Where to watch" stat box
=======================================================
For film / TV queries, surfaces a compact box (rendered by serp_enhance.js, at
the top of the results column where the Reddit "sources" card sits) listing
*where to watch* the title, **organised cheapest-first** and priced in the
user's local currency: free/ad-supported first, then subscription, then the
cheapest rentals and purchases.

The data comes from JustWatch — the streaming-availability aggregator (it has no
official public API, so we use its public GraphQL endpoint, the same one the
JustWatch website itself calls).  No other service aggregates per-country,
per-provider *pricing* like this.

Privacy model (mirrors card_meta / ai_summary):
  - The browser only ever talks to THIS origin: GET /watch_offers?q=<query>
  - The origin queries JustWatch server-side, so the user's IP and the title
    they searched never leak to JustWatch from the user's machine.
  - The client supplies only the query string; the region is derived
    server-side from the search locale (overridable with &region=<cc>).
  - Provider logos are NOT loaded (they'd be third-party requests) — we render
    provider names as text, keeping the box fully first-party.
  - Responses are cached (in-memory, TTL) and rate-limited per IP, so this is
    cheap and stays well under JustWatch's anonymous limits.

CONFIGURATION (settings.yml)
------------------------------
  plugins:
    searx.plugins.watch_offers.SXNGPlugin:
      active: true

  watch_offers:
    timeout:    6        # upstream request timeout (seconds)
    cache_ttl:  21600    # cache offers for 6 hours (availability changes slowly)
    cache_max:  1000     # max cached entries
    max_offers: 12       # max offer rows returned to the client
    default_region: GB   # ISO-3166 alpha-2 fallback when no locale is present
    rate_limit_capacity: 15
    rate_limit_rate:     1.0
"""

import json
import logging
import math
import re
import threading
import time
import typing as t
import unicodedata

import httpx
from flask_babel import gettext as _
from searx import get_setting
from searx import intent_boost
from searx.plugins import Plugin, PluginInfo

if t.TYPE_CHECKING:
    from searx.plugins import PluginCfg

logger = logging.getLogger(__name__)

_USER_AGENT = "uSearch-watch-offers/1.0 (+https://search.utoggl.in)"
_JW_GRAPHQL = "https://apis.justwatch.com/graphql"
_JW_WEB = "https://www.justwatch.com"

_MAX_QUERY_LEN = 200

# For a TV series, users (and JustWatch's own URLs) tack a season/episode marker
# onto the title — "Raising Dion season 2", "the bear s3 episode 1". JustWatch's
# search matches the bare show title fine, but the *trailing* tokens after the
# marker ("...release date netflix") poison its relevance ranking and surface the
# wrong title entirely. So we cut the query at the first season/episode marker.
# A digit is required after the word forms, so this only fires on real season
# references and never mangles movie titles like "Season of the Witch" or
# "Kill Bill: Vol. 1" without a number.
_SEASON_MARKER = re.compile(
    r"\s+(?:"
    r"(?:season|series|chapter|part|vol(?:ume)?|episode|ep)\s*\.?\s*\d+"  # "season 2", "ep 3"
    r"|s\d{1,2}(?:\s*e\d{1,3})?"                                          # "s3", "s02e05"
    r").*$",
    re.IGNORECASE,
)

# A trailing *generic* media qualifier ("...series", "...tv show", "...anime")
# is how people disambiguate a title that shares its name with a game or product
# ("pokemon black and white series" -> the anime, not the Nintendo game). It is
# not part of the actual title, so strip it before searching JustWatch — it only
# adds noise to the relevance match and bloats the cache key. Kept conservative:
# we never strip bare "show"/"movie"/"film" (real titles end in those — "The
# Truman Show", "The Lego Movie").
_MEDIA_QUALIFIER = re.compile(
    r"\s+(?:the\s+)?(?:tv\s+series|web\s+series|mini[-\s]?series|tv\s+show|series|anime)\s*$",
    re.IGNORECASE,
)


def _normalise_query(query: str) -> str:
    """Strip a trailing season/episode reference (and anything after it), then a
    trailing generic media qualifier, so a series resolves to its show page.
    Falls back to the original query if the result would be empty."""
    stripped = _SEASON_MARKER.sub("", query)
    stripped = _MEDIA_QUALIFIER.sub("", stripped).strip(" -:|·")
    return stripped or query

# JustWatch GraphQL: search titles + their per-country streaming offers. Kept
# lean — only the fields we render. `popularTitles` is already relevance-sorted.
_SEARCH_QUERY = """
query GetSearchTitles($filter: TitleFilter, $country: Country!, $language: Language!, $first: Int!) {
  popularTitles(country: $country, filter: $filter, first: $first) {
    edges {
      node {
        objectType
        content(country: $country, language: $language) {
          title
          originalReleaseYear
          fullPath
        }
        offers(country: $country, platform: WEB) {
          monetizationType
          presentationType
          retailPriceValue
          currency
          standardWebURL
          package { clearName }
        }
      }
    }
  }
}
""".strip()

# monetizationType -> (kind, sort rank). Lower rank == cheaper / shown first.
# FREE / ADS cost nothing, FLATRATE is "included in a sub you may already have",
# then metered RENT and BUY sorted by their actual price.
_KIND = {
    "FREE": ("free", 0),
    "ADS": ("free", 0),
    "FLATRATE": ("stream", 1),
    "FLATRATE_AND_BUY": ("stream", 1),
    "RENT": ("rent", 2),
    "BUY": ("buy", 3),
}

_QUALITY = {"_4K": "4K", "HD": "HD", "SD": "SD"}
_QUALITY_RANK = {"4K": 0, "HD": 1, "SD": 2}  # best first when de-duping

_CURRENCY_SYMBOL = {
    "GBP": "£", "USD": "$", "EUR": "€", "JPY": "¥",
    "AUD": "A$", "CAD": "C$", "NZD": "NZ$", "INR": "₹", "BRL": "R$",
    "CHF": "CHF ", "SEK": "kr ", "NOK": "kr ", "DKK": "kr ", "PLN": "zł ",
    "MXN": "$", "ZAR": "R", "RUB": "₽", "KRW": "₩", "TRY": "₺",
}


def _setting(key: str, default=None):
    return get_setting(f"watch_offers.{key}", default)


# ── In-memory TTL cache ───────────────────────────────────────────────────────
_cache: dict = {}          # {cache_key: {"data": dict, "ts": float}}
_cache_lock = threading.Lock()


def _cache_get(key: str):
    ttl = float(_setting("cache_ttl", 21600))
    with _cache_lock:
        entry = _cache.get(key)
        if entry and time.time() - entry["ts"] < ttl:
            return entry["data"]
    return None


def _cache_set(key: str, data: dict):
    cap = int(_setting("cache_max", 1000))
    with _cache_lock:
        now = time.time()
        ttl = float(_setting("cache_ttl", 21600))
        expired = [k for k, v in list(_cache.items()) if now - v["ts"] > ttl]
        for k in expired:
            del _cache[k]
        if key not in _cache and len(_cache) >= cap:
            oldest = min(_cache, key=lambda k: _cache[k]["ts"])
            del _cache[oldest]
        _cache[key] = {"data": data, "ts": now}


# ── Per-IP token-bucket rate limiter ─────────────────────────────────────────
_rate_buckets: dict = {}
_rate_lock = threading.Lock()


def _check_rate_limit(ip: str) -> bool:
    capacity = float(_setting("rate_limit_capacity", 15))
    rate = float(_setting("rate_limit_rate", 1.0))
    now = time.time()
    with _rate_lock:
        bucket = _rate_buckets.get(ip)
        if bucket is None:
            _rate_buckets[ip] = {"tokens": capacity - 1, "last": now}
            return True
        elapsed = now - bucket["last"]
        tokens = min(capacity, bucket["tokens"] + elapsed * rate)
        bucket["last"] = now
        if tokens >= 1:
            bucket["tokens"] = tokens - 1
            idle = [k for k, v in list(_rate_buckets.items())
                    if v["tokens"] >= capacity and now - v["last"] > capacity / rate]
            for k in idle:
                del _rate_buckets[k]
            return True
        bucket["tokens"] = tokens
        return False


# ── Offer normalisation ───────────────────────────────────────────────────────

def _price_text(kind: str, value, currency: str) -> str:
    if kind == "free":
        return _("Free")
    if kind == "stream":
        return _("Subscription")
    if value is None:
        return ""
    sym = _CURRENCY_SYMBOL.get((currency or "").upper(), "")
    amount = f"{value:.2f}"
    return f"{sym}{amount}" if sym else f"{amount} {currency}".strip()


def _normalise_offers(raw_offers: list, max_offers: int) -> list:
    """Collapse JustWatch's per-quality offer rows into one row per
    (provider, kind), keeping the cheapest / best quality, then sort
    cheapest-first."""
    best: dict = {}  # (provider, kind) -> offer dict
    for off in raw_offers or []:
        mon = off.get("monetizationType")
        mapped = _KIND.get(mon)
        if not mapped:
            continue
        kind, rank = mapped
        pkg = off.get("package") or {}
        provider = (pkg.get("clearName") or "").strip()
        if not provider:
            continue
        value = off.get("retailPriceValue")
        try:
            value = float(value) if value is not None else None
        except (TypeError, ValueError):
            value = None
        quality = _QUALITY.get(off.get("presentationType") or "", "")
        cur = (off.get("currency") or "").upper()

        key = (provider.lower(), kind)
        cand = {
            "provider": provider,
            "kind": kind,
            "rank": rank,
            "price": value,
            "currency": cur,
            "quality": quality,
            "url": off.get("standardWebURL") or "",
        }
        prev = best.get(key)
        if prev is None or _cheaper_better(cand, prev):
            best[key] = cand

    offers = list(best.values())
    # cheapest first: by kind rank, then price (None == 0 for free/stream),
    # then best quality, then provider name for stable output.
    offers.sort(key=lambda o: (
        o["rank"],
        o["price"] if o["price"] is not None else 0.0,
        _QUALITY_RANK.get(o["quality"], 9),
        o["provider"].lower(),
    ))

    out = []
    for o in offers[:max_offers]:
        out.append({
            "provider": o["provider"],
            "kind": o["kind"],
            "price": o["price"],
            "currency": o["currency"],
            "priceText": _price_text(o["kind"], o["price"], o["currency"]),
            "quality": o["quality"],
            "url": o["url"],
        })
    return out


def _cheaper_better(a: dict, b: dict) -> bool:
    """Is offer ``a`` a better keep than ``b`` for the same (provider, kind)?
    Cheaper wins; on equal price, higher quality wins."""
    pa = a["price"] if a["price"] is not None else 0.0
    pb = b["price"] if b["price"] is not None else 0.0
    if pa != pb:
        return pa < pb
    return _QUALITY_RANK.get(a["quality"], 9) < _QUALITY_RANK.get(b["quality"], 9)


_TOKEN_RE = re.compile(r"[0-9a-z]+")

# Tokens that carry no title identity — articles/conjunctions and the
# platform/format words people append ("...on netflix", "...full movie hd").
# Excluded when measuring how well the chosen title accounts for the query, so
# they neither prop up nor sink a confidence score.
_GATE_STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "to", "in", "on", "at", "for", "with",
    "tv", "series", "season", "episode", "show", "movie", "film", "watch",
    "stream", "streaming", "online", "free", "full", "hd", "uhd", "4k",
    "netflix", "hulu", "disney", "plus", "prime", "video", "amazon", "hbo",
    "max", "peacock", "paramount", "apple", "iplayer", "bbc", "itv", "channel",
}

# Minimum share of the query's *content* tokens the chosen title must cover for
# the box to be shown. Below this, JustWatch only had a looser/parent title than
# what was searched (it carries "Pokémon" but not the "...Black & White" series),
# so a box would mislead — better to show nothing.
_MIN_CONFIDENCE = 0.5


def _match_confidence(title: str, query: str) -> float:
    """Fraction of the query's content tokens (stopwords/platform words removed)
    that ``title`` accounts for. 1.0 when the query is all qualifiers so an
    all-noise query (e.g. "the show") is never gated out."""
    q = _tokens(query) - _GATE_STOPWORDS
    if not q:
        return 1.0
    return len(q & _tokens(title)) / len(q)


def _tokens(text: str) -> set:
    # Fold accents (NFKD + drop combining marks) so "Pokémon" tokenises to
    # {pokemon} and matches an un-accented query "pokemon" — otherwise the
    # `[0-9a-z]+` regex splits it into {pok, mon} and never matches.
    folded = unicodedata.normalize("NFKD", (text or "").lower())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return set(_TOKEN_RE.findall(folded))


def _pick_title(edges: list, query: str) -> "dict | None":
    """Pick the node whose title best matches the query, breaking ties toward
    nodes that actually have offers, then JustWatch's own relevance order.

    JustWatch's popularTitles is relevance-sorted, but for a long/specific title
    ("Thunderbirds Are Go") a shorter sibling ("Thunderbirds") can be the first
    edge that happens to carry offers — so the old "first with offers" rule
    silently showed the wrong title. Scoring the candidate titles against the
    query keeps the box honest in both directions: "thunderbirds are go" prefers
    the exact match, while a bare "thunderbirds" prefers the shorter title (the
    longer one carries extra, unmatched tokens).

    Coverage is IDF-weighted over the candidate set so a *distinctive* query
    token outweighs generic ones. For "pokemon black and white" JustWatch
    returns both the "Pokémon" show and a 2002 film literally titled "Black and
    White"; plain token-count coverage picked the film (it matches three common
    words), but "pokemon" is rare across the candidates and "black"/"and"/"white"
    are not, so weighting by rarity correctly surfaces the show."""
    q_tokens = _tokens(query)

    nodes: list = []
    cand_tokens: list = []
    for edge in edges or []:
        node = (edge or {}).get("node") or {}
        nodes.append(node)
        cand_tokens.append(_tokens((node.get("content") or {}).get("title")))

    n = len(cand_tokens)
    df: dict = {}
    for ct in cand_tokens:
        for tok in ct:
            df[tok] = df.get(tok, 0) + 1

    def _idf(tok: str) -> float:
        # Classic log(N/df): a token in every candidate scores 0 (fully generic),
        # a token in one candidate scores high. Single-candidate sets have no
        # signal, so weight all tokens equally.
        return math.log(n / max(df.get(tok, 0), 1)) if n > 1 else 1.0

    q_weight = sum(_idf(tok) for tok in q_tokens)

    best = None
    best_key = None
    for node, t_tokens in zip(nodes, cand_tokens):
        if q_tokens:
            exact = 1 if q_tokens == t_tokens else 0
            matched = q_tokens & t_tokens
            if q_weight > 0:
                covered = sum(_idf(tok) for tok in matched) / q_weight
            else:
                # Every query token is generic across candidates — fall back to
                # plain coverage so we still prefer the fuller match.
                covered = len(matched) / len(q_tokens)
            extra = len(t_tokens - q_tokens)
        else:
            exact = covered = extra = 0
        has_offers = 1 if node.get("offers") else 0
        # exact > weighted coverage > fewer spurious tokens > has offers > JW order
        key = (exact, covered, -extra, has_offers)
        if best is None or key > best_key:
            best, best_key = node, key
    return best


def _fetch_offers(query: str, country: str, language: str, max_offers: int) -> dict:
    payload = {
        "operationName": "GetSearchTitles",
        "variables": {
            "first": 5,
            "filter": {"searchQuery": query},
            "country": country,
            "language": language,
        },
        "query": _SEARCH_QUERY,
    }
    timeout = float(_setting("timeout", 6))
    headers = {
        "User-Agent": _USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    resp = httpx.post(_JW_GRAPHQL, json=payload, headers=headers,
                      timeout=timeout, follow_redirects=True)
    resp.raise_for_status()
    body = resp.json()

    edges = (((body.get("data") or {}).get("popularTitles") or {}).get("edges")) or []
    node = _pick_title(edges, query)
    if not node:
        return {"type": None, "missing": True}

    content = node.get("content") or {}
    title = (content.get("title") or "").strip()
    offers = _normalise_offers(node.get("offers"), max_offers)
    if not title or not offers:
        # A title with zero streaming offers isn't worth a box.
        return {"type": None, "missing": True}

    if _match_confidence(title, query) < _MIN_CONFIDENCE:
        # The best JustWatch match doesn't cover the query's content words —
        # e.g. "pokemon black and white" resolves only to the parent "Pokémon"
        # show. Showing it would mislead, so report nothing rather than guess.
        return {"type": None, "missing": True}

    full_path = content.get("fullPath") or ""
    return {
        "type": "watch",
        "title": title,
        "year": content.get("originalReleaseYear") or None,
        "kindLabel": "show" if node.get("objectType") == "SHOW" else "movie",
        "url": (_JW_WEB + full_path) if full_path.startswith("/") else "",
        "region": country,
        "offers": offers,
    }


class SXNGPlugin(Plugin):
    """Where-to-watch offer box — server-side JustWatch proxy.

    The frontend (serp_enhance.js) is already injected by the card_meta plugin;
    this plugin only provides the JSON endpoint it calls.
    """

    id = "watch_offers"
    keywords: list = []

    def __init__(self, plg_cfg: "PluginCfg") -> None:
        super().__init__(plg_cfg)
        self.info = PluginInfo(
            id=self.id,
            name=_("Where to Watch"),
            description=_("Show streaming, rent and buy options (cheapest first) for film/TV queries"),
            preference_section="general",
        )

    def init(self, app) -> bool:
        from flask import request as freq, Response

        def _client_ip() -> str:
            return (freq.headers.get("X-Forwarded-For", "")
                    or freq.remote_addr or "").split(",")[0].strip()

        def _region() -> str:
            # Explicit override wins (validated 2-letter), else derive from the
            # user's search locale, else the configured default.
            override = (freq.args.get("region", "") or "").strip()
            if len(override) == 2 and override.isalpha():
                return intent_boost.justwatch_country(override)
            try:
                lang = freq.preferences.get_value("language")
            except Exception:
                lang = None
            if lang and lang != "all":
                return intent_boost.justwatch_country(lang)
            return intent_boost.justwatch_country(_setting("default_region", "GB"))

        @app.route("/watch_offers", methods=["GET"])
        def watch_offers():
            query = (freq.args.get("q", "") or "").strip()[:_MAX_QUERY_LEN]
            if not query:
                return Response('{"type":null}', mimetype="application/json", status=400)

            # Series queries carry a "season N"/"sNNeMM" tail that derails
            # JustWatch search — resolve to the bare show title (also tightens
            # the cache key, so "raising dion season 2" shares "raising dion").
            query = _normalise_query(query)

            region = _region()
            language = region.lower()  # JustWatch accepts the country code as language
            cache_key = f"{region}:{query.lower()}"

            cached = _cache_get(cache_key)
            if cached is not None:
                return Response(json.dumps(cached), mimetype="application/json",
                                headers={"X-Cache": "HIT"})

            if not _check_rate_limit(_client_ip()):
                return Response('{"type":null,"error":"rate_limited"}',
                                mimetype="application/json", status=429)

            max_offers = int(_setting("max_offers", 12))
            try:
                data = _fetch_offers(query, region, language, max_offers)
            except httpx.HTTPStatusError as exc:
                logger.info("watch_offers upstream %s", exc.response.status_code)
                return Response('{"type":null,"error":"upstream"}',
                                mimetype="application/json", status=502)
            except Exception as exc:  # noqa: BLE001 — never 500 the SERP
                logger.warning("watch_offers error: %s", exc)
                return Response('{"type":null,"error":"fetch"}',
                                mimetype="application/json", status=502)

            _cache_set(cache_key, data)
            return Response(json.dumps(data), mimetype="application/json",
                            headers={"X-Cache": "MISS"})

        return True
