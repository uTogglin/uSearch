# Encrypted query channel (edge-blind search)

Application-layer encryption so the Cloudflare edge — which terminates TLS and
runs the bouncer Worker — only ever sees **ciphertext** queries and results. The
browser and the Fly origin establish an ECIES-style channel *inside* the
CF-terminated TLS; only the Fly origin holds the private key.

## Threat model (read this)

This protects against Cloudflare **passively** seeing queries: edge logs, cache,
a breach, or the bouncer Worker. It does **not** stop an **active** Cloudflare
from rewriting the client JS it delivers (it could swap the published key). Hard
protection against an active edge would require shipping the client outside CF
(a browser extension). For "CF and its logs never see my queries," this delivers
exactly that.

## Crypto

- Key agreement: ECDH on NIST P-256 (Web Crypto on the client, `cryptography`
  on the server — both native/C, so the per-request cost is ~1–3 ms).
- Key schedule: HKDF-SHA256, fixed salt, direction- and epk-bound `info`.
- AEAD: AES-256-GCM, 12-byte IV, 16-byte tag appended.

The server holds one **static** P-256 key pair. Each request carries a fresh
**ephemeral** client public key, so the server is fully stateless (no session
store — important for the scale-to-zero box). Two keys are derived per request:
`k_c2s` (decrypts requests) and `k_s2c` (encrypts responses / stream frames).

Implementations, kept byte-for-byte in sync:
- Server: [`searx/echannel.py`](../searx/echannel.py)
- Client: [`client/simple/src/js/util/echannel.ts`](../client/simple/src/js/util/echannel.ts)
- AI-summary client (standalone, inlines its own copy):
  [`client/simple/static/js/ai_summary.js`](../client/simple/static/js/ai_summary.js)

Interop is covered by `tools/e2e_interop_test.mjs` (`node tools/e2e_interop_test.mjs`).

## Encrypted endpoints

All are `POST` with a JSON body `{epk, iv, ct}` (the decrypted plaintext shape
is noted). They `404` when `E2E_PRIVATE_KEY` is unset, and the client silently
falls back to the plaintext path — so an un-keyed deployment still works.

| Route                | Plaintext in                       | Reuses            |
|----------------------|------------------------------------|-------------------|
| `/esearch`           | urlencoded search form             | `search()`        |
| `/eautocompleter`    | `{"q": "..."}`                     | `_run_autocompleter()` |
| `/eai_summary`       | `{"q": "..."}` (SSE, encrypted frames) | `/ai_summary` logic |
| `/eai_summary_more`  | `{"q": "..."}` (SSE, encrypted frames) | `/ai_summary_more` logic |

The plaintext `/search`, `/autocompleter`, `/ai_summary*` routes are unchanged
and still work (gated by the bouncer); the client just stops using them when a
key is published. The query is kept only in the URL **fragment** (`#q=…`), which
is never sent upstream, so reload/back/bookmark work while the edge stays blind.

## Deployment

### 1. Generate the static key pair

Run the keygen (standalone, no full app import needed):

```sh
.venv/Scripts/python searx/echannel.py      # Windows
# or: python searx/echannel.py
```

It prints `E2E_PRIVATE_KEY=…` (base64 PKCS#8) and `E2E_PUBLIC_KEY=…` (reference
only — the public key is derived from the private key at runtime and published
to clients automatically via `get_client_settings`).

### 2. Set the Fly secret

```sh
fly secrets set E2E_PRIVATE_KEY=<the base64 value> -a usearch-degoog
```

That is the **only** secret required. With it set, `echannel.is_enabled()` is
true, `e2e_pubkey` appears in the client settings, and the client uses the
encrypted routes automatically. Rotating the key = set a new secret + redeploy;
no client rebuild (the public key ships via settings, not the bundle).

### 3. Bouncer Worker

The bouncer (`usearch-bouncer` repo, private) is a transparent reverse proxy: it
forwards **every** path to the origin, forwards POST bodies, streams SSE, and
signs a per-request HMAC over `v1:<ts>:<method>:<pathname>`. So the new encrypted
routes are proxied and signed with **no allowlist change**.

The one change required: the cost-split rate limiter classifies AI traffic by
pathname, and must match the encrypted variants too, or `/eai_summary*` would get
the generous limiter instead of the strict OpenRouter-spend one:

```js
const isAI = url.pathname.startsWith('/ai_summary') || url.pathname.startsWith('/eai_summary');
```

Deploy with `npx wrangler deploy` from `worker/` (secrets persist across deploys).
```
