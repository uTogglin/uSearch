# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-module-docstring, global-statement

import typing as t
from types import TracebackType

import asyncio
import logging
import os
import random
from ssl import SSLContext
import threading
import time

import httpx
from httpx_socks import AsyncProxyTransport
from python_socks import parse_proxy_url, ProxyConnectionError, ProxyTimeoutError, ProxyError

from searx import logger

CertTypes = str | tuple[str, str] | tuple[str, str, str]
SslContextKeyType = tuple[str | None, CertTypes | None, bool, bool]

logger = logger.getChild('searx.network.client')
LOOP: asyncio.AbstractEventLoop = None  # pyright: ignore[reportAssignmentType]

SSLCONTEXTS: dict[SslContextKeyType, SSLContext] = {}


def shuffle_ciphers(ssl_context: SSLContext):
    """Shuffle httpx's default ciphers of a SSL context randomly.

    From `What Is TLS Fingerprint and How to Bypass It`_

    > When implementing TLS fingerprinting, servers can't operate based on a
    > locked-in whitelist database of fingerprints.  New fingerprints appear
    > when web clients or TLS libraries release new versions. So, they have to
    > live off a blocklist database instead.
    > ...
    > It's safe to leave the first three as is but shuffle the remaining ciphers
    > and you can bypass the TLS fingerprint check.

    .. _What Is TLS Fingerprint and How to Bypass It:
       https://www.zenrows.com/blog/what-is-tls-fingerprint#how-to-bypass-tls-fingerprinting

    """
    c_list = [cipher["name"] for cipher in ssl_context.get_ciphers()]
    sc_list, c_list = c_list[:3], c_list[3:]
    random.shuffle(c_list)
    ssl_context.set_ciphers(":".join(sc_list + c_list))


def get_sslcontexts(
    proxy_url: str | None = None, cert: CertTypes | None = None, verify: bool = True, trust_env: bool = True
) -> SSLContext:
    key: SslContextKeyType = (proxy_url, cert, verify, trust_env)
    if key not in SSLCONTEXTS:
        SSLCONTEXTS[key] = httpx.create_ssl_context(verify, cert, trust_env)
    shuffle_ciphers(SSLCONTEXTS[key])
    return SSLCONTEXTS[key]


class AsyncHTTPTransportNoHttp(httpx.AsyncHTTPTransport):
    """Block HTTP request

    The constructor is blank because httpx.AsyncHTTPTransport.__init__ creates an SSLContext unconditionally:
    https://github.com/encode/httpx/blob/0f61aa58d66680c239ce43c8cdd453e7dc532bfc/httpx/_transports/default.py#L271

    Each SSLContext consumes more than 500kb of memory, since there is about one network per engine.

    In consequence, this class overrides all public methods

    For reference: https://github.com/encode/httpx/issues/2298
    """

    def __init__(self, *args, **kwargs):  # type: ignore
        # pylint: disable=super-init-not-called
        # this on purpose if the base class is not called
        pass

    async def handle_async_request(self, request: httpx.Request):
        raise httpx.UnsupportedProtocol('HTTP protocol is disabled')

    async def aclose(self) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        pass


class AsyncProxyTransportFixed(AsyncProxyTransport):
    """Fix httpx_socks.AsyncProxyTransport

    Map python_socks exceptions to httpx.ProxyError exceptions
    """

    async def handle_async_request(self, request: httpx.Request):
        try:
            return await super().handle_async_request(request)
        except ProxyConnectionError as e:
            raise httpx.ProxyError("ProxyConnectionError: " + str(e.strerror), request=request) from e
        except ProxyTimeoutError as e:
            raise httpx.ProxyError("ProxyTimeoutError: " + str(e.args[0]), request=request) from e
        except ProxyError as e:
            raise httpx.ProxyError("ProxyError: " + str(e.args[0]), request=request) from e


def get_transport_for_socks_proxy(
    verify: bool, http2: bool, local_address: str, proxy_url: str, limit: httpx.Limits, retries: int
):
    # support socks5h (requests compatibility):
    # https://requests.readthedocs.io/en/master/user/advanced/#socks
    # socks5://   hostname is resolved on client side
    # socks5h://  hostname is resolved on proxy side
    rdns = False
    socks5h = 'socks5h://'
    if proxy_url.startswith(socks5h):
        proxy_url = 'socks5://' + proxy_url[len(socks5h) :]
        rdns = True

    proxy_type, proxy_host, proxy_port, proxy_username, proxy_password = parse_proxy_url(proxy_url)
    _verify = get_sslcontexts(proxy_url, None, verify, True) if verify is True else verify
    return AsyncProxyTransportFixed(
        proxy_type=proxy_type,
        proxy_host=proxy_host,
        proxy_port=proxy_port,
        username=proxy_username,
        password=proxy_password,
        rdns=rdns,
        loop=get_loop(),
        verify=_verify,  # pyright: ignore[reportArgumentType]
        http2=http2,
        local_address=local_address,
        limits=limit,
        retries=retries,
    )


def get_transport(
    verify: bool, http2: bool, local_address: str, proxy_url: str | None, limit: httpx.Limits, retries: int
):
    _verify = get_sslcontexts(None, None, verify, True) if verify is True else verify
    return httpx.AsyncHTTPTransport(
        # pylint: disable=protected-access
        verify=_verify,
        http2=http2,
        limits=limit,
        proxy=httpx._config.Proxy(proxy_url) if proxy_url else None,  # pyright: ignore[reportPrivateUsage]
        local_address=local_address,
        retries=retries,
    )


def new_client(
    # pylint: disable=too-many-arguments
    enable_http: bool,
    verify: bool,
    enable_http2: bool,
    max_connections: int,
    max_keepalive_connections: int,
    keepalive_expiry: float,
    proxies: dict[str, str],
    local_address: str,
    retries: int,
    max_redirects: int,
    hook_log_response: t.Callable[..., t.Any] | None,
) -> httpx.AsyncClient:
    limit = httpx.Limits(
        max_connections=max_connections,
        max_keepalive_connections=max_keepalive_connections,
        keepalive_expiry=keepalive_expiry,
    )
    # See https://www.python-httpx.org/advanced/#routing
    mounts = {}
    mounts: None | (dict[str, t.Any | None]) = {}
    for pattern, proxy_url in proxies.items():
        if not enable_http and pattern.startswith('http://'):
            continue
        if proxy_url.startswith('socks4://') or proxy_url.startswith('socks5://') or proxy_url.startswith('socks5h://'):
            mounts[pattern] = get_transport_for_socks_proxy(
                verify, enable_http2, local_address, proxy_url, limit, retries
            )
        else:
            mounts[pattern] = get_transport(verify, enable_http2, local_address, proxy_url, limit, retries)

    if not enable_http:
        mounts['http://'] = AsyncHTTPTransportNoHttp()

    transport = get_transport(verify, enable_http2, local_address, None, limit, retries)

    event_hooks = None
    if hook_log_response:
        event_hooks = {'response': [hook_log_response]}

    return httpx.AsyncClient(
        transport=transport,
        mounts=mounts,
        max_redirects=max_redirects,
        event_hooks=event_hooks,
    )


def get_loop() -> asyncio.AbstractEventLoop:
    return LOOP


# --- VM suspend/resume recovery -------------------------------------------
#
# When the host suspends the VM (e.g. Fly.io ``auto_stop_machines = "suspend"``,
# which snapshots RAM instead of a clean shutdown), the long-lived httpx
# connection pools come back with sockets whose peers are long gone. Reusing
# those dead sockets makes every engine request hang until timeout -> a search
# returns 0 results ~12s after the box wakes. Fly itself warns: "On resume, the
# machine thinks its network connections are still live. External systems may
# disagree."
#
# There is no in-guest resume signal, so we detect the resume by watching the
# wall clock: the snapshot freezes the guest, and on resume NTP yanks
# CLOCK_REALTIME forward by the suspend duration while CLOCK_MONOTONIC just
# continues. A large forward divergence between the two is the resume signal;
# we react by closing every pooled client so the next request dials fresh.

RESUME_DRIFT_THRESHOLD = 5.0
"""Seconds of wall-vs-monotonic forward drift that signals a VM resume. Real
suspends last at least the autostop idle delay (minutes), so this sits far above
any GC pause or NTP micro-step yet far below any genuine suspend."""

_RESUME_CHECK_INTERVAL = 1.0

# Extra work to run once a VM resume is detected, beyond dropping HTTP pools —
# e.g. game_offers re-pulls foreign-exchange rates so the first search after a
# long sleep prices in current rates, not a snapshot that is now days stale.
# Kept here (rather than importing plugins) so the network layer stays free of
# upward dependencies; callers register with register_resume_callback().
_RESUME_CALLBACKS: list = []
_RESUME_CALLBACKS_LOCK = threading.Lock()


def register_resume_callback(callback) -> None:
    """Register a zero-arg callable to run (best-effort) after each VM resume.

    Idempotent per callable, so a module re-imported under a different name does
    not stack duplicates.  Safe to call before or after the watchdog starts."""
    with _RESUME_CALLBACKS_LOCK:
        if callback not in _RESUME_CALLBACKS:
            _RESUME_CALLBACKS.append(callback)


def _run_resume_callbacks() -> None:
    with _RESUME_CALLBACKS_LOCK:
        callbacks = list(_RESUME_CALLBACKS)
    for callback in callbacks:
        try:
            callback()
        except Exception:  # pylint: disable=broad-except
            logger.exception('resume callback %r failed', getattr(callback, '__name__', callback))


def _resume_drift(prev_wall: float, prev_mono: float, wall: float, mono: float) -> float:
    """Forward jump of the wall clock relative to the monotonic clock between two
    samples. ~0 in normal operation; ≈ the suspend duration right after a resume."""
    return (wall - prev_wall) - (mono - prev_mono)


def reset_networks_after_resume() -> None:
    """Close every pooled httpx client so the next request opens fresh sockets."""
    # Lazy import: network.py imports this module, so a module-scope import would
    # be circular.
    from searx.network.network import Network  # pylint: disable=import-outside-toplevel

    loop = get_loop()
    if loop is None:
        return
    try:
        future = asyncio.run_coroutine_threadsafe(Network.aclose_all(), loop)
        future.result(10)
    except Exception:  # pylint: disable=broad-except
        logger.exception('failed to reset HTTP pools after resume')


def _resume_watchdog() -> None:
    prev_wall = time.time()
    prev_mono = time.monotonic()
    while True:
        time.sleep(_RESUME_CHECK_INTERVAL)
        wall = time.time()
        mono = time.monotonic()
        drift = _resume_drift(prev_wall, prev_mono, wall, mono)
        prev_wall, prev_mono = wall, mono
        if drift > RESUME_DRIFT_THRESHOLD:
            # WARNING (not INFO) so it stays visible at SearXNG's default prod log
            # level — a resume is infrequent and worth an operational breadcrumb.
            logger.warning('detected %.0fs clock jump (VM resume); resetting HTTP pools', drift)
            reset_networks_after_resume()
            _run_resume_callbacks()


def init():
    # log
    for logger_name in (
        'httpx',
        'httpcore.proxy',
        'httpcore.connection',
        'httpcore.http11',
        'httpcore.http2',
        'hpack.hpack',
        'hpack.table',
    ):
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    # loop
    def loop_thread():
        global LOOP
        LOOP = asyncio.new_event_loop()
        LOOP.run_forever()

    thread = threading.Thread(
        target=loop_thread,
        name='asyncio_loop',
        daemon=True,
    )
    thread.start()

    # Watchdog that drops stale connection pools after a VM suspend/resume.
    # Disable on hosts that never suspend with SEARXNG_DISABLE_RESUME_WATCHDOG=1.
    if os.environ.get('SEARXNG_DISABLE_RESUME_WATCHDOG', '').lower() not in ('1', 'true', 'yes'):
        threading.Thread(
            target=_resume_watchdog,
            name='resume_watchdog',
            daemon=True,
        ).start()


init()
