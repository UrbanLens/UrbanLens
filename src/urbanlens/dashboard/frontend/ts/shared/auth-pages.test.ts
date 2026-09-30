import { afterEach, beforeEach, describe, expect, test } from "bun:test";

import { installApiKeyChoice, installAuthPages, installSetPassword } from "./auth-pages";
import { e2eeUrlsFromJson } from "./e2ee-urls";

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

const ISLAND = `<script id="e2ee-urls" type="application/json">${JSON.stringify({
    loginParams: "/e2ee/login-params/",
    enroll: "/e2ee/enroll/",
    keys: "/e2ee/keys/",
    rewrap: "/e2ee/rewrap/",
    reset: "/e2ee/reset/",
    partnerKeyBase: "/e2ee/keys/",
    conversationKeyBase: "/e2ee/conversation-key/",
    login: "/login/",
    changePassword: "/e2ee/change-password/",
    passkeyRegister: "/settings/security/passkeys/",
})}</script>`;

let calls: string[] = [];
let unlockState: "unlocked" | "locked" | "not-enrolled" = "unlocked";
let unlockDialogAnswer = true;
let passkeyResult: { ok: boolean; error?: string } = { ok: true };
let passwordResult: Promise<{ ok: boolean; error?: string }> = Promise.resolve({ ok: true });
let navigated: string[] = [];

const e2ee = {
    init: (cfg: { selfSlug: string | null; urls: { changePassword?: string } }) => void calls.push(`init:${cfg.selfSlug}:${cfg.urls.changePassword}`),
    wireLoginForm: (form: HTMLFormElement) => void calls.push(`login:${form.id}`),
    wireSignupForm: (form: HTMLFormElement) => void calls.push(`signup:${form.id}`),
    wireResetConfirmForm: (form: HTMLFormElement, mode: "legacy" | "derived") => {
        calls.push(`reset:${mode}`);
        form.addEventListener("submit", (event) => {
            event.preventDefault();
            calls.push("derive");
        });
    },
    unlockFromLoginAssertion: async (credential: PublicKeyCredential) => {
        calls.push(`unlock-from-assertion:${credential.id}`);
        return true;
    },
    getUnlockState: async () => unlockState,
    enrollOauthIfNeeded: async () => {
        calls.push("enroll-oauth");
        return true;
    },
    showUnlockDialog: async () => unlockDialogAnswer,
    enrollPasskeyUnlock: async () => {
        calls.push("enroll-passkey");
        return passkeyResult;
    },
    changePassword: (current: string, next: string, identifier: string) => {
        calls.push(`change:${current}:${next}:${identifier}`);
        return passwordResult;
    },
};

const realPublicKeyCredential = window.PublicKeyCredential;

beforeEach(() => {
    calls = [];
    navigated = [];
    unlockState = "unlocked";
    unlockDialogAnswer = true;
    passkeyResult = { ok: true };
    passwordResult = Promise.resolve({ ok: true });
});

afterEach(() => {
    Object.defineProperty(window, "PublicKeyCredential", { value: realPublicKeyCredential, configurable: true, writable: true });
});

function withWebAuthn(available: boolean): void {
    Object.defineProperty(window, "PublicKeyCredential", { value: available ? function PublicKeyCredential() {} : undefined, configurable: true, writable: true });
}

describe("e2eeUrlsFromJson", () => {
    test("reads the island's URLs, leaving an endpoint the page lacks absent", () => {
        const urls = e2eeUrlsFromJson(JSON.stringify({ login: "/login/", keys: "/keys/", changePassword: 7 }));
        expect(urls?.login).toBe("/login/");
        expect(urls?.keys).toBe("/keys/");
        expect(urls?.changePassword).toBeUndefined();
        expect(urls?.passkeyWrap).toBeUndefined();
    });

    test("no island, no URLs", () => {
        expect(e2eeUrlsFromJson(null)).toBeNull();
        expect(e2eeUrlsFromJson("null")).toBeNull();
    });
});

describe("installAuthPages", () => {
    test("the login form is wired once the client knows its URLs", () => {
        document.body.innerHTML = `<form id="password-login-form"></form>${ISLAND}`;
        installAuthPages(e2ee, undefined);
        expect(calls).toEqual(["init:null:/e2ee/change-password/", "login:password-login-form"]);
    });

    test("signup", () => {
        document.body.innerHTML = `<form id="signup-form"></form>${ISLAND}`;
        installAuthPages(e2ee, undefined);
        expect(calls).toEqual(["init:null:/e2ee/change-password/", "signup:signup-form"]);
    });

    test("the reset form derives in the account's mode", () => {
        document.body.innerHTML = `<form id="reset-confirm-form" data-e2ee-mode="derived"></form>${ISLAND}`;
        installAuthPages(e2ee, undefined);
        expect(calls).toContain("reset:derived");
        document.body.innerHTML = `<form id="reset-confirm-form" data-e2ee-mode="legacy"></form>${ISLAND}`;
        installAuthPages(e2ee, undefined);
        expect(calls).toContain("reset:legacy");
    });

    test("set-password identifies the signed-in user", () => {
        document.body.innerHTML = `<form id="set-password-form" data-self-slug="owl"></form>${ISLAND}`;
        installAuthPages(e2ee, undefined);
        expect(calls).toEqual(["init:owl:/e2ee/change-password/"]);
    });

    test("2FA runs the passkey login and unlocks messages with the same assertion", async () => {
        document.body.innerHTML = `<div id="webauthn-2fa-status" hidden></div><button id="webauthn-2fa-retry" data-options-url="/2fa/options/" data-verify-url="/2fa/verify/"></button>${ISLAND}`;
        const configs: { optionsUrl: string; verifyUrl: string; retryButtonId: string; statusElId: string; beforeRedirect?: (c: PublicKeyCredential) => Promise<void> }[] = [];
        installAuthPages(e2ee, { runLogin: (cfg) => void configs.push(cfg) });
        expect(configs.map(({ beforeRedirect: _, ...rest }) => rest)).toEqual([
            { optionsUrl: "/2fa/options/", verifyUrl: "/2fa/verify/", retryButtonId: "webauthn-2fa-retry", statusElId: "webauthn-2fa-status" },
        ]);
        const credential: PublicKeyCredential = {
            id: "cred-1",
            rawId: new ArrayBuffer(0),
            type: "public-key",
            authenticatorAttachment: null,
            response: { clientDataJSON: new ArrayBuffer(0) },
            getClientExtensionResults: () => ({}),
            toJSON: () => ({ id: "cred-1", rawId: "", type: "public-key", clientExtensionResults: {}, response: { clientDataJSON: "", authenticatorData: "", signature: "" } }),
        };
        await configs[0]?.beforeRedirect?.(credential);
        expect(calls).toContain("unlock-from-assertion:cred-1");
    });

    test("without the island nothing is wired", () => {
        document.body.innerHTML = `<form id="password-login-form"></form>`;
        installAuthPages(e2ee, undefined);
        expect(calls).toEqual([]);
    });
});

const RESET_FORM = `
<form id="reset-confirm-form">
  <input name="new_password1"><input name="new_password2">
  <input type="hidden" name="revoke_api_keys" id="revoke-api-keys-field" value="">
  <button type="submit">Set</button>
</form>
<dialog id="api-key-choice-dialog"><button data-dialog-close>x</button>
  <button type="button" id="api-key-choice-keep">Keep</button><button type="button" id="api-key-choice-revoke">Revoke</button>
</dialog>`;

function resetPage(filled: boolean): { form: HTMLFormElement; dialog: HTMLDialogElement; field: HTMLInputElement } {
    document.body.innerHTML = RESET_FORM;
    const form = document.querySelector("form");
    const dialog = document.querySelector("dialog");
    const field = document.getElementById("revoke-api-keys-field");
    if (!(form instanceof HTMLFormElement) || !(dialog instanceof HTMLDialogElement) || !(field instanceof HTMLInputElement)) throw new Error("fixture");
    for (const input of form.querySelectorAll<HTMLInputElement>("input[name^=new_password]")) input.value = filled ? "correct horse battery" : "";
    installApiKeyChoice(form);
    e2ee.wireResetConfirmForm(form, "derived");
    calls = [];
    return { form, dialog, field };
}

describe("installApiKeyChoice", () => {
    test("asks before the credential is derived, then derives with the answer", () => {
        const { form, dialog, field } = resetPage(true);
        form.requestSubmit();
        expect(dialog.open).toBe(true);
        expect(calls).toEqual([]);
        document.getElementById("api-key-choice-revoke")?.click();
        expect(field.value).toBe("1");
        expect(dialog.open).toBe(false);
        expect(calls).toEqual(["derive"]);
    });

    test("keeping the keys submits with the field empty", () => {
        const { form, field } = resetPage(true);
        form.requestSubmit();
        document.getElementById("api-key-choice-keep")?.click();
        expect(field.value).toBe("");
        expect(calls).toEqual(["derive"]);
    });

    test("closing the dialog any other way keeps the keys", async () => {
        const { form, dialog, field } = resetPage(true);
        form.requestSubmit();
        dialog.querySelector<HTMLElement>("[data-dialog-close]")?.click();
        await settle();
        expect(field.value).toBe("");
        expect(calls).toEqual(["derive"]);
    });

    test("an empty form is not asked about", () => {
        const { form, dialog } = resetPage(false);
        form.requestSubmit();
        expect(dialog.open).toBe(false);
        expect(calls).toEqual(["derive"]);
    });
});

const SET_PASSWORD = `
<div id="set-password-errors" hidden><p></p></div>
<div id="passkey-option" hidden><button type="button" id="add-passkey-btn">Add a passkey</button><details><div id="password-fields-wrap"></div></details></div>
<form id="set-password-form" data-username="owl" data-done-url="/welcome/">
  <input id="id_new_password"><input id="id_confirm_password">
  <button type="submit" id="set-password-submit">Set password</button>
</form>`;

function setPasswordPage(): { form: HTMLFormElement; errors: HTMLElement; submit: HTMLButtonElement; passkey: HTMLButtonElement } {
    document.body.innerHTML = SET_PASSWORD;
    const form = document.getElementById("set-password-form");
    const errors = document.getElementById("set-password-errors");
    const submit = document.getElementById("set-password-submit");
    const passkey = document.getElementById("add-passkey-btn");
    if (!(form instanceof HTMLFormElement) || !errors || !(submit instanceof HTMLButtonElement) || !(passkey instanceof HTMLButtonElement)) throw new Error("fixture");
    installSetPassword(form, e2ee, (url) => void navigated.push(url));
    return { form, errors, submit, passkey };
}

function typePasswords(first: string, second: string): void {
    const [a, b] = [document.getElementById("id_new_password"), document.getElementById("id_confirm_password")];
    if (a instanceof HTMLInputElement) a.value = first;
    if (b instanceof HTMLInputElement) b.value = second;
}

describe("installSetPassword", () => {
    test("without WebAuthn the password form stands alone", () => {
        withWebAuthn(false);
        const { form } = setPasswordPage();
        expect(document.getElementById("passkey-option")?.hidden).toBe(true);
        expect(form.parentElement).toBe(document.body);
    });

    test("with WebAuthn the passkey leads and the form folds under it", () => {
        withWebAuthn(true);
        const { form } = setPasswordPage();
        expect(document.getElementById("passkey-option")?.hidden).toBe(false);
        expect(form.parentElement?.id).toBe("password-fields-wrap");
    });

    test("passwords that differ are refused before anything is sent", async () => {
        withWebAuthn(false);
        const { form, errors } = setPasswordPage();
        typePasswords("correct horse battery", "correct horse batterx");
        form.requestSubmit();
        await settle();
        expect(errors.hidden).toBe(false);
        expect(errors.textContent).toContain("don't match");
        expect(calls).toEqual([]);
    });

    test("a set password moves on", async () => {
        withWebAuthn(false);
        const { form, submit } = setPasswordPage();
        typePasswords("correct horse battery", "correct horse battery");
        form.requestSubmit();
        expect(submit.disabled).toBe(true);
        await settle();
        expect(calls).toEqual(["change::correct horse battery:owl"]);
        expect(navigated).toEqual(["/welcome/"]);
    });

    test("a refusal is shown and the form can be tried again", async () => {
        withWebAuthn(false);
        passwordResult = Promise.resolve({ ok: false, error: "Too common." });
        const { form, errors, submit } = setPasswordPage();
        typePasswords("password1234", "password1234");
        form.requestSubmit();
        await settle();
        expect(errors.textContent).toBe("Too common.");
        expect(submit.disabled).toBe(false);
        expect(navigated).toEqual([]);
    });

    test("a failed request says so", async () => {
        withWebAuthn(false);
        passwordResult = Promise.reject(new Error("offline"));
        const { form, errors, submit } = setPasswordPage();
        typePasswords("correct horse battery", "correct horse battery");
        form.requestSubmit();
        await settle();
        expect(errors.textContent).toBe("Could not set your password. Please try again.");
        expect(submit.disabled).toBe(false);
    });

    test("a fresh account is enrolled before the passkey wraps its key", async () => {
        withWebAuthn(true);
        unlockState = "not-enrolled";
        const { passkey } = setPasswordPage();
        passkey.click();
        await settle();
        expect(calls).toEqual(["enroll-oauth", "enroll-passkey"]);
        expect(navigated).toEqual(["/welcome/"]);
    });

    test("a locked device that stays locked adds no passkey", async () => {
        withWebAuthn(true);
        unlockState = "locked";
        unlockDialogAnswer = false;
        const { passkey, errors } = setPasswordPage();
        passkey.click();
        await settle();
        expect(calls).toEqual([]);
        expect(errors.textContent).toBe("Unlock your messages first, then try again.");
        expect(passkey.disabled).toBe(false);
    });

    test("a passkey that could not be added says why", async () => {
        withWebAuthn(true);
        passkeyResult = { ok: false, error: "That passkey can't unlock messages." };
        const { passkey, errors } = setPasswordPage();
        passkey.click();
        await settle();
        expect(errors.textContent).toBe("That passkey can't unlock messages.");
        expect(passkey.disabled).toBe(false);
        expect(navigated).toEqual([]);
    });
});
