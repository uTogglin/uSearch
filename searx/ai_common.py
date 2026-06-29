# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Shared AI plumbing for uSearch's OpenRouter-backed features
===========================================================
Reusable helpers that back the Universal Summarizer and the Assistant (both
new) alongside the original AI Summary plugin. This module factors out the
proven patterns already battle-tested in :mod:`searx.plugins.ai_summary`:

  * OpenRouter (OpenAI-compatible) **streaming** chat completions
  * dual SSE transports — plaintext GET + per-frame-**encrypted** POST
    (via :mod:`searx.echannel`)
  * per-IP **token-bucket** rate limiting
  * a small **persistent SQLite** response cache

Design notes
------------
* Each consumer keeps its OWN settings namespace (``summarizer.*``,
  ``assistant.*``, …); the functions here take explicit parameters so they are
  namespace-agnostic.
* ``ai_summary.py`` is deliberately left UNTOUCHED — it is the live "AI
  Overview" box and we don't want to churn it. New features import from here.
* The OpenRouter API key resolves from an explicit setting first, then the
  ``OPENROUTER_API_KEY`` env var (same precedence as ai_summary), so the key
  never has to land in a committed ``settings.yml``.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import typing as t

import httpx

from searx import echannel

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


# ── OpenRouter config helpers ─────────────────────────────────────────────────

def resolve_api_key(setting_value: "str | None" = None) -> str:
    """Resolve the OpenRouter key: explicit setting first, then env var.

    Keeping the key in ``OPENROUTER_API_KEY`` is recommended so it never lands
    in a committed ``settings.yml``.
    """
    return (setting_value or os.environ.get("OPENROUTER_API_KEY", "") or "").strip()


def normalize_base_url(setting_value: "str | None", default: str = DEFAULT_BASE_URL) -> str:
    return (setting_value or default or DEFAULT_BASE_URL).rstrip("/")


# ── LLM streaming ─────────────────────────────────────────────────────────────

def stream_chat(
    messages: "list[dict]",
    *,
    base_url: str,
    api_key: str,
    model: str,
    max_tokens: int,
    temperature: float = 0.3,
    timeout: float = 60.0,
    referer: str = "",
    title: str = "",
) -> t.Iterator[str]:
    """Stream an OpenAI-compatible chat completion, yielding content deltas.

    ``messages`` is a standard ``[{"role": ..., "content": ...}, …]`` list, so
    this supports both one-shot summaries and multi-turn conversations. The
    optional OpenRouter ranking headers (``HTTP-Referer`` / ``X-Title``) are
    harmless for other OpenAI-compatible providers.

    Raises on transport/HTTP errors — callers wrap this in try/except and emit
    an error frame, exactly like ai_summary's ``_stream_llm``.
    """
    endpoint = normalize_base_url(base_url) + "/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    if referer:
        headers["HTTP-Referer"] = referer
    if title:
        headers["X-Title"] = title

    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
        "messages": messages,
    }

    with httpx.stream("POST", endpoint, headers=headers, json=payload, timeout=timeout) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if raw == "[DONE]":
                return
            try:
                chunk = json.loads(raw)
                delta = chunk["choices"][0]["delta"].get("content", "")
                if delta:
                    yield delta
            except (json.JSONDecodeError, KeyError, IndexError):
                continue


# ── Per-IP token-bucket rate limiter ──────────────────────────────────────────

class TokenBucket:
    """A standalone per-IP token bucket. Construct one per endpoint family.

    Mirrors the limiter in ai_summary/card_meta: idle, fully-refilled buckets
    are evicted opportunistically so the dict stays bounded.
    """

    def __init__(self) -> None:
        self._buckets: dict = {}
        self._lock = threading.Lock()

    def check(self, ip: str, capacity: float, rate: float) -> bool:
        """Return True if allowed, False if rate-limited. ``rate`` = tokens/sec."""
        capacity = float(capacity)
        rate = float(rate)
        now = time.time()
        with self._lock:
            bucket = self._buckets.get(ip)
            if bucket is None:
                self._buckets[ip] = {"tokens": capacity - 1, "last": now}
                return True
            elapsed = now - bucket["last"]
            tokens = min(capacity, bucket["tokens"] + elapsed * rate)
            bucket["last"] = now
            if tokens >= 1:
                bucket["tokens"] = tokens - 1
                if rate > 0:
                    idle = [k for k, v in list(self._buckets.items())
                            if v["tokens"] >= capacity and now - v["last"] > capacity / rate]
                    for k in idle:
                        del self._buckets[k]
                return True
            bucket["tokens"] = tokens
            return False


def client_ip(request) -> str:
    """First hop of X-Forwarded-For, else remote_addr."""
    return (request.headers.get("X-Forwarded-For", "")
            or request.remote_addr or "").split(",")[0].strip()


# ── SSE framing (plaintext + encrypted) ───────────────────────────────────────
# Each ``payloads`` iterable yields the *bare* payload strings — exactly the
# text that goes after ``data: `` and before ``\n\n`` (e.g. ``json.dumps(chunk)``,
# ``"[CACHED]"``, ``[DONE]``). One source of truth drives both transports.

def sse_plain(payloads: t.Iterable[str]) -> t.Iterator[str]:
    for payload in payloads:
        yield f"data: {payload}\n\n"


def sse_encrypted(session: "echannel.Session", payloads: t.Iterable[str]) -> t.Iterator[str]:
    for payload in payloads:
        yield f"data: {json.dumps(session.encrypt(payload))}\n\n"


def open_encrypted_request(body: "dict | None") -> "tuple[str, echannel.Session]":
    """Decrypt an inbound ``{epk,iv,ct}`` POST body to plaintext + a reply Session.

    Returns ``(plaintext_str, session)``. Raises :class:`echannel.EChannelError`
    on anything malformed (callers map that to HTTP 400). The caller is
    responsible for ``json.loads`` of the plaintext and field validation.
    """
    if not isinstance(body, dict):
        raise echannel.EChannelError("body must be a JSON object")
    plaintext, session = echannel.open_request(body)
    return plaintext.decode("utf-8", "replace"), session


# ── Persistent response cache (SQLite) ────────────────────────────────────────
# Stores completed LLM outputs keyed by (cache_key, kind). Survives restarts.
# Mirrors ai_summary's persistent summary cache; each consumer owns its own DB
# file (pass a distinct ``path``) so namespaces never collide.

class ResponseCache:
    def __init__(self, path: str, default_ttl: float = 604800.0, enabled: bool = True) -> None:
        self.path = path
        self.default_ttl = float(default_ttl)
        self.enabled = bool(enabled)
        self._lock = threading.Lock()
        if self.enabled:
            self._init_db()

    def _init_db(self) -> None:
        try:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with self._lock:
                with sqlite3.connect(self.path) as conn:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS ai_cache (
                            cache_key  TEXT NOT NULL,
                            kind       TEXT NOT NULL,
                            content    TEXT NOT NULL,
                            created_at REAL NOT NULL,
                            PRIMARY KEY (cache_key, kind)
                        )
                    """)
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_cache_created "
                                 "ON ai_cache(created_at)")
                    conn.commit()
            logger.info("ai_common: response cache at %s", self.path)
        except Exception as exc:  # noqa: BLE001 — cache is best-effort
            logger.warning("ai_common: could not init response cache: %s", exc)
            self.enabled = False

    def get(self, cache_key: str, kind: str, ttl: "float | None" = None) -> "str | None":
        if not self.enabled:
            return None
        ttl = self.default_ttl if ttl is None else float(ttl)
        try:
            with self._lock:
                with sqlite3.connect(self.path) as conn:
                    row = conn.execute(
                        "SELECT content, created_at FROM ai_cache WHERE cache_key=? AND kind=?",
                        (cache_key, kind),
                    ).fetchone()
            if row and (time.time() - row[1]) < ttl:
                return row[0]
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("ai_common cache get error: %s", exc)
            return None

    def set(self, cache_key: str, kind: str, content: str) -> None:
        if not self.enabled or not content:
            return
        try:
            with self._lock:
                with sqlite3.connect(self.path) as conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO ai_cache (cache_key, kind, content, created_at) "
                        "VALUES (?,?,?,?)",
                        (cache_key, kind, content, time.time()),
                    )
                    conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("ai_common cache set error: %s", exc)
