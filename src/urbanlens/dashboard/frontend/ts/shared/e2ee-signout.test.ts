/**
 * Signing out has to discard the cached keys, and must never be blocked by that.
 */
import { afterEach, beforeEach, describe, expect, test } from "bun:test";

import { installFakeIndexedDB, type FakeIndexedDB } from "../testing/fake-indexeddb";
import { init, wireSignOutForm } from "./e2ee-client";

let db: FakeIndexedDB;

const CONFIG = {
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
};

/** A sign-out form whose native submit is recorded rather than performed. */
function signOutForm(): { form: HTMLFormElement; submitted: () => number } {
    const form = document.createElement("form");
    form.className = "nav-dropdown-logout-form";
    let count = 0;
    form.submit = () => {
        count += 1;
    };
    document.body.append(form);
    return { form, submitted: () => count };
}

/** Submit the form the way a click on its button would. */
function submit(form: HTMLFormElement): boolean {
    return form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
}

/** Let the clear's promise chain settle. */
const settle = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0));

beforeEach(() => {
    db = installFakeIndexedDB("keys");
    init(CONFIG);
});

afterEach(() => {
    document.body.innerHTML = "";
    db.uninstall();
});

describe("signing out", () => {
    test("discards this profile's cached keys", async () => {
        db.set("identity:jess", { privateKey: new Uint8Array(32), publicKey: "pub", version: 1 });
        db.set("conv:jess:someone", { key: new Uint8Array(32) });
        const { form } = signOutForm();
        wireSignOutForm(form);

        submit(form);
        await settle();

        expect(db.get("identity:jess")).toBeUndefined();
        expect(db.get("conv:jess:someone")).toBeUndefined();
    });

    test("still submits the form", async () => {
        const { form, submitted } = signOutForm();
        wireSignOutForm(form);

        submit(form);
        await settle();

        expect(submitted()).toBe(1);
    });

    test("submits even when there is no IndexedDB at all", async () => {
        // A private window, a browser with site data blocked, or an origin over quota.
        db.uninstall();
        delete (globalThis as { indexedDB?: unknown }).indexedDB;
        const { form, submitted } = signOutForm();
        wireSignOutForm(form);

        submit(form);
        await settle();

        expect(submitted(), "a storage failure must not strand someone on the page").toBe(1);
    });

    test("leaves another profile's keys alone", async () => {
        db.set("identity:jess", { privateKey: new Uint8Array(32), publicKey: "pub", version: 1 });
        db.set("identity:someone-else", { privateKey: new Uint8Array(32), publicKey: "pub", version: 1 });
        const { form } = signOutForm();
        wireSignOutForm(form);

        submit(form);
        await settle();

        expect(db.get("identity:jess")).toBeUndefined();
        expect(db.get("identity:someone-else")).toBeDefined();
    });

    test("does nothing when nobody is signed in", async () => {
        init({ ...CONFIG, selfSlug: null });
        const { form, submitted } = signOutForm();
        wireSignOutForm(form);

        // Not prevented, so the browser performs the submit itself - which is
        // why `submitted()` stays 0 here rather than 1.
        expect(submit(form)).toBe(true);
        await settle();
        expect(submitted()).toBe(0);
    });
});
