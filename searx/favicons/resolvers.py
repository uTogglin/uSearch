# SPDX-License-Identifier: AGPL-3.0-or-later
"""Implementations of the favicon *resolvers* that are available in the favicon
proxy by default.  A *resolver* is a function that obtains the favicon from an
external source.  The *resolver* function receives two arguments (``domain,
timeout``) and returns a tuple ``(data, mime)``.

"""


__all__ = ["DEFAULT_RESOLVER_MAP", "allesedv", "duckduckgo", "google", "yandex", "direct", "multi"]

from typing import Callable
from urllib.parse import urljoin

from httpx import HTTPError
from lxml import html as lxml_html

from searx import network
from searx import logger
from searx.exceptions import SearxEngineResponseException

DEFAULT_RESOLVER_MAP: dict[str, Callable]
logger = logger.getChild('favicons.resolvers')

# HTTP statuses that mean "ask again later", not "this domain has no favicon".
# A transient failure (rate-limit, gateway/server error) must never be treated as
# a definitive miss: the caller caches misses, so caching one of these would blank
# the favicon for the whole cache hold time (30 days) over a momentary blip. This
# is exactly how big domains (e.g. linkedin.com, play.google.com) end up icon-less
# — the favicon provider rate-limits our datacenter IP once and the empty answer
# sticks. We raise on these instead so the resolver chain falls through / the
# result is left uncached and retried on the next request.
_TRANSIENT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524})


def _req_args(**kwargs):
    # add the request arguments from the searx.network
    d = {"raise_for_httperror": False}
    d.update(kwargs)
    return d


def _fetch(url: str, timeout: int):
    """GET ``url`` for a favicon resolver.

    Returns the response on HTTP 200, or ``None`` on a *definitive* miss (e.g. a
    404 — the provider is sure it has no icon, which is safe to cache). Raises
    :py:class:`SearxEngineResponseException` on a *transient* failure (rate-limit
    / gateway / server error, see :py:data:`_TRANSIENT_STATUS`) so the caller does
    not cache an empty answer it would be stuck with for the cache hold time.
    Network-level errors (timeouts, connection resets) already raise out of
    :py:func:`network.get` as ``HTTPError`` and are handled the same way upstream.
    """
    logger.debug("fetch favicon from: %s", url)
    response = network.get(url, **_req_args(timeout=timeout))
    if response is None:
        raise SearxEngineResponseException(f"no response from favicon source: {url}")
    if response.status_code == 200:
        return response
    if response.status_code in _TRANSIENT_STATUS:
        raise SearxEngineResponseException(f"transient {response.status_code} from favicon source: {url}")
    return None


def allesedv(domain: str, timeout: int) -> tuple[None | bytes, None | str]:
    """Favicon Resolver from allesedv.com / https://favicon.allesedv.com/"""
    url = f"https://f1.allesedv.com/32/{domain}"

    # will just return a 200 regardless of the favicon existing or not; an
    # image/gif is its "no favicon" sentinel. sometimes correct size, sometimes not
    response = _fetch(url, timeout)
    if response is None:
        return None, None
    mime = response.headers['Content-Type']
    if mime == 'image/gif':
        return None, None
    return response.content, mime


def duckduckgo(domain: str, timeout: int) -> tuple[None | bytes, None | str]:
    """Favicon Resolver from duckduckgo.com / https://blog.jim-nielsen.com/2021/displaying-favicons-for-any-domain/"""
    url = f"https://icons.duckduckgo.com/ip2/{domain}.ico"

    # will return a 404 if the favicon does not exist and a 200 (32x32 png) if it does
    response = _fetch(url, timeout)
    if response is None:
        return None, None
    return response.content, response.headers['Content-Type']


def google(domain: str, timeout: int) -> tuple[None | bytes, None | str]:
    """Favicon Resolver from google.com"""

    # URL https://www.google.com/s2/favicons?sz=32&domain={domain}" will be
    # redirected (HTTP 301 Moved Permanently) to t1.gstatic.com/faviconV2:
    url = (
        f"https://t1.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL"
        f"&url=https://{domain}&size=32"
    )

    # will return a 404 if the favicon does not exist and a 200 (32x32 png) if it does
    response = _fetch(url, timeout)
    if response is None:
        return None, None
    return response.content, response.headers['Content-Type']


def yandex(domain: str, timeout: int) -> tuple[None | bytes, None | str]:
    """Favicon Resolver from yandex.com"""
    url = f"https://favicon.yandex.net/favicon/{domain}"

    # api will respond with a 16x16 png image, if it doesn't exist, it will be a
    # 1x1 png image (70 bytes)
    response = _fetch(url, timeout)
    if response is None or len(response.content) <= 70:
        return None, None
    return response.content, response.headers['Content-Type']


# Maximum favicon size (bytes) the :py:func:`direct` resolver will accept. The
# aggregators return small 32x32 PNGs; a site's own favicon.ico can be a
# multi-resolution 100 KB+ blob. Favicons are inlined as base64 into the search
# HTML, so an oversized icon bloats every result row — reject it here and let the
# chain fall through to a small aggregator icon instead. Kept comfortably under
# the cache's BLOB_MAX_BYTES so an accepted direct icon is also cacheable.
_DIRECT_MAX_BYTES = 30 * 1024

# rel tokens (space-separated, in any order) that mark a <link> as a favicon.
_ICON_REL_TOKENS = frozenset(
    ("icon", "shortcut", "apple-touch-icon", "apple-touch-icon-precomposed", "mask-icon", "fluid-icon")
)


def _sniff_image_mime(data: bytes, header_mime: str | None) -> str | None:
    """Return an image mime-type for ``data`` or ``None`` if it is not an image.

    Trusts an ``image/*`` Content-Type header when present, otherwise sniffs the
    magic bytes. This guards against servers that answer a missing favicon with
    an HTML error page served as HTTP 200 (the classic "soft 404") — without the
    sniff we would cache and inline a chunk of HTML as if it were an icon.
    """
    if header_mime:
        m = header_mime.split(";")[0].strip().lower()
        if m.startswith("image/"):
            return m
    if not data:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"\x00\x00\x01\x00":
        return "image/x-icon"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    head = data[:512].lstrip().lower()
    if head[:5] == b"<?xml" or head[:4] == b"<svg":
        return "image/svg+xml"
    return None


def _direct_fetch_image(url: str, timeout: int) -> tuple[None | bytes, None | str]:
    """GET ``url`` and return ``(data, mime)`` only if it is a real, small image."""
    response = _fetch(url, timeout)
    if response is None:
        return None, None
    data = response.content
    if not data or len(data) > _DIRECT_MAX_BYTES:
        return None, None
    mime = _sniff_image_mime(data, response.headers.get("Content-Type"))
    if mime is None:
        return None, None
    return data, mime


def _best_icon_href(html_bytes: bytes) -> str | None:
    """Pick the most suitable favicon URL declared in a page's ``<head>``.

    Parses ``<link rel="...icon...">`` tags and prefers an icon whose declared
    ``sizes`` is closest to 32x32 (what we render at), mildly favouring
    apple-touch-icons as they are reliably square. Returns the raw href (possibly
    relative) or ``None`` if the page declares no icon link.
    """
    try:
        doc = lxml_html.fromstring(html_bytes)
    except Exception:  # pylint: disable=broad-except
        return None
    best_href = None
    best_score = -1
    for link in doc.iterfind(".//link"):
        tokens = set((link.get("rel") or "").lower().split())
        if tokens.isdisjoint(_ICON_REL_TOKENS):
            continue
        href = link.get("href")
        if not href:
            continue
        sizes = (link.get("sizes") or "").lower()
        if "x" in sizes:
            try:
                px = int(sizes.split("x")[0])
                score = 100 - min(abs(px - 32), 100)
            except ValueError:
                score = 10
        else:
            score = 10
        if "apple-touch-icon" in tokens:
            score += 1
        if score > best_score:
            best_score = score
            best_href = href
    return best_href


def direct(domain: str, timeout: int) -> tuple[None | bytes, None | str]:
    """Favicon resolver that fetches the icon straight from the site itself.

    Third-party aggregators (google, duckduckgo, …) miss a long tail of domains —
    newer sites, sites that block datacenter crawlers, intranet-style hosts. The
    site is the source of truth, so this resolver:

      1. tries the well-known ``/favicon.ico`` location, then
      2. falls back to parsing the homepage for a ``<link rel="...icon...">`` and
         fetching the best candidate it declares.

    Kept late in :py:data:`_MULTI_CHAIN` because it is heavier (up to two
    requests) than the aggregators and only needs to run for the domains they
    could not resolve. Transient/network failures are swallowed: this is a
    best-effort source and the providers ahead of it already carry the
    transient-vs-definitive signal for the chain.
    """
    # 1) well-known location
    try:
        data, mime = _direct_fetch_image(f"https://{domain}/favicon.ico", timeout)
        if data is not None:
            return data, mime
    except (HTTPError, SearxEngineResponseException):
        pass

    # 2) parse the homepage for declared icon <link> tags
    try:
        response = _fetch(f"https://{domain}/", timeout)
        if response is None:
            return None, None
        href = _best_icon_href(response.content)
        if not href:
            return None, None
        return _direct_fetch_image(urljoin(str(response.url), href), timeout)
    except (HTTPError, SearxEngineResponseException):
        return None, None


# Order of the providers tried by :py:func:`multi`. Highest coverage / best
# quality first, near-guaranteed last-resort last, so most domains resolve on the
# first hop and only genuinely obscure ones walk the whole chain.
#   * google     — faviconV2, the broadest coverage and cleanest 32x32 output
#   * duckduckgo — solid second source, different crawl so it fills google's gaps
#   * yandex     — only 16x16, but covers domains the western crawlers miss
#   * allesedv   — answers for almost anything, the near-guaranteed backstop
#   * direct     — the site's own favicon.ico / <link rel=icon>; the source of
#                  truth for the long tail, but kept DEAD LAST on purpose: its
#                  icons run up to 30 KB (vs the aggregators' tiny 32x32 PNGs), so
#                  it only fires when every small-icon source has failed. That
#                  keeps the big icons out of the cache and fits far more domains
#                  inside the size budget.
_MULTI_CHAIN = (google, duckduckgo, yandex, allesedv, direct)


def multi(domain: str, timeout: int) -> tuple[None | bytes, None | str]:
    """Fallback-chain resolver: try each provider in :py:data:`_MULTI_CHAIN` in
    order and return the first one that yields a real favicon.

    A single provider has no icon for a large share of domains, so relying on
    just one leaves many results showing the blank placeholder. Walking several
    providers raises the hit rate dramatically: a domain only falls back to the
    placeholder when *none* of them has an icon.

    Each provider validates its own "no icon" sentinel (404, 1x1 pixel, gif
    placeholder, …) and returns ``(None, None)`` on a definitive miss, so here we
    take the first non-empty result. A provider that fails *transiently*
    (rate-limit / network / server error) raises; we skip it and keep walking the
    chain, but remember that it failed. If we reach the end with no icon AND at
    least one provider failed transiently, we re-raise so the caller leaves the
    result uncached and retries later — only an all-providers-agree miss (no
    transient failures) is cached as a negative.
    """
    transient_failure = False
    for func in _MULTI_CHAIN:
        try:
            data, mime = func(domain, timeout)
        except (HTTPError, SearxEngineResponseException):
            transient_failure = True
            continue
        except Exception:  # pylint: disable=broad-except
            logger.exception("favicon resolver %s failed for %s", func.__name__, domain)
            transient_failure = True
            continue
        if data is not None and mime is not None:
            return data, mime
    if transient_failure:
        raise SearxEngineResponseException(f"all favicon resolvers failed transiently for {domain}")
    return None, None


DEFAULT_RESOLVER_MAP = {
    "allesedv": allesedv,
    "duckduckgo": duckduckgo,
    "google": google,
    "yandex": yandex,
    "direct": direct,
    "multi": multi,
}
