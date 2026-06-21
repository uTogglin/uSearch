# SPDX-License-Identifier: AGPL-3.0-or-later
"""Redirect result links to privacy-respecting frontends.

Many popular sites (Twitter/X, Reddit, Quora, Imgur, ...) track every visitor,
require JS, or wall content behind a login.  This plugin rewrites result URLs
that point at those sites to a privacy-respecting *frontend* instead -- e.g.
``twitter.com/foo`` becomes ``<nitter>/foo`` -- so the user lands on a
lightweight, trackerless mirror that exposes the same content.

**YouTube is deliberately left untouched.**  No YouTube/youtu.be URL is ever
rewritten, by design.

The rewrite runs in the ``on_result`` hook via :py:meth:`Result.filter_urls`,
the same mechanism :py:mod:`searx.plugins.link_unwrap` and the tracker remover
use.  It runs over the URL *after* those plugins have unwrapped redirectors and
stripped trackers, so it always sees the canonical destination host.

The plugin appears in the *Privacy* tab of the preferences and can be toggled
off per-user.

CONFIGURATION (settings.yml)
----------------------------
Enable / disable the plugin:

.. code:: yaml

   plugins:
     searx.plugins.privacy_redirect.SXNGPlugin:
       active: true

Point each service at the frontend instance you trust (optional -- sensible
public defaults are used when unset).  **Leave a value empty to disable the
redirect for that single service.**

.. code:: yaml

   privacy_redirect:
     nitter:     "https://nitter.net"          # Twitter / X
     redlib:     "https://redlib.catsarch.com" # Reddit
     quetre:     "https://quetre.iket.me"      # Quora
     rimgo:      "https://rimgo.totaldarkness.net"  # Imgur
     proxitok:   "https://proxitok.lunar.icu"  # TikTok
     scribe:     "https://scribe.rip"          # Medium
     libremdb:   "https://libremdb.iket.me"    # IMDb
     dumb:       "https://dumb.lunar.icu"       # Genius
     librarian:  "https://librarian.pussthecat.org"  # Odysee
     wikiless:   "https://wikiless.org"        # Wikipedia
     breezewiki: "https://breezewiki.com"      # Fandom / Wikia
"""

import logging
import typing as t
from urllib.parse import urlparse, urlunparse

from flask_babel import gettext

from searx import get_setting

from . import Plugin, PluginInfo

if t.TYPE_CHECKING:
    from searx.search import SearchWithPlugins
    from searx.extended_types import SXNG_Request
    from searx.result_types import Result, LegacyResult  # pyright: ignore[reportPrivateLocalImportUsage]
    from searx.plugins import PluginCfg


log = logging.getLogger("searx.plugins.privacy_redirect")

# Default frontend instances, one per service "key".  Every key can be
# overridden (or blanked out to disable) via the top-level ``privacy_redirect``
# settings mapping.  Public instances come and go -- operators should point
# these at an instance they trust.
_DEFAULTS: dict[str, str] = {
    "nitter": "https://nitter.net",
    "redlib": "https://redlib.catsarch.com",
    "quetre": "https://quetre.iket.me",
    "rimgo": "https://rimgo.totaldarkness.net",
    "proxitok": "https://proxitok.lunar.icu",
    "scribe": "https://scribe.rip",
    "libremdb": "https://libremdb.iket.me",
    "dumb": "https://dumb.lunar.icu",
    "librarian": "https://librarian.pussthecat.org",
    "wikiless": "https://wikiless.org",
    "breezewiki": "https://breezewiki.com",
}

# Plain host-swap services: any URL whose host equals or is a subdomain of one
# of these suffixes is rebuilt against the configured frontend, keeping the
# path / query / fragment verbatim.  (host suffixes, service key)
#
# NOTE: youtube.com / youtu.be are intentionally NOT listed -- see module docs.
_HOST_SWAP: tuple[tuple[tuple[str, ...], str], ...] = (
    (("twitter.com", "x.com", "mobile.twitter.com"), "nitter"),
    (("reddit.com",), "redlib"),
    (("quora.com",), "quetre"),
    (("imgur.com",), "rimgo"),
    (("tiktok.com",), "proxitok"),
    (("medium.com",), "scribe"),
    (("imdb.com",), "libremdb"),
    (("genius.com",), "dumb"),
    (("odysee.com",), "librarian"),
)


def _host_of(url: str) -> str:
    return urlparse(url).netloc.lower().split(":", 1)[0]


def _host_match(host: str, suffixes: tuple[str, ...]) -> bool:
    return any(host == s or host.endswith("." + s) for s in suffixes)


def _frontend(cfg: dict[str, str], key: str) -> tuple[str, str] | None:
    """Return ``(scheme, netloc)`` of the configured frontend for ``key``, or
    ``None`` when it is unset (the service redirect is then disabled)."""
    base = (cfg.get(key) or "").strip()
    if not base:
        return None
    parsed = urlparse(base)
    if not parsed.scheme or not parsed.netloc:
        return None
    return parsed.scheme, parsed.netloc


def _swap_host(url: str, scheme: str, netloc: str) -> str:
    """Rebuild ``url`` against a new scheme+host, keeping path/query/fragment."""
    p = urlparse(url)
    return urlunparse((scheme, netloc, p.path, p.params, p.query, p.fragment))


def _rewrite_wikipedia(url: str, scheme: str, netloc: str) -> str:
    """``<lang>.wikipedia.org/wiki/X`` -> ``<wikiless>/wiki/X?lang=<lang>``.

    Wikiless serves every language from one host and selects it with a
    ``lang`` query parameter, so the subdomain is carried over there.
    """
    p = urlparse(url)
    host = p.netloc.lower().split(":", 1)[0]
    lang = host.split(".wikipedia.org", 1)[0].split(".")[-1] if ".wikipedia.org" in host else ""
    query = p.query
    if lang and lang not in ("www", "wikipedia"):
        query = (query + "&" if query else "") + f"lang={lang}"
    return urlunparse((scheme, netloc, p.path, p.params, query, p.fragment))


def _rewrite_fandom(url: str, scheme: str, netloc: str) -> str:
    """``<wiki>.fandom.com/wiki/X`` -> ``<breezewiki>/<wiki>/wiki/X``.

    BreezeWiki addresses each Fandom wiki by its subdomain as the first path
    segment, so the subdomain is folded into the path.
    """
    p = urlparse(url)
    host = p.netloc.lower().split(":", 1)[0]
    sub = host.split(".fandom.com", 1)[0]
    if not sub or sub == "www":
        # No identifiable wiki subdomain -- safer to leave the URL alone.
        return url
    path = "/" + sub + (p.path if p.path.startswith("/") else "/" + p.path)
    return urlunparse((scheme, netloc, path, p.params, p.query, p.fragment))


def _clean(url: str, cfg: dict[str, str]) -> str:
    host = _host_of(url)
    if not host:
        return url

    # Plain host-swap services.
    for suffixes, key in _HOST_SWAP:
        if _host_match(host, suffixes):
            front = _frontend(cfg, key)
            return _swap_host(url, *front) if front else url

    # Services that need the subdomain folded into the destination.
    if host == "wikipedia.org" or host.endswith(".wikipedia.org"):
        front = _frontend(cfg, "wikiless")
        return _rewrite_wikipedia(url, *front) if front else url

    if host == "fandom.com" or host.endswith(".fandom.com"):
        front = _frontend(cfg, "breezewiki")
        return _rewrite_fandom(url, *front) if front else url

    return url


@t.final
class SXNGPlugin(Plugin):
    """Redirect result links to privacy-respecting frontends (except YouTube)."""

    id = "privacy_redirect"

    def __init__(self, plg_cfg: "PluginCfg") -> None:

        super().__init__(plg_cfg)
        # Merge configured instances over the built-in defaults once at load.
        self.instances: dict[str, str] = {**_DEFAULTS, **(get_setting("privacy_redirect", {}) or {})}
        self.info = PluginInfo(
            id=self.id,
            name=gettext("Privacy redirect"),
            description=gettext(
                "Open links to Twitter, Reddit, Quora, Imgur and other tracking sites"
                " through privacy-respecting frontends instead (YouTube is left as-is)"
            ),
            preference_section="privacy",
        )

    def on_result(self, request: "SXNG_Request", search: "SearchWithPlugins", result: "Result") -> bool:

        result.filter_urls(self.filter_url_field)
        return True

    def filter_url_field(self, result: "Result|LegacyResult", field_name: str, url_src: str) -> bool | str:
        """Returns ``True`` to keep the URL unchanged, or the rewritten URL
        string when it points at a site with a configured privacy frontend."""

        if not url_src or not url_src.startswith(("http://", "https://")):
            return True

        new_url = _clean(url_src, self.instances)
        if new_url != url_src:
            log.debug("privacy_redirect (%s): %s -> %s", field_name, url_src, new_url)
            return new_url
        return True
