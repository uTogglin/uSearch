#!/usr/bin/env python
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build the per-domain tracker-density list used by the SERP "shield" panel.

uSearch shows, on each result's shield, how many third-party trackers a site
*typically* loads — the same idea as Kagi's per-result privacy badge.  We don't
crawl the web ourselves, so the numbers come from `whotracks.me`_, Ghostery's
long-running public measurement of online tracking.  Their monthly ``sites.csv``
lists, for the ~10k most popular sites, the average number of distinct trackers
(``trackers``) and tracker companies (``companies``) observed on each.

This script downloads the latest available month, keeps just the two counts per
registrable domain, and writes a compact gzipped JSON map consumed at runtime by
:py:mod:`searx.trackers` (mirroring how :py:mod:`searx.popularity` consumes the
Tranco list).

.. _whotracks.me: https://whotracks.me/

Run::

    python searxng_extra/update/update_trackers.py
"""

import csv
import gzip
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

DATA_DIR = Path(__file__).resolve().parents[2] / "searx" / "data"
OUT_FILE = DATA_DIR / "tracker_radar.json.gz"

# whotracks.me publishes monthly aggregates to a public S3 bucket.  Path-style
# access works without credentials; the "global" partition is the worldwide
# aggregate (per-country partitions also exist).
_BUCKET = "https://s3.amazonaws.com/data.whotracks.me"


def _latest_month() -> str:
    """Find the most recent month that has a ``global/sites.csv``."""
    now = datetime.now(timezone.utc)
    year, month = now.year, now.month
    for _ in range(18):  # walk back up to 18 months
        ym = f"{year:04d}-{month:02d}"
        url = f"{_BUCKET}/{ym}/global/sites.csv"
        try:
            resp = httpx.head(url, timeout=20, follow_redirects=True)
            if resp.status_code == 200:
                return ym
        except httpx.HTTPError:
            pass
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    raise SystemExit("update_trackers: no recent whotracks.me month found")


def _fetch_sites_csv(ym: str) -> str:
    url = f"{_BUCKET}/{ym}/global/sites.csv"
    print(f"update_trackers: downloading {url}", file=sys.stderr)
    resp = httpx.get(url, timeout=120, follow_redirects=True)
    resp.raise_for_status()
    return resp.text


def build() -> dict:
    ym = _latest_month()
    text = _fetch_sites_csv(ym)
    reader = csv.DictReader(io.StringIO(text))

    out: "dict[str, list[int]]" = {}
    for row in reader:
        site = (row.get("site") or "").strip().lower().lstrip(".")
        if not site or "." not in site:
            continue
        try:
            trackers = round(float(row.get("trackers") or 0))
            companies = round(float(row.get("companies") or 0))
        except (TypeError, ValueError):
            continue
        if trackers <= 0:
            continue
        # [tracker_count, company_count]; keep the worst (highest) if a domain
        # appears more than once.
        prev = out.get(site)
        if prev is None or trackers > prev[0]:
            out[site] = [trackers, companies]

    print(f"update_trackers: {len(out)} domains from {ym}", file=sys.stderr)
    return {"_month": ym, "sites": out}


def main() -> None:
    data = build()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")
    with gzip.open(OUT_FILE, "wb") as f:
        f.write(payload)
    print(f"update_trackers: wrote {OUT_FILE} ({OUT_FILE.stat().st_size} bytes)", file=sys.stderr)


if __name__ == "__main__":
    main()
