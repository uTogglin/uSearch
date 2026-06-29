// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * uSearch Assistant — multi-turn, web-aware AI chat.
 *
 * Reuses the tested ai_summary.js E2E + SSE machinery (P-256 ECDH + HKDF-SHA256
 * + AES-256-GCM), generalized so the encrypted reader POSTs an arbitrary payload
 * object ({messages, web}) instead of just {q}. Maintains the conversation in
 * memory (optionally persisted to localStorage) and streams each reply into the
 * latest assistant bubble. Handles the "[SOURCES]" two-frame citation protocol.
 */
(function () {
  "use strict";

  // ── End-to-end encryption helper (mirrors ai_summary.js exactly) ────────────
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

  // ── SSE payload handling (mirrors ai_summary.js handlePayload) ──────────────
  function handlePayload(raw, onChunk, onDone) {
    if (!raw || raw === "[DONE]") {
      if (raw === "[DONE]") { onDone(); return true; }
      return false;
    }
    try {
      const txt = JSON.parse(raw);
      if (typeof txt === "string" && txt) onChunk(txt);
      else if (Array.isArray(txt)) onChunk(txt); // sources array frame
    } catch (_) {
      if (raw && raw !== "[DONE]") onChunk(raw);
    }
    return false;
  }

  // Plaintext POST reader (fallback when no E2E channel).
  async function readStreamPlain(url, payloadObj, onChunk, onDone, onError) {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 90000);
    try {
      const resp = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
        body: JSON.stringify(payloadObj),
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

  // Encrypted POST reader — generalized to accept an arbitrary payload object.
  async function readStreamEncrypted(url, channel, payloadObj, onChunk, onDone, onError) {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 90000);
    try {
      const envelope = await channel.encrypt(JSON.stringify(payloadObj));
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

  // Dispatcher: encrypted POST when a channel exists, else plaintext POST.
  async function requestChat(payloadObj, onChunk, onDone, onError) {
    let channel = null;
    try {
      const p = getChannel();
      if (p) channel = await p;
    } catch (_) {
      channel = null;
    }
    if (channel) {
      return readStreamEncrypted("/eassistant", channel, payloadObj, onChunk, onDone, onError);
    }
    return readStreamPlain("/assistant_chat", payloadObj, onChunk, onDone, onError);
  }

  // ── State ───────────────────────────────────────────────────────────────────
  const STORAGE_KEY = "usearch.assistant";
  const MAX_TURNS = 12;
  let conversation = [];

  function loadConversation() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return [];
      const arr = JSON.parse(raw);
      if (!Array.isArray(arr)) return [];
      return arr.filter(m => m && (m.role === "user" || m.role === "assistant") && typeof m.content === "string");
    } catch (_) {
      return [];
    }
  }

  function saveConversation() {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(conversation.slice(-MAX_TURNS))); } catch (_) {}
  }

  // ── DOM helpers ─────────────────────────────────────────────────────────────
  const messagesEl = () => document.getElementById("assistant-messages");

  function esc(s) {
    return String(s || "")
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function hostnameOf(url) {
    try { return new URL(url).hostname.replace(/^www\./, ""); }
    catch { return ""; }
  }

  function safeHttpUrl(u) {
    try {
      const p = new URL(u);
      if (p.protocol === "http:" || p.protocol === "https:") return p.href;
    } catch (_) {}
    return null;
  }

  function clearEmpty() {
    const empty = messagesEl().querySelector(".assistant-empty");
    if (empty) empty.remove();
  }

  function scrollToBottom() {
    const el = messagesEl();
    el.scrollTop = el.scrollHeight;
  }

  function addBubble(role, text) {
    clearEmpty();
    const wrap = document.createElement("div");
    wrap.className = "assistant-msg assistant-" + role;
    const body = document.createElement("div");
    body.className = "assistant-bubble";
    body.textContent = text || "";
    wrap.appendChild(body);
    messagesEl().appendChild(wrap);
    scrollToBottom();
    return wrap;
  }

  // Render assistant text with inline [n] citations becoming superscript links.
  function renderAssistantText(bodyEl, text, sources) {
    bodyEl.textContent = "";
    const re = /\[(\d{1,2})\]/g;
    let last = 0, m;
    while ((m = re.exec(text)) !== null) {
      if (m.index > last) bodyEl.appendChild(document.createTextNode(text.slice(last, m.index)));
      const n = parseInt(m[1], 10);
      const src = sources && sources[n - 1];
      const url = src ? safeHttpUrl(src.url) : null;
      if (url) {
        const a = document.createElement("a");
        a.className = "assistant-cite";
        a.href = url;
        a.target = "_blank";
        a.rel = "noopener";
        a.title = (src && src.title) || hostnameOf(url);
        a.textContent = String(n);
        bodyEl.appendChild(a);
      } else {
        bodyEl.appendChild(document.createTextNode(m[0]));
      }
      last = m.index + m[0].length;
    }
    if (last < text.length) bodyEl.appendChild(document.createTextNode(text.slice(last)));
  }

  function buildSourcesBlock(sources) {
    // Preserve the ORIGINAL 1-based index so the footer numbering stays aligned
    // with the inline [n] citations (which index into the full sources array).
    const items = (sources || [])
      .map((s, idx) => ({ url: safeHttpUrl(s && s.url), title: (s && s.title) || "", n: idx + 1 }))
      .filter(s => s.url);
    if (!items.length) return null;
    const block = document.createElement("div");
    block.className = "assistant-sources";
    const label = document.createElement("div");
    label.className = "assistant-sources-label";
    label.textContent = "Sources";
    block.appendChild(label);
    const row = document.createElement("div");
    row.className = "assistant-sources-row";
    items.forEach((s) => {
      const host = hostnameOf(s.url) || s.url;
      const a = document.createElement("a");
      a.className = "assistant-source-tag";
      a.href = s.url;
      a.target = "_blank";
      a.rel = "noopener";
      a.title = s.title || host;
      const num = document.createElement("span");
      num.className = "assistant-source-num";
      num.textContent = String(s.n);
      a.appendChild(num);
      a.appendChild(document.createTextNode(" " + host));
      row.appendChild(a);
    });
    block.appendChild(row);
    return block;
  }

  // ── Send flow ───────────────────────────────────────────────────────────────
  let inFlight = false;

  function send() {
    if (inFlight) return;
    const input = document.getElementById("assistant-input");
    const text = (input.value || "").trim();
    if (!text) return;
    const web = !!document.getElementById("assistant-web").checked;

    input.value = "";
    autosize(input);

    conversation.push({ role: "user", content: text });
    saveConversation();
    addBubble("user", text);

    // Assistant bubble with a thinking indicator.
    const wrap = addBubble("assistant", "");
    const bodyEl = wrap.querySelector(".assistant-bubble");
    bodyEl.classList.add("assistant-thinking");
    bodyEl.innerHTML = '<span class="assistant-dot"></span><span class="assistant-dot"></span><span class="assistant-dot"></span>';

    let answer = "";
    let sources = [];
    let expectSources = false;
    let started = false;
    inFlight = true;
    setSending(true);

    function ensureStarted() {
      if (started) return;
      started = true;
      bodyEl.classList.remove("assistant-thinking");
      bodyEl.textContent = "";
    }

    const payload = { messages: conversation.slice(-MAX_TURNS), web: web };

    requestChat(
      payload,
      (chunk) => {
        // Sources protocol: "[SOURCES]" sentinel, then an array frame.
        if (chunk === "[SOURCES]") { expectSources = true; return; }
        if (expectSources && Array.isArray(chunk)) { sources = chunk; expectSources = false; return; }
        if (Array.isArray(chunk)) return;
        if (typeof chunk !== "string") return;
        if (chunk.indexOf("[ERROR]") === 0) {
          ensureStarted();
          bodyEl.classList.add("assistant-error");
          bodyEl.textContent = chunk.replace(/^\[ERROR\]\s*/, "") || "Something went wrong.";
          return;
        }
        ensureStarted();
        answer += chunk;
        renderAssistantText(bodyEl, answer, sources);
        scrollToBottom();
      },
      () => {
        finishTurn(wrap, bodyEl, answer, sources);
      },
      (err) => {
        ensureStarted();
        bodyEl.classList.add("assistant-error");
        bodyEl.textContent = "Could not reach the assistant. Please try again.";
        // eslint-disable-next-line no-console
        console.warn("assistant error:", err);
        inFlight = false;
        setSending(false);
      }
    );
  }

  function finishTurn(wrap, bodyEl, answer, sources) {
    inFlight = false;
    setSending(false);
    if (answer.trim()) {
      conversation.push({ role: "assistant", content: answer });
      saveConversation();
      renderAssistantText(bodyEl, answer, sources);
    } else if (!bodyEl.classList.contains("assistant-error")) {
      bodyEl.textContent = "No response.";
    }
    const block = buildSourcesBlock(sources);
    if (block) wrap.appendChild(block);
    scrollToBottom();
  }

  function setSending(busy) {
    const btn = document.getElementById("assistant-send");
    if (btn) { btn.disabled = busy; btn.textContent = busy ? "…" : "Send"; }
  }

  function autosize(el) {
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 200) + "px";
  }

  // ── Init / hydrate ──────────────────────────────────────────────────────────
  function hydrate() {
    conversation = loadConversation();
    if (!conversation.length) return;
    clearEmpty();
    conversation.forEach(m => {
      if (m.role === "user") {
        addBubble("user", m.content);
      } else {
        const wrap = addBubble("assistant", "");
        renderAssistantText(wrap.querySelector(".assistant-bubble"), m.content, []);
      }
    });
    scrollToBottom();
  }

  function run() {
    const form = document.getElementById("assistant-form");
    const input = document.getElementById("assistant-input");
    if (!form || !input) return;

    hydrate();

    form.addEventListener("submit", (e) => { e.preventDefault(); send(); });
    input.addEventListener("input", () => autosize(input));
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
    });

    const clearBtn = document.getElementById("assistant-clear");
    if (clearBtn) {
      clearBtn.addEventListener("click", () => {
        conversation = [];
        saveConversation();
        const el = messagesEl();
        el.innerHTML = '<div class="assistant-empty">Ask anything. With web access on, each reply is grounded in fresh search results with citations.</div>';
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", run);
  } else {
    run();
  }
})();
