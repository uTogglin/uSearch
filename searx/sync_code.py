# SPDX-License-Identifier: AGPL-3.0-or-later
"""Device-sync codes (uSearch fork) — privacy-first settings transfer.

A visitor can generate a short, typeable **code** on the home page that, entered
on another device, restores their settings (SearXNG preferences plus this fork's
browser-local Lenses / site priorities / bang mode). The bundle is too big to
embed in a typeable code, so it is parked here server-side, keyed by the code,
and handed back exactly once.

Privacy model — deliberately minimal:

  * A stored entry is ONLY ``{"b": <opaque bundle>, "exp": <unix expiry>}``.
    No IP, no User-Agent, no creation wall-clock, nothing that ties a code to a
    person. ``exp`` exists solely so unused codes self-delete.
  * **One-time**: :func:`consume` deletes the entry as it returns it, so the
    bundle lives on the server only for the brief window between "generate" on
    one device and "restore" on the other (and never past ``exp``).
  * Codes are never associated with searches and never leave this instance.

Storage mirrors :mod:`searx.site_priority`: a tiny JSON file on the Fly volume
(so multiple gunicorn workers + scale-to-zero resume share it), guarded by a
lock and written atomically. Unlike that module the data here is short-lived, so
every read opportunistically garbage-collects expired entries.
"""

import json
import os
import secrets
import tempfile
import threading
import time

from searx import logger, get_setting

log = logger.getChild("sync_code")

# Crockford-ish base32 alphabet: no 0/O/1/I/L/U so a hand-typed code is
# unambiguous. 9 chars over 30 symbols ≈ 44 bits — ample for a one-time secret
# that also expires in minutes.
_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
_CODE_LEN = 9
_GROUP = 3  # display/normalise as XXX-XXX-XXX

# Defaults (overridable via settings.yml ``sync_code:``).
_DEF_TTL = 1800          # 30 min
_DEF_MAX_CODES = 5000    # cap stored entries (abuse / disk bound)
_DEF_MAX_BYTES = 64 * 1024  # reject oversized bundles

_lock = threading.Lock()
_path_cache: "str | None" = None


def _setting(key: str, default):
    return get_setting(f"sync_code.{key}", default)


def _store_path() -> str:
    """Resolve (and remember) a writable path for the JSON store.

    Defaults to the Fly volume (same mount as the favicon / site-priority
    stores); falls back to the system temp dir for local dev or if the
    configured directory isn't writable.
    """
    global _path_cache  # pylint: disable=global-statement
    if _path_cache is not None:
        return _path_cache
    configured = (_setting("path", "") or "").strip()
    candidate = configured or "/var/cache/searxng/sync_codes.json"
    parent = os.path.dirname(candidate) or "."
    try:
        os.makedirs(parent, exist_ok=True)
        if os.access(parent, os.W_OK):
            _path_cache = candidate
            return _path_cache
    except OSError:
        pass
    fallback = os.path.join(tempfile.gettempdir(), "sync_codes.json")
    log.warning("sync_code: %s not writable, using %s", candidate, fallback)
    _path_cache = fallback
    return _path_cache


def _read(path: str) -> dict:
    """Load the raw store from disk (empty dict on any problem)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            return raw
    except FileNotFoundError:
        pass
    except (ValueError, OSError) as exc:
        log.warning("sync_code: failed to read %s: %s", path, exc)
    return {}


def _prune(data: dict, now: int) -> dict:
    """Drop expired entries; keep only well-formed ones."""
    out = {}
    for code, entry in data.items():
        if not isinstance(entry, dict):
            continue
        exp = entry.get("exp")
        if not isinstance(exp, int) or exp <= now:
            continue
        if not isinstance(entry.get("b"), str):
            continue
        out[code] = {"b": entry["b"], "exp": exp}
    return out


def _write(path: str, data: dict) -> bool:
    """Atomically replace the store file."""
    tmp = f"{path}.{os.getpid()}.{int(time.time() * 1000)}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
        return True
    except OSError as exc:
        log.warning("sync_code: failed to write %s: %s", path, exc)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def normalize(code: str) -> str:
    """Canonicalise a user-typed code: upper-case, strip groupers/whitespace."""
    if not code:
        return ""
    out = []
    for ch in code.upper():
        if ch in _ALPHABET:
            out.append(ch)
    return "".join(out)


def _new_code() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LEN))


def grouped(code: str) -> str:
    """Render a bare code as ``XXX-XXX-XXX`` for display."""
    code = normalize(code)
    return "-".join(code[i:i + _GROUP] for i in range(0, len(code), _GROUP))


def create(bundle_b64: str, ttl: "int | None" = None) -> "str | None":
    """Park a bundle and return its one-time code (grouped for display).

    Returns ``None`` if the bundle is too large or the store can't be written.
    """
    if not isinstance(bundle_b64, str):
        return None
    max_bytes = int(_setting("max_bytes", _DEF_MAX_BYTES))
    if len(bundle_b64) > max_bytes:
        log.info("sync_code: bundle rejected (%d > %d bytes)", len(bundle_b64), max_bytes)
        return None
    if ttl is None:
        ttl = int(_setting("ttl", _DEF_TTL))
    max_codes = int(_setting("max_codes", _DEF_MAX_CODES))
    now = int(time.time())
    path = _store_path()
    with _lock:
        data = _prune(_read(path), now)
        if len(data) >= max_codes:
            log.warning("sync_code: store full (%d entries), refusing new code", len(data))
            return None
        for _ in range(8):  # collision retries (practically never needed)
            code = _new_code()
            if code not in data:
                break
        else:
            return None
        data[code] = {"b": bundle_b64, "exp": now + ttl}
        if not _write(path, data):
            return None
        return grouped(code)


def consume(code: str) -> "str | None":
    """Return the bundle for ``code`` exactly once, deleting it.

    Returns ``None`` if the code is unknown or expired.
    """
    code = normalize(code)
    if len(code) != _CODE_LEN:
        return None
    now = int(time.time())
    path = _store_path()
    with _lock:
        data = _read(path)
        entry = data.get(code)
        bundle = None
        if isinstance(entry, dict) and isinstance(entry.get("b"), str) \
                and isinstance(entry.get("exp"), int) and entry["exp"] > now:
            bundle = entry["b"]
        # Delete the consumed code and sweep expired ones in the same write.
        pruned = _prune(data, now)
        pruned.pop(code, None)
        if pruned != data:
            _write(path, pruned)
        return bundle
