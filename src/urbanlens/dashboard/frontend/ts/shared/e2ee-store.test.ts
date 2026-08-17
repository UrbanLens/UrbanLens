/**
 * The key cache is per-profile, and clearing one profile must not touch
 * another's. Two accounts sharing a browser is the case that matters: an
 * over-broad wipe silently locks the account that was not being reset, and
 * "my messages stopped decrypting" is a hard bug to trace back to someone
 * else's key reset.
 */
import { afterEach, beforeEach, describe, expect, test } from "bun:test";

import { installFakeIndexedDB, type FakeIndexedDB } from "../testing/fake-indexeddb";
import { clearProfileKeys, getConversationKey, getIdentity, putConversationKey, putGroupKey, putIdentity, type CachedIdentity } from "./e2ee-store";

let db: FakeIndexedDB;

function identity(publicKey: string): CachedIdentity {
    return { privateKey: new Uint8Array([1, 2, 3]), publicKey, version: 1 };
}

beforeEach(() => {
    db = installFakeIndexedDB("keys");
});

afterEach(() => {
    db.uninstall();
});

describe("round-tripping cached material", () => {
    test("an identity comes back out as it went in", async () => {
        await putIdentity("jess", identity("pub-jess"));

        expect((await getIdentity("jess"))?.publicKey).toBe("pub-jess");
    });

    test("an absent identity reads as locked, not as an error", async () => {
        expect(await getIdentity("nobody")).toBeNull();
    });

    test("conversation keys are stored per version", async () => {
        await putConversationKey("jess", "sam", 1, new Uint8Array([9]));
        await putConversationKey("jess", "sam", 2, new Uint8Array([8]));

        expect(await getConversationKey("jess", "sam", 1)).toEqual(new Uint8Array([9]));
        expect(await getConversationKey("jess", "sam", 2)).toEqual(new Uint8Array([8]));
    });
});

describe("clearProfileKeys", () => {
    test("removes the profile's own identity", async () => {
        await putIdentity("jess", identity("pub-jess"));
        await clearProfileKeys("jess");

        expect(await getIdentity("jess")).toBeNull();
    });

    test("removes the profile's conversation and group keys", async () => {
        await putConversationKey("jess", "sam", 1, new Uint8Array([1]));
        await putGroupKey("jess", "group-uuid", 1, new Uint8Array([2]));
        await clearProfileKeys("jess");

        expect(db.keys()).toHaveLength(0);
    });

    test("leaves a slug that merely starts the same alone", async () => {
        // "identity:jess" is a prefix of "identity:jess2": deleting by prefix
        // locked the second account out of its own messages.
        await putIdentity("jess", identity("pub-jess"));
        await putIdentity("jess2", identity("pub-jess2"));

        await clearProfileKeys("jess");

        expect(await getIdentity("jess")).toBeNull();
        expect((await getIdentity("jess2"))?.publicKey).toBe("pub-jess2");
    });

    test("leaves the similar slug's conversation keys alone too", async () => {
        await putConversationKey("jess", "sam", 1, new Uint8Array([1]));
        await putConversationKey("jess2", "sam", 1, new Uint8Array([2]));

        await clearProfileKeys("jess");

        expect(await getConversationKey("jess", "sam", 1)).toBeNull();
        expect(await getConversationKey("jess2", "sam", 1)).toEqual(new Uint8Array([2]));
    });

    test("clearing the longer slug does not touch the shorter one", async () => {
        await putIdentity("jess", identity("pub-jess"));
        await putIdentity("jess2", identity("pub-jess2"));

        await clearProfileKeys("jess2");

        expect((await getIdentity("jess"))?.publicKey).toBe("pub-jess");
        expect(await getIdentity("jess2")).toBeNull();
    });
});
