/**
 * Site admin > Statistics (``pages/site_admin_stats.html``): flashes the refresh badge on each htmx swap.
 */

export interface SiteStatsDeps {
    later: (ms: number, run: () => void) => void;
}

const defaults: SiteStatsDeps = {
    later: (ms, run) => void setTimeout(run, ms),
};

/** Returns the uninstaller. */
export function installSiteStatsPage(root: Document, deps: SiteStatsDeps = defaults): () => void {
    const onSwap = (): void => {
        const badge = root.getElementById("stats-refresh-badge");
        if (!badge) return;
        badge.classList.add("stats-refresh-badge--active");
        deps.later(800, () => badge.classList.remove("stats-refresh-badge--active"));
    };

    root.addEventListener("htmx:afterSwap", onSwap);
    return () => {
        root.removeEventListener("htmx:afterSwap", onSwap);
    };
}
