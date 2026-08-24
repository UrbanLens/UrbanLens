/**
 * What a pin on the campus learns about itself, without being told.
 *
 * The wiki has its own file; this one is about the *pin*. The requirements ask
 * for an article on both, and they are genuinely separate objects with separate
 * routes (`pins/{slug}/article/` and `wikis/{location_slug}/article/`) and
 * separate scopes - `views_pin_article`'s docstring is explicit that using
 * `wiki:*` scopes for the pin routes would be a privacy bug, because a pin is
 * one person's private record and a wiki is community content.
 *
 * Everything here is enrichment that follows pin creation, so everything here
 * needs waiting. What it does *not* need is the parcel: address geocoding and
 * Wikipedia seeding key off the coordinate, not off the place. So these tests
 * deliberately do not call `requireBoundary()` - they are among the few in this
 * directory that should still run, and still mean something, when the parcel
 * never arrives.
 */

import { expect, locationDataTest as test, skipUnlessLocationDataEnabled } from "./fixtures.js";
import { waitForOrNull } from "../../lib/waiting.js";

skipUnlessLocationDataEnabled();

/** The name the fixture gives the campus pin, asserted to survive enrichment. */
const CAMPUS_PIN_NAME = "e2e Hudson River State Hospital";

test.describe("Hudson River State Hospital - what the pin learns", () => {
    test("the pin is geocoded to the right municipality", async ({ campus }) => {
        const detail = await waitForOrNull(
            () => campus.api.json<{ city?: string | null; state?: string | null; county?: string | null }>("get", `pins/${campus.pin.slug}/`),
            (pin) => Boolean(pin.city || pin.county),
            {
                what: "an address for the campus pin",
                timeoutMs: 240_000,
                intervalMs: 15_000,
                describe: (pin) => `city=${pin.city ?? "null"} county=${pin.county ?? "null"} state=${pin.state ?? "null"}`,
            },
        );

        expect(detail, "the pin was never geocoded - it has neither a city nor a county").not.toBeNull();

        // Asserted loosely on purpose. Whether a geocoder says "Poughkeepsie" or
        // "Town of Poughkeepsie" is its business and varies by provider; that it
        // landed in the right place is the claim.
        const where = `${detail!.city ?? ""} ${detail!.county ?? ""} ${detail!.state ?? ""}`.toLowerCase();
        expect(
            /poughkeepsie|dutchess/.test(where),
            `the pin geocoded to "${where.trim()}". These coordinates are in the Town of Poughkeepsie, Dutchess County, New York - a ` +
                "result naming somewhere else means the coordinate was transposed or the geocoder answered about a different point",
        ).toBe(true);
    });

    test("the pin gains an article seeded from Wikipedia", async ({ campus }) => {
        const article = await waitForOrNull(
            () => campus.api.get(`pins/${campus.pin.slug}/article/`),
            (response) => response.status() === 200,
            {
                what: "an article on the campus pin",
                timeoutMs: 300_000,
                intervalMs: 15_000,
                describe: (response) => `HTTP ${response.status()}`,
            },
        );

        expect(
            article,
            "the pin never got an article. Hudson River State Hospital has a substantial Wikipedia page, so there is something to seed " +
                "from - check that prefetch_location_external_data ran. It is enqueued at the end of create_pin_for_profile and is gated " +
                "on the profile external_apis_enabled flag",
        ).not.toBeNull();

        const body = await campus.api.json<{ content?: string }>("get", `pins/${campus.pin.slug}/article/`);
        expect((body.content ?? "").length, "the article exists but is empty").toBeGreaterThan(200);
    });

    test("the pin article is about this place", async ({ campus }) => {
        const response = await campus.api.get(`pins/${campus.pin.slug}/article/`);
        test.skip(response.status() !== 200, "no article on the pin - see the previous test.");

        const body = await campus.api.json<{ content?: string }>("get", `pins/${campus.pin.slug}/article/`);
        const content = (body.content ?? "").toLowerCase();

        // Guards a seeding step that matched the wrong Wikipedia page - a real
        // risk when the search term is a placeholder name rather than the
        // resolved official one.
        expect(
            /hudson river state hospital|poughkeepsie/.test(content),
            "the article mentions neither Hudson River State Hospital nor Poughkeepsie, so whatever it was seeded from is about " +
                "somewhere else",
        ).toBe(true);
    });

    test("official aliases reach the pin", async ({ campus }) => {
        const detail = await waitForOrNull(
            () => campus.api.json<{ aliases?: Array<{ name?: string; kind?: string }> }>("get", `pins/${campus.pin.slug}/`),
            (pin) => (pin.aliases ?? []).length > 0,
            {
                what: "official aliases on the campus pin",
                timeoutMs: 300_000,
                intervalMs: 15_000,
                describe: (pin) => `${(pin.aliases ?? []).length} alias(es)`,
            },
        );

        expect(
            detail,
            "the pin has no aliases. Name providers write PinAlias rows before any name is chosen, so an empty list means no provider " +
                "answered for this coordinate",
        ).not.toBeNull();

        const aliases = detail!.aliases ?? [];
        const official = aliases.filter((alias) => alias.kind === "official");
        expect(
            official.length,
            `the pin has ${aliases.length} alias(es) but none marked official: ` +
                `${JSON.stringify(aliases.map((alias) => `${alias.name}/${alias.kind}`))}. Official aliases are what an automatic rename ` +
                "is allowed to draw on",
        ).toBeGreaterThan(0);
    });

    test("the location has an official name for automatic naming to draw on", async ({ campus }) => {
        const detail = await waitForOrNull(
            () => campus.api.json<{ official_name?: string | null; name?: string }>("get", `pins/${campus.pin.slug}/`),
            (pin) => Boolean(pin.official_name),
            {
                what: "an official name for the campus location",
                timeoutMs: 300_000,
                intervalMs: 15_000,
                describe: (pin) => `official_name=${pin.official_name ?? "null"} name=${pin.name ?? ""}`,
            },
        );

        // `official_name` belongs to the Location, not the pin, and is the value
        // automatic naming uses. The fixture pin is created with
        // `name_is_user_provided`, so its own name is deliberately left alone -
        // which is why this asserts on official_name rather than on name.
        expect(
            detail,
            "no official name was resolved for this location. That is what automatic naming draws on, so without it the requirement " +
                "that a name should populate from the official aliases has nothing to populate from",
        ).not.toBeNull();
    });

    test("a user-provided pin name is never overwritten by enrichment", async ({ campus }) => {
        // The other half of automatic naming, and the more important half: the
        // fixture pin is created with name_is_user_provided, and enrichment has
        // to respect that however good a name it finds.
        const detail = await campus.api.json<{ name?: string }>("get", `pins/${campus.pin.slug}/`);

        expect(
            detail.name,
            `the pin was created as "${CAMPUS_PIN_NAME}" with name_is_user_provided set, and is now called "${detail.name}". Background ` +
                "enrichment renamed something a human supplied, which is the one thing that flag exists to prevent",
        ).toBe(CAMPUS_PIN_NAME);
    });
});
