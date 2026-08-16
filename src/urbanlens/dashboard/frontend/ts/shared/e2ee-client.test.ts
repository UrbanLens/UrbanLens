/**
 * Outgoing encryption must say *why* it produced no ciphertext.
 *
 * "Nobody to encrypt to" and "the key request failed" are different answers:
 * the first legitimately allows the caller to send plaintext, the second must
 * not, because the thread is one both participants believe is encrypted. These
 * were once the same `null`, so a single 500 on a key fetch sent a readable
 * message into an encrypted conversation with nothing but a console line.
 */
import { afterEach, beforeEach, describe, expect, test } from "bun:test";

import { installFakeIndexedDB, type FakeIndexedDB } from "../testing/fake-indexeddb";
import { encryptForGroup, encryptForPartner, init } from "./e2ee-client";

const realFetch = globalThis.fetch;
let db: FakeIndexedDB;

/** Route each URL prefix to a canned response. */
type Route = { status: number; body?: unknown } | "throw";

function stubFetch(routes: Record<string, Route>): void {
    globalThis.fetch = ((url: string) => {
        const target = String(url);
        const match = Object.keys(routes).find((prefix) => target.startsWith(prefix));
        const route = match ? routes[match]! : { status: 404 };
        if (route === "throw") return Promise.reject(new Error("offline"));
        return Promise.resolve({
            ok: route.status >= 200 && route.status < 300,
            status: route.status,
            json: () => Promise.resolve(route.body ?? {}),
        } as Response);
    }) as unknown as typeof fetch;
}

function cacheIdentity(slug = "jess"): void {
    db.set(`identity:${slug}`, { privateKey: new Uint8Array(32), publicKey: "pub", version: 1 });
}

beforeEach(() => {
    db = installFakeIndexedDB("keys");
    init({
        urls: {
            loginParams: "/e2ee/login-params/",
            enroll: "/e2ee/enroll/",
            keys: "/e2ee/keys/",
            rewrap: "/e2ee/rewrap/",
            reset: "/e2ee/reset/",
            partnerKeyBase: "/e2ee/keys/",
            conversationKeyBase: "/e2ee/conversation-key/",
            groupKeyBase: "/e2ee/group-key/",
            login: "/login/",
        },
        selfSlug: "jess",
    });
});

afterEach(() => {
    globalThis.fetch = realFetch;
    db.uninstall();
});

describe("a conversation that cannot be encrypted by design", () => {
    test("a locked device is unencryptable, not an error", async () => {
        stubFetch({});
        // No cached identity: nothing to decrypt or seal with.
        const result = await encryptForPartner("sam", "hello");

        expect(result.status).toBe("unencryptable");
    });

    test("an unenrolled partner (404) is unencryptable", async () => {
        cacheIdentity();
        stubFetch({
            "/e2ee/conversation-key/": { status: 200, body: { keys: [], latest: 0 } },
            "/e2ee/keys/": { status: 404 },
        });

        const result = await encryptForPartner("sam", "hello");

        expect(result.status).toBe("unencryptable");
    });

    test("a group with an unenrolled member is unencryptable", async () => {
        cacheIdentity();
        stubFetch({
            "/e2ee/group-key/": { status: 200, body: { keys: [], latest: 0, needs_rotation: false, members: null } },
        });

        const result = await encryptForGroup("group-uuid", "hello");

        expect(result.status).toBe("unencryptable");
    });
});

describe("a conversation whose keys could not be fetched", () => {
    // Each of these once returned the same null as "unencryptable" above, and
    // the messages page answered that null by sending the body in the clear.
    for (const status of [401, 403, 500, 502]) {
        test(`a ${status} on the conversation key is an error, not a plaintext licence`, async () => {
            cacheIdentity();
            stubFetch({ "/e2ee/conversation-key/": { status } });

            const result = await encryptForPartner("sam", "hello");

            expect(result.status).toBe("error");
        });
    }

    test("a 500 on the partner key is an error, not 'they aren't enrolled'", async () => {
        cacheIdentity();
        stubFetch({
            "/e2ee/conversation-key/": { status: 200, body: { keys: [], latest: 0 } },
            "/e2ee/keys/": { status: 500 },
        });

        const result = await encryptForPartner("sam", "hello");

        expect(result.status).toBe("error");
    });

    test("a network failure is an error", async () => {
        cacheIdentity();
        stubFetch({ "/e2ee/conversation-key/": "throw" });

        const result = await encryptForPartner("sam", "hello");

        expect(result.status).toBe("error");
    });

    test("a failed group key fetch is an error", async () => {
        cacheIdentity();
        stubFetch({ "/e2ee/group-key/": { status: 500 } });

        const result = await encryptForGroup("group-uuid", "hello");

        expect(result.status).toBe("error");
    });

    test("the error carries a reason worth showing", async () => {
        cacheIdentity();
        stubFetch({ "/e2ee/conversation-key/": { status: 500 } });

        const result = await encryptForPartner("sam", "hello");

        expect(result.status === "error" && result.reason.length > 0).toBe(true);
    });
});

describe("a conversation that encrypts normally", () => {
    test("returns ciphertext and the key version it used", async () => {
        cacheIdentity();
        db.set("conv:jess:sam:3", new Uint8Array(32).fill(7));
        stubFetch({
            "/e2ee/conversation-key/": { status: 200, body: { keys: [{ version: 3, wrapped_key: "x" }], latest: 3 } },
        });

        const result = await encryptForPartner("sam", "hello");

        expect(result.status).toBe("encrypted");
        if (result.status !== "encrypted") return;
        expect(result.payload.key_version).toBe(3);
        expect(result.payload.ciphertext.length).toBeGreaterThan(0);
        expect(result.payload.nonce.length).toBeGreaterThan(0);
    });

    test("the plaintext is not present in what gets sent", async () => {
        cacheIdentity();
        db.set("conv:jess:sam:1", new Uint8Array(32).fill(4));
        stubFetch({
            "/e2ee/conversation-key/": { status: 200, body: { keys: [{ version: 1, wrapped_key: "x" }], latest: 1 } },
        });

        const result = await encryptForPartner("sam", "meet me at the old mill");

        expect(result.status).toBe("encrypted");
        if (result.status !== "encrypted") return;
        expect(result.payload.ciphertext).not.toContain("old mill");
    });
});
