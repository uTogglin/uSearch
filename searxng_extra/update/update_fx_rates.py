#!/usr/bin/env python
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fetch fresh USD-base foreign-exchange rates for local-currency conversion.

Output file: :origin:`searx/data/fx_rates.json`

This is the persisted seed used by :origin:`searx/plugins/game_offers.py` to
convert CheapShark's USD prices into the visitor's local currency.  Without it
the plugin falls back to a small table of rates hard-coded at release time,
which drift further out of date the longer the deploy runs.

The data source is the free, keyless `open.er-api.com <https://www.exchangerate-api.com/docs/free>`_
feed (USD base, refreshed daily upstream).  Run it from CI/cron, or — as wired
up here — every time the host is pulled out of suspension, so a machine that
sleeps for a week wakes up with current rates instead of stale ones::

    python searxng_extra/update/update_fx_rates.py

The script never overwrites a good file with garbage: a malformed or partial
response is rejected and the previous ``fx_rates.json`` is left untouched.
"""

# pylint: disable=invalid-name

import json
import sys

import httpx

from searx.data import data_dir

FX_URL = "https://open.er-api.com/v6/latest/USD"

OUTPUT_FILE = data_dir / "fx_rates.json"

# A response missing these has to be treated as broken — converting against it
# would silently mis-price every offer.
_REQUIRED = ("USD", "EUR", "GBP")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def fetch_rates() -> dict:
    """Return ``{"base": "USD", "fetched_at": <unix>, "source": <url>,
    "rates": {ISO4217: float, ...}}`` or raise on a response we can't trust."""
    resp = httpx.get(FX_URL, headers={"User-Agent": _USER_AGENT}, timeout=15)
    resp.raise_for_status()
    body = resp.json()

    if not isinstance(body, dict) or body.get("result") != "success":
        raise ValueError(f"FX feed did not report success: {body.get('result')!r}")

    raw = body.get("rates")
    if not isinstance(raw, dict):
        raise ValueError("FX feed response has no 'rates' object")

    rates = {k: float(v) for k, v in raw.items() if isinstance(v, (int, float))}

    # USD is the base, so it must be exactly 1.0; if the feed disagrees the whole
    # table is suspect.
    if abs(rates.get("USD", 0.0) - 1.0) > 1e-6:
        raise ValueError(f"USD base rate is {rates.get('USD')!r}, expected 1.0")

    missing = [c for c in _REQUIRED if c not in rates]
    if missing:
        raise ValueError(f"FX feed missing required currencies: {missing}")

    return {
        "base": "USD",
        # `time_last_update_unix` is when upstream refreshed; fall back to our
        # own fetch time so the consumer's freshness check always has a value.
        "fetched_at": int(body.get("time_last_update_unix") or 0),
        "source": FX_URL,
        "rates": rates,
    }


def main() -> int:
    try:
        data = fetch_rates()
    except Exception as exc:  # noqa: BLE001 — never trample a good file on error
        print(f"update_fx_rates: FX feed unavailable, keeping existing data: {exc}", file=sys.stderr)
        return 1

    OUTPUT_FILE.write_text(
        json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"update_fx_rates: wrote {len(data['rates'])} rates to {OUTPUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
