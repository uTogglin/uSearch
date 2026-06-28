// SPDX-License-Identifier: AGPL-3.0-or-later

import { http, listen, settings } from "../toolkit.ts";
import { assertElement } from "../util/assertElement.ts";
import { getChannel, isAvailable } from "../util/echannel.ts";
import { submitEncrypted } from "../util/esearch.ts";

// Plain OpenSearch reply is [prefix, completions]; the featured-bang menu adds
// two aligned arrays — [prefix, completions, names, icons] — where names are the
// human labels and icons are inline favicon data URLs (or "" → letter avatar).
type AutocompleteResponse = [string, string[]] | [string, string[], string[], string[]];

// Featured-bang behaviour, shared with the server via a cookie it reads on
// search: "redirect" jumps to the site's own search, "usearch" runs it as a
// site:-scoped uSearch search (the "More results from this site" behaviour).
type BangMode = "redirect" | "usearch";
const BANG_MODE_COOKIE = "featured_bang_mode";

const getBangMode = (): BangMode =>
  document.cookie
    .split("; ")
    .find((c) => c.startsWith(`${BANG_MODE_COOKIE}=`))
    ?.split("=")[1] === "usearch"
    ? "usearch"
    : "redirect";

const setBangMode = (mode: BangMode): void => {
  document.cookie = `${BANG_MODE_COOKIE}=${mode}; path=/; max-age=31536000; SameSite=Lax`;
};

// Cache suggestions per query so backspacing/retyping an already-seen prefix
// costs zero requests, and keep at most one autocomplete request in flight —
// each new keystroke cancels the previous one. Both cut edge Worker invocations.
const cache = new Map<string, AutocompleteResponse>();
let inFlight: AbortController | null = null;

const fetchResults = async (qInput: HTMLInputElement, query: string): Promise<void> => {
  try {
    let results: AutocompleteResponse;

    const cached = cache.get(query);
    if (cached) {
      results = cached;
    } else {
      inFlight?.abort();
      const controller = new AbortController();
      inFlight = controller;

      if (isAvailable()) {
        // Encrypted: the partial query never reaches the edge in clear. The reply
        // is the [sug_prefix, results] shape, encrypted. No plaintext fallback —
        // dropping suggestions is preferable to leaking the query.
        const channel = await getChannel();
        const envelope = await channel.encrypt(JSON.stringify({ q: query }));
        const res = await fetch("./eautocompleter", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(envelope),
          signal: controller.signal
        });
        if (!res.ok) throw new Error(`eautocompleter ${res.status}`);
        results = JSON.parse(await channel.decryptText(await res.json()));
      } else {
        let res: Response;
        if (settings.method === "GET") {
          res = await http("GET", `./autocompleter?q=${query}`);
        } else {
          res = await http("POST", "./autocompleter", { body: new URLSearchParams({ q: query }) });
        }
        results = await res.json();
      }

      cache.set(query, results);
    }

    const autocomplete = document.querySelector<HTMLElement>(".autocomplete");
    assertElement(autocomplete);

    const autocompleteList = document.querySelector<HTMLUListElement>(".autocomplete ul");
    assertElement(autocompleteList);

    autocomplete.classList.add("open");
    autocompleteList.replaceChildren();

    // show an error message that no result was found
    if (results?.[1]?.length === 0) {
      autocompleteList.classList.remove("bang-menu");
      const noItemFoundMessage = Object.assign(document.createElement("li"), {
        className: "no-item-found",
        textContent: settings.translations?.no_item_found ?? "No results found"
      });
      autocompleteList.append(noItemFoundMessage);
      return;
    }

    const fragment = new DocumentFragment();

    // Featured-bang menu: names present and aligned with the completions.
    const names = results[2];
    const icons = results[3];
    const isBangMenu = Array.isArray(names) && names.length === results[1].length;

    // The bang menu lays its rows out as a Kagi-style two-column grid; plain
    // suggestions stay a single-column list. The class drives that switch.
    autocompleteList.classList.toggle("bang-menu", isBangMenu);

    // Mode toggle pinned to the top of the bang menu. Not a suggestion, so it is
    // excluded from keyboard navigation (see the keyup handler) and never submits.
    if (isBangMenu) {
      const toggle = document.createElement("li");
      toggle.className = "bang-mode-toggle";
      const renderToggle = () => {
        const mode = getBangMode();
        toggle.replaceChildren(
          Object.assign(document.createElement("span"), {
            className: "bang-mode-toggle__label",
            textContent: mode === "usearch" ? "Search in uSearch" : "Redirect to site"
          }),
          Object.assign(document.createElement("span"), {
            className: `bang-mode-toggle__switch is-${mode}`,
            textContent: mode === "usearch" ? "uSearch" : "Site"
          })
        );
      };
      renderToggle();
      listen("mousedown", toggle, (event: MouseEvent) => {
        event.preventDefault();
        setBangMode(getBangMode() === "usearch" ? "redirect" : "usearch");
        renderToggle();
        qInput.focus();
      });
      autocompleteList.append(toggle);
    }

    results[1].forEach((completion, i) => {
      const li = document.createElement("li");
      // Stash the value the keyboard nav / submit should use; the rich rows show
      // name + shortcut, so textContent is no longer the value to submit.
      li.dataset.value = completion;

      if (isBangMenu && names) {
        li.classList.add("bang-suggestion");
        const shortcut = completion.trim().split(/\s+/).pop() ?? "";
        const icon = icons?.[i];

        if (icon) {
          const img = Object.assign(document.createElement("img"), { src: icon, alt: "" });
          img.className = "bang-suggestion__icon";
          li.append(img);
        } else {
          const avatar = Object.assign(document.createElement("span"), {
            textContent: (names[i]?.[0] ?? "!").toUpperCase()
          });
          avatar.className = "bang-suggestion__icon bang-suggestion__icon--letter";
          li.append(avatar);
        }

        li.append(
          Object.assign(document.createElement("span"), {
            className: "bang-suggestion__name",
            textContent: names[i] ?? ""
          }),
          Object.assign(document.createElement("span"), {
            className: "bang-suggestion__shortcut",
            textContent: shortcut
          })
        );
      } else {
        li.textContent = completion;
      }

      listen("mousedown", li, (event: MouseEvent) => {
        // Keep focus in the input so a bang selection can be typed onto.
        event.preventDefault();
        const form = document.querySelector<HTMLFormElement>("#search");

        if (isBangMenu) {
          // Fill the bang and let the user keep typing the query — don't submit.
          qInput.value = `${completion} `;
          qInput.focus();
          autocomplete.classList.remove("open");
          return;
        }

        qInput.value = completion;
        if (!form) return;
        if (isAvailable()) {
          void submitEncrypted(form).catch(() => form.submit());
        } else {
          form.submit();
        }
      });

      fragment.append(li);
    });

    autocompleteList.append(fragment);

    // Kagi-style footer link spanning the full grid width. It is a real
    // navigation (the search-syntax help page), so — unlike a suggestion row —
    // it must not be keyboard-navigable (see the keyup filter) and must follow
    // its href on click rather than fill the input.
    if (isBangMenu) {
      const footer = document.createElement("li");
      footer.className = "bang-menu-footer locked";
      const link = Object.assign(document.createElement("a"), {
        href: "./info/en/search-syntax",
        textContent: "Learn more about bangs"
      });
      listen("mousedown", footer, (event: MouseEvent) => {
        // mousedown blurs the input and closes the menu before a click can
        // land, so navigate here and keep the default blur from racing us.
        event.preventDefault();
        window.location.assign(link.href);
      });
      footer.append(link);
      autocompleteList.append(footer);
    }
  } catch (error) {
    // A superseded request was aborted by a newer keystroke — not an error.
    if ((error as Error)?.name === "AbortError") return;
    console.error("Error fetching autocomplete results:", error);
  }
};

const qInput = document.getElementById("q") as HTMLInputElement | null;
assertElement(qInput);

let timeoutId: number;

listen("input", qInput, () => {
  clearTimeout(timeoutId);

  const query = qInput.value;
  // A single-'!' bang menu opens on the very first character; everything else
  // waits for the usual minimum so we don't fire on every stray keystroke.
  const lastToken = query.split(/\s+/).pop() ?? "";
  const typingBang = lastToken.startsWith("!") && !lastToken.startsWith("!!");
  const minLength = typingBang ? 1 : (settings.autocomplete_min ?? 2);

  if (query.length < minLength) return;

  timeoutId = window.setTimeout(async () => {
    if (query === qInput.value) {
      await fetchResults(qInput, query);
    }
  }, 400);
});

const autocomplete: HTMLElement | null = document.querySelector<HTMLElement>(".autocomplete");
const autocompleteList: HTMLUListElement | null = document.querySelector<HTMLUListElement>(".autocomplete ul");
if (autocompleteList) {
  listen("keydown", qInput, (event: KeyboardEvent) => {
    if (event.key === "Escape") {
      autocomplete?.classList.remove("open");
    }
  });
  listen("keyup", qInput, (event: KeyboardEvent) => {
    const listItems = ([...autocompleteList.children] as HTMLElement[]).filter(
      (item) =>
        !item.classList.contains("bang-mode-toggle") && !item.classList.contains("bang-menu-footer")
    );

    const currentIndex = listItems.findIndex((item) => item.classList.contains("active"));
    let newCurrentIndex = -1;

    switch (event.key) {
      case "ArrowUp": {
        const currentItem = listItems[currentIndex];
        if (currentItem && currentIndex >= 0) {
          currentItem.classList.remove("active");
        }
        // we need to add listItems.length to the index calculation here because the JavaScript modulos
        // operator doesn't work with negative numbers
        newCurrentIndex = (currentIndex - 1 + listItems.length) % listItems.length;
        break;
      }
      case "ArrowDown": {
        const currentItem = listItems[currentIndex];
        if (currentItem && currentIndex >= 0) {
          currentItem.classList.remove("active");
        }
        newCurrentIndex = (currentIndex + 1) % listItems.length;
        break;
      }
      case "Enter":
        if (autocomplete) {
          autocomplete.classList.remove("open");
        }
        break;
      default:
        break;
    }

    if (newCurrentIndex !== -1) {
      const selectedItem = listItems[newCurrentIndex];
      if (selectedItem) {
        selectedItem.classList.add("active");

        if (!selectedItem.classList.contains("no-item-found")) {
          const qInput = document.getElementById("q") as HTMLInputElement | null;
          if (qInput) {
            qInput.value = selectedItem.dataset.value ?? selectedItem.textContent ?? "";
          }
        }
      }
    }
  });

  listen("blur", qInput, () => {
    autocomplete?.classList.remove("open");
  });

  listen("focus", qInput, () => {
    autocomplete?.classList.add("open");
  });
}
