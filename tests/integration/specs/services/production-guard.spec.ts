/**
 * The exact-hostname matching behind the production-write guard. `lib/env.ts` throws at module-load
 * time, before any test runs, if `UL_E2E_BASE_URL`'s hostname is in `UL_E2E_PRODUCTION_HOSTS` -
 * this suite writes and deletes rows as a real account, and pointing it at production is data loss.
 */

import { expect, test } from "@playwright/test";

import { isProductionHost } from "../../lib/production-guard.js";

test.describe("production-write guard", () => {
    test("an exact production hostname is caught", () => {
        expect(isProductionHost("urbanlens.org", ["urbanlens.org", "www.urbanlens.org"])).toBe(true);
    });

    test("a dev/staging host sharing production's domain suffix is not caught", () => {
        expect(isProductionHost("s1.dev.urbanlens.org", ["urbanlens.org"])).toBe(false);
    });

    test("a production host is not caught by an unrelated denylist entry", () => {
        expect(isProductionHost("urbanlens.org", ["some-other-app.example"])).toBe(false);
    });

    test("matching is case-insensitive", () => {
        expect(isProductionHost("URBANLENS.ORG", ["urbanlens.org"])).toBe(true);
    });

    test("an empty denylist catches nothing", () => {
        expect(isProductionHost("urbanlens.org", [])).toBe(false);
    });

    test("every default production host is genuinely caught by its own list", () => {
        const defaults = ["urbanlens.org", "www.urbanlens.org", "app.urbanlens.org"];
        for (const host of defaults) {
            expect(isProductionHost(host, defaults)).toBe(true);
        }
    });
});
