import { afterEach, beforeEach, expect, test } from "bun:test";

import { installSiteStatsPage } from "./site-stats-page";

let scheduled: { ms: number; run: () => void }[] = [];
let uninstall: () => void = () => {};

beforeEach(() => {
    scheduled = [];
    document.body.innerHTML = `<span id="stats-refresh-badge"></span><div id="system"></div>`;
    uninstall = installSiteStatsPage(document, { later: (ms, run) => void scheduled.push({ ms, run }) });
});

afterEach(() => {
    uninstall();
});

test("each swap flashes the refresh badge", () => {
    document.getElementById("system")?.dispatchEvent(new CustomEvent("htmx:afterSwap", { bubbles: true }));
    expect(document.getElementById("stats-refresh-badge")?.classList.contains("stats-refresh-badge--active")).toBe(true);
    for (const s of scheduled) s.run();
    expect(document.getElementById("stats-refresh-badge")?.classList.contains("stats-refresh-badge--active")).toBe(false);
});
