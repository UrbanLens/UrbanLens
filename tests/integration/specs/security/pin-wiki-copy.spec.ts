/**
 * Pin data reaches a wiki only as an opt-in, per-field copy, never as a live reference
 * (docs/GOALS.md, "Privacy model": "Pin → wiki", and the litmus test).
 */

import type { APIRequestContext, APIResponse } from "@playwright/test";

import type { ApiClient } from "../../lib/api-client.js";
import { expect, ifSecondaryAccount, test } from "../../lib/fixtures.js";
import { apiUrl, resourceName } from "../../lib/env.js";
import { expectIndistinguishableFromMissing, MISSING_UUID, uniqueMarker } from "../../lib/security.js";
import { waitFor } from "../../lib/waiting.js";
import { csrfHeaders, locationSlugOf, pinWithWiki, placelessCoordinates, waitForWiki, type WikiDetail } from "../../lib/wiki.js";

interface StatComposite {
    rounded: number | null;
    exact: number | null;
    my_vote: number | null;
}

interface SharedWiki {
    ownerPinSlug: string;
    locationSlug: string;
    wiki: WikiDetail;
    ownerPinWikiSlug: string | null;
}

/**
 * A wiki `viewer` earned first, then `owner` pinning the same point.
 *
 * The order matters: a pin is linked to its wiki only when the wiki already exists at creation
 * (services/pins/pin_creation.py), and the pin-to-wiki stat sync runs only through that link.
 */
async function sharedWikiWithLinkedPin(owner: ApiClient, viewer: ApiClient, label: string): Promise<SharedWiki> {
    const point = placelessCoordinates();
    const viewerPin = await viewer.createPin({ name: resourceName(`${label} viewer`), ...point });
    const locationSlug = await locationSlugOf(viewer, viewerPin.slug);
    const wiki = await waitForWiki(viewer, locationSlug);
    const ownerPin = await owner.createPin({ name: resourceName(`${label} owner`), ...point });
    const detail = await owner.json<{ location_slug: string; wiki_slug: string | null }>("get", `pins/${ownerPin.slug}/`);
    expect(detail.location_slug, "the owner's pin at the same coordinates resolved a different Location, so it is not on the viewer's wiki at all").toBe(locationSlug);
    return { ownerPinSlug: ownerPin.slug, locationSlug, wiki, ownerPinWikiSlug: detail.wiki_slug };
}

async function composite(api: ApiClient, locationSlug: string, field: string): Promise<StatComposite> {
    return api.json<StatComposite>("get", `wikis/${locationSlug}/votes/${field}/`);
}

function publicPart(stat: StatComposite): { rounded: number | null; exact: number | null } {
    return { rounded: stat.rounded, exact: stat.exact };
}

async function expectOk(response: APIResponse, what: string): Promise<void> {
    expect(response.ok(), `${what} answered ${response.status()}: ${(await response.text()).slice(0, 200)}`).toBeTruthy();
}

/** Uploads a 1x1 PNG onto `pinSlug` and returns its uuid, skipping when the malware scanner is down. */
async function uploadPhoto(apiRequestContext: APIRequestContext, apiKey: string, pinSlug: string, marker: string): Promise<string> {
    const png = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");
    const upload = await apiRequestContext.post(apiUrl("photos/"), {
        headers: { Authorization: `Bearer ${apiKey}` },
        multipart: { file: { name: `${marker}.png`, mimeType: "image/png", buffer: png }, caption: marker, pin: pinSlug },
    });
    test.skip(upload.status() === 503, `The malware scanner is unavailable: ${(await upload.text()).slice(0, 160)}`);
    expect(upload.status(), `photo upload answered ${upload.status()}: ${(await upload.text()).slice(0, 200)}`).toBeLessThan(300);
    const photo = (await upload.json()) as { uuid?: string };
    expect(photo.uuid, "the upload response carried no uuid").toBeTruthy();
    return String(photo.uuid);
}

async function galleryText(api: ApiClient, locationSlug: string): Promise<string> {
    const response = await api.get(`wikis/${locationSlug}/gallery/`);
    expect(response.status(), `wikis/{slug}/gallery/ answered ${response.status()}`).toBe(200);
    return response.text();
}

test.describe("the auto-created wiki is not a window into the pin that caused it", () => {
    ifSecondaryAccount()("the auto-created wiki carries neither the pin's private name nor its description", async ({ api, secondaryApi, secondaryPage }) => {
        test.slow();
        const nameMarker = uniqueMarker("pwcname");
        const descriptionMarker = uniqueMarker("pwcdesc");
        const { pin, locationSlug } = await pinWithWiki(api, {
            name: `${resourceName("private pin name")} ${nameMarker}`,
            description: `Private notes ${descriptionMarker}`,
        });

        const ownPin = await (await api.get(`pins/${pin.slug}/`)).text();
        expect(ownPin, "the owner's own pin does not carry the name marker, so its absence from the wiki would prove nothing").toContain(nameMarker);
        expect(ownPin, "the owner's own pin does not carry the description marker, so its absence from the wiki would prove nothing").toContain(descriptionMarker);

        await secondaryApi.createPin({ name: resourceName("wiki copy visitor"), latitude: pin.latitude, longitude: pin.longitude });
        const wiki = await secondaryApi.get(`wikis/${locationSlug}/`);
        expect(wiki.status(), `the visitor who pinned the same point could not read the wiki (${wiki.status()}), so every miss below would be vacuous`).toBe(200);

        const surfaces: Array<[string, string]> = [["wikis/{slug}/", await wiki.text()]];
        for (const route of ["aliases/", "history/", "article/", "comments/"]) {
            const response = await secondaryApi.get(`wikis/${locationSlug}/${route}`);
            expect([200, 404], `wikis/{slug}/${route} answered ${response.status()} to a member`).toContain(response.status());
            surfaces.push([`wikis/{slug}/${route}`, await response.text()]);
        }
        const page = await secondaryPage.request.get(`/dashboard/location/${locationSlug}/wiki/`);
        expect(page.status(), `the wiki page answered ${page.status()} to a member`).toBe(200);
        surfaces.push(["the wiki page", await page.text()]);

        for (const [surface, body] of surfaces) {
            expect(body, `${surface} carries the private pin's name`).not.toContain(nameMarker);
            expect(body, `${surface} carries the private pin's description`).not.toContain(descriptionMarker);
        }

        expect(await (await api.get("search/", { q: nameMarker })).text(), "the owner's search cannot find their own pin by name, so the member's miss below would prove nothing").toContain(pin.slug);
        const theirSearch = (await (await secondaryApi.get("search/", { q: nameMarker })).json()) as { groups?: unknown[] };
        expect(JSON.stringify(theirSearch.groups ?? []), "a wiki member found the wiki by the private pin's name, so the name reached the wiki").not.toContain(locationSlug);
    });
});

test.describe("editing a pin does not change what other people see on its wiki", () => {
    ifSecondaryAccount()("precondition: a pin made after its wiki is linked to it, and the vote endpoint reflects changes", async ({ api, secondaryApi }) => {
        test.slow();
        const shared = await sharedWikiWithLinkedPin(api, secondaryApi, "wiki sync precondition");
        expect(shared.ownerPinWikiSlug, "the owner's pin was not linked to the wiki that already existed at its location, so the sync test has nothing to sync through").toBeTruthy();
        expect(shared.ownerPinWikiSlug, "the owner's pin is linked to some other wiki").toBe(shared.wiki.wiki_slug);

        for (const field of ["danger", "rating"]) {
            const cast = await secondaryApi.put(`wikis/${shared.locationSlug}/votes/${field}/`, { value: 3 });
            await expectOk(cast, `the viewer casting their own ${field} vote`);
            expect(((await cast.json()) as StatComposite).my_vote, `the ${field} endpoint did not reflect the viewer's own vote, so an unchanged reading would prove nothing`).toBe(3);
            expect((await composite(secondaryApi, shared.locationSlug, field)).exact, `the ${field} composite ignored a vote that was just cast`).not.toBeNull();
            await expectOk(await secondaryApi.delete(`wikis/${shared.locationSlug}/votes/${field}/`), `the viewer withdrawing their ${field} vote`);
            expect((await composite(secondaryApi, shared.locationSlug, field)).my_vote, `withdrawing the ${field} vote did not clear it`).toBeNull();
        }

        await expectOk(await api.patch(`pins/${shared.ownerPinSlug}/`, { danger: 2 }), "the owner setting their pin's danger");
        await expectOk(await api.put(`pins/${shared.ownerPinSlug}/review/`, { rating: 4 }), "the owner rating their pin");
        expect((await api.json<{ rating: number }>("get", `pins/${shared.ownerPinSlug}/review/`)).rating, "the owner's rating did not persist").toBe(4);

        const settings = await api.json<Record<string, unknown>>("get", "settings/");
        test.info().annotations.push({
            type: "wiki-sync-settings",
            description: `sync_danger_to_wiki=${String(settings.sync_danger_to_wiki)} sync_rating_to_wiki=${String(settings.sync_rating_to_wiki)}`,
        });
    });

    ifSecondaryAccount()("editing a pin's danger and rating, with no per-field opt-in, never changes another user's view of the wiki", async ({ api, secondaryApi }) => {
        test.info().annotations.push({
            type: "goals-conflict",
            description:
                "GOALS: 'Pin → wiki: sender opts in per field; the wiki gets a copy' and 'if editing a pin's fields ever causes a different user's view ... to change, that's a bug' vs models/profile/model.py:586-589 (sync_*_to_wiki default True) and models/pin/signals.py:244,276 (every pin edit live-mirrored into WikiStatVote, read back exactly by external_api/views_wiki.py:300)",
        });
        test.fail();
        test.slow();
        const shared = await sharedWikiWithLinkedPin(api, secondaryApi, "wiki sync litmus");

        const before = {
            danger: publicPart(await composite(secondaryApi, shared.locationSlug, "danger")),
            rating: publicPart(await composite(secondaryApi, shared.locationSlug, "rating")),
        };

        for (const value of [5, 1]) {
            await expectOk(await api.patch(`pins/${shared.ownerPinSlug}/`, { danger: value }), `the owner setting danger ${value}`);
            await expectOk(await api.put(`pins/${shared.ownerPinSlug}/review/`, { rating: value }), `the owner rating ${value}`);
            expect.soft(
                publicPart(await composite(secondaryApi, shared.locationSlug, "danger")),
                `after the owner set their private danger to ${value}, another member's view of the wiki's danger changed`,
            ).toEqual(before.danger);
            expect.soft(
                publicPart(await composite(secondaryApi, shared.locationSlug, "rating")),
                `after the owner rated their private pin ${value}, another member's view of the wiki's rating changed`,
            ).toEqual(before.rating);
        }
    });
});

test.describe("a photo shared to a wiki is a copy the owner's library cannot take back", () => {
    ifSecondaryAccount()("a photo shared to the wiki survives the owner deleting it from their library", async ({ api, page, secondaryApi, apiRequestContext, account }) => {
        test.slow();
        test.skip(!account.apiKey, "No API key on the primary account.");
        const marker = uniqueMarker("pwcphoto");
        const { pin, locationSlug } = await pinWithWiki(api, { name: resourceName("wiki photo copy") });
        const photoUuid = await uploadPhoto(apiRequestContext, account.apiKey as string, pin.slug, marker);
        api.track("photo", photoUuid, () => api.delete(`photos/${photoUuid}/?from_wiki=true`));

        await secondaryApi.createPin({ name: resourceName("wiki photo visitor"), latitude: pin.latitude, longitude: pin.longitude });
        expect(await galleryText(secondaryApi, locationSlug), "a pin photo nobody shared already appears in the wiki gallery").not.toContain(photoUuid);
        await expectIndistinguishableFromMissing(await secondaryApi.get(`photos/${photoUuid}/`), await secondaryApi.get(`photos/${MISSING_UUID}/`), "an unshared pin photo, read by a wiki member");

        const shareUrl = `/dashboard/map/pin/${pin.slug}/wiki/share/`;
        const dialog = await page.request.get(shareUrl);
        expect(dialog.status(), `the share-to-wiki dialog answered ${dialog.status()}`).toBe(200);
        const imageId = /name="image_ids"\s+value="(\d+)"/.exec(await dialog.text())?.[1];
        expect(imageId, "the share-to-wiki dialog offered no photo to pick").toBeTruthy();

        const headers = await csrfHeaders(page);
        // Only processed uploads are shareable (wiki_share._processed_photo_ids); until then the POST answers shared=false.
        await waitFor(
            async () => {
                const response = await page.request.post(shareUrl, { headers, form: { image_ids: String(imageId) } });
                return { status: response.status(), trigger: response.headers()["hx-trigger"] ?? "" };
            },
            (seen) => {
                if (seen.status !== 200) {
                    throw new Error(`sharing the photo to the wiki answered ${seen.status}`);
                }
                return /"shared":\s*true/.test(seen.trigger);
            },
            { what: "the photo becoming shareable once upload processing finished", timeoutMs: 90_000, intervalMs: 3_000, describe: (seen) => `HX-Trigger ${seen.trigger}` },
        );

        await waitFor(
            () => galleryText(secondaryApi, locationSlug),
            (body) => body.includes(photoUuid),
            { what: "the shared photo appearing in the wiki gallery for another member", timeoutMs: 60_000, intervalMs: 3_000, describe: (body) => body.slice(0, 200) },
        );

        const deleted = await api.delete(`photos/${photoUuid}/`);
        expect(deleted.status(), `the owner deleting the photo from their library answered ${deleted.status()}`).toBe(204);

        expect(await galleryText(secondaryApi, locationSlug), "deleting the photo from the owner's library also took the wiki's copy away").toContain(photoUuid);
        expect((await secondaryApi.get(`photos/${photoUuid}/`)).status(), "the wiki's copy of the photo is no longer readable by a member after the owner's library delete").toBe(200);
    });
});
