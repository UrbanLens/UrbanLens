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
 * Panels a pin on this campus should be offered **on the external API**.
 *
 * That qualifier is the whole subtlety, and getting it wrong cost a cycle. A
 * panel appears in `GET pins/{slug}/panels/` only if its `api_kinds` is
 * non-empty, and the default on `PanelSource` is empty - "this panel is not
 * exposed on the external API". Two of the most obvious candidates opt out:
 *
 * - `property_records` sets `api_kinds = frozenset()` explicitly.
 * - `wikipedia` (the article summary panel) also reports `api_kinds=[]`.
 *
 * Both were verified by querying the running application rather than inferred
 * from the class hierarchy - reading the base classes suggested `wikipedia`
 * inherited `{INFO}`, and it does not. Expecting either here made this spec fail
 * against an application that was behaving correctly.
 *
 * So the web page, not this endpoint, is where to assert the Wikipedia summary
 * and the Ownership card. What follows are panels confirmed API-visible for this
 * pin, each tied to something the requirements actually ask about.
 *
 * Not asserted as a complete set - plugins get added and a deployment can turn
 * providers off, so demanding an exact list would fail for the wrong reason.
 * Missing ones are reported together, so the message names which integration
 * stopped contributing rather than just "the count changed".
 */
const EXPECTED_PANELS = [
    { key: "boundary", why: "every pin with coordinates has one" },
    { key: "parcel_buildings", why: "REData lists 33 CRIS buildings on this parcel" },
    { key: "epa_echo_detail", why: "this is a regulated site and the panel is ungated" },
    { key: "wikipedia_media", why: "the site has a substantial Wikipedia presence" },
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

    test("the panels backing this place's own data become ready", async ({ campus }) => {
        // Scoped to EXPECTED_PANELS rather than to everything offered, and the
        // reason is a measurement rather than a preference: of the 33 panels
        // offered on this pin, 21 become ready and 12 do not - and the 12 are
        // overwhelmingly providers this deployment holds no credentials for
        // (flickr, azure_maps, searxng_images, gdelt, inaturalist,
        // census_tigerweb). They are *offered* because their gate only asks
        // whether the pin has coordinates, and they can never become ready
        // without configuration.
        //
        // So "every offered panel becomes ready" is not an invariant of the
        // application at all; it is a statement about how one deployment is
        // configured, and asserting it fails against a healthy instance. What is
        // an invariant is that the panels backing this place's own data fill in.
        const settled = await waitForOrNull(
            () => panels(campus),
            (listed) => EXPECTED_PANELS.every((expected) => listed.find((panel) => panel.key === expected.key)?.ready),
            {
                what: "the panels backing this place's data to become ready",
                timeoutMs: PANEL_READY_TIMEOUT_MS,
                intervalMs: 15_000,
                describe: (listed) => {
                    const stuck = EXPECTED_PANELS.filter((expected) => !listed.find((panel) => panel.key === expected.key)?.ready).map((expected) => expected.key);
                    return `still pending: ${stuck.join(", ") || "none"}`;
                },
            },
        );

        if (settled === null) {
            const listed = await panels(campus);
            const stuck = EXPECTED_PANELS.filter((expected) => !listed.find((panel) => panel.key === expected.key)?.ready).map((expected) => expected.key);
            expect(
                stuck,
                "these panels never became ready. They are fetched by fetch_panel_source on the panel_fetch queue - the default celery " +
                    "worker does not consume it, so a deployment without celery-worker-panels leaves them pending indefinitely with " +
                    "nothing in the UI to say so. Rule that out before treating it as a provider problem",
            ).toEqual([]);
        }
    });

    test("how many panels are stuck is reported, not asserted", async ({ campus }, testInfo) => {
        // Deliberately cannot fail on the stuck set. That set is genuinely
        // useful - it is how you notice a provider that used to work has stopped
        // - but it is also legitimately non-empty on any deployment missing
        // third-party credentials, and a test that is red on every run gets
        // muted and then catches nothing. Attached to the report instead, where
        // it can be read when something else prompts the question.
        const listed = await panels(campus);
        const stuck = listed.filter((panel) => !panel.ready).map((panel) => panel.key);

        await testInfo.attach("panels-not-ready.txt", {
            body:
                `${listed.length - stuck.length}/${listed.length} panels ready.\n\n` +
                `Not ready (${stuck.length}): ${stuck.join(", ") || "none"}\n\n` +
                "A panel is offered when its gate passes - which for most of these only asks whether the pin has coordinates - so a " +
                "provider with no API key configured is offered and never becomes ready. That is a deployment fact, not a defect. " +
                "Compare against a previous run to spot one that has regressed.",
            contentType: "text/plain",
        });

        expect(listed.length, "no panels at all are offered, which is a different and much worse problem").toBeGreaterThan(0);
    });

    test("a panel that is ready answers something a client can parse", async ({ campus }) => {
        const listed = await panels(campus);
        const ready = listed.filter((panel) => panel.ready);
        test.skip(ready.length === 0, "no panel is ready yet - see the previous test.");

        const broken: string[] = [];
        for (const panel of ready) {
            const response = await campus.api.get(`pins/${campus.pin.slug}/panels/${panel.key}/`);
            if (!response.ok()) {
                broken.push(`${panel.key}: HTTP ${response.status()}`);
                continue;
            }
            // 204 is a documented, correct answer here, not an empty one to
            // complain about: `PinPanelDetailView` returns "the web panel's
            // quiet 204" when a ready source has nothing to show for this place,
            // and declares `204: None` in its own schema. Several sources reach
            // it legitimately - there is no NPS park at these coordinates, and
            // no Overture building attributes.
            //
            // Two earlier versions of this test were wrong about exactly this:
            // one treated `{}` as a defect, the next called JSON.parse on a 204
            // body and reported six healthy panels as broken. What actually has
            // to hold is narrower - a panel that answers 200 must answer
            // parseable JSON, because a 200 is a promise of a document.
            if (response.status() === 204) {
                continue;
            }
            const body = await response.text();
            try {
                JSON.parse(body);
            } catch {
                broken.push(`${panel.key}: HTTP ${response.status()} but not JSON - ${JSON.stringify(body.slice(0, 60))}`);
            }
        }

        expect(broken, "these panels report themselves ready but do not answer parseable JSON, so a client cannot render them at all").toEqual([]);
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
