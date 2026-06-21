# SPDX-License-Identifier: AGPL-3.0-or-later
"""Implementations of the favicon *resolvers* that are available in the favicon
proxy by default.  A *resolver* is a function that obtains the favicon from an
external source.  The *resolver* function receives two arguments (``domain,
timeout``) and returns a tuple ``(data, mime)``.

"""


__all__ = ["DEFAULT_RESOLVER_MAP", "allesedv", "duckduckgo", "google", "yandex", "multi"]

from typing import Callable

from httpx import HTTPError

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


# Order of the providers tried by :py:func:`multi`. Highest coverage / best
# quality first, near-guaranteed last-resort last, so most domains resolve on the
# first hop and only genuinely obscure ones walk the whole chain.
#   * google     — faviconV2, the broadest coverage and cleanest 32x32 output
#   * duckduckgo — solid second source, different crawl so it fills google's gaps
#   * yandex     — only 16x16, but covers domains the western crawlers miss
#   * allesedv   — answers for almost anything, kept last as a backstop
_MULTI_CHAIN = (google, duckduckgo, yandex, allesedv)


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
    "multi": multi,
}
