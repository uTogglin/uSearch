# SPDX-License-Identifier: AGPL-3.0-or-later
"""
SearXNG Card Meta Plugin — GitHub & Reddit SERP cards
=====================================================
Turns GitHub repo and Reddit thread results into rich "cards" that show live
stats (GitHub stars / forks / last push, Reddit score / comments) right in the
search results page.

Privacy model (mirrors ai_summary):
  - The browser only ever talks to THIS origin: GET /card_meta?url=<result url>
  - The origin fetches the public GitHub JSON server-side, so the user's IP and
    query never leak to GitHub.
  - Only github.com / reddit.com thread URLs are accepted, and the outbound
    request is rebuilt from the parsed owner/repo or post id — the client can
    never steer the fetch at anything else (no SSRF).
  - Responses are cached (in-memory, TTL) and rate-limited per IP, so this costs
    nothing extra: both upstream endpoints are free and unauthenticated, and the
    cache keeps us comfortably under their anonymous rate limits.

The frontend (serp_enhance.js) is injected on every results page by this
plugin's after_request hook, exactly like ai_summary.js.

CONFIGURATION (settings.yml)
------------------------------
  plugins:
    searx.plugins.card_meta.SXNGPlugin:
      active: true

  card_meta:
    timeout:    6        # upstream request timeout (seconds)
    cache_ttl:  3600     # cache lifetime for fetched metadata (seconds)
    cache_max:  1000     # max cached entries
    rate_limit_capacity: 20
    rate_limit_rate:     2.0
    # Optional: a GitHub token (or the GITHUB_TOKEN env var) raises the
    # anonymous 60/hr limit to 5000/hr. Entirely optional — leave empty.
    github_token: ""
"""

import json
import logging
import os
import re
import threading
import time
import typing as t
from urllib.parse import urlsplit

import httpx
from flask_babel import gettext as _
from searx import get_setting
from searx.plugins import Plugin, PluginInfo

if t.TYPE_CHECKING:
    from searx.plugins import PluginCfg

logger = logging.getLogger(__name__)

_USER_AGENT = "uSearch-card-meta/1.0 (+https://search.utoggl.in)"

# Path segments that look like "owner/repo" but are really GitHub site pages.
_GH_RESERVED = {
    "orgs", "sponsors", "features", "about", "pricing", "marketplace",
    "topics", "collections", "trending", "settings", "notifications",
    "explore", "login", "join", "new", "search", "apps", "users",
    "organizations", "site", "contact", "readme", "watching", "stars",
    "dashboard", "account", "codespaces", "issues", "pulls",
}
_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,100}$")

# Reddit thread (base36) post id, e.g. /r/python/comments/1abcdef/...
_REDDIT_ID_RE = re.compile(r"^[A-Za-z0-9]{4,12}$")


def _setting(key: str, default=None):
    return get_setting(f"card_meta.{key}", default)


# ── In-memory TTL cache ───────────────────────────────────────────────────────
_cache: dict = {}          # {api_key: {"data": dict, "ts": float}}
_cache_lock = threading.Lock()


def _cache_get(key: str):
    ttl = float(_setting("cache_ttl", 3600))
    with _cache_lock:
        entry = _cache.get(key)
        if entry and time.time() - entry["ts"] < ttl:
            return entry["data"]
    return None


def _cache_set(key: str, data: dict):
    cap = int(_setting("cache_max", 1000))
    with _cache_lock:
        now = time.time()
        ttl = float(_setting("cache_ttl", 3600))
        expired = [k for k, v in list(_cache.items()) if now - v["ts"] > ttl]
        for k in expired:
            del _cache[k]
        if key not in _cache and len(_cache) >= cap:
            oldest = min(_cache, key=lambda k: _cache[k]["ts"])
            del _cache[oldest]
        _cache[key] = {"data": data, "ts": now}


# ── Per-IP token-bucket rate limiter ─────────────────────────────────────────
_rate_buckets: dict = {}
_rate_lock = threading.Lock()


def _check_rate_limit(ip: str) -> bool:
    capacity = float(_setting("rate_limit_capacity", 20))
    rate = float(_setting("rate_limit_rate", 2.0))
    now = time.time()
    with _rate_lock:
        bucket = _rate_buckets.get(ip)
        if bucket is None:
            _rate_buckets[ip] = {"tokens": capacity - 1, "last": now}
            return True
        elapsed = now - bucket["last"]
        tokens = min(capacity, bucket["tokens"] + elapsed * rate)
        bucket["last"] = now
        if tokens >= 1:
            bucket["tokens"] = tokens - 1
            idle = [k for k, v in list(_rate_buckets.items())
                    if v["tokens"] >= capacity and now - v["last"] > capacity / rate]
            for k in idle:
                del _rate_buckets[k]
            return True
        bucket["tokens"] = tokens
        return False


# ── URL classification ────────────────────────────────────────────────────────

def _classify(raw_url: str):
    """Map a result URL to a canonical upstream fetch descriptor, or None.

    Returns (kind, cache_key, fetch_callable) where fetch_callable() performs
    the server-side request and returns the normalised card dict.
    """
    try:
        parts = urlsplit(raw_url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower().lstrip(".")
    if host.startswith("www."):
        host = host[4:]
    segs = [s for s in parts.path.split("/") if s]

    # GitHub repo: github.com/<owner>/<repo>[/...]
    if host == "github.com" and len(segs) >= 2:
        owner, repo = segs[0], segs[1]
        if repo.endswith(".git"):
            repo = repo[:-4]
        if (owner.lower() not in _GH_RESERVED
                and _NAME_RE.match(owner) and _NAME_RE.match(repo)):
            key = f"gh:{owner.lower()}/{repo.lower()}"
            return ("github", key, lambda: _fetch_github(owner, repo))

    # Reddit thread: reddit.com/r/<sub>/comments/<id>/...  (also /comments/<id>)
    if host == "reddit.com" or host.endswith(".reddit.com"):
        if "comments" in segs:
            i = segs.index("comments")
            if i + 1 < len(segs) and _REDDIT_ID_RE.match(segs[i + 1]):
                post_id = segs[i + 1]
                sub = segs[1] if len(segs) >= 2 and segs[0] == "r" else ""
                key = f"re:{post_id.lower()}"
                return ("reddit", key, lambda: _fetch_reddit(post_id, sub))

    return None


# ── Upstream fetchers ─────────────────────────────────────────────────────────

def _http_json(url: str, headers: dict):
    timeout = float(_setting("timeout", 6))
    h = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    h.update(headers or {})
    resp = httpx.get(url, headers=h, timeout=timeout, follow_redirects=True)
    resp.raise_for_status()
    return resp.json()


def _github_headers() -> dict:
    token = (_setting("github_token") or os.environ.get("GITHUB_TOKEN", "") or "").strip()
    h = {"Accept": "application/vnd.github+json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _fetch_github(owner: str, repo: str) -> dict:
    data = _http_json(f"https://api.github.com/repos/{owner}/{repo}", _github_headers())
    return {
        "type": "github",
        "full_name": data.get("full_name") or f"{owner}/{repo}",
        "description": (data.get("description") or "")[:300],
        "stars": int(data.get("stargazers_count") or 0),
        "forks": int(data.get("forks_count") or 0),
        "issues": int(data.get("open_issues_count") or 0),
        "language": data.get("language") or "",
        "pushed_at": data.get("pushed_at") or "",
        "archived": bool(data.get("archived")),
    }


def _fetch_reddit(post_id: str, sub: str) -> dict:
    # Reddit's public thread JSON: append .json to the thread. limit=1 keeps the
    # comment listing small — we only want the post (children[0]) metadata.
    url = f"https://www.reddit.com/comments/{post_id}.json?limit=1&raw_json=1"
    data = _http_json(url, {})
    try:
        post = data[0]["data"]["children"][0]["data"]
    except (KeyError, IndexError, TypeError):
        post = {}
    return {
        "type": "reddit",
        "title": (post.get("title") or "")[:300],
        "subreddit": post.get("subreddit") or sub,
        "score": int(post.get("score") or 0),
        "comments": int(post.get("num_comments") or 0),
        "author": post.get("author") or "",
        "created_utc": float(post.get("created_utc") or 0),
        "nsfw": bool(post.get("over_18")),
    }


class SXNGPlugin(Plugin):
    """GitHub result cards — server-side metadata proxy + injected JS."""

    id = "card_meta"
    keywords: list = []

    def __init__(self, plg_cfg: "PluginCfg") -> None:
        super().__init__(plg_cfg)
        self.info = PluginInfo(
            id=self.id,
            name=_("Result Cards"),
            description=_("Show GitHub repo and Reddit thread stats in results"),
            preference_section="general",
        )

    def init(self, app) -> bool:
        from flask import request as freq, Response

        def _client_ip() -> str:
            return (freq.headers.get("X-Forwarded-For", "")
                    or freq.remote_addr or "").split(",")[0].strip()

        @app.route("/card_meta", methods=["GET"])
        def card_meta():
            url = freq.args.get("url", "").strip()[:600]
            if not url:
                return Response('{"type":null}', mimetype="application/json", status=400)

            descriptor = _classify(url)
            if descriptor is None:
                return Response('{"type":null}', mimetype="application/json")
            kind, cache_key, fetch = descriptor

            cached = _cache_get(cache_key)
            if cached is not None:
                return Response(json.dumps(cached), mimetype="application/json",
                                headers={"X-Cache": "HIT"})

            if not _check_rate_limit(_client_ip()):
                return Response('{"type":null,"error":"rate_limited"}',
                                mimetype="application/json", status=429)

            try:
                data = fetch()
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                # Negative-cache 404 so a deleted/private item isn't refetched.
                if code == 404:
                    miss = {"type": None, "missing": True}
                    _cache_set(cache_key, miss)
                    return Response(json.dumps(miss), mimetype="application/json")
                logger.info("card_meta %s upstream %s", kind, code)
                return Response('{"type":null,"error":"upstream"}',
                                mimetype="application/json", status=502)
            except Exception as exc:  # noqa: BLE001 — never 500 the SERP
                logger.warning("card_meta %s error: %s", kind, exc)
                return Response('{"type":null,"error":"fetch"}',
                                mimetype="application/json", status=502)

            _cache_set(cache_key, data)
            return Response(json.dumps(data), mimetype="application/json",
                            headers={"X-Cache": "MISS"})

        # ── Script injection ─────────────────────────────────────────────
        @app.after_request
        def inject_card_script(response):
            if not response.content_type.startswith("text/html"):
                return response
            try:
                body = response.get_data(as_text=True)
                if 'id="results"' not in body and "id='results'" not in body:
                    return response
                _v = str(int(time.time() // 60))
                script = ('\n<script src="/static/themes/simple/js/serp_enhance.js'
                          f'?v={_v}"></script>')
                body = body.replace("</body>", script + "\n</body>")
                response.set_data(body)
            except Exception as exc:  # noqa: BLE001
                logger.warning("card_meta inject error: %s", exc)
            return response

        return True
