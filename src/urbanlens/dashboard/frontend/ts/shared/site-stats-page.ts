/**
 * Site admin > Statistics (``pages/site_admin_stats.html``): the growth charts, the system panel's "pull latest code"
 * button (which arrives in an htmx swap), and the badge that flashes on each refresh.
 */

import { drawAttributeBarCharts } from "./bar-chart";
import { getCsrfToken } from "./csrf";
import type { FetchInit } from "./site-runtime";

const PULL_FAILED = "Could not pull latest code.";

export interface SiteStatsDeps {
    reload: () => void;
    later: (ms: number, run: () => void) => void;
}

const defaults: SiteStatsDeps = {
    reload: () => window.location.reload(),
    later: (ms, run) => void setTimeout(run, ms),
};

async function pull(url: string): Promise<{ message: string; changed: boolean }> {
    const init: FetchInit = { method: "POST", headers: { Accept: "application/json", "X-CSRFToken": getCsrfToken() }, credentials: "same-origin", __ulReported: true };
    const response = await fetch(url, init);
    const payload: unknown = await response.json().catch(() => null);
    const field = (key: string): unknown => (payload && typeof payload === "object" ? Reflect.get(payload, key) : undefined);
    const message = field("message");
    if (!response.ok || field("ok") !== true) throw new Error(typeof message === "string" && message ? message : response.statusText || PULL_FAILED);
    return { message: typeof message === "string" ? message : "", changed: field("changed") === true };
}

/** Returns the uninstaller. */
export function installSiteStatsPage(root: Document, deps: SiteStatsDeps = defaults): () => void {
    drawAttributeBarCharts(root);

    const onClick = async (event: Event): Promise<void> => {
        const button = event.target instanceof Element ? event.target.closest("#git-pull-refresh-btn") : null;
        if (!(button instanceof HTMLButtonElement) || button.disabled) return;
        const original = Array.from(button.childNodes);
        const spinner = document.createElement("span");
        spinner.className = "ul-spinner";
        spinner.setAttribute("aria-hidden", "true");
        button.disabled = true;
        button.replaceChildren(spinner, " Pulling...");
        window.toastr?.info("Pulling latest code...");
        try {
            const { message, changed } = await pull(button.dataset.url ?? "");
            window.toastr?.success(message || "Code updated. Refreshing...");
            deps.later(changed ? 1200 : 500, deps.reload);
        } catch (error) {
            button.disabled = false;
            button.replaceChildren(...original);
            window.toastr?.error(error instanceof Error && error.message ? error.message : PULL_FAILED);
        }
    };
    const onSwap = (): void => {
        const badge = root.getElementById("stats-refresh-badge");
        if (!badge) return;
        badge.classList.add("stats-refresh-badge--active");
        deps.later(800, () => badge.classList.remove("stats-refresh-badge--active"));
    };

    root.addEventListener("click", onClick);
    root.addEventListener("htmx:afterSwap", onSwap);
    return () => {
        root.removeEventListener("click", onClick);
        root.removeEventListener("htmx:afterSwap", onSwap);
    };
}
