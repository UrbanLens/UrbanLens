/**
 * Outgoing encryption must say *why* it produced no ciphertext.
 */
import { afterEach, beforeEach, describe, expect, test } from "bun:test";

import { installFakeIndexedDB, type FakeIndexedDB } from "../testing/fake-indexeddb";
import { encryptForGroup, encryptForPartner, init } from "./e2ee-client";
import { cryptoReady, generateRecoveryKey } from "./e2ee-crypto";
import { NETWORK_FAILURE_MESSAGE } from "./fetch-json";
import { wrapFetch } from "./site-runtime";

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

/** Whether *pending* settled within *ms*, rather than leaving its caller waiting for good. */
async function settlesWithin(pending: Promise<unknown>, ms = 200): Promise<"resolved" | "rejected" | "still waiting"> {
    const timeout = new Promise<"still waiting">((resolve) => setTimeout(() => resolve("still waiting"), ms));
    return Promise.race([pending.then(() => "resolved" as const, () => "rejected" as const), timeout]);
}

describe("an unlock dialog whose options cannot be loaded", () => {
    test("opens on the recovery-key path, as it does for a refused request, instead of never appearing", async () => {
        const { showUnlockDialog } = await import("./e2ee-client");
        stubFetch({ "/e2ee/keys/": "throw" });
        const pending = showUnlockDialog();
        await settle();

        const overlay = document.querySelector(".e2ee-recovery-overlay");
        expect(overlay, "no dialog appeared, so the caller's disabled button never came back").not.toBeNull();
        expect(overlay?.querySelector(".e2ee-unlock-recovery")).not.toBeNull();
        overlay?.querySelector<HTMLButtonElement>(".e2ee-unlock-cancel")?.click();
        expect(await settlesWithin(pending)).toBe("resolved");
    });
});

describe("a passkey unlock enrollment that cannot reach the server", () => {
    const realPublicKeyCredential = window.PublicKeyCredential;

    beforeEach(() => {
        Object.defineProperty(window, "PublicKeyCredential", { value: function PublicKeyCredential() {}, configurable: true, writable: true });
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
                passkeyWrap: "/e2ee/passkey-wrap/",
                passkeyRegisterOptions: "/webauthn/register/options/",
                passkeyRegister: "/webauthn/register/",
            },
            selfSlug: "jess",
        });
        cacheIdentity();
        stubFetch({ "/e2ee/keys/": "throw" });
    });

    afterEach(() => {
        Object.defineProperty(window, "PublicKeyCredential", { value: realPublicKeyCredential, configurable: true, writable: true });
        document.body.innerHTML = "";
    });

    test("without a password to collect, it fails rather than leaving Settings' button disabled for good", async () => {
        const { showPasskeyEnrollDialog } = await import("./e2ee-client");
        expect(await settlesWithin(showPasskeyEnrollDialog(false))).toBe("rejected");
    });

    test("after collecting the password, it says so in the dialog, which stays open for another try", async () => {
        const { showPasskeyEnrollDialog } = await import("./e2ee-client");
        const pending = showPasskeyEnrollDialog(true);
        document.querySelector<HTMLInputElement>(".e2ee-enroll-password")!.value = "account-password";
        document.querySelector<HTMLButtonElement>(".e2ee-enroll-submit")!.click();
        await settle();

        const error = document.querySelector<HTMLElement>(".e2ee-unlock-error");
        expect(error?.hidden).toBe(false);
        expect(error?.textContent).toContain("Could not add that passkey");
        document.querySelector<HTMLButtonElement>(".e2ee-enroll-cancel")!.click();
        expect(await settlesWithin(pending)).toBe("resolved");
    });
});

describe("an unlock that cannot fetch the key bundle", () => {
    const bundle = { enrolled: true, profile_slug: "jess", version: 1, public_key: "pub", password_wrapped_secret: "wrapped", password_wrap_salt: "salt", recovery_wrapped_secret: "wrapped" };

    /** The dialog's own options load, then the unlock's fetch of the same bundle meets *failure*. */
    function bundleThen(failure: "network" | number): string[] {
        const netReports: string[] = [];
        let calls = 0;
        const answer = async (): Promise<Response> => {
            calls += 1;
            if (calls === 1) return Response.json(bundle);
            if (failure === "network") throw new TypeError("Failed to fetch");
            return new Response("", { status: failure });
        };
        globalThis.fetch = wrapFetch(Object.assign(answer, realFetch), (m) => void netReports.push(m));
        return netReports;
    }

    afterEach(() => {
        document.body.innerHTML = "";
    });

    async function unlockWith(field: "password" | "recovery"): Promise<string> {
        const { showUnlockDialog } = await import("./e2ee-client");
        void showUnlockDialog();
        await settle();
        await cryptoReady();
        const value = field === "password" ? "account-password" : generateRecoveryKey().display;
        document.querySelector<HTMLInputElement>(`.e2ee-unlock-${field}`)!.value = value;
        document.querySelector<HTMLButtonElement>(".e2ee-unlock-submit")!.click();
        await settle();
        const error = document.querySelector<HTMLElement>(".e2ee-unlock-error");
        expect(error?.hidden).toBe(false);
        return error?.textContent ?? "";
    }

    for (const field of ["password", "recovery"] as const) {
        test(`a ${field} unlock offline says the server could not be reached, not that the ${field} was wrong`, async () => {
            const netReports = bundleThen("network");

            expect(await unlockWith(field)).toBe(NETWORK_FAILURE_MESSAGE);
            expect(netReports).toEqual([]);
        });

        test(`a ${field} unlock the server refused says the keys could not be loaded`, async () => {
            bundleThen(500);

            expect(await unlockWith(field)).toBe("Couldn't load your encryption keys. Please try again.");
        });
    }

    test("options that cannot be loaded are said in the dialog, not by the fetch net", async () => {
        const netReports: string[] = [];
        const offline = async (): Promise<Response> => {
            throw new TypeError("Failed to fetch");
        };
        globalThis.fetch = wrapFetch(Object.assign(offline, realFetch), (m) => void netReports.push(m));
        const { showUnlockDialog } = await import("./e2ee-client");
        void showUnlockDialog();
        await settle();

        const error = document.querySelector<HTMLElement>(".e2ee-unlock-error");
        expect(error?.hidden).toBe(false);
        expect(error?.textContent).toBe(NETWORK_FAILURE_MESSAGE);
        expect(netReports).toEqual([]);
    });
});
