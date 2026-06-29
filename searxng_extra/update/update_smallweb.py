#!/usr/bin/env python
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Vendor Kagi's "Small Web" domain list for the built-in *Small Web* lens.

uSearch ships a built-in "favor" lens that floats independent blogs and personal
sites above SEO-spam — the same idea as Kagi's `Small Web`_ initiative, which
maintains a large, community-curated list of such sites.  Rather than re-curate
the web ourselves, we vendor Kagi's public ``smallweb.txt`` (a newline list of
site URLs / domains), reduce every entry to its bare registrable host (no
scheme, path or ``www.``), de-duplicate, sort, and write a compact JSON array.

The result lands in ``client/simple/static/data/smallweb.json``.  Because it
lives under ``client/simple/static/`` the vite build copies it to
``searx/static/themes/simple/data/smallweb.json``, served at
``/static/themes/simple/data/smallweb.json`` and fetched lazily (and cached in
localStorage) by ``serp_enhance.js`` only when the Small Web lens is active.

.. _Small Web: https://github.com/kagisearch/smallweb

Run::

    python searxng_extra/update/update_smallweb.py
"""

import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote

import httpx

# A bare hostname: ASCII letters/digits/hyphen/dot plus any non-ASCII (IDN) char.
_HOST_RE = re.compile(r"^[a-z0-9¡-￿.\-]+$")

# client/simple/static/data/smallweb.json (sibling of the served theme copy that
# vite generates). parents[2] == repo root.
OUT_FILE = (
    Path(__file__).resolve().parents[2]
    / "client"
    / "simple"
    / "static"
    / "data"
    / "smallweb.json"
)

# Kagi publishes the curated list as a flat newline file of site URLs/domains.
SOURCE_URL = "https://raw.githubusercontent.com/kagisearch/smallweb/main/smallweb.txt"


def clean_domain(line: str) -> str:
    """Reduce one raw line to a bare lowercase host (drop scheme/path/www).

    Mirrors ``cleanDomain()`` in serp_enhance.js so the vendored hosts match
    exactly what the client subdomain-walk compares against.
    """
    s = (line or "").strip().lower()
    if not s or s.startswith("#"):
        return ""
    # strip scheme
    if "://" in s:
        s = s.split("://", 1)[1]
    # strip path / query / fragment — host is everything up to the first slash
    s = s.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    # strip credentials and port
    s = s.split("@")[-1].split(":", 1)[0]
    # some IDN hosts arrive percent-encoded — decode then re-normalise
    if "%" in s:
        try:
            s = unquote(s)
        except Exception:  # noqa: BLE001
            pass
    if s.startswith("www."):
        s = s[4:]
    s = s.strip().strip(".")
    # crude sanity check: must look like a domain (a dot, no whitespace) and
    # contain only hostname-legal characters
    if not s or "." not in s or not _HOST_RE.match(s):
        return ""
    return s


def fetch_list(url: str) -> list[str]:
    resp = httpx.get(url, timeout=60, follow_redirects=True)
    resp.raise_for_status()
    return resp.text.splitlines()


def build(lines: list[str]) -> list[str]:
    hosts = set()
    for line in lines:
        host = clean_domain(line)
        if host:
            hosts.add(host)
    return sorted(hosts)


def main() -> int:
    try:
        lines = fetch_list(SOURCE_URL)
    except httpx.HTTPError as exc:
        print(f"update_smallweb: download failed: {exc}", file=sys.stderr)
        return 1

    domains = build(lines)
    if not domains:
        print("update_smallweb: produced an empty list — aborting", file=sys.stderr)
        return 1

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(domains, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"update_smallweb: wrote {len(domains)} domains -> {OUT_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
