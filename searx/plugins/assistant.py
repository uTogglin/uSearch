# SPDX-License-Identifier: AGPL-3.0-or-later
"""
uSearch Assistant — multi-turn web-aware AI chat
================================================
A natural extension of the AI Summary box: a full conversational assistant
powered by `OpenRouter <https://openrouter.ai/>`_ (or any OpenAI-compatible
API) with optional **web access** via auto-search injection. Each user turn,
when web access is on, runs an in-process SearXNG general search for the latest
user message and injects the top results as grounded context with numbered
citations, exactly like the AI Summary feature but spanning a conversation.

It deliberately mirrors the proven idioms of :mod:`searx.plugins.ai_summary`
and reuses the shared plumbing in :mod:`searx.ai_common`:

  * dual SSE transports — plaintext ``POST /assistant_chat`` and per-frame
    **encrypted** ``POST /eassistant`` (via :mod:`searx.echannel`)
  * per-IP **token-bucket** rate limiting (strict — this is the heaviest
    endpoint: a multi-turn LLM call *plus* a web search per turn)
  * the SSE frame protocol: each frame is ``data: <payload>\\n\\n``; token
    payloads are ``json.dumps(str)``; ``[DONE]`` ends the stream; sentinels
    like ``"[SOURCES]"`` / ``"[ERROR] …"`` are JSON-string frames.

Endpoints
---------
  * ``POST /assistant_chat`` — plaintext SSE.  JSON body::

        {"messages": [{"role": "user"|"assistant", "content": "…"}, …],
         "web": true|false}

  * ``POST /eassistant`` — encrypted.  Body is ``{epk, iv, ct}`` whose
    decrypted plaintext is the SAME JSON object as above.  Every SSE frame
    payload is encrypted with the per-request session key.

  * ``GET /assistant`` — the chat HTML page.

SSE frame protocol (per turn)
-----------------------------
  1. *(web access only)* a ``"[SOURCES]"`` sentinel frame, immediately
     followed by a ``json.dumps([...])`` frame holding the sources array
     ``[{"title": …, "url": …}, …]`` (same two-frame trick as ai_summary's
     ``"[CACHED]"``).  The UI renders these as citations.
  2. zero or more token frames — each ``json.dumps(delta_str)``.
  3. a terminal ``[DONE]`` frame.
  On error: a ``"[ERROR] …"`` JSON-string frame, then ``[DONE]``.

CONFIGURATION (settings.yml)
----------------------------
This plugin is registered by the orchestrator — do NOT add it to settings.yml
here. The expected configuration block is::

  plugins:
    searx.plugins.assistant.SXNGPlugin:
      active: true

  assistant:
    base_url:            "https://openrouter.ai/api/v1"
    # api_key is read from this setting OR the OPENROUTER_API_KEY env var
    # (env var recommended so the key never lands in a committed settings.yml).
    api_key:             ""
    model:               "openai/gpt-4o-mini"   # chat model
    # Optional OpenRouter ranking headers (harmless for other providers)
    http_referer:        ""                     # e.g. "https://my-searxng.example"
    x_title:             "uSearch Assistant"
    max_tokens:          800
    temperature:         0.4
    timeout:             60
    web_results:         5          # top-N search results injected per turn
    max_turns:           12         # cap on conversation length
    max_message_chars:   4000       # cap on a single message
    max_total_chars:     24000      # cap on the whole conversation payload
    # Strict rate limit — heaviest endpoint (multi-turn LLM + a web search).
    rate_limit_capacity: 4
    rate_limit_rate:     0.3        # tokens / second

Note: a conversation is not cacheable like a single query, so there is no
ResponseCache here (intentionally skipped). Metrics are kept minimal.
"""

from __future__ import annotations

import json
import logging
import typing as t

from flask_babel import gettext as _

from searx import get_setting
from searx import ai_common
from searx.plugins import Plugin, PluginInfo

if t.TYPE_CHECKING:
    from searx.plugins import PluginCfg

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
_DEFAULT_MODEL = "openai/gpt-4o-mini"

# Hard ceilings — defensive caps applied on top of the (configurable) settings
# so a misconfiguration can never let an unbounded payload through.
_HARD_MAX_TURNS = 40
_HARD_MAX_MESSAGE_CHARS = 16000
_HARD_MAX_TOTAL_CHARS = 80000

_SYSTEM_BASE = (
    "You are uSearch Assistant, a concise, helpful AI assistant with web "
    "access. Answer the user's questions directly and accurately. Use plain "
    "prose; keep answers tight and avoid filler or meta-commentary."
)

_SYSTEM_WEB = (
    "You have been given web search results for the user's latest message, "
    "listed below as numbered sources. Ground your answer in these results and "
    "cite them inline using bracketed numbers like [1], [2] that match the "
    "source order. Synthesize across sources rather than listing them one by "
    "one. If the results do not cover the question, say so plainly and answer "
    "from your own knowledge, making clear which parts are not sourced."
)


def _setting(key: str, default=None):
    return get_setting(f"assistant.{key}", default)


# ── Per-IP token-bucket rate limiter ─────────────────────────────────────────
_rate = ai_common.TokenBucket()


def _check_rate_limit(ip: str) -> bool:
    capacity = _setting("rate_limit_capacity", 4)
    rate = _setting("rate_limit_rate", 0.3)
    return _rate.check(ip, capacity, rate)


# ── Conversation parsing + validation ────────────────────────────────────────

def _parse_conversation(plaintext: str) -> "tuple[list[dict], bool] | None":
    """Parse + validate the request JSON.

    Returns ``(messages, web)`` on success or ``None`` if malformed. ``messages``
    is a cleaned list of ``{"role", "content"}`` dicts ending in a user turn.
    """
    try:
        body = json.loads(plaintext)
    except Exception:
        return None
    if not isinstance(body, dict):
        return None

    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        return None

    web = bool(body.get("web", True))

    max_turns = min(int(_setting("max_turns", 12) or 12), _HARD_MAX_TURNS)
    max_msg = min(int(_setting("max_message_chars", 4000) or 4000), _HARD_MAX_MESSAGE_CHARS)
    max_total = min(int(_setting("max_total_chars", 24000) or 24000), _HARD_MAX_TOTAL_CHARS)

    # Keep only the most recent `max_turns` messages.
    raw_messages = raw_messages[-max_turns:]

    cleaned: list[dict] = []
    total = 0
    for m in raw_messages:
        if not isinstance(m, dict):
            return None
        role = m.get("role")
        content = m.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            return None
        content = content.strip()[:max_msg]
        if not content:
            continue
        total += len(content)
        if total > max_total:
            return None
        cleaned.append({"role": role, "content": content})

    if not cleaned:
        return None
    # The last message must be a user turn.
    if cleaned[-1]["role"] != "user":
        return None
    return cleaned, web


# ── In-process web search (auto-search injection) ────────────────────────────

def _read(result, key: str) -> str:
    try:
        val = getattr(result, key, None)
        if val is not None:
            return str(val)
        if hasattr(result, "get"):
            val = result.get(key)
            if val is not None:
                return str(val)
    except Exception:
        pass
    return ""


def _web_search(query: str, limit: int) -> "list[dict]":
    """Run an in-process SearXNG general search; return up to ``limit`` results
    as ``[{"title", "url", "content"}, …]``. Degrades to ``[]`` on any error —
    callers must never let this raise (no-web fallback)."""
    if not query:
        return []
    try:
        # Lazy imports — search machinery + engine registry.
        import searx.search  # pylint: disable=import-outside-toplevel
        from searx.search.models import SearchQuery  # pylint: disable=import-outside-toplevel
        from searx.webadapter import (  # pylint: disable=import-outside-toplevel
            get_engineref_from_category_list,
        )

        engineref_list = get_engineref_from_category_list(["general"], [])
        if not engineref_list:
            return []

        search_query = SearchQuery(
            query=query,
            engineref_list=engineref_list,
            lang="all",
            safesearch=0,
            pageno=1,
        )
        search_obj = searx.search.Search(search_query)
        container = search_obj.search()

        results: list[dict] = []
        for r in container.get_ordered_results():
            url = _read(r, "url")
            if not url:
                continue
            results.append(
                {
                    "title": _read(r, "title"),
                    "url": url,
                    "content": _read(r, "content")[:500],
                }
            )
            if len(results) >= limit:
                break
        return results
    except Exception as exc:  # noqa: BLE001 — web access is best-effort
        logger.warning("assistant: web search failed for %r: %s", query, exc)
        return []


def _build_context_block(results: "list[dict]") -> str:
    lines = ["Web search results for the user's latest message:\n"]
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        url = r.get("url", "")
        content = r.get("content", "")
        lines.append(f"[{i}] {title} ({url})\n{content}\n")
    return "\n".join(lines)


def _build_messages(conversation: "list[dict]", results: "list[dict]") -> "list[dict]":
    """Assemble the final messages list: a system message (web-grounding rules +
    numbered context when present) followed by the conversation."""
    system = _SYSTEM_BASE
    if results:
        system = system + "\n\n" + _SYSTEM_WEB + "\n\n" + _build_context_block(results)
    return [{"role": "system", "content": system}, *conversation]


# ── SSE payload producer ─────────────────────────────────────────────────────

def _chat_payloads(conversation: "list[dict]", web: bool):
    """Yield bare SSE payload strings for one turn (sources sentinel, tokens,
    [DONE]). Never raises — errors become an ``"[ERROR] …"`` frame."""
    results: list[dict] = []
    if web:
        query = conversation[-1]["content"]
        limit = int(_setting("web_results", 5) or 5)
        results = _web_search(query, max(1, limit))
        if results:
            sources = [{"title": r.get("title", ""), "url": r.get("url", "")} for r in results]
            yield "\"[SOURCES]\""
            yield json.dumps(sources)

    api_key = ai_common.resolve_api_key(_setting("api_key"))
    if not api_key:
        logger.error("assistant: no API key (set assistant.api_key or OPENROUTER_API_KEY)")
        yield json.dumps("[ERROR] Assistant is not configured")
        yield "[DONE]"
        return

    model = _setting("model", _DEFAULT_MODEL) or _DEFAULT_MODEL
    base_url = ai_common.normalize_base_url(_setting("base_url", _DEFAULT_BASE_URL))
    max_tokens = int(_setting("max_tokens", 800) or 800)
    temperature = float(_setting("temperature", 0.4) or 0.4)
    timeout = float(_setting("timeout", 60) or 60)
    referer = _setting("http_referer", "") or ""
    title = _setting("x_title", "") or ""

    messages = _build_messages(conversation, results)

    gen = None
    try:
        gen = ai_common.stream_chat(
            messages,
            base_url=base_url,
            api_key=api_key,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
            referer=referer,
            title=title,
        )
        for delta in gen:
            yield json.dumps(delta)
    except Exception as exc:  # noqa: BLE001 — never 500, always degrade
        logger.warning("assistant stream error: %s", exc)
        yield json.dumps("[ERROR] The assistant could not complete the reply")
    finally:
        if gen is not None:
            try:
                gen.close()
            except Exception:  # noqa: BLE001
                pass
    yield "[DONE]"


class SXNGPlugin(Plugin):
    """Multi-turn, web-aware AI assistant (OpenRouter-backed)."""

    id = "assistant"
    keywords: list = []

    def __init__(self, plg_cfg: "PluginCfg") -> None:
        super().__init__(plg_cfg)
        self.info = PluginInfo(
            id=self.id,
            name=_("Assistant"),
            description=_("Multi-turn AI assistant with web access"),
            preference_section="general",
        )

    def init(self, app) -> bool:
        from flask import request as freq, Response, stream_with_context

        _SSE_HEADERS = {"X-Accel-Buffering": "no", "Cache-Control": "no-cache"}

        def _err_stream(payloads, *, encrypt_with=None, status=200):
            """Build a one-shot SSE Response from bare payload strings."""
            if encrypt_with is not None:
                body = "".join(ai_common.sse_encrypted(encrypt_with, payloads))
            else:
                body = "".join(ai_common.sse_plain(payloads))
            return Response(
                body, status=status, mimetype="text/event-stream", headers=_SSE_HEADERS
            )

        # ── Plaintext endpoint ───────────────────────────────────────────
        @app.route("/assistant_chat", methods=["POST"])
        def assistant_chat_api():
            ip = ai_common.client_ip(freq)
            if not _check_rate_limit(ip):
                return _err_stream(
                    ["\"[ERROR] Rate limit exceeded\"", "[DONE]"], status=429
                )
            body = freq.get_json(silent=True)
            if not isinstance(body, dict):
                return Response("bad request", status=400)
            parsed = _parse_conversation(json.dumps(body))
            if parsed is None:
                return _err_stream(["\"[ERROR] Invalid request\"", "[DONE]"], status=400)
            conversation, web = parsed
            return Response(
                stream_with_context(ai_common.sse_plain(_chat_payloads(conversation, web))),
                mimetype="text/event-stream",
                headers=_SSE_HEADERS,
            )

        # ── Encrypted (E2E) endpoint ─────────────────────────────────────
        @app.route("/eassistant", methods=["POST"])
        def eassistant_api():
            ip = ai_common.client_ip(freq)
            body = freq.get_json(silent=True)
            try:
                plaintext, session = ai_common.open_encrypted_request(body)
            except Exception:  # echannel.EChannelError or anything malformed
                return Response("bad request", status=400)

            if not _check_rate_limit(ip):
                return _err_stream(
                    ["\"[ERROR] Rate limit exceeded\"", "[DONE]"],
                    encrypt_with=session,
                    status=429,
                )
            parsed = _parse_conversation(plaintext)
            if parsed is None:
                return _err_stream(
                    ["\"[ERROR] Invalid request\"", "[DONE]"], encrypt_with=session
                )
            conversation, web = parsed
            return Response(
                stream_with_context(
                    ai_common.sse_encrypted(session, _chat_payloads(conversation, web))
                ),
                mimetype="text/event-stream",
                headers=_SSE_HEADERS,
            )

        # ── Chat page ────────────────────────────────────────────────────
        @app.route("/assistant", methods=["GET"])
        def assistant_page():
            from searx import webapp  # pylint: disable=import-outside-toplevel
            # render() prepends the active theme ("simple/"), so pass the bare name.
            return webapp.render("assistant.html")

        return True
