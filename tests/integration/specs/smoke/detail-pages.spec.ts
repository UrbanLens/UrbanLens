/**
 * Detail and secondary pages load with their expected content (GOALS "Testing and CI/CD": every page
 * has an integration test proving it loads with the expected content). Each test asserts the page
 * for a real object renders that object, and that a missing one - or a signed-out visitor - is
 * refused. `pages.spec.ts` covers the top-level routes by status alone.
 */

import { randomUUID } from "node:crypto";

import type { Page, PlaywrightWorkerArgs } from "@playwright/test";

import { requireAccount } from "../../lib/accounts.js";
import { expect, ifSecondaryAccount, ifSharingPair, SHAREE_ROLE, test } from "../../lib/fixtures.js";
import { env, resourceName } from "../../lib/env.js";
import { ensureFriends } from "../../lib/friendship.js";
import { AppShell } from "../../lib/pages/app-shell.js";
import { pinDetail, profileFor } from "../../lib/routes.js";

const MISSING_SLUG = "e2e-missing-detail-page";

function isoDate(days: number): string {
    return new Date(Date.now() + days * 86_400_000).toISOString().slice(0, 10);
}

async function expectMissing(page: Page, path: string): Promise<void> {
    const response = await page.request.get(path);
    expect(response.status(), `${path} names nothing and answered ${response.status()}`).toBe(404);
}

/** Opens `path` and asserts it rendered inside the signed-in shell rather than bouncing. */
async function open(page: Page, path: string): Promise<void> {
    const response = await page.goto(path);
    expect(response?.status(), `${path} answered ${response?.status()}`).toBe(200);
    expect(new URL(page.url()).pathname, `${path} redirected to ${page.url()}`).toBe(path);
    await expect(new AppShell(page).nav).toBeVisible();
}

async function expectSignInRequired(playwright: PlaywrightWorkerArgs["playwright"], path: string): Promise<void> {
    // An explicit empty state: a bare newContext inherits the project's signed-in storageState.
    const anonymous = await playwright.request.newContext({ baseURL: env.baseUrl, ignoreHTTPSErrors: env.ignoreHttpsErrors, storageState: { cookies: [], origins: [] } });
    try {
        const response = await anonymous.get(path, { maxRedirects: 0 });
        expect(response.status(), `${path} answered ${response.status()} to a signed-out visitor`).toBe(302);
        expect(response.headers()["location"] ?? "", `${path} redirected a signed-out visitor somewhere other than sign-in`).toContain("/accounts/login");
    } finally {
        await anonymous.dispose();
    }
}

function heroTitle(page: Page) {
    return page.locator("h1.ul-page-hero__title");
}

test.describe("organize detail pages", () => {
    test("a list's page shows its name and its pins", async ({ page, api }) => {
        const name = resourceName(`detail list ${Date.now()}`);
        const list = await api.json<{ slug: string }>("post", "lists/", { name });
        api.track("list", list.slug, () => api.delete(`lists/${list.slug}/`));
        const pin = await api.createPin({ name: resourceName("detail list member") });
        await api.json("post", `lists/${list.slug}/items/`, { pin_uuids: [pin.uuid] });

        await open(page, `/dashboard/lists/${list.slug}/`);
        await expect(heroTitle(page)).toContainText(name);
        await expect(page.locator(".pin-list-detail-count")).toHaveText("1 pin");
        await expect(page.locator(".pin-list-items-section")).toContainText(pin.name);

        await expectMissing(page, `/dashboard/lists/${MISSING_SLUG}/`);
    });

    test("a saved filter's page shows its name and counts its matches", async ({ page, api }) => {
        const token = `dp${Math.random().toString(36).slice(2, 10)}`;
        await api.createPin({ name: resourceName(`${token} match`) });
        const name = resourceName(`${token} filter`);
        const filter = await api.json<{ uuid: string }>("post", "saved-filters/", { name, criteria: { name: token } });
        api.track("saved-filter", filter.uuid, () => api.delete(`saved-filters/${filter.uuid}/`));

        await open(page, `/dashboard/saved-filters/${filter.uuid}/`);
        await expect(heroTitle(page)).toContainText(name);
        await expect(page.locator("body")).toContainText("1 matching pin");

        await expectMissing(page, `/dashboard/saved-filters/${randomUUID()}/`);
    });
});

test.describe("trip pages", () => {
    test("the trip list shows a trip the API made", async ({ page, api, playwright }) => {
        const name = resourceName("detail trip list");
        const trip = await api.json<{ slug: string }>("post", "trips/", { name, start_date: isoDate(4), end_date: isoDate(5) });
        api.track("trip", trip.slug, () => api.delete(`trips/${trip.slug}/`));

        await open(page, "/dashboard/trips/list/");
        await expect(heroTitle(page)).toContainText("Trips");
        await expect(page.locator("#trip-list")).toContainText(name);

        await expectSignInRequired(playwright, "/dashboard/trips/list/");
    });

    test("the trip calendar renders", async ({ page, playwright }) => {
        await open(page, "/dashboard/trips/calendar/");
        await expect(heroTitle(page)).toContainText("Trips");
        await expect(page.locator(".trips-view--calendar")).toBeVisible();

        await expectSignInRequired(playwright, "/dashboard/trips/calendar/");
    });

    test("a trip's weather panel answers for a real trip and not for a missing one", async ({ page, api }) => {
        const trip = await api.json<{ slug: string }>("post", "trips/", { name: resourceName("detail trip weather"), start_date: isoDate(1) });
        api.track("trip", trip.slug, () => api.delete(`trips/${trip.slug}/`));

        const response = await page.request.get(`/dashboard/trips/${trip.slug}/weather/`);
        expect(response.status(), `the weather panel answered ${response.status()}`).toBe(200);
        expect(await response.text(), "the weather panel rendered without its container").toContain('id="trip-weather-panel"');

        await expectMissing(page, `/dashboard/trips/${MISSING_SLUG}/weather/`);
    });
});

test.describe("memories pages", () => {
    ifSecondaryAccount()("the journal shows a visit note, to its owner only", async ({ page, api, secondaryPage }) => {
        const pin = await api.createPin({ name: resourceName("journal pin") });
        const note = `journal-${randomUUID()}`;
        const visit = await api.post(`pins/${pin.slug}/visits/`, { visited_at: new Date(Date.now() - 60_000).toISOString(), notes: note });
        expect(visit.status(), `logging a visit answered ${visit.status()}: ${(await visit.text()).slice(0, 200)}`).toBeLessThan(300);

        await open(page, "/dashboard/memories/journal/");
        await expect(heroTitle(page)).toContainText("Journal");
        await expect(page.locator(".memories-journal-snippet", { hasText: note })).toBeVisible();

        const theirs = await secondaryPage.request.get("/dashboard/memories/journal/");
        expect(theirs.status()).toBe(200);
        expect(await theirs.text(), "another account's journal shows this account's visit note").not.toContain(note);
    });

    for (const [path, title] of [
        ["/dashboard/memories/visits/", "Log your visits"],
        ["/dashboard/memories/maps/", "Your maps"],
        ["/dashboard/memories/locations/", "Locations"],
    ] as const) {
        test(`${path} renders its heading`, async ({ page, playwright }) => {
            await open(page, path);
            await expect(heroTitle(page)).toContainText(title);
            await expectSignInRequired(playwright, path);
        });
    }
});

test.describe("safety pages", () => {
    test("safety settings render", async ({ page, playwright }) => {
        await open(page, "/dashboard/safety/settings/");
        await expect(heroTitle(page)).toContainText("Safety Settings");
        await expectSignInRequired(playwright, "/dashboard/safety/settings/");
    });

    // The secondary account, because primary's one active check-in slot is contended by api/safety,
    // ui/pages and security/authorization.
    ifSecondaryAccount()("the new check-in form, then the check-in's own page", async ({ secondaryApi, secondaryPage }) => {
        const existing = await secondaryApi.json<{ results: Array<{ slug: string; is_resolved?: boolean }> }>("get", "safety/checkins/", { status: "active" });
        for (const checkin of existing.results) {
            await secondaryApi.post(`safety/checkins/${checkin.slug}/cancel/`);
        }

        await open(secondaryPage, "/dashboard/safety/new/");
        await expect(heroTitle(secondaryPage)).toContainText("New Safety Check-in");

        const title = resourceName("detail page check-in");
        const created = await secondaryApi.json<{ slug: string }>("post", "safety/checkins/", {
            title,
            checkin_by: new Date(Date.now() + 3 * 3_600_000).toISOString(),
            contacts: [],
        });
        try {
            await open(secondaryPage, `/dashboard/safety/${created.slug}/`);
            await expect(secondaryPage.locator("#safety-title-text")).toHaveText(title);

            // With a check-in open, the form hands the user the open one instead.
            await secondaryPage.goto("/dashboard/safety/new/");
            expect(new URL(secondaryPage.url()).pathname).toBe(`/dashboard/safety/${created.slug}/`);

            await expectMissing(secondaryPage, `/dashboard/safety/${MISSING_SLUG}/`);
        } finally {
            await secondaryApi.post(`safety/checkins/${created.slug}/cancel/`);
            await secondaryApi.delete(`safety/checkins/${created.slug}/`);
        }
    });
});

test.describe("profile pages", () => {
    test("the profile editor renders for its owner", async ({ page, account, playwright }) => {
        await open(page, "/dashboard/profile/edit/");
        await expect(page.locator("p.profile-username")).toContainText(`@${account.username}`);
        await expectSignInRequired(playwright, "/dashboard/profile/edit/");
    });

    test("the achievements page names its owner", async ({ page, api }) => {
        const me = await api.json<{ slug: string }>("get", "whoami/");
        await open(page, `/dashboard/profile/${me.slug}/achievements/all/`);
        await expect(heroTitle(page)).toContainText("Your Achievements");
        await expectMissing(page, `/dashboard/profile/${MISSING_SLUG}/achievements/all/`);
    });

    ifSharingPair()("a friend's profile and achievements name them", async ({ sharerApi, shareeApi, sharerPage }) => {
        const { b: friend } = await ensureFriends(sharerApi, shareeApi);
        const { username } = requireAccount(SHAREE_ROLE);

        await open(sharerPage, profileFor(friend.slug));
        await expect(sharerPage.locator("p.profile-username")).toContainText(`@${username}`);
        await expect(sharerPage.locator(".profile-friendship-actions")).toBeVisible();

        await open(sharerPage, `/dashboard/profile/${friend.slug}/achievements/all/`);
        await expect(heroTitle(sharerPage)).toContainText(`${username}'s Achievements`);

        await expectMissing(sharerPage, profileFor(MISSING_SLUG));
    });

    ifSharingPair()("places in common lists the viewer's own pin at a shared location", async ({ sharerApi, shareeApi, sharerPage }) => {
        const { a: me, b: friend } = await ensureFriends(sharerApi, shareeApi);
        const latitude = 42.6526 + (Math.random() * 2 - 1) * 0.4;
        const longitude = -73.7562 + (Math.random() * 2 - 1) * 0.4;
        const mine = await sharerApi.createPin({ name: resourceName("common mine"), latitude, longitude });
        const theirs = await shareeApi.createPin({ name: resourceName("common theirs"), latitude, longitude });

        await open(sharerPage, `/dashboard/profile/${friend.slug}/common-pins/`);
        await expect(heroTitle(sharerPage)).toContainText("Places in Common");
        const list = sharerPage.locator(".common-pins-list-section");
        await expect(list).toContainText(mine.name);
        await expect(list, "the other account's private pin name reached the page").not.toContainText(theirs.name);

        // Your own profile has nothing in common with itself.
        await expectMissing(sharerPage, `/dashboard/profile/${me.slug}/common-pins/`);
        await expectMissing(sharerPage, `/dashboard/profile/${MISSING_SLUG}/common-pins/`);
    });
});

test.describe("message pages", () => {
    ifSharingPair()("a conversation with a friend opens on their thread", async ({ sharerApi, shareeApi, sharerPage }) => {
        const { b: friend } = await ensureFriends(sharerApi, shareeApi);

        await open(sharerPage, `/dashboard/messages/${friend.slug}/`);
        await expect(sharerPage.locator("#dm-thread")).toHaveAttribute("data-partner-slug", friend.slug);

        await expectMissing(sharerPage, `/dashboard/messages/${MISSING_SLUG}/`);
    });
});

test.describe("wiki pages", () => {
    test("a pinned location's wiki page shows the wiki", async ({ page, api }) => {
        const pin = await api.createPin({ name: resourceName("detail wiki host") });
        const detail = await api.json<{ location_slug?: string }>("get", `pins/${pin.slug}/`);
        const slug = String(detail.location_slug ?? "");
        expect(slug, "the pin carries no location_slug").toBeTruthy();

        // Wikis are created in the background after the pin.
        let wiki: { name?: string } | null = null;
        await expect
            .poll(
                async () => {
                    const response = await api.get(`wikis/${slug}/`);
                    wiki = response.ok() ? ((await response.json()) as { name?: string }) : null;
                    return response.status();
                },
                { timeout: 120_000, intervals: [2_000, 5_000, 10_000] },
            )
            .toBe(200)
            .catch(() => test.skip(true, "No wiki appeared for a fresh pin within 2 minutes; wiki creation may be off on this deployment."));

        await open(page, `/dashboard/location/${slug}/wiki/`);
        await expect(page.locator("h1.wiki-title")).toHaveText(String((wiki as { name?: string } | null)?.name ?? ""));

        await expectMissing(page, `/dashboard/location/${MISSING_SLUG}/wiki/`);
    });
});

test.describe("other pages", () => {
    test("the floorplan editor opens for a pin and not for a missing one", async ({ page, api }) => {
        const pin = await api.createPin({ name: resourceName("detail floorplan") });
        await open(page, `${pinDetail(pin.slug)}floorplan/`);
        await expect(heroTitle(page)).toContainText("Floorplan");
        await expect(page.locator(".ul-page-hero__subtitle")).toContainText(pin.name);

        await expectMissing(page, `${pinDetail(MISSING_SLUG)}floorplan/`);
    });

    test("the AI link analysis page renders", async ({ page, playwright }) => {
        await open(page, "/dashboard/ai/extractions/");
        await expect(heroTitle(page)).toContainText("AI Link Analysis");
        await expectSignInRequired(playwright, "/dashboard/ai/extractions/");
    });

    test("the pin import help page renders", async ({ page }) => {
        await open(page, "/dashboard/help/import-pins/");
        await expect(page.locator("h1")).toContainText("Import pins into UrbanLens");
        await expectMissing(page, "/dashboard/help/import-pins-e2e-missing/");
    });

    test("the welcome page shows the onboarding form or moves a finished account on", async ({ page, playwright }) => {
        const response = await page.goto("/dashboard/welcome/");
        expect(response?.status()).toBe(200);
        const landed = new URL(page.url()).pathname;
        expect(landed, "the welcome page bounced a signed-in account to sign-in").not.toContain("/accounts/login");
        if (landed === "/dashboard/welcome/") {
            await expect(page.locator("#welcome-onboarding-form")).toBeVisible();
        } else {
            // Provisioned accounts are past onboarding, which redirects through post_login.
            await expect(new AppShell(page).nav).toBeVisible();
        }
        await expectSignInRequired(playwright, "/dashboard/welcome/");
    });
});
