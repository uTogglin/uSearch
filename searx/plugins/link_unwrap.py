# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unwrap redirector links and de-AMP result URLs.

Two privacy cleanups applied to every result URL, in addition to the tracker
argument stripping done by :py:mod:`searx.plugins.tracker_url_remover`:

* **Redirect unwrapping** -- many engines hand back links wrapped in a
  redirector whose only job is to log the click and pass your identity on to
  the real site (``google.com/url?q=...``, ``l.facebook.com/l.php?u=...``,
  ``out.reddit.com/...?url=...``, ``duckduckgo.com/l/?uddg=...`` ...).  We pull
  the real destination out of the wrapper and link straight to it.

* **De-AMP** -- Google / Cloudflare AMP *cache* URLs
  (``www.google.com/amp/s/...``, ``<pub>.cdn.ampproject.org/c/s/...``) are
  rewritten back to the canonical publisher URL, so you land on the real page
  instead of a Google-hosted copy.  Only the reliably reversible cache/viewer
  hosts are touched; a publisher's own ``/amp/`` page is left alone.

Both run in the ``on_result`` hook via :py:meth:`Result.filter_urls`, the same
mechanism the tracker remover uses.  Trackers exposed on a freshly unwrapped
URL are cleaned by the tracker remover, which runs over the same result.
"""

import logging
import re
import typing as t
from urllib.parse import urlparse, parse_qsl, unquote

from flask_babel import gettext  # pyright: ignore[reportUnknownVariableType]

from . import Plugin, PluginInfo

if t.TYPE_CHECKING:
    import flask
    from searx.search import SearchWithPlugins
    from searx.extended_types import SXNG_Request
    from searx.result_types import Result, LegacyResult  # pyright: ignore[reportPrivateLocalImportUsage]
    from searx.plugins import PluginCfg


log = logging.getLogger("searx.plugins.link_unwrap")

# How many nested redirectors to follow (a redirector pointing at another).
_MAX_DEPTH = 3

# Google's "/url?q=" redirector lives on every ccTLD (google.com, google.co.uk,
# google.de, ...), so it is matched by pattern rather than an explicit host.
_GOOGLE_REDIRECT = re.compile(r"^(www\.)?google(\.[a-z]{2,3}){1,2}$")

# Known redirectors: (host suffixes, required path prefix or "", destination
# query params in priority order).  A param tuple of ("",) means the raw query
# string itself is the destination URL (href.li style).
_REDIRECT_RULES: tuple[tuple[tuple[str, ...], str, tuple[str, ...]], ...] = (
    (("l.facebook.com", "lm.facebook.com"), "/l.php", ("u",)),
    (("l.instagram.com",), "", ("u",)),
    (("l.messenger.com",), "", ("u",)),
    (("out.reddit.com",), "", ("url",)),
    (("vk.com", "away.vk.com"), "/away", ("to",)),
    (("steamcommunity.com",), "/linkfilter", ("url",)),
    (("duckduckgo.com",), "/l", ("uddg",)),
    (("www.youtube.com", "youtube.com"), "/redirect", ("q",)),
    (("t.umblr.com",), "", ("z",)),
    (("href.li", "href.net"), "", ("",)),
)

# AMP cache path: /c/s/<dest> (document, https), /c/<dest> (http), /i/s/<dest>
# (image), /v/s/<dest> (video).  The leading letter is the content type, an
# optional "s/" marks https.
_AMP_CACHE_PATH = re.compile(r"^/[civ]/(s/)?(.+)$", re.IGNORECASE)
# Google AMP viewer: /amp/s/<dest> (https) or /amp/<dest> (http).
_GOOGLE_AMP_PATH = re.compile(r"^/amp/(s/)?(.+)$", re.IGNORECASE)


def _host_of(url: str) -> str:
    return urlparse(url).netloc.lower().split(":", 1)[0]


def _looks_like_url(value: str) -> bool:
    """True if ``value`` is an absolute http(s) URL with a host."""
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _host_match(host: str, suffixes: tuple[str, ...]) -> bool:
    return any(host == s or host.endswith("." + s) for s in suffixes)


def _unwrap(url: str, _depth: int = 0) -> str:
    """Follow a redirector to its destination (recursively, bounded)."""
    if _depth >= _MAX_DEPTH:
        return url

    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":", 1)[0]
    dest = None

    # Google /url?q= / url= across every ccTLD.
    if _GOOGLE_REDIRECT.match(host) and parsed.path == "/url":
        query = dict(parse_qsl(parsed.query))
        cand = unquote(query.get("url") or query.get("q") or "")
        if _looks_like_url(cand):
            dest = cand

    if dest is None:
        for suffixes, path_prefix, params in _REDIRECT_RULES:
            if not _host_match(host, suffixes):
                continue
            if path_prefix and not parsed.path.startswith(path_prefix):
                continue
            if params == ("",):
                # The whole raw query is the destination (href.li style).
                cand = unquote(parsed.query)
                if _looks_like_url(cand):
                    dest = cand
                break
            query = dict(parse_qsl(parsed.query))
            for name in params:
                cand = unquote(query.get(name, ""))
                if _looks_like_url(cand):
                    dest = cand
                    break
            break

    if dest and dest != url:
        return _unwrap(dest, _depth + 1)
    return url


def _deamp(url: str) -> str:
    """Rewrite a Google / Cloudflare AMP cache or viewer URL to the origin."""
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":", 1)[0]

    match = None
    if host == "cdn.ampproject.org" or host.endswith(".cdn.ampproject.org"):
        match = _AMP_CACHE_PATH.match(parsed.path)
    elif host in ("www.google.com", "google.com"):
        match = _GOOGLE_AMP_PATH.match(parsed.path)

    if not match:
        return url

    scheme = "https" if match.group(1) else "http"
    new_url = f"{scheme}://{match.group(2)}"
    # The destination's own query rides along on the cache URL; keep it (the
    # tracker remover strips any utm_* afterwards).  The fragment is viewer
    # state, so drop it.
    if parsed.query:
        new_url += "?" + parsed.query
    return new_url if _looks_like_url(new_url) else url


def _clean(url: str) -> str:
    out = _deamp(_unwrap(url))
    # A redirector can point at an AMP URL, so de-AMP once more after unwrap.
    return _deamp(out)


@t.final
class SXNGPlugin(Plugin):
    """Unwrap redirector links and de-AMP result URLs."""

    id = "link_unwrap"

    def __init__(self, plg_cfg: "PluginCfg") -> None:

        super().__init__(plg_cfg)
        self.info = PluginInfo(
            id=self.id,
            name=gettext("Link unwrapper & de-AMP"),
            description=gettext(
                "Unwrap redirector links (so the destination never learns what you searched)"
                " and rewrite AMP cache URLs back to the original page"
            ),
            preference_section="privacy",
        )

    def on_result(self, request: "SXNG_Request", search: "SearchWithPlugins", result: "Result") -> bool:

        result.filter_urls(self.filter_url_field)
        return True

    @classmethod
    def filter_url_field(cls, result: "Result|LegacyResult", field_name: str, url_src: str) -> bool | str:
        """Returns bool ``True`` to use the URL unchanged.  If the URL is a
        redirector or AMP cache link, the rewritten URL string is returned."""

        if not url_src or not url_src.startswith(("http://", "https://")):
            return True

        new_url = _clean(url_src)
        if new_url != url_src:
            log.debug("link_unwrap (%s): %s -> %s", field_name, url_src, new_url)
            return new_url
        return True
