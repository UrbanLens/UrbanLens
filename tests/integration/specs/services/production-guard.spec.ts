/**
 * The exact-hostname matching behind the production-write guard. `lib/env.ts` throws at module-load
 * time, before any test runs, if `UL_E2E_BASE_URL`'s hostname is in `UL_E2E_PRODUCTION_HOSTS` -
 * this suite writes and deletes rows as a real account, and pointing it at production is data loss.
 */

import { expect, test } from "@playwright/test";

import { DEFAULT_PRODUCTION_HOSTS, isProductionHost } from "../../lib/production-guard.js";

test.describe("production-write guard", () => {
    test("an exact production hostname is caught", () => {
        expect(isProductionHost("example.com", ["example.com", "www.example.com"])).toBe(true);
    });

    test("a dev/staging host sharing production's domain suffix is not caught", () => {
        expect(isProductionHost("s1.dev.example.com", ["example.com"])).toBe(false);
    });

    test("a production host is not caught by an unrelated denylist entry", () => {
        expect(isProductionHost("example.com", ["some-other-app.example"])).toBe(false);
    });

    test("matching is case-insensitive", () => {
        expect(isProductionHost("EXAMPLE.COM", ["example.com"])).toBe(true);
    });

    test("a fully-qualified hostname with its trailing dot is caught", () => {
        expect(isProductionHost("example.com.", ["example.com"])).toBe(true);
    });

    test("an empty denylist catches nothing", () => {
        expect(isProductionHost("example.com", [])).toBe(false);
    });

    test("every default production host is genuinely caught by its own list", () => {
        for (const host of DEFAULT_PRODUCTION_HOSTS) {
            expect(isProductionHost(host, [...DEFAULT_PRODUCTION_HOSTS])).toBe(true);
        }
    });

    test("every hostname the production tunnel serves the web app on is refused by default", () => {
        // The production tunnel routes these to the production web app.
        for (const host of ["example.com", "www.example.com"]) {
            expect(isProductionHost(host, [...DEFAULT_PRODUCTION_HOSTS])).toBe(true);
        }
    });
});
