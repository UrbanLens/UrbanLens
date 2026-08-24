/**
 * The published contract: the schema, the envelopes, the paging.
 *
 * A third party generates a client from `schema/` and then depends on it. That
 * makes the schema a deployed artefact rather than a build product, and one
 * with a specific and well-known failure mode: `drf-spectacular` resolves model
 * annotations at *generation* time, so a serializer whose type hints only exist
 * under `TYPE_CHECKING` produces a 500 from this endpoint while every endpoint
 * it describes keeps working perfectly. Nothing but fetching it notices.
 */

import { expect, test } from "../../lib/fixtures.js";
import { apiUrl } from "../../lib/env.js";

/** The external API's mount point. */
const API_PREFIX = "/dashboard/api/external/v1/";

/**
 * Every prefix the schema is allowed to document.
 *
 * Mirrors `external_api.schema.PUBLISHED_SCHEMA_PREFIXES`, which is a tuple
 * rather than a single string on purpose: "allowed to call" is not the same as
 * "lives under the external mount", and the end-to-end encryption endpoints are
 * a documented case of the former. Update this together with that constant.
 */
const PUBLISHED_PREFIXES = [API_PREFIX, "/dashboard/e2ee/"];

/** The surface that must never appear: internal, session-authenticated, no contract. */
const INTERNAL_PREFIX = "/dashboard/rest/";

test.describe("published contract", () => {
    test("the OpenAPI schema generates and parses", async ({ anonymousApi }) => {
        const response = await anonymousApi.get("schema/", { format: "json" });
        expect(response.status(), `schema generation answered ${response.status()}: ${(await response.text()).slice(0, 500)}`).toBe(200);

        const schema = (await response.json()) as { openapi?: string; paths?: Record<string, unknown>; components?: unknown };
        expect(schema.openapi, "the document does not declare an OpenAPI version").toMatch(/^3\./);
        expect(Object.keys(schema.paths ?? {}).length, "the schema documents no paths at all").toBeGreaterThan(20);
    });

    test("the schema never publishes the internal REST surface", async ({ anonymousApi }) => {
        const schema = await anonymousApi.json<{ paths: Record<string, unknown> }>("get", "schema/", { format: "json" });

        // The property `preprocess_external_api_only` exists to protect: the
        // internal, session-authenticated API has no public contract, and
        // publishing it would present it to third parties as one they may
        // depend on.
        const leaked = Object.keys(schema.paths).filter((path) => path.startsWith(INTERNAL_PREFIX));
        expect(leaked, `the schema publishes the internal REST surface:\n  ${leaked.join("\n  ")}`).toHaveLength(0);
    });

    test("the schema documents nothing outside the published prefixes", async ({ anonymousApi }) => {
        const schema = await anonymousApi.json<{ paths: Record<string, unknown> }>("get", "schema/", { format: "json" });

        const foreign = Object.keys(schema.paths).filter((path) => !PUBLISHED_PREFIXES.some((prefix) => path.startsWith(prefix)));
        expect(
            foreign,
            `the schema documents paths outside PUBLISHED_SCHEMA_PREFIXES:\n  ${foreign.join("\n  ")}\n` +
                "If a new prefix was published deliberately, add it to PUBLISHED_PREFIXES in this spec too.",
        ).toHaveLength(0);
    });

    test("the endpoints this suite depends on are documented", async ({ anonymousApi }) => {
        const schema = await anonymousApi.json<{ paths: Record<string, unknown> }>("get", "schema/", { format: "json" });

        for (const path of ["whoami/", "pins/", "labels/"]) {
            expect(Object.keys(schema.paths), `${path} is served but undocumented`).toContain(`${API_PREFIX}${path}`);
        }
    });

    test.describe("interactive documentation", () => {
        // Swagger UI is third-party markup that loads its own assets and is
        // entitled to log whatever it likes; the console check would be about
        // it rather than about this application.
        test.use({ strictConsole: false });

        test("renders", async ({ page }) => {
            // Served unauthenticated alongside the schema, and the fastest way
            // for a human to notice the schema is broken.
            const response = await page.goto(apiUrl("docs/"));
            expect(response?.status()).toBe(200);
            await expect(page.locator("body")).toContainText(/swagger|api/i);
        });
    });

    test("a browse endpoint pages with the documented envelope", async ({ api }) => {
        const response = await api.get("labels/", { page_size: 5 });
        expect(response.status()).toBe(200);

        const body = (await response.json()) as Record<string, unknown>;
        // `{count, next, previous, results}` - what a paging UI expects, and
        // deliberately different from the pin sync feed's cursor envelope.
        for (const key of ["count", "next", "previous", "results"]) {
            expect(body, `the paging envelope is missing "${key}"`).toHaveProperty(key);
        }
        expect(Array.isArray(body.results)).toBeTruthy();
        expect((body.results as unknown[]).length).toBeLessThanOrEqual(5);
    });

    test("an absurd page size is clamped rather than honoured", async ({ api }) => {
        // `max_page_size` is what stops `?page_size=100000` from trying to
        // serialise a whole table. Enforced server-side, so only a real request
        // can prove it.
        const response = await api.get("labels/", { page_size: 100000 });
        expect(response.status()).toBe(200);
        const body = (await response.json()) as { results: unknown[] };
        expect(body.results.length).toBeLessThanOrEqual(100);
    });

    test("a method an endpoint does not offer is refused with the same envelope", async ({ api }) => {
        const response = await api.delete("whoami/");

        // 403 rather than 405, and that is not a bug: permission checks run
        // before method dispatch, and a method with no entry in the view's
        // `required_scopes_by_method` grants no scope, so the request is
        // refused as unauthorised before anything notices the method is
        // unsupported. Either answer is fine; what must hold is the envelope.
        expect([403, 405], `expected a refusal, got ${response.status()}`).toContain(response.status());
        // DRF's own body for both is `{"detail": ...}`. The envelope mixin
        // rewrites it, and a third shape is one more than a generated client
        // can parse without special-casing endpoints.
        expect(await response.json()).toHaveProperty("error");
    });

    test("a malformed request body is refused with the envelope, not a stack trace", async ({ apiRequestContext, account }) => {
        // Sent raw rather than through the client, because the point is a body
        // no JSON serialiser would ever produce. `OAuth2Authentication` parses
        // the body during authentication for every request reaching a dual-auth
        // view, so this exercises a failure that happens before the view runs.
        const response = await apiRequestContext.post(apiUrl("pins/"), {
            headers: { Authorization: `Bearer ${account.apiKey ?? ""}`, "Content-Type": "application/json" },
            data: "{not valid json",
        });
        expect(response.status(), "a malformed body produced a server error rather than a 400").toBe(400);
        expect(await response.json()).toHaveProperty("error");
    });
});
