/**
 * The enrichment panels a real place is supposed to offer, and whether they fill.
 *
 * A census rather than a set of individual assertions. `GET pins/{slug}/panels/`
 * lists every API-exposed panel the caller may see, each with a `ready` flag, so
 * one request covers a dozen integrations at once - and covers them the way they
 * actually fail, which is not "the endpoint 500s" but "it is listed, it never
 * becomes ready, and nothing says why".
 *
 * ## What "listed" and "ready" each mean
 *
 * The list is already filtered twice, and the distinction matters when reading a
 * failure:
 *
 * - A panel whose `gate(pin)` refuses - no coordinates, outside the USA - is
 *   **omitted entirely**, not listed as unready.
 * - A panel gated on a subscription feature the account lacks is also omitted,
 *   and its detail route answers **404 rather than 403**, deliberately, so the
 *   list cannot be used to enumerate what a subscription would buy.
 *
 * So "absent" is ambiguous between "not applicable here" and "you may not see
 * it", and only "listed but never ready" is unambiguously a pipeline that is not
 * running. These tests are written around that: the census is reported rather
 * than asserted against a fixed list, and the sharp assertion is on readiness.
 *
 * The one hard expectation is that a real place in New York offers *several*
 * panels. A campus with a Wikipedia article, a CRIS inventory record and EPA
 * regulated facilities that offers one or two has lost its providers.
 */

import { expect, locationDataTest as test, skipUnlessLocationDataEnabled } from "./fixtures.js";
import { waitForOrNull } from "../../lib/waiting.js";

skipUnlessLocationDataEnabled();

interface PanelEntry {
    key: string;
    kinds: string[];
    ready: boolean;
}

/**
 * Panels a pin on this campus should be offered.
 *
 * Not asserted as a complete set - plugins are added and a deployment can turn
 * providers off, so demanding an exact list would make this file a maintenance
 * burden that fails for the wrong reason. Each is checked individually and
 * missing ones are reported together, so the message says which integration
 * stopped contributing rather than just "the count changed".
 */
const EXPECTED_PANELS = [
    { key: "wikipedia", why: "the site has a substantial Wikipedia article" },
    { key: "property_records", why: "REData has a county parcel record for these coordinates" },
    { key: "parcel_buildings", why: "REData lists 33 CRIS buildings on this parcel" },
    { key: "epa_echo_detail", why: "this is a regulated site and the panel is ungated" },
    { key: "boundary", why: "every pin with coordinates has one" },
] as const;

/** How long a panel gets to become ready before it counts as stuck. */
const PANEL_READY_TIMEOUT_MS = 300_000;

async function panels(campus: { api: { json: <T>(m: "get", p: string) => Promise<T> }; pin: { slug: string } }): Promise<PanelEntry[]> {
    const body = await campus.api.json<PanelEntry[] | { results?: PanelEntry[] }>("get", `pins/${campus.pin.slug}/panels/`);
    return Array.isArray(body) ? body : (body.results ?? []);
}

test.describe("Hudson River State Hospital - enrichment panels", () => {
    test("a real place offers a substantial set of panels", async ({ campus }) => {
        const listed = await panels(campus);

        expect(
            listed.length,
            `only ${listed.length} panel(s) are offered on this pin: ${JSON.stringify(listed.map((panel) => panel.key))}. A place with a ` +
                "Wikipedia article, a county parcel record, a CRIS inventory entry and EPA facilities should offer several. A short list " +
                "usually means the account cannot make outbound calls (external_apis_enabled) or the deployment has no REData configured",
        ).toBeGreaterThan(3);
    });

    test("the panels this place should have are all offered", async ({ campus }) => {
        const listed = await panels(campus);
        const keys = new Set(listed.map((panel) => panel.key));
        const missing = EXPECTED_PANELS.filter((panel) => !keys.has(panel.key));

        expect(
            missing.map((panel) => `${panel.key} (${panel.why})`),
            `offered: ${JSON.stringify([...keys].sort())}. A panel is omitted from this list for one of two reasons, and they are worth ` +
                "telling apart: its gate(pin) refused - which for these coordinates would be surprising - or it is gated on a " +
                "subscription feature this account lacks, in which case it is hidden deliberately and its detail route answers 404 rather " +
                "than 403 so the list cannot enumerate what a subscription would buy",
        ).toEqual([]);
    });

    test("every offered panel eventually becomes ready", async ({ campus }) => {
        // The failure this catches is the quiet one: a panel that is listed,
        // polls, and never fills. On the web that is a card that spins forever;
        // here it is a `ready` flag that never flips.
        const settled = await waitForOrNull(() => panels(campus), (listed) => listed.every((panel) => panel.ready), {
            what: "every offered panel to become ready",
            timeoutMs: PANEL_READY_TIMEOUT_MS,
            intervalMs: 15_000,
            describe: (listed) => {
                const stuck = listed.filter((panel) => !panel.ready).map((panel) => panel.key);
                return `${listed.length - stuck.length}/${listed.length} ready; still pending: ${stuck.join(", ") || "none"}`;
            },
        });

        if (settled === null) {
            const listed = await panels(campus);
            const stuck = listed.filter((panel) => !panel.ready).map((panel) => panel.key);
            expect(
                stuck,
                "these panels are offered but never became ready. Panels are fetched by fetch_panel_source on the panel_fetch queue - " +
                    "the default celery worker does not consume it, so a deployment without celery-worker-panels leaves every one of " +
                    "these pending indefinitely with nothing in the UI to say so. Rule that out before treating it as a provider problem",
            ).toEqual([]);
        }
    });

    test("a panel that is ready actually returns content", async ({ campus }) => {
        const listed = await panels(campus);
        const ready = listed.filter((panel) => panel.ready);
        test.skip(ready.length === 0, "no panel is ready yet - see the previous test.");

        const empty: string[] = [];
        for (const panel of ready) {
            const response = await campus.api.get(`pins/${campus.pin.slug}/panels/${panel.key}/`);
            if (!response.ok()) {
                empty.push(`${panel.key}: HTTP ${response.status()}`);
                continue;
            }
            const body = await response.text();
            // "Ready" that answers an empty document is worse than unready: the
            // UI renders a blank card and the user reads it as "nothing here".
            if (body.trim().length < 3 || body === "{}" || body === "null") {
                empty.push(`${panel.key}: ${body.slice(0, 60)}`);
            }
        }

        expect(empty, "these panels report themselves ready but return nothing, so their cards render blank").toEqual([]);
    });

    test("a panel this account may not see is hidden, not refused", async ({ campus }) => {
        // The anti-enumeration rule, which is a security property rather than a
        // convenience: a 403 on a subscription-gated panel would let anyone read
        // off the full list of what a subscription includes.
        const listed = await panels(campus);
        const keys = new Set(listed.map((panel) => panel.key));
        test.skip(keys.has("epa_echo"), "this account can see the nearby-research panels, so there is no hidden one to check.");

        const response = await campus.api.get(`pins/${campus.pin.slug}/panels/epa_echo/`);

        expect(
            response.status(),
            `a panel omitted from the list answered ${response.status()}. It must be 404: a 403 would confirm the panel exists and is ` +
                "merely withheld, which turns this route into a directory of paid features",
        ).toBe(404);
    });
});
