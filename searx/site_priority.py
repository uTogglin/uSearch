# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-site *global* ranking overrides (uSearch fork).

The SERP "shield" panel lets a visitor nudge a domain up or down, but those
adjustments live in their own browser (localStorage) and never touch the server.
An **admin** can additionally make an adjustment *global* — applied to every
user's results — and that is what this module stores.

Unlike the static ``hostnames:`` block in ``settings.yml`` (which only takes
effect on restart / redeploy), this store is read live on every request and is
editable at runtime through the admin-gated ``POST /site_priority`` endpoint, so
a global block/boost lands immediately and survives deploys (it is persisted on
the Fly volume alongside the favicon cache).

Levels (mirrors the client control):

    block  -> score forced to 0          (sinks to the very bottom / hidden)
    lower  -> score x0.25                 (demoted)
    raise  -> score x2.0                  (boosted)
    pin    -> score x2 + PIN_BUMP         (floats to the top)
    normal -> no entry (the default)

The store is a tiny JSON map ``{registrable_domain: level}`` and is reloaded
whenever the file's mtime changes, so multiple gunicorn workers stay in sync.
"""

import json
import os
import tempfile
import threading
import time

from searx import logger, get_setting

log = logger.getChild("site_priority")

LEVELS = ("block", "lower", "normal", "raise", "pin")

# Multiplicative factors (normal is implicit / never stored).
_LOWER_MULT = 0.25
_RAISE_MULT = 2.0
# Additive bump that lifts a pinned result clear of any organic score.
PIN_BUMP = 1_000_000.0

_lock = threading.Lock()
_store: "dict[str, str] | None" = None
_mtime: float = -1.0
_path_cache: "str | None" = None


def _store_path() -> str:
    """Resolve (and remember) a writable path for the JSON store.

    Defaults to the Fly volume (same mount as the favicon cache) so the map
    survives deploys; falls back to the system temp dir for local dev or if the
    configured directory isn't writable.
    """
    global _path_cache  # pylint: disable=global-statement
    if _path_cache is not None:
        return _path_cache
    configured = (get_setting("site_priority.path", "") or "").strip()
    candidate = configured or "/var/cache/searxng/site_priorities.json"
    parent = os.path.dirname(candidate) or "."
    try:
        os.makedirs(parent, exist_ok=True)
        # Probe writability without clobbering an existing store.
        if os.access(parent, os.W_OK):
            _path_cache = candidate
            return _path_cache
    except OSError:
        pass
    fallback = os.path.join(tempfile.gettempdir(), "site_priorities.json")
    log.warning("site_priority: %s not writable, using %s", candidate, fallback)
    _path_cache = fallback
    return _path_cache


def _load() -> "dict[str, str]":
    """Return the store, reloading from disk if the file changed."""
    global _store, _mtime  # pylint: disable=global-statement
    path = _store_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = -1.0
    with _lock:
        if _store is not None and mtime == _mtime:
            return _store
        data: "dict[str, str]" = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if isinstance(k, str) and v in LEVELS and v != "normal":
                        data[k.lower()] = v
        except FileNotFoundError:
            pass
        except (ValueError, OSError) as exc:
            log.warning("site_priority: failed to read %s: %s", path, exc)
        _store = data
        _mtime = mtime
        return _store


def _registrable(netloc: str) -> str:
    """Normalise a netloc to the key the panel shows: host minus ``www.`` /port."""
    host = (netloc or "").lower().split(":")[0].strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def _lookup_level(netloc: str) -> "str | None":
    """Best matching stored level for a host.

    Tries the exact host, then walks subdomains off the front (keeping at least
    two labels) so a rule on ``medium.com`` also covers ``blog.medium.com``.
    """
    store = _load()
    if not store:
        return None
    host = _registrable(netloc)
    if not host:
        return None
    labels = host.split(".")
    for i in range(len(labels) - 1):
        lvl = store.get(".".join(labels[i:]))
        if lvl:
            return lvl
    return None


def get(netloc: str) -> str:
    """Stored level for a host, or ``"normal"`` when no global rule applies."""
    return _lookup_level(netloc) or "normal"


def get_all() -> "dict[str, str]":
    return dict(_load())


def adjust(score: float, netloc: str) -> float:
    """Apply the global override (if any) to an organic score."""
    if not score:
        return score
    lvl = _lookup_level(netloc)
    if lvl is None or lvl == "normal":
        return score
    if lvl == "block":
        return 0.0
    if lvl == "lower":
        return score * _LOWER_MULT
    if lvl == "raise":
        return score * _RAISE_MULT
    if lvl == "pin":
        return score * _RAISE_MULT + PIN_BUMP
    return score


def set_level(netloc: str, level: str) -> bool:
    """Create / update / clear a global rule. ``normal`` removes the entry.

    Returns ``True`` on a successful write. Persisted atomically so a concurrent
    reader never sees a half-written file.
    """
    global _store, _mtime  # pylint: disable=global-statement
    if level not in LEVELS:
        return False
    host = _registrable(netloc)
    if not host:
        return False
    path = _store_path()
    with _lock:
        # Start from the current on-disk state to avoid clobbering a peer worker.
        data: "dict[str, str]" = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                data = {k.lower(): v for k, v in raw.items()
                        if isinstance(k, str) and v in LEVELS and v != "normal"}
        except (FileNotFoundError, ValueError, OSError):
            data = {}
        if level == "normal":
            data.pop(host, None)
        else:
            data[host] = level
        tmp = f"{path}.{os.getpid()}.{int(time.time() * 1000)}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=0, sort_keys=True)
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("site_priority: failed to write %s: %s", path, exc)
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False
        _store = data
        try:
            _mtime = os.path.getmtime(path)
        except OSError:
            _mtime = -1.0
        return True
