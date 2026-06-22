# SPDX-License-Identifier: AGPL-3.0-or-later
"""
SearXNG Currency Box Plugin — Google/Brave-style FX converter
=============================================================
For a currency-conversion query ("25 gbp to usd", "euro to yen", "$50 in inr")
this provides the data for the converter card rendered by serp_enhance.js at the
very top of the results column: the converted amount, an editable amount, and a
~30-day line chart of the pair's exchange rate.

Where the data comes from
-------------------------
The upstream feed is `Frankfurter <https://frankfurter.dev>`_ — a free, keyless
ECB-data source.  It is pulled **only when the VM is taken out of suspension**
(via the resume watchdog in :origin:`searx/network/client.py`), plus one
non-blocking seed on a cold start, mirroring how game_offers refreshes its FX
table.  A *single* EUR-base request fetches the whole ~30-day matrix for every
ECB currency at once; that matrix is held in memory (and persisted to
:origin:`searx/data/fx_timeseries.json` as a warm seed across restarts).

  * Every conversion's *chart* is derived from that in-memory matrix by
    cross-division (``B/A`` per day), so a request **never touches the network**.
    Exotic pairs the ECB doesn't publish simply show the number without a chart.

  * The *rate* (the big number) is the chart's latest point when the pair is
    covered, else a cross-rate from :origin:`searx/data/fx_rates.json` (the same
    USD-base table game_offers uses, itself refreshed on resume) so the number
    works even for pairs the chart can't cover.

Privacy: the browser only ever talks to THIS origin (GET /currency_convert?q=…);
Frankfurter is queried server-side on resume, never from the request path.

CONFIGURATION (settings.yml)
------------------------------
  plugins:
    searx.plugins.currency_box.SXNGPlugin:
      active: true

  currency_box:
    base_url:   "https://api.frankfurter.dev/v1"
    timeout:    8        # upstream request timeout (seconds)
    days:       30       # chart window, in calendar days
    rate_limit_capacity: 30
    rate_limit_rate:     3.0
"""

import datetime
import json
import logging
import re
import threading
import time
import typing as t
import unicodedata

import httpx
from flask_babel import gettext as _
from searx import get_setting
from searx.data import CURRENCIES, data_dir
from searx.plugins import Plugin, PluginInfo

if t.TYPE_CHECKING:
    from searx.plugins import PluginCfg

logger = logging.getLogger(__name__)

_MAX_QUERY_LEN = 120
_DEFAULT_BASE_URL = "https://api.frankfurter.dev/v1"

_FX_FILE = data_dir / "fx_rates.json"            # USD-base spot table (shared)
_TS_FILE = data_dir / "fx_timeseries.json"       # EUR-base history (this plugin)

# Currency symbols → ISO 4217. ``$`` defaults to USD (the convention the bundled
# online_currency engine also uses).
_SYMBOLS = {
    "$": "USD", "£": "GBP", "€": "EUR", "¥": "JPY", "₹": "INR",
    "₩": "KRW", "₽": "RUB", "₺": "TRY", "₪": "ILS", "₫": "VND",
    "฿": "THB", "₴": "UAH", "₦": "NGN", "﷼": "SAR",
}
# Multi-character symbol prefixes, longest first.
_SYMBOL_PREFIXES = (
    ("nz$", "NZD"), ("a$", "AUD"), ("c$", "CAD"), ("hk$", "HKD"),
    ("r$", "BRL"), ("us$", "USD"),
)

# Colloquial currency words → ISO 4217. Kept in sync with the client-side gate
# in serp_enhance.js / ai_summary.js so that anything the browser recognises as
# a currency query, the server can also resolve.
_COLLOQUIAL = {
    "dollar": "USD", "dollars": "USD", "buck": "USD", "bucks": "USD",
    "pound": "GBP", "pounds": "GBP", "quid": "GBP", "sterling": "GBP",
    "euro": "EUR", "euros": "EUR",
    "yen": "JPY", "rupee": "INR", "rupees": "INR",
    "won": "KRW", "yuan": "CNY", "rmb": "CNY", "renminbi": "CNY",
    "franc": "CHF", "francs": "CHF",
    "peso": "MXN", "pesos": "MXN", "real": "BRL", "reais": "BRL",
    "rand": "ZAR", "ruble": "RUB", "rubles": "RUB", "rouble": "RUB",
    "lira": "TRY", "ringgit": "MYR", "baht": "THB", "shekel": "ILS",
    "dirham": "AED", "riyal": "SAR", "zloty": "PLN", "krona": "SEK",
    "krone": "NOK",
}

# Curated dropdown list (majors) for the card's currency selectors. The current
# from/to are appended server-side if missing, so any valid pair is selectable.
_COMMON = [
    "USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "CNY", "HKD", "NZD",
    "SGD", "INR", "KRW", "MXN", "BRL", "ZAR", "RUB", "TRY", "SEK", "NOK",
    "DKK", "PLN", "THB", "IDR", "HUF", "CZK", "ILS", "AED", "SAR", "PHP",
    "MYR", "RON",
]

_SEP = r"(?:into|in|to|=|->|→|–|—)"
_QUERY_RE = re.compile(
    r"^\s*(?:convert\s+|how\s+much\s+is\s+|whats?\s+is\s+|what'?s\s+)?"
    r"(?P<left>.+?)\s+" + _SEP + r"\s+(?P<right>.+?)\s*$",
    re.IGNORECASE,
)
_AMOUNT_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _setting(key: str, default=None):
    return get_setting(f"currency_box.{key}", default)


# ── Local USD-base spot table (fallback rate for non-ECB pairs) ────────────────
_fx_lock = threading.Lock()
_fx_cache: dict = {"rates": None, "mtime": 0.0}


def _load_fx() -> dict:
    """Read fx_rates.json once, reloading only when the file changes on disk."""
    with _fx_lock:
        try:
            mtime = _FX_FILE.stat().st_mtime
        except OSError:
            return _fx_cache["rates"] or {}
        if _fx_cache["rates"] is None or mtime != _fx_cache["mtime"]:
            try:
                with open(_FX_FILE, encoding="utf-8") as f:
                    data = json.load(f)
                rates = {k.upper(): float(v) for k, v in (data.get("rates") or {}).items()}
                rates["USD"] = 1.0
                _fx_cache["rates"] = rates
                _fx_cache["mtime"] = mtime
            except Exception as exc:  # noqa: BLE001
                logger.warning("currency_box: cannot read fx_rates.json: %s", exc)
                return _fx_cache["rates"] or {}
        return _fx_cache["rates"] or {}


def _cross_rate(frm: str, to: str) -> "float | None":
    """Value of 1 ``frm`` in ``to`` via the USD-base table (rates are X per USD)."""
    if frm == to:
        return 1.0
    rates = _load_fx()
    rf, rt = rates.get(frm), rates.get(to)
    if not rf or not rt:
        return None
    return rt / rf


# ── EUR-base historical matrix (Frankfurter; refreshed only on resume) ─────────
# Held in memory as {date: {CUR: EUR→CUR rate}}. EUR is added as 1.0 so EUR pairs
# work too. Any pair's series is derived by cross-division, so the request path
# never calls upstream.
_ts_lock = threading.Lock()
_ts: dict = {"rates": {}, "dates": [], "fetched_at": 0.0}
_seeded = False  # guards the one-time cold-start seed


def _persist_ts(rates: dict) -> None:
    """Write the fetched matrix to disk as a warm seed across restarts.
    Best-effort: a read-only data dir must never break anything."""
    payload = {
        "base": "EUR",
        "fetched_at": int(time.time()),
        "source": _DEFAULT_BASE_URL,
        "rates": rates,
    }
    try:
        _TS_FILE.write_text(
            json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("currency_box: could not persist timeseries: %s", exc)


def _normalise_matrix(raw: dict) -> dict:
    """Coerce a ``{date: {CUR: val}}`` mapping to floats and add EUR=1.0."""
    out: dict = {}
    for day, vals in (raw or {}).items():
        if isinstance(vals, dict):
            row = {k.upper(): float(v) for k, v in vals.items() if isinstance(v, (int, float))}
            row.setdefault("EUR", 1.0)
            out[day] = row
    return out


def _load_persisted_ts() -> bool:
    """Load the persisted matrix into memory. ``True`` if anything was loaded."""
    try:
        body = json.loads(_TS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except Exception as exc:  # noqa: BLE001
        logger.info("currency_box: persisted timeseries unreadable: %s", exc)
        return False
    rates = _normalise_matrix(body.get("rates") or {})
    if not rates:
        return False
    with _ts_lock:
        _ts["rates"] = rates
        _ts["dates"] = sorted(rates)
        _ts["fetched_at"] = float(body.get("fetched_at") or 0)
    return True


def _fetch_matrix(days: int) -> dict:
    """One EUR-base request for the whole ~``days`` matrix of every ECB currency.
    Raises on transport/HTTP error; returns ``{}`` on an empty/odd response."""
    end = datetime.date.today()
    start = end - datetime.timedelta(days=days)
    base_url = (_setting("base_url", _DEFAULT_BASE_URL) or _DEFAULT_BASE_URL).rstrip("/")
    resp = httpx.get(
        f"{base_url}/{start.isoformat()}..{end.isoformat()}",
        headers={"User-Agent": "uSearch-currency-box/1.0 (+https://search.utoggl.in)"},
        timeout=float(_setting("timeout", 8)),
        follow_redirects=True,
    )
    resp.raise_for_status()
    return _normalise_matrix((resp.json() or {}).get("rates") or {})


def _refresh_timeseries() -> bool:
    """Pull a fresh EUR-base matrix, swap it into memory, and persist it.
    Best-effort: on any failure the previous in-memory matrix is kept."""
    days = int(_setting("days", 30))
    try:
        rates = _fetch_matrix(days)
    except Exception as exc:  # noqa: BLE001 — chart is optional
        logger.info("currency_box: timeseries refresh failed: %s", exc)
        return False
    if not rates:
        return False
    with _ts_lock:
        _ts["rates"] = rates
        _ts["dates"] = sorted(rates)
        _ts["fetched_at"] = time.time()
    _persist_ts(rates)
    logger.info("currency_box: refreshed FX history (%d days)", len(rates))
    return True


def _ensure_seed() -> None:
    """At app init: load the persisted matrix, and if it's missing or a day-plus
    stale, kick off ONE background refresh so startup never blocks on the network.
    Routine refreshes after this happen only on VM resume."""
    global _seeded  # pylint: disable=global-statement
    if _seeded:
        return
    _seeded = True
    _load_persisted_ts()
    with _ts_lock:
        have = bool(_ts["rates"])
        age = time.time() - _ts["fetched_at"]
    if not have or age > 86400:
        threading.Thread(target=_refresh_timeseries,
                         name="currency_box_seed", daemon=True).start()


def _register_resume_refresh() -> None:
    """Hook _refresh_timeseries into the suspend/resume watchdog if available.
    Best-effort and guarded so it never breaks plugin loading or a standalone
    import of this module (e.g. under unit tests)."""
    try:
        from searx.network.client import register_resume_callback  # pylint: disable=import-outside-toplevel

        register_resume_callback(_refresh_timeseries)
    except Exception as exc:  # noqa: BLE001
        logger.debug("currency_box: resume-refresh hook unavailable: %s", exc)


def _pair_series(frm: str, to: str) -> "list[dict]":
    """Daily ``frm→to`` rates from the in-memory EUR-base matrix, oldest first.
    ``[]`` when either leg isn't covered (no network call is ever made)."""
    if frm == to:
        return []
    with _ts_lock:
        rates = _ts["rates"]
        dates = list(_ts["dates"])
    if not rates:
        return []
    out = []
    for day in dates:
        row = rates.get(day) or {}
        a, b = row.get(frm), row.get(to)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and a:
            out.append({"d": day, "v": round(b / a, 6)})
    return out


# ── Currency-name resolution ──────────────────────────────────────────────────

def _normalize_name(name: str) -> str:
    name = name.strip().lower().replace("-", " ")
    name = re.sub(" +", " ", name)
    return unicodedata.normalize("NFKD", name)


def _resolve_currency(text: str) -> "str | None":
    """Resolve a free-text currency fragment to an ISO 4217 code, or None."""
    text = text.strip()
    if not text:
        return None
    low = text.lower()
    for pre, iso in _SYMBOL_PREFIXES:
        if low.startswith(pre) or low.endswith(pre):
            return iso
    for sym, iso in _SYMBOLS.items():
        if sym in text:
            return iso

    token = text.strip(" .").strip()
    if not token:
        return None
    up = token.upper()
    if len(up) <= 3 and CURRENCIES.is_iso4217(up):
        return up
    if low in _COLLOQUIAL:
        return _COLLOQUIAL[low]

    name = _normalize_name(token)
    iso = CURRENCIES.name_to_iso4217(name)
    if iso:
        return iso
    # The currency may be one token among trailing noise ("us dollar",
    # "japanese yen", "usd today") — scan from the end for a colloquial word or
    # a bare ISO code so the server resolves anything the client gate accepts.
    for word in reversed(name.split()):
        if word in _COLLOQUIAL:
            return _COLLOQUIAL[word]
        wu = word.upper()
        if len(wu) == 3 and CURRENCIES.is_iso4217(wu):
            return wu
    if name.endswith("s"):
        iso = CURRENCIES.name_to_iso4217(name[:-1])
    return iso


def _parse_query(query: str) -> "tuple[float, str, str] | None":
    """Return ``(amount, from_iso, to_iso)`` for a currency query, else None."""
    m = _QUERY_RE.match(query)
    if not m:
        return None
    left, right = m.group("left"), m.group("right")

    amount = 1.0
    am = _AMOUNT_RE.search(left)
    if am:
        try:
            amount = float(am.group(0).replace(",", ""))
        except ValueError:
            amount = 1.0
        left = (left[: am.start()] + left[am.end():]).strip()

    frm = _resolve_currency(left)
    to = _resolve_currency(right)
    if not frm or not to or amount <= 0:
        return None
    return amount, frm, to


# ── Currency display names + dropdown list ────────────────────────────────────
_names_lock = threading.Lock()
_names_cache: "dict | None" = None


def _currency_name(iso: str) -> str:
    return CURRENCIES.iso4217_to_name(iso, "en") or iso


def _dropdown(frm: str, to: str) -> "list[list[str]]":
    """Curated majors list plus the active pair, as ``[[code, name], ...]``."""
    global _names_cache  # pylint: disable=global-statement
    with _names_lock:
        if _names_cache is None:
            _names_cache = {c: _currency_name(c) for c in _COMMON}
    out = dict(_names_cache)
    for iso in (frm, to):
        if iso not in out:
            out[iso] = _currency_name(iso)
    return [[c, n] for c, n in out.items()]


def _build(amount: float, frm: str, to: str) -> "dict | None":
    """Assemble the converter payload from in-memory data (no network call)."""
    series = _pair_series(frm, to)
    # Prefer the chart's latest point as the spot rate so the big number lines up
    # with the right edge of the graph; fall back to the local USD-base cross.
    rate = series[-1]["v"] if series else _cross_rate(frm, to)
    if rate is None:
        return None
    return {
        "type": "currency",
        "from": frm,
        "to": to,
        "fromName": _currency_name(frm),
        "toName": _currency_name(to),
        "amount": amount,
        "rate": rate,
        "result": round(amount * rate, 4),
        "series": series,
        "currencies": _dropdown(frm, to),
    }


# ── Per-IP token-bucket rate limiter ─────────────────────────────────────────
_rate_buckets: dict = {}
_rate_lock = threading.Lock()


def _check_rate_limit(ip: str) -> bool:
    capacity = float(_setting("rate_limit_capacity", 30))
    rate = float(_setting("rate_limit_rate", 3.0))
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


class SXNGPlugin(Plugin):
    """Currency converter card — in-memory ECB history + local FX fallback.

    The frontend (serp_enhance.js) is injected by the card_meta plugin; this
    plugin only provides the JSON endpoint it calls.  Upstream is pulled only on
    VM resume (plus a background cold-start seed), never from the request path.
    """

    id = "currency_box"
    keywords: list = []

    def __init__(self, plg_cfg: "PluginCfg") -> None:
        super().__init__(plg_cfg)
        self.info = PluginInfo(
            id=self.id,
            name=_("Currency Converter"),
            description=_("Show a converter card with a rate chart for currency queries"),
            preference_section="general",
        )

    def init(self, app) -> bool:
        from flask import request as freq, Response

        # Refresh the FX history whenever the VM is pulled out of suspension, and
        # seed it once in the background on cold start.  Done in the app-init hook
        # (not at import) so it only runs in the live app.
        _register_resume_refresh()
        _ensure_seed()

        def _client_ip() -> str:
            return (freq.headers.get("X-Forwarded-For", "")
                    or freq.remote_addr or "").split(",")[0].strip()

        @app.route("/currency_convert", methods=["GET"])
        def currency_convert():
            query = (freq.args.get("q", "") or "").strip()[:_MAX_QUERY_LEN]
            if not query:
                return Response('{"type":null}', mimetype="application/json", status=400)

            parsed = _parse_query(query)
            if parsed is None:
                return Response('{"type":null}', mimetype="application/json")
            amount, frm, to = parsed

            if not _check_rate_limit(_client_ip()):
                return Response('{"type":null,"error":"rate_limited"}',
                                mimetype="application/json", status=429)

            data = _build(amount, frm, to)
            if data is None:
                return Response('{"type":null}', mimetype="application/json")
            return Response(json.dumps(data), mimetype="application/json")

        return True
