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
import threading
import time
import typing as t

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


def _pick_title(edges: list) -> "dict | None":
    """Pick the most relevant title that actually has offers; fall back to the
    top relevance match."""
    first = None
    for edge in edges or []:
        node = (edge or {}).get("node") or {}
        if first is None:
            first = node
        if node.get("offers"):
            return node
    return first


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
    node = _pick_title(edges)
    if not node:
        return {"type": None, "missing": True}

    content = node.get("content") or {}
    title = (content.get("title") or "").strip()
    offers = _normalise_offers(node.get("offers"), max_offers)
    if not title or not offers:
        # A title with zero streaming offers isn't worth a box.
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
