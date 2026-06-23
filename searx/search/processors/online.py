# SPDX-License-Identifier: AGPL-3.0-or-later
"""Processor used for ``online`` engines."""

__all__ = ["OnlineProcessor", "OnlineParams"]

import typing as t

from timeit import default_timer
import asyncio
import copy
import ssl
import httpx

import searx.network
from searx import get_setting
from searx.utils import gen_useragent
from searx.exceptions import (
    SearxEngineAccessDeniedException,
    SearxEngineCaptchaException,
    SearxEngineTooManyRequestsException,
)
from searx.metrics.error_recorder import count_error
from .abstract import EngineProcessor, RequestParams

if t.TYPE_CHECKING:
    from searx.search.models import SearchQuery
    from searx.results import ResultContainer
    from searx.result_types import EngineResults


class HTTPParams(t.TypedDict):
    """HTTP request parameters"""

    method: t.Literal["GET", "POST"]
    """HTTP request method."""

    headers: dict[str, str]
    """HTTP header information."""

    data: dict[str, str | int | dict[str, str | int]]
    """Sending `form encoded data`_.

    .. _form encoded data:
       https://www.python-httpx.org/quickstart/#sending-form-encoded-data
    """

    json: dict[str, t.Any]
    """`Sending `JSON encoded data`_.

    .. _JSON encoded data:
       https://www.python-httpx.org/quickstart/#sending-json-encoded-data
    """

    content: bytes
    """`Sending `binary request data`_.

    .. _binary request data:
       https://www.python-httpx.org/quickstart/#sending-json-encoded-data
    """

    url: str | None
    """Requested url."""

    cookies: dict[str, str]
    """HTTP cookies."""

    allow_redirects: bool
    """Follow redirects"""

    max_redirects: int
    """Maximum redirects, hard limit."""

    soft_max_redirects: int
    """Maximum redirects, soft limit. Record an error but don't stop the engine."""

    verify: None | t.Literal[False] | str  # not sure str really works
    """If not ``None``, it overrides the verify value defined in the network.  Use
    ``False`` to accept any server certificate and use a path to file to specify a
    server certificate"""

    auth: str | None
    """An authentication to use when sending requests."""

    raise_for_httperror: bool
    """Raise an exception if the `HTTP response status code`_ is ``>= 300``.

    .. _HTTP response status code:
        https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Status
    """


class OnlineParams(HTTPParams, RequestParams):
    """Request parameters of a ``online`` engine."""


def default_request_params() -> HTTPParams:
    """Default request parameters for ``online`` engines."""
    return {
        "method": "GET",
        "headers": {},
        "data": {},
        "json": {},
        "content": b"",
        "url": "",
        "cookies": {},
        "allow_redirects": False,
        "max_redirects": 0,
        "soft_max_redirects": 0,
        "auth": None,
        "verify": None,
        "raise_for_httperror": True,
    }


class OnlineProcessor(EngineProcessor):
    """Processor class for ``online`` engines."""

    engine_type: str = "online"

    def __init__(self, engine: t.Any):
        super().__init__(engine)
        # When an engine is blocked (captcha / HTTP 429) on the direct IP we
        # re-query it through the fallback proxy.  On success we keep routing the
        # engine through the proxy until this monotonic deadline, so the next
        # searches go straight to the proxy instead of wasting a doomed direct
        # request every time.  0 means "use the direct network".
        self._prefer_proxy_until: float = 0.0

    def _proxy_network_name(self) -> str | None:
        """Name of this engine's fallback-proxy twin network, or ``None`` if no
        fallback proxy is configured (see searx/network/network.py)."""
        name = f"{self.engine.name}__proxy"
        return name if searx.network.get_network(name) is not None else None

    def _retry_via_proxy(
        self,
        proxy_name: str,
        query: str,
        params: "OnlineParams",
        result_container: "ResultContainer",
        start_time: float,
        timeout_limit: float,
    ) -> bool:
        """Re-query the engine through the fallback proxy after a block on the
        direct IP.  Returns ``True`` as soon as one attempt goes through (results
        added to the container), ``False`` if every attempt is blocked too.

        The proxy twin disables keep-alive (see network.py), so each attempt
        opens a fresh tunnel and the rotating residential pool hands back a new
        exit IP -- retrying a few times makes it very likely to land on an IP the
        engine hasn't rate-limited.  ``params`` is the pristine (pre-request)
        snapshot; it is deep-copied per attempt because ``engine.request()``
        mutates it.
        """
        searx.network.set_context_network_name(proxy_name)
        attempts = max(1, int(get_setting("search.proxy_fallback_attempts")))
        for attempt in range(1, attempts + 1):
            if attempt > 1 and (default_timer() - start_time) >= timeout_limit:
                self.logger.warning("%s fallback proxy: out of time after %d attempt(s)", self.engine.name, attempt - 1)
                break
            self.logger.warning(
                "%s blocked on direct IP, re-querying via fallback proxy (attempt %d/%d)",
                self.engine.name,
                attempt,
                attempts,
            )
            try:
                search_results = self._search_basic(query, copy.deepcopy(params))
                self.extend_container(result_container, start_time, search_results)
            except (
                SearxEngineCaptchaException,
                SearxEngineTooManyRequestsException,
                SearxEngineAccessDeniedException,
            ) as e:
                # blocked on this exit IP too -- rotate (new tunnel) and retry
                self.logger.warning("%s fallback proxy attempt %d blocked: %s", self.engine.name, attempt, e)
                continue
            except Exception as e:  # pylint: disable=broad-except
                self.logger.warning("%s fallback proxy attempt %d failed: %s", self.engine.name, attempt, e)
                continue
            # Stick with the proxy for a while so we stop hammering the blocked IP.
            self._prefer_proxy_until = default_timer() + float(get_setting("search.max_ban_time_on_fail"))
            return True
        return False

    def init_engine(self) -> bool:
        """This method is called in a thread, and before the base method is
        called, the network must be set up for the ``online`` engines."""
        self.init_network_in_thread(start_time=default_timer(), timeout_limit=self.engine.timeout)
        return super().init_engine()

    def init_network_in_thread(self, start_time: float, timeout_limit: float):
        # set timeout for all HTTP requests
        searx.network.set_timeout_for_thread(timeout_limit, start_time=start_time)
        # reset the HTTP total time
        searx.network.reset_time_for_thread()
        # set the network
        searx.network.set_context_network_name(self.engine.name)

    def get_params(self, search_query: "SearchQuery", engine_category: str) -> OnlineParams | None:
        """Returns a dictionary with the :ref:`request params <engine request
        online>` (:py:obj:`OnlineParams`), if the search condition is not
        supported by the engine, ``None`` is returned."""

        base_params: RequestParams | None = super().get_params(search_query, engine_category)
        if base_params is None:
            return base_params

        params: OnlineParams = {**default_request_params(), **base_params}

        headers = params["headers"]
        headers["Accept-Encoding"] = "gzip, deflate"
        headers["Cache-Control"] = "no-cache"
        headers["DNT"] = "1"
        headers["Connection"] = "keep-alive"

        # add an user agent
        headers["User-Agent"] = gen_useragent()

        # add Accept-Language header
        # https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Accept-Language

        if self.engine.send_accept_language_header:
            if search_query.locale:
                _l = search_query.locale.language
                _t = search_query.locale.territory or _l
                headers["Accept-Language"] = f"{_l},{_l}-{_t};q=0.7,en;q=0.3"
            else:
                headers["Accept-Language"] = "en-US,en;q=0.9"
        self.logger.debug("HTTP Accept-Language: %s", headers.get("Accept-Language", ""))

        return params

    def _send_http_request(self, params: OnlineParams):

        # create dictionary which contain all information about the request
        request_args: dict[str, t.Any] = {
            "headers": params["headers"],
            "cookies": params["cookies"],
            "auth": params["auth"],
        }

        verify = params.get("verify")
        if verify is not None:
            request_args["verify"] = verify

        # max_redirects
        max_redirects = params.get("max_redirects")
        if max_redirects:
            request_args["max_redirects"] = max_redirects

        # allow_redirects
        if "allow_redirects" in params:
            request_args["allow_redirects"] = params["allow_redirects"]

        # soft_max_redirects
        soft_max_redirects: int = params.get("soft_max_redirects", max_redirects or 0)

        # raise_for_status
        request_args["raise_for_httperror"] = params.get("raise_for_httperror", True)

        # specific type of request (GET or POST)
        if params["method"] == "GET":
            req = searx.network.get
        else:
            req = searx.network.post
            if params["data"]:
                request_args["data"] = params["data"]
            if params["json"]:
                request_args["json"] = params["json"]
            if params["content"]:
                request_args["content"] = params["content"]

        # send the request
        response = req(params["url"], **request_args)  # pyright: ignore[reportArgumentType]

        # check soft limit of the redirect count
        if len(response.history) > soft_max_redirects:
            # unexpected redirect : record an error
            # but the engine might still return valid results.
            status_code = str(response.status_code or "")
            reason = response.reason_phrase or ""
            hostname = response.url.host
            count_error(
                self.engine.name,
                "{} redirects, maximum: {}".format(len(response.history), soft_max_redirects),
                (status_code, reason, hostname),
                secondary=True,
            )

        return response

    def _search_basic(self, query: str, params: OnlineParams) -> "EngineResults|None":
        # update request parameters dependent on
        # search-engine (contained in engines folder)
        self.engine.request(query, params)

        # ignoring empty urls
        if not params["url"]:
            return None

        # send request
        response = self._send_http_request(params)

        # parse the response
        response.search_params = params
        return self.engine.response(response)

    def search(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        query: str,
        params: OnlineParams,
        result_container: "ResultContainer",
        start_time: float,
        timeout_limit: float,
    ):
        self.init_network_in_thread(start_time, timeout_limit)

        proxy_name = self._proxy_network_name()
        # While the direct IP is blocked, skip it and go straight through the
        # proxy.  Reverts to the direct network automatically once the cooldown
        # (set in _retry_via_proxy) expires.
        prefer_proxy = proxy_name is not None and default_timer() < self._prefer_proxy_until
        if prefer_proxy:
            searx.network.set_context_network_name(proxy_name)  # type: ignore[arg-type]
        # engine.request() mutates params, so snapshot it for a possible retry.
        retry_params = copy.deepcopy(params) if (proxy_name is not None and not prefer_proxy) else None

        try:
            # send requests and parse the results
            search_results = self._search_basic(query, params)
            self.extend_container(result_container, start_time, search_results)
        except ssl.SSLError as e:
            # requests timeout (connect or read)
            self.handle_exception(result_container, e, suspend=True)
            self.logger.error("SSLError {}, verify={}".format(e, searx.network.get_network(self.engine.name).verify))
        except (httpx.TimeoutException, asyncio.TimeoutError) as e:
            # requests timeout (connect or read)
            self.handle_exception(result_container, e, suspend=True)
            self.logger.error(
                "HTTP requests timeout (search duration : {0} s, timeout: {1} s) : {2}".format(
                    default_timer() - start_time, timeout_limit, e.__class__.__name__
                )
            )
        except (httpx.HTTPError, httpx.StreamError) as e:
            # other requests exception
            self.handle_exception(result_container, e, suspend=True)
            self.logger.exception(
                "requests exception (search duration : {0} s, timeout: {1} s) : {2}".format(
                    default_timer() - start_time, timeout_limit, e
                )
            )
        except (
            SearxEngineCaptchaException,
            SearxEngineTooManyRequestsException,
            SearxEngineAccessDeniedException,
        ) as e:
            # Blocked on the direct IP: re-query once through the fallback proxy
            # before giving up (only if a proxy twin exists and we weren't
            # already on it).
            if retry_params is not None and self._retry_via_proxy(
                proxy_name,  # type: ignore[arg-type]
                query,
                retry_params,
                result_container,
                start_time,
                timeout_limit,
            ):
                return
            self.handle_exception(result_container, e, suspend=True)
            self.logger.exception(e.message)
        except Exception as e:  # pylint: disable=broad-except
            self.handle_exception(result_container, e)
            self.logger.exception("exception : {0}".format(e))
