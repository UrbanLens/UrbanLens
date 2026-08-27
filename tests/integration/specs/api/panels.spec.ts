/**
 * Plugin-contributed enrichment panels, as a native client sees them.
 *
 * The most deployment-dependent surface in the API and the least testable
 * anywhere else. Which panels exist is decided at import time by the plugin
 * registry - 54 of them on a full deployment - and which ones a *caller* may
 * see depends on their key's scopes and on whether the deployment enabled the
 * provider at all. None of that is knowable from a fixture; it is a property of
 * the machine the code is running on.
 *
 * So the assertions here are about the contract rather than about contents: a
 * client has to be able to ask what panels exist, get a stable key for each,
 * and fetch one by key without guessing. What a panel *says* depends on what
 * the outside world knows about a coordinate and is not something a test can
 * pin down.
 */

import { expect, test } from "../../lib/fixtures.js";
import { resourceName } from "../../lib/env.js";

/** One entry in the panel index. */
interface PanelSummary {
    key: string;
    kinds?: string[];
    ready?: boolean;
}

test.describe("pin panels", () => {
    test("a pin advertises its panels, each with a key", async ({ api }) => {
        const pin = await api.createPin({ name: resourceName("panel host") });

        const response = await api.get(`pins/${pin.slug}/panels/`);
        expect(response.status(), `the panel index answered ${response.status()}: ${(await response.text()).slice(0, 250)}`).toBe(200);

        const body = (await response.json()) as PanelSummary[] | { results?: PanelSummary[] };
        const panels = Array.isArray(body) ? body : (body.results ?? []);
        expect(Array.isArray(panels), `the panel index is not a list: ${JSON.stringify(body).slice(0, 200)}`).toBeTruthy();

        // A panel without a key cannot be fetched, so it may as well not be
        // listed - and an index that returns unusable entries is worse than an
        // empty one, because a client will render tabs that lead nowhere.
        for (const panel of panels) {
            expect(panel.key, `a panel in the index has no key: ${JSON.stringify(panel)}`).toBeTruthy();
        }
    });

    test("every advertised panel can actually be fetched by its key", async ({ api }) => {
        const pin = await api.createPin({ name: resourceName("panel fetch") });
        const body = (await api.json<PanelSummary[] | { results?: PanelSummary[] }>("get", `pins/${pin.slug}/panels/`));
        const panels = Array.isArray(body) ? body : (body.results ?? []);
        test.skip(panels.length === 0, "This deployment advertises no panels for a new pin, so there is nothing to fetch.");

        // A handful rather than all of them: each is a real provider lookup and
        // the point is that the index and the item route agree, not to walk 54
        // integrations on every run.
        const failures: string[] = [];
        for (const panel of panels.slice(0, 5)) {
            const response = await api.get(`pins/${pin.slug}/panels/${panel.key}/`);
            // 204 is a legitimate answer - the provider had nothing to say -
            // and so is 404 for a panel this key may not read. A 500 is not.
            if (response.status() >= 500) {
                failures.push(`${panel.key} -> ${response.status()}: ${(await response.text()).slice(0, 120)}`);
            }
        }
        expect(failures, `panels listed in the index failed when fetched:\n  ${failures.join("\n  ")}`).toHaveLength(0);
    });

    test("a panel key that was never registered is refused rather than crashing", async ({ api }) => {
        const pin = await api.createPin({ name: resourceName("unknown panel") });

        const response = await api.get(`pins/${pin.slug}/panels/definitely-not-a-registered-panel/`);
        expect([204, 400, 404], `an unregistered panel key answered ${response.status()}`).toContain(response.status());
    });

    test("a key without the panels scope cannot read them", async ({ api, restrictedApi }) => {
        const pin = await api.createPin({ name: resourceName("panel scope") });
        const response = await restrictedApi.get(`pins/${pin.slug}/panels/`);
        expect(response.status(), "a profile:read key read the panel index").toBe(403);
    });
});
