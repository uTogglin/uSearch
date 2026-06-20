// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * uSearch application-layer encrypted channel (client half).
 *
 * Establishes an ECIES-style channel to the Fly origin so the Cloudflare edge
 * only ever sees ciphertext. Primitives are kept byte-for-byte in sync with the
 * server half, `searx/echannel.py`:
 *
 *   - key agreement : ECDH on NIST P-256 (secp256r1), all via native Web Crypto
 *   - key schedule  : HKDF-SHA256, fixed salt, direction-bound + epk-bound info
 *   - AEAD          : AES-256-GCM, 12-byte IV, 16-byte tag appended to ciphertext
 *
 * One ephemeral key pair is generated per page load; the derived keys are reused
 * for every request that page makes (search, autocomplete, AI summary), so there
 * is no per-request handshake. The server's static public key arrives via
 * `settings.e2e_pubkey` (base64 raw point) — rotating it is a redeploy, no
 * client rebuild.
 */

import { settings } from "../toolkit.ts";

// ── Protocol constants (keep in lockstep with echannel.py) ───────────────────
const SALT = utf8("usearch-e2e/v1/salt");
const INFO_C2S = utf8("usearch-e2e/v1/c2s");
const INFO_S2C = utf8("usearch-e2e/v1/s2c");
const IV_LEN = 12;

export type Envelope = { epk: string; iv: string; ct: string };
export type Frame = { iv: string; ct: string };

// All byte helpers return ArrayBuffer-backed arrays so they satisfy Web Crypto's
// BufferSource (TS 5.7+ distinguishes Uint8Array<ArrayBuffer> from ArrayBufferLike).
function utf8(s: string): Uint8Array<ArrayBuffer> {
  return new Uint8Array(new TextEncoder().encode(s));
}

function b64encode(bytes: Uint8Array): string {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s);
}

function b64decode(s: string): Uint8Array<ArrayBuffer> {
  const bin = atob(s);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function concat(a: Uint8Array, b: Uint8Array): Uint8Array<ArrayBuffer> {
  const out = new Uint8Array(a.length + b.length);
  out.set(a, 0);
  out.set(b, a.length);
  return out;
}

/** A live channel: an ephemeral key pair + the two derived AES-GCM keys. */
class Channel {
  private constructor(
    private readonly epkB64: string,
    private readonly kC2S: CryptoKey,
    private readonly kS2C: CryptoKey
  ) {}

  static async create(serverPubB64: string): Promise<Channel> {
    const subtle = crypto.subtle;
    const serverPub = await subtle.importKey(
      "raw",
      b64decode(serverPubB64),
      { name: "ECDH", namedCurve: "P-256" },
      false,
      []
    );
    const eph = await subtle.generateKey({ name: "ECDH", namedCurve: "P-256" }, false, ["deriveBits"]);
    const epkRaw = new Uint8Array(await subtle.exportKey("raw", eph.publicKey));

    // ECDH → 32-byte shared secret (the P-256 X coordinate), matching
    // `cryptography`'s `.exchange(ec.ECDH(), peer)`.
    const sharedBits = await subtle.deriveBits({ name: "ECDH", public: serverPub }, eph.privateKey, 256);
    const shared = await subtle.importKey("raw", sharedBits, "HKDF", false, ["deriveKey"]);

    const kC2S = await deriveAesKey(shared, concat(INFO_C2S, epkRaw));
    const kS2C = await deriveAesKey(shared, concat(INFO_S2C, epkRaw));

    return new Channel(b64encode(epkRaw), kC2S, kS2C);
  }

  /** Encrypt a request: produces an `{epk, iv, ct}` envelope (POST as JSON). */
  async encrypt(plaintext: string | Uint8Array): Promise<Envelope> {
    const data = typeof plaintext === "string" ? utf8(plaintext) : new Uint8Array(plaintext);
    const iv = crypto.getRandomValues(new Uint8Array(IV_LEN));
    const ct = new Uint8Array(await crypto.subtle.encrypt({ name: "AES-GCM", iv }, this.kC2S, data));
    return { epk: this.epkB64, iv: b64encode(iv), ct: b64encode(ct) };
  }

  /** Decrypt a server response/stream frame `{iv, ct}` back to bytes. */
  async decrypt(frame: Frame): Promise<Uint8Array> {
    const iv = b64decode(frame.iv);
    const ct = b64decode(frame.ct);
    const pt = await crypto.subtle.decrypt({ name: "AES-GCM", iv }, this.kS2C, ct);
    return new Uint8Array(pt);
  }

  async decryptText(frame: Frame): Promise<string> {
    return new TextDecoder().decode(await this.decrypt(frame));
  }
}

async function deriveAesKey(hkdfKey: CryptoKey, info: Uint8Array<ArrayBuffer>): Promise<CryptoKey> {
  return crypto.subtle.deriveKey(
    { name: "HKDF", hash: "SHA-256", salt: SALT, info },
    hkdfKey,
    { name: "AES-GCM", length: 256 },
    false,
    ["encrypt", "decrypt"]
  );
}

// ── Singleton session (one channel per page load) ────────────────────────────

let channelPromise: Promise<Channel> | null = null;

/** Whether the server published a key (i.e. the encrypted channel is usable). */
export function isAvailable(): boolean {
  return typeof settings.e2e_pubkey === "string" && settings.e2e_pubkey.length > 0 && !!crypto?.subtle;
}

/**
 * Get (or lazily create) the page's channel. Rejects if the channel isn't
 * available — callers should check {@link isAvailable} first and fall back to
 * the plaintext path.
 */
export function getChannel(): Promise<Channel> {
  if (!isAvailable()) return Promise.reject(new Error("encrypted channel unavailable"));
  if (!channelPromise) {
    channelPromise = Channel.create(settings.e2e_pubkey as string).catch((err) => {
      channelPromise = null; // allow retry on transient failure
      throw err;
    });
  }
  return channelPromise;
}

export type { Channel };
