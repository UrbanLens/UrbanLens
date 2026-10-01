/**
 * The sign-in pages under ``templates/registration/``: each hands its form to the E2EE client, which derives the
 * login credential in the browser so the raw password never reaches the server.
 */

import { e2eeUrlsFromJson } from "./e2ee-urls";

type E2EE = Window["UrbanLensE2EE"];
type WebAuthn = Window["UrbanLensWebAuthn"];
type SetPasswordE2EE = Pick<E2EE, "getUnlockState" | "enrollOauthIfNeeded" | "showUnlockDialog" | "enrollPasskeyUnlock" | "changePassword">;
type AuthE2EE = SetPasswordE2EE & Pick<E2EE, "init" | "wireLoginForm" | "wireSignupForm" | "wireResetConfirmForm" | "unlockFromLoginAssertion">;

const PASSKEY_FAILED = "Could not add that passkey. Please try again.";
const SET_PASSWORD_FAILED = "Could not set your password. Please try again.";

function byId<T extends HTMLElement>(id: string, type: new () => T): T | null {
    const el = document.getElementById(id);
    return el instanceof type ? el : null;
}

function navigateTo(url: string): void {
    window.location.assign(url);
}

/**
 * The password-reset confirmation asks whether to revoke API keys too, before the password is set: that POST is
 * the only moment this flow identifies the account. Must be installed before ``wireResetConfirmForm`` so its
 * ``stopImmediatePropagation`` holds the credential derivation back until a choice is made.
 *
 * Every way out of the dialog but "Revoke them" keeps the keys, as does a browser that never runs this.
 */
export function installApiKeyChoice(form: HTMLFormElement): void {
    const dialog = document.getElementById("api-key-choice-dialog");
    const field = byId("revoke-api-keys-field", HTMLInputElement);
    if (typeof HTMLDialogElement === "undefined" || !(dialog instanceof HTMLDialogElement) || !field) return;

    const choose = (revoke: boolean): void => {
        field.value = revoke ? "1" : "";
        form.dataset.apiKeyChoiceMade = "1";
        dialog.close();
        form.requestSubmit();
    };
    form.addEventListener("submit", (event) => {
        if (form.dataset.apiKeyChoiceMade === "1") return;
        const password = form.elements.namedItem("new_password1");
        const repeat = form.elements.namedItem("new_password2");
        // An empty form fails validation the ordinary way rather than behind a modal.
        if (!(password instanceof HTMLInputElement) || !(repeat instanceof HTMLInputElement) || !password.value || !repeat.value) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        dialog.showModal();
    });
    document.getElementById("api-key-choice-keep")?.addEventListener("click", () => choose(false));
    document.getElementById("api-key-choice-revoke")?.addEventListener("click", () => choose(true));
    dialog.querySelector("[data-dialog-close]")?.addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", () => {
        if (form.dataset.apiKeyChoiceMade !== "1") choose(false);
    });
}

/**
 * The set-password page for accounts that signed in through OAuth: lead with adding a passkey where WebAuthn
 * exists, with the password form folded under it.
 */
export function installSetPassword(form: HTMLFormElement, e2ee: SetPasswordE2EE, navigate: (url: string) => void = navigateTo): void {
    const doneUrl = form.dataset.doneUrl ?? "/";
    const errors = document.getElementById("set-password-errors");
    const showError = (text: string): void => {
        const line = errors?.querySelector("p");
        if (!errors || !line) return;
        line.textContent = text;
        errors.hidden = false;
    };
    const clearError = (): void => {
        if (errors) errors.hidden = true;
    };

    const option = document.getElementById("passkey-option");
    const passkeyBtn = byId("add-passkey-btn", HTMLButtonElement);
    if (window.PublicKeyCredential && option && passkeyBtn) {
        option.hidden = false;
        document.getElementById("password-fields-wrap")?.append(form);
        passkeyBtn.addEventListener("click", async () => {
            clearError();
            passkeyBtn.disabled = true;
            try {
                // A fresh signup may have no E2EE bundle yet, and a cold device a locked one.
                const state = await e2ee.getUnlockState();
                if (state === "not-enrolled") await e2ee.enrollOauthIfNeeded();
                if (state === "locked" && !(await e2ee.showUnlockDialog())) {
                    passkeyBtn.disabled = false;
                    showError("Unlock your messages first, then try again.");
                    return;
                }
                const result = await e2ee.enrollPasskeyUnlock();
                if (result.ok) {
                    navigate(doneUrl);
                    return;
                }
                showError(result.error || PASSKEY_FAILED);
            } catch {
                showError(PASSKEY_FAILED);
            }
            passkeyBtn.disabled = false;
        });
    }

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const password = byId("id_new_password", HTMLInputElement);
        const repeat = byId("id_confirm_password", HTMLInputElement);
        const submit = byId("set-password-submit", HTMLButtonElement);
        if (!password || !repeat) return;
        clearError();
        if (password.value !== repeat.value) {
            showError("Those passwords don't match.");
            return;
        }
        if (submit) submit.disabled = true;
        try {
            const result = await e2ee.changePassword("", password.value, form.dataset.username ?? "");
            if (result.ok) {
                navigate(doneUrl);
                return;
            }
            showError(result.error || SET_PASSWORD_FAILED);
        } catch {
            showError(SET_PASSWORD_FAILED);
        }
        if (submit) submit.disabled = false;
    });
}

/** Configure the client from the page's ``#e2ee-urls`` island and wire whichever sign-in form the page has. */
export function installAuthPages(e2ee: AuthE2EE | undefined, webauthn: Pick<WebAuthn, "runLogin"> | undefined): void {
    const urls = e2eeUrlsFromJson(document.getElementById("e2ee-urls")?.textContent);
    if (!e2ee || !urls) return;
    const setPasswordForm = byId("set-password-form", HTMLFormElement);
    e2ee.init({ selfSlug: setPasswordForm?.dataset.selfSlug || null, urls });

    const login = byId("password-login-form", HTMLFormElement);
    if (login) e2ee.wireLoginForm(login);
    const signup = byId("signup-form", HTMLFormElement);
    if (signup) e2ee.wireSignupForm(signup);
    const reset = byId("reset-confirm-form", HTMLFormElement);
    if (reset) {
        installApiKeyChoice(reset);
        e2ee.wireResetConfirmForm(reset, reset.dataset.e2eeMode === "derived" ? "derived" : "legacy");
    }
    if (setPasswordForm) installSetPassword(setPasswordForm, e2ee);

    const retry = document.getElementById("webauthn-2fa-retry");
    if (retry && webauthn) {
        webauthn.runLogin({
            optionsUrl: retry.dataset.optionsUrl ?? "",
            verifyUrl: retry.dataset.verifyUrl ?? "",
            retryButtonId: retry.id,
            statusElId: "webauthn-2fa-status",
            // Passkeys holding an E2EE wrap carry PRF inputs, so the tap that finished 2FA also unlocks messages on a
            // cold device.
            beforeRedirect: async (credential) => {
                await e2ee.unlockFromLoginAssertion(credential);
            },
        });
    }
}
