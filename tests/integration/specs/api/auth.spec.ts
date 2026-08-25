/**
 * Who the external API lets in, and what it lets them do.
 *
 * These are the assertions that cannot be made from inside the process. A unit
 * test proves `HasApiKeyScope` returns False for a key without the scope; only
 * a request to a running deployment proves the authenticator is actually wired
 * into the view, that the middleware ahead of it did not already answer, and
 * that no proxy is stripping the `Authorization` header on its way through -
 * which is a real and silent failure mode, because a stripped header presents
 * as "the key is invalid".
 */

import { ApiClient } from "../../lib/api-client.js";
import { expect, test } from "../../lib/fixtures.js";

test.describe("external API authentication", () => {
    test("an unauthenticated request is refused", async ({ anonymousApi }) => {
        const response = await anonymousApi.get("whoami/");
        expect(response.status()).toBe(401);
        // Every error in this API renders one envelope, so a generated client
        // does not have to special-case endpoints. See external_api/errors.py.
        expect(await response.json()).toHaveProperty("error");
    });

    test("a malformed key is refused", async ({ apiRequestContext }) => {
        const forged = new ApiClient(apiRequestContext, "ulk_0000000000notarealkeyatall");
        const response = await forged.get("whoami/");
        expect(response.status()).toBe(401);
    });

    test("a bearer token that is not an API key is not claimed by the key authenticator", async ({ apiRequestContext }) => {
        // `ApiKeyAuthentication` returns None for a non-`ulk_` bearer token so
        // the OAuth2 authenticator gets its turn. Either way this must be a 401
        // rather than a 500 from something trying to parse it as a key.
        const foreign = new ApiClient(apiRequestContext, "not-an-urbanlens-key");
        const response = await foreign.get("whoami/");
        expect(response.status()).toBe(401);
    });

    test("a valid key identifies its owner and nothing else", async ({ api, account }) => {
        const response = await api.get("whoami/");
        expect(response.status()).toBe(200);

        const body = (await response.json()) as Record<string, unknown>;
        // The narrowest profile read in the API: uuid and slug, by design. A
        // field appearing here that the `profile:read` scope does not cover is
        // a privacy regression, so the shape is asserted exactly.
        expect(Object.keys(body).sort()).toEqual(["slug", "uuid"]);
        expect(body.uuid).toBeTruthy();

        if (account.profileUuid) {
            expect(body.uuid).toBe(account.profileUuid);
        }
    });

    test("a key without the scope is refused, not merely unauthenticated", async ({ restrictedApi, account }) => {
        test.skip(!account.restrictedApiKey, "No restricted key was provisioned; re-run provision_integration_env.");

        // The restricted key holds profile:read only. It authenticates fine...
        const identified = await restrictedApi.get("whoami/");
        expect(identified.status(), "the restricted key could not even identify itself").toBe(200);

        // ...and must still be refused where it has no grant. 403, not 401:
        // the difference is "we know who you are and you may not" versus "we do
        // not know who you are", and a client retries on one and not the other.
        const refused = await restrictedApi.get("pins/");
        expect(refused.status(), "a key without pins:read was allowed to read pins").toBe(403);
        expect(await refused.json()).toHaveProperty("error");
    });

    test("a write is refused for a key that may only read", async ({ restrictedApi, account }) => {
        test.skip(!account.restrictedApiKey, "No restricted key was provisioned.");

        const response = await restrictedApi.post("pins/", { name: "should never be created", latitude: 0, longitude: 0 });
        expect(response.status(), "a read-only key created a pin").toBe(403);
    });

    test("the schema is published without credentials", async ({ anonymousApi }) => {
        // Deliberately unauthenticated: the schema is the published contract,
        // not user data. A 401 here breaks every generated client's build.
        const response = await anonymousApi.get("schema/");
        expect(response.status()).toBe(200);
    });
});
