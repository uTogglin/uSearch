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

  // Reddit privacy-frontend hosts (e.g. redlib) the card_meta plugin injects
  // from the privacy_redirect setting. A result URL the Privacy redirect
  // rewrote (reddit.com → redlib…) is still recognised as a Reddit thread, so
  // it keeps its place in the top "sources" card. reddit.com is always known.
  const REDDIT_HOSTS = (function () {
    const set = new Set(["reddit.com"]);
    try {
      const s = document.querySelector('script[src*="serp_enhance.js"]');
      const raw = s && s.getAttribute("data-reddit-frontends");
      if (raw) raw.split(",").forEach((h) => {
        h = h.trim().toLowerCase().replace(/^www\./, "");
        if (h) set.add(h);
      });
    } catch (_) { /* ignore */ }
    return set;
  })();

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

    /* "Where to watch" box — JustWatch offers, cheapest first */
    .serp-src-watch .serp-src-head { color:#1f6feb; }
    .serp-watch-head-title { color:inherit; text-decoration:none; }
    .serp-watch-head-title:hover { text-decoration:underline; }
    .serp-watch-yr { font-weight:400; color:#8a8a8a; }
    .serp-watch-row {
      display:flex; align-items:center; gap:8px; padding:6px 6px; border-radius:8px;
      text-decoration:none; color:inherit; border-top:1px solid rgba(127,127,127,0.12);
    }
    .serp-src-watch .serp-watch-row:first-of-type { border-top:none; }
    .serp-watch-row:hover { background:rgba(66,133,244,0.08); }
    .serp-watch-provider {
      font-size:0.84rem; font-weight:600; color:var(--color-base-font,#333);
      flex:1 1 auto; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
    }
    .serp-watch-right { display:inline-flex; align-items:center; gap:7px; flex-shrink:0; }
    .serp-watch-kind {
      font-size:0.66rem; font-weight:700; letter-spacing:.03em; text-transform:uppercase;
      padding:2px 6px; border-radius:5px; line-height:1;
    }
    .serp-watch-free   { color:#1a7f37; background:rgba(26,127,55,0.12); }
    .serp-watch-stream { color:#1f6feb; background:rgba(31,111,235,0.12); }
    .serp-watch-rent   { color:#9a6700; background:rgba(154,103,0,0.13); }
    .serp-watch-buy    { color:#8250df; background:rgba(130,80,223,0.13); }
    .serp-watch-quality { font-size:0.68rem; font-weight:600; color:#8a8a8a; min-width:1.4em; text-align:right; }
    .serp-watch-price {
      font-size:0.82rem; font-weight:700; color:var(--color-base-font,#333);
      min-width:3.6em; text-align:right;
    }
    .serp-watch-price.muted { font-weight:500; color:#8a8a8a; }
    .serp-watch-msg { padding:4px 6px; color:#8a8a8a; font-size:0.78rem; }
    @media (prefers-color-scheme: dark) {
      .serp-watch-provider, .serp-watch-price { color:#dcdcdc; }
    }
    .theme-dark .serp-watch-provider, .theme-dark .serp-watch-price { color:#dcdcdc; }

    /* "Best PC price" box — CheapShark + ITAD official stores, cheapest first */
    .serp-src-game .serp-src-head { color:#1a7f37; }
    .serp-game-head-title { color:inherit; text-decoration:none; }
    .serp-game-head-title:hover { text-decoration:underline; }
    .serp-game-sub { font-weight:400; font-size:0.72rem; color:#8a8a8a; margin-left:2px; }
    .serp-game-section { margin-top:2px; }
    .serp-game-label {
      font-size:0.66rem; font-weight:700; letter-spacing:.04em; text-transform:uppercase;
      color:#8a8a8a; margin:0 0 2px 2px;
    }
    .serp-game-row {
      display:flex; align-items:center; gap:8px; padding:6px 6px; border-radius:8px;
      text-decoration:none; color:inherit; border-top:1px solid rgba(127,127,127,0.12);
    }
    .serp-game-list .serp-game-row:first-of-type { border-top:none; }
    .serp-game-row:hover { background:rgba(26,127,55,0.08); }
    .serp-game-store {
      font-size:0.84rem; font-weight:600; color:var(--color-base-font,#333);
      flex:1 1 auto; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
    }
    .serp-game-right { display:inline-flex; align-items:center; gap:7px; flex-shrink:0; }
    .serp-game-cut {
      font-size:0.66rem; font-weight:700; color:#1a7f37; background:rgba(26,127,55,0.12);
      padding:2px 5px; border-radius:5px; line-height:1;
    }
    .serp-game-price {
      font-size:0.82rem; font-weight:700; color:var(--color-base-font,#333);
      min-width:3.6em; text-align:right;
    }
    .serp-game-regular { font-size:0.7rem; color:#8a8a8a; text-decoration:line-through; }
    .serp-game-msg { padding:4px 6px; color:#8a8a8a; font-size:0.78rem; }
    @media (prefers-color-scheme: dark) {
      .serp-game-store, .serp-game-price { color:#dcdcdc; }
    }
    .theme-dark .serp-game-store, .theme-dark .serp-game-price { color:#dcdcdc; }

    /* "Maps" box — deep-links a local/place query to Apple Maps */
    .serp-src-maps .serp-src-head { color:#1a73e8; flex-wrap:wrap; }
    .serp-maps-head-title { color:inherit; text-decoration:none; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .serp-maps-head-title:hover { text-decoration:underline; }
    .serp-maps-actions { display:flex; flex-wrap:wrap; gap:8px; margin-top:4px; }
    .serp-maps-btn {
      display:inline-flex; align-items:center; gap:6px; text-decoration:none;
      font-size:0.8rem; font-weight:600; line-height:1; padding:8px 12px;
      border-radius:8px; border:1px solid rgba(26,115,232,0.35);
      color:#1a73e8; background:rgba(26,115,232,0.06); white-space:nowrap;
    }
    .serp-maps-btn svg { width:15px; height:15px; }
    .serp-maps-btn:hover { background:rgba(26,115,232,0.14); }
    .serp-maps-btn.primary { color:#fff; background:#1a73e8; border-color:#1a73e8; }
    .serp-maps-btn.primary:hover { background:#1666d0; }
    @media (prefers-color-scheme: dark) {
      .serp-maps-btn { color:#8ab4f8; border-color:rgba(138,180,248,0.4); background:rgba(138,180,248,0.08); }
      .serp-maps-btn.primary { color:#fff; background:#1a73e8; }
    }
    .theme-dark .serp-maps-btn { color:#8ab4f8; border-color:rgba(138,180,248,0.4); background:rgba(138,180,248,0.08); }
    .theme-dark .serp-maps-btn.primary { color:#fff; background:#1a73e8; }

    /* Currency converter card (currency-conversion queries) */
    #serp-currency {
      border:1px solid rgba(127,127,127,0.22); border-radius:14px;
      background:rgba(127,127,127,0.04); padding:16px 18px 14px;
      margin:0 0 16px 0; box-sizing:border-box;
    }
    .serp-fx-grid {
      display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1.1fr);
      gap:18px 24px; align-items:start;
    }
    @media (max-width:560px) { .serp-fx-grid { grid-template-columns:1fr; } }
    .serp-fx-equals { font-size:0.92rem; color:#8a8a8a; margin:0 0 2px; }
    .serp-fx-result {
      font-size:2rem; font-weight:400; line-height:1.15; margin:0 0 14px;
      color:var(--color-base-font,#202124); letter-spacing:-0.01em;
    }
    .serp-fx-result .serp-fx-amt { font-weight:600; }
    .serp-fx-result .serp-fx-cur { color:#8a8a8a; }
    .serp-fx-fields { display:flex; flex-direction:column; gap:9px; }
    .serp-fx-row {
      display:grid; grid-template-columns:1fr 9.5em; gap:8px; align-items:stretch;
    }
    .serp-fx-input, .serp-fx-select {
      font:inherit; font-size:0.92rem; color:var(--color-base-font,#202124);
      background:var(--color-base-background,#fff);
      border:1px solid rgba(127,127,127,0.4); border-radius:8px;
      padding:8px 10px; min-width:0; box-sizing:border-box; height:38px;
    }
    .serp-fx-input:focus, .serp-fx-select:focus {
      outline:none; border-color:#4285f4; box-shadow:0 0 0 1px rgba(66,133,244,0.4);
    }
    .serp-fx-input { -moz-appearance:textfield; }
    .serp-fx-input::-webkit-outer-spin-button,
    .serp-fx-input::-webkit-inner-spin-button { -webkit-appearance:none; margin:0; }
    .serp-fx-select { cursor:pointer; }

    .serp-fx-chart { position:relative; min-width:0; }
    .serp-fx-chart svg { display:block; width:100%; height:auto; overflow:visible; }
    .serp-fx-chart .serp-fx-gridline { stroke:rgba(127,127,127,0.18); stroke-width:1; }
    .serp-fx-chart .serp-fx-axis {
      font-size:10px; fill:#8a8a8a; font-family:inherit;
    }
    .serp-fx-rateline { font-size:0.74rem; color:#8a8a8a; margin:6px 2px 0; }
    .serp-fx-chart-empty {
      display:flex; align-items:center; justify-content:center; min-height:120px;
      color:#8a8a8a; font-size:0.8rem; text-align:center; padding:0 10px;
    }
    @media (prefers-color-scheme: dark) {
      .serp-fx-result { color:#e8e8e8; }
      .serp-fx-input, .serp-fx-select {
        color:#e8e8e8; background:rgba(255,255,255,0.04); border-color:rgba(255,255,255,0.18);
      }
    }
    .theme-dark .serp-fx-result { color:#e8e8e8; }
    .theme-dark .serp-fx-input, .theme-dark .serp-fx-select {
      color:#e8e8e8; background:rgba(255,255,255,0.04); border-color:rgba(255,255,255,0.18);
    }

    /* ── Site "shield" — per-result priority + info panel ─────────────────── */
    .serp-shield-btn {
      display:inline-flex; align-items:center; justify-content:center;
      vertical-align:middle; margin-left:8px; position:relative; top:-1px;
      background:transparent; border:1px solid rgba(127,127,127,0.28);
      border-radius:6px; color:var(--color-base-font,#666);
      width:22px; height:20px; padding:0; cursor:pointer; opacity:0.5;
      transition:opacity .15s,background .15s,border-color .15s,color .15s;
    }
    .result:hover .serp-shield-btn { opacity:0.9; }
    .serp-shield-btn:hover { opacity:1; background:rgba(66,133,244,0.10); border-color:rgba(66,133,244,0.45); color:#4285f4; }
    .serp-shield-btn svg { width:13px; height:13px; }
    /* The shield reflects the current per-site priority at a glance. */
    .serp-shield-btn.lvl-block  { color:#c0392b; border-color:rgba(192,57,43,0.5);  opacity:0.95; }
    .serp-shield-btn.lvl-lower  { color:#9a6700; border-color:rgba(154,103,0,0.45); opacity:0.9; }
    .serp-shield-btn.lvl-raise  { color:#1a7f37; border-color:rgba(26,127,55,0.45); opacity:0.9; }
    .serp-shield-btn.lvl-pin    { color:#1f6feb; border-color:rgba(31,111,235,0.5); opacity:0.95; }

    /* A blocked result is collapsed to a thin "unblock" stub (not removed, so
       the user can always reverse it). */
    .result.serp-blocked > *:not(.serp-blocked-note) { display:none !important; }
    .result.serp-blocked {
      opacity:1; padding:6px 0 !important; margin:0 !important; border:none !important; box-shadow:none !important;
    }
    .serp-blocked-note {
      display:flex; align-items:center; gap:8px; font-size:0.78rem; color:#8a8a8a;
    }
    .serp-blocked-note button {
      background:transparent; border:1px solid rgba(127,127,127,0.3); border-radius:6px;
      color:inherit; font:inherit; font-size:0.74rem; padding:2px 8px; cursor:pointer;
    }
    .serp-blocked-note button:hover { color:#4285f4; border-color:rgba(66,133,244,0.45); }
    .result.serp-lowered { opacity:0.62; }

    /* Modal */
    .serp-modal-backdrop {
      position:fixed; inset:0; background:rgba(0,0,0,0.5);
      display:flex; align-items:flex-start; justify-content:center;
      padding:6vh 16px 16px; z-index:9999; overflow:auto;
      animation:serp-fade .12s ease;
    }
    @keyframes serp-fade { from { opacity:0; } to { opacity:1; } }
    .serp-modal {
      width:100%; max-width:420px; border-radius:16px; overflow:hidden;
      background:var(--color-base-background,#fff);
      border:1px solid rgba(127,127,127,0.22);
      box-shadow:0 18px 48px rgba(0,0,0,0.32); color:var(--color-base-font,#202124);
    }
    .serp-modal-head {
      display:flex; align-items:flex-start; gap:10px; padding:16px 16px 14px;
      background:rgba(127,127,127,0.06); border-bottom:1px solid rgba(127,127,127,0.14);
    }
    .serp-modal-fav { width:22px; height:22px; border-radius:5px; flex-shrink:0; margin-top:2px; object-fit:contain; background:rgba(127,127,127,0.12); }
    .serp-modal-titles { flex:1 1 auto; min-width:0; }
    .serp-modal-title { font-size:0.96rem; font-weight:600; line-height:1.3; margin:0; word-break:break-word; }
    .serp-modal-host { font-size:0.8rem; color:#8a8a8a; margin-top:2px; word-break:break-all; }
    .serp-modal-close {
      flex-shrink:0; background:transparent; border:none; cursor:pointer;
      color:var(--color-base-font,#555); width:28px; height:28px; border-radius:8px;
      display:inline-flex; align-items:center; justify-content:center;
    }
    .serp-modal-close:hover { background:rgba(127,127,127,0.14); }
    .serp-modal-close svg { width:16px; height:16px; }
    .serp-modal-body { padding:14px 16px 18px; }

    .serp-rank-label {
      display:flex; align-items:center; gap:6px; font-size:0.88rem; font-weight:600; margin:0 0 9px;
    }
    .serp-rank-label .serp-rank-info {
      width:15px; height:15px; color:#9aa0a6; cursor:help; flex-shrink:0;
    }
    .serp-seg {
      display:grid; grid-template-columns:repeat(5,1fr); gap:0;
      border:1px solid rgba(127,127,127,0.28); border-radius:9px; overflow:hidden;
    }
    .serp-seg button {
      background:rgba(127,127,127,0.06); border:none; border-left:1px solid rgba(127,127,127,0.18);
      color:var(--color-base-font,#444); font:inherit; font-size:0.8rem; font-weight:600;
      padding:9px 4px; cursor:pointer; transition:background .12s,color .12s;
    }
    .serp-seg button:first-child { border-left:none; }
    .serp-seg button:hover { background:rgba(127,127,127,0.14); }
    .serp-seg button.active { background:#202124; color:#fff; }
    .theme-dark .serp-seg button.active { background:#e8e8e8; color:#202124; }
    .serp-seg button.active.act-block { background:#c0392b; color:#fff; }
    .serp-seg button.active.act-lower { background:#9a6700; color:#fff; }
    .serp-seg button.active.act-raise { background:#1a7f37; color:#fff; }
    .serp-seg button.active.act-pin   { background:#1f6feb; color:#fff; }

    .serp-global {
      display:flex; align-items:center; gap:8px; flex-wrap:wrap;
      margin:11px 0 2px; font-size:0.78rem; color:#8a8a8a;
    }
    .serp-global b { color:var(--color-base-font,#444); font-weight:600; }
    .serp-global-btn {
      margin-left:auto; background:transparent; border:1px solid rgba(31,111,235,0.45);
      color:#1f6feb; border-radius:7px; font:inherit; font-size:0.76rem; font-weight:600;
      padding:5px 10px; cursor:pointer;
    }
    .serp-global-btn:hover { background:rgba(31,111,235,0.10); }
    .serp-global-btn:disabled { opacity:0.5; cursor:default; }
    .serp-global-msg { width:100%; font-size:0.74rem; }
    .serp-global-msg.ok { color:#1a7f37; }
    .serp-global-msg.err { color:#c0392b; }

    .serp-info { list-style:none; margin:16px 0 0; padding:14px 0 0; border-top:1px solid rgba(127,127,127,0.14); }
    .serp-info li { display:flex; align-items:flex-start; gap:12px; padding:8px 0; }
    .serp-info .serp-info-ic { width:20px; height:20px; color:#9aa0a6; flex-shrink:0; margin-top:1px; }
    .serp-info .serp-info-ic svg { width:20px; height:20px; }
    .serp-info-k { font-size:0.86rem; color:#8a8a8a; flex:1 1 auto; }
    .serp-info-v { font-size:0.86rem; font-weight:600; text-align:right; word-break:break-word; max-width:60%; }
    .serp-info-v.muted { font-weight:400; color:#9aa0a6; }
    .serp-info-v .ok { color:#1a7f37; }
    @media (prefers-color-scheme: dark) {
      .serp-shield-btn { color:#bdbdbd; }
    }
    .theme-dark .serp-shield-btn { color:#bdbdbd; }

    /* Top status line — search time + results hidden by the user's shield prefs.
       Sits between the language/filters row and the results column. The left
       margin mirrors .search_filters (@results-offset + 0.6rem) so it lines up
       with the filters above it rather than running to the page edge. */
    .serp-statusbar {
      display:flex; align-items:center; flex-wrap:wrap; gap:5px 11px;
      margin:2px 2rem 14px 10.6rem; padding:0 2px; font-size:0.78rem; color:#70757a;
    }
    @media screen and (max-width:79.75em) {
      .serp-statusbar { margin-left:3.5rem; }  /* @results-tablet-offset + 3rem */
    }
    @media screen and (min-width:50em) {
      .center-alignment-yes #main_results .serp-statusbar {
        width:var(--center-page-width); margin-left:0.5rem; margin-right:0;
      }
    }
    .serp-status-time { display:inline-flex; align-items:center; gap:6px; }
    .serp-status-time svg { width:13px; height:13px; opacity:0.7; }
    .serp-status-sep { opacity:0.4; }
    .serp-status-blocked {
      display:inline-flex; align-items:center; gap:6px; background:transparent;
      border:none; padding:0; margin:0; color:inherit; font:inherit; font-size:0.78rem;
      cursor:default;
    }
    .serp-status-blocked svg { width:13px; height:13px; opacity:0.7; flex-shrink:0; }
    .serp-status-blocked.has-blocked { cursor:pointer; color:#c0392b; }
    .serp-status-blocked.has-blocked svg { opacity:0.95; }
    .serp-status-view { text-decoration:underline; opacity:0.85; }
    .serp-status-blocked.has-blocked:hover .serp-status-view { opacity:1; }
    .serp-status-host {
      flex:1 1 auto; min-width:0; font-weight:600; color:var(--color-base-font,#444);
      overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
    }
    .serp-status-ttl { color:#8a8a8a; font-weight:400; }
    /* Blocked-results modal: same card as the shield panel, just a scrollable
       list of hidden sites with an Unblock action (no per-site connection/stats). */
    .serp-blk-ic {
      display:inline-flex; align-items:center; justify-content:center;
      color:#c0392b; background:rgba(192,57,43,0.12);
    }
    .serp-blk-ic svg { width:14px; height:14px; }
    .serp-blk-body { padding:8px 8px 12px; }
    .serp-blk-list { max-height:min(52vh,380px); overflow-y:auto; }
    .serp-blk-row {
      display:flex; align-items:center; gap:10px; padding:9px 8px;
      border-top:1px solid rgba(127,127,127,0.12);
    }
    .serp-blk-row:first-child { border-top:none; }
    .serp-blk-row button {
      flex-shrink:0; background:transparent; border:1px solid rgba(127,127,127,0.3);
      border-radius:6px; color:inherit; font:inherit; font-size:0.76rem; padding:3px 10px; cursor:pointer;
    }
    .serp-blk-row button:hover { color:#4285f4; border-color:rgba(66,133,244,0.45); }
    @media (prefers-color-scheme: dark) { .serp-statusbar { color:#9aa0a6; } }
    .theme-dark .serp-statusbar { color:#9aa0a6; }

    /* ── Lenses: saved filter-set dropdown below the search bar ───────────── */
    .serp-lens { position:relative; display:inline-flex; vertical-align:middle; }
    .search_filters .serp-lens { margin-left:0.4rem; }
    #search_view .serp-lens { margin:0.55rem auto 0; }
    .serp-lens-btn {
      display:inline-flex; align-items:center; gap:6px;
      background:rgba(127,127,127,0.06); border:1px solid rgba(127,127,127,0.28);
      border-radius:8px; color:var(--color-base-font,#444); font:inherit; font-size:0.82rem;
      padding:5px 9px; cursor:pointer; transition:background .12s,border-color .12s,color .12s;
    }
    .serp-lens-btn:hover { background:rgba(66,133,244,0.08); border-color:rgba(66,133,244,0.4); }
    .serp-lens.active .serp-lens-btn { color:#1f6feb; border-color:rgba(31,111,235,0.5); background:rgba(31,111,235,0.08); }
    .serp-lens-ic { display:inline-flex; } .serp-lens-ic svg { width:14px; height:14px; }
    .serp-lens-caret svg { width:13px; height:13px; opacity:0.7; }
    .serp-lens-cur { font-weight:600; white-space:nowrap; }
    .serp-lens-menu {
      position:absolute; top:calc(100% + 6px); left:0; z-index:9998; min-width:210px;
      background:var(--color-base-background,#fff); border:1px solid rgba(127,127,127,0.22);
      border-radius:12px; box-shadow:0 12px 32px rgba(0,0,0,0.22); padding:6px;
      animation:serp-fade .1s ease;
    }
    .serp-lens-item {
      display:flex; align-items:center; justify-content:space-between; gap:10px; width:100%;
      background:transparent; border:none; border-radius:8px; color:var(--color-base-font,#333);
      font:inherit; font-size:0.85rem; text-align:left; padding:8px 10px; cursor:pointer;
    }
    .serp-lens-item:hover { background:rgba(127,127,127,0.1); }
    .serp-lens-item.active { background:rgba(31,111,235,0.12); color:#1f6feb; font-weight:600; }
    .serp-lens-mode { font-size:0.7rem; font-weight:600; text-transform:uppercase; letter-spacing:0.03em; color:#9aa0a6; }
    .serp-lens-item.active .serp-lens-mode { color:#1f6feb; }
    .serp-lens-divider { height:1px; background:rgba(127,127,127,0.16); margin:5px 6px; }
    .serp-lens-edit-btn { color:#1f6feb; font-weight:600; }

    /* Lens manager modal (reuses .serp-modal shell) */
    .serp-lens-modal { max-width:480px; }
    .serp-lens-headic { display:inline-flex; align-items:center; justify-content:center; padding:3px; color:#1f6feb; }
    .serp-lens-headic svg { width:16px; height:16px; }
    .serp-lens-mgr { max-height:min(70vh,560px); overflow-y:auto; }
    .serp-lens-edit { border:1px solid rgba(127,127,127,0.2); border-radius:12px; padding:10px; margin-bottom:10px; }
    .serp-lens-row { display:flex; align-items:center; gap:8px; margin-bottom:8px; }
    .serp-lens-name {
      flex:1 1 auto; min-width:0; font:inherit; font-size:0.9rem; font-weight:600;
      padding:6px 9px; border:1px solid rgba(127,127,127,0.28); border-radius:8px;
      background:var(--color-base-background,#fff); color:var(--color-base-font,#202124);
    }
    .serp-lens-modeseg { display:inline-flex; border:1px solid rgba(127,127,127,0.28); border-radius:8px; overflow:hidden; flex-shrink:0; }
    .serp-lens-modeseg button {
      background:rgba(127,127,127,0.06); border:none; border-left:1px solid rgba(127,127,127,0.18);
      color:var(--color-base-font,#444); font:inherit; font-size:0.76rem; font-weight:600; padding:6px 10px; cursor:pointer;
    }
    .serp-lens-modeseg button:first-child { border-left:none; }
    .serp-lens-modeseg button.active { background:#1f6feb; color:#fff; }
    .serp-lens-del {
      flex-shrink:0; background:transparent; border:1px solid rgba(127,127,127,0.28); border-radius:8px;
      width:30px; height:30px; display:inline-flex; align-items:center; justify-content:center;
      color:#9aa0a6; cursor:pointer;
    }
    .serp-lens-del:hover { color:#c0392b; border-color:rgba(192,57,43,0.45); background:rgba(192,57,43,0.08); }
    .serp-lens-del svg { width:14px; height:14px; }
    .serp-lens-sites {
      width:100%; box-sizing:border-box; font:inherit; font-size:0.82rem; line-height:1.5;
      padding:8px 10px; border:1px solid rgba(127,127,127,0.28); border-radius:8px; resize:vertical;
      background:var(--color-base-background,#fff); color:var(--color-base-font,#202124);
    }
    .serp-lens-add {
      width:100%; background:transparent; border:1px dashed rgba(127,127,127,0.4); border-radius:10px;
      color:var(--color-base-font,#444); font:inherit; font-size:0.85rem; font-weight:600; padding:9px; cursor:pointer;
    }
    .serp-lens-add:hover { color:#1f6feb; border-color:rgba(31,111,235,0.5); background:rgba(31,111,235,0.06); }
    .serp-lens-help, .serp-lens-empty { font-size:0.76rem; color:#8a8a8a; margin:10px 2px 0; line-height:1.5; }
    .theme-dark .serp-lens-name, .theme-dark .serp-lens-sites { color:#e8e8e8; background:rgba(255,255,255,0.04); border-color:rgba(255,255,255,0.18); }
    .theme-dark .serp-lens-btn { color:#cfcfcf; }
    .theme-dark .serp-lens-menu, .theme-dark .serp-lens-edit { border-color:rgba(255,255,255,0.14); }
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
    watch: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="10 8 16 12 10 16 10 8" fill="currentColor" stroke="none"/><rect x="2" y="4" width="20" height="15" rx="2.5"/><line x1="8" y1="22" x2="16" y2="22"/></svg>',
    game: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="6" y1="11" x2="10" y2="11"/><line x1="8" y1="9" x2="8" y2="13"/><line x1="15" y1="12" x2="15.01" y2="12"/><line x1="18" y1="10" x2="18.01" y2="10"/><rect x="2" y="6" width="20" height="12" rx="4"/></svg>',
    maps: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 10c0 6-9 12-9 12s-9-6-9-12a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/></svg>',
    directions: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="3 11 22 2 13 21 11 13 3 11"/></svg>',
    shield: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>',
    info: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="11" x2="12" y2="16"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>',
    lock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>',
    globe: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><line x1="3" y1="12" x2="21" y2="12"/><path d="M12 3a14 14 0 0 1 0 18 14 14 0 0 1 0-18z"/></svg>',
    user: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/></svg>',
    starline: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 .59l3.09 6.26 6.91 1-5 4.87 1.18 6.88L12 16.5l-6.18 3.25L7 12.72l-5-4.87 6.91-1z"/></svg>',
    close: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><line x1="6" y1="6" x2="18" y2="18"/><line x1="18" y1="6" x2="6" y2="18"/></svg>',
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
    if (REDDIT_HOSTS.has(host) || host.endsWith(".reddit.com")) {
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

  // ── "Where to watch" box (film/TV queries) ─────────────────────────────────

  // Title-page URLs that mark a result set as a film/TV query (mirrors the
  // server-side intent_boost.ENTERTAINMENT_REF list). Matched against the full
  // "host/path" — a bare domain is NOT enough: a real film/TV query always
  // surfaces a title page, whereas a generic query ("why are search results so
  // bad") only ever picks up a stray article/list page on one of these hosts,
  // which used to wrongly light up the watch box. iket.me is libremdb.
  const ENT_TITLE_RE = [
    /(^|\.)imdb\.com\/(?:[a-z]{2}\/)?title\/tt\d+/,
    /(^|\.)iket\.me\/title\/tt\d+/,
    /(^|\.)themoviedb\.org\/(?:movie|tv)\/\d+/,
    /(^|\.)rottentomatoes\.com\/(?:m|tv)\/[^/]+/,
    /(^|\.)metacritic\.com\/(?:movie|tv|tv-show)\/[^/]+/,
    /(^|\.)justwatch\.com\//,
    /(^|\.)thetvdb\.com\/(?:series|movies)\/[^/]+/,
    /(^|\.)letterboxd\.com\/film\/[^/]+/,
  ];

  // Explicit film/TV qualifiers in the query (independent of the result set, so
  // they fire even when a same-named game or product dominates the results).
  //
  // WATCH_TRAIL_RE is the *trailing* qualifier people append to disambiguate a
  // title that also names a game ("sonic movie", "the last of us series"). A
  // trailing qualifier is decisive (see queryIntent) and the last word wins, so
  // "the lego movie game" reads as a game while "untitled goose game movie"
  // reads as a film. Bare "series"/"show" only count at the very end, so
  // "time series analysis" is not treated as entertainment.
  const WATCH_TRAIL_RE = /\b(?:tv\s+series|web\s+series|mini[-\s]?series|tv\s+show|web\s+show|series|show|movies?|films?|anime|cartoons?)\s*$/i;

  // WATCH_ANY_RE matches qualifiers anywhere in the query: "movie"/"film" carry
  // entertainment intent wherever they sit ("watch sonic movie online", "scary
  // movie"), as do season/episode markers and the multi-word series/show forms.
  const WATCH_ANY_RE = /\bmovies?\b|\bfilms?\b|\b(?:tv|web)\s+series\b|\bmini[-\s]?series\b|\b(?:tv|web)\s+show\b|\banime\b|\bseason\s*\d+\b|\bepisode\s*\d+\b|\bs\d{1,2}e\d{1,3}\b/i;

  function hasEntHost() {
    let found = false;
    document.querySelectorAll(".result").forEach((r) => {
      if (found) return;
      const u = resultUrl(r);
      if (!u) return;
      const hostPath = u.replace(/^https?:\/\//i, "").toLowerCase();
      if (ENT_TITLE_RE.some((re) => re.test(hostPath))) found = true;
    });
    return found;
  }

  function currentQuery() {
    try {
      const q = new URLSearchParams(location.search).get("q");
      if (q && q.trim()) return q.trim();
    } catch (_) { /* ignore */ }
    const inp = document.getElementById("q");
    return inp && inp.value ? inp.value.trim() : "";
  }

  const KIND_LABEL = { free: "Free", stream: "Stream", rent: "Rent", buy: "Buy" };

  function renderWatchOffers(box, d, newTab) {
    const head = box.querySelector(".serp-src-head");
    const yr = d.year ? ' <span class="serp-watch-yr">(' + esc(d.year) + ")</span>" : "";
    const titleHtml = ICON.watch + "<span>" + _("Where to watch") + " · </span>";
    if (d.url) {
      const a = document.createElement("a");
      a.className = "serp-watch-head-title";
      a.href = d.url;
      if (newTab) { a.target = "_blank"; a.rel = "noopener noreferrer"; } else a.rel = "noopener";
      a.innerHTML = esc(d.title) + yr;
      head.innerHTML = titleHtml;
      head.appendChild(a);
    } else {
      head.innerHTML = titleHtml + "<span>" + esc(d.title) + yr + "</span>";
    }

    const list = box.querySelector(".serp-watch-list");
    list.innerHTML = "";
    (d.offers || []).forEach((o) => {
      const a = document.createElement("a");
      a.className = "serp-watch-row";
      a.href = o.url || d.url || "#";
      if (newTab) { a.target = "_blank"; a.rel = "noopener noreferrer"; } else a.rel = "noopener";
      // Price column: bold cost for rent/buy, muted label for free/subscription
      // (the kind badge already says "Free"/"Stream", so don't repeat it).
      let priceHtml = "";
      if (o.kind === "rent" || o.kind === "buy") {
        priceHtml = '<span class="serp-watch-price">' + esc(o.priceText || "") + "</span>";
      } else if (o.kind === "stream") {
        priceHtml = '<span class="serp-watch-price muted">' + esc(o.priceText || "") + "</span>";
      }
      a.innerHTML =
        '<span class="serp-watch-provider"></span>' +
        '<span class="serp-watch-right">' +
          '<span class="serp-watch-kind serp-watch-' + o.kind + '">' +
            esc(KIND_LABEL[o.kind] || o.kind) + "</span>" +
          '<span class="serp-watch-quality">' + esc(o.quality || "") + "</span>" +
          priceHtml +
        "</span>";
      a.querySelector(".serp-watch-provider").textContent = o.provider;
      list.appendChild(a);
    });
    box.style.display = list.children.length ? "" : "none";
  }

  // Insert (once) a "Where to watch" box at the top of the sources wrap and
  // fetch its offers. One-shot: the fetch is attempted at most once per page,
  // so a "no offers" drop isn't retried on every mutation-observer tick.
  let _watchAttempted = false;
  function ensureWatchBox(wrap, newTab) {
    if (_watchAttempted || document.getElementById("serp-watch")) return;
    const query = currentQuery();
    if (!query) return;
    _watchAttempted = true;

    const box = document.createElement("div");
    box.id = "serp-watch";
    box.className = "serp-src-group serp-src-watch";
    box.style.display = "none";
    box.innerHTML =
      '<div class="serp-src-head">' + ICON.watch + "<span>" + _("Where to watch") + "</span></div>" +
      '<div class="serp-watch-list"><div class="serp-watch-msg">' + _("Loading…") + "</div></div>";
    wrap.insertBefore(box, wrap.firstChild);  // above Reddit/GitHub

    function drop() {
      box.remove();
      // If the wrap existed only for this box, don't leave an empty container.
      if (wrap && !wrap.querySelector(".serp-src-group")) wrap.remove();
    }

    fetch("/watch_offers?q=" + encodeURIComponent(query),
          { headers: { Accept: "application/json" } })
      .then((r) => r.json())
      .then((d) => {
        if (!d || d.type !== "watch" || !(d.offers && d.offers.length)) {
          drop();  // nothing to show — don't leave an empty card
          return;
        }
        renderWatchOffers(box, d, newTab);
      })
      .catch(drop);
  }

  // ── "Best PC price" box (PC-game queries) ──────────────────────────────────

  // Hosts that mark a result set as a PC-game query. Curated to game-specific
  // domains (Metacritic is deliberately excluded — it also covers film/TV and
  // would collide with the watch box). The box self-cancels when no offers are
  // found, so a stray match costs at most one cancelled fetch.
  const GAME_HOSTS = ["store.steampowered.com", "steampowered.com", "steamcommunity.com",
    "gog.com", "epicgames.com", "store.epicgames.com", "igdb.com", "pcgamingwiki.com",
    "gamefaqs.gamespot.com", "howlongtobeat.com", "isthereanydeal.com",
    "gg.deals", "opencritic.com", "protondb.com"];

  // A trailing "game"/"video game" is how people disambiguate a title that also
  // names a film or show ("sonic game", "the last of us game"). It is decisive
  // (see queryIntent) and, being the last word, beats an earlier film/TV
  // qualifier. NOTE: bare trailing "game" is inherently ambiguous — "squid game"
  // and "game of thrones" name shows, not games — but the user typed the word,
  // so we honour it: the box self-cancels if no PC game matches, and appending
  // "show"/"series"/"movie" flips the query back to the watch box.
  const GAME_TRAIL_RE = /\b(?:video\s*game|pc\s+game|videogame|game)\s*$/i;

  // Explicit shopping phrasing ("buy/price/cheapest … pc/steam", "steam/cd key")
  // is game intent anywhere in the query, even with a generic result set.
  const GAME_ANY_RE = /\bcd\s*keys?\b|\b(?:steam|gog|epic|game)\s+keys?\b|\bpc\s+game\b|\b(?:buy|price|prices|cheapest|deal|deals)\b[\s\S]*\b(?:pc|steam|game)\b|\b(?:pc|steam|game)\b[\s\S]*\b(?:buy|price|prices|cheapest|deal|deals)\b/i;

  function hasGameHost() {
    let found = false;
    document.querySelectorAll(".result").forEach((r) => {
      if (found) return;
      const h = hostnameOf(resultUrl(r));
      if (h && GAME_HOSTS.some((e) => h === e || h.endsWith("." + e))) found = true;
    });
    return found;
  }

  // Decide film/TV vs game from the query's explicit qualifiers, so the user's
  // words override whatever the result hosts suggest. A *trailing* qualifier
  // ("sonic game" vs "sonic movie") is the strongest signal and the last word
  // wins; absent that, a qualifier anywhere decides. Returns "game" | "watch" |
  // null — null falls back to result-host detection, which may legitimately
  // light up BOTH boxes for a franchise with both a game and a show.
  function queryIntent() {
    const q = currentQuery();
    if (!q) return null;
    if (GAME_TRAIL_RE.test(q)) return "game";
    if (WATCH_TRAIL_RE.test(q)) return "watch";
    if (GAME_ANY_RE.test(q)) return "game";
    if (WATCH_ANY_RE.test(q)) return "watch";
    return null;
  }

  function gameRow(o, newTab) {
    const a = document.createElement("a");
    a.className = "serp-game-row";
    a.href = o.url || "#";
    if (newTab) { a.target = "_blank"; a.rel = "noopener noreferrer"; } else a.rel = "noopener";
    const cut = (o.cut && o.cut > 0) ? '<span class="serp-game-cut">-' + esc(o.cut) + "%</span>" : "";
    const reg = (o.regular && o.cut && o.cut > 0)
      ? '<span class="serp-game-regular">' + esc(fmtPrice(o.regular, o.currency)) + "</span>" : "";
    a.innerHTML =
      '<span class="serp-game-store"></span>' +
      '<span class="serp-game-right">' + cut + reg +
        '<span class="serp-game-price">' + esc(o.priceText || "") + "</span>" +
      "</span>";
    a.querySelector(".serp-game-store").textContent = o.store;
    return a;
  }

  function gameSection(label, offers, newTab) {
    if (!offers || !offers.length) return null;
    const sec = document.createElement("div");
    sec.className = "serp-game-section";
    const lab = document.createElement("div");
    lab.className = "serp-game-label";
    lab.textContent = label;
    sec.appendChild(lab);
    const list = document.createElement("div");
    list.className = "serp-game-list";
    offers.forEach((o) => list.appendChild(gameRow(o, newTab)));
    sec.appendChild(list);
    return sec;
  }

  function fmtPrice(value, currency) {
    const SYM = { GBP: "£", USD: "$", EUR: "€", JPY: "¥", AUD: "A$", CAD: "C$",
      NZD: "NZ$", INR: "₹", BRL: "R$", PLN: "zł ", SEK: "kr ", NOK: "kr ", DKK: "kr " };
    const sym = SYM[(currency || "").toUpperCase()] || "";
    const amt = Number(value).toFixed(2);
    return sym ? sym + amt : amt + " " + (currency || "");
  }

  function renderGameOffers(box, d, newTab) {
    const head = box.querySelector(".serp-src-head");
    const bits = [];
    if (typeof d.metacritic === "number") bits.push("Metacritic " + esc(d.metacritic));
    if (d.historicalLow && d.historicalLow.priceText)
      bits.push("lowest ever " + esc(d.historicalLow.priceText));
    const sub = bits.length ? ' <span class="serp-game-sub">· ' + bits.join(" · ") + "</span>" : "";
    const titleHtml = ICON.game + "<span>" + _("Best PC price") + " · </span>";
    if (d.moreUrl) {
      const a = document.createElement("a");
      a.className = "serp-game-head-title";
      a.href = d.moreUrl;
      if (newTab) { a.target = "_blank"; a.rel = "noopener noreferrer"; } else a.rel = "noopener";
      a.innerHTML = esc(d.title) + sub;
      head.innerHTML = titleHtml;
      head.appendChild(a);
    } else {
      head.innerHTML = titleHtml + "<span>" + esc(d.title) + sub + "</span>";
    }

    const body = box.querySelector(".serp-game-body");
    body.innerHTML = "";
    const official = gameSection(_("Official stores"), d.official, newTab);
    if (official) body.appendChild(official);
    box.style.display = body.children.length ? "" : "none";
  }

  // Insert (once) a "Best PC price" box and fetch its offers. One-shot per page.
  let _gameAttempted = false;
  function ensureGameBox(wrap, newTab) {
    if (_gameAttempted || document.getElementById("serp-game")) return;
    const query = currentQuery();
    if (!query) return;
    _gameAttempted = true;

    const box = document.createElement("div");
    box.id = "serp-game";
    box.className = "serp-src-group serp-src-game";
    box.style.display = "none";
    box.innerHTML =
      '<div class="serp-src-head">' + ICON.game + "<span>" + _("Best PC price") + "</span></div>" +
      '<div class="serp-game-body"><div class="serp-game-msg">' + _("Loading…") + "</div></div>";
    wrap.insertBefore(box, wrap.firstChild);  // top of the sources column

    function drop() {
      box.remove();
      if (wrap && !wrap.querySelector(".serp-src-group")) wrap.remove();
    }

    fetch("/game_offers?q=" + encodeURIComponent(query),
          { headers: { Accept: "application/json" } })
      .then((r) => r.json())
      .then((d) => {
        if (!d || d.type !== "game" || !(d.official && d.official.length)) {
          drop();
          return;
        }
        renderGameOffers(box, d, newTab);
      })
      .catch(drop);
  }

  // ── "Maps" box (local / place-finder queries) ─────────────────────────────
  // A pure deep-link card: no API call, no token. A local-intent query ("X near
  // me", "directions to Y", "theme park nearby") hands off to Apple Maps, which
  // supplies the live map, ratings and turn-by-turn directions from the user's
  // own location. maps.apple.com renders in any browser and opens the Maps app
  // on Apple devices.

  // Strong locality cues — any one of these alone triggers the box.
  const MAPS_CUE_RE = /\bnear\s*(?:me|by|here)\b|\bnearby\b|\baround\s+me\b|\bclose\s+(?:to\s+me|by)\b|\b(?:closest|nearest)\b|\bin\s+my\s+area\b|\bwalking\s+distance\b|\bdirections?\s+(?:to|from)\b|\bget\s+directions\b|\bhow\s+(?:do\s+i|to)\s+get\s+to\b|\bmap\s+of\b|\bon\s+the\s+map\b|\bwhere\s+is\b/i;

  // Place-type nouns — only count when paired with a "near/around/closest" cue,
  // so "italian restaurant near me" lights up but "restaurant pos software" or
  // "italian recipes" don't.
  const PLACE_TYPE_RE = /\b(?:restaurants?|caf[eé]s?|coffee\s+shops?|bars?|pubs?|hotels?|motels?|hostels?|gas\s+stations?|petrol\s+stations?|pharmac(?:y|ies)|hospitals?|clinics?|atms?|banks?|supermarkets?|grocer(?:y|ies)|theme\s+parks?|amusement\s+parks?|water\s+parks?|zoos?|aquariums?|museums?|gyms?|cinemas?|movie\s+theat(?:er|re)s?|airports?|train\s+stations?|bus\s+stations?|parking|mechanics?|dentists?|doctors?|vets?|veterinarians?|salons?|barbers?|plumbers?|electricians?|stores?|shops?|malls?|markets?|baker(?:y|ies)|takeaway|takeout|diner)\b/i;

  const NEAR_PREP_RE = /\b(?:near|nearby|around|close|closest|nearest)\b/i;

  function mapsIntent(q) {
    if (!q) return false;
    if (MAPS_CUE_RE.test(q)) return true;
    if (PLACE_TYPE_RE.test(q) && NEAR_PREP_RE.test(q)) return true;
    return false;
  }

  // Strip SearXNG search operators before handing the query to Apple Maps —
  // otherwise the "More results from this site" link (which appends "site:host")
  // and any leading !bang leak verbatim into the maps deep link.
  function cleanMapsQuery(q) {
    return (q || "")
      .replace(/\bsite:\S+/gi, " ")        // "… site:example.com" scoping
      .replace(/(^|\s)![^\s]+/g, " ")       // !bangs (!images, !!g, …)
      .replace(/\s+/g, " ")
      .trim();
  }

  // Insert (once) a Maps box at the top of the sources wrap. Synchronous — the
  // deep links are built from the query, so there's nothing to fetch or drop.
  let _mapsAttempted = false;
  function ensureMapsBox(wrap, newTab) {
    if (_mapsAttempted || document.getElementById("serp-maps")) return;
    const rawQuery = currentQuery();
    if (!rawQuery) return;
    _mapsAttempted = true;

    const query = cleanMapsQuery(rawQuery) || rawQuery;
    const enc = encodeURIComponent(query);
    const searchUrl = "https://maps.apple.com/?q=" + enc;
    const dirUrl = "https://maps.apple.com/?daddr=" + enc + "&dirflg=d";

    const box = document.createElement("div");
    box.id = "serp-maps";
    box.className = "serp-src-group serp-src-maps";
    box.innerHTML =
      '<div class="serp-src-head">' + ICON.maps + "<span>" + _("Maps") + " · </span></div>" +
      '<div class="serp-maps-actions"></div>';

    function link(el, href) {
      el.href = href;
      if (newTab) { el.target = "_blank"; el.rel = "noopener noreferrer"; } else el.rel = "noopener";
    }

    // The searched place itself links through to the Apple Maps results.
    const head = box.querySelector(".serp-src-head");
    const titleA = document.createElement("a");
    titleA.className = "serp-maps-head-title";
    titleA.textContent = query;
    link(titleA, searchUrl);
    head.appendChild(titleA);

    const actions = box.querySelector(".serp-maps-actions");
    function btn(label, href, icon, primary) {
      const a = document.createElement("a");
      a.className = "serp-maps-btn" + (primary ? " primary" : "");
      a.innerHTML = icon + "<span>" + esc(label) + "</span>";
      link(a, href);
      return a;
    }
    actions.appendChild(btn(_("Open in Apple Maps"), searchUrl, ICON.maps, true));
    actions.appendChild(btn(_("Directions"), dirUrl, ICON.directions, false));

    wrap.insertBefore(box, wrap.firstChild);  // top of the sources column
  }

  // ── Currency converter card (currency-conversion queries) ──────────────────
  // Detection vocabulary kept in sync with ai_summary.js (which suppresses its
  // box for these queries) and the server-side currency_box.py resolver.
  const FX_SYMBOLS = "$£€¥₹₩₽₺₪₫฿₴₦";
  const FX_WORDS = new Set([
    "usd","eur","gbp","jpy","aud","cad","chf","cny","hkd","nzd","sgd","inr",
    "krw","mxn","brl","zar","rub","try","sek","nok","dkk","pln","thb","idr",
    "huf","czk","ils","aed","sar","php","myr","ron","rmb",
    "dollar","dollars","buck","bucks","pound","pounds","quid","sterling",
    "euro","euros","yen","rupee","rupees","won","yuan","renminbi","franc",
    "francs","peso","pesos","real","reais","rand","ruble","rubles","rouble",
    "lira","ringgit","baht","shekel","dirham","riyal","zloty","krona","krone",
  ]);

  function fxSide(side) {
    side = (side || "").toLowerCase().replace(/[0-9.,]+/g, "").trim();
    for (const s of FX_SYMBOLS) if (side.indexOf(s) !== -1) return true;
    side = side.replace(new RegExp("[" + FX_SYMBOLS.replace(/[$]/g, "\\$") + "]", "g"), "").trim();
    if (!side) return false;
    if (FX_WORDS.has(side)) return true;
    return side.split(/\s+/).some((w) => FX_WORDS.has(w));
  }

  function isCurrencyQuery(q) {
    if (!q) return false;
    const m = q.match(/^\s*(?:convert\s+)?(.+?)\s+(?:into|in|to|=|->|→)\s+(.+?)\s*$/i);
    if (!m) return false;
    return fxSide(m[1]) && fxSide(m[2]);
  }

  function fxFmt(n) {
    n = Number(n) || 0;
    const a = Math.abs(n);
    const max = a >= 1 ? 2 : a >= 0.01 ? 4 : 6;
    return n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: Math.max(2, max) });
  }

  function fxShortDate(iso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || "");
    return m ? m[3] + "/" + m[2] : "";
  }

  // Hand-rolled SVG area+line chart (dependency-free), styled like the screenshot:
  // gridlines + right-hand value axis + a few date ticks, coloured by trend.
  function fxChartSVG(series) {
    if (!series || series.length < 2) return null;
    const W = 320, H = 150, padL = 6, padR = 40, padT = 10, padB = 20;
    const x0 = padL, x1 = W - padR, y0 = padT, y1 = H - padB;
    const vals = series.map((p) => p.v);
    let lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    if (hi === lo) { hi += hi * 0.001 || 0.001; lo -= lo * 0.001 || 0.001; }
    const pad = (hi - lo) * 0.12;
    lo -= pad; hi += pad;
    const n = series.length;
    const X = (i) => x0 + (x1 - x0) * (i / (n - 1));
    const Y = (v) => y1 - (y1 - y0) * ((v - lo) / (hi - lo));

    const up = vals[n - 1] >= vals[0];
    const color = vals[n - 1] === vals[0] ? "#4285f4" : up ? "#1a9c4a" : "#d23f31";
    const gid = "fxg" + Math.random().toString(36).slice(2, 8);

    let line = "M" + X(0).toFixed(1) + " " + Y(vals[0]).toFixed(1);
    for (let i = 1; i < n; i++) line += " L" + X(i).toFixed(1) + " " + Y(vals[i]).toFixed(1);
    const area = line + " L" + x1.toFixed(1) + " " + y1.toFixed(1) +
                 " L" + x0.toFixed(1) + " " + y1.toFixed(1) + " Z";

    // 3 horizontal gridlines (lo / mid / hi) with right-hand value labels.
    let grid = "";
    for (let k = 0; k <= 2; k++) {
      const v = lo + (hi - lo) * (k / 2);
      const y = Y(v).toFixed(1);
      grid += '<line class="serp-fx-gridline" x1="' + x0 + '" y1="' + y +
              '" x2="' + x1 + '" y2="' + y + '"/>' +
              '<text class="serp-fx-axis" x="' + (x1 + 4) + '" y="' + (Number(y) + 3) +
              '">' + esc(fxFmt(v)) + "</text>";
    }
    // Date ticks: first / middle / last.
    let ticks = "";
    [[0, "start"], [Math.floor((n - 1) / 2), "middle"], [n - 1, "end"]].forEach(([i, anchor]) => {
      const lbl = fxShortDate(series[i].d);
      if (lbl) ticks += '<text class="serp-fx-axis" x="' + X(i).toFixed(1) + '" y="' + (H - 6) +
                        '" text-anchor="' + anchor + '">' + esc(lbl) + "</text>";
    });

    return '<svg viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="exchange rate chart" preserveAspectRatio="none">' +
      '<defs><linearGradient id="' + gid + '" x1="0" y1="0" x2="0" y2="1">' +
        '<stop offset="0" stop-color="' + color + '" stop-opacity="0.22"/>' +
        '<stop offset="1" stop-color="' + color + '" stop-opacity="0"/>' +
      "</linearGradient></defs>" +
      grid +
      '<path d="' + area + '" fill="url(#' + gid + ')" stroke="none"/>' +
      '<path d="' + line + '" fill="none" stroke="' + color + '" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/>' +
      ticks +
      "</svg>";
  }

  // Build (and re-build, on selector change) the converter card body.
  function renderCurrency(box, d, newQuery) {
    const grid = document.createElement("div");
    grid.className = "serp-fx-grid";

    // Left: equals line, big result, editable amount + currency selectors.
    const left = document.createElement("div");
    const opts = (d.currencies || []).map((c) =>
      '<option value="' + esc(c[0]) + '">' + esc(c[0] + " — " + c[1]) + "</option>").join("");
    left.innerHTML =
      '<div class="serp-fx-equals"></div>' +
      '<div class="serp-fx-result"><span class="serp-fx-amt"></span> <span class="serp-fx-cur"></span></div>' +
      '<div class="serp-fx-fields">' +
        '<div class="serp-fx-row">' +
          '<input class="serp-fx-input serp-fx-from-amt" type="number" inputmode="decimal" min="0" step="any" aria-label="amount">' +
          '<select class="serp-fx-select serp-fx-from-cur" aria-label="from currency">' + opts + "</select>" +
        "</div>" +
        '<div class="serp-fx-row">' +
          '<input class="serp-fx-input serp-fx-to-amt" type="number" inputmode="decimal" min="0" step="any" aria-label="converted amount">' +
          '<select class="serp-fx-select serp-fx-to-cur" aria-label="to currency">' + opts + "</select>" +
        "</div>" +
      "</div>";

    // Right: chart (or a graceful note when the ECB feed lacks the pair).
    const right = document.createElement("div");
    right.className = "serp-fx-chart";
    const svg = fxChartSVG(d.series);
    if (svg) {
      right.innerHTML = svg +
        '<div class="serp-fx-rateline">1 ' + esc(d.from) + " = " + esc(fxFmt(d.rate)) + " " + esc(d.to) + "</div>";
    } else {
      right.innerHTML = '<div class="serp-fx-chart-empty">' +
        esc("1 " + d.from + " = " + fxFmt(d.rate) + " " + d.to + " · chart unavailable for this pair") + "</div>";
    }

    grid.appendChild(left);
    grid.appendChild(right);
    box.innerHTML = "";
    box.appendChild(grid);

    const equals = left.querySelector(".serp-fx-equals");
    const bigAmt = left.querySelector(".serp-fx-amt");
    const bigCur = left.querySelector(".serp-fx-cur");
    const fromAmt = left.querySelector(".serp-fx-from-amt");
    const toAmt = left.querySelector(".serp-fx-to-amt");
    const fromCur = left.querySelector(".serp-fx-from-cur");
    const toCur = left.querySelector(".serp-fx-to-cur");
    fromCur.value = d.from;
    toCur.value = d.to;

    function paint(amount) {
      const result = amount * d.rate;
      equals.textContent = fxFmt(amount) + " " + d.fromName + " equals";
      bigAmt.textContent = fxFmt(result);
      bigCur.textContent = d.toName;
      toAmt.value = Number(result.toFixed(4));
    }
    fromAmt.value = d.amount;
    paint(d.amount);

    fromAmt.addEventListener("input", () => {
      const a = parseFloat(fromAmt.value);
      paint(isFinite(a) && a >= 0 ? a : 0);
    });
    toAmt.addEventListener("input", () => {
      const r = parseFloat(toAmt.value);
      const a = isFinite(r) && r >= 0 && d.rate ? r / d.rate : 0;
      fromAmt.value = Number(a.toFixed(4));
      equals.textContent = fxFmt(a) + " " + d.fromName + " equals";
      bigAmt.textContent = fxFmt(r >= 0 ? r : 0);
      bigCur.textContent = d.toName;
    });
    function reconvert() {
      const a = parseFloat(fromAmt.value);
      newQuery((isFinite(a) && a > 0 ? a : 1) + " " + fromCur.value + " to " + toCur.value);
    }
    fromCur.addEventListener("change", reconvert);
    toCur.addEventListener("change", reconvert);
  }

  // Suppress the AI-summary box and the bundled engine's text answer — the card
  // is the answer for a currency query.
  function fxHideDuplicates() {
    const ai = document.getElementById("ai-summary-wrapper");
    if (ai) ai.remove();
    const answers = document.getElementById("answers");
    if (answers) answers.style.display = "none";
  }

  // Insert (once) the converter card at the top of the results column and fetch
  // the conversion. One-shot per page; re-fetches on selector change.
  let _currencyAttempted = false;
  function ensureCurrencyBox() {
    if (_currencyAttempted || document.getElementById("serp-currency")) return;
    const results = document.getElementById("results");
    if (!results) return;
    const query = currentQuery();
    if (!query || !isCurrencyQuery(query)) return;
    _currencyAttempted = true;

    const box = document.createElement("div");
    box.id = "serp-currency";
    box.style.display = "none";
    results.insertBefore(box, results.firstChild);

    function load(q, isInitial) {
      fetch("/currency_convert?q=" + encodeURIComponent(q),
            { headers: { Accept: "application/json" } })
        .then((r) => r.json())
        .then((d) => {
          if (!d || d.type !== "currency") {
            // Not a currency query after all — drop the card. On the initial
            // load the AI summary was suppressed up-front by ai_summary.js; this
            // only happens for contrived inputs, so leaving no box is acceptable.
            if (isInitial) box.remove();
            return;
          }
          fxHideDuplicates();
          renderCurrency(box, d, (nq) => load(nq, false));
          box.style.display = "";
        })
        .catch(() => { if (isInitial) box.remove(); });
    }
    load(query, true);
  }

  // Minimal gettext shim — the SERP is rendered server-side already, so these
  // strings stay English unless a translation hook is added later.
  function _(s) { return s; }

  const TOP_MAX = 6;
  function buildTopSection() {
    const results = document.getElementById("results");
    if (!results) return;

    const newTab = !!document.querySelector(
      '.result h3 a[target="_blank"], .result a.url_header[target="_blank"]');
    // An explicit query qualifier wins and is exclusive ("sonic game" shows only
    // the game box, "sonic movie" only the watch box, even if the other type's
    // hosts appear in the results). With no qualifier, fall back to result-host
    // detection, which may light up both boxes for a dual game/show franchise.
    const intent = queryIntent();
    const wantWatch = intent ? intent === "watch" : hasEntHost();
    const wantGame = intent ? intent === "game" : hasGameHost();
    const wantMaps = mapsIntent(currentQuery());

    let wrap = document.getElementById("serp-sources");
    if (!wrap) {
      const items = collectMatches();
      const reddit = items.filter((it) => it.kind === "reddit");
      const github = items.filter((it) => it.kind === "github");
      if (!reddit.length && !github.length && !wantWatch && !wantGame && !wantMaps) return;

      wrap = document.createElement("div");
      wrap.id = "serp-sources";
      // Reddit first (the headline "discussion" card), then GitHub.
      if (reddit.length)
        wrap.appendChild(buildGroup("Reddit", ICON.reddit, "reddit", reddit.slice(0, TOP_MAX), newTab));
      if (github.length)
        wrap.appendChild(buildGroup("GitHub", ICON.github, "github", github.slice(0, TOP_MAX), newTab));

      // Sits at the very top of the results column, just under the AI summary.
      // #results is a named-area grid; a stray child with no grid-area gets
      // auto-placed into the first free top cell or overflows to an implicit
      // row at the bottom (which happens when an answer/correction already
      // occupies the top cells). Inserting inside #urls — which owns the
      // "urls" grid-area — keeps the wrap pinned to the top of the result list.
      const urls = document.getElementById("urls");
      const ai = document.getElementById("ai-summary-wrapper");
      if (urls) urls.insertBefore(wrap, urls.firstChild);
      else if (ai && ai.parentNode === results) ai.after(wrap);
      else results.insertBefore(wrap, results.firstChild);
    }

    // Offer boxes sit above the Reddit/GitHub groups (cheapest-first). The game
    // box is inserted first, then the watch box, so on the rare query that is
    // both, "where to watch" ends up on top.
    if (wantGame) ensureGameBox(wrap, newTab);
    if (wantWatch) ensureWatchBox(wrap, newTab);
    // Maps box last → it lands at the very top for local "near me" queries.
    if (wantMaps) ensureMapsBox(wrap, newTab);
  }

  // ── Site shield: per-result priority + info panel ──────────────────────────
  // The shield on each result opens a panel to (a) adjust how that domain ranks
  // — Block / Lower / Normal / Raise / Pin — and (b) see site info (connection,
  // popularity, registration). Per-user adjustments live in localStorage and are
  // applied client-side by reordering/hiding results; an admin can additionally
  // push an adjustment globally (server-side) from the same control.

  const PRIO_KEY = "usearch.sitePriority";
  const ADMIN_KEY = "usearch.adminToken";
  const PRIO_LEVELS = ["block", "lower", "normal", "raise", "pin"];
  const PRIO_LABEL = { block: "Block", lower: "Lower", normal: "Normal", raise: "Raise", pin: "Pin" };
  const PRIO_RANK = { pin: 0, raise: 1, normal: 2, lower: 3, block: 4 };

  function loadPrio() {
    try {
      const raw = JSON.parse(localStorage.getItem(PRIO_KEY) || "{}");
      if (raw && typeof raw === "object") return raw;
    } catch (_) { /* ignore */ }
    return {};
  }
  function savePrio(map) {
    try { localStorage.setItem(PRIO_KEY, JSON.stringify(map)); } catch (_) { /* ignore */ }
  }
  // Match a host against the store, walking subdomains off the front so a rule on
  // "medium.com" also covers "blog.medium.com" (mirrors the server lookup).
  function getLevel(host) {
    if (!host) return "normal";
    const map = loadPrio();
    const labels = host.split(".");
    for (let i = 0; i < labels.length - 1; i++) {
      const lvl = map[labels.slice(i).join(".")];
      if (lvl && PRIO_LEVELS.indexOf(lvl) !== -1) return lvl;
    }
    return "normal";
  }
  function setLevel(host, level) {
    if (!host) return;
    const map = loadPrio();
    if (level === "normal") delete map[host];
    else map[host] = level;
    savePrio(map);
    applyPriorities();
  }

  function getAdminToken() {
    try { return localStorage.getItem(ADMIN_KEY) || ""; } catch (_) { return ""; }
  }
  function setAdminToken(t) {
    try { if (t) localStorage.setItem(ADMIN_KEY, t); else localStorage.removeItem(ADMIN_KEY); } catch (_) { /* ignore */ }
  }

  // Reflect a result's current level: collapse blocked ones to a reversible
  // stub, dim lowered ones, and tint that result's shield button. Idempotent.
  function styleResult(result, host, level) {
    result.dataset.serpLevel = level;
    result.classList.toggle("serp-lowered", level === "lower");
    const blocked = level === "block";
    result.classList.toggle("serp-blocked", blocked);
    let note = result.querySelector(":scope > .serp-blocked-note");
    if (blocked && !note) {
      note = document.createElement("div");
      note.className = "serp-blocked-note";
      note.innerHTML = "<span></span><button type=\"button\">" + _("Unblock") + "</button>";
      note.querySelector("span").textContent = host + " " + _("blocked");
      note.querySelector("button").addEventListener("click", (e) => {
        e.preventDefault(); e.stopPropagation(); setLevel(host, "normal");
      });
      result.appendChild(note);
    } else if (!blocked && note) {
      note.remove();
    }
    const btn = result.querySelector(":scope .serp-shield-btn");
    if (btn) {
      btn.className = "serp-shield-btn" + (level !== "normal" ? " lvl-" + level : "");
      btn.title = level === "normal" ? _("Adjust this site") : (PRIO_LABEL[level] + " · " + host);
    }
  }

  // Apply all per-user priorities: tag every result, then reorder #urls so
  // pinned float up and lowered/blocked sink. Only writes the DOM when the order
  // actually changes, so the mutation observer doesn't loop.
  function applyPriorities() {
    const urls = document.getElementById("urls");
    if (!urls) return;
    const results = Array.prototype.filter.call(
      urls.children, (el) => el.classList && el.classList.contains("result"));
    if (!results.length) return;

    results.forEach((r) => {
      const host = hostnameOf(resultUrl(r));
      // Manual per-site rules win; an active "favor" lens fills in for sites the
      // user hasn't touched, raising them to the top.
      let level = getLevel(host);
      if (level === "normal" && lensFavorHost(host)) level = "raise";
      styleResult(r, host, level);
    });

    const desired = results
      .map((r, i) => ({ r, i, rank: PRIO_RANK[r.dataset.serpLevel] != null ? PRIO_RANK[r.dataset.serpLevel] : 2 }))
      .sort((a, b) => (a.rank - b.rank) || (a.i - b.i))
      .map((o) => o.r);

    const changed = desired.some((r, i) => r !== results[i]);
    if (changed) {
      const frag = document.createDocumentFragment();
      desired.forEach((r) => frag.appendChild(r));
      urls.appendChild(frag);
    }

    updateStatusBlocked();
  }

  // ── Top status line: search time + "blocked by your preferences" ─────────────
  // A small line just under the language/filters row: how long the search took
  // (from the browser's navigation timing) and how many results on this page are
  // hidden by the per-site shield. The count reflects the live localStorage
  // priorities, so it updates whenever a site is blocked or unblocked.

  function searchTimeText() {
    let ms = null;
    try {
      const nav = performance.getEntriesByType && performance.getEntriesByType("navigation")[0];
      if (nav && nav.responseEnd > 0 && nav.requestStart >= 0) ms = nav.responseEnd - nav.requestStart;
      if ((ms == null || ms <= 0) && performance.timing) {
        const t = performance.timing;
        if (t.responseEnd && t.requestStart) ms = t.responseEnd - t.requestStart;
      }
    } catch (_) { /* ignore */ }
    if (ms == null || !isFinite(ms) || ms <= 0) return "";
    return (ms / 1000).toFixed(2) + " " + _("seconds");
  }

  function blockedResults() {
    const urls = document.getElementById("urls");
    if (!urls) return [];
    return Array.prototype.slice.call(urls.querySelectorAll(".result.serp-blocked"));
  }

  function buildStatusBar() {
    const results = document.getElementById("results");
    if (!results || !results.parentNode) return;
    if (!document.querySelector("#urls .result")) return;  // only on a results page

    if (!document.getElementById("serp-statusbar")) {
      const bar = document.createElement("div");
      bar.id = "serp-statusbar";
      bar.className = "serp-statusbar";
      const t = searchTimeText();
      bar.innerHTML =
        (t ? '<span class="serp-status-time">' + ICON.clock + "<span>" +
              esc(_("Search completed in") + " " + t) + "</span></span>" +
             '<span class="serp-status-sep">·</span>' : "") +
        '<button type="button" class="serp-status-blocked"></button>';
      results.parentNode.insertBefore(bar, results);

      bar.querySelector(".serp-status-blocked").addEventListener("click", (e) => {
        e.preventDefault();
        if (!e.currentTarget.classList.contains("has-blocked")) return;
        openBlockedPanel();
      });
    }
    updateStatusBlocked();
  }

  function updateStatusBlocked() {
    const bar = document.getElementById("serp-statusbar");
    if (!bar) return;
    const blocked = blockedResults();
    // Skip the rebuild when nothing changed (the observer fires often).
    const sig = blocked.map((r) => hostnameOf(resultUrl(r)) || "?").join("|");
    if (bar.dataset.blockedSig === sig) return;
    bar.dataset.blockedSig = sig;

    const toggle = bar.querySelector(".serp-status-blocked");
    const n = blocked.length;
    const has = n > 0;

    toggle.classList.toggle("has-blocked", has);
    toggle.innerHTML = ICON.shield + "<span>" +
      esc(n + " " + (n === 1
        ? _("query blocked due to your preferences")
        : _("queries blocked due to your preferences"))) + "</span>" +
      (has ? ' <span class="serp-status-view">' + esc(_("click to view")) + "</span>" : "");
    toggle.title = has ? _("Show the results hidden by your site preferences") : "";
  }

  // ── The panel ───────────────────────────────────────────────────────────────

  let _panelEsc = null;
  function closePanel() {
    const back = document.getElementById("serp-modal-backdrop");
    if (back) back.remove();
    if (_panelEsc) { document.removeEventListener("keydown", _panelEsc); _panelEsc = null; }
  }

  function infoRow(icon, key, valueHtml, muted) {
    return '<li><span class="serp-info-ic">' + icon + "</span>" +
      '<span class="serp-info-k">' + esc(key) + "</span>" +
      '<span class="serp-info-v' + (muted ? " muted" : "") + '">' + valueHtml + "</span></li>";
  }

  function fmtRegDate(iso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || "");
    if (!m) return "";
    const months = ["January", "February", "March", "April", "May", "June", "July",
      "August", "September", "October", "November", "December"];
    const mon = months[parseInt(m[2], 10) - 1] || "";
    return mon + " " + parseInt(m[3], 10) + ", " + m[1];
  }

  function openPanel(opts) {
    closePanel();
    const host = opts.host;
    const url = opts.url;

    const back = document.createElement("div");
    back.id = "serp-modal-backdrop";
    back.className = "serp-modal-backdrop";

    const favHtml = opts.favSrc
      ? '<img class="serp-modal-fav" src="' + esc(opts.favSrc) + '" alt="">'
      : '<span class="serp-modal-fav"></span>';

    const seg = PRIO_LEVELS.map((lv) =>
      '<button type="button" data-lvl="' + lv + '" class="act-' + lv + '">' +
      esc(PRIO_LABEL[lv]) + "</button>").join("");

    back.innerHTML =
      '<div class="serp-modal" role="dialog" aria-modal="true">' +
        '<div class="serp-modal-head">' + favHtml +
          '<div class="serp-modal-titles">' +
            '<p class="serp-modal-title"></p>' +
            '<div class="serp-modal-host"></div>' +
          "</div>" +
          '<button type="button" class="serp-modal-close" aria-label="Close">' + ICON.close + "</button>" +
        "</div>" +
        '<div class="serp-modal-body">' +
          '<div class="serp-rank-label">' +
            "<span>" + _("Ranking adjustment for") + " " + esc(host) + "</span>" +
            '<span class="serp-rank-info" title="' +
              esc(_("Block hides this site, Lower/Raise nudge it down/up, Pin floats it to the top. Saved in this browser.")) +
            '">' + ICON.info + "</span>" +
          "</div>" +
          '<div class="serp-seg">' + seg + "</div>" +
          '<div class="serp-global" style="display:none">' +
            "<span>" + _("Global") + ": <b class=\"serp-global-cur\">normal</b></span>" +
            '<button type="button" class="serp-global-btn">' + _("Apply globally") + "</button>" +
            '<div class="serp-global-msg"></div>' +
          "</div>" +
          '<ul class="serp-info">' +
            infoRow(ICON.lock, _("Connection"), '<span class="serp-info-conn">…</span>', true) +
            infoRow(ICON.starline, _("Popularity"), '<span class="serp-info-pop">…</span>', true) +
            infoRow(ICON.globe, _("Domain registered"), '<span class="serp-info-reg">…</span>', true) +
            infoRow(ICON.user, _("Owned by"), '<span class="serp-info-own">…</span>', true) +
          "</ul>" +
        "</div>" +
      "</div>";

    back.querySelector(".serp-modal-title").textContent = opts.title || host;
    back.querySelector(".serp-modal-host").textContent = host;

    document.body.appendChild(back);

    // Close interactions.
    back.addEventListener("click", (e) => { if (e.target === back) closePanel(); });
    back.querySelector(".serp-modal-close").addEventListener("click", closePanel);
    _panelEsc = (e) => { if (e.key === "Escape") closePanel(); };
    document.addEventListener("keydown", _panelEsc);

    // Segmented control.
    const segEl = back.querySelector(".serp-seg");
    function paintSeg() {
      const cur = getLevel(host);
      segEl.querySelectorAll("button").forEach((b) =>
        b.classList.toggle("active", b.dataset.lvl === cur));
    }
    paintSeg();
    segEl.querySelectorAll("button").forEach((b) => {
      b.addEventListener("click", () => {
        setLevel(host, b.dataset.lvl);
        paintSeg();
        // Setting a level is a one-shot action; the modal otherwise covers the
        // whole page, hiding the result collapsing and the top "blocked" count
        // from updating. Close it so the change is immediately visible.
        closePanel();
      });
    });

    // Global control + site info (one fetch).
    const globalWrap = back.querySelector(".serp-global");
    const globalCur = back.querySelector(".serp-global-cur");
    const globalBtn = back.querySelector(".serp-global-btn");
    const globalMsg = back.querySelector(".serp-global-msg");
    const conn = back.querySelector(".serp-info-conn");
    const pop = back.querySelector(".serp-info-pop");
    const reg = back.querySelector(".serp-info-reg");
    const own = back.querySelector(".serp-info-own");

    globalBtn.addEventListener("click", () => {
      let token = getAdminToken();
      if (!token) {
        token = (window.prompt(_("Admin token to apply this globally:")) || "").trim();
        if (!token) return;
        setAdminToken(token);
      }
      const level = getLevel(host);
      globalBtn.disabled = true;
      globalMsg.className = "serp-global-msg";
      globalMsg.textContent = _("Saving…");
      fetch("/site_priority", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Admin-Token": token },
        body: JSON.stringify({ host: host, level: level }),
      })
        .then((r) => r.json().then((d) => ({ ok: r.ok, d })))
        .then(({ ok, d }) => {
          globalBtn.disabled = false;
          if (ok && d && d.ok) {
            globalCur.textContent = level;
            globalMsg.className = "serp-global-msg ok";
            globalMsg.textContent = _("Applied for everyone.");
          } else {
            if (d && (d.error === "forbidden" || d.error === "disabled")) setAdminToken("");
            globalMsg.className = "serp-global-msg err";
            globalMsg.textContent = d && d.error === "forbidden"
              ? _("Wrong admin token.") : _("Couldn't apply globally.");
          }
        })
        .catch(() => {
          globalBtn.disabled = false;
          globalMsg.className = "serp-global-msg err";
          globalMsg.textContent = _("Couldn't apply globally.");
        });
    });

    fetch("/site_info?url=" + encodeURIComponent(url), { headers: { Accept: "application/json" } })
      .then((r) => r.json())
      .then((d) => {
        if (!d || d.type !== "site") { conn.textContent = pop.textContent = reg.textContent = own.textContent = "—"; return; }
        conn.parentElement.classList.remove("muted");
        conn.innerHTML = d.secure
          ? _("This site uses") + ' <span class="ok">' + esc(d.scheme) + "</span>"
          : esc((d.scheme || "http") + " (not secure)");
        pop.textContent = (typeof d.popularity_rank === "number")
          ? "#" + d.popularity_rank + " " + _("by traffic") : _("Not in top sites");
        reg.textContent = fmtRegDate(d.registered) || _("Unknown");
        own.textContent = d.owner || _("Private / redacted");
        if (d.admin_enabled) {
          globalWrap.style.display = "";
          globalCur.textContent = d.global_priority || "normal";
        }
      })
      .catch(() => { conn.textContent = pop.textContent = reg.textContent = own.textContent = "—"; });
  }

  // The "N queries blocked" status opens this: the same modal card as the shield
  // panel, but just a scrollable list of the sites hidden on this page with an
  // Unblock action — no per-site connection/stats. Reuses closePanel/backdrop.
  function openBlockedPanel() {
    closePanel();
    const blocked = blockedResults();
    if (!blocked.length) return;

    const back = document.createElement("div");
    back.id = "serp-modal-backdrop";
    back.className = "serp-modal-backdrop";
    back.innerHTML =
      '<div class="serp-modal" role="dialog" aria-modal="true">' +
        '<div class="serp-modal-head">' +
          '<span class="serp-modal-fav serp-blk-ic">' + ICON.shield + "</span>" +
          '<div class="serp-modal-titles">' +
            '<p class="serp-modal-title">' + esc(_("Blocked sites")) + "</p>" +
            '<div class="serp-modal-host"></div>' +
          "</div>" +
          '<button type="button" class="serp-modal-close" aria-label="Close">' + ICON.close + "</button>" +
        "</div>" +
        '<div class="serp-modal-body serp-blk-body">' +
          '<div class="serp-blk-list"></div>' +
        "</div>" +
      "</div>";

    const sub = back.querySelector(".serp-modal-host");
    const list = back.querySelector(".serp-blk-list");
    function setSub(count) {
      sub.textContent = count + " " + (count === 1
        ? _("result hidden on this page") : _("results hidden on this page"));
    }
    setSub(blocked.length);

    blocked.forEach((r) => {
      const host = hostnameOf(resultUrl(r)) || "";
      const h3 = r.querySelector("h3");
      const title = h3 ? h3.textContent.trim() : "";
      const row = document.createElement("div");
      row.className = "serp-blk-row";
      row.innerHTML =
        '<span class="serp-status-host">' + esc(host) +
          (title && title !== host ? ' <span class="serp-status-ttl">— ' + esc(title) + "</span>" : "") +
        '</span><button type="button">' + esc(_("Unblock")) + "</button>";
      row.querySelector("button").addEventListener("click", (e) => {
        e.preventDefault();
        setLevel(host, "normal");
        row.remove();
        const left = list.querySelectorAll(".serp-blk-row").length;
        if (!left) { closePanel(); return; }
        setSub(left);
      });
      list.appendChild(row);
    });

    document.body.appendChild(back);
    back.addEventListener("click", (e) => { if (e.target === back) closePanel(); });
    back.querySelector(".serp-modal-close").addEventListener("click", closePanel);
    _panelEsc = (e) => { if (e.key === "Escape") closePanel(); };
    document.addEventListener("keydown", _panelEsc);
  }

  function enhanceShields(root) {
    (root || document).querySelectorAll(".result").forEach((result) => {
      if (result.dataset.serpShield) return;
      const url = resultUrl(result);
      const host = url && hostnameOf(url);
      if (!host) return;
      result.dataset.serpShield = "1";

      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "serp-shield-btn";
      btn.innerHTML = ICON.shield;
      btn.title = _("Adjust this site");
      btn.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        const img = result.querySelector(".favicon-img, img.favicon");
        const a = result.querySelector("h3 a") || result.querySelector("h3");
        openPanel({
          host: host,
          url: resultUrl(result),
          title: a ? a.textContent.trim() : host,
          favSrc: img ? (img.src || img.getAttribute("src") || "") : "",
        });
      });
      // Sit it inline right after the result title. (It can't go in the header
      // row — .url_header is an <a> and nested buttons aren't valid.)
      const h3 = result.querySelector("h3");
      if (h3) h3.appendChild(btn);
      else result.appendChild(btn);
    });
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

  // ── Lenses: per-browser saved filter sets (Kagi-style) ─────────────────────
  // A "Lens" dropdown below the search bar lets the user define named filter sets
  // — e.g. "Deals", "Movies" — and switch between them before searching. Each lens
  // has a mode:
  //   "only"  → true scoping: the query is rewritten with (site:a OR site:b …) on
  //             submit so the engines actually return pages from those sites.
  //   "favor" → soft boost: the lens's sites are raised in applyPriorities() so
  //             they float to the top of the organic results (client re-rank).
  // Definitions live in localStorage (this browser only), mirroring the per-user
  // side of the site shield.

  const LENS_KEY = "usearch.lenses";
  const LENS_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/></svg>';
  const CARET_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>';

  // Seeded on first run so the feature is discoverable; fully editable/deletable.
  const LENS_SEED = {
    active: "",
    lenses: [
      { id: "deals", name: "Deals", mode: "only",
        sites: ["slickdeals.net", "dealnews.com", "hotukdeals.com",
                "camelcamelcamel.com", "retailmenot.com", "idealo.co.uk"] },
      { id: "movies", name: "Movies", mode: "favor",
        sites: ["imdb.com", "rottentomatoes.com", "letterboxd.com",
                "themoviedb.org", "metacritic.com"] },
    ],
  };

  function loadLenses() {
    try {
      const raw = JSON.parse(localStorage.getItem(LENS_KEY) || "null");
      if (raw && Array.isArray(raw.lenses)) return raw;
    } catch (_) { /* ignore */ }
    saveLenses(LENS_SEED);
    return JSON.parse(JSON.stringify(LENS_SEED));
  }
  function saveLenses(state) {
    try { localStorage.setItem(LENS_KEY, JSON.stringify(state)); } catch (_) { /* ignore */ }
  }
  function activeLens() {
    const st = loadLenses();
    return st.active ? (st.lenses.find((l) => l.id === st.active) || null) : null;
  }
  function setActiveLens(id) {
    const st = loadLenses();
    st.active = id || "";
    saveLenses(st);
  }
  function uniqueLensId(st) {
    let id;
    do { id = "lens-" + Date.now().toString(36) + "-" + Math.floor(Math.random() * 1e4).toString(36); }
    while (st.lenses.some((l) => l.id === id));
    return id;
  }

  // Normalise a user-entered line to a bare host (drop scheme/path/www).
  function cleanDomain(line) {
    let s = (line || "").trim().toLowerCase();
    if (!s) return "";
    s = s.replace(/^https?:\/\//, "").replace(/\/.*$/, "").replace(/^www\./, "");
    return s.split(/\s+/)[0] || "";
  }

  // Build the site: clause for an "only" lens. Plain lines → include, lines
  // prefixed "-"/"!" → exclude. Includes are OR-grouped; excludes appended after.
  function lensClause(lens) {
    const inc = [], exc = [];
    (lens.sites || []).forEach((raw) => {
      const neg = /^[-!]/.test(raw);
      const host = cleanDomain(neg ? raw.slice(1) : raw);
      if (!host) return;
      (neg ? exc : inc).push(host);
    });
    let clause = "";
    if (inc.length === 1) clause = "site:" + inc[0];
    else if (inc.length > 1) clause = "(" + inc.map((h) => "site:" + h).join(" OR ") + ")";
    if (exc.length) clause += (clause ? " " : "") + exc.map((h) => "-site:" + h).join(" ");
    return clause;
  }

  // Strip a trailing site:/-site: clause we previously appended, so the visible
  // query box stays clean and re-submitting doesn't stack clauses.
  const LENS_CLAUSE_RE = /\s*(?:\((?:\s*-?site:[^\s()]+(?:\s+OR\s+)?)+\)|(?:\s*-?site:[^\s()]+)+)\s*$/i;
  function stripLensClause(q) {
    return (q || "").replace(LENS_CLAUSE_RE, "").trim();
  }

  // Is this host covered by the active "favor" lens? (subdomain walk, like getLevel)
  function lensFavorHost(host) {
    const lens = activeLens();
    if (!lens || lens.mode !== "favor" || !host) return false;
    const set = (lens.sites || []).map((s) => cleanDomain(s)).filter(Boolean);
    if (!set.length) return false;
    const labels = host.split(".");
    for (let i = 0; i < labels.length - 1; i++) {
      if (set.indexOf(labels.slice(i).join(".")) !== -1) return true;
    }
    return false;
  }

  function lensForm() { return document.getElementById("search"); }
  function lensQ() { return document.getElementById("q"); }

  // Inject the "Lens" dropdown into the filter row (results page) or just under
  // the search box (home page). Idempotent.
  function buildLensUI() {
    const form = lensForm();
    if (!form) return;
    if (document.getElementById("serp-lens")) { refreshLensBtn(); return; }

    const wrap = document.createElement("div");
    wrap.id = "serp-lens";
    wrap.className = "serp-lens";
    wrap.innerHTML =
      '<button type="button" class="serp-lens-btn" aria-haspopup="true" aria-expanded="false">' +
        '<span class="serp-lens-ic">' + LENS_ICON + "</span>" +
        '<span class="serp-lens-cur"></span>' +
        '<span class="serp-lens-caret">' + CARET_ICON + "</span>" +
      "</button>" +
      '<div class="serp-lens-menu" hidden></div>';

    const filters = form.querySelector(".search_filters");
    if (filters) {
      filters.appendChild(wrap);
    } else {
      const box = form.querySelector(".search_box") || form.querySelector("#search_header");
      if (box && box.parentNode) box.parentNode.insertBefore(wrap, box.nextSibling);
      else form.appendChild(wrap);
    }

    const btn = wrap.querySelector(".serp-lens-btn");
    const menu = wrap.querySelector(".serp-lens-menu");
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      if (menu.hidden) openLensMenu(menu, btn); else closeLensMenu();
    });

    // Rewrite the query on submit for "only" lenses (true scoping). .submit()
    // bypasses this handler, so selectLens() uses requestSubmit().
    form.addEventListener("submit", () => {
      const q = lensQ();
      if (!q) return;
      const clean = stripLensClause(q.value);
      const lens = activeLens();
      if (lens && lens.mode === "only") {
        const clause = lensClause(lens);
        q.value = clause ? (clean ? clean + " " + clause : clause) : clean;
      } else {
        q.value = clean;
      }
    });

    // On the results page the server-rendered query may still carry a clause we
    // appended last time — strip it for display so the box stays readable.
    const q = lensQ();
    if (q) q.value = stripLensClause(q.value);

    refreshLensBtn();
  }

  function refreshLensBtn() {
    const wrap = document.getElementById("serp-lens");
    if (!wrap) return;
    const lens = activeLens();
    wrap.querySelector(".serp-lens-cur").textContent = lens ? lens.name : _("Lens");
    wrap.classList.toggle("active", !!lens);
  }

  let _lensDocClick = null;
  function closeLensMenu() {
    const menu = document.querySelector("#serp-lens .serp-lens-menu");
    const btn = document.querySelector("#serp-lens .serp-lens-btn");
    if (menu) menu.hidden = true;
    if (btn) btn.setAttribute("aria-expanded", "false");
    if (_lensDocClick) { document.removeEventListener("click", _lensDocClick); _lensDocClick = null; }
  }

  function openLensMenu(menu, btn) {
    const st = loadLenses();
    let html =
      '<button type="button" class="serp-lens-item' + (!st.active ? " active" : "") +
        '" data-id="">' + _("All") + "</button>";
    st.lenses.forEach((l) => {
      html += '<button type="button" class="serp-lens-item' +
        (st.active === l.id ? " active" : "") + '" data-id="' + esc(l.id) + '">' +
        "<span>" + esc(l.name) + "</span>" +
        '<span class="serp-lens-mode">' + (l.mode === "only" ? _("only") : _("favor")) + "</span>" +
      "</button>";
    });
    html += '<div class="serp-lens-divider"></div>' +
      '<button type="button" class="serp-lens-item serp-lens-edit-btn">' + _("Edit…") + "</button>";
    menu.innerHTML = html;
    menu.hidden = false;
    btn.setAttribute("aria-expanded", "true");

    menu.querySelectorAll(".serp-lens-item").forEach((it) => {
      it.addEventListener("click", (e) => {
        e.preventDefault(); e.stopPropagation();
        if (it.classList.contains("serp-lens-edit-btn")) { closeLensMenu(); openLensManager(); return; }
        selectLens(it.dataset.id);
        closeLensMenu();
      });
    });

    _lensDocClick = (e) => { if (!menu.parentNode.contains(e.target)) closeLensMenu(); };
    setTimeout(() => document.addEventListener("click", _lensDocClick), 0);
  }

  function selectLens(id) {
    setActiveLens(id);
    refreshLensBtn();
    const q = lensQ();
    const onResults = !!document.getElementById("results");
    if (onResults && q && stripLensClause(q.value)) {
      const form = lensForm();
      if (form.requestSubmit) form.requestSubmit(); else form.submit();
    } else {
      applyPriorities();  // home page / empty query: re-rank anything on screen
    }
  }

  // ── Lens manager modal ──────────────────────────────────────────────────────

  function openLensManager() {
    closePanel();
    const back = document.createElement("div");
    back.id = "serp-modal-backdrop";
    back.className = "serp-modal-backdrop";
    back.innerHTML =
      '<div class="serp-modal serp-lens-modal" role="dialog" aria-modal="true">' +
        '<div class="serp-modal-head">' +
          '<span class="serp-modal-fav serp-lens-headic">' + LENS_ICON + "</span>" +
          '<div class="serp-modal-titles">' +
            '<p class="serp-modal-title">' + _("Lenses") + "</p>" +
            '<div class="serp-modal-host">' + _("Saved filter sets for this browser") + "</div>" +
          "</div>" +
          '<button type="button" class="serp-modal-close" aria-label="Close">' + ICON.close + "</button>" +
        "</div>" +
        '<div class="serp-modal-body serp-lens-mgr">' +
          '<div class="serp-lens-list"></div>' +
          '<button type="button" class="serp-lens-add">+ ' + _("Add lens") + "</button>" +
          '<p class="serp-lens-help">' +
            _("One domain per line. Prefix a line with “-” to exclude it. “Only” searches just these sites; “Favor” boosts them within normal results.") +
          "</p>" +
        "</div>" +
      "</div>";
    document.body.appendChild(back);
    back.addEventListener("click", (e) => { if (e.target === back) closePanel(); });
    back.querySelector(".serp-modal-close").addEventListener("click", closePanel);
    _panelEsc = (e) => { if (e.key === "Escape") closePanel(); };
    document.addEventListener("keydown", _panelEsc);

    const list = back.querySelector(".serp-lens-list");
    renderLensEditors(list);
    back.querySelector(".serp-lens-add").addEventListener("click", () => {
      const st = loadLenses();
      st.lenses.push({ id: uniqueLensId(st), name: _("New lens"), mode: "only", sites: [] });
      saveLenses(st);
      renderLensEditors(list);
    });
  }

  function renderLensEditors(list) {
    const st = loadLenses();
    list.innerHTML = "";
    if (!st.lenses.length) {
      const empty = document.createElement("p");
      empty.className = "serp-lens-empty";
      empty.textContent = _("No lenses yet — add one below.");
      list.appendChild(empty);
    }
    st.lenses.forEach((lens) => {
      const card = document.createElement("div");
      card.className = "serp-lens-edit";
      card.innerHTML =
        '<div class="serp-lens-row">' +
          '<input class="serp-lens-name" type="text" value="' + esc(lens.name) + '" placeholder="' + esc(_("Lens name")) + '">' +
          '<div class="serp-lens-modeseg">' +
            '<button type="button" data-mode="only"' + (lens.mode === "only" ? ' class="active"' : "") + ">" + _("Only") + "</button>" +
            '<button type="button" data-mode="favor"' + (lens.mode === "favor" ? ' class="active"' : "") + ">" + _("Favor") + "</button>" +
          "</div>" +
          '<button type="button" class="serp-lens-del" aria-label="' + esc(_("Delete lens")) + '">' + ICON.close + "</button>" +
        "</div>" +
        '<textarea class="serp-lens-sites" rows="4" spellcheck="false" placeholder="example.com"></textarea>';
      card.querySelector(".serp-lens-sites").value = (lens.sites || []).join("\n");

      const nameEl = card.querySelector(".serp-lens-name");
      nameEl.addEventListener("input", () => updateLens(lens.id, (l) => { l.name = nameEl.value; }));
      const sitesEl = card.querySelector(".serp-lens-sites");
      sitesEl.addEventListener("input", () => updateLens(lens.id, (l) => {
        l.sites = sitesEl.value.split("\n").map((s) => s.trim()).filter(Boolean);
      }));
      card.querySelectorAll(".serp-lens-modeseg button").forEach((b) => {
        b.addEventListener("click", () => {
          updateLens(lens.id, (l) => { l.mode = b.dataset.mode; });
          card.querySelectorAll(".serp-lens-modeseg button").forEach((x) => x.classList.toggle("active", x === b));
        });
      });
      card.querySelector(".serp-lens-del").addEventListener("click", () => {
        const st2 = loadLenses();
        st2.lenses = st2.lenses.filter((l) => l.id !== lens.id);
        if (st2.active === lens.id) st2.active = "";
        saveLenses(st2);
        renderLensEditors(list);
        refreshLensBtn();
      });
      list.appendChild(card);
    });
  }

  function updateLens(id, fn) {
    const st = loadLenses();
    const lens = st.lenses.find((l) => l.id === id);
    if (!lens) return;
    fn(lens);
    saveLenses(st);
    refreshLensBtn();
  }

  // ── Orchestration ──────────────────────────────────────────────────────────

  function enhanceAll() {
    ensureCurrencyBox();
    buildTopSection();
    enhanceCopyLinks(document);
    enhanceCards(document);
    enhanceShields(document);
    applyPriorities();
    buildStatusBar();
    enhanceAiSummary();
  }

  function run() {
    injectStyles();
    // The lens dropdown lives below the search bar, so it must init even on the
    // home page where there are no results yet.
    buildLensUI();
    if (!document.getElementById("results") && !document.querySelector(".result")) return;
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
