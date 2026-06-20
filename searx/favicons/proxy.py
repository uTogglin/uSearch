# SPDX-License-Identifier: AGPL-3.0-or-later
"""Implementations for a favicon proxy"""


from typing import Callable
from concurrent.futures import ThreadPoolExecutor

import importlib
import base64
import pathlib
import urllib.parse

import flask
from httpx import HTTPError
import msgspec

from searx import get_setting, logger

from searx.webutils import new_hmac, is_hmac_of
from searx.exceptions import SearxEngineResponseException
from searx.extended_types import sxng_request

from .resolvers import DEFAULT_RESOLVER_MAP
from . import cache

logger = logger.getChild('favicons.proxy')

DEFAULT_FAVICON_URL = {}
CFG: "FaviconProxyConfig" = None  # type: ignore


def init(cfg: "FaviconProxyConfig"):
    global CFG  # pylint: disable=global-statement
    CFG = cfg


def _initial_resolver_map():
    d = {}
    name: str = get_setting("search.favicon_resolver", None)  # type: ignore
    if name:
        func = DEFAULT_RESOLVER_MAP.get(name)
        if func:
            d = {name: f"searx.favicons.resolvers.{func.__name__}"}
    return d


class FaviconProxyConfig(msgspec.Struct):
    """Configuration of the favicon proxy."""

    max_age: int = 60 * 60 * 24 * 7  # seven days
    """HTTP header Cache-Control_ ``max-age``

    .. _Cache-Control: https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Cache-Control
    """

    secret_key: str = get_setting("server.secret_key")  # type: ignore
    """By default, the value from :ref:`server.secret_key <settings server>`
    setting is used."""

    resolver_timeout: int = get_setting("outgoing.request_timeout")  # type: ignore
    """Timeout which the resolvers should not exceed, is usually passed to the
    outgoing request of the resolver.  By default, the value from
    :ref:`outgoing.request_timeout <settings outgoing>` setting is used."""

    resolver_map: dict[str, str] = msgspec.field(default_factory=_initial_resolver_map)
    """The resolver_map is a key / value dictionary where the key is the name of
    the resolver and the value is the fully qualifying name (fqn) of resolver's
    function (the callable).  The resolvers from the python module
    :py:obj:`searx.favicons.resolver` are available by default."""

    def get_resolver(self, name: str) -> Callable | None:
        """Returns the callable object (function) of the resolver with the
        ``name``.  If no resolver is registered for the ``name``, ``None`` is
        returned.
        """
        fqn = self.resolver_map.get(name)
        if fqn is None:
            return None
        mod_name, _, func_name = fqn.rpartition('.')
        mod = importlib.import_module(mod_name)
        func = getattr(mod, func_name)
        if func is None:
            raise ValueError(f"resolver {fqn} is not implemented")
        return func

    favicon_path: str = get_setting("ui.static_path") + "/themes/{theme}/img/empty_favicon.svg"  # type: ignore
    favicon_mime_type: str = "image/svg+xml"

    def favicon(self, **replacements):
        """Returns pathname and mimetype of the default favicon."""
        return (
            pathlib.Path(self.favicon_path.format(**replacements)),
            self.favicon_mime_type,
        )

    def favicon_data_url(self, **replacements):
        """Returns data image URL of the default favicon."""

        cache_key = ", ".join(f"{x}:{replacements[x]}" for x in sorted(list(replacements.keys()), key=str))
        data_url = DEFAULT_FAVICON_URL.get(cache_key)
        if data_url is not None:
            return data_url

        fav, mimetype = CFG.favicon(**replacements)
        # hint: encoding utf-8 limits favicons to be a SVG image
        with fav.open("r", encoding="utf-8") as f:
            data_url = f.read()

        data_url = urllib.parse.quote(data_url)
        data_url = f"data:{mimetype};utf8,{data_url}"
        DEFAULT_FAVICON_URL[cache_key] = data_url
        return data_url


def favicon_proxy():
    """REST API of SearXNG's favicon proxy service

    ::

        /favicon_proxy?authority=<...>&h=<...>

    ``authority``:
      Domain name :rfc:`3986` / see :py:obj:`favicon_url`

    ``h``:
      HMAC :rfc:`2104`, build up from the :ref:`server.secret_key <settings
      server>` setting.

    """
    authority = sxng_request.args.get('authority')

    # malformed request or RFC 3986 authority
    if not authority or "/" in authority:
        return '', 400

    # malformed request / does not have authorisation
    if not is_hmac_of(
        CFG.secret_key,
        authority.encode(),
        sxng_request.args.get('h', ''),
    ):
        return '', 400

    resolver = sxng_request.preferences.get_value('favicon_resolver')  # type: ignore
    # if resolver is empty or not valid, just return HTTP 400.
    if not resolver or resolver not in CFG.resolver_map.keys():
        return "", 400

    data, mime = search_favicon(resolver, authority)

    if data is not None and mime is not None:
        resp = flask.Response(data, mimetype=mime)  # type: ignore
        resp.headers['Cache-Control'] = f"max-age={CFG.max_age}"
        return resp

    # return default favicon from static path
    theme = sxng_request.preferences.get_value("theme")  # type: ignore
    fav, mimetype = CFG.favicon(theme=theme)
    return flask.send_from_directory(fav.parent, fav.name, mimetype=mimetype)


def search_favicon(resolver: str, authority: str) -> tuple[None | bytes, None | str]:
    """Sends the request to the favicon resolver and returns a tuple for the
    favicon.  The tuple consists of ``(data, mime)``, if the resolver has not
    determined a favicon, both values are ``None``.

    ``data``:
      Binary data of the favicon.

    ``mime``:
      Mime type of the favicon.

    """

    data, mime = (None, None)

    func = CFG.get_resolver(resolver)
    if func is None:
        return data, mime

    # to avoid superfluous requests to the resolver, first look in the cache
    data_mime = cache.CACHE(resolver, authority)
    if data_mime is not None:
        return data_mime

    try:
        data, mime = func(authority, timeout=CFG.resolver_timeout)
        if data is None or mime is None:
            data, mime = (None, None)

    except (HTTPError, SearxEngineResponseException):
        pass

    cache.CACHE.set(resolver, authority, mime, data)
    return data, mime


def favicon_url(authority: str) -> str:
    """Function to generate the image URL used for favicons in SearXNG's result
    lists.  The ``authority`` argument (aka netloc / :rfc:`3986`) is usually a
    (sub-) domain name.  This function is used in the HTML (jinja) templates.

    .. code:: html

       <div class="favicon">
          <img src="{{ favicon_url(result.parsed_url.netloc) }}">
       </div>

    The returned URL is a route to :py:obj:`favicon_proxy` REST API.

    If the favicon is already in the cache, the returned URL is a `data URL`_
    (something like ``data:image/png;base64,...``).  By generating a data url from
    the :py:obj:`.cache.FaviconCache`, additional HTTP roundtripps via the
    :py:obj:`favicon_proxy` are saved.  However, it must also be borne in mind
    that data urls are not cached in the client (web browser).

    .. _data URL: https://developer.mozilla.org/en-US/docs/Web/HTTP/Basics_of_HTTP/Data_URLs

    """

    resolver = sxng_request.preferences.get_value('favicon_resolver')  # type: ignore
    # if resolver is empty or not valid, just return nothing.
    if not resolver or resolver not in CFG.resolver_map.keys():
        return ""

    data_mime = cache.CACHE(resolver, authority)

    if data_mime == (None, None):
        # we have already checked, the resolver does not have a favicon
        theme = sxng_request.preferences.get_value("theme")  # type: ignore
        return CFG.favicon_data_url(theme=theme)

    if data_mime is not None:
        data, mime = data_mime
        return f"data:{mime};base64,{str(base64.b64encode(data), 'utf-8')}"  # type: ignore

    h = new_hmac(CFG.secret_key, authority.encode())
    proxy_url = flask.url_for('favicon_proxy')
    query = urllib.parse.urlencode({"authority": authority, "h": h})
    return f"{proxy_url}?{query}"


def favicon_cached_data_url(authority: str) -> str | None:
    """Inline favicon data URL for ``authority`` *iff* it needs no network request.

    Returns a ``data:`` URL when the resolver result is already known — a cached
    positive (the real icon) or a cached negative (the empty placeholder) — and
    ``None`` on a genuine cache miss, where resolving would mean a blocking
    resolver round-trip.

    This lets the template inline every already-known favicon straight into the
    search HTML. On a warm instance that resolves a whole results page with no
    extra request at all, dropping the ``/favicon_batch`` round-trip (and the
    edge Worker invocation it costs) entirely; only genuinely-unknown domains keep
    the placeholder + ``data-favicon-authority`` slot the client batch fills in.
    The cache is populated by that very batch, so coverage grows on its own. The
    server render never blocks on a resolver, so there is no latency cost."""
    resolver = sxng_request.preferences.get_value('favicon_resolver') or get_setting(  # type: ignore
        'search.favicon_resolver', ''
    )
    if not resolver or resolver not in CFG.resolver_map.keys():
        return None

    data_mime = cache.CACHE(resolver, authority)
    if data_mime is None:
        # genuine cache miss — inlining would require a resolver round-trip
        return None
    if data_mime == (None, None):
        # cached negative: known to have no icon → inline the empty placeholder
        theme = sxng_request.preferences.get_value("theme")  # type: ignore
        return CFG.favicon_data_url(theme=theme)
    data, mime = data_mime
    return f"data:{mime};base64,{str(base64.b64encode(data), 'utf-8')}"  # type: ignore


def favicon_placeholder() -> str:
    """The default (empty) favicon as an inline data URL.

    Used as the initial ``src`` of every result favicon so the page never fires
    a per-result network request just to show an icon; the real favicons are
    swapped in afterwards by a single batched call (see :py:obj:`favicon_data_url`
    and the ``/favicon_batch`` route)."""
    theme = sxng_request.preferences.get_value("theme")  # type: ignore
    return CFG.favicon_data_url(theme=theme)


def favicon_data_url(authority: str) -> str:
    """Resolve the favicon for ``authority`` and return it as an inline data URL.

    Cache-aware exactly like :py:obj:`favicon_url` (a hit returns instantly, a
    miss resolves via the configured resolver and is then cached), but it *always*
    inlines — it never emits a ``/favicon_proxy`` route. That lets a results page
    resolve every favicon in ONE batched request instead of one HTTP round-trip
    per result, and keeps the visited domains out of any plaintext URL the
    Cloudflare edge could read. Falls back to the placeholder when the resolver
    has no icon."""
    # Fall back to the instance's configured resolver when the per-user preference
    # is unset (None). A stale preferences cookie from before the favicon feature
    # leaves the preference empty, which would otherwise return placeholders for
    # everyone but a fresh session — even though the template still renders the
    # favicon slots (Jinja treats ``None != ""`` as true).
    resolver = sxng_request.preferences.get_value('favicon_resolver') or get_setting(  # type: ignore
        'search.favicon_resolver', ''
    )
    if not resolver or resolver not in CFG.resolver_map.keys():
        return favicon_placeholder()

    data, mime = search_favicon(resolver, authority)
    if data is not None and mime is not None:
        return f"data:{mime};base64,{str(base64.b64encode(data), 'utf-8')}"  # type: ignore
    return favicon_placeholder()


# Resolving a results page worth of favicons one after another means paying one
# blocking resolver round-trip (up to ``resolver_timeout`` each) per cache miss,
# in series — a page of uncached domains takes N timeouts back to back. The batch
# below resolves the misses concurrently, so wall-clock collapses to roughly a
# single round-trip regardless of how many domains miss the cache.
FAVICON_BATCH_WORKERS = 16


def favicon_data_url_batch(authorities: "list[str]", max_count: int) -> "dict[str, str]":
    """Resolve favicons for many ``authorities`` in one call → ``{authority: data_url}``.

    Equivalent to calling :py:obj:`favicon_data_url` per authority, but cache
    *misses* are resolved concurrently instead of in series, so the page is bound
    by the slowest single resolver round-trip rather than their sum. Cache hits and
    the placeholder fallback do no network work.

    Input is validated, de-duplicated and capped at ``max_count``. Must run inside
    a Flask request context: the per-user resolver and theme preferences are read
    once, up front, before any worker thread starts (worker threads have no request
    context, so only :py:obj:`search_favicon` — cache + network — is handed off)."""

    # Read request-context-bound state once, in the calling (request) thread.
    resolver = sxng_request.preferences.get_value('favicon_resolver') or get_setting(  # type: ignore
        'search.favicon_resolver', ''
    )
    placeholder = favicon_placeholder()

    todo: "list[str]" = []
    seen: "set[str]" = set()
    for authority in authorities:
        if len(todo) >= max_count:
            break
        if not isinstance(authority, str) or not authority or "/" in authority or authority in seen:
            continue
        seen.add(authority)
        todo.append(authority)

    if not todo:
        return {}

    if not resolver or resolver not in CFG.resolver_map.keys():
        return {authority: placeholder for authority in todo}

    # Warm the cache's SQLite schema on THIS (request) thread before the pool
    # starts. The schema is created lazily on first DB access; the init guard
    # (`SQLiteAppl._init_done`) flips to "done" BEFORE the CREATE TABLE commits.
    # If the very first access happens concurrently from the worker threads, one
    # worker sets the guard while another races ahead and queries a table that
    # does not exist yet → "no such table: blob_map", which aborts the whole
    # batch (HTTP 500) and blanks every favicon on the page. One main-thread touch
    # creates and commits the schema up front, so the workers only ever open
    # connections to an already-initialised DB.
    try:
        _ = cache.CACHE.DB  # noqa: F841
    except Exception:  # pylint: disable=broad-except
        logger.exception("favicon cache warm-up failed")

    def resolve(authority: str) -> str:
        # Resolve defensively: a single authority's failure (network, resolver,
        # or a transient cache error) must never propagate out of the pool and
        # 500 the whole batch — that would blank EVERY favicon on the page.
        try:
            data, mime = search_favicon(resolver, authority)
            if data is not None and mime is not None:
                return f"data:{mime};base64,{str(base64.b64encode(data), 'utf-8')}"  # type: ignore
        except Exception:  # pylint: disable=broad-except
            logger.exception("favicon batch resolve failed for %s", authority)
        return placeholder

    # The cache (per-thread SQLite connection) and searx.network (dispatches to a
    # shared background event loop) are both safe to call from worker threads once
    # the schema has been initialised above.
    with ThreadPoolExecutor(max_workers=min(FAVICON_BATCH_WORKERS, len(todo))) as pool:
        return dict(zip(todo, pool.map(resolve, todo)))
