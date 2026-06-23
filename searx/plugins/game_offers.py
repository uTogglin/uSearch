# SPDX-License-Identifier: AGPL-3.0-or-later
"""
SearXNG Game Offers Plugin — "Best PC price" box
================================================
For PC-game queries, surfaces a compact box (rendered by serp_enhance.js, at the
top of the results column where the Reddit "sources" card sits) listing **where
to buy the game cheapest** among official / authorised PC stores (Steam, GOG,
Humble, Fanatical, GreenManGaming, Epic, …), organised cheapest-first and priced
in the user's local currency.

DATA SOURCES (merged; each is best-effort — a dead source never breaks the box)
------------------------------------------------------------------------------
  * **CheapShark** (https://www.cheapshark.com/api) — keyless JSON REST covering
    ~30 official PC stores.  Prices are USD and converted to the display
    currency with a daily-cached FX rate.
  * **IsThereAnyDeal** (https://api.isthereanydeal.com) — official stores plus
    the all-time historical low.  Needs a free API key (``game_offers.itad_api_key``
    or the ``ITAD_API_KEY`` env var); the box simply omits ITAD data if unset.

Privacy model (mirrors watch_offers / card_meta):
  - The browser only ever talks to THIS origin: GET /game_offers?q=<query>
  - The origin queries the upstreams server-side, so the user's IP and the game
    they searched never leak to CheapShark / ITAD.
  - Store logos are NOT loaded (they'd be third-party requests) — store names
    are rendered as text, keeping the box fully first-party.
  - Responses are cached (in-memory, TTL) and rate-limited per IP.

CONFIGURATION (settings.yml) — see the ``game_offers:`` block.
"""

import json
import logging
import math
import os
import re
import threading
import time
import typing as t
import unicodedata

import httpx
from flask_babel import gettext as _
from searx import get_setting
from searx import intent_boost
from searx.data import data_dir
from searx.plugins import Plugin, PluginInfo

if t.TYPE_CHECKING:
    from searx.plugins import PluginCfg

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_CHEAPSHARK = "https://www.cheapshark.com/api/1.0"
_ITAD = "https://api.isthereanydeal.com"
_FX_URL = "https://open.er-api.com/v6/latest/USD"

_MAX_QUERY_LEN = 200

# Shopping-*intent* words ("buy", "price", "cheapest", "cd key", …) almost never
# occur inside a real game title, so they are stripped wherever they appear.
# Replaced with a space (not deleted) so neighbouring words don't glue together.
_INTENT = re.compile(
    r"\b(?:buy|prices?|cheap(?:est)?|deals?|discount|sale"
    r"|cd\s*keys?|steam\s*keys?|game\s*keys?|keys?)\b",
    re.IGNORECASE,
)

# Platform words ("pc", "steam", "ps5", …) DO appear inside titles ("Epic
# Mickey", "SteamWorld Dig"), so they are only stripped when they *trail* the
# query — the "...on steam"/"...pc" suffix people add to disambiguate.
_TRAIL_PLATFORM = re.compile(
    r"(?:\s+(?:on|for))?\s+"
    r"(?:pc|steam|gog|epic|origin|uplay|ubisoft|xbox|playstation|ps[45]|switch)\s*$",
    re.IGNORECASE,
)

# A trailing generic "game"/"video game" is the qualifier people append to
# disambiguate a title that also names a film or show ("sonic game", "the last
# of us game").  It is not part of the real title, so strip it — trailing only,
# so a title that genuinely contains the word ("Game Dev Tycoon", "This War of
# Mine") keeps it.  Mirrors the frontend GAME_TRAIL_RE in serp_enhance.js.
_TRAIL_GENERIC = re.compile(
    r"\s+(?:the\s+)?(?:video\s*game|pc\s+game|videogame|game)\s*$",
    re.IGNORECASE,
)


def _normalise_query(query: str) -> str:
    """Strip shopping intent words (anywhere) plus trailing platform / generic
    "game" qualifiers so the title resolves cleanly and the cache key tightens
    ("sonic game" -> "sonic", "hades steam key buy cheap pc" -> "hades").  Falls
    back to the original query if the result is empty."""
    cur = _INTENT.sub(" ", query)
    prev = None
    while cur != prev:  # peel stacked trailing qualifiers ("... game on steam pc")
        prev = cur
        cur = _TRAIL_PLATFORM.sub("", cur).rstrip(" -:|·")
        cur = _TRAIL_GENERIC.sub("", cur).rstrip(" -:|·")
    cur = re.sub(r"\s+", " ", cur).strip(" -:|·")
    return cur or query


_CURRENCY_SYMBOL = {
    "GBP": "£", "USD": "$", "EUR": "€", "JPY": "¥",
    "AUD": "A$", "CAD": "C$", "NZD": "NZ$", "INR": "₹", "BRL": "R$",
    "CHF": "CHF ", "SEK": "kr ", "NOK": "kr ", "DKK": "kr ", "PLN": "zł ",
    "MXN": "$", "ZAR": "R", "RUB": "₽", "KRW": "₩", "TRY": "₺",
}

# ISO-3166 country -> ISO-4217 currency for the stores we price against.  Only
# the currencies the upstreams support need to be exact; anything missing falls
# back to the configured default currency.
_COUNTRY_CURRENCY = {
    "GB": "GBP", "US": "USD", "IE": "EUR", "DE": "EUR", "FR": "EUR", "ES": "EUR",
    "IT": "EUR", "NL": "EUR", "BE": "EUR", "AT": "EUR", "PT": "EUR", "FI": "EUR",
    "GR": "EUR", "AU": "AUD", "CA": "CAD", "NZ": "NZD", "IN": "INR", "BR": "BRL",
    "CH": "CHF", "SE": "SEK", "NO": "NOK", "DK": "DKK", "PL": "PLN", "MX": "MXN",
    "ZA": "ZAR", "JP": "JPY", "KR": "KRW", "TR": "TRY",
}

# Last-resort USD-> rates if both the persisted table and the live feed are
# unreachable (order-of-magnitude is all that matters for cheapest-first
# ordering).  These drift over time, so they are only a floor: the fresher
# searx/data/fx_rates.json (refreshed by searxng_extra/update/update_fx_rates.py)
# is preferred whenever present, and a successful live fetch is written back to
# it so the file stays current across restarts and suspend/resume cycles.
_FX_FALLBACK = {
    "USD": 1.0, "GBP": 0.79, "EUR": 0.92, "AUD": 1.52, "CAD": 1.37, "NZD": 1.66,
    "INR": 83.0, "BRL": 5.4, "CHF": 0.88, "SEK": 10.5, "NOK": 10.7, "DKK": 6.9,
    "PLN": 4.0, "MXN": 17.5, "ZAR": 18.5, "JPY": 150.0, "KRW": 1330.0, "TRY": 32.0,
}

# Persisted USD-base rate table shared with the updater script.  Imported lazily
# (and tolerantly) so a missing/corrupt file simply degrades to _FX_FALLBACK.
_FX_FILE = data_dir / "fx_rates.json"


def _setting(key: str, default=None):
    return get_setting(f"game_offers.{key}", default)


# ── In-memory TTL cache (per query+currency) ──────────────────────────────────
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


# ── FX rates (USD base, refreshed daily) ──────────────────────────────────────
_fx: dict = {"rates": {}, "ts": 0.0}
_fx_lock = threading.Lock()


def _load_persisted_fx() -> dict:
    """USD-base rates from searx/data/fx_rates.json, or ``{}`` if the file is
    absent/corrupt.  Written by searxng_extra/update/update_fx_rates.py and by a
    successful live fetch below, so it holds the freshest rates we have seen."""
    try:
        body = json.loads(_FX_FILE.read_text(encoding="utf-8"))
        rates = body.get("rates")
        if isinstance(rates, dict) and abs(float(rates.get("USD", 0)) - 1.0) < 1e-6:
            return {k: float(v) for k, v in rates.items() if isinstance(v, (int, float))}
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001 — a bad file must never break pricing
        logger.info("game_offers persisted FX table unreadable: %s", exc)
    return {}


def _persist_fx(rates: dict) -> None:
    """Write freshly-fetched rates to fx_rates.json so the next cold start (or a
    machine resumed from suspension) seeds from current rates, not the static
    fallback.  Best-effort: a read-only data dir must not break the request."""
    payload = {
        "base": "USD",
        "fetched_at": int(time.time()),
        "source": _FX_URL,
        "rates": rates,
    }
    try:
        _FX_FILE.write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("game_offers could not persist FX table: %s", exc)


def _fx_rates() -> dict:
    """USD-base conversion table, cached in-memory for a day.  Seeds from the
    persisted file (preferred) over the static fallback, refreshes from the live
    feed when the cache is cold/stale, and writes successful fetches back to disk.
    Never raises, so CheapShark's USD prices are always comparable."""
    ttl = float(_setting("fx_ttl", 86400))
    with _fx_lock:
        # On resume from suspension time.time() has jumped forward while ``ts``
        # was frozen, so a long sleep correctly expires the cache and refetches.
        if _fx["rates"] and time.time() - _fx["ts"] < ttl:
            return _fx["rates"]
    # Freshest non-live source first: persisted file, then static fallback.
    rates = {**_FX_FALLBACK, **_load_persisted_fx()}
    try:
        resp = httpx.get(_FX_URL, headers={"User-Agent": _USER_AGENT},
                         timeout=float(_setting("timeout", 6)))
        resp.raise_for_status()
        body = resp.json()
        live = body.get("rates") if isinstance(body, dict) else None
        if isinstance(live, dict) and live.get("USD"):
            rates = {k: float(v) for k, v in live.items() if isinstance(v, (int, float))}
            _persist_fx(rates)
    except Exception as exc:  # noqa: BLE001 — FX is best-effort
        logger.info("game_offers FX feed unavailable, using cached/fallback table: %s", exc)
    with _fx_lock:
        _fx["rates"] = rates
        _fx["ts"] = time.time()
    return rates


def _refresh_fx_on_resume() -> None:
    """Invalidate the in-memory FX cache and re-pull immediately.  Registered with
    the VM resume watchdog so a machine pulled out of suspension prices the first
    search in current rates (and refreshes fx_rates.json) rather than waiting for
    the daily TTL to lapse on some later request."""
    with _fx_lock:
        _fx["ts"] = 0.0
    _fx_rates()


def _register_resume_refresh() -> None:
    """Hook _refresh_fx_on_resume into the suspend/resume watchdog if it is
    available.  Best-effort and guarded: importing the network layer is a no-op
    in the running app (already imported) but must never break plugin loading or
    a standalone import of this module (e.g. under unit tests)."""
    try:
        from searx.network.client import register_resume_callback  # pylint: disable=import-outside-toplevel

        register_resume_callback(_refresh_fx_on_resume)
    except Exception as exc:  # noqa: BLE001
        logger.debug("game_offers: resume-refresh hook unavailable: %s", exc)


def _convert(amount: float, src: str, dst: str) -> "float | None":
    """Convert ``amount`` from currency ``src`` to ``dst`` via the USD table."""
    if amount is None:
        return None
    src = (src or "USD").upper()
    dst = (dst or "USD").upper()
    if src == dst:
        return float(amount)
    rates = _fx_rates()
    rs, rd = rates.get(src), rates.get(dst)
    if not rs or not rd:
        return None
    return float(amount) / rs * rd


def _price_text(value, currency: str) -> str:
    if value is None:
        return ""
    sym = _CURRENCY_SYMBOL.get((currency or "").upper(), "")
    amount = f"{value:.2f}"
    return f"{sym}{amount}" if sym else f"{amount} {currency}".strip()


# ── Title token matching (shared idea with watch_offers) ──────────────────────
_TOKEN_RE = re.compile(r"[0-9a-z]+")

_GATE_STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "to", "in", "on", "at", "for", "with",
    "buy", "price", "prices", "cheap", "cheapest", "deal", "deals", "key", "keys",
    "cd", "steam", "pc", "game", "discount", "sale", "edition",
}

_MIN_CONFIDENCE = 0.5


def _tokens(text: str) -> set:
    folded = unicodedata.normalize("NFKD", (text or "").lower())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return set(_TOKEN_RE.findall(folded))


def _match_confidence(title: str, query: str) -> float:
    """Fraction of the query's content tokens that ``title`` accounts for."""
    q = _tokens(query) - _GATE_STOPWORDS
    if not q:
        return 1.0
    return len(q & _tokens(title)) / len(q)


def _best_match(query: str, candidates: list, title_of) -> "t.Any | None":
    """Pick the candidate whose title best matches the query (IDF-weighted token
    coverage, exact match wins).  ``title_of(candidate)`` returns its title."""
    q_tokens = _tokens(query)
    cand_tokens = [_tokens(title_of(c)) for c in candidates]
    if not candidates:
        return None

    n = len(cand_tokens)
    df: dict = {}
    for ct in cand_tokens:
        for tok in ct:
            df[tok] = df.get(tok, 0) + 1

    def _idf(tok: str) -> float:
        return math.log(n / max(df.get(tok, 0), 1)) if n > 1 else 1.0

    q_weight = sum(_idf(tok) for tok in q_tokens)

    best, best_key = None, None
    for cand, t_tokens in zip(candidates, cand_tokens):
        if q_tokens:
            exact = 1 if q_tokens == t_tokens else 0
            matched = q_tokens & t_tokens
            if q_weight > 0:
                covered = sum(_idf(tok) for tok in matched) / q_weight
            else:
                covered = len(matched) / len(q_tokens)
            extra = len(t_tokens - q_tokens)
        else:
            exact = covered = extra = 0
        key = (exact, covered, -extra)
        if best is None or key > best_key:
            best, best_key = cand, key
    return best


# ── CheapShark (keyless; official stores; USD) ────────────────────────────────
_cs_stores: dict = {"map": {}, "ts": 0.0}
_cs_stores_lock = threading.Lock()


def _cheapshark_stores() -> dict:
    """storeID -> storeName for active stores, cached for a day."""
    with _cs_stores_lock:
        if _cs_stores["map"] and time.time() - _cs_stores["ts"] < 86400:
            return _cs_stores["map"]
    out: dict = {}
    try:
        resp = httpx.get(f"{_CHEAPSHARK}/stores", headers={"User-Agent": _USER_AGENT},
                         timeout=float(_setting("timeout", 6)))
        resp.raise_for_status()
        for s in resp.json() or []:
            if str(s.get("isActive")) in ("1", "True", "true"):
                out[str(s.get("storeID"))] = (s.get("storeName") or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.info("game_offers CheapShark stores unavailable: %s", exc)
    if out:
        with _cs_stores_lock:
            _cs_stores["map"] = out
            _cs_stores["ts"] = time.time()
    return out or _cs_stores["map"]


def _cheapshark(query: str, display_cur: str) -> dict:
    """Returns {"title", "steamAppID", "official": [...], "low": float|None}."""
    timeout = float(_setting("timeout", 6))
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    resp = httpx.get(f"{_CHEAPSHARK}/games", params={"title": query, "limit": 8},
                     headers=headers, timeout=timeout)
    resp.raise_for_status()
    games = resp.json() or []
    game = _best_match(query, games, lambda g: g.get("external") or "")
    if not game:
        return {}
    game_id = game.get("gameID")
    title = (game.get("external") or "").strip()
    if not game_id:
        return {}

    detail = httpx.get(f"{_CHEAPSHARK}/games", params={"id": game_id},
                       headers=headers, timeout=timeout)
    detail.raise_for_status()
    body = detail.json() or {}
    info = body.get("info") or {}
    stores = _cheapshark_stores()

    offers = []
    for deal in body.get("deals") or []:
        usd = _to_float(deal.get("price"))
        if usd is None:
            continue
        price = _convert(usd, "USD", display_cur)
        if price is None:
            continue
        retail = _convert(_to_float(deal.get("retailPrice")), "USD", display_cur)
        cut = _to_int(deal.get("savings"))
        store = stores.get(str(deal.get("storeID")), "")
        deal_id = deal.get("dealID") or ""
        offers.append({
            "store": store or "Store",
            "official": True,
            "price": round(price, 2),
            "currency": display_cur,
            "regular": round(retail, 2) if retail is not None else None,
            "cut": cut,
            "url": f"https://www.cheapshark.com/redirect?dealID={deal_id}" if deal_id else "",
            "source": "cheapshark",
        })

    low_usd = _to_float((body.get("cheapestPriceEver") or {}).get("price"))
    low = _convert(low_usd, "USD", display_cur) if low_usd is not None else None
    return {
        "title": (info.get("title") or title).strip(),
        "steamAppID": info.get("steamAppID") or game.get("steamAppID") or None,
        "official": offers,
        "low": round(low, 2) if low is not None else None,
    }


# ── IsThereAnyDeal (API key; official stores + all-time low) ───────────────────
def _itad(query: str, country: str, display_cur: str, api_key: str) -> dict:
    if not api_key:
        return {}
    timeout = float(_setting("timeout", 6))
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}

    search = httpx.get(f"{_ITAD}/games/search/v1",
                       params={"key": api_key, "title": query, "results": 8},
                       headers=headers, timeout=timeout)
    search.raise_for_status()
    results = [g for g in (search.json() or []) if g.get("type") == "game"] or (search.json() or [])
    game = _best_match(query, results, lambda g: g.get("title") or "")
    if not game or not game.get("id"):
        return {}
    game_id = game["id"]
    title = (game.get("title") or "").strip()

    prices = httpx.post(f"{_ITAD}/games/prices/v3",
                        params={"key": api_key, "country": country, "capacity": 20,
                                "vouchers": "true"},
                        json=[game_id], headers=headers, timeout=timeout)
    prices.raise_for_status()
    rows = prices.json() or []
    row = next((r for r in rows if r.get("id") == game_id), rows[0] if rows else None)
    if not row:
        return {"title": title}

    offers = []
    for deal in row.get("deals") or []:
        p = deal.get("price") or {}
        amount = _to_float(p.get("amount"))
        if amount is None:
            continue
        cur = (p.get("currency") or display_cur).upper()
        price = _convert(amount, cur, display_cur)
        if price is None:
            continue
        reg = (deal.get("regular") or {})
        regular = _convert(_to_float(reg.get("amount")), (reg.get("currency") or cur), display_cur)
        offers.append({
            "store": ((deal.get("shop") or {}).get("name") or "Store").strip(),
            "official": True,  # ITAD lists authorised sellers only
            "price": round(price, 2),
            "currency": display_cur,
            "regular": round(regular, 2) if regular is not None else None,
            "cut": _to_int(deal.get("cut")),
            "url": deal.get("url") or "",
            "source": "itad",
        })

    low = None
    hist = (row.get("historyLow") or {}).get("all") or {}
    amount = _to_float(hist.get("amount"))
    if amount is not None:
        low = _convert(amount, (hist.get("currency") or display_cur), display_cur)
    return {
        "title": title,
        "official": offers,
        "low": round(low, 2) if low is not None else None,
    }


def _to_float(v):
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _to_int(v):
    try:
        return int(round(float(v))) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


# ── Merge ─────────────────────────────────────────────────────────────────────
def _dedupe_cheapest(offers: list) -> list:
    """One row per store (cheapest wins), sorted cheapest-first."""
    best: dict = {}
    for o in offers:
        key = o["store"].strip().lower()
        prev = best.get(key)
        if prev is None or (o["price"] is not None and
                            (prev["price"] is None or o["price"] < prev["price"])):
            best[key] = o
    rows = [o for o in best.values() if o["price"] is not None]
    rows.sort(key=lambda o: o["price"])
    return rows


def _assemble(query: str, results: list, display_cur: str, max_offers: int) -> dict:
    """Combine the per-source dicts into the client payload, gated on a confident
    title match so non-game / wrong-title queries show nothing."""
    sources = [r for r in results if r]
    if not sources:
        return {"type": None, "missing": True}

    # Title: prefer a source that produced offers; fall back to the first.
    title = ""
    for r in sources:
        if r.get("official") and r.get("title"):
            title = r["title"]
            break
    title = title or next((r.get("title") for r in sources if r.get("title")), "")
    if not title or _match_confidence(title, query) < _MIN_CONFIDENCE:
        return {"type": None, "missing": True}

    official_raw = []
    for r in sources:
        official_raw.extend(r.get("official") or [])

    official = _dedupe_cheapest(official_raw)[:max_offers]
    for o in official:
        o["priceText"] = _price_text(o["price"], o["currency"])

    if not official:
        return {"type": None, "missing": True}

    # Historical low: cheapest across the sources that report one.
    lows = [r["low"] for r in sources if r.get("low") is not None]
    historical = None
    if lows:
        lv = min(lows)
        historical = {"price": round(lv, 2), "currency": display_cur,
                      "priceText": _price_text(lv, display_cur)}

    return {
        "type": "game",
        "title": title,
        "currency": display_cur,
        "official": official,
        "historicalLow": historical,
        "steamAppID": next((r.get("steamAppID") for r in sources if r.get("steamAppID")), None),
    }


def _fetch(query: str, country: str, display_cur: str, api_key: str, max_offers: int) -> dict:
    """Query the enabled sources (each isolated so one failure can't sink the box)
    and assemble the payload."""
    def _safe(fn, name):
        try:
            return fn()
        except httpx.HTTPStatusError as exc:
            logger.info("game_offers %s upstream %s", name, exc.response.status_code)
        except Exception as exc:  # noqa: BLE001
            logger.info("game_offers %s error: %s", name, exc)
        return {}

    results = []
    if _setting("enable_cheapshark", True):
        results.append(_safe(lambda: _cheapshark(query, display_cur), "cheapshark"))
    if _setting("enable_itad", True) and api_key:
        results.append(_safe(lambda: _itad(query, country, display_cur, api_key), "itad"))

    return _assemble(query, results, display_cur, max_offers)


def _display_currency(country: str) -> str:
    default_cur = (_setting("default_currency", "GBP") or "GBP").upper()
    return _COUNTRY_CURRENCY.get(country.upper(), default_cur)


def _itad_api_key() -> str:
    return (_setting("itad_api_key", "") or os.environ.get("ITAD_API_KEY", "") or "").strip()


class SXNGPlugin(Plugin):
    """Best-PC-price offer box — server-side CheapShark / ITAD proxy.

    The frontend (serp_enhance.js) is already injected by the card_meta plugin;
    this plugin only provides the JSON endpoint it calls.
    """

    id = "game_offers"
    keywords: list = []

    def __init__(self, plg_cfg: "PluginCfg") -> None:
        super().__init__(plg_cfg)
        self.info = PluginInfo(
            id=self.id,
            name=_("Best PC Price"),
            description=_("Show cheapest PC-game prices (official stores) for game queries"),
            preference_section="general",
        )

    def init(self, app) -> bool:
        from flask import request as freq, Response

        # Refresh FX rates whenever the VM is pulled out of suspension.  Done in
        # the app-init hook (not at import) so it only runs in the live app.
        _register_resume_refresh()

        def _client_ip() -> str:
            return (freq.headers.get("X-Forwarded-For", "")
                    or freq.remote_addr or "").split(",")[0].strip()

        def _country() -> str:
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

        @app.route("/game_offers", methods=["GET"])
        def game_offers():
            query = (freq.args.get("q", "") or "").strip()[:_MAX_QUERY_LEN]
            if not query:
                return Response('{"type":null}', mimetype="application/json", status=400)

            query = _normalise_query(query)
            country = _country()
            display_cur = _display_currency(country)
            cache_key = f"{display_cur}:{country}:{query.lower()}"

            cached = _cache_get(cache_key)
            if cached is not None:
                return Response(json.dumps(cached), mimetype="application/json",
                                headers={"X-Cache": "HIT"})

            if not _check_rate_limit(_client_ip()):
                return Response('{"type":null,"error":"rate_limited"}',
                                mimetype="application/json", status=429)

            max_offers = int(_setting("max_offers", 8))
            try:
                data = _fetch(query, country, display_cur, _itad_api_key(), max_offers)
            except Exception as exc:  # noqa: BLE001 — never 500 the SERP
                logger.warning("game_offers error: %s", exc)
                return Response('{"type":null,"error":"fetch"}',
                                mimetype="application/json", status=502)

            _cache_set(cache_key, data)
            return Response(json.dumps(data), mimetype="application/json",
                            headers={"X-Cache": "MISS"})

        return True
