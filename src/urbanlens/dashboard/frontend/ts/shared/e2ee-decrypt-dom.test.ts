/**
 * Decrypting a thread fetches each key bundle once, and a message that failed stays decryptable.
 */
import { afterEach, beforeEach, describe, expect, test } from "bun:test";

import { installFakeIndexedDB, type FakeIndexedDB } from "../testing/fake-indexeddb";
import { decryptDom, init } from "./e2ee-client";
import { cryptoReady, encryptMessage, generateConversationKey } from "./e2ee-crypto";

const realFetch = globalThis.fetch;
let db: FakeIndexedDB;
let requested: string[];

function stubFetch(bodyFor: (url: string) => unknown): void {
    requested = [];
    globalThis.fetch = ((url: string) => {
        const target = String(url);
        requested.push(target);
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(bodyFor(target)) } as Response);
    }) as unknown as typeof fetch;
}

function cacheIdentity(): void {
    db.set("identity:jess", { privateKey: new Uint8Array(32), publicKey: "pub", version: 1 });
}

function thread(messages: { ct: string; nonce: string; version: number }[]): HTMLElement {
    const root = document.createElement("ul");
    for (const message of messages) {
        const node = document.createElement("li");
        node.dataset.e2eeCt = message.ct;
        node.dataset.e2eeNonce = message.nonce;
        node.dataset.e2eeKv = String(message.version);
        root.appendChild(node);
    }
    return root;
}

const conversationKeyRequests = (): string[] => requested.filter((url) => url.startsWith("/e2ee/conversation-key/"));

beforeEach(async () => {
    await cryptoReady();
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

describe("a thread whose key this device cannot open", () => {
    test("fetches the conversation's keys once, not once per message", async () => {
        cacheIdentity();
        stubFetch(() => ({ keys: [{ version: 2, wrapped_key: "c2VhbGVkIHRvIHNvbWVvbmUgZWxzZQ==" }], latest: 2 }));
        const root = thread(Array.from({ length: 5 }, () => ({ ct: "Y3Q=", nonce: "bm9uY2U=", version: 2 })));

        await decryptDom(root, "sam");

        expect(conversationKeyRequests()).toHaveLength(1);
        expect(root.querySelectorAll(".e2ee-failed")).toHaveLength(5);
    });

    test("fetches again for a version newer than the keys it already holds", async () => {
        cacheIdentity();
        stubFetch(() => ({ keys: [{ version: 1, wrapped_key: "eA==" }], latest: 1 }));

        await decryptDom(thread([{ ct: "Y3Q=", nonce: "bm9uY2U=", version: 1 }]), "sam");
        await decryptDom(thread([{ ct: "Y3Q=", nonce: "bm9uY2U=", version: 2 }]), "sam");

        expect(conversationKeyRequests()).toHaveLength(2);
    });
});

describe("a message that failed to decrypt", () => {
    test("decrypts on a later pass once the device can open its key", async () => {
        const key = generateConversationKey();
        const sealed = encryptMessage("meet at the old mill", key);
        stubFetch(() => ({ keys: [], latest: 0 }));
        const root = thread([{ ct: sealed.ciphertext, nonce: sealed.nonce, version: 1 }]);

        await decryptDom(root, "sam");
        const node = root.querySelector("li")!;
        expect(node.classList.contains("e2ee-failed")).toBe(true);

        cacheIdentity();
        db.set("conv:jess:sam:1", key);
        await decryptDom(root, "sam");

        expect(node.textContent).toBe("meet at the old mill");
        expect(node.classList.contains("e2ee-failed")).toBe(false);
        expect(node.dataset.e2eeCt).toBeUndefined();
    });
});
