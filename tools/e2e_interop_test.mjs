// SPDX-License-Identifier: AGPL-3.0-or-later
// Interop test for the uSearch encrypted channel.
//
// The client steps below mirror client/simple/src/js/util/echannel.ts exactly
// (same Web Crypto calls + same protocol constants). It round-trips against the
// real server module via tools/e2e_interop_server.py:
//
//   1. server (python) generates a P-256 key pair
//   2. client (this file) derives keys, encrypts a request
//   3. server decrypts it, verifies the plaintext, encrypts a response
//   4. client decrypts the response and verifies it
//
// Run: node tools/e2e_interop_test.mjs

import { execFileSync } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { webcrypto as crypto } from "node:crypto";
import { tmpdir } from "node:os";
import { join } from "node:path";

const PY = process.platform === "win32" ? ".venv/Scripts/python" : ".venv/bin/python";
const SERVER = "tools/e2e_interop_server.py";

// ── constants mirrored from echannel.ts / echannel.py ────────────────────────
const utf8 = (s) => new TextEncoder().encode(s);
const SALT = utf8("usearch-e2e/v1/salt");
const INFO_C2S = utf8("usearch-e2e/v1/c2s");
const INFO_S2C = utf8("usearch-e2e/v1/s2c");
const IV_LEN = 12;

const b64encode = (bytes) => Buffer.from(bytes).toString("base64");
const b64decode = (s) => new Uint8Array(Buffer.from(s, "base64"));
const concat = (a, b) => {
  const out = new Uint8Array(a.length + b.length);
  out.set(a, 0);
  out.set(b, a.length);
  return out;
};

async function createChannel(serverPubB64) {
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
  const sharedBits = await subtle.deriveBits({ name: "ECDH", public: serverPub }, eph.privateKey, 256);
  const shared = await subtle.importKey("raw", sharedBits, "HKDF", false, ["deriveKey"]);
  const deriveAes = (info) =>
    subtle.deriveKey(
      { name: "HKDF", hash: "SHA-256", salt: SALT, info },
      shared,
      { name: "AES-GCM", length: 256 },
      false,
      ["encrypt", "decrypt"]
    );
  const kC2S = await deriveAes(concat(INFO_C2S, epkRaw));
  const kS2C = await deriveAes(concat(INFO_S2C, epkRaw));
  const epkB64 = b64encode(epkRaw);
  return {
    async encrypt(text) {
      const iv = crypto.getRandomValues(new Uint8Array(IV_LEN));
      const ct = new Uint8Array(await subtle.encrypt({ name: "AES-GCM", iv }, kC2S, utf8(text)));
      return { epk: epkB64, iv: b64encode(iv), ct: b64encode(ct) };
    },
    async decrypt(frame) {
      const pt = await subtle.decrypt({ name: "AES-GCM", iv: b64decode(frame.iv) }, kS2C, b64decode(frame.ct));
      return new TextDecoder().decode(pt);
    }
  };
}

async function main() {
  const dir = mkdtempSync(join(tmpdir(), "echannel-"));
  const reqPath = join(dir, "req.json");

  // 1. server generates keys
  const { priv, pub } = JSON.parse(execFileSync(PY, [SERVER, "gen"], { encoding: "utf-8" }));

  // 2. client encrypts a request (unicode + emoji to catch utf-8 issues)
  const message = "secret query: café ☕ 日本語 🔐";
  const channel = await createChannel(pub);
  const envelope = await channel.encrypt(message);
  writeFileSync(reqPath, JSON.stringify(envelope));

  // 3. server decrypts, verifies, encrypts response
  const respRaw = execFileSync(PY, [SERVER, "process", reqPath, message], {
    encoding: "utf-8",
    env: { ...process.env, E2E_PRIVATE_KEY: priv }
  });
  const response = JSON.parse(respRaw);

  // 4. client decrypts response
  const decrypted = await channel.decrypt(response);
  const expected = `pong: ${message}`;
  if (decrypted !== expected) {
    throw new Error(`CLIENT MISMATCH: got ${JSON.stringify(decrypted)}, expected ${JSON.stringify(expected)}`);
  }

  console.log("PASS: client encrypt -> server decrypt -> server encrypt -> client decrypt");
  console.log(`  request plaintext : ${message}`);
  console.log(`  response plaintext: ${decrypted}`);
}

main().catch((err) => {
  console.error("FAIL:", err.message);
  process.exit(1);
});
