#!/usr/bin/env python
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Update the domain-popularity list used as a ranking signal.

uSearch fork: SearXNG ranks main results purely by cross-engine position
consensus, with no notion of how popular / trafficked a site actually is.  To
give a generic authority nudge (so e.g. ``netflix.com`` outranks a random
print-on-demand shop that happens to appear in two engines) we fold in a
domain-popularity score derived from the Tranco list.

`Tranco <https://tranco-list.eu>`_ is a research-grade ranking that aggregates
several popularity sources (Cisco Umbrella DNS volume, Cloudflare Radar, Chrome
UX report, Majestic) into one manipulation-resistant list of registrable
domains.  We keep the top :py:obj:`KEEP` domains, one per line in rank order
(line *N* == rank *N*), gzip-compressed.

Run::

    python searxng_extra/update/update_popularity.py
"""

import gzip
import io
import zipfile

import httpx

from searx.data import data_dir

DATA_FILE = data_dir / "domain_popularity.txt.gz"

# Latest daily Tranco list (rank,domain CSV inside a zip).
TRANCO_URL = "https://tranco-list.eu/top-1m.csv.zip"

# How many of the top domains to keep.  The boost curve is logarithmic, so the
# long tail past ~200k contributes almost nothing; keeping 200k bounds memory
# (~30 MB resident) while covering every site that realistically shows up in
# mainstream search results.
KEEP = 200_000


def fetch_domains() -> list[str]:
    resp = httpx.get(TRANCO_URL, timeout=120, follow_redirects=True)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        name = zf.namelist()[0]
        raw = zf.read(name).decode("utf-8")

    domains: list[str] = []
    for line in raw.splitlines():
        # format: "<rank>,<domain>"
        _, _, domain = line.partition(",")
        domain = domain.strip().lower()
        if domain:
            domains.append(domain)
        if len(domains) >= KEEP:
            break
    return domains


def main() -> None:
    domains = fetch_domains()
    blob = ("\n".join(domains) + "\n").encode("utf-8")
    with gzip.open(DATA_FILE, "wb") as f:
        f.write(blob)
    print(f"wrote {len(domains):,} domains -> {DATA_FILE}")


if __name__ == "__main__":
    main()
