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
import { clearProfileKeys, getConversationKey, getIdentity, putConversationKey, putIdentity } from "./e2ee-store";

/** Endpoint URLs, provided by templates via {% url %} (see init()). */
export interface E2EEUrls {
    loginParams: string;
    enroll: string;
    keys: string;
    rewrap: string;
    reset: string;
    /** Base of the partner-key endpoint; the client appends "<slug>/". */
    partnerKeyBase: string;
    /** Base of the conversation-key endpoint; the client appends "<slug>/". */
    conversationKeyBase: string;
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

interface KeyBundlePayload {
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
    if (keysResponse.status === 404) {
        // AccountKdf exists (signup created it) but no bundle yet - finish
        // enrollment now that we're authenticated.
        const result = await enroll({ password, rotateAuth: false });
        if (result) {
            await showRecoveryDialog(result.recoveryDisplay);
        }
        return;
    }
    if (!keysResponse.ok) {
        return;
    }
    const bundle = (await keysResponse.json()) as KeyBundlePayload;

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

/** Django's default minimum password length, enforced client-side because the
 * server only ever sees the derived credential (which always "looks strong"). */
const MIN_PASSWORD_LENGTH = 8;

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
        password1.setCustomValidity(`Use at least ${MIN_PASSWORD_LENGTH} characters, not all numbers.`);
        password1.reportValidity();
        password1.addEventListener("input", () => password1.setCustomValidity(""), { once: true });
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
        password1.setCustomValidity(`Use at least ${MIN_PASSWORD_LENGTH} characters, not all numbers.`);
        password1.reportValidity();
        password1.addEventListener("input", () => password1.setCustomValidity(""), { once: true });
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
    if (response.status === 404) {
        return "not-enrolled";
    }
    if (!response.ok) {
        return "locked";
    }
    const bundle = (await response.json()) as KeyBundlePayload;
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
    const privateKey = unwrapSecretKey(bundle.recovery_wrapped_secret, key);
    if (privateKey === null) {
        return false;
    }
    await putIdentity(bundle.profile_slug, { privateKey, publicKey: bundle.public_key, version: bundle.version });
    return true;
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

/**
 * Nuclear option: replace the keypair entirely. Old encrypted messages become
 * permanently unreadable to this account.
 *
 * @param password - The account password when one exists (re-creates the
 *   password-wrapped copy); omit for OAuth accounts.
 * @returns The new recovery key display string, or null on failure.
 */
export async function resetKeys(password?: string): Promise<string | null> {
    await cryptoReady();
    const selfSlug = cfg().selfSlug;
    if (!selfSlug) {
        return null;
    }
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
    const response = await postJson(cfg().urls.reset, body);
    if (!response.ok) {
        return null;
    }
    const payload = (await response.json()) as { version: number };
    await clearProfileKeys(selfSlug);
    await putIdentity(selfSlug, { privateKey: identity.privateKey, publicKey: identity.publicKey, version: payload.version });
    return recovery.display;
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
 * data-e2ee-partner or inherit the partnerSlug argument. Decrypted text
 * replaces the element's textContent; failures show a lock placeholder.
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
        const partner = node.dataset.e2eePartner || partnerSlug;
        delete node.dataset.e2eeCt;
        delete node.dataset.e2eeNonce;
        delete node.dataset.e2eeKv;
        if (!ciphertext || !nonce || !version || !partner) {
            continue;
        }
        const plaintext = await decryptFromPartner(partner, ciphertext, nonce, version);
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
