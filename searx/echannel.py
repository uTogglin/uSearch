# SPDX-License-Identifier: AGPL-3.0-or-later
"""
uSearch application-layer encrypted channel (server half)
=========================================================
A small, dependency-light ECIES-style channel that lets the browser and this
Fly origin exchange data that the Cloudflare edge (which terminates TLS) only
ever sees as ciphertext.

Primitives (must stay byte-for-byte in sync with the client half,
``client/simple/src/js/util/echannel.ts``):

  * key agreement : ECDH on NIST P-256 (secp256r1)
  * key schedule  : HKDF-SHA256, fixed salt, direction-bound + epk-bound info
  * AEAD          : AES-256-GCM, 12-byte IV, 16-byte tag appended to ciphertext

The server holds one *static* P-256 key pair. Its public point is published to
the browser (raw uncompressed point, base64) via ``get_client_settings``; its
private key lives only in the ``E2E_PRIVATE_KEY`` Fly secret. Every request
carries a fresh *ephemeral* client public key (``epk``), so the server stays
completely stateless — it re-derives the shared secret per request. From that
secret two keys are derived:

  * ``k_c2s`` — client→server (decrypts requests)
  * ``k_s2c`` — server→client (encrypts responses / stream frames)

Wire shapes (JSON, all binary fields standard-base64):
  request  : {"epk": <raw65>, "iv": <12>, "ct": <ciphertext||tag>}
  response : {"iv": <12>, "ct": <ciphertext||tag>}
"""
from __future__ import annotations

import base64
import logging
import os
import typing as t

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_der_private_key,
)

logger = logging.getLogger(__name__)

# ── Protocol constants (keep in lockstep with echannel.ts) ────────────────────
_CURVE = ec.SECP256R1()
_HKDF_SALT = b"usearch-e2e/v1/salt"
_INFO_C2S = b"usearch-e2e/v1/c2s"
_INFO_S2C = b"usearch-e2e/v1/s2c"
_IV_LEN = 12
_RAW_PUBKEY_LEN = 65  # 0x04 || X(32) || Y(32) for an uncompressed P-256 point

_ENV_PRIVATE_KEY = "E2E_PRIVATE_KEY"


class EChannelError(Exception):
    """Raised on any malformed/forged/undecryptable payload (always → 400)."""


# ── Static server key ─────────────────────────────────────────────────────────

_private_key: "ec.EllipticCurvePrivateKey | None" = None
_loaded = False


def _load() -> "ec.EllipticCurvePrivateKey | None":
    """Load (and cache) the static private key from ``E2E_PRIVATE_KEY``.

    The env var holds a base64-encoded PKCS#8 DER P-256 private key
    (as emitted by ``tools/gen_e2e_key.py``). Returns ``None`` when unset so
    callers can decide whether the encrypted channel is simply disabled
    (local dev) versus required.
    """
    global _private_key, _loaded
    if _loaded:
        return _private_key
    _loaded = True
    raw = (os.environ.get(_ENV_PRIVATE_KEY, "") or "").strip()
    if not raw:
        logger.warning("echannel: %s unset — encrypted channel disabled", _ENV_PRIVATE_KEY)
        _private_key = None
        return None
    try:
        der = base64.b64decode(raw)
        key = load_der_private_key(der, password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
            raise EChannelError("E2E_PRIVATE_KEY is not a P-256 private key")
        _private_key = key
        logger.info("echannel: static P-256 key loaded; encrypted channel active")
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("echannel: failed to load %s: %s", _ENV_PRIVATE_KEY, exc)
        _private_key = None
    return _private_key


def is_enabled() -> bool:
    return _load() is not None


def public_key_b64() -> "str | None":
    """Raw uncompressed public point (65 bytes), base64 — for the client."""
    key = _load()
    if key is None:
        return None
    raw = key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return base64.b64encode(raw).decode("ascii")


# ── Key schedule ──────────────────────────────────────────────────────────────


def _derive_keys(epk_raw: bytes) -> "tuple[bytes, bytes]":
    """ECDH(static_priv, client_epk) → HKDF → (k_c2s, k_s2c)."""
    key = _load()
    if key is None:
        raise EChannelError("encrypted channel not configured")
    try:
        peer = ec.EllipticCurvePublicKey.from_encoded_point(_CURVE, epk_raw)
    except Exception as exc:  # invalid/off-curve point
        raise EChannelError("invalid ephemeral public key") from exc
    shared = key.exchange(ec.ECDH(), peer)
    k_c2s = HKDF(hashes.SHA256(), 32, _HKDF_SALT, _INFO_C2S + epk_raw).derive(shared)
    k_s2c = HKDF(hashes.SHA256(), 32, _HKDF_SALT, _INFO_S2C + epk_raw).derive(shared)
    return k_c2s, k_s2c


# ── AEAD helpers ──────────────────────────────────────────────────────────────


def _seal(key: bytes, plaintext: bytes) -> "dict[str, str]":
    iv = os.urandom(_IV_LEN)
    ct = AESGCM(key).encrypt(iv, plaintext, None)
    return {"iv": base64.b64encode(iv).decode("ascii"), "ct": base64.b64encode(ct).decode("ascii")}


def _open(key: bytes, iv_b64: str, ct_b64: str) -> bytes:
    try:
        iv = base64.b64decode(iv_b64)
        ct = base64.b64decode(ct_b64)
    except Exception as exc:
        raise EChannelError("malformed base64") from exc
    if len(iv) != _IV_LEN:
        raise EChannelError("bad IV length")
    try:
        return AESGCM(key).decrypt(iv, ct, None)
    except Exception as exc:  # auth tag mismatch / tampering
        raise EChannelError("decryption failed") from exc


# ── Public API used by the webapp routes ──────────────────────────────────────


class Session:
    """Per-request derived keys. Decrypt the inbound payload, then use the same
    instance to encrypt the response / stream frames back."""

    __slots__ = ("k_c2s", "k_s2c")

    def __init__(self, k_c2s: bytes, k_s2c: bytes) -> None:
        self.k_c2s = k_c2s
        self.k_s2c = k_s2c

    def encrypt(self, plaintext: "bytes | str") -> "dict[str, str]":
        if isinstance(plaintext, str):
            plaintext = plaintext.encode("utf-8")
        return _seal(self.k_s2c, plaintext)


def open_request(body: "dict[str, t.Any]") -> "tuple[bytes, Session]":
    """Validate + decrypt an inbound ``{epk, iv, ct}`` body.

    Returns the decrypted plaintext bytes and a :class:`Session` bound to the
    same shared secret for encrypting the reply. Raises :class:`EChannelError`
    on anything malformed (caller maps that to HTTP 400).
    """
    if not isinstance(body, dict):
        raise EChannelError("body must be a JSON object")
    epk_b64, iv_b64, ct_b64 = body.get("epk"), body.get("iv"), body.get("ct")
    if not (isinstance(epk_b64, str) and isinstance(iv_b64, str) and isinstance(ct_b64, str)):
        raise EChannelError("missing epk/iv/ct")
    try:
        epk_raw = base64.b64decode(epk_b64)
    except Exception as exc:
        raise EChannelError("malformed epk") from exc
    if len(epk_raw) != _RAW_PUBKEY_LEN or epk_raw[0] != 0x04:
        raise EChannelError("epk must be an uncompressed P-256 point")
    k_c2s, k_s2c = _derive_keys(epk_raw)
    plaintext = _open(k_c2s, iv_b64, ct_b64)
    return plaintext, Session(k_c2s, k_s2c)


def reset_cache_for_tests() -> None:
    """Test hook: force re-reading ``E2E_PRIVATE_KEY`` from the environment."""
    global _private_key, _loaded
    _private_key = None
    _loaded = False


# ── CLI: generate a key pair ──────────────────────────────────────────────────


def _generate_keypair() -> "tuple[str, str]":
    key = ec.generate_private_key(_CURVE)
    der = key.private_bytes(Encoding.DER, PrivateFormat.PKCS8, NoEncryption())
    priv_b64 = base64.b64encode(der).decode("ascii")
    raw_pub = key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    pub_b64 = base64.b64encode(raw_pub).decode("ascii")
    return priv_b64, pub_b64


if __name__ == "__main__":
    priv, pub = _generate_keypair()
    print("# uSearch encrypted-channel key pair (P-256)")
    print("# Set the private key as a Fly secret (never commit it):")
    print(f"#   fly secrets set E2E_PRIVATE_KEY={priv}")
    print("# The public key is published to clients automatically via settings.")
    print()
    print(f"E2E_PRIVATE_KEY={priv}")
    print(f"E2E_PUBLIC_KEY={pub}")
