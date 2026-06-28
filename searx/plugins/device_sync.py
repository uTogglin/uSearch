# SPDX-License-Identifier: AGPL-3.0-or-later
"""
SearXNG Device Sync Plugin (uSearch fork)
=========================================
Generate a short, typeable code on the home page; enter it on another device to
restore your settings. Two endpoints, both local to this instance:

  POST /sync/create   — bundle the requester's settings, park them server-side
                        (see :mod:`searx.sync_code`), return a one-time code.
  POST /sync/restore  — exchange a code for its bundle exactly once.

The bundle carries:
  * ``prefs``        — the standard SearXNG preferences hash, read SERVER-SIDE
                       from the requester's own cookies (never client-supplied),
                       via ``Preferences.get_as_url_params()``.
  * ``lenses``       — this fork's ``usearch.lenses`` localStorage (Kagi-style
                       saved filter sets), supplied by the client.
  * ``sitePriority`` — this fork's ``usearch.sitePriority`` localStorage
                       (per-site Block/Lower/Raise/Pin), supplied by the client.
  * ``bangMode``     — the ``featured_bang_mode`` cookie value.

Privacy (the whole point of this feature):
  * Nothing identifying is stored — the parked entry is just the opaque bundle
    plus an expiry (see :mod:`searx.sync_code`). These handlers log no request
    body and no client IP.
  * The ``usearch.adminToken`` credential is deliberately NOT part of the bundle.
  * Codes are one-time and short-lived, are never tied to a search, and never
    leave this instance.

The home-page UI lives in ``serp_enhance.js`` (already injected on every page by
the card_meta plugin); this module only provides the JSON endpoints.

CONFIGURATION (settings.yml)
------------------------------
  plugins:
    searx.plugins.device_sync.SXNGPlugin:
      active: true

  sync_code:
    path:       ""        # JSON store path (default: Fly volume, temp fallback)
    ttl:        1800      # seconds a generated code stays valid (default 30 min)
    max_codes:  5000      # cap on simultaneously-parked codes
    max_bytes:  65536     # reject bundles larger than this (base64 chars)
    rate_limit_capacity: 12
    rate_limit_rate:     0.2
"""

import json
import logging
import threading
import time
import typing as t
from base64 import urlsafe_b64encode, urlsafe_b64decode
from zlib import compress, decompress, error as ZlibError

from flask_babel import gettext as _
from searx import get_setting, sync_code
from searx.extended_types import sxng_request
from searx.plugins import Plugin, PluginInfo

if t.TYPE_CHECKING:
    from searx.plugins import PluginCfg

logger = logging.getLogger(__name__)

_BUNDLE_VERSION = 1
# Client-supplied JSON parts are capped before they ever reach the store, so a
# single request can't park a huge blob. Generous enough for real lens/priority
# collections, small enough to bound abuse.
_MAX_PART_BYTES = 32 * 1024


def _setting(key: str, default=None):
    return get_setting(f"sync_code.{key}", default)


# ── Per-IP token-bucket rate limiter (ephemeral, never tied to a code) ─────────
_rate_buckets: dict = {}
_rate_lock = threading.Lock()


def _check_rate_limit(ip: str) -> bool:
    capacity = float(_setting("rate_limit_capacity", 12))
    rate = float(_setting("rate_limit_rate", 0.2))
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


def _clean_part(value, allow) -> "object | None":
    """Validate one client-supplied bundle part: must be JSON-serialisable,
    of an allowed type, and within the per-part size cap. Returns the value, or
    ``None`` if it fails (callers substitute a safe default)."""
    if value is None or not isinstance(value, allow):
        return None
    try:
        if len(json.dumps(value)) > _MAX_PART_BYTES:
            return None
    except (TypeError, ValueError):
        return None
    return value


class SXNGPlugin(Plugin):
    """Cross-device settings transfer via one-time codes."""

    id = "device_sync"
    keywords: list = []

    def __init__(self, plg_cfg: "PluginCfg") -> None:
        super().__init__(plg_cfg)
        self.info = PluginInfo(
            id=self.id,
            name=_("Device Sync"),
            description=_("Move your settings to another device with a one-time code"),
            preference_section="general",
        )

    def init(self, app) -> bool:
        from flask import request as freq, Response

        def _json(payload: dict, status: int = 200) -> "Response":
            return Response(json.dumps(payload), mimetype="application/json", status=status)

        def _client_ip() -> str:
            return (freq.headers.get("X-Forwarded-For", "")
                    or freq.remote_addr or "").split(",")[0].strip()

        @app.route("/sync/create", methods=["POST"])
        def sync_create():
            if not _check_rate_limit(_client_ip()):
                return _json({"ok": False, "error": "rate_limited"}, 429)

            payload = freq.get_json(silent=True) or {}
            lenses = _clean_part(payload.get("lenses"), (dict,))
            site_priority = _clean_part(payload.get("sitePriority"), (dict,))
            bang_mode = payload.get("bangMode")
            if not isinstance(bang_mode, str) or len(bang_mode) > 32:
                bang_mode = ""

            # Preferences come from the requester's OWN cookies, server-side —
            # the client can't smuggle in someone else's (or a forged) prefs.
            try:
                prefs = sxng_request.preferences.get_as_url_params()
            except Exception as exc:  # noqa: BLE001 — never 500 the UI
                logger.warning("device_sync: prefs encode failed: %s", exc)
                prefs = ""

            bundle = {
                "v": _BUNDLE_VERSION,
                "prefs": prefs,
                "lenses": lenses or {},
                "sitePriority": site_priority or {},
                "bangMode": bang_mode,
            }
            try:
                bundle_b64 = urlsafe_b64encode(
                    compress(json.dumps(bundle, separators=(",", ":")).encode())
                ).decode()
            except (TypeError, ValueError) as exc:
                logger.warning("device_sync: bundle encode failed: %s", exc)
                return _json({"ok": False, "error": "encode_failed"}, 400)

            code = sync_code.create(bundle_b64)
            if not code:
                return _json({"ok": False, "error": "store_failed"}, 503)
            return _json({"ok": True, "code": code, "ttl": int(_setting("ttl", 1800))})

        @app.route("/sync/restore", methods=["POST"])
        def sync_restore():
            if not _check_rate_limit(_client_ip()):
                return _json({"ok": False, "error": "rate_limited"}, 429)

            payload = freq.get_json(silent=True) or {}
            code = payload.get("code", "")
            if not isinstance(code, str):
                return _json({"ok": False, "error": "bad_request"}, 400)

            bundle_b64 = sync_code.consume(code)
            if bundle_b64 is None:
                return _json({"ok": False, "error": "invalid_or_expired"}, 404)
            try:
                bundle = json.loads(decompress(urlsafe_b64decode(bundle_b64)).decode())
            except (ValueError, ZlibError, TypeError) as exc:
                logger.warning("device_sync: bundle decode failed: %s", exc)
                return _json({"ok": False, "error": "corrupt"}, 500)
            if not isinstance(bundle, dict):
                return _json({"ok": False, "error": "corrupt"}, 500)

            return _json({
                "ok": True,
                "prefs": bundle.get("prefs") or "",
                "lenses": bundle.get("lenses") or {},
                "sitePriority": bundle.get("sitePriority") or {},
                "bangMode": bundle.get("bangMode") or "",
            })

        return True
