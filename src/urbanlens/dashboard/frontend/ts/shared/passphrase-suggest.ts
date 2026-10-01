/**
 * "Suggest 5 strong passphrases" on the password fields of the sign-in pages (``partials/auth/_passphrase_suggest.html``).
 */

import type { FetchInit } from "./site-runtime";

const LOAD_FAILED = "Could not load suggestions.";

async function fetchPassphrases(url: string): Promise<string[]> {
    const init: FetchInit = { headers: { Accept: "application/json" }, credentials: "same-origin", __ulReported: true };
    const response = await fetch(url, init);
    if (response.status === 429) throw new Error("Too many requests. Try again in a few minutes.");
    if (!response.ok) throw new Error(LOAD_FAILED);
    const data: unknown = await response.json();
    const phrases: unknown = data && typeof data === "object" ? Reflect.get(data, "passphrases") : null;
    return Array.isArray(phrases) ? phrases.filter((p): p is string => typeof p === "string") : [];
}

function install(root: HTMLElement): void {
    const button = root.querySelector<HTMLButtonElement>("[data-passphrase-suggest]");
    const list = root.querySelector<HTMLElement>("[data-passphrase-list]");
    const status = root.querySelector<HTMLElement>("[data-passphrase-status]");
    const url = root.dataset.suggestUrl;
    const password = document.getElementById(root.dataset.passwordId ?? "");
    const confirm = root.dataset.confirmId ? document.getElementById(root.dataset.confirmId) : null;
    if (!button || !list || !url || !(password instanceof HTMLInputElement)) return;

    const setStatus = (message: string, isError = false): void => {
        if (!status) return;
        status.hidden = !message;
        status.textContent = message;
        status.classList.toggle("auth-passphrase-status--error", isError);
    };
    const apply = (phrase: string): void => {
        // Shown in the clear so it can be copied down before it is needed.
        for (const input of [password, confirm]) {
            if (!(input instanceof HTMLInputElement)) continue;
            input.value = phrase;
            input.dispatchEvent(new Event("input", { bubbles: true }));
            input.type = "text";
        }
        setStatus("Passphrase filled in both fields. Copy it somewhere safe before continuing.");
    };
    const render = (phrases: string[]): void => {
        list.replaceChildren(
            ...phrases.map((phrase) => {
                const pick = document.createElement("button");
                pick.type = "button";
                pick.className = "auth-passphrase-option";
                pick.textContent = phrase;
                pick.addEventListener("click", () => apply(phrase));
                const li = document.createElement("li");
                li.append(pick);
                return li;
            }),
        );
        list.hidden = phrases.length === 0;
    };

    button.addEventListener("click", async () => {
        button.disabled = true;
        setStatus("Generating…");
        try {
            render(await fetchPassphrases(url));
            setStatus("Click a passphrase to use it.");
        } catch (error) {
            list.hidden = true;
            setStatus(error instanceof Error && error.message ? error.message : LOAD_FAILED, true);
        } finally {
            button.disabled = false;
        }
    });
}

export function installPassphraseSuggest(root: ParentNode): void {
    for (const el of root.querySelectorAll<HTMLElement>(".auth-passphrase")) install(el);
}
