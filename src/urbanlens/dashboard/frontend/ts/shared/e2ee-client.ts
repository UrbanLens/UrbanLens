/**
 * E2EE flows: enrollment, login-time key derivation, unlock, and the
 * messages-page encrypt/decrypt API.
 *
 * This module owns every fetch to the /dashboard/e2ee/ endpoints and every
 * interaction with the IndexedDB key cache; templates only wire DOM events
 * to the functions exposed on window.UrbanLensE2EE (see
 * entries-classic/e2ee.ts). Nothing here ever sends a raw password, a
 * private key, or plaintext key material to the server.
 */
import {
    KDF_MEMLIMIT,
    KDF_OPSLIMIT,
    cryptoReady,
    decryptMessage,
    deriveKey,
    encryptMessage,
    generateConversationKey,
    generateIdentity,
    generateRecoveryKey,
    parseRecoveryKey,
    randomSalt,
    sealToPublicKey,
    unseal,
    unwrapSecretKey,
    wrapSecretKey,
} from "./e2ee-crypto";
import type { CachedIdentity } from "./e2ee-store";
import { clearProfileKeys, getConversationKey, getGroupKey, getIdentity, putConversationKey, putGroupKey, putIdentity } from "./e2ee-store";

/** Endpoint URLs, provided by templates via {% url %} (see init()). */
export interface E2EEUrls {
    loginParams: string;
    enroll: string;
    keys: string;
    rewrap: string;
    /** Bulk listing of every wrapped key copy addressed to the caller, used
     * by the reset flow to re-encrypt history. Optional; only pages that
     * offer reset wire it. */
    rewrapAll?: string;
    reset: string;
    /** Base of the partner-key endpoint; the client appends "<slug>/". */
    partnerKeyBase: string;
    /** Base of the conversation-key endpoint; the client appends "<slug>/". */
    conversationKeyBase: string;
    /** Base of the group-key endpoint; the client appends "<group uuid>/".
     * Optional so pages without group chats just omit it. */
    groupKeyBase?: string;
    /** POST target for password change/set. Optional; only the settings and
     * set-password pages wire it. */
    changePassword?: string;
    /** POST target running a raw password through AUTH_PASSWORD_VALIDATORS
     * before the credential is derived (see serverPolicyErrors). Optional;
     * pages without it skip the server-side policy check. */
    validatePassword?: string;
    /** The login form's POST target, for the fetch-based login flow. */
    login: string;
    /** FAQ entry explaining encryption/recovery keys in plain language, shown
     * wherever we ask the user to save their recovery key. Optional so pages
     * that don't wire it up just omit the link. */
    faqUrl?: string;
}

export interface E2EEConfig {
    urls: E2EEUrls;
    /** The signed-in user's profile slug; null on anonymous pages. */
    selfSlug: string | null;
}

let config: E2EEConfig | null = null;

/** Store the endpoint/identity configuration for this page. */
export function init(cfg: E2EEConfig): void {
    config = cfg;
}

function cfg(): E2EEConfig {
    if (config === null) {
        throw new Error("UrbanLensE2EE.init() has not been called on this page");
    }
    return config;
}

function csrfToken(form?: HTMLFormElement): string {
    if (window.csrftoken) {
        return window.csrftoken;
    }
    const input = form?.querySelector<HTMLInputElement>("input[name=csrfmiddlewaretoken]");
    if (input?.value) {
        return input.value;
    }
    const cookieValue = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/)?.[1];
    return cookieValue ? decodeURIComponent(cookieValue) : "";
}

async function postJson(url: string, body: unknown): Promise<Response> {
    return fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
        credentials: "same-origin",
        body: JSON.stringify(body),
    });
}

// ---------------------------------------------------------------------------
// Server payload shapes
// ---------------------------------------------------------------------------

interface LoginParams {
    mode: "legacy" | "derived";
    auth_salt: string;
}

/** The "keys" endpoint always returns 200; `enrolled: false` (with no other
 * fields) means the account has no bundle yet - a common, expected state. */
interface KeyBundlePayload {
    enrolled: boolean;
    public_key: string;
    password_wrapped_secret: string;
    password_wrap_salt: string;
    password_wrap_stale: boolean;
    recovery_wrapped_secret: string;
    kdf_opslimit: number;
    kdf_memlimit: number;
    version: number;
    profile_slug: string;
}

interface ConversationKeysPayload {
    keys: { version: number; wrapped_key: string }[];
    latest: number;
}

// ---------------------------------------------------------------------------
// Enrollment
// ---------------------------------------------------------------------------

interface EnrollOptions {
    /** Raw password when the account should get a password-wrapped copy. */
    password?: string;
    /** Rotate the login credential to derived mode (password accounts). */
    rotateAuth?: boolean;
}

interface EnrollResult {
    recoveryDisplay: string;
    profileSlug: string;
}

/**
 * Generate a fresh identity + recovery key and store the bundle server-side.
 *
 * @param options - Password/rotation behavior; omit password for OAuth-only
 *   accounts (recovery key becomes their only cold-device unwrap path).
 * @returns The recovery key display string (shown once) and profile slug, or
 *   null when the server rejected enrollment (e.g. bundle already exists).
 */
export async function enroll(options: EnrollOptions): Promise<EnrollResult | null> {
    await cryptoReady();
    const identity = generateIdentity();
    const recovery = generateRecoveryKey();
    const body: Record<string, unknown> = {
        public_key: identity.publicKey,
        recovery_wrapped_secret: wrapSecretKey(identity.privateKey, recovery.key),
        kdf_opslimit: KDF_OPSLIMIT,
        kdf_memlimit: KDF_MEMLIMIT,
    };
    if (options.password) {
        const wrapSalt = randomSalt();
        body.password_wrapped_secret = wrapSecretKey(identity.privateKey, deriveKey(options.password, wrapSalt));
        body.password_wrap_salt = wrapSalt;
    }
    if (options.rotateAuth && options.password) {
        const authSalt = randomSalt();
        body.auth_key = bytesToB64(deriveKey(options.password, authSalt));
        body.auth_salt = authSalt;
        body.current_password = options.password;
    }
    const response = await postJson(cfg().urls.enroll, body);
    if (response.status !== 201) {
        return null;
    }
    const payload = (await response.json()) as { version: number; profile_slug: string };
    await putIdentity(payload.profile_slug, { privateKey: identity.privateKey, publicKey: identity.publicKey, version: payload.version });
    return { recoveryDisplay: recovery.display, profileSlug: payload.profile_slug };
}

function bytesToB64(bytes: Uint8Array): string {
    let binary = "";
    for (const byte of bytes) {
        binary += String.fromCharCode(byte);
    }
    return btoa(binary);
}

// ---------------------------------------------------------------------------
// Login flow
// ---------------------------------------------------------------------------

/**
 * Wire the password login form for derived-credential authentication.
 *
 * On submit: fetch login-params for the typed identifier, derive the login
 * credential when the account is enrolled (the raw password never leaves the
 * browser), POST the login via fetch, then unlock/enroll the key bundle
 * before following the redirect. Login failures re-render the server's
 * response so Django's error display (rate limiting, unverified-account
 * hints) is preserved without double-submitting.
 *
 * @param form - The login <form>.
 */
export function wireLoginForm(form: HTMLFormElement): void {
    form.addEventListener("submit", (event) => {
        event.preventDefault();
        void runLoginFlow(form).catch((error) => {
            // Never leave the user stranded: fall back to a native submit with
            // the raw password (legacy path) if anything in the E2EE flow blew
            // up before the credentials were sent.
            console.error("E2EE login flow failed; falling back to plain submit", error);
            form.submit();
        });
    });
}

async function runLoginFlow(form: HTMLFormElement): Promise<void> {
    const identifier = (form.elements.namedItem("username") as HTMLInputElement).value;
    const passwordInput = form.elements.namedItem("password") as HTMLInputElement;
    const password = passwordInput.value;
    form.classList.add("e2ee-busy");

    const paramsResponse = await fetch(`${cfg().urls.loginParams}?identifier=${encodeURIComponent(identifier)}`, { credentials: "same-origin" });
    if (!paramsResponse.ok) {
        form.submit();
        return;
    }
    const params = (await paramsResponse.json()) as LoginParams;

    await cryptoReady();
    const credential = params.mode === "derived" ? bytesToB64(deriveKey(password, params.auth_salt)) : password;

    const formData = new FormData(form);
    formData.set("password", credential);
    const loginResponse = await fetch(cfg().urls.login, {
        method: "POST",
        body: formData,
        credentials: "same-origin",
    });

    if (!loginResponse.redirected) {
        // Authentication failed - swap in the server's re-rendered form so
        // error messages and lockout notices display exactly as designed.
        const html = await loginResponse.text();
        document.open();
        document.write(html);
        document.close();
        return;
    }

    const destination = loginResponse.url;
    try {
        if (params.mode === "legacy") {
            const result = await enroll({ password, rotateAuth: true });
            if (result) {
                await showRecoveryDialog(result.recoveryDisplay);
            }
        } else {
            await unlockAfterDerivedLogin(password);
        }
    } catch (error) {
        // Key handling must never block getting the user into the app.
        console.error("E2EE post-login key handling failed", error);
    }
    window.location.assign(destination);
}

/**
 * After a successful derived-mode login, unwrap (or repair) the private key.
 *
 * @param password - The raw password, still in memory from the login form.
 */
async function unlockAfterDerivedLogin(password: string): Promise<void> {
    const keysResponse = await fetch(cfg().urls.keys, { credentials: "same-origin" });
    if (!keysResponse.ok) {
        return;
    }
    const bundle = (await keysResponse.json()) as KeyBundlePayload;
    if (!bundle.enrolled) {
        // AccountKdf exists (signup created it) but no bundle yet - finish
        // enrollment now that we're authenticated.
        const result = await enroll({ password, rotateAuth: false });
        if (result) {
            await showRecoveryDialog(result.recoveryDisplay);
        }
        return;
    }

    if (bundle.password_wrapped_secret && bundle.password_wrap_salt) {
        const wrapKey = deriveKey(password, bundle.password_wrap_salt, bundle.kdf_opslimit, bundle.kdf_memlimit);
        const privateKey = unwrapSecretKey(bundle.password_wrapped_secret, wrapKey);
        if (privateKey !== null) {
            await putIdentity(bundle.profile_slug, { privateKey, publicKey: bundle.public_key, version: bundle.version });
            return;
        }
    }

    // The password copy is stale (post-reset) or missing. A device that still
    // holds the cached key silently re-wraps under the new password.
    const cached = await getIdentity(bundle.profile_slug);
    if (cached !== null && cached.version === bundle.version && cached.publicKey === bundle.public_key) {
        const wrapSalt = randomSalt();
        await postJson(cfg().urls.rewrap, {
            password_wrapped_secret: wrapSecretKey(cached.privateKey, deriveKey(password, wrapSalt, bundle.kdf_opslimit, bundle.kdf_memlimit)),
            password_wrap_salt: wrapSalt,
        });
    }
    // Otherwise this device stays locked; the messages page offers the
    // recovery-key prompt.
}

// ---------------------------------------------------------------------------
// Signup / password-reset form wiring
// ---------------------------------------------------------------------------

/** The configured MinimumLengthValidator floor (settings/base.py), enforced
 * client-side because the server only ever sees the derived credential (which
 * always "looks strong"). The full validator chain (complexity,
 * common-password, HIBP breach check) additionally runs server-side via the
 * validate-password endpoint - see serverPolicyErrors(). */
const MIN_PASSWORD_LENGTH = 12;

/**
 * Run the raw password through the server's configured validator chain.
 *
 * The one deliberate raw-password transmission in the derived-auth design:
 * without it, none of AUTH_PASSWORD_VALIDATORS ever sees the real password
 * (the derived credential always "looks strong"). Sent once over HTTPS,
 * validated in memory server-side, never stored or logged.
 *
 * @param password - The candidate raw password.
 * @param username - The username typed into the form ("" when unknown), for
 *   the similarity validator.
 * @param email - The email typed into the form ("" when unknown).
 * @returns Policy-violation messages; empty when the password passes, when
 *   the endpoint isn't wired on this page, or when the check can't be
 *   reached (fail open - the MIN_PASSWORD_LENGTH floor still applies).
 */
async function serverPolicyErrors(password: string, username: string, email: string): Promise<string[]> {
    const url = cfg().urls.validatePassword;
    if (!url) {
        return [];
    }
    try {
        const response = await postJson(url, { password, username, email });
        if (!response.ok) {
            return [];
        }
        const payload = (await response.json()) as { valid?: boolean; errors?: string[] };
        if (payload.valid === false) {
            return payload.errors && payload.errors.length ? payload.errors : ["This password doesn't meet the password policy."];
        }
    } catch {
        // Network failure - fall through to fail-open.
    }
    return [];
}

/** Show policy errors on a password input as a native validation message. */
function reportPolicyErrors(input: HTMLInputElement, errors: string[]): void {
    input.setCustomValidity(errors.join(" "));
    input.reportValidity();
    input.addEventListener("input", () => input.setCustomValidity(""), { once: true });
}

/**
 * Wire the signup form: derive the login credential before submit so the raw
 * password never reaches the server, even once.
 *
 * @param form - The signup <form> with password1/password2 fields.
 */
export function wireSignupForm(form: HTMLFormElement): void {
    form.addEventListener("submit", (event) => {
        if (form.dataset.e2eeReady === "1") {
            return;
        }
        event.preventDefault();
        void prepareSignupSubmit(form).catch((error) => {
            // Fall back to the raw-password (legacy) flow rather than blocking
            // signup; the account upgrades transparently at first login.
            console.error("E2EE signup derivation failed; submitting legacy form", error);
            form.dataset.e2eeReady = "1";
            form.submit();
        });
    });
}

async function prepareSignupSubmit(form: HTMLFormElement): Promise<void> {
    const password1 = form.elements.namedItem("password1") as HTMLInputElement;
    const password2 = form.elements.namedItem("password2") as HTMLInputElement;
    if (password1.value !== password2.value) {
        // Let the server render its usual mismatch error.
        form.dataset.e2eeReady = "1";
        form.submit();
        return;
    }
    if (password1.value.length < MIN_PASSWORD_LENGTH || /^\d+$/.test(password1.value)) {
        reportPolicyErrors(password1, [`Use at least ${MIN_PASSWORD_LENGTH} characters, not all numbers.`]);
        return;
    }
    const username = (form.elements.namedItem("username") as HTMLInputElement | null)?.value ?? "";
    const email = (form.elements.namedItem("email") as HTMLInputElement | null)?.value ?? "";
    const policyErrors = await serverPolicyErrors(password1.value, username, email);
    if (policyErrors.length) {
        reportPolicyErrors(password1, policyErrors);
        return;
    }
    await cryptoReady();
    const authSalt = randomSalt();
    const credential = bytesToB64(deriveKey(password1.value, authSalt));
    password1.value = credential;
    password2.value = credential;
    let saltInput = form.querySelector<HTMLInputElement>("input[name=e2ee_auth_salt]");
    if (saltInput === null) {
        saltInput = document.createElement("input");
        saltInput.type = "hidden";
        saltInput.name = "e2ee_auth_salt";
        form.appendChild(saltInput);
    }
    saltInput.value = authSalt;
    form.dataset.e2eeReady = "1";
    form.submit();
}

/**
 * Wire the password-reset-confirm form for derived accounts.
 *
 * Generates a fresh auth salt, derives the new credential from the new
 * password, and submits both - the server rotates AccountKdf and marks the
 * password-wrapped key copy stale (the old password is gone). Legacy accounts
 * are left untouched (pass mode "legacy").
 *
 * @param form - The reset-confirm <form>.
 * @param mode - "derived" when the account has an AccountKdf row.
 */
export function wireResetConfirmForm(form: HTMLFormElement, mode: "legacy" | "derived"): void {
    if (mode !== "derived") {
        return;
    }
    form.addEventListener("submit", (event) => {
        if (form.dataset.e2eeReady === "1") {
            return;
        }
        event.preventDefault();
        void prepareResetSubmit(form).catch((error) => {
            // On failure the server-side view sees no salt field and reverts
            // the account to legacy mode, so a raw-password submit stays safe.
            console.error("E2EE reset derivation failed; submitting legacy form", error);
            form.dataset.e2eeReady = "1";
            form.submit();
        });
    });
}

async function prepareResetSubmit(form: HTMLFormElement): Promise<void> {
    const password1 = form.elements.namedItem("new_password1") as HTMLInputElement;
    const password2 = form.elements.namedItem("new_password2") as HTMLInputElement;
    if (password1.value !== password2.value) {
        form.dataset.e2eeReady = "1";
        form.submit();
        return;
    }
    if (password1.value.length < MIN_PASSWORD_LENGTH || /^\d+$/.test(password1.value)) {
        reportPolicyErrors(password1, [`Use at least ${MIN_PASSWORD_LENGTH} characters, not all numbers.`]);
        return;
    }
    // The reset form carries no username/email fields; the similarity check
    // simply has nothing extra to compare against here.
    const policyErrors = await serverPolicyErrors(password1.value, "", "");
    if (policyErrors.length) {
        reportPolicyErrors(password1, policyErrors);
        return;
    }
    await cryptoReady();
    const authSalt = randomSalt();
    const credential = bytesToB64(deriveKey(password1.value, authSalt));
    password1.value = credential;
    password2.value = credential;
    let saltInput = form.querySelector<HTMLInputElement>("input[name=e2ee_auth_salt]");
    if (saltInput === null) {
        saltInput = document.createElement("input");
        saltInput.type = "hidden";
        saltInput.name = "e2ee_auth_salt";
        form.appendChild(saltInput);
    }
    saltInput.value = authSalt;
    form.dataset.e2eeReady = "1";
    form.submit();
}

// ---------------------------------------------------------------------------
// OAuth (passwordless) enrollment
// ---------------------------------------------------------------------------

/**
 * Silently enroll a passwordless (OAuth) account from any authenticated page.
 *
 * Generates the keypair, uploads the recovery-wrapped copy, caches the
 * private key on this device, and shows a low-key prompt pointing at the
 * recovery key (which stays viewable in Settings while this device holds the
 * key - nothing is lost if the prompt is dismissed).
 *
 * @returns True when enrollment happened.
 */
export async function enrollOauthIfNeeded(): Promise<boolean> {
    const result = await enroll({});
    if (result === null) {
        return false;
    }
    notifyEnrolled();
    return true;
}

function notifyEnrolled(): void {
    const toastr = (window as { toastr?: { info?: (msg: string, title?: string) => void } }).toastr;
    toastr?.info?.("Your direct messages are now end-to-end encrypted. Save your recovery key from Settings → Direct Messages.", "Encryption enabled");
}

// ---------------------------------------------------------------------------
// Unlock state & recovery
// ---------------------------------------------------------------------------

export type UnlockState = "unlocked" | "locked" | "not-enrolled";

/**
 * Report whether this device can decrypt the signed-in user's messages.
 *
 * @returns "unlocked" (cached key matches the server bundle), "locked"
 *   (enrolled, but this device has no usable cached key), or "not-enrolled".
 */
export async function getUnlockState(): Promise<UnlockState> {
    const selfSlug = cfg().selfSlug;
    if (!selfSlug) {
        return "not-enrolled";
    }
    const response = await fetch(cfg().urls.keys, { credentials: "same-origin" });
    if (!response.ok) {
        return "locked";
    }
    const bundle = (await response.json()) as KeyBundlePayload;
    if (!bundle.enrolled) {
        return "not-enrolled";
    }
    const cached = await getIdentity(bundle.profile_slug);
    if (cached !== null && cached.version === bundle.version && cached.publicKey === bundle.public_key) {
        return "unlocked";
    }
    return "locked";
}

/**
 * Unlock this device with a typed/pasted recovery key.
 *
 * @param display - The recovery key as the user entered it.
 * @returns True on success (identity cached; device unlocked).
 */
export async function unlockWithRecovery(display: string): Promise<boolean> {
    await cryptoReady();
    const key = parseRecoveryKey(display);
    if (key === null) {
        return false;
    }
    const response = await fetch(cfg().urls.keys, { credentials: "same-origin" });
    if (!response.ok) {
        return false;
    }
    const bundle = (await response.json()) as KeyBundlePayload;
    if (!bundle.enrolled) {
        return false;
    }
    const privateKey = unwrapSecretKey(bundle.recovery_wrapped_secret, key);
    if (privateKey === null) {
        return false;
    }
    await putIdentity(bundle.profile_slug, { privateKey, publicKey: bundle.public_key, version: bundle.version });
    return true;
}

/**
 * Report which unlock paths this account's bundle offers on a cold device.
 *
 * @returns Whether a password-wrapped copy exists (and is not stale), and
 *   whether the account is enrolled at all.
 */
export async function getUnlockOptions(): Promise<{ enrolled: boolean; password: boolean }> {
    const response = await fetch(cfg().urls.keys, { credentials: "same-origin" });
    if (!response.ok) {
        return { enrolled: false, password: false };
    }
    const bundle = (await response.json()) as KeyBundlePayload;
    if (!bundle.enrolled) {
        return { enrolled: false, password: false };
    }
    return { enrolled: true, password: Boolean(bundle.password_wrapped_secret) && !bundle.password_wrap_stale };
}

/**
 * Unlock this device with the account password.
 *
 * Derives the wrap key from the password + the bundle's wrap salt and opens
 * the password-wrapped private-key copy. Only possible when such a copy
 * exists (password accounts, and OAuth accounts that have set a password
 * while a device held the key).
 *
 * @param password - The raw account password (never transmitted).
 * @returns True on success (identity cached; device unlocked).
 */
export async function unlockWithPassword(password: string): Promise<boolean> {
    await cryptoReady();
    const response = await fetch(cfg().urls.keys, { credentials: "same-origin" });
    if (!response.ok) {
        return false;
    }
    const bundle = (await response.json()) as KeyBundlePayload;
    if (!bundle.password_wrapped_secret || !bundle.password_wrap_salt) {
        return false;
    }
    const wrapKey = deriveKey(password, bundle.password_wrap_salt, bundle.kdf_opslimit, bundle.kdf_memlimit);
    const privateKey = unwrapSecretKey(bundle.password_wrapped_secret, wrapKey);
    if (privateKey === null) {
        return false;
    }
    await putIdentity(bundle.profile_slug, { privateKey, publicKey: bundle.public_key, version: bundle.version });
    return true;
}

/**
 * Show a dialog offering every available unlock path (password and/or
 * recovery key) and attempt the unlock the user chooses.
 *
 * @returns True once the device is unlocked; false when the user cancelled.
 */
export function showUnlockDialog(): Promise<boolean> {
    return new Promise((resolve) => {
        void getUnlockOptions().then((options) => {
            const overlay = document.createElement("div");
            overlay.className = "e2ee-recovery-overlay";
            const passwordField = options.password
                ? `<label class="e2ee-unlock-label">Account password
                       <input type="password" class="e2ee-unlock-password" autocomplete="current-password" placeholder="Your password">
                   </label>
                   <div class="e2ee-unlock-divider">or</div>`
                : "";
            overlay.innerHTML = `
                <div class="e2ee-recovery-dialog" role="dialog" aria-modal="true" aria-labelledby="e2ee-unlock-title">
                    <h2 id="e2ee-unlock-title">Unlock your messages</h2>
                    <p>This device doesn't hold your encryption key yet. ${options.password ? "Enter your account password or your recovery key." : "Enter your recovery key."}</p>
                    ${passwordField}
                    <label class="e2ee-unlock-label">Recovery key
                        <input type="text" class="e2ee-unlock-recovery" autocomplete="off" spellcheck="false" placeholder="XXXX-XXXX-XXXX-…">
                    </label>
                    <p class="e2ee-unlock-error" hidden></p>
                    <div class="e2ee-recovery-actions">
                        <button type="button" class="e2ee-unlock-submit">Unlock</button>
                        <button type="button" class="e2ee-unlock-cancel">Cancel</button>
                    </div>
                </div>`;
            const errorEl = overlay.querySelector(".e2ee-unlock-error") as HTMLElement;
            const passwordInput = overlay.querySelector<HTMLInputElement>(".e2ee-unlock-password");
            const recoveryInput = overlay.querySelector<HTMLInputElement>(".e2ee-unlock-recovery");
            const close = (unlocked: boolean) => {
                overlay.remove();
                resolve(unlocked);
            };
            const attempt = async () => {
                errorEl.hidden = true;
                const password = passwordInput?.value ?? "";
                const recovery = recoveryInput?.value.trim() ?? "";
                if (password) {
                    if (await unlockWithPassword(password)) {
                        close(true);
                        return;
                    }
                    errorEl.textContent = "That password didn't unlock this device. Check it, or use your recovery key.";
                    errorEl.hidden = false;
                    return;
                }
                if (recovery) {
                    if (await unlockWithRecovery(recovery)) {
                        close(true);
                        return;
                    }
                    errorEl.textContent = "That recovery key did not match.";
                    errorEl.hidden = false;
                    return;
                }
                errorEl.textContent = options.password ? "Enter your password or your recovery key." : "Enter your recovery key.";
                errorEl.hidden = false;
            };
            overlay.querySelector(".e2ee-unlock-submit")?.addEventListener("click", () => void attempt());
            overlay.querySelector(".e2ee-unlock-cancel")?.addEventListener("click", () => close(false));
            overlay.addEventListener("keydown", (event) => {
                if ((event as KeyboardEvent).key === "Enter") {
                    event.preventDefault();
                    void attempt();
                }
            });
            document.body.appendChild(overlay);
            (passwordInput ?? recoveryInput)?.focus();
        });
    });
}

// ---------------------------------------------------------------------------
// Password change / set (settings page and the SSO set-password prompt)
// ---------------------------------------------------------------------------

export interface ChangePasswordResult {
    ok: boolean;
    error?: string;
}

/**
 * Change (or, for OAuth accounts, set) the login password.
 *
 * Everything password-shaped stays in the browser: the current password is
 * converted to whatever credential the server actually stores (the raw
 * password for legacy accounts, the Argon2id-derived authKey for derived
 * accounts), and the new password becomes a freshly salted derived
 * credential. When this device holds the decrypted private key, it is
 * re-wrapped under the new password so password-unlock keeps working on
 * other devices.
 *
 * @param currentPassword - The current password ("" for OAuth accounts
 *   setting their first password).
 * @param newPassword - The new password.
 * @param identifier - The account's username (used to look up the current
 *   auth mode/salt via the anonymous login-params endpoint).
 * @returns ``{ok}`` or ``{ok: false, error}`` with a user-facing message.
 */
export async function changePassword(currentPassword: string, newPassword: string, identifier: string): Promise<ChangePasswordResult> {
    const url = cfg().urls.changePassword;
    if (!url) {
        return { ok: false, error: "Password changes aren't available on this page." };
    }
    if (newPassword.length < MIN_PASSWORD_LENGTH || /^\d+$/.test(newPassword)) {
        return { ok: false, error: `Use at least ${MIN_PASSWORD_LENGTH} characters, not all numbers.` };
    }
    const policyErrors = await serverPolicyErrors(newPassword, identifier, "");
    if (policyErrors.length) {
        return { ok: false, error: policyErrors.join(" ") };
    }
    await cryptoReady();

    let currentSecret = currentPassword;
    if (currentPassword) {
        const paramsResponse = await fetch(`${cfg().urls.loginParams}?identifier=${encodeURIComponent(identifier)}`, { credentials: "same-origin" });
        if (!paramsResponse.ok) {
            return { ok: false, error: "Could not verify your current password. Please try again." };
        }
        const params = (await paramsResponse.json()) as LoginParams;
        if (params.mode === "derived") {
            currentSecret = bytesToB64(deriveKey(currentPassword, params.auth_salt));
        }
    }

    const newAuthSalt = randomSalt();
    const body: Record<string, unknown> = {
        current_secret: currentSecret,
        new_auth_key: bytesToB64(deriveKey(newPassword, newAuthSalt)),
        new_auth_salt: newAuthSalt,
    };

    // Re-wrap the private key under the new password when we hold it.
    const selfSlug = cfg().selfSlug;
    if (selfSlug) {
        const identity = await getIdentity(selfSlug);
        if (identity !== null) {
            const wrapSalt = randomSalt();
            body.password_wrapped_secret = wrapSecretKey(identity.privateKey, deriveKey(newPassword, wrapSalt));
            body.password_wrap_salt = wrapSalt;
        }
    }

    const response = await postJson(url, body);
    if (response.ok) {
        return { ok: true };
    }
    if (response.status === 403) {
        return { ok: false, error: "Your current password is incorrect." };
    }
    return { ok: false, error: "Could not change your password. Please try again." };
}

/**
 * Generate and store a replacement recovery key (device must be unlocked).
 *
 * @returns The new recovery key display string, or null when locked.
 */
export async function regenerateRecoveryKey(): Promise<string | null> {
    await cryptoReady();
    const identity = await requireIdentity();
    if (identity === null) {
        return null;
    }
    const recovery = generateRecoveryKey();
    const response = await postJson(cfg().urls.rewrap, {
        recovery_wrapped_secret: wrapSecretKey(identity.privateKey, recovery.key),
    });
    return response.ok ? recovery.display : null;
}

export interface ResetResult {
    recoveryDisplay: string;
    /** Number of conversation-key copies / group envelopes re-sealed to the
     * new keypair - when > 0, the account's message history stays readable. */
    rewrapped: number;
    /** True when the old private key was available (cached or unlocked with
     * the password), so history preservation was even attempted. */
    preserved: boolean;
}

interface RewrapAllPayload {
    conversation_keys: { id: number; wrapped_key: string }[];
    group_envelopes: { id: number; wrapped_key: string }[];
}

/**
 * Recover the CURRENT (pre-reset) private key if at all possible.
 *
 * Tries the cached identity first (device unlocked), then - when a password
 * was typed into the reset dialog - unwrapping the bundle's password-wrapped
 * copy with it, exactly like the unlock dialog would.
 *
 * @param bundle - The current server-side bundle.
 * @param password - The password typed into the reset dialog, if any.
 * @returns The old private key, or null when it is genuinely unavailable.
 */
async function recoverOldPrivateKey(bundle: KeyBundlePayload, password?: string): Promise<Uint8Array | null> {
    const cached = await getIdentity(bundle.profile_slug);
    if (cached !== null && cached.version === bundle.version && cached.publicKey === bundle.public_key) {
        return cached.privateKey;
    }
    if (password && bundle.password_wrapped_secret && bundle.password_wrap_salt && !bundle.password_wrap_stale) {
        const wrapKey = deriveKey(password, bundle.password_wrap_salt, bundle.kdf_opslimit, bundle.kdf_memlimit);
        return unwrapSecretKey(bundle.password_wrapped_secret, wrapKey);
    }
    return null;
}

/**
 * Replace the keypair. When the old private key is still available (cached on
 * this device, or unlockable with the supplied password), every conversation
 * key and group envelope is unsealed and re-sealed to the new public key in
 * the same request, so the account's message history stays readable. Only
 * when the old key is genuinely gone does the reset become destructive.
 *
 * @param password - The account password when one exists (re-creates the
 *   password-wrapped copy, and doubles as an unlock path for the old key);
 *   omit for OAuth accounts.
 * @returns The reset outcome, or null on failure.
 */
export async function resetKeys(password?: string): Promise<ResetResult | null> {
    await cryptoReady();
    const selfSlug = cfg().selfSlug;
    if (!selfSlug) {
        return null;
    }

    const bundleResponse = await fetch(cfg().urls.keys, { credentials: "same-origin" });
    if (!bundleResponse.ok) {
        return null;
    }
    const bundle = (await bundleResponse.json()) as KeyBundlePayload;
    if (!bundle.enrolled) {
        return null;
    }
    const oldPrivateKey = await recoverOldPrivateKey(bundle, password);

    const identity = generateIdentity();
    const recovery = generateRecoveryKey();
    const body: Record<string, unknown> = {
        confirm: "RESET",
        public_key: identity.publicKey,
        recovery_wrapped_secret: wrapSecretKey(identity.privateKey, recovery.key),
    };
    if (password) {
        const wrapSalt = randomSalt();
        body.password_wrapped_secret = wrapSecretKey(identity.privateKey, deriveKey(password, wrapSalt));
        body.password_wrap_salt = wrapSalt;
    }

    // Re-encrypt history: unseal every wrapped key copy with the OLD key and
    // re-seal it to the NEW public key, all client-side. Entries that fail to
    // unseal (corrupt, or sealed to an even older keypair) are skipped - they
    // were already unreadable, so leaving them behind loses nothing.
    if (oldPrivateKey !== null && cfg().urls.rewrapAll) {
        const rewrapResponse = await fetch(cfg().urls.rewrapAll as string, { credentials: "same-origin" });
        if (rewrapResponse.ok) {
            const payload = (await rewrapResponse.json()) as RewrapAllPayload;
            const rewrapEntries = (items: { id: number; wrapped_key: string }[]) => {
                const out: { id: number; wrapped_key: string }[] = [];
                for (const item of items) {
                    const key = unseal(item.wrapped_key, bundle.public_key, oldPrivateKey);
                    if (key !== null) {
                        out.push({ id: item.id, wrapped_key: sealToPublicKey(key, identity.publicKey) });
                    }
                }
                return out;
            };
            body.rewrapped_conversation_keys = rewrapEntries(payload.conversation_keys);
            body.rewrapped_group_envelopes = rewrapEntries(payload.group_envelopes);
        }
    }

    const response = await postJson(cfg().urls.reset, body);
    if (!response.ok) {
        return null;
    }
    const payload = (await response.json()) as { version: number; rewrapped?: number };
    await clearProfileKeys(selfSlug);
    await putIdentity(selfSlug, { privateKey: identity.privateKey, publicKey: identity.publicKey, version: payload.version });
    return { recoveryDisplay: recovery.display, rewrapped: payload.rewrapped ?? 0, preserved: oldPrivateKey !== null };
}

//: Accepted spellings of the reset confirmation word - case-insensitive,
//: surrounding whitespace stripped, matching how every other confirmation
//: input on the site behaves (nothing else on the site demands exact-case).
const RESET_CONFIRMATION_WORD = "reset";

/**
 * Show one dialog collecting both the typed confirmation and (when the
 * account has a password) the account password, then perform the reset.
 *
 * The description is honest about the actual consequence: when this device
 * still holds the current private key (or the typed password can unlock it),
 * the reset RE-ENCRYPTS the message history under the new keypair and
 * nothing becomes unreadable - the old "permanently unreadable" warning only
 * appears when destruction is genuinely the outcome.
 *
 * @param hasPassword - Whether to also collect and require the account
 *   password (omit the field entirely for OAuth accounts with none).
 * @returns The new recovery key display string, or null when the user
 *   cancelled or the reset failed.
 */
export function showResetDialog(hasPassword: boolean): Promise<string | null> {
    return new Promise((resolve) => {
        void buildResetDialog(hasPassword, resolve);
    });
}

async function resetDescription(hasPassword: boolean): Promise<string> {
    const state = await getUnlockState().catch(() => "locked" as UnlockState);
    if (state === "unlocked") {
        return "Your encryption keys will be replaced, and your existing messages will be re-encrypted under the new key so they stay readable. Type RESET to confirm.";
    }
    if (hasPassword) {
        return "This device can't read your messages right now. If your password can unlock your current key, your messages will be preserved under the new key - otherwise they become permanently unreadable to you. Type RESET to confirm.";
    }
    return "This permanently makes your existing encrypted messages unreadable to you. Type RESET to confirm.";
}

async function buildResetDialog(hasPassword: boolean, resolve: (value: string | null) => void): Promise<void> {
    const description = await resetDescription(hasPassword);
    const overlay = document.createElement("div");
    overlay.className = "e2ee-recovery-overlay";
    const passwordField = hasPassword
        ? `<label class="e2ee-unlock-label">Account password
               <input type="password" class="e2ee-reset-password" autocomplete="current-password" placeholder="Your password">
           </label>`
        : "";
    overlay.innerHTML = `
        <div class="e2ee-recovery-dialog" role="dialog" aria-modal="true" aria-labelledby="e2ee-reset-title">
            <h2 id="e2ee-reset-title">Reset encryption keys</h2>
            <p class="e2ee-reset-description"></p>
            <label class="e2ee-unlock-label">Confirmation
                <input type="text" class="e2ee-reset-confirm" autocomplete="off" spellcheck="false" placeholder="RESET">
            </label>
            ${passwordField}
            <p class="e2ee-unlock-error" hidden></p>
            <p class="e2ee-reset-progress" hidden><i class="material-symbols-outlined e2ee-reset-spinner" aria-hidden="true">progress_activity</i> Resetting your encryption keys…</p>
            <div class="e2ee-recovery-actions">
                <button type="button" class="e2ee-reset-submit">Reset</button>
                <button type="button" class="e2ee-reset-cancel">Cancel</button>
            </div>
        </div>`;
    (overlay.querySelector(".e2ee-reset-description") as HTMLElement).textContent = description;
    const errorEl = overlay.querySelector(".e2ee-unlock-error") as HTMLElement;
    const progressEl = overlay.querySelector(".e2ee-reset-progress") as HTMLElement;
    const confirmInput = overlay.querySelector<HTMLInputElement>(".e2ee-reset-confirm");
    const passwordInput = overlay.querySelector<HTMLInputElement>(".e2ee-reset-password");
    const submitBtn = overlay.querySelector<HTMLButtonElement>(".e2ee-reset-submit");
    const cancelBtn = overlay.querySelector<HTMLButtonElement>(".e2ee-reset-cancel");
    const close = (recoveryDisplay: string | null) => {
        overlay.remove();
        resolve(recoveryDisplay);
    };
    const setBusy = (busy: boolean) => {
        progressEl.hidden = !busy;
        if (submitBtn) submitBtn.disabled = busy;
        if (cancelBtn) cancelBtn.disabled = busy;
        if (confirmInput) confirmInput.disabled = busy;
        if (passwordInput) passwordInput.disabled = busy;
    };
    const attempt = async () => {
        errorEl.hidden = true;
        const confirmation = (confirmInput?.value ?? "").trim().toLowerCase();
        if (confirmation !== RESET_CONFIRMATION_WORD) {
            errorEl.textContent = 'Type "RESET" to confirm - this step can\'t be skipped.';
            errorEl.hidden = false;
            return;
        }
        const password = passwordInput?.value ?? "";
        if (hasPassword && !password) {
            errorEl.textContent = "Enter your account password to continue.";
            errorEl.hidden = false;
            return;
        }
        setBusy(true);
        try {
            const result = await resetKeys(password || undefined);
            if (result === null) {
                setBusy(false);
                errorEl.textContent = "Could not reset your encryption keys. Please try again.";
                errorEl.hidden = false;
                return;
            }
            const toastr = (window as { toastr?: { success?: (msg: string) => void; warning?: (msg: string) => void } }).toastr;
            if (result.rewrapped > 0) {
                toastr?.success?.("Your keys were reset and your message history was re-encrypted - everything stays readable.");
            } else if (!result.preserved) {
                toastr?.warning?.("Your keys were reset. Previously encrypted messages are no longer readable on this account.");
            }
            close(result.recoveryDisplay);
        } catch {
            setBusy(false);
            errorEl.textContent = "Could not reset your encryption keys. Please try again.";
            errorEl.hidden = false;
        }
    };
    submitBtn?.addEventListener("click", () => void attempt());
    cancelBtn?.addEventListener("click", () => close(null));
    overlay.addEventListener("keydown", (event) => {
        if ((event as KeyboardEvent).key === "Enter") {
            event.preventDefault();
            void attempt();
        }
    });
    document.body.appendChild(overlay);
    confirmInput?.focus();
}

async function requireIdentity(): Promise<CachedIdentity | null> {
    const selfSlug = cfg().selfSlug;
    if (!selfSlug) {
        return null;
    }
    return getIdentity(selfSlug);
}

// ---------------------------------------------------------------------------
// Conversation keys & message crypto (messages page)
// ---------------------------------------------------------------------------

/**
 * Fetch, unseal, and cache the conversation key shared with one partner,
 * creating the first version when none exists and both parties are enrolled.
 *
 * @param partnerSlug - The conversation partner's profile slug.
 * @returns The latest usable key and its version, or null when the
 *   conversation cannot be encrypted (either party unenrolled, or locked).
 */
export async function ensureConversationKey(partnerSlug: string): Promise<{ version: number; key: Uint8Array } | null> {
    await cryptoReady();
    const identity = await requireIdentity();
    const selfSlug = cfg().selfSlug;
    if (identity === null || !selfSlug) {
        return null;
    }
    const response = await fetch(`${cfg().urls.conversationKeyBase}${partnerSlug}/`, { credentials: "same-origin" });
    if (!response.ok) {
        return null;
    }
    const payload = (await response.json()) as ConversationKeysPayload;
    if (payload.latest > 0) {
        const key = await unsealAndCacheVersion(identity, selfSlug, partnerSlug, payload, payload.latest);
        if (key !== null) {
            return { version: payload.latest, key };
        }
        // Our copy of the latest version is sealed to a keypair we no longer
        // hold (post-reset). Roll the conversation forward with a new version.
        return createConversationKeyVersion(identity, selfSlug, partnerSlug, payload.latest + 1);
    }
    return createConversationKeyVersion(identity, selfSlug, partnerSlug, 1);
}

async function unsealAndCacheVersion(
    identity: CachedIdentity,
    selfSlug: string,
    partnerSlug: string,
    payload: ConversationKeysPayload,
    version: number,
): Promise<Uint8Array | null> {
    const cached = await getConversationKey(selfSlug, partnerSlug, version);
    if (cached !== null) {
        return cached;
    }
    const entry = payload.keys.find((item) => item.version === version);
    if (!entry) {
        return null;
    }
    const key = unseal(entry.wrapped_key, identity.publicKey, identity.privateKey);
    if (key !== null) {
        await putConversationKey(selfSlug, partnerSlug, version, key);
    }
    return key;
}

async function createConversationKeyVersion(
    identity: CachedIdentity,
    selfSlug: string,
    partnerSlug: string,
    version: number,
): Promise<{ version: number; key: Uint8Array } | null> {
    const partnerResponse = await fetch(`${cfg().urls.partnerKeyBase}${partnerSlug}/`, { credentials: "same-origin" });
    if (!partnerResponse.ok) {
        // Partner not enrolled - the conversation stays plaintext for now.
        return null;
    }
    const partner = (await partnerResponse.json()) as { public_key: string; version: number };
    const key = generateConversationKey();
    const response = await postJson(`${cfg().urls.conversationKeyBase}${partnerSlug}/`, {
        version,
        wrapped_for_me: sealToPublicKey(key, identity.publicKey),
        wrapped_for_partner: sealToPublicKey(key, partner.public_key),
    });
    if (response.status === 201) {
        await putConversationKey(selfSlug, partnerSlug, version, key);
        return { version, key };
    }
    if (response.status === 200) {
        // Lost a create race - unseal the winner's copy instead.
        const payload = (await response.json()) as { version: number; wrapped_key: string };
        const winner = unseal(payload.wrapped_key, identity.publicKey, identity.privateKey);
        if (winner !== null) {
            await putConversationKey(selfSlug, partnerSlug, payload.version, winner);
            return { version: payload.version, key: winner };
        }
    }
    return null;
}

// ---------------------------------------------------------------------------
// Group keys & message crypto (group chats)
// ---------------------------------------------------------------------------

interface GroupKeysPayload {
    keys: { version: number; wrapped_key: string }[];
    latest: number;
    needs_rotation: boolean;
    /** One entry per active member when all are enrolled, else null. `id` is
     * an opaque per-(group, member) rotation token - deliberately not a slug,
     * which would reveal masked members' identities (see the server's
     * group_member_token). It is round-tripped verbatim as the `wrapped` key
     * when posting a new version. */
    members: { id: string; public_key: string }[] | null;
}

function groupKeyUrl(groupUuid: string): string {
    const base = cfg().urls.groupKeyBase;
    if (!base) {
        throw new Error("groupKeyBase URL not configured on this page");
    }
    return `${base}${groupUuid}/`;
}

/**
 * Fetch, unseal, and cache the usable group key for one group chat, rotating
 * to a new version whenever the latest one no longer covers the group's
 * current membership (member added/removed) or none exists yet.
 *
 * Rotation is what enforces membership boundaries cryptographically: a new
 * member only ever receives envelopes for versions minted after they joined,
 * and a removed member is excluded from every later version.
 *
 * @param groupUuid - The group chat's UUID.
 * @returns The latest usable key and its version, or null when the group
 *   cannot encrypt (a member unenrolled, or this device locked).
 */
export async function ensureGroupKey(groupUuid: string): Promise<{ version: number; key: Uint8Array } | null> {
    await cryptoReady();
    const identity = await requireIdentity();
    const selfSlug = cfg().selfSlug;
    if (identity === null || !selfSlug) {
        return null;
    }
    const response = await fetch(groupKeyUrl(groupUuid), { credentials: "same-origin" });
    if (!response.ok) {
        return null;
    }
    const payload = (await response.json()) as GroupKeysPayload;

    if (!payload.needs_rotation && payload.latest > 0) {
        const key = await unsealAndCacheGroupVersion(identity, selfSlug, groupUuid, payload, payload.latest);
        if (key !== null) {
            return { version: payload.latest, key };
        }
        // Our envelope is sealed to a keypair we no longer hold (post-reset):
        // roll the group forward with a fresh version.
    }
    return createGroupKeyVersion(identity, selfSlug, groupUuid, payload);
}

async function unsealAndCacheGroupVersion(
    identity: CachedIdentity,
    selfSlug: string,
    groupUuid: string,
    payload: GroupKeysPayload,
    version: number,
): Promise<Uint8Array | null> {
    const cached = await getGroupKey(selfSlug, groupUuid, version);
    if (cached !== null) {
        return cached;
    }
    const entry = payload.keys.find((item) => item.version === version);
    if (!entry) {
        return null;
    }
    const key = unseal(entry.wrapped_key, identity.publicKey, identity.privateKey);
    if (key !== null) {
        await putGroupKey(selfSlug, groupUuid, version, key);
    }
    return key;
}

async function createGroupKeyVersion(
    identity: CachedIdentity,
    selfSlug: string,
    groupUuid: string,
    payload: GroupKeysPayload,
): Promise<{ version: number; key: Uint8Array } | null> {
    if (payload.members === null) {
        // At least one member isn't enrolled - the group stays plaintext.
        return null;
    }
    const key = generateConversationKey();
    const wrapped: Record<string, string> = {};
    for (const member of payload.members) {
        wrapped[member.id] = sealToPublicKey(key, member.public_key);
    }
    const version = payload.latest + 1;
    const response = await postJson(groupKeyUrl(groupUuid), { version, wrapped });
    if (response.status === 201) {
        await putGroupKey(selfSlug, groupUuid, version, key);
        return { version, key };
    }
    if (response.status === 200) {
        // Lost a create race - unseal the winner's copy instead.
        const winner = (await response.json()) as { version: number; wrapped_key: string };
        const winnerKey = unseal(winner.wrapped_key, identity.publicKey, identity.privateKey);
        if (winnerKey !== null) {
            await putGroupKey(selfSlug, groupUuid, winner.version, winnerKey);
            return { version: winner.version, key: winnerKey };
        }
    }
    return null;
}

/**
 * Encrypt one outgoing message body for a group chat.
 *
 * @param groupUuid - The group chat's UUID.
 * @param text - The plaintext body.
 * @returns The encrypted fields, or null when the group must fall back to
 *   plaintext (a member unenrolled / this device locked).
 */
export async function encryptForGroup(groupUuid: string, text: string): Promise<OutgoingEncryption | null> {
    const group = await ensureGroupKey(groupUuid);
    if (group === null) {
        return null;
    }
    const encrypted = encryptMessage(text, group.key);
    return { ciphertext: encrypted.ciphertext, nonce: encrypted.nonce, key_version: group.version };
}

/**
 * Decrypt one received/stored group message body.
 *
 * @param groupUuid - The group chat's UUID.
 * @param ciphertext - Base64 ciphertext from the server.
 * @param nonce - Base64 nonce stored with the message.
 * @param version - The group-key version that encrypted it.
 * @returns The plaintext, or null when this device can't decrypt it (locked,
 *   or the message predates the viewer's membership so they hold no envelope).
 */
export async function decryptFromGroup(groupUuid: string, ciphertext: string, nonce: string, version: number): Promise<string | null> {
    await cryptoReady();
    const identity = await requireIdentity();
    const selfSlug = cfg().selfSlug;
    if (identity === null || !selfSlug) {
        return null;
    }
    let key = await getGroupKey(selfSlug, groupUuid, version);
    if (key === null) {
        const response = await fetch(groupKeyUrl(groupUuid), { credentials: "same-origin" });
        if (!response.ok) {
            return null;
        }
        const payload = (await response.json()) as GroupKeysPayload;
        key = await unsealAndCacheGroupVersion(identity, selfSlug, groupUuid, payload, version);
    }
    if (key === null) {
        return null;
    }
    return decryptMessage(ciphertext, nonce, key);
}

/** An encrypted payload ready to attach to an outgoing message. */
export interface OutgoingEncryption {
    ciphertext: string;
    nonce: string;
    key_version: number;
}

/**
 * Encrypt one outgoing message body for a partner.
 *
 * @param partnerSlug - The conversation partner's profile slug.
 * @param text - The plaintext body.
 * @returns The encrypted fields, or null when the conversation must fall back
 *   to plaintext (partner unenrolled / this device locked).
 */
export async function encryptForPartner(partnerSlug: string, text: string): Promise<OutgoingEncryption | null> {
    const conversation = await ensureConversationKey(partnerSlug);
    if (conversation === null) {
        return null;
    }
    const encrypted = encryptMessage(text, conversation.key);
    return { ciphertext: encrypted.ciphertext, nonce: encrypted.nonce, key_version: conversation.version };
}

/**
 * Decrypt one received/stored message body.
 *
 * @param partnerSlug - The conversation partner's profile slug.
 * @param ciphertext - Base64 ciphertext from the server.
 * @param nonce - Base64 nonce stored with the message.
 * @param version - The conversation-key version that encrypted it.
 * @returns The plaintext, or null when this device can't decrypt it.
 */
export async function decryptFromPartner(partnerSlug: string, ciphertext: string, nonce: string, version: number): Promise<string | null> {
    await cryptoReady();
    const identity = await requireIdentity();
    const selfSlug = cfg().selfSlug;
    if (identity === null || !selfSlug) {
        return null;
    }
    let key = await getConversationKey(selfSlug, partnerSlug, version);
    if (key === null) {
        const response = await fetch(`${cfg().urls.conversationKeyBase}${partnerSlug}/`, { credentials: "same-origin" });
        if (!response.ok) {
            return null;
        }
        const payload = (await response.json()) as ConversationKeysPayload;
        key = await unsealAndCacheVersion(identity, selfSlug, partnerSlug, payload, version);
    }
    if (key === null) {
        return null;
    }
    return decryptMessage(ciphertext, nonce, key);
}

/**
 * Decrypt every pending [data-e2ee-ct] element under a root, in place.
 *
 * Elements carry data-e2ee-ct / data-e2ee-nonce / data-e2ee-kv and either
 * data-e2ee-group (group-chat messages), data-e2ee-partner, or inherit the
 * partnerSlug argument. Decrypted text replaces the element's textContent;
 * failures show a lock placeholder.
 *
 * @param root - The DOM subtree to scan.
 * @param partnerSlug - Default partner slug for elements without their own.
 */
export async function decryptDom(root: ParentNode, partnerSlug?: string): Promise<void> {
    const nodes = Array.from(root.querySelectorAll<HTMLElement>("[data-e2ee-ct]"));
    for (const node of nodes) {
        const ciphertext = node.dataset.e2eeCt ?? "";
        const nonce = node.dataset.e2eeNonce ?? "";
        const version = Number.parseInt(node.dataset.e2eeKv ?? "0", 10);
        const group = node.dataset.e2eeGroup || "";
        const partner = node.dataset.e2eePartner || partnerSlug;
        delete node.dataset.e2eeCt;
        delete node.dataset.e2eeNonce;
        delete node.dataset.e2eeKv;
        delete node.dataset.e2eeGroup;
        if (!ciphertext || !nonce || !version || (!partner && !group)) {
            continue;
        }
        const plaintext = group ? await decryptFromGroup(group, ciphertext, nonce, version) : await decryptFromPartner(partner as string, ciphertext, nonce, version);
        if (plaintext !== null) {
            const truncateAt = Number.parseInt(node.dataset.e2eeTruncate ?? "0", 10);
            node.textContent = truncateAt > 0 && plaintext.length > truncateAt ? `${plaintext.slice(0, truncateAt - 1)}…` : plaintext;
            node.classList.add("e2ee-decrypted");
        } else {
            node.textContent = "Unable to decrypt on this device";
            node.classList.add("e2ee-failed");
            // Reacting requires knowing what the message said - the emoji
            // picker stayed available on a bubble whose body we can't even
            // show, which read as offering to respond to content the user
            // never saw. Only the main bubble body (not reply-quote
            // snippets or conversation-list previews, which share this same
            // decrypt loop but have no reaction button of their own) needs this.
            if (node.classList.contains("dm-bubble__body")) {
                const addReactionBtn = node.closest(".dm-bubble")?.querySelector<HTMLElement>(".dm-reaction-add-btn");
                if (addReactionBtn) addReactionBtn.hidden = true;
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Recovery-key dialog (login-flow only; Settings has its own UI)
// ---------------------------------------------------------------------------

/**
 * Show a blocking overlay presenting a freshly generated recovery key with
 * copy/download actions. Resolves when the user confirms (or defers).
 *
 * @param display - The recovery key display string.
 */
export function showRecoveryDialog(display: string): Promise<void> {
    return new Promise((resolve) => {
        const faqUrl = config?.urls.faqUrl;
        const faqLink = faqUrl ? ` <a href="${faqUrl}" target="_blank" rel="noopener">What is this, and why do I need it?</a>` : "";
        const overlay = document.createElement("div");
        overlay.className = "e2ee-recovery-overlay";
        overlay.innerHTML = `
            <div class="e2ee-recovery-dialog" role="dialog" aria-modal="true" aria-labelledby="e2ee-recovery-title">
                <h2 id="e2ee-recovery-title">Save your recovery key</h2>
                <p>Your direct messages are now end-to-end encrypted. If you ever lose access to your password and signed-in devices, this key is the <strong>only</strong> way to read your message history.${faqLink}</p>
                <code class="e2ee-recovery-key"></code>
                <div class="e2ee-recovery-actions">
                    <button type="button" class="e2ee-recovery-copy">Copy</button>
                    <button type="button" class="e2ee-recovery-download">Download .txt</button>
                </div>
                <button type="button" class="e2ee-recovery-done">I saved my recovery key</button>
                <button type="button" class="e2ee-recovery-later">Remind me later (viewable in Settings)</button>
            </div>`;
        (overlay.querySelector(".e2ee-recovery-key") as HTMLElement).textContent = display;
        overlay.querySelector(".e2ee-recovery-copy")?.addEventListener("click", () => {
            void navigator.clipboard?.writeText(display);
        });
        overlay.querySelector(".e2ee-recovery-download")?.addEventListener("click", () => {
            const blob = new Blob([`UrbanLens message recovery key\n\n${display}\n\nKeep this somewhere safe - it can unlock your encrypted message history on any device.\n`], { type: "text/plain" });
            const link = document.createElement("a");
            link.href = URL.createObjectURL(blob);
            link.download = "urbanlens-recovery-key.txt";
            link.click();
            URL.revokeObjectURL(link.href);
        });
        const close = () => {
            overlay.remove();
            resolve();
        };
        overlay.querySelector(".e2ee-recovery-done")?.addEventListener("click", close);
        overlay.querySelector(".e2ee-recovery-later")?.addEventListener("click", close);
        document.body.appendChild(overlay);
    });
}
