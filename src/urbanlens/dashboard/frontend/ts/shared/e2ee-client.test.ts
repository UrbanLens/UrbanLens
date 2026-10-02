/**
 * Outgoing encryption must say *why* it produced no ciphertext.
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

describe("a sign-in refused for asking too often", () => {
    test("says so, and never submits the raw password", async () => {
        const { wireLoginForm } = await import("./e2ee-client");
        stubFetch({ "/e2ee/login-params/": { status: 429 } });
        document.body.innerHTML = `
            <form id="login"><input name="username" value="jess"><input name="password" value="secret">
            <button type="submit" class="btn">Sign in</button></form>`;
        const form = document.getElementById("login") as HTMLFormElement;
        let submitted = false;
        form.submit = () => {
            submitted = true;
        };
        wireLoginForm(form);

        form.dispatchEvent(new Event("submit", { cancelable: true }));
        await new Promise((resolve) => setTimeout(resolve, 20));

        expect(submitted).toBe(false);
        expect(document.body.textContent).toContain("Too many sign-in attempts");
        expect(form.querySelector("button")!.classList.contains("is-loading")).toBe(false);
    });
});

/** Records every request, answering each URL prefix as `stubFetch` would. */
function recordFetch(routes: Record<string, Route>): Array<{ url: string; body: string }> {
    const sent: Array<{ url: string; body: string }> = [];
    globalThis.fetch = ((url: string, init?: RequestInit) => {
        const target = String(url);
        const body = init?.body instanceof FormData ? JSON.stringify(Object.fromEntries(init.body.entries())) : String(init?.body ?? "");
        sent.push({ url: target, body });
        const match = Object.keys(routes).find((prefix) => target.startsWith(prefix));
        const route = match ? routes[match]! : { status: 404 };
        if (route === "throw") return Promise.reject(new Error("offline"));
        return Promise.resolve({
            ok: route.status >= 200 && route.status < 300,
            status: route.status,
            redirected: false,
            url: target,
            json: () => Promise.resolve(route.body ?? {}),
            text: () => Promise.resolve(""),
        } as Response);
    }) as unknown as typeof fetch;
    return sent;
}

function loginForm(): { form: HTMLFormElement; submitted: () => boolean } {
    document.body.innerHTML = `
        <form id="login"><input name="username" value="jess"><input name="password" value="raw-secret-password">
        <button type="submit" class="btn">Sign in</button></form>`;
    const form = document.getElementById("login") as HTMLFormElement;
    let submitted = false;
    form.submit = () => {
        submitted = true;
    };
    return { form, submitted: () => submitted };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 50));

describe("a sign-in that cannot learn how the account signs in", () => {
    for (const [label, route] of [["a 500", { status: 500 }], ["a 503", { status: 503 }], ["a network failure", "throw"]] as const) {
        test(`${label} on the sign-in parameters never sends the raw password anywhere`, async () => {
            const { wireLoginForm } = await import("./e2ee-client");
            const sent = recordFetch({ "/e2ee/login-params/": route });
            const { form, submitted } = loginForm();
            wireLoginForm(form);

            form.dispatchEvent(new Event("submit", { cancelable: true }));
            await settle();

            expect(submitted(), "the form fell back to a native submit of the raw password").toBe(false);
            expect(sent.some((request) => request.body.includes("raw-secret-password"))).toBe(false);
            expect(document.body.textContent).toContain("Couldn't sign you in");
            expect(form.querySelector("button")!.classList.contains("is-loading")).toBe(false);
        });
    }

    test("a derived-mode sign-in that fails after its parameters arrive does not fall back to the raw password", async () => {
        const { wireLoginForm } = await import("./e2ee-client");
        const sent = recordFetch({ "/e2ee/login-params/": { status: 200, body: { mode: "derived", auth_salt: "AAAAAAAAAAAAAAAAAAAAAA==" } }, "/login/": "throw" });
        const { form, submitted } = loginForm();
        wireLoginForm(form);

        form.dispatchEvent(new Event("submit", { cancelable: true }));
        await settle();

        expect(submitted()).toBe(false);
        expect(sent.some((request) => request.body.includes("raw-secret-password"))).toBe(false);
        expect(document.body.textContent).toContain("Couldn't sign you in");
    });

    test("a legacy-mode sign-in that fails still gets through by the plain form, whose credential is the password", async () => {
        const { wireLoginForm } = await import("./e2ee-client");
        recordFetch({ "/e2ee/login-params/": { status: 200, body: { mode: "legacy" } }, "/login/": "throw" });
        const { form, submitted } = loginForm();
        wireLoginForm(form);

        form.dispatchEvent(new Event("submit", { cancelable: true }));
        await settle();

        expect(submitted()).toBe(true);
    });
});

describe("a password check that cannot learn how the account signs in", () => {
    test("a key reset refuses rather than sending the raw password as proof", async () => {
        const { init: reinit, resetKeys } = await import("./e2ee-client");
        reinit({
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
            loginIdentifier: "jess",
        });
        const sent = recordFetch({
            "/e2ee/login-params/": { status: 500 },
            "/e2ee/keys/": { status: 200, body: { enrolled: true, profile_slug: "jess", version: 1, public_key: "pub" } },
            "/e2ee/reset/": { status: 200, body: { version: 2 } },
        });

        expect(await resetKeys("raw-secret-password")).toBeNull();
        expect(sent.some((request) => request.url.startsWith("/e2ee/reset/"))).toBe(false);
        expect(sent.some((request) => request.body.includes("raw-secret-password"))).toBe(false);
    });
});
