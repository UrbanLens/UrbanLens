/**
 * Private data must not leak through any *other* surface than its own URL.
 *
 * Object-level 404s (authorization.spec.ts) are necessary and not sufficient.
 * Search, the map JSON the browser fetches, the wiki of a location the
 * stranger has not pinned, photo bytes, and HTML of a page the stranger can
 * actually load are all separate read paths, and a row that is 404 at
 * `pins/{slug}/` can still appear in `search/?q=` or `/dashboard/map/pins/`.
 *
 * The control in each case is that the *owner* can see the thing on that
 * same surface, so an empty result for the stranger is not "search is
 * broken".
 */

import { expect, ifSecondaryAccount, test } from "../../lib/fixtures.js";
import { apiUrl, resourceName } from "../../lib/env.js";
import { appRoutes, mapDataRoutes, pinDetail, shellFragmentRoutes } from "../../lib/routes.js";
import {
    anonymousContext,
    containsMarker,
    expectIndistinguishableFromMissing,
    expectNotServerError,
    expectRefused,
    MISSING_UUID,
    uniqueMarker,
    wasRefused,
    whoami,
} from "../../lib/security.js";

interface SearchResponse {
    total?: number;
    groups?: unknown[];
}

test.describe("search does not return another account's private rows", () => {
    ifSecondaryAccount()("a unique pin name is findable by its owner and by nobody else", async ({ api, secondaryApi }) => {
        const marker = uniqueMarker("srch");
        const pin = await api.createPin({ name: `${resourceName("searchable")} ${marker}` });

        const mine = await api.get("search/", { q: marker });
        expect(mine.status()).toBe(200);
        const mineBody = (await mine.json()) as SearchResponse;
        expect(
            JSON.stringify(mineBody),
            `the owner searching for a pin they just created got no hit. total=${mineBody.total}`,
        ).toContain(pin.slug);

        const theirs = await secondaryApi.get("search/", { q: marker });
        expect(theirs.status()).toBe(200);
        const theirsBody = await theirs.text();
        expect(theirsBody, "another account's search results include this account's pin slug").not.toContain(pin.slug);
        expect(theirsBody, "another account's search results include this account's pin uuid").not.toContain(pin.uuid);
        expect(containsMarker(theirsBody, marker), "another account's search results include the unique name marker").toBeFalsy();
    });

    test("an unauthenticated search is refused", async ({ anonymousApi }) => {
        const response = await anonymousApi.get("search/", { q: "anything" });
        expect(response.status(), "unauthenticated search was served").toBe(401);
    });
});

test.describe("the map JSON is scoped to the signed-in user", () => {
    ifSecondaryAccount()("another account's map payload does not name this account's pin", async ({ api, page, secondaryPage, guard }) => {
        guard.allow(/wikipedia|comments|panels|weather/i);
        const marker = uniqueMarker("map");
        const pin = await api.createPin({ name: `${resourceName("on the map")} ${marker}` });

        const mine = await page.request.get(mapDataRoutes.pins);
        expect(mine.status(), `the owner's map pins JSON answered ${mine.status()}`).toBe(200);
        const mineBody = await mine.text();
        expect(mineBody, "the owner's map JSON does not include a pin they just created, so the stranger's empty result would prove nothing").toContain(pin.slug);

        const theirs = await secondaryPage.request.get(mapDataRoutes.pins);
        expect(theirs.status()).toBe(200);
        const theirsBody = await theirs.text();
        expect(theirsBody, "another account's map JSON includes this account's pin slug").not.toContain(pin.slug);
        expect(theirsBody, "another account's map JSON includes this account's pin uuid").not.toContain(pin.uuid);
        expect(containsMarker(theirsBody, marker), "another account's map JSON includes the unique name marker").toBeFalsy();
    });

    test("an anonymous client is not served the map pin feed", async ({ browser }) => {
        const context = await anonymousContext(browser);
        try {
            const response = await context.request.get(mapDataRoutes.pins, { maxRedirects: 0 });
            await expectRefused(response, "GET /dashboard/map/pins/ without a session");
            if (response.status() === 200) {
                expect(await response.text(), "anonymous map JSON included pin rows").not.toMatch(/"slug"\s*:/);
            }
        } finally {
            await context.close();
        }
    });
});

test.describe("HTML pages do not leak another account's private names", () => {
    ifSecondaryAccount()("the signed-in shell does not embed another account's pin name", async ({ api, secondaryPage, guard }) => {
        guard.allow(/wikipedia|comments|panels|weather/i);
        const marker = uniqueMarker("html");
        await api.createPin({ name: `${resourceName("html isolation")} ${marker}` });

        await secondaryPage.goto(appRoutes.map);
        const html = await secondaryPage.content();
        expect(containsMarker(html, marker), "the other account's map HTML contains this account's unique pin marker").toBeFalsy();

        await secondaryPage.goto(appRoutes.home);
        const home = await secondaryPage.content();
        expect(containsMarker(home, marker), "the other account's home HTML contains this account's unique pin marker").toBeFalsy();
    });

    ifSecondaryAccount()("a pin detail URL is not a window into another account's pin", async ({ api, secondaryPage, page, guard }) => {
        guard.allow(/wikipedia|comments|panels|weather/i);
        const marker = uniqueMarker("pdet");
        const pin = await api.createPin({ name: `${resourceName("detail isolation")} ${marker}` });

        await page.goto(pinDetail(pin.slug));
        await expect(page.locator("#pin-detail-hero")).toBeVisible();
        expect(containsMarker(await page.content(), marker), "the owner's pin detail page does not show the name, so the stranger's miss would prove nothing").toBeTruthy();

        const response = await secondaryPage.goto(pinDetail(pin.slug));
        const status = response?.status() ?? 0;
        const html = await secondaryPage.content();
        const url = secondaryPage.url();

        const looksMissing =
            status === 404 ||
            url.includes("/accounts/login") ||
            /not found|doesn't exist|do not have access/i.test(html);
        expect(
            looksMissing,
            `another account loaded this pin's detail page (status ${status}, url ${url})`,
        ).toBeTruthy();
        expect(containsMarker(html, marker), "another account's 404/login page still contains the private pin marker").toBeFalsy();
    });
});

test.describe("photos stay with their uploader", () => {
    ifSecondaryAccount()("another account cannot read a private photo's metadata or file", async ({ api, secondaryApi, apiRequestContext, account }) => {
        test.skip(!account.apiKey, "No API key on the primary account.");
        const marker = uniqueMarker("pho");
        const pin = await api.createPin({ name: resourceName("photo isolation") });
        const png = Buffer.concat([
            Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64"),
            Buffer.from(`\n${marker}`, "utf-8"),
        ]);

        const upload = await apiRequestContext.post(apiUrl("photos/"), {
            headers: { Authorization: `Bearer ${account.apiKey}` },
            multipart: {
                file: { name: "sec-photo.png", mimeType: "image/png", buffer: png },
                caption: marker,
                pin: pin.slug,
            },
        });
        test.skip(upload.status() === 503, `The malware scanner is unavailable: ${(await upload.text()).slice(0, 160)}`);
        expect(upload.status(), `upload answered ${upload.status()}: ${(await upload.text()).slice(0, 200)}`).toBeLessThan(300);
        const photo = (await upload.json()) as { uuid: string; url?: string };
        expect(photo.uuid).toBeTruthy();
        api.track("photo", photo.uuid, () => api.delete(`photos/${photo.uuid}/`));

        expect((await api.get(`photos/${photo.uuid}/`)).status(), "the owner cannot read the photo they uploaded").toBe(200);

        await expectIndistinguishableFromMissing(
            await secondaryApi.get(`photos/${photo.uuid}/`),
            await secondaryApi.get(`photos/${MISSING_UUID}/`),
            "another account's photo",
        );

        const file = await secondaryApi.get(`photos/${photo.uuid}/file/`);
        await expectNotServerError(file, "GET photos/{uuid}/file/ as a stranger");
        expect(wasRefused(file) || file.status() >= 400, `another account downloaded the photo file (${file.status()})`).toBeTruthy();

        const list = await secondaryApi.get("photos/");
        if (list.status() === 200) {
            expect(await list.text(), "another account's photo index lists this upload").not.toContain(photo.uuid);
            expect(await list.text()).not.toContain(marker);
        }
    });
});

test.describe("wikis of unpinned places are not an oracle", () => {
    ifSecondaryAccount()("a wiki the stranger has not earned answers 404, never 403", async ({ api, secondaryApi }) => {
        const pin = await api.createPin({ name: resourceName("wiki isolation") });
        const detail = await api.json<{ location_slug?: string }>("get", `pins/${pin.slug}/`);
        expect(detail.location_slug, "pin detail carried no location_slug").toBeTruthy();
        const slug = String(detail.location_slug);

        const ownerView = await api.get(`wikis/${slug}/`);
        expect([200, 404], `the owner's wiki answered ${ownerView.status()}`).toContain(ownerView.status());

        const stranger = await secondaryApi.get(`wikis/${slug}/`);
        await expectNotServerError(stranger, "wiki the stranger has not earned");
        expect(
            stranger.status(),
            `an unearned wiki answered ${stranger.status()} - 403 confirms the wiki exists, which leaks the location`,
        ).not.toBe(403);

        if (ownerView.status() === 200) {
            // A visible wiki the owner earned by pinning. The stranger, who
            // has not pinned this place, must not see it - and must not be
            // told it exists.
            expect(stranger.status(), "a stranger could read a wiki they have not earned").toBe(404);
        }
    });
});

test.describe("shell fragments require a session", () => {
    test("notification and message badges are not served anonymously", async ({ browser }) => {
        const context = await anonymousContext(browser);
        try {
            const leaks: string[] = [];
            for (const [name, path] of Object.entries(shellFragmentRoutes)) {
                const response = await context.request.get(path, { maxRedirects: 0 });
                if (!wasRefused(response)) {
                    leaks.push(`${name}: GET ${path} -> ${response.status()}`);
                }
            }
            expect(leaks, `anonymous clients were served signed-in shell fragments:\n  ${leaks.join("\n  ")}`).toHaveLength(0);
        } finally {
            await context.close();
        }
    });
});

test.describe("profile contact details stay off a stranger's payload", () => {
    ifSecondaryAccount()("whoami and a stranger profile do not carry the other account's email", async ({ api, secondaryApi, account }) => {
        const me = await whoami(api);
        const them = await whoami(secondaryApi);

        const theirWhoami = await secondaryApi.json<Record<string, unknown>>("get", "whoami/");
        expect(JSON.stringify(theirWhoami), "secondary whoami included this account's email").not.toContain(account.email);
        expect(JSON.stringify(theirWhoami), "secondary whoami included this account's slug").not.toContain(me.slug);

        const stranger = await api.get(`profiles/${them.slug}/`);
        // 404 is the documented answer for no relationship. A 200 must still
        // not include contact fields.
        if (stranger.status() === 200) {
            const body = await stranger.text();
            expect(body, "a stranger's profile payload includes an email address").not.toMatch(/@e2e\.invalid/);
            expect(body.toLowerCase(), "a stranger's profile payload includes a password").not.toContain("password");
        } else {
            expect(stranger.status()).toBe(404);
        }
    });
});
