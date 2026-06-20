// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * Encrypted search transport.
 *
 * Replaces the plaintext form navigation (`GET/POST /search`) with an encrypted
 * round-trip to `/esearch`, so the query never appears in a URL or any header
 * the Cloudflare edge can read. The server returns the fully-rendered results
 * page encrypted; we swap it into the document.
 *
 * The query is kept in the URL *fragment* (`#q=…`) only — fragments are never
 * sent to the server/edge — so reload, back, and bookmarks still work while the
 * edge stays blind.
 */

import { getChannel, isAvailable } from "./echannel.ts";

const ESEARCH_URL = "./esearch";

type EsearchPayload = { html?: string; redirect?: string };

function formParams(form: HTMLFormElement): string {
  // FormData yields the same successful controls a native submit would send.
  const params = new URLSearchParams();
  new FormData(form).forEach((value, key) => {
    params.append(key, typeof value === "string" ? value : "");
  });
  return params.toString();
}

function queryOf(form: HTMLFormElement): string {
  return form.querySelector<HTMLInputElement>('input[name="q"]')?.value ?? "";
}

/** Whether encrypted search can be used (server published a key + Web Crypto). */
export const encryptedSearchAvailable = isAvailable;

/**
 * Encrypt + submit a search form, then swap the decrypted results page in.
 * Throws on any failure so callers can fall back to a native submit.
 */
export async function submitEncrypted(form: HTMLFormElement): Promise<void> {
  const channel = await getChannel();
  const envelope = await channel.encrypt(formParams(form));

  const res = await fetch(ESEARCH_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(envelope)
  });
  if (!res.ok) throw new Error(`esearch ${res.status}`);

  const payload: EsearchPayload = JSON.parse(await channel.decryptText(await res.json()));

  if (payload.redirect) {
    window.location.href = payload.redirect;
    return;
  }
  if (typeof payload.html !== "string") throw new Error("esearch: malformed payload");

  // Persist the query in the fragment (never sent upstream) for reload/back,
  // without adding a spurious history entry before the page swap.
  const hash = `#q=${encodeURIComponent(queryOf(form))}`;
  history.replaceState(null, "", window.location.pathname + window.location.search + hash);

  // document.write re-executes the page's scripts, re-bootstrapping the app
  // against the new DOM (form interception, plugins, ai_summary, …).
  document.open();
  document.write(payload.html);
  document.close();
}

/**
 * Fetch a results page over the encrypted channel and return its decrypted HTML
 * *without* swapping the document — used by infinite scroll, which extracts a
 * fragment from it. Throws on failure (caller falls back).
 */
export async function fetchEncryptedPage(form: HTMLFormElement): Promise<string> {
  const channel = await getChannel();
  const envelope = await channel.encrypt(formParams(form));

  const res = await fetch(ESEARCH_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(envelope)
  });
  if (!res.ok) throw new Error(`esearch ${res.status}`);

  const payload: EsearchPayload = JSON.parse(await channel.decryptText(await res.json()));
  if (typeof payload.html !== "string") throw new Error("esearch: malformed payload");
  return payload.html;
}

/**
 * On a fresh load of a page that carries `#q=…` but has no results yet (e.g. a
 * reload or an opened bookmark — the browser reloads the query-less URL), re-run
 * the encrypted search so the results come back. Returns true if it kicked off.
 */
export function restoreFromHash(form: HTMLFormElement): boolean {
  if (!isAvailable()) return false;
  const hash = window.location.hash;
  if (!hash.startsWith("#q=")) return false;
  // Already on a results page? Nothing to restore.
  if (document.querySelector("#results")) return false;

  const q = decodeURIComponent(hash.slice(3));
  if (!q) return false;

  const input = form.querySelector<HTMLInputElement>('input[name="q"]');
  if (input) input.value = q;

  void submitEncrypted(form).catch((err) => console.error("esearch restore failed:", err));
  return true;
}
