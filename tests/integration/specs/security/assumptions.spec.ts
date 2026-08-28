/**
 * Controls that make every other file in this project mean something.
 *
 * A security assertion that "the stranger got 404" is worthless if the owner
 * also gets 404, if the two accounts are the same person, or if the
 * restricted key is already revoked. These tests establish the preconditions
 * the rest of the project relies on. If this file is red, ignore the files
 * below it: they are answering a different question than the one they ask.
 */

import { expect, ifSecondaryAccount, test } from "../../lib/fixtures.js";
import { env } from "../../lib/env.js";
import { publicRoutes } from "../../lib/routes.js";
import { whoami } from "../../lib/security.js";

test.describe("the suite can actually observe a working deployment", () => {
    test("liveness still answers, so later refusals are not a down host", async ({ request }) => {
        const response = await request.get(publicRoutes.healthLive);
        expect(response.status(), "the deployment did not answer /health/live, so every 404 below is meaningless").toBe(200);
        expect((await response.text()).trim()).toBe("Okay!");
    });

    test("the primary credential can identify itself", async ({ api, account }) => {
        const me = await whoami(api);
        expect(me.uuid, "whoami returned no uuid").toBeTruthy();
        expect(me.slug, "whoami returned no slug").toBeTruthy();
        if (account.profileUuid) {
            expect(me.uuid, "whoami's uuid does not match the provisioned account").toBe(account.profileUuid);
        }
    });

    test("the primary credential can create and read a pin", async ({ api }) => {
        // Without this, every "stranger cannot read this pin" test could pass
        // because *nobody* can read pins.
        const pin = await api.createPin();
        const mine = await api.get(`pins/${pin.slug}/`);
        expect(mine.status(), `the owner could not read a pin they just created (${mine.status()})`).toBe(200);
        const body = (await mine.json()) as { name?: string; uuid?: string };
        expect(body.uuid).toBe(pin.uuid);
        expect(body.name).toBe(pin.name);
    });

    test("the restricted key is valid, so its 403s are about scopes", async ({ restrictedApi, account }) => {
        test.skip(!account.restrictedApiKey, "No restricted key was provisioned; re-run provision_integration_env.");
        const response = await restrictedApi.get("whoami/");
        expect(
            response.status(),
            `the restricted key cannot identify itself (${response.status()}), so every 403 it produces may be authentication rather than authorization`,
        ).toBe(200);
    });

    test("an anonymous client is genuinely anonymous", async ({ anonymousApi }) => {
        const response = await anonymousApi.get("whoami/");
        expect(response.status(), "the anonymous client authenticated, so unauthenticated-refusal tests are not testing that").toBe(401);
    });
});

test.describe("the two accounts are two people", () => {
    ifSecondaryAccount()("primary and secondary whoami uuids differ", async ({ api, secondaryApi }) => {
        const me = await whoami(api);
        const them = await whoami(secondaryApi);
        expect(them.uuid, "the secondary account has no uuid").toBeTruthy();
        expect(them.uuid, "primary and secondary resolved to the same profile, so every isolation test is asserting a user against themselves").not.toBe(me.uuid);
        expect(them.slug).not.toBe(me.slug);
    });

    ifSecondaryAccount()("secondary can also create and read its own pin", async ({ secondaryApi }) => {
        const pin = await secondaryApi.createPin();
        const mine = await secondaryApi.get(`pins/${pin.slug}/`);
        expect(mine.status(), `secondary could not read its own pin (${mine.status()})`).toBe(200);
    });
});

test.describe("the run is pointed at the deployment it thinks it is", () => {
    test("the configured host is the host pages actually load", async ({ page }) => {
        await page.goto("/");
        expect(new URL(page.url()).hostname, "the browser landed on a different host than UL_E2E_BASE_URL").toBe(env.host);
    });
});
