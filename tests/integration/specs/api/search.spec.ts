/**
 * Search: does a thing you just made turn up when you look for it?
 *
 * The question sounds trivial and is the one most worth asking of a deployment.
 * Search reaches across domains in a single query, and its result envelope
 * carries fields a client uses to explain *itself* - `used_fallback` when the
 * primary strategy found nothing, `omitted_types` when a scope kept a group
 * out, `errors` when one source failed but the rest answered. All three are
 * invisible in a unit test that asserts on hits alone, and all three change
 * what the UI tells the user.
 */

import { expect, test } from "../../lib/fixtures.js";
import { resourceName } from "../../lib/env.js";

/** The documented search envelope. */
interface SearchResponse {
    query: string;
    total: number;
    groups: Array<{ type?: string; results?: unknown[]; items?: unknown[] }>;
    filter_chips?: unknown[];
    omitted_types?: string[];
    used_fallback?: boolean;
    errors?: unknown[];
}

test.describe("search", () => {
    test("a pin created a moment ago is findable by name", async ({ api }) => {
        // The name is unique to this run, so a hit cannot come from data
        // somebody else left behind.
        const name = resourceName("findable landmark");
        const pin = await api.createPin({ name });

        const response = await api.get("search/", { q: name });
        expect(response.status(), `search answered ${response.status()}: ${(await response.text()).slice(0, 200)}`).toBe(200);

        const body = (await response.json()) as SearchResponse;
        expect(body.query, "the response does not echo the query, so a client cannot tell late answers apart").toBe(name);
        expect(Array.isArray(body.groups)).toBeTruthy();

        // Serialised whole rather than reaching into a group shape: what
        // matters is that the pin is *somewhere* in the answer, and pinning the
        // exact nesting would break every time a group is added.
        expect(JSON.stringify(body.groups), `searching for a pin created seconds ago returned nothing. total=${body.total}, errors=${JSON.stringify(body.errors)}`).toContain(pin.slug);
    });

    test("a query that matches nothing answers cleanly rather than erroring", async ({ api }) => {
        const response = await api.get("search/", { q: "zzzqqq-no-such-thing-91b2c" });
        expect(response.status()).toBe(200);

        const body = (await response.json()) as SearchResponse;
        expect(Array.isArray(body.groups)).toBeTruthy();
        expect(body.total, "a query matching nothing reported a non-zero total").toBe(0);
    });

    test("no search source failed", async ({ api }) => {
        // `errors` exists because a source can fail while the rest answer - the
        // response is still a 200, and the failure is only visible here. That
        // is precisely the shape of failure a deployment introduces and a unit
        // test cannot: a source that is unreachable from *this* container.
        const body = await api.json<SearchResponse>("get", "search/", { q: "e2e" });
        expect(body.errors ?? [], `search answered 200 but reported source errors: ${JSON.stringify(body.errors)}`).toHaveLength(0);
    });

    test("the type filter is honoured", async ({ api }) => {
        const pin = await api.createPin({ name: resourceName("typed search") });

        // The accepted tokens are not enumerated in the schema (`types` is a
        // free-form string), so the test reads back the type of the group that
        // actually contained the pin and asks for that. Hardcoding a guess
        // tests the guess: `types=pin` returns an empty result set, and it is
        // indistinguishable from the filter being broken.
        const unfiltered = await api.json<SearchResponse>("get", "search/", { q: pin.name });
        const group = unfiltered.groups.find((candidate) => JSON.stringify(candidate).includes(pin.slug));
        expect(group, `the pin was not in any group of an unfiltered search: ${JSON.stringify(unfiltered.groups).slice(0, 200)}`).toBeTruthy();
        const type = group?.type;
        test.skip(!type, "the search response does not label its groups with a type, so there is nothing to filter by.");

        const response = await api.get("search/", { q: pin.name, types: String(type) });
        expect(response.status(), `a types filter answered ${response.status()}`).toBe(200);
        const body = (await response.json()) as SearchResponse;
        expect(JSON.stringify(body.groups), `restricting the search to "${type}" - the type the pin was found under - dropped the pin`).toContain(pin.slug);
    });

    test("a key without the search scope is refused", async ({ restrictedApi }) => {
        const response = await restrictedApi.get("search/", { q: "anything" });
        expect(response.status(), "a profile:read key was allowed to search").toBe(403);
    });
});
