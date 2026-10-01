/**
 * No search surface returns another account's rows. Each seed puts a unique token into one private
 * field; the owner must find it (the control), then the stranger must not, on every surface that
 * searches that field.
 *
 * Every seed's text is `<token> <secret>`: only the token is ever queried, so the secret can only
 * reach a response through a result row, never through a query echo.
 */

import type { Page } from "@playwright/test";

import type { ApiClient, CreatedPin } from "../../lib/api-client.js";
import { expect, ifSecondaryAccount, test } from "../../lib/fixtures.js";
import { resourceName } from "../../lib/env.js";
import { createChild, createLabel, createList, createTrip, isUnavailable, logVisit, middayUtc, openCheckin, randomMarker, uploadPhoto } from "../../lib/object-factories.js";

const SEARCH_PANEL = "/dashboard/search/panel/";
const AUTOCOMPLETE_LOCAL = "/dashboard/map/search/autocomplete/local/";

interface Seed {
    what: string;
    token: string;
    /** Identifiers of the owner's object the token should surface. */
    ids: string[];
}

type Surface = "api search" | "web search panel" | "api location search" | "web autocomplete";

interface Scope {
    api: ApiClient;
    secondaryApi: ApiClient;
    page: Page;
    secondaryPage: Page;
    secret: string;
    /** Carried by the owner's object names; a leaked row names it even when its snippet does not. */
    nameMarker: string;
}

/** Only the parts of a response that are result rows; echoes of the query are dropped. */
async function rowsText(surface: Surface, client: ApiClient | Page, token: string): Promise<{ status: number; text: string }> {
    const q = encodeURIComponent(token);
    switch (surface) {
        case "api search": {
            const response = await (client as ApiClient).get("search/", { q: token });
            const body = (await response.json().catch(() => ({}))) as { groups?: unknown };
            return { status: response.status(), text: JSON.stringify(body.groups ?? []) };
        }
        case "api location search": {
            const response = await (client as ApiClient).get("locations/search/", { q: token, sources: "local" });
            const body = (await response.json().catch(() => ({}))) as { results?: unknown };
            return { status: response.status(), text: JSON.stringify(body.results ?? []) };
        }
        case "web autocomplete": {
            const response = await (client as Page).request.get(`${AUTOCOMPLETE_LOCAL}?q=${q}`);
            const body = (await response.json().catch(() => ({}))) as { results?: unknown };
            return { status: response.status(), text: JSON.stringify(body.results ?? []) };
        }
        case "web search panel": {
            // HTML echoes the query, so the token itself is masked and only the secret and ids count.
            const response = await (client as Page).request.get(`${SEARCH_PANEL}?q=${q}`);
            return { status: response.status(), text: (await response.text()).split(token).join("[query]") };
        }
    }
}

/**
 * Runs every seed through every surface: owner control first, then the stranger.
 *
 * @returns One line per failure, so a single run reports every leaking provider rather than the first.
 */
async function probe(scope: Scope, seeds: Seed[], surfaces: Surface[]): Promise<string[]> {
    const problems: string[] = [];
    for (const seed of seeds) {
        for (const surface of surfaces) {
            const owner = surface.startsWith("api") ? scope.api : scope.page;
            const stranger = surface.startsWith("api") ? scope.secondaryApi : scope.secondaryPage;

            const mine = await rowsText(surface, owner, seed.token);
            const ownerHit = [seed.token, scope.secret, scope.nameMarker, ...seed.ids].some((needle) => mine.text.includes(needle));
            if (mine.status !== 200 || !ownerHit) {
                problems.push(`${seed.what} via ${surface}: the owner's own search (HTTP ${mine.status}) does not find it, so the stranger's miss would prove nothing`);
                continue;
            }

            const theirs = await rowsText(surface, stranger, seed.token);
            if (theirs.status >= 500) {
                problems.push(`${seed.what} via ${surface}: the stranger's search crashed (HTTP ${theirs.status})`);
                continue;
            }
            const leaked = [seed.token, scope.secret, scope.nameMarker, ...seed.ids].filter((needle) => theirs.text.includes(needle));
            if (leaked.length > 0) {
                problems.push(`${seed.what} via ${surface}: another account's search returned ${leaked.join(", ")}`);
            }
        }
    }
    return problems;
}

function makeScope(api: ApiClient, secondaryApi: ApiClient, page: Page, secondaryPage: Page): Scope {
    return { api, secondaryApi, page, secondaryPage, secret: randomMarker("secret"), nameMarker: randomMarker("name") };
}

async function scopedPin(scope: Scope, extra: { description?: string } = {}): Promise<CreatedPin> {
    return scope.api.createPin({ name: `${resourceName("search scope")} ${scope.nameMarker}`, ...extra });
}

test.describe("no search provider returns another account's rows", () => {
    ifSecondaryAccount()("a pin's description, aliases, notes and labels are found only by its owner", async ({ api, secondaryApi, page, secondaryPage }) => {
        const scope = makeScope(api, secondaryApi, page, secondaryPage);
        const tokens = { description: randomMarker("desc"), alias: randomMarker("alias"), note: randomMarker("note"), label: randomMarker("label") };

        const pin = await scopedPin(scope, { description: `${tokens.description} ${scope.secret}` });
        await createChild(api, `pins/${pin.slug}/aliases/`, { name: `${tokens.alias} ${scope.secret}` });
        await createChild(api, `pins/${pin.slug}/notes/`, { text: `${tokens.note} ${scope.secret}` });
        const labelRow = await createLabel(api, `${tokens.label} ${scope.secret}`);
        const tagged = await api.patch(`pins/${pin.slug}/`, { label_uuids: [labelRow.uuid] });
        expect(tagged.status(), `labelling the pin answered ${tagged.status()}: ${(await tagged.text()).slice(0, 200)}`).toBe(200);

        const ids = [pin.slug, pin.uuid];
        const everywhere: Surface[] = ["api search", "web search panel", "api location search", "web autocomplete"];
        const problems = [
            ...(await probe(scope, [{ what: "a pin description", token: tokens.description, ids }], everywhere)),
            ...(await probe(scope, [{ what: "a pin alias", token: tokens.alias, ids }], everywhere)),
            ...(await probe(scope, [{ what: "a pin label", token: tokens.label, ids }], everywhere)),
            // The location searches match name, aliases, description and labels only (map_pins/autocomplete.py:76-86).
            ...(await probe(scope, [{ what: "a pin note", token: tokens.note, ids }], ["api search", "web search panel"])),
        ];
        expect(problems, problems.join("\n")).toEqual([]);
    });

    ifSecondaryAccount()("a pin's article, its comments and its visit notes are found only by its owner", async ({ api, secondaryApi, page, secondaryPage }) => {
        const scope = makeScope(api, secondaryApi, page, secondaryPage);
        const tokens = { article: randomMarker("art"), comment: randomMarker("cmt"), visit: randomMarker("visit") };

        const pin = await scopedPin(scope);
        await api.json("put", `pins/${pin.slug}/article/`, { content: `${tokens.article} ${scope.secret}`, base_revision_id: null, edit_summary: "search scope" });
        await createChild(api, `pins/${pin.slug}/comments/`, { text: `${tokens.comment} ${scope.secret}` });
        await logVisit(api, pin.slug, middayUtc(1), `${tokens.visit} ${scope.secret}`);

        const ids = [pin.slug, pin.uuid];
        const problems = await probe(
            scope,
            [
                { what: "a pin article", token: tokens.article, ids },
                { what: "a pin comment", token: tokens.comment, ids },
                { what: "a visit note", token: tokens.visit, ids },
            ],
            ["api search", "web search panel"],
        );
        expect(problems, problems.join("\n")).toEqual([]);
    });

    ifSecondaryAccount()("a trip, its activities and its comments are found only by its members", async ({ api, secondaryApi, page, secondaryPage }) => {
        const scope = makeScope(api, secondaryApi, page, secondaryPage);
        const tokens = { trip: randomMarker("trip"), activity: randomMarker("act"), comment: randomMarker("tcmt") };

        const trip = await createTrip(api, `${tokens.trip} ${scope.nameMarker}`);
        await createChild(api, `trips/${trip.slug}/activities/`, { title: `${tokens.activity} ${scope.secret}` });
        await createChild(api, `trips/${trip.slug}/comments/`, { text: `${tokens.comment} ${scope.secret}` });

        const problems = await probe(
            scope,
            [
                { what: "a trip name", token: tokens.trip, ids: [trip.slug] },
                { what: "a trip activity", token: tokens.activity, ids: [trip.slug] },
                { what: "a trip comment", token: tokens.comment, ids: [trip.slug] },
            ],
            ["api search", "web search panel"],
        );
        expect(problems, problems.join("\n")).toEqual([]);
    });

    ifSecondaryAccount()("photo captions, markup-map titles and check-in titles are found only by their owner", async ({ api, secondaryApi, page, secondaryPage, apiRequestContext }) => {
        const scope = makeScope(api, secondaryApi, page, secondaryPage);
        const tokens = { caption: randomMarker("cap"), map: randomMarker("map"), checkin: randomMarker("chk") };
        const pin = await scopedPin(scope);
        const seeds: Seed[] = [];

        // Markup maps have no create route of their own; a list's map is titled after the list
        // (services/map/map_snapshot.py default_markup_map_title).
        const list = await createList(api, `${tokens.map} ${scope.nameMarker}`);
        await api.json("post", `lists/${list.slug}/items/`, { pin_uuids: [pin.uuid] });
        const map = await api.json<{ markup_map_uuid: string }>("post", `lists/${list.slug}/markup-map/`);
        seeds.push({ what: "a markup-map title", token: tokens.map, ids: [String(map.markup_map_uuid)] });

        const photo = await uploadPhoto(apiRequestContext, api, pin.slug, `${tokens.caption} ${scope.secret}`);
        if (isUnavailable(photo)) {
            test.info().annotations.push({ type: "skipped-seed", description: `photo caption: ${photo.unavailable}` });
        } else {
            seeds.push({ what: "a photo caption", token: tokens.caption, ids: [photo.uuid] });
        }

        const checkin = await openCheckin(api, `${tokens.checkin} ${scope.secret}`);
        if (isUnavailable(checkin)) {
            test.info().annotations.push({ type: "skipped-seed", description: `check-in title: ${checkin.unavailable}` });
        } else {
            seeds.push({ what: "a check-in title", token: tokens.checkin, ids: [checkin.slug, checkin.uuid] });
        }

        const problems = await probe(scope, seeds, ["api search", "web search panel"]);
        expect(problems, problems.join("\n")).toEqual([]);
    });
});
