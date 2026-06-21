# SPDX-License-Identifier: AGPL-3.0-or-later
"""Redirect YouTube result links to an Invidious frontend.

This is the YouTube counterpart to :py:mod:`searx.plugins.privacy_redirect`
(which deliberately leaves YouTube alone).  It is a **separate** plugin so that
YouTube redirection can be toggled on/off independently of the other privacy
frontends -- some users want a trackerless YouTube mirror, others would rather
keep the canonical site.

``youtube.com/watch?v=ID`` becomes ``<invidious>/watch?v=ID`` and the short
``youtu.be/ID`` form is expanded to ``<invidious>/watch?v=ID``; every other
YouTube path (``/channel/...``, ``/playlist?list=...``, ``/shorts/...``,
``/@handle``) is carried over verbatim, since Invidious mirrors the same URL
layout.

The rewrite runs in the ``on_result`` hook via :py:meth:`Result.filter_urls`,
the same mechanism :py:mod:`searx.plugins.link_unwrap` and the tracker remover
use, so it sees the canonical destination after redirectors are unwrapped.

The plugin appears in the *Privacy* tab of the preferences and can be toggled
off per-user.

CONFIGURATION (settings.yml)
----------------------------
Enable / disable the plugin:

.. code:: yaml

   plugins:
     searx.plugins.youtube_redirect.SXNGPlugin:
       active: true

Point it at the Invidious instance you trust (optional -- a sensible public
default is used when unset).  Leave the value empty ("") to disable the
redirect without disabling the plugin.

.. code:: yaml

   youtube_redirect:
     invidious: "https://yewtu.be"
"""

import logging
import typing as t
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

from flask_babel import gettext

from searx import get_setting

from . import Plugin, PluginInfo

if t.TYPE_CHECKING:
    from searx.search import SearchWithPlugins
    from searx.extended_types import SXNG_Request
    from searx.result_types import Result, LegacyResult  # pyright: ignore[reportPrivateLocalImportUsage]
    from searx.plugins import PluginCfg


log = logging.getLogger("searx.plugins.youtube_redirect")

# Default Invidious instance.  Public instances come and go -- operators should
# point this at an instance they trust via the ``youtube_redirect`` setting.
_DEFAULT_INVIDIOUS = "https://yewtu.be"

# Hosts treated as YouTube.  youtu.be is handled specially (path -> ?v=ID).
_YOUTUBE_HOSTS = ("youtube.com", "youtube-nocookie.com")


def _host_of(url: str) -> str:
    return urlparse(url).netloc.lower().split(":", 1)[0]


def _host_match(host: str, suffixes: tuple[str, ...]) -> bool:
    return any(host == s or host.endswith("." + s) for s in suffixes)


def _frontend(base: str) -> tuple[str, str] | None:
    """Return ``(scheme, netloc)`` of the configured Invidious instance, or
    ``None`` when it is unset / malformed (the redirect is then disabled)."""
    base = (base or "").strip()
    if not base:
        return None
    parsed = urlparse(base)
    if not parsed.scheme or not parsed.netloc:
        return None
    return parsed.scheme, parsed.netloc


def _rewrite(url: str, scheme: str, netloc: str) -> str:
    p = urlparse(url)
    host = p.netloc.lower().split(":", 1)[0]

    if host == "youtu.be" or host.endswith(".youtu.be"):
        # Short link: the path IS the video id -> /watch?v=<id>.
        video_id = p.path.lstrip("/").split("/", 1)[0]
        if not video_id:
            return url
        query = dict(parse_qsl(p.query))
        query["v"] = video_id
        return urlunparse((scheme, netloc, "/watch", "", urlencode(query), p.fragment))

    # youtube.com / youtube-nocookie.com: Invidious mirrors the same paths.
    return urlunparse((scheme, netloc, p.path, p.params, p.query, p.fragment))


def _is_youtube(host: str) -> bool:
    return _host_match(host, _YOUTUBE_HOSTS) or host == "youtu.be" or host.endswith(".youtu.be")


@t.final
class SXNGPlugin(Plugin):
    """Redirect YouTube result links to an Invidious frontend."""

    id = "youtube_redirect"

    def __init__(self, plg_cfg: "PluginCfg") -> None:

        super().__init__(plg_cfg)
        cfg = get_setting("youtube_redirect", {}) or {}
        self.invidious: str = cfg.get("invidious") or _DEFAULT_INVIDIOUS
        self.info = PluginInfo(
            id=self.id,
            name=gettext("YouTube redirect"),
            description=gettext(
                "Open YouTube links through a privacy-respecting Invidious frontend"
            ),
            preference_section="privacy",
        )

    def on_result(self, request: "SXNG_Request", search: "SearchWithPlugins", result: "Result") -> bool:

        result.filter_urls(self.filter_url_field)
        return True

    def filter_url_field(self, result: "Result|LegacyResult", field_name: str, url_src: str) -> bool | str:
        """Returns ``True`` to keep the URL unchanged, or the rewritten URL
        string when it points at YouTube and an Invidious instance is set."""

        if not url_src or not url_src.startswith(("http://", "https://")):
            return True

        if not _is_youtube(_host_of(url_src)):
            return True

        front = _frontend(self.invidious)
        if not front:
            return True

        new_url = _rewrite(url_src, *front)
        if new_url != url_src:
            log.debug("youtube_redirect (%s): %s -> %s", field_name, url_src, new_url)
            return new_url
        return True
