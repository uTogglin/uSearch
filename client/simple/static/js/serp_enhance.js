// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * uSearch SERP Enhancer
 * =====================
 * Two privacy-respecting, dependency-free enhancements injected on the results
 * page (mirrors ai_summary.js's injection):
 *
 *   1. GitHub cards — turns matching results into expandable cards. Stats
 *      (stars/forks/last push) are fetched LAZILY, only when the user expands a
 *      card, and only through this origin's /card_meta proxy — the browser never
 *      talks to GitHub directly.
 *
 *   2. Quick-copy buttons — "Copy link" on every result and "Copy summary" on
 *      the AI summary block. Pure client-side, no network.
 *
 * Everything is idempotent and re-applies to results added by infinite scroll.
 */
(function () {
  "use strict";

  // ── Styles ─────────────────────────────────────────────────────────────────
  const CSS = `
    .serp-copy-btn {
      display:inline-flex; align-items:center; gap:5px;
      background:transparent; border:1px solid rgba(127,127,127,0.28);
      border-radius:6px; color:var(--color-base-font,#666);
      font:inherit; font-size:0.72rem; line-height:1; padding:3px 8px;
      cursor:pointer; opacity:0.55; transition:opacity .15s,background .15s,border-color .15s,color .15s;
      vertical-align:middle;
    }
    .result:hover .serp-copy-btn { opacity:0.9; }
    .serp-copy-btn:hover { opacity:1; background:rgba(66,133,244,0.10); border-color:rgba(66,133,244,0.45); color:#4285f4; }
    .serp-copy-btn.copied { color:#2e9e57; border-color:rgba(46,158,87,0.5); background:rgba(46,158,87,0.10); opacity:1; }
    .serp-copy-btn svg { width:12px; height:12px; flex-shrink:0; }
    .engines .serp-copy-btn { margin-left:6px; }

    #ai-summary-box .ai-header .serp-copy-btn { margin-left:auto; }

    .serp-card {
      margin:8px 0 2px 0; border:1px solid rgba(127,127,127,0.22);
      border-radius:10px; overflow:hidden; font-size:0.82rem;
      background:rgba(127,127,127,0.04);
    }
    .serp-card-toggle {
      display:flex; align-items:center; gap:8px; width:100%;
      background:transparent; border:none; cursor:pointer; text-align:left;
      padding:7px 11px; color:var(--color-base-font,#555); font:inherit; font-size:0.78rem;
    }
    .serp-card-toggle:hover { background:rgba(127,127,127,0.07); }
    .serp-card-badge {
      display:inline-flex; align-items:center; gap:5px; font-weight:600;
      font-size:0.72rem; letter-spacing:.01em;
    }
    .serp-card-badge svg { width:14px; height:14px; }
    .serp-card-github .serp-card-badge { color:#6e5494; }
    .serp-card-hint { color:#8a8a8a; font-size:0.72rem; }
    .serp-card-chevron { margin-left:auto; font-size:0.7rem; opacity:.55; transition:transform .2s; }
    .serp-card.open .serp-card-chevron { transform:rotate(180deg); }
    .serp-card-body { display:none; padding:0 11px 10px; }
    .serp-card.open .serp-card-body { display:block; }
    .serp-stats { display:flex; flex-wrap:wrap; gap:6px; align-items:center; }
    .serp-stat {
      display:inline-flex; align-items:center; gap:4px;
      background:rgba(127,127,127,0.10); border-radius:6px; padding:3px 8px;
      font-size:0.74rem; font-weight:600; color:var(--color-base-font,#444); white-space:nowrap;
    }
    .serp-stat svg { width:12px; height:12px; opacity:.8; }
    .serp-stat .serp-stat-sub { font-weight:400; opacity:.7; }
    .serp-card-desc { margin:8px 0 0; color:#8a8a8a; font-size:0.76rem; line-height:1.45; }
    .serp-card-flag { color:#c0392b; font-weight:600; }
    .serp-card-loading { display:flex; align-items:center; gap:8px; color:#8a8a8a; font-size:0.76rem; padding:2px 0; }
    .serp-card-spin {
      width:12px; height:12px; border:2px solid rgba(127,127,127,0.25);
      border-top-color:#4285f4; border-radius:50%; animation:serp-spin .7s linear infinite; flex-shrink:0;
    }
    @keyframes serp-spin { to { transform:rotate(360deg); } }
    @media (prefers-color-scheme: dark) {
      .serp-stat { color:#cfcfcf; }
      .serp-card-toggle { color:#bdbdbd; }
    }
    .theme-dark .serp-stat { color:#cfcfcf; }
    .theme-dark .serp-card-toggle { color:#bdbdbd; }

    /* Top "sources" section — Reddit threads + GitHub repos */
    #serp-sources { display:grid; grid-template-columns:1fr; gap:12px; margin:0 0 16px 0; }
    .serp-src-group {
      border:1px solid rgba(127,127,127,0.22); border-radius:12px;
      background:rgba(127,127,127,0.04); padding:10px 13px 7px; min-width:0;
    }
    .serp-src-head {
      display:flex; align-items:center; gap:7px; font-weight:700;
      font-size:0.82rem; margin-bottom:5px;
    }
    .serp-src-head svg { width:15px; height:15px; }
    .serp-src-github .serp-src-head { color:#6e5494; }
    .serp-src-reddit .serp-src-head { color:#ff4500; }
    .serp-src-item {
      display:block; padding:7px 6px; border-radius:8px; text-decoration:none;
      color:inherit; border-top:1px solid rgba(127,127,127,0.12);
    }
    .serp-src-group .serp-src-item:first-of-type { border-top:none; }
    .serp-src-item:hover { background:rgba(66,133,244,0.08); }
    .serp-src-title {
      display:block; font-size:0.86rem; line-height:1.3;
      color:var(--color-result-link-font,#1a0dab);
      overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
    }
    .serp-src-meta {
      display:flex; align-items:center; gap:10px; margin-top:2px;
      font-size:0.73rem; color:#8a8a8a;
    }
    .serp-src-host { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; flex:1 1 auto; min-width:0; }
    .serp-src-statline { display:inline-flex; gap:9px; align-items:center; flex-shrink:0; }
    .serp-src-stat { display:inline-flex; align-items:center; gap:3px; font-weight:600; color:var(--color-base-font,#555); }
    .serp-src-stat svg { width:11px; height:11px; opacity:.8; }
  `;

  function injectStyles() {
    if (document.getElementById("serp-enhance-styles")) return;
    const s = document.createElement("style");
    s.id = "serp-enhance-styles";
    s.textContent = CSS;
    document.head.appendChild(s);
  }

  // ── Small DOM/format helpers ───────────────────────────────────────────────

  const ICON = {
    copy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
    check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>',
    github: '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8z"/></svg>',
    star: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 .59l3.09 6.26 6.91 1-5 4.87 1.18 6.88L12 16.5l-6.18 3.25L7 12.72l-5-4.87 6.91-1z"/></svg>',
    fork: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="3" r="2"/><circle cx="6" cy="21" r="2"/><circle cx="18" cy="6" r="2"/><path d="M18 8v1a3 3 0 0 1-3 3H9a3 3 0 0 0-3 3v2M6 5v10"/></svg>',
    issue: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>',
    clock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15 14"/></svg>',
    reddit: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 0C5.37 0 0 5.37 0 12s5.37 12 12 12 12-5.37 12-12S18.63 0 12 0zm5.93 12.6c.02.16.03.33.03.5 0 2.55-2.97 4.62-6.63 4.62s-6.63-2.07-6.63-4.62c0-.17.01-.34.03-.5a1.49 1.49 0 0 1-.5-2.66 1.49 1.49 0 0 1 1.84.18c.9-.62 2.12-1.02 3.47-1.07l.66-3.1a.32.32 0 0 1 .38-.25l2.2.47a1.04 1.04 0 1 1-.14.64l-1.97-.42-.59 2.78c1.33.06 2.53.46 3.42 1.07a1.49 1.49 0 0 1 2.06.12 1.49 1.49 0 0 1-.13 2.04 1.5 1.5 0 0 1-.7.4zM9.07 11.6a1.04 1.04 0 1 0 2.08 0 1.04 1.04 0 0 0-2.08 0zm5.86 2.95a.34.34 0 0 0-.48 0c-.43.43-1.32.58-2.04.58-.72 0-1.61-.15-2.04-.58a.34.34 0 1 0-.48.48c.68.68 1.98.73 2.52.73.54 0 1.84-.05 2.52-.73a.34.34 0 0 0-.01-.48zm-.99-1.9a1.04 1.04 0 1 0 0-2.08 1.04 1.04 0 0 0 0 2.08z"/></svg>',
    upvote: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 3l8 9h-5v9H9v-9H4z"/></svg>',
    comment: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>',
  };

  function fmtNum(n) {
    n = Number(n) || 0;
    if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1).replace(/\.0$/, "") + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e4 ? 0 : 1).replace(/\.0$/, "") + "k";
    return String(n);
  }

  function timeAgo(ms) {
    const s = Math.max(0, (Date.now() - ms) / 1000);
    const units = [["year", 31536000], ["month", 2592000], ["week", 604800],
                   ["day", 86400], ["hour", 3600], ["minute", 60]];
    for (const [name, secs] of units) {
      const v = Math.floor(s / secs);
      if (v >= 1) return v + " " + name + (v > 1 ? "s" : "") + " ago";
    }
    return "just now";
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function copyToClipboard(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    // Legacy fallback for non-secure contexts.
    return new Promise((resolve, reject) => {
      try {
        const ta = document.createElement("textarea");
        ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
        document.body.appendChild(ta); ta.select();
        document.execCommand("copy"); ta.remove(); resolve();
      } catch (e) { reject(e); }
    });
  }

  function flashCopied(btn, label) {
    const original = btn.innerHTML;
    btn.classList.add("copied");
    btn.innerHTML = ICON.check + "<span>Copied</span>";
    setTimeout(() => { btn.classList.remove("copied"); btn.innerHTML = original; }, 1600);
    void label;
  }

  function makeCopyBtn(label, getText) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "serp-copy-btn";
    btn.innerHTML = ICON.copy + "<span>" + esc(label) + "</span>";
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      const text = getText();
      if (!text) return;
      copyToClipboard(text).then(() => flashCopied(btn, label)).catch(() => {});
    });
    return btn;
  }

  // ── 1. Copy-link button on each result ─────────────────────────────────────

  function resultUrl(result) {
    const a = result.querySelector("h3 a") || result.querySelector("a.url_header") ||
              result.querySelector("a[href]");
    return a ? (a.href || a.getAttribute("href") || "") : "";
  }

  function enhanceCopyLinks(root) {
    (root || document).querySelectorAll(".result").forEach((result) => {
      if (result.dataset.serpCopy) return;
      const url = resultUrl(result);
      if (!url) return;
      result.dataset.serpCopy = "1";
      const btn = makeCopyBtn("Copy link", () => resultUrl(result));
      const engines = result.querySelector(".engines");
      if (engines) engines.appendChild(btn);
      else result.appendChild(btn);
    });
  }

  // ── 2. GitHub cards ────────────────────────────────────────────────────────

  function classify(url) {
    let u;
    try { u = new URL(url); } catch (_) { return null; }
    if (u.protocol !== "http:" && u.protocol !== "https:") return null;
    let host = u.hostname.toLowerCase();
    if (host.startsWith("www.")) host = host.slice(4);
    const segs = u.pathname.split("/").filter(Boolean);

    const GH_RESERVED = new Set(["orgs", "sponsors", "features", "about", "pricing",
      "marketplace", "topics", "collections", "trending", "settings", "notifications",
      "explore", "login", "join", "new", "search", "apps", "users", "organizations",
      "site", "contact", "readme", "watching", "stars", "dashboard", "account",
      "codespaces", "issues", "pulls"]);
    const NAME = /^[A-Za-z0-9_.\-]{1,100}$/;

    if (host === "github.com" && segs.length >= 2) {
      let repo = segs[1].endsWith(".git") ? segs[1].slice(0, -4) : segs[1];
      if (!GH_RESERVED.has(segs[0].toLowerCase()) && NAME.test(segs[0]) && NAME.test(repo)) {
        return "github";
      }
    }

    const REDDIT_ID = /^[A-Za-z0-9]{4,12}$/;
    if (host === "reddit.com" || host.endsWith(".reddit.com")) {
      const ci = segs.indexOf("comments");
      if (ci >= 0 && ci + 1 < segs.length && REDDIT_ID.test(segs[ci + 1])) {
        return "reddit";
      }
    }
    return null;
  }

  function statChip(icon, value, sub) {
    return '<span class="serp-stat">' + icon + "<span>" + esc(value) + "</span>" +
      (sub ? '<span class="serp-stat-sub">' + esc(sub) + "</span>" : "") + "</span>";
  }

  function renderGithub(d) {
    const chips = [
      statChip(ICON.star, fmtNum(d.stars), "stars"),
      statChip(ICON.fork, fmtNum(d.forks), "forks"),
      statChip(ICON.issue, fmtNum(d.issues), "issues"),
    ];
    if (d.pushed_at) {
      const t = Date.parse(d.pushed_at);
      if (!isNaN(t)) chips.push(statChip(ICON.clock, "Updated " + timeAgo(t)));
    }
    if (d.language) chips.push(statChip("", d.language));
    let html = '<div class="serp-stats">' + chips.join("") + "</div>";
    if (d.archived) html += '<div class="serp-card-desc"><span class="serp-card-flag">Archived</span></div>';
    if (d.description) html += '<div class="serp-card-desc">' + esc(d.description) + "</div>";
    return html;
  }

  // Shared per-page cache so the top section and the inline cards never fetch
  // the same URL twice.
  const _metaCache = new Map();
  function fetchMeta(url) {
    if (_metaCache.has(url)) return _metaCache.get(url);
    const p = fetch("/card_meta?url=" + encodeURIComponent(url),
                    { headers: { Accept: "application/json" } })
      .then((r) => r.json()).catch(() => null);
    _metaCache.set(url, p);
    return p;
  }

  function loadCard(url, body, toggleHint) {
    body.innerHTML = '<div class="serp-card-loading"><span class="serp-card-spin"></span> Loading stats…</div>';
    fetchMeta(url)
      .then((d) => {
        if (!d || d.type !== "github") {
          body.innerHTML = '<div class="serp-card-desc">' +
            (d && d.missing ? "This item is unavailable." : "Stats unavailable right now.") + "</div>";
          if (toggleHint) toggleHint.textContent = "No stats";
          return;
        }
        body.innerHTML = renderGithub(d);
        if (toggleHint) toggleHint.textContent = fmtNum(d.stars) + " stars";
      })
      .catch(() => {
        body.innerHTML = '<div class="serp-card-desc">Stats unavailable right now.</div>';
      });
  }

  function enhanceCards(root) {
    (root || document).querySelectorAll(".result").forEach((result) => {
      if (result.dataset.serpCard) return;
      const url = resultUrl(result);
      const kind = url && classify(url);
      // Inline expandable cards are GitHub-only. Reddit results get the top
      // "sources" card instead — no extra stats under each result link.
      if (kind !== "github") return;
      result.dataset.serpCard = "1";

      const card = document.createElement("div");
      card.className = "serp-card serp-card-github";
      card.innerHTML =
        '<button type="button" class="serp-card-toggle">' +
          '<span class="serp-card-badge">' + ICON.github + "GitHub repo</span>" +
          '<span class="serp-card-hint">Show stats</span>' +
          '<span class="serp-card-chevron">▾</span>' +
        "</button>" +
        '<div class="serp-card-body"></div>';

      const toggle = card.querySelector(".serp-card-toggle");
      const body = card.querySelector(".serp-card-body");
      const hint = card.querySelector(".serp-card-hint");
      let loaded = false;
      toggle.addEventListener("click", (e) => {
        e.preventDefault();
        const open = card.classList.toggle("open");
        if (open && !loaded) { loaded = true; loadCard(url, body, hint); }
      });

      // Place the card just before the engines footer so it sits under the snippet.
      const engines = result.querySelector(".engines");
      if (engines && engines.parentNode) engines.parentNode.insertBefore(card, engines);
      else result.appendChild(card);
    });
  }

  // ── Top "sources" section: Reddit threads + GitHub repos ───────────────────

  function hostnameOf(url) {
    try { return new URL(url).hostname.replace(/^www\./, ""); } catch (_) { return ""; }
  }
  function ghRepoPath(url) {
    try {
      const s = new URL(url).pathname.split("/").filter(Boolean);
      return s.length >= 2 ? s[0] + "/" + s[1].replace(/\.git$/, "") : "";
    } catch (_) { return ""; }
  }
  function inlineStat(icon, val) {
    return '<span class="serp-src-stat">' + icon + "<span>" + esc(val) + "</span></span>";
  }
  function redditSubPath(url) {
    try {
      const s = new URL(url).pathname.split("/").filter(Boolean);
      const ri = s.indexOf("r");
      if (ri >= 0 && ri + 1 < s.length) return "r/" + s[ri + 1];
    } catch (_) { /* fall through */ }
    return "reddit.com";
  }
  // The host line under each top-section item, by result kind.
  function hostTextFor(kind, url) {
    if (kind === "github") return ghRepoPath(url) || hostnameOf(url);
    if (kind === "reddit") return redditSubPath(url);
    return hostnameOf(url);
  }
  // The inline stat chips under each top-section item, from fetched metadata.
  function statlineFor(d) {
    if (d.type === "github") {
      let h = inlineStat(ICON.star, fmtNum(d.stars));
      if (d.forks) h += inlineStat(ICON.fork, fmtNum(d.forks));
      return h;
    }
    if (d.type === "reddit") {
      return inlineStat(ICON.upvote, fmtNum(d.score)) +
             inlineStat(ICON.comment, fmtNum(d.comments));
    }
    return "";
  }

  function collectMatches() {
    const items = [];
    const seen = new Set();
    document.querySelectorAll(".result").forEach((result) => {
      const url = resultUrl(result);
      const kind = url && classify(url);
      if (!kind || seen.has(url)) return;
      seen.add(url);
      const a = result.querySelector("h3 a") || result.querySelector("h3");
      items.push({ url, kind, title: a ? a.textContent.trim() : url });
    });
    return items;
  }

  function buildGroup(label, icon, kind, items, newTab) {
    const g = document.createElement("div");
    g.className = "serp-src-group serp-src-" + kind;
    const head = document.createElement("div");
    head.className = "serp-src-head";
    head.innerHTML = icon + "<span>" + label + "</span>";
    g.appendChild(head);
    items.forEach((it) => {
      const a = document.createElement("a");
      a.className = "serp-src-item";
      a.href = it.url;
      if (newTab) { a.target = "_blank"; a.rel = "noopener noreferrer"; }
      else a.rel = "noopener";
      a.innerHTML =
        '<span class="serp-src-title"></span>' +
        '<span class="serp-src-meta"><span class="serp-src-host"></span>' +
        '<span class="serp-src-statline"></span></span>';
      a.querySelector(".serp-src-title").textContent = it.title || it.url;
      a.querySelector(".serp-src-host").textContent = hostTextFor(kind, it.url);
      g.appendChild(a);
      // Stats are small and the set is tiny, so load them straight away (cached,
      // and shared with the inline cards via _metaCache).
      fetchMeta(it.url).then((d) => {
        if (!d || d.type !== kind) return;
        a.querySelector(".serp-src-statline").innerHTML = statlineFor(d);
      });
    });
    return g;
  }

  const TOP_MAX = 6;
  function buildTopSection() {
    if (document.getElementById("serp-sources")) return;
    const results = document.getElementById("results");
    if (!results) return;
    const items = collectMatches();
    if (!items.length) return;

    const newTab = !!document.querySelector(
      '.result h3 a[target="_blank"], .result a.url_header[target="_blank"]');

    const wrap = document.createElement("div");
    wrap.id = "serp-sources";
    // Reddit first (the headline "discussion" card), then GitHub.
    const reddit = items.filter((it) => it.kind === "reddit");
    const github = items.filter((it) => it.kind === "github");
    if (reddit.length)
      wrap.appendChild(buildGroup("Reddit", ICON.reddit, "reddit", reddit.slice(0, TOP_MAX), newTab));
    if (github.length)
      wrap.appendChild(buildGroup("GitHub", ICON.github, "github", github.slice(0, TOP_MAX), newTab));
    if (!wrap.children.length) return;

    // Sits at the very top of the results column, just under the AI summary.
    const ai = document.getElementById("ai-summary-wrapper");
    if (ai && ai.parentNode === results) ai.after(wrap);
    else results.insertBefore(wrap, results.firstChild);
  }

  // ── 3. Copy-summary button on the AI summary block ─────────────────────────

  function aiSummaryText() {
    const box = document.getElementById("ai-summary-box");
    if (!box) return "";
    const parts = [];
    const content = box.querySelector(".ai-content");
    if (content) parts.push(content.innerText.trim());
    const expanded = box.querySelector(".ai-expanded.visible");
    if (expanded) parts.push(expanded.innerText.trim());
    return parts.filter(Boolean).join("\n\n").trim();
  }

  function enhanceAiSummary() {
    const box = document.getElementById("ai-summary-box");
    if (!box || box.dataset.serpCopy) return;
    // Only add once the streaming content area exists (button removed before then).
    if (!box.querySelector(".ai-content")) return;
    const header = box.querySelector(".ai-header");
    if (!header) return;
    box.dataset.serpCopy = "1";
    header.appendChild(makeCopyBtn("Copy summary", aiSummaryText));
  }

  // ── Orchestration ──────────────────────────────────────────────────────────

  function enhanceAll() {
    buildTopSection();
    enhanceCopyLinks(document);
    enhanceCards(document);
    enhanceAiSummary();
  }

  function run() {
    if (!document.getElementById("results") && !document.querySelector(".result")) return;
    injectStyles();
    enhanceAll();

    // Re-enhance results added by infinite scroll and the AI summary box, which
    // is built asynchronously after the user clicks "Show AI summary".
    const target = document.getElementById("results") || document.body;
    let scheduled = false;
    const observer = new MutationObserver(() => {
      if (scheduled) return;
      scheduled = true;
      requestAnimationFrame(() => { scheduled = false; enhanceAll(); });
    });
    observer.observe(target, { childList: true, subtree: true });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", run);
  } else {
    run();
  }
})();
