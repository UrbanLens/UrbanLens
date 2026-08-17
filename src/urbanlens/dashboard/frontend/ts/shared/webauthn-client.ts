/**
 * Passkey (WebAuthn) registration and login-time authentication ceremonies.
 *
 * Mirrors e2ee-client.ts's shape: this module owns every fetch to the
 * passkey endpoints and every navigator.credentials.* call; templates only
 * wire DOM events to the functions exposed on window.UrbanLensWebAuthn (see
 * entries-classic/webauthn.ts). The option/verification JSON shapes are the
 * standard WebAuthn base64url encoding produced by py_webauthn's
 * options_to_json() and expected by its verify_*_response() (see
 * services/webauthn.py) - hand-rolled here (rather than relying on
 * PublicKeyCredential.parseCreationOptionsFromJSON()/toJSON()) for broader
 * browser support.
 */

function base64urlToBuffer(value: string): ArrayBuffer {
    const padded = value.replace(/-/g, "+").replace(/_/g, "/");
    const padding = "=".repeat((4 - (padded.length % 4)) % 4);
    const raw = atob(padded + padding);
    const bytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) {
        bytes[i] = raw.charCodeAt(i);
    }
    return bytes.buffer;
}

function bufferToBase64url(buffer: ArrayBuffer): string {
    const bytes = new Uint8Array(buffer);
    let binary = "";
    for (let i = 0; i < bytes.byteLength; i++) {
        binary += String.fromCharCode(bytes[i]!);
    }
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function csrfToken(): string {
    // window.csrftoken is set by a page-level <script> on most dashboard pages,
    // but not on the minimal auth_base.html layout the 2FA login challenge
    // (login_2fa.html) uses - falling back to the csrftoken cookie directly
    // (Django's own documented AJAX pattern) means this works regardless of
    // which layout the calling page happens to use. Without this fallback,
    // runLogin()'s fetch calls here sent an empty X-CSRFToken header on that
    // page, which Django's CSRF middleware always rejected with a 403 HTML
    // error page - silently swallowed by safeJson() into the generic
    // "Could not start passkey sign-in." message, since there was no JSON
    // body to read an error out of.
    if (window.csrftoken) return window.csrftoken;
    const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]!) : "";
}

interface CredentialDescriptorJSON {
    id: string;
    type: string;
    transports?: AuthenticatorTransport[];
}

/** The `prf` extension in wire form: base64url inputs, exactly the shape
 * WebAuthn Level 3's parse*OptionsFromJSON uses and the server's
 * `_with_prf_extension` (services/auth/webauthn.py) emits. */
interface PrfExtensionJSON {
    eval?: { first: string; second?: string };
    evalByCredential?: Record<string, { first: string; second?: string }>;
}

interface ExtensionsJSON {
    prf?: PrfExtensionJSON;
}

interface RegistrationOptionsJSON {
    rp: { id?: string; name: string };
    user: { id: string; name: string; displayName: string };
    challenge: string;
    pubKeyCredParams: PublicKeyCredentialParameters[];
    timeout?: number;
    excludeCredentials?: CredentialDescriptorJSON[];
    authenticatorSelection?: AuthenticatorSelectionCriteria;
    attestation?: AttestationConveyancePreference;
    extensions?: ExtensionsJSON;
}

interface AuthenticationOptionsJSON {
    challenge: string;
    timeout?: number;
    rpId?: string;
    allowCredentials?: CredentialDescriptorJSON[];
    userVerification?: UserVerificationRequirement;
    extensions?: ExtensionsJSON;
}

/** Convert wire-form extensions (base64url strings) to the BufferSources the
 * browser API wants. Only `prf` is understood; unknown extensions are dropped
 * rather than passed through malformed. */
function extensionsFromJSON(json: ExtensionsJSON | undefined): AuthenticationExtensionsClientInputs | undefined {
    const prf = json?.prf;
    if (!prf) {
        return undefined;
    }
    const converted: { eval?: { first: BufferSource }; evalByCredential?: Record<string, { first: BufferSource }> } = {};
    if (prf.eval) {
        converted.eval = { first: base64urlToBuffer(prf.eval.first) };
    }
    if (prf.evalByCredential) {
        converted.evalByCredential = {};
        for (const [credId, values] of Object.entries(prf.evalByCredential)) {
            converted.evalByCredential[credId] = { first: base64urlToBuffer(values.first) };
        }
    }
    // Empty {} is meaningful on registration: it asks the authenticator to
    // enable PRF so later assertions can evaluate it.
    return { prf: converted } as AuthenticationExtensionsClientInputs;
}

function creationOptionsFromJSON(json: RegistrationOptionsJSON): CredentialCreationOptions {
    return {
        publicKey: {
            rp: json.rp,
            user: {
                id: base64urlToBuffer(json.user.id),
                name: json.user.name,
                displayName: json.user.displayName,
            },
            challenge: base64urlToBuffer(json.challenge),
            pubKeyCredParams: json.pubKeyCredParams,
            timeout: json.timeout,
            excludeCredentials: (json.excludeCredentials ?? []).map((cred) => ({
                id: base64urlToBuffer(cred.id),
                type: "public-key" as const,
                transports: cred.transports,
            })),
            authenticatorSelection: json.authenticatorSelection,
            attestation: json.attestation,
            extensions: extensionsFromJSON(json.extensions),
        },
    };
}

function requestOptionsFromJSON(json: AuthenticationOptionsJSON): CredentialRequestOptions {
    return {
        publicKey: {
            challenge: base64urlToBuffer(json.challenge),
            timeout: json.timeout,
            rpId: json.rpId,
            allowCredentials: (json.allowCredentials ?? []).map((cred) => ({
                id: base64urlToBuffer(cred.id),
                type: "public-key" as const,
                transports: cred.transports,
            })),
            userVerification: json.userVerification,
            extensions: extensionsFromJSON(json.extensions),
        },
    };
}

/** The base64url rawId of a credential - the id every wrap and wire payload keys on. */
export function credentialIdOf(credential: PublicKeyCredential): string {
    return bufferToBase64url(credential.rawId);
}

/** Shape of getClientExtensionResults().prf, absent from lib.dom.d.ts. */
interface PrfExtensionResults {
    enabled?: boolean;
    results?: { first?: ArrayBuffer | Uint8Array; second?: ArrayBuffer | Uint8Array };
}

/**
 * Extract the prf extension's first evaluation result from a credential.
 *
 * @param credential - The credential returned by create()/get().
 * @returns The 32-byte PRF output, or null when the authenticator did not
 *   evaluate the extension (unsupported, or no input was supplied).
 */
export function getPrfResult(credential: PublicKeyCredential): Uint8Array | null {
    const results = (credential.getClientExtensionResults() as { prf?: PrfExtensionResults }).prf?.results;
    const first = results?.first;
    if (!first) {
        return null;
    }
    return first instanceof Uint8Array ? first : new Uint8Array(first);
}

/**
 * Report whether a registration enabled PRF on the new credential.
 *
 * @param credential - The credential returned by create().
 * @returns True when the authenticator confirmed PRF support.
 */
export function prfEnabled(credential: PublicKeyCredential): boolean {
    return Boolean((credential.getClientExtensionResults() as { prf?: PrfExtensionResults }).prf?.enabled);
}

/** What a PRF assertion produced: a usable output, an authenticator that
 * cannot do PRF at all, or nothing (cancelled, errored, or unsupported). */
export type PrfAssertionResult = { status: "ok"; credentialId: string; prf: Uint8Array } | { status: "no-prf" } | { status: "unavailable" };

/**
 * Run a client-challenged assertion purely to evaluate the PRF extension.
 *
 * The signature is discarded and nothing is sent to the server - the PRF
 * output is the entire point (it authenticates nothing; the wrapped blob it
 * opens is already served to any authenticated session, exactly like
 * password_wrapped_secret). The challenge is random-local because nobody
 * verifies it.
 *
 * The two failure cases are kept apart because callers act on them
 * differently: an authenticator that answered but has no PRF means "this key
 * can never unlock, offer another route", while a cancel or error means "the
 * user did not choose anything, leave them where they were".
 *
 * @param evalByCredential - base64url credential id -> base64url PRF input.
 * @returns The credential id used and its PRF output, or why it produced none.
 */
export async function assertForPrf(evalByCredential: Record<string, string>): Promise<PrfAssertionResult> {
    if (!window.PublicKeyCredential) {
        return { status: "unavailable" };
    }
    const challenge = new Uint8Array(32);
    crypto.getRandomValues(challenge);
    try {
        const credential = (await navigator.credentials.get({
            publicKey: {
                challenge,
                allowCredentials: Object.keys(evalByCredential).map((id) => ({ id: base64urlToBuffer(id), type: "public-key" as const })),
                userVerification: "preferred",
                extensions: extensionsFromJSON({ prf: { evalByCredential: Object.fromEntries(Object.entries(evalByCredential).map(([id, input]) => [id, { first: input }])) } }),
            },
        })) as PublicKeyCredential | null;
        if (!credential) {
            return { status: "unavailable" };
        }
        const prf = getPrfResult(credential);
        if (!prf) {
            return { status: "no-prf" };
        }
        return { status: "ok", credentialId: bufferToBase64url(credential.rawId), prf };
    } catch {
        return { status: "unavailable" };
    }
}

function credentialToJSON(credential: PublicKeyCredential): Record<string, unknown> {
    const base = {
        id: credential.id,
        rawId: bufferToBase64url(credential.rawId),
        type: credential.type,
        authenticatorAttachment: credential.authenticatorAttachment ?? undefined,
    };
    const response = credential.response;
    if (response instanceof AuthenticatorAttestationResponse) {
        return {
            ...base,
            response: {
                clientDataJSON: bufferToBase64url(response.clientDataJSON),
                attestationObject: bufferToBase64url(response.attestationObject),
                transports: response.getTransports ? response.getTransports() : undefined,
            },
        };
    }
    const assertion = response as AuthenticatorAssertionResponse;
    return {
        ...base,
        response: {
            clientDataJSON: bufferToBase64url(assertion.clientDataJSON),
            authenticatorData: bufferToBase64url(assertion.authenticatorData),
            signature: bufferToBase64url(assertion.signature),
            userHandle: assertion.userHandle ? bufferToBase64url(assertion.userHandle) : undefined,
        },
    };
}

async function safeJson(response: Response): Promise<{ error?: string; [key: string]: unknown }> {
    try {
        return await response.json();
    } catch {
        return {};
    }
}

function isCancellation(err: unknown): boolean {
    return err instanceof DOMException && err.name === "NotAllowedError";
}

// ---------------------------------------------------------------------------
// Registration (Settings > Security)
// ---------------------------------------------------------------------------

export interface RegisterConfig {
    optionsUrl: string;
    registerUrl: string;
    /** Optional nickname to save. Left blank, the server auto-generates one (e.g. "Passkey 2") - see webauthn.py. */
    name?: string;
    /** "unlock" registers an E2EE-unlock-only key (is_login_factor=False server-side). */
    purpose?: "unlock";
    /** base64 PRF input to evaluate during creation, when the caller intends
     * to wrap a key under this credential. Some browsers return the result at
     * create time, saving the follow-up assertion; others only enable PRF and
     * the caller falls back to assertForPrf(). */
    prfInput?: string;
}

export interface WebAuthnResult {
    ok: boolean;
    error?: string;
    /** base64url rawId of the new credential (registration only). */
    credentialId?: string;
    /** Database id of the new credential, for undoing a registration that
     * turns out to be unusable (registration only). */
    credentialPk?: number;
    /** PRF output when the browser evaluated it at create time. */
    prf?: Uint8Array | null;
    /** Whether the authenticator reports PRF support for this credential. */
    prfEnabled?: boolean;
}

export async function registerPasskey(cfg: RegisterConfig): Promise<WebAuthnResult> {
    if (!window.PublicKeyCredential) {
        return { ok: false, error: "This browser doesn't support passkeys." };
    }
    try {
        const optionsResp = await fetch(cfg.optionsUrl, {
            method: "POST",
            headers: { "X-CSRFToken": csrfToken() },
            credentials: "same-origin",
        });
        if (!optionsResp.ok) {
            const body = await safeJson(optionsResp);
            return { ok: false, error: body.error ?? "Could not start passkey registration." };
        }
        const optionsJson = (await optionsResp.json()) as RegistrationOptionsJSON;
        if (cfg.prfInput) {
            optionsJson.extensions = { ...optionsJson.extensions, prf: { eval: { first: cfg.prfInput } } };
        }
        const credential = (await navigator.credentials.create(creationOptionsFromJSON(optionsJson))) as PublicKeyCredential | null;
        if (!credential) {
            return { ok: false, error: "Passkey creation was cancelled." };
        }

        const name = cfg.name ?? "";
        const form = new URLSearchParams();
        form.set("credential", JSON.stringify(credentialToJSON(credential)));
        form.set("name", name);
        if (cfg.purpose) {
            form.set("purpose", cfg.purpose);
        }
        const completeResp = await fetch(cfg.registerUrl, {
            method: "POST",
            headers: { "Content-Type": "application/x-www-form-urlencoded", "X-CSRFToken": csrfToken() },
            credentials: "same-origin",
            body: form.toString(),
        });
        const completeBody = await safeJson(completeResp);
        if (!completeResp.ok) {
            return { ok: false, error: completeBody.error ?? "That passkey could not be saved." };
        }
        return {
            ok: true,
            credentialId: bufferToBase64url(credential.rawId),
            credentialPk: typeof completeBody.id === "number" ? completeBody.id : undefined,
            prf: getPrfResult(credential),
            prfEnabled: prfEnabled(credential),
        };
    } catch (err) {
        return { ok: false, error: isCancellation(err) ? "Passkey creation was cancelled." : "Something went wrong creating that passkey." };
    }
}

// ---------------------------------------------------------------------------
// Login-time authentication (accounts/login/2fa/)
// ---------------------------------------------------------------------------

export interface LoginConfig {
    optionsUrl: string;
    verifyUrl: string;
    retryButtonId: string;
    statusElId: string;
    /** Runs after the server accepts the assertion (session established) and
     * before the redirect. The 2FA options may carry PRF inputs for wrap-bearing
     * credentials (see services/auth/webauthn.py), so this is where the E2EE
     * layer harvests the PRF output and unlocks - one tap does both jobs.
     * Failures are swallowed: key handling must never block getting the user
     * into the app. */
    beforeRedirect?: (credential: PublicKeyCredential) => Promise<void>;
}

export function runLogin(cfg: LoginConfig): void {
    const statusEl = document.getElementById(cfg.statusElId);
    const retryBtn = document.getElementById(cfg.retryButtonId) as HTMLButtonElement | null;

    function setStatus(text: string): void {
        if (!statusEl) return;
        statusEl.textContent = text;
        statusEl.hidden = !text;
    }

    async function attempt(): Promise<void> {
        if (!window.PublicKeyCredential) {
            setStatus("This browser doesn't support passkeys. Try a different device or browser.");
            return;
        }
        if (retryBtn) retryBtn.disabled = true;
        setStatus("");
        try {
            const optionsResp = await fetch(cfg.optionsUrl, {
                method: "POST",
                headers: { "X-CSRFToken": csrfToken() },
                credentials: "same-origin",
            });
            if (!optionsResp.ok) {
                const body = await safeJson(optionsResp);
                setStatus(body.error ?? "Could not start passkey sign-in.");
                return;
            }
            const optionsJson = (await optionsResp.json()) as AuthenticationOptionsJSON;
            const credential = (await navigator.credentials.get(requestOptionsFromJSON(optionsJson))) as PublicKeyCredential | null;
            if (!credential) {
                setStatus("Passkey sign-in was cancelled.");
                return;
            }
            const verifyResp = await fetch(cfg.verifyUrl, {
                method: "POST",
                headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
                credentials: "same-origin",
                body: JSON.stringify(credentialToJSON(credential)),
            });
            const verifyBody = await safeJson(verifyResp);
            if (!verifyResp.ok) {
                setStatus((verifyBody.error as string | undefined) ?? "That passkey could not be verified.");
                return;
            }
            if (cfg.beforeRedirect) {
                try {
                    await cfg.beforeRedirect(credential);
                } catch {
                    // E2EE unlock is best-effort here; the login itself succeeded.
                }
            }
            window.location.href = (verifyBody.redirect as string | undefined) || "/";
        } catch (err) {
            setStatus(isCancellation(err) ? "Passkey sign-in was cancelled." : "Something went wrong verifying that passkey.");
        } finally {
            if (retryBtn) retryBtn.disabled = false;
        }
    }

    retryBtn?.addEventListener("click", () => {
        void attempt();
    });
    void attempt();
}
