/**
 * Nothing one account does changes another account's private pin.
 *
 * The secondary account (B) keeps a pin with its own note, alias and link. The primary account (A) then pins beside
 * it and does everything an account does there: edits its pin's fields and type, adds notes, aliases
 * and links, nests a building child pin on the spot, edits the shared wiki and comments on it, and deletes it all.
 * B's pin record is then read repeatedly while A's background work (enrichment, the campus sweep) lands, and has to
 * match what it was before, field for field.
 *
 * The fields B's pin reads from the shared Location (address and its parts, the place's official name, the
 * boundary) may fill in from anyone's lookups and are left out. Everything the pin row holds is compared.
 */

import { expect, ifSecondaryAccount, test } from "../../lib/fixtures.js";
import { resourceName } from "../../lib/env.js";
import { locationSlugOf } from "../../lib/wiki.js";
import type { ApiClient } from "../../lib/api-client.js";

/** Read from the pin's shared Location, which other accounts' lookups may improve. */
const SHARED_LOCATION_FIELDS = ["address", "official_name", "city", "state", "county", "country", "zipcode", "boundary"];

/** How long A's background work gets to reach B's pin, and how often B's pin is read meanwhile. */
const SETTLE_MS = 60_000;
/** About 5 m north. */
const NEIGHBOURING_DEG = 0.000045;
const SAMPLE_MS = 10_000;

type PinRecord = Record<string, unknown>;

async function recordOf(api: ApiClient, slug: string): Promise<PinRecord> {
    const record = await api.json<PinRecord>("get", `pins/${slug}/`);
    for (const field of SHARED_LOCATION_FIELDS) {
        delete record[field];
    }
    return record;
}

async function ok(response: { ok(): boolean; status(): number; text(): Promise<string> }, what: string): Promise<void> {
    expect(response.ok(), `${what} answered ${response.status()}: ${(await response.text()).slice(0, 200)}`).toBeTruthy();
}

test.describe("another account's activity", () => {
    ifSecondaryAccount()("leaves my private pin exactly as it was", async ({ api, secondaryApi }) => {
        test.slow();

        const mine = await secondaryApi.createPin({ name: resourceName("my private pin") });
        await ok(await secondaryApi.post(`pins/${mine.slug}/notes/`, { text: resourceName("my note") }), "B's note");
        await ok(await secondaryApi.post(`pins/${mine.slug}/aliases/`, { name: resourceName("my alias") }), "B's alias");
        await ok(await secondaryApi.post(`pins/${mine.slug}/links/`, { url: "https://example.invalid/mine", name: resourceName("my link") }), "B's link");
        const before = await recordOf(secondaryApi, mine.slug);

        // A's own pin a few metres away, so its building child can stand exactly on my coordinate (P181).
        const theirs = await api.createPin({ name: resourceName("their pin beside my spot"), latitude: mine.latitude + NEIGHBOURING_DEG, longitude: mine.longitude });
        await ok(
            await api.patch(`pins/${theirs.slug}/`, {
                name: resourceName("their renamed pin"),
                description: "Edited by another account.",
                pin_type: "building",
                priority: 4,
                danger: 3,
                vulnerability: 2,
                date_built: "1890-01-01",
                color: "#123456",
            }),
            "A's edit",
        );
        await ok(await api.post(`pins/${theirs.slug}/notes/`, { text: resourceName("their note") }), "A's note");
        await ok(await api.post(`pins/${theirs.slug}/aliases/`, { name: resourceName("their alias") }), "A's alias");
        await ok(await api.post(`pins/${theirs.slug}/links/`, { url: "https://example.invalid/theirs", name: resourceName("their link") }), "A's link");

        const child = await api.json<{ slug: string }>("post", "pins/", {
            name: resourceName("their building on my spot"),
            name_is_user_provided: true,
            latitude: mine.latitude,
            longitude: mine.longitude,
            pin_type: "building",
            parent_id: theirs.uuid,
        });
        api.track("pin", child.slug, () => api.delete(`pins/${child.slug}/`));

        const shared = await locationSlugOf(api, theirs.slug);
        const wiki = await api.get(`wikis/${shared}/`);
        if (wiki.ok()) {
            await ok(await api.patch(`wikis/${shared}/`, { description: "Edited by another account." }), "A's wiki edit");
            await ok(await api.post(`wikis/${shared}/comments/`, { text: resourceName("their comment") }), "A's wiki comment");
        }

        await ok(await api.delete(`pins/${child.slug}/`), "deleting A's child pin");
        await ok(await api.delete(`pins/${theirs.slug}/`), "deleting A's pin");

        const deadline = Date.now() + SETTLE_MS;
        do {
            expect(await recordOf(secondaryApi, mine.slug), "another account's activity changed my private pin").toEqual(before);
            await new Promise((resolve) => setTimeout(resolve, SAMPLE_MS));
        } while (Date.now() < deadline);
        expect(await recordOf(secondaryApi, mine.slug), "another account's background work changed my private pin").toEqual(before);
    });
});
