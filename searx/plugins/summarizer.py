# SPDX-License-Identifier: AGPL-3.0-or-later
"""
uSearch Universal Summarizer Plugin — OpenRouter Edition
========================================================
Summarize ANY arbitrary URL or YouTube video on demand — not just SERP
results.  Powered by `OpenRouter <https://openrouter.ai/>`_ (or any
OpenAI-compatible API) and the shared AI plumbing in :mod:`searx.ai_common`.

This is a sibling of :mod:`searx.plugins.ai_summary` (the SERP "AI Overview"
box) and deliberately reuses its proven idioms: ``init(self, app)`` registering
Flask routes, dual SSE transports (plaintext GET + per-frame-encrypted POST via
:mod:`searx.echannel`), a per-IP token bucket, a persistent SQLite response
cache, and the SSE frame protocol described below.

Endpoints
---------
``GET  /ai_summarize?url=<url>``
    Plaintext SSE stream summarizing ``url``.

``POST /eai_summarize``
    Encrypted variant.  Body is an ``{epk,iv,ct}`` envelope whose decrypted
    plaintext is JSON ``{"url": "..."}``.  Every reply frame is encrypted with
    the per-request session key.

``GET  /summarize`` (and ``GET /summarize?url=<url>``)
    The paste-a-URL HTML page.  When a ``?url=`` param is present the page
    auto-starts summarizing it, so a per-result "Summarize" button can
    deep-link straight into a summary.

SSE frame protocol (identical to ai_summary, so the client reuses one reader)
----------------------------------------------------------------------------
Each event line is ``data: <payload>\\n\\n``.  Payloads:
  * a streamed token   → ``json.dumps(token)``  (a JSON-quoted string)
  * a cache hit        → the sentinel ``"[CACHED]"`` followed by a single
                         ``json.dumps(full_text)`` frame
  * an error           → ``"[ERROR] <message>"`` (a JSON-quoted string)
  * end of stream      → ``[DONE]``
On the encrypted transport each of those payload strings is wrapped through
``session.encrypt(...)`` and sent as ``data: {"iv":..,"ct":..}\\n\\n``.

Content acquisition (the new part)
----------------------------------
* **YouTube** (``youtube.com/watch?v=``, ``youtu.be/<id>``, ``m.youtube.com``,
  ``youtube.com/shorts/<id>``): dependency-free transcript scrape.  Fetch the
  watch page, pull a caption track ``baseUrl`` out of ``ytInitialPlayer
  Response``, fetch the timedtext (JSON3 preferred, XML fallback) and
  concatenate the cue text.  If no transcript is available (captions disabled /
  blocked) fall back to the scraped video title + description.  Transcript is
  capped *before* it reaches the LLM.
* **General URLs**: fetch the HTML with a browser-y User-Agent, ``follow_
  redirects=True`` and a streamed **response-size cap** (we stop reading at
  ~1.5 MB so a huge page can't blow up memory).  Readable text is extracted
  with **lxml** (a SearXNG dependency): script/style/nav/header/footer/aside/
  form/noscript are dropped, ``<article>``/``<main>`` is preferred when present,
  whitespace is collapsed, and the text is capped to ~12000 chars.

Outbound fetch path / proxy
---------------------------
We fetch **directly** first.  Only on failure/block (timeout, 403/429, or an
empty extract) do we retry ONCE through the instance's configured fallback
proxy, and we keep the size cap so we minimise bandwidth to it (the user's
directive: "route via proxy very rarely and minimise bandwidth to it").  The
proxy URL comes from ``summarizer.proxy_url`` if set, otherwise from the
instance-wide fallback proxy resolved by
:func:`searx.network.network._get_fallback_proxies` (``outgoing.fallback_
proxies`` setting or the ``PROXY_HOST``/``PROXY_PORT``/``PROXY_USERNAME``/
``PROXY_PASSWORD`` env vars).  If neither is configured the proxy retry is
simply skipped.

SSRF / abuse hardening (mirrors card_meta)
------------------------------------------
* http/https only, URL length capped (~2048).
* The host is resolved and every resolved IP is checked: private, loopback,
  link-local, reserved, multicast and non-global addresses are rejected.
* Per-IP token-bucket rate limit.
* The handler NEVER 500s — every failure degrades to an SSE ``[ERROR]`` frame
  (or a safe response).

CONFIGURATION (settings.yml) — registered by the orchestrator, NOT here
-----------------------------------------------------------------------
  plugins:
    searx.plugins.summarizer.SXNGPlugin:
      active: true

  summarizer:
    base_url:        "https://openrouter.ai/api/v1"
    # api_key is read from this setting OR the OPENROUTER_API_KEY env var
    # (env var recommended so the key never lands in settings.yml).
    api_key:         ""
    model:           "openai/gpt-4o-mini"
    max_tokens:      500
    timeout:         60            # LLM stream timeout (seconds)
    # Optional OpenRouter ranking headers
    http_referer:    ""
    x_title:         "uSearch Summarizer"
    # Rate limiting (per client IP)
    rate_limit_capacity: 5
    rate_limit_rate:     0.5       # tokens / second
    # Persistent summary cache (SQLite) — survives restarts
    response_cache_path:    "/var/cache/searxng/summarizer_cache.db"
    response_cache_ttl:     604800   # 7 days
    response_cache_enabled: true
    # Outbound content fetch
    fetch_timeout:      12           # per-request fetch timeout (seconds)
    fetch_max_bytes:    1572864      # 1.5 MB streamed response cap
    extract_max_chars:  12000        # chars sent to the LLM
    # Proxy used ONLY on a failed/blocked direct fetch (very rarely). Leave
    # empty to fall back to the instance-wide outgoing.fallback_proxies.
    proxy_url:          ""
"""

import ipaddress
import json
import logging
import re
import socket
import tempfile
import time
import typing as t
from urllib.parse import parse_qs, urlsplit

import httpx
from flask_babel import gettext as _

from searx import ai_common, get_setting
from searx.plugins import Plugin, PluginInfo

if t.TYPE_CHECKING:
    from searx.plugins import PluginCfg

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
_DEFAULT_CACHE_PATH = "/var/cache/searxng/summarizer_cache.db"

_MAX_URL_LEN = 2048

# A normal browser User-Agent — many sites (and YouTube) gate bot-y agents.
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# YouTube video id (the canonical 11-char base64url-ish id).
_YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# Pull the ytInitialPlayerResponse JSON blob out of a watch page.
_YT_PLAYER_RE = re.compile(
    r"ytInitialPlayerResponse\s*=\s*(\{.+?\})\s*;\s*(?:var\s|</script>)", re.DOTALL
)

_SYSTEM_PROMPT_URL = (
    """You summarize the web page content the user provides. Lead with the gist
in the first one or two sentences, then give the key points as a short, tight
list of plain sentences. Be faithful to the source — do not invent facts. Be
specific: prefer names, numbers, and dates. Plain text only — no markdown, no
preamble, no meta-commentary (never open with "This page", "The content", or
"The user"). If the supplied content is empty or clearly not the real article
(e.g. a login wall or error page), say so in one sentence instead of guessing."""
)

_SYSTEM_PROMPT_YOUTUBE = (
    """You summarize the YouTube video whose transcript the user provides. Lead
with what the video is about in the first one or two sentences, then give the
main points covered, in order, as a short list of plain sentences. Be faithful
to the transcript — do not invent facts. Plain text only — no markdown, no
preamble, no meta-commentary. If only the title and description are available
(no transcript), summarize from those and note that no transcript was
available."""
)


def _setting(key: str, default=None):
    return get_setting(f"summarizer.{key}", default)


# ── Shared singletons (lazily wired in init) ──────────────────────────────────
_bucket = ai_common.TokenBucket()
_cache: "ai_common.ResponseCache | None" = None


def _get_cache() -> "ai_common.ResponseCache":
    global _cache
    if _cache is None:
        path = _setting("response_cache_path", _DEFAULT_CACHE_PATH) or _DEFAULT_CACHE_PATH
        cache = ai_common.ResponseCache(
            path,
            default_ttl=float(_setting("response_cache_ttl", 604800)),
            enabled=bool(_setting("response_cache_enabled", True)),
        )
        if not cache.enabled and _setting("response_cache_enabled", True):
            # The configured (Fly-volume) path was not writable — fall back to a
            # temp-dir DB so caching still works in local/dev environments.
            fallback = tempfile.gettempdir() + "/searxng_summarizer_cache.db"
            cache = ai_common.ResponseCache(
                fallback,
                default_ttl=float(_setting("response_cache_ttl", 604800)),
                enabled=True,
            )
        _cache = cache
    return _cache


# ── URL validation + SSRF guard ───────────────────────────────────────────────

def _validate_url(raw: str) -> "tuple[str, str] | None":
    """Return ``(normalized_url, host)`` for a safe http/https URL, else None.

    Mirrors card_meta's defensiveness: scheme allow-list, length cap, and a
    DNS-resolution SSRF guard that rejects any host resolving to a non-global
    (private / loopback / link-local / reserved / multicast) address.
    """
    if not raw or len(raw) > _MAX_URL_LEN:
        return None
    try:
        parts = urlsplit(raw.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower()
    if not host:
        return None
    if not _host_is_public(host):
        return None
    # Normalize: drop fragment, keep scheme/host/path/query.
    normalized = parts._replace(fragment="").geturl()
    return normalized, host


def _host_is_public(host: str) -> bool:
    """Resolve ``host`` and reject any non-globally-routable address.

    A literal IP host is checked directly; a name is resolved (A + AAAA) and
    every returned address must be global.  On resolution failure we reject
    (fail-closed) — a host we can't resolve is one we shouldn't fetch.
    """
    # Literal IP?
    try:
        ip = ipaddress.ip_address(host)
        return _ip_is_public(ip)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    if not infos:
        return False
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr.split("%")[0])  # strip IPv6 zone id
        except ValueError:
            return False
        if not _ip_is_public(ip):
            return False
    return True


def _ip_is_public(ip: "ipaddress._BaseAddress") -> bool:
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


# ── YouTube detection + transcript ────────────────────────────────────────────

def _youtube_id(parts) -> "str | None":
    """Extract a YouTube video id from a parsed URL, or None if not YouTube."""
    host = (parts.hostname or "").lower().lstrip(".")
    if host.startswith("www."):
        host = host[4:]
    segs = [s for s in parts.path.split("/") if s]

    if host in ("youtube.com", "m.youtube.com", "music.youtube.com"):
        if segs and segs[0] == "watch":
            pass  # unusual; fall through to query parsing
        if segs and segs[0] == "shorts" and len(segs) >= 2:
            vid = segs[1]
            return vid if _YT_ID_RE.match(vid) else None
        if segs and segs[0] == "embed" and len(segs) >= 2:
            vid = segs[1]
            return vid if _YT_ID_RE.match(vid) else None
        q = parse_qs(parts.query or "")
        vid = (q.get("v") or [""])[0]
        return vid if _YT_ID_RE.match(vid) else None

    if host == "youtu.be" and segs:
        vid = segs[0]
        return vid if _YT_ID_RE.match(vid) else None

    return None


def _fetch_youtube(video_id: str, *, timeout: float, max_bytes: int,
                   max_chars: int, proxy: "str | None") -> "tuple[str, str]":
    """Return ``(title, content)`` for a YouTube video.

    ``content`` is the transcript when available, else the description. Tries a
    direct fetch first; retries once via ``proxy`` on failure/empty.
    """
    watch_url = f"https://www.youtube.com/watch?v={video_id}&hl=en"

    def _attempt(use_proxy: "str | None"):
        html_text = _http_get_text(watch_url, timeout=timeout, max_bytes=max_bytes,
                                   proxy=use_proxy)
        player = _extract_player_response(html_text)
        title = ""
        desc = ""
        if player:
            details = player.get("videoDetails") or {}
            title = (details.get("title") or "")[:300]
            desc = (details.get("shortDescription") or "")
        transcript = _youtube_transcript(player, timeout=timeout,
                                         max_bytes=max_bytes, proxy=use_proxy)
        content = transcript or desc
        return title, (content or "")[:max_chars]

    try:
        title, content = _attempt(None)
        if content.strip():
            return title, content
    except Exception as exc:  # noqa: BLE001
        logger.info("summarizer: youtube direct fetch failed: %s", exc)

    if proxy:
        try:
            title, content = _attempt(proxy)
            if content.strip() or title:
                return title, content
        except Exception as exc:  # noqa: BLE001
            logger.info("summarizer: youtube proxy fetch failed: %s", exc)
    return "", ""


def _extract_player_response(html_text: str) -> "dict | None":
    if not html_text:
        return None
    m = _YT_PLAYER_RE.search(html_text)
    if not m:
        # Looser fallback: find the assignment and brace-match from there.
        idx = html_text.find("ytInitialPlayerResponse")
        if idx == -1:
            return None
        brace = html_text.find("{", idx)
        if brace == -1:
            return None
        blob = _brace_match(html_text, brace)
        if not blob:
            return None
        try:
            return json.loads(blob)
        except (json.JSONDecodeError, ValueError):
            return None
    try:
        return json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        # The non-greedy regex may have stopped early; brace-match instead.
        blob = _brace_match(html_text, html_text.find("{", m.start()))
        if blob:
            try:
                return json.loads(blob)
            except (json.JSONDecodeError, ValueError):
                return None
    return None


def _brace_match(s: str, start: int) -> "str | None":
    """Return the balanced ``{...}`` substring starting at ``start`` (a '{')."""
    if start < 0 or start >= len(s) or s[start] != "{":
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
        if i - start > 5_000_000:  # safety bound
            return None
    return None


def _youtube_transcript(player: "dict | None", *, timeout: float,
                        max_bytes: int, proxy: "str | None") -> str:
    """Best-effort transcript text from a ytInitialPlayerResponse dict."""
    if not player:
        return ""
    try:
        tracks = (player["captions"]["playerCaptionsTracklistRenderer"]
                  ["captionTracks"])
    except (KeyError, TypeError):
        return ""
    if not tracks:
        return ""

    # Prefer an English track; otherwise take the first available.
    base_url = ""
    for tr in tracks:
        code = (tr.get("languageCode") or "").lower()
        if code.startswith("en"):
            base_url = tr.get("baseUrl") or ""
            break
    if not base_url:
        base_url = tracks[0].get("baseUrl") or ""
    if not base_url:
        return ""
    # ``baseUrl`` is supplied by YouTube's own player JSON, but treat it as
    # external data: run it through the same SSRF guard before fetching so a
    # tampered/unexpected value can't steer us at an internal host.
    if _validate_url(base_url) is None:
        return ""

    # JSON3 is the cleanest format to parse.
    json3_url = base_url + ("&" if "?" in base_url else "?") + "fmt=json3"
    try:
        raw = _http_get_text(json3_url, timeout=timeout, max_bytes=max_bytes,
                             proxy=proxy)
        text = _parse_timedtext_json3(raw)
        if text:
            return text
    except Exception as exc:  # noqa: BLE001
        logger.info("summarizer: timedtext json3 failed: %s", exc)

    # XML fallback.
    try:
        raw = _http_get_text(base_url, timeout=timeout, max_bytes=max_bytes,
                             proxy=proxy)
        return _parse_timedtext_xml(raw)
    except Exception as exc:  # noqa: BLE001
        logger.info("summarizer: timedtext xml failed: %s", exc)
        return ""


def _parse_timedtext_json3(raw: str) -> str:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return ""
    parts: list = []
    for event in data.get("events") or []:
        for seg in event.get("segs") or []:
            piece = seg.get("utf8")
            if piece:
                parts.append(piece)
    text = "".join(parts)
    return _collapse_ws(text.replace("\n", " "))


def _parse_timedtext_xml(raw: str) -> str:
    if not raw:
        return ""
    try:
        from lxml import etree
        root = etree.fromstring(raw.encode("utf-8") if isinstance(raw, str) else raw)
    except Exception:  # noqa: BLE001
        return ""
    import html as _htmlmod
    parts = []
    for node in root.iter("text"):
        if node.text:
            parts.append(_htmlmod.unescape(node.text))
    return _collapse_ws(" ".join(parts).replace("\n", " "))


# ── General URL fetch + readable-text extraction ──────────────────────────────

_MAX_REDIRECTS = 5


def _http_get_text(url: str, *, timeout: float, max_bytes: int,
                   proxy: "str | None") -> str:
    """Streamed GET with a hard response-size cap. Returns decoded text.

    Streaming + early-stop means a multi-hundred-MB page can never be pulled in
    full; we stop reading once ``max_bytes`` have accumulated. Raises on HTTP
    error so callers can decide whether to retry via proxy.

    Redirects are followed MANUALLY (``follow_redirects=False``) so each hop's
    host can be re-validated through the SSRF guard — otherwise a public URL
    could 30x-redirect to ``http://169.254.169.254/`` (cloud metadata) or an
    internal host and httpx would follow it blindly. Each ``Location`` must pass
    :func:`_validate_url` (scheme allow-list + non-global-IP rejection) before we
    fetch it. (When ``proxy`` is set the proxy does the DNS, but re-validating
    the host name here is still the right defence-in-depth gate.)
    """
    headers = {
        "User-Agent": _BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    client_kwargs: dict = {"follow_redirects": False, "timeout": timeout, "headers": headers}
    if proxy:
        # httpx accepts a single proxy URL string here.
        client_kwargs["proxy"] = proxy

    chunks: list = []
    total = 0
    encoding = "utf-8"
    with httpx.Client(**client_kwargs) as client:
        current = url
        for _hop in range(_MAX_REDIRECTS + 1):
            with client.stream("GET", current) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location", "")
                    if not location:
                        resp.raise_for_status()
                        break
                    # Resolve relative redirects against the current URL, then
                    # re-validate the absolute target through the SSRF guard.
                    nxt = str(httpx.URL(current).join(location))
                    if _validate_url(nxt) is None:
                        raise httpx.RequestError("redirect to disallowed/internal host blocked")
                    current = nxt
                    continue
                resp.raise_for_status()
                for chunk in resp.iter_bytes():
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= max_bytes:
                        break
                encoding = resp.encoding or "utf-8"
                break
        else:
            raise httpx.RequestError("too many redirects")
    body = b"".join(chunks)
    try:
        return body.decode(encoding, "replace")
    except (LookupError, TypeError):
        return body.decode("utf-8", "replace")


def _fetch_url_text(url: str, *, timeout: float, max_bytes: int,
                    max_chars: int, proxy: "str | None") -> "tuple[str, str]":
    """Return ``(title, extracted_text)`` for a general URL.

    Direct fetch first; one proxy retry on failure/empty.
    """
    def _attempt(use_proxy: "str | None"):
        html_text = _http_get_text(url, timeout=timeout, max_bytes=max_bytes,
                                   proxy=use_proxy)
        return _extract_readable(html_text, max_chars)

    try:
        title, text = _attempt(None)
        if text.strip():
            return title, text
    except Exception as exc:  # noqa: BLE001
        logger.info("summarizer: direct fetch failed for %s: %s", url, exc)

    if proxy:
        try:
            title, text = _attempt(proxy)
            return title, text
        except Exception as exc:  # noqa: BLE001
            logger.info("summarizer: proxy fetch failed for %s: %s", url, exc)
    return "", ""


def _extract_readable(html_text: str, max_chars: int) -> "tuple[str, str]":
    """Extract ``(title, readable_text)`` from HTML with lxml."""
    if not html_text:
        return "", ""
    try:
        from lxml import html as lxml_html
        doc = lxml_html.fromstring(html_text)
    except Exception:  # noqa: BLE001 — malformed HTML, etc.
        return "", ""

    title = ""
    title_nodes = doc.xpath("//title/text()")
    if title_nodes:
        title = _collapse_ws(str(title_nodes[0]))[:300]

    # Strip non-content elements.
    for tag in ("script", "style", "nav", "header", "footer", "aside",
                "form", "noscript", "template", "iframe", "svg"):
        for el in doc.xpath(f"//{tag}"):
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)

    # Prefer the main article container when present.
    container = None
    for xp in ("//article", "//main", "//*[@role='main']"):
        nodes = doc.xpath(xp)
        if nodes:
            container = nodes[0]
            break
    if container is None:
        body = doc.xpath("//body")
        container = body[0] if body else doc

    text = container.text_content() if container is not None else ""
    text = _collapse_ws(text)
    return title, text[:max_chars]


def _collapse_ws(text: str) -> str:
    if not text:
        return ""
    # Collapse runs of whitespace (incl. newlines) into single spaces.
    return re.sub(r"\s+", " ", text).strip()


# ── Proxy resolution ──────────────────────────────────────────────────────────

def _proxy_url() -> "str | None":
    """The proxy to use on a failed/blocked direct fetch, or None.

    ``summarizer.proxy_url`` wins; otherwise reuse the instance-wide fallback
    proxy (``outgoing.fallback_proxies`` / PROXY_* env vars). A dict mapping
    (httpx ``{pattern: url}``) is reduced to its ``all://`` / first value so we
    can pass a single proxy URL to httpx.
    """
    explicit = (_setting("proxy_url") or "").strip()
    if explicit:
        return explicit
    try:
        from searx.network.network import _get_fallback_proxies
        fb = _get_fallback_proxies()
    except Exception as exc:  # noqa: BLE001
        logger.info("summarizer: could not resolve fallback proxy: %s", exc)
        return None
    if not fb:
        return None
    if isinstance(fb, str):
        return fb
    if isinstance(fb, dict):
        return (fb.get("all://") or fb.get("https://")
                or next(iter(fb.values()), None))
    return None


# ── Content acquisition entry point ───────────────────────────────────────────

def _acquire_content(url: str) -> "tuple[str, str, str]":
    """Fetch + extract content for ``url``.

    Returns ``(kind, title, content)`` where ``kind`` is ``"youtube"`` or
    ``"url"``. ``content`` may be empty if extraction failed.
    """
    timeout = float(_setting("fetch_timeout", 12))
    max_bytes = int(_setting("fetch_max_bytes", 1_572_864))
    max_chars = int(_setting("extract_max_chars", 12000))
    proxy = _proxy_url()

    parts = urlsplit(url)
    video_id = _youtube_id(parts)
    if video_id:
        title, content = _fetch_youtube(
            video_id, timeout=timeout, max_bytes=max_bytes,
            max_chars=max_chars, proxy=proxy,
        )
        return "youtube", title, content

    title, content = _fetch_url_text(
        url, timeout=timeout, max_bytes=max_bytes,
        max_chars=max_chars, proxy=proxy,
    )
    return "url", title, content


def _build_messages(kind: str, url: str, title: str, content: str) -> "list[dict]":
    system = _SYSTEM_PROMPT_YOUTUBE if kind == "youtube" else _SYSTEM_PROMPT_URL
    if content.strip():
        label = "Video transcript" if kind == "youtube" else "Page content"
        user = f"Title: {title or '(unknown)'}\nSource: {url}\n\n{label}:\n{content}"
    else:
        user = (
            f"Title: {title or '(unknown)'}\nSource: {url}\n\n"
            "No readable content could be extracted from this source."
        )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


# ── SSE payload producer ──────────────────────────────────────────────────────

def _summary_payloads(url: str):
    """Yield bare SSE payload strings for summarizing ``url`` (see module doc)."""
    cache = _get_cache()
    cache_key = url  # already normalized by _validate_url

    cached = cache.get(cache_key, "summary")
    if cached:
        yield "\"[CACHED]\""
        yield json.dumps(cached)
        yield "[DONE]"
        return

    try:
        kind, title, content = _acquire_content(url)
    except Exception as exc:  # noqa: BLE001 — never let acquisition raise out
        logger.warning("summarizer: content acquisition error: %s", exc)
        kind, title, content = "url", "", ""

    model = _setting("model", "openai/gpt-4o-mini")
    api_key = ai_common.resolve_api_key(_setting("api_key"))
    if not model or not api_key:
        logger.error("summarizer: model or api_key missing — cannot summarize")
        yield json.dumps("[ERROR] Summarizer is not configured")
        yield "[DONE]"
        return

    messages = _build_messages(kind, url, title, content)
    base_url = ai_common.normalize_base_url(_setting("base_url"), _DEFAULT_BASE_URL)
    max_tokens = int(_setting("max_tokens", 500))
    timeout = float(_setting("timeout", 60))
    referer = _setting("http_referer", "") or ""
    x_title = _setting("x_title", "uSearch Summarizer") or ""

    chunks: list = []
    gen = None
    try:
        gen = ai_common.stream_chat(
            messages,
            base_url=base_url,
            api_key=api_key,
            model=model,
            max_tokens=max_tokens,
            timeout=timeout,
            referer=referer,
            title=x_title,
        )
        for chunk in gen:
            chunks.append(chunk)
            yield json.dumps(chunk)
    except Exception as exc:  # noqa: BLE001
        logger.warning("summarizer: LLM stream error: %s", exc)
        if not chunks:
            yield json.dumps("[ERROR] Could not generate summary")
    finally:
        if gen is not None:
            gen.close()

    if chunks:
        cache.set(cache_key, "summary", "".join(chunks))
    yield "[DONE]"


class SXNGPlugin(Plugin):
    """Universal Summarizer — summarize any URL or YouTube video on demand."""

    id = "summarizer"
    keywords: list = []

    def __init__(self, plg_cfg: "PluginCfg") -> None:
        super().__init__(plg_cfg)
        self.info = PluginInfo(
            id=self.id,
            name=_("Universal Summarizer"),
            description=_("Summarize any URL or YouTube video on demand"),
            preference_section="general",
        )

    def init(self, app) -> bool:
        # Warm the cache singleton (best-effort; safe if it falls back).
        _get_cache()

        from flask import request as freq, Response, stream_with_context

        _SSE_HEADERS = {"X-Accel-Buffering": "no", "Cache-Control": "no-cache"}

        def _rate_ok(ip: str) -> bool:
            capacity = float(_setting("rate_limit_capacity", 5))
            rate = float(_setting("rate_limit_rate", 0.5))
            return _bucket.check(ip, capacity, rate)

        def _early(payloads):
            """A non-streamed plaintext SSE response from bare payloads."""
            return Response(
                "".join(ai_common.sse_plain(payloads)),
                mimetype="text/event-stream", headers=_SSE_HEADERS,
            )

        def _early_enc(session, payloads, status=200):
            return Response(
                "".join(ai_common.sse_encrypted(session, payloads)),
                status=status, mimetype="text/event-stream", headers=_SSE_HEADERS,
            )

        # ── Plaintext GET ────────────────────────────────────────────────
        @app.route("/ai_summarize", methods=["GET"])
        def ai_summarize_api():
            ip = ai_common.client_ip(freq)
            if not _rate_ok(ip):
                return Response(
                    "data: \"[ERROR] Rate limit exceeded\"\ndata: [DONE]\n\n",
                    status=429, mimetype="text/event-stream", headers=_SSE_HEADERS,
                )
            raw = freq.args.get("url", "").strip()
            validated = _validate_url(raw)
            if validated is None:
                return _early(["\"[ERROR] Invalid or unsupported URL\"", "[DONE]"])
            url, _host = validated
            return Response(
                stream_with_context(ai_common.sse_plain(_summary_payloads(url))),
                mimetype="text/event-stream", headers=_SSE_HEADERS,
            )

        # ── Encrypted POST ───────────────────────────────────────────────
        @app.route("/eai_summarize", methods=["POST"])
        def eai_summarize_api():
            from searx import echannel

            ip = ai_common.client_ip(freq)
            body = freq.get_json(silent=True)
            try:
                plaintext, session = ai_common.open_encrypted_request(body)
            except echannel.EChannelError:
                return Response("bad request", status=400)
            try:
                req = json.loads(plaintext)
                raw = (req.get("url", "") if isinstance(req, dict) else "").strip()
            except Exception:  # noqa: BLE001
                return Response("bad request", status=400)

            if not _rate_ok(ip):
                return _early_enc(session,
                                  ["\"[ERROR] Rate limit exceeded\"", "[DONE]"],
                                  status=429)
            validated = _validate_url(raw)
            if validated is None:
                return _early_enc(session,
                                  ["\"[ERROR] Invalid or unsupported URL\"", "[DONE]"])
            url, _host = validated
            return Response(
                stream_with_context(
                    ai_common.sse_encrypted(session, _summary_payloads(url))),
                mimetype="text/event-stream", headers=_SSE_HEADERS,
            )

        # ── Paste-a-URL page ─────────────────────────────────────────────
        @app.route("/summarize", methods=["GET"])
        def summarize_page():
            # Lazy import so the webapp's render() (which injects client_settings
            # incl. e2e_pubkey) is available without a circular import at load.
            from searx import webapp
            prefill = freq.args.get("url", "").strip()[:_MAX_URL_LEN]
            # render() prepends the active theme ("simple/"), so pass the bare name.
            return webapp.render("summarize.html", prefill_url=prefill)

        return True
