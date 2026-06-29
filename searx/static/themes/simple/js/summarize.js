// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * uSearch Universal Summarizer — paste-a-URL page client.
 *
 * Reuses the tested E2E + SSE streaming pattern from ai_summary.js: encrypted
 * POST /eai_summarize when an e2e channel is available, else plaintext GET
 * /ai_summarize. The only protocol difference is that this client sends
 * {url} instead of {q}. The streamed summary renders with a typewriter cursor.
 */
(function () {
  "use strict";

  // ── End-to-end encryption helper (copied from ai_summary.js) ────────────────
  // P-256 ECDH + HKDF-SHA256 + AES-256-GCM. When enabled, request + response
  // frames travel as ciphertext so the TLS-terminating edge sees opaque blobs.
  const E2E = (() => {
    const enc = new TextEncoder(), dec = new TextDecoder();
    const utf8 = s => enc.encode(s);
    const SALT = utf8("usearch-e2e/v1/salt");
    const INFO_C2S = utf8("usearch-e2e/v1/c2s");
    const INFO_S2C = utf8("usearch-e2e/v1/s2c");
    const b64e = b => { let s = ""; for (const x of b) s += String.fromCharCode(x); return btoa(s); };
    const b64d = s => { const t = atob(s), o = new Uint8Array(t.length); for (let i = 0; i < t.length; i++) o[i] = t.charCodeAt(i); return o; };
    const cat = (a, b) => { const o = new Uint8Array(a.length + b.length); o.set(a, 0); o.set(b, a.length); return o; };
    async function create(pubB64) {
      const sub = crypto.subtle;
      const pub = await sub.importKey("raw", b64d(pubB64), { name: "ECDH", namedCurve: "P-256" }, false, []);
      const eph = await sub.generateKey({ name: "ECDH", namedCurve: "P-256" }, false, ["deriveBits"]);
      const epk = new Uint8Array(await sub.exportKey("raw", eph.publicKey));
      const bits = await sub.deriveBits({ name: "ECDH", public: pub }, eph.privateKey, 256);
      const hk = await sub.importKey("raw", bits, "HKDF", false, ["deriveKey"]);
      const dk = info => sub.deriveKey({ name: "HKDF", hash: "SHA-256", salt: SALT, info }, hk, { name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]);
      const kc = await dk(cat(INFO_C2S, epk)), ks = await dk(cat(INFO_S2C, epk));
      const epkB64 = b64e(epk);
      return {
        async encrypt(t) { const iv = crypto.getRandomValues(new Uint8Array(12)); const ct = new Uint8Array(await sub.encrypt({ name: "AES-GCM", iv }, kc, utf8(t))); return { epk: epkB64, iv: b64e(iv), ct: b64e(ct) }; },
        async decrypt(f) { const pt = await sub.decrypt({ name: "AES-GCM", iv: b64d(f.iv) }, ks, b64d(f.ct)); return dec.decode(pt); }
      };
    }
    return { create };
  })();

  function getPubKey() {
    try {
      const el = document.querySelector("script[client_settings]");
      if (!el) return null;
      const settings = JSON.parse(atob(el.getAttribute("client_settings")));
      return settings && settings.e2e_pubkey ? settings.e2e_pubkey : null;
    } catch (_) {
      return null;
    }
  }

  let _channelPromise = null;
  function getChannel() {
    if (_channelPromise) return _channelPromise;
    const pubkey = getPubKey();
    if (!pubkey || !(window.crypto && crypto.subtle)) return null;
    _channelPromise = E2E.create(pubkey);
    return _channelPromise;
  }

  // ── SSE payload handling (copied from ai_summary.js) ────────────────────────
  function handlePayload(raw, onChunk, onDone) {
    if (!raw || raw === "[DONE]") {
      if (raw === "[DONE]") { onDone(); return true; }
      return false;
    }
    try {
      const txt = JSON.parse(raw);
      if (typeof txt === "string" && txt) onChunk(txt);
    } catch (_) {
      if (raw && raw !== "[DONE]") onChunk(raw);
    }
    return false;
  }

  async function readStream(url, query, onChunk, onDone, onError) {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 60000);
    try {
      const params = new URLSearchParams({ url: query || "" });
      const resp = await fetch(url + "?" + params.toString(), {
        method: "GET",
        headers: { "Accept": "text/event-stream" },
        signal: controller.signal,
      });
      if (!resp.ok) throw new Error("HTTP " + resp.status);

      const reader = resp.body.getReader();
      const dec = new TextDecoder("utf-8");
      let buf = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const lines = buf.split("\n");
        buf = lines.pop() || "";
        for (const line of lines) {
          const t = line.replace(/\r/g, "").trim();
          if (!t.startsWith("data:")) continue;
          if (handlePayload(t.slice(5).trim(), onChunk, onDone)) return;
        }
      }
      onDone();
    } catch (err) {
      onError(err);
    } finally {
      clearTimeout(timeoutId);
    }
  }

  async function readStreamEncrypted(url, channel, query, onChunk, onDone, onError) {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 60000);
    try {
      const envelope = await channel.encrypt(JSON.stringify({ url: query || "" }));
      const resp = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
        body: JSON.stringify(envelope),
        signal: controller.signal,
      });
      if (!resp.ok) throw new Error("HTTP " + resp.status);

      const reader = resp.body.getReader();
      const dec = new TextDecoder("utf-8");
      let buf = "";

      async function processEvent(event) {
        for (const line of event.split("\n")) {
          const t = line.replace(/\r/g, "").trim();
          if (!t.startsWith("data:")) continue;
          const raw = t.slice(5).trim();
          if (!raw) continue;
          let payload;
          try {
            payload = await channel.decrypt(JSON.parse(raw));
          } catch (_) {
            continue;
          }
          if (handlePayload(payload, onChunk, onDone)) return true;
        }
        return false;
      }

      async function flush(force) {
        while (true) {
          const idx = buf.indexOf("\n\n");
          if (idx === -1) {
            if (!force) break;
            const tail = buf; buf = "";
            if (!tail) break;
            if (await processEvent(tail)) return true;
            break;
          }
          const event = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          if (await processEvent(event)) return true;
        }
        return false;
      }

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        if (await flush(false)) return;
      }
      if (await flush(true)) return;
      onDone();
    } catch (err) {
      onError(err);
    } finally {
      clearTimeout(timeoutId);
    }
  }

  // Dispatcher: encrypted POST when a channel is available, else plaintext GET.
  async function requestSummary(url, eUrl, query, onChunk, onDone, onError) {
    let channel = null;
    try {
      const p = getChannel();
      if (p) channel = await p;
    } catch (_) {
      channel = null;
    }
    if (channel) {
      return readStreamEncrypted(eUrl, channel, query, onChunk, onDone, onError);
    }
    return readStream(url, query, onChunk, onDone, onError);
  }

  // ── Page wiring ─────────────────────────────────────────────────────────────

  let busy = false;

  function startSummarize(targetUrl) {
    if (busy) return;
    const u = (targetUrl || "").trim();
    if (!u) return;
    busy = true;

    const submit = document.getElementById("sz-submit");
    const result = document.getElementById("sz-result");
    if (submit) submit.setAttribute("disabled", "disabled");
    if (!result) { busy = false; return; }

    result.classList.add("visible");
    result.textContent = "";
    const loading = document.createElement("span");
    loading.className = "sz-loading";
    loading.innerHTML = '<span class="sz-spinner"></span> Reading and summarizing…';
    result.appendChild(loading);

    // Typewriter state.
    let queue = [], displayed = "", streamDone = false, timerID = null;
    let fromCache = false, started = false;

    function ensureContentEl() {
      if (started) return;
      started = true;
      result.textContent = "";
    }

    function render() {
      result.textContent = displayed;
      const cursor = document.createElement("span");
      cursor.className = "sz-cursor";
      result.appendChild(cursor);
    }

    function tick() {
      if (!queue.length) {
        if (streamDone) {
          result.textContent = displayed;
          finish();
          return;
        }
        timerID = setTimeout(tick, 16);
        return;
      }
      const cpt = queue.length > 60 ? 8 : queue.length > 30 ? 4 : queue.length > 10 ? 2 : 1;
      for (let i = 0; i < cpt && queue.length; i++) displayed += queue.shift();
      render();
      timerID = setTimeout(tick, 16);
    }

    function finish() {
      busy = false;
      if (submit) submit.removeAttribute("disabled");
    }

    function showError(msg) {
      if (timerID) { clearTimeout(timerID); timerID = null; }
      result.textContent = "";
      const e = document.createElement("span");
      e.className = "sz-error";
      e.textContent = msg || "Could not summarize this URL.";
      result.appendChild(e);
      finish();
    }

    requestSummary(
      "/ai_summarize",
      "/eai_summarize",
      u,
      (chunk) => {
        ensureContentEl();
        // Error frames arrive as a normal string chunk prefixed "[ERROR]".
        if (typeof chunk === "string" && chunk.indexOf("[ERROR]") === 0) {
          showError(chunk.replace("[ERROR]", "").trim() || "Could not summarize this URL.");
          // Swallow the trailing [DONE].
          streamDone = true;
          return;
        }
        // Cache hit: sentinel then full text in one frame.
        if (chunk === "[CACHED]") { fromCache = true; return; }
        if (fromCache) {
          displayed = chunk;
          result.textContent = displayed;
          return;
        }
        for (const ch of chunk) queue.push(ch);
        if (!timerID) timerID = setTimeout(tick, 16);
      },
      () => {
        if (busy === false) return; // already finished via error path
        if (fromCache) { result.textContent = displayed; finish(); return; }
        streamDone = true;
        if (!timerID) timerID = setTimeout(tick, 16);
      },
      (err) => {
        console.warn("summarize error:", err);
        showError("Could not load summary. Please try again.");
      }
    );
  }

  function run() {
    const form = document.getElementById("sz-form");
    const input = document.getElementById("sz-url");
    if (!form || !input) return;

    form.addEventListener("submit", (e) => {
      e.preventDefault();
      startSummarize(input.value);
    });

    // Deep-link autostart: ?url= present (server prefilled the input).
    const params = new URLSearchParams(window.location.search);
    const deep = params.get("url");
    if (deep && deep.trim()) {
      startSummarize(deep.trim());
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", run);
  } else {
    run();
  }
})();
