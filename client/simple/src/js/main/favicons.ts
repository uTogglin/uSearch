// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * Batched favicon resolver.
 *
 * Each result favicon is rendered as a placeholder carrying its domain in
 * `data-favicon-authority` (see `macros.html`). Instead of letting the browser
 * fetch one `/favicon_proxy` URL per result — a dozen-plus edge Worker
 * invocations per search, each leaking the visited domain in a plaintext URL —
 * this collects every domain on the page and resolves them in a SINGLE request,
 * routed over the encrypted channel when available so the edge stays blind.
 *
 * A MutationObserver picks up favicons added later by infinite scroll and folds
 * each appended page into its own single batch.
 */

import { getChannel, isAvailable } from "../util/echannel.ts";

const SELECTOR = "img[data-favicon-authority]:not([data-favicon-done])";

async function resolveBatch(authorities: string[]): Promise<Record<string, string>> {
  if (isAvailable()) {
    const channel = await getChannel();
    const envelope = await channel.encrypt(JSON.stringify({ authorities }));
    const res = await fetch("./efavicon_batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(envelope)
    });
    if (!res.ok) throw new Error(`efavicon_batch ${res.status}`);
    const payload = JSON.parse(await channel.decryptText(await res.json()));
    return payload.icons ?? {};
  }

  const res = await fetch("./favicon_batch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ authorities })
  });
  if (!res.ok) throw new Error(`favicon_batch ${res.status}`);
  return ((await res.json()).icons as Record<string, string>) ?? {};
}

async function flush(): Promise<void> {
  const imgs = [...document.querySelectorAll<HTMLImageElement>(SELECTOR)];
  if (imgs.length === 0) return;

  // Mark immediately so a follow-up flush (observer fired again) doesn't
  // re-request the same icons. Failed resolutions just keep the placeholder.
  const byAuthority = new Map<string, HTMLImageElement[]>();
  for (const img of imgs) {
    img.dataset.faviconDone = "1";
    const authority = img.dataset.faviconAuthority ?? "";
    if (!authority) continue;
    const group = byAuthority.get(authority);
    if (group) group.push(img);
    else byAuthority.set(authority, [img]);
  }

  const authorities = [...byAuthority.keys()];
  if (authorities.length === 0) return;

  try {
    const icons = await resolveBatch(authorities);
    for (const [authority, group] of byAuthority) {
      const url = icons[authority];
      if (url) for (const img of group) img.src = url;
    }
  } catch (error) {
    console.error("favicon batch failed:", error);
  }
}

let timer: number | undefined;
function schedule(): void {
  clearTimeout(timer);
  // Coalesce a page's worth of favicons (initial render or one infinite-scroll
  // append) into a single request.
  timer = window.setTimeout(() => void flush(), 50);
}

const results = document.getElementById("results");
if (results) {
  new MutationObserver(schedule).observe(results, { childList: true, subtree: true });
}
schedule();
